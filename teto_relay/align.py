"""Forced alignment - measure when each word was actually said.

Whisper derives word timings from attention, and they are systematically off.
Measured against `torchaudio`'s MMS_FA aligner on known speech, every word in
every sentence started **later** than whisper claimed - by 0.06 to 0.20 s,
averaging around 0.12 - and most spans were too long.

Two things depend on those spans, so the error propagates:

* Note length, which decides how much the note has to be stretched.
* Pitch, which is measured over the span - a span that starts early and runs
  long samples silence and the neighbouring word along with the word itself.

The aligner costs about 0.06 s per utterance once loaded, so this is close to
free. The model is ~1.2 GB and downloads on first use.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from .stt import Word

log = logging.getLogger(__name__)

_lock = threading.Lock()
_bundle = None


#: The checkpoint is ~1.26 GB and torch's loader does not survive a failed
#: allocation - it segfaults, taking the process with it and leaving no
#: traceback, which looks exactly like the cuDNN load-order crash. Refuse
#: politely instead.
_NEEDED_BYTES = 1_600_000_000


def _available_memory() -> int | None:
    """Free physical memory in bytes, or None if it cannot be determined."""
    try:
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPhys)
    except Exception:
        return None


#: What loading needs when the checkpoint is memory-mapped (_load_mapped):
#: the weights stream from disk to the device instead of sitting in RAM.
_NEEDED_BYTES_MAPPED = 400_000_000


def _checkpoint():
    """The downloaded MMS_FA checkpoint, wherever torch's hub cache has it."""
    import torch
    from torchaudio.pipelines import MMS_FA

    from . import paths

    name = Path(MMS_FA._path).name
    for hub in (Path(torch.hub.get_dir()), paths.cache_dir() / "torch" / "hub"):
        candidate = hub / "checkpoints" / name
        if candidate.is_file():
            return candidate
    return None


def _load_mapped(path, device):
    """MMS_FA without holding it in RAM: the model is built empty (on the
    `meta` device) and given the checkpoint memory-mapped from disk, then
    moved to `device`. torchaudio's own loader builds the model and reads the
    checkpoint into RAM besides - about 2.5 GB at once, more than this 8 GB
    PC ever had free, so the aligner had never run here."""
    import torch
    from torchaudio.pipelines import MMS_FA
    from torchaudio.pipelines._wav2vec2 import utils

    with torch.device("meta"):
        model = utils._get_model(MMS_FA._model_type, MMS_FA._params)
    state = torch.load(str(path), map_location="cpu", mmap=True, weights_only=True)
    if MMS_FA._remove_aux_axis:
        utils._remove_aux_axes(state, MMS_FA._remove_aux_axis)
    model.load_state_dict(state, assign=True)
    model = utils._extend_model(model, normalize_waveform=MMS_FA._normalize_waveform,
                                apply_log_softmax=True, append_star=True)
    return model.to(device).eval()


def _load(cfg):
    """Load the aligner once. Returns (model, tokenizer, aligner, device)."""
    global _bundle
    with _lock:
        if _bundle is not None:
            return _bundle

        import torch
        from torchaudio.pipelines import MMS_FA

        device = cfg.align_device
        if device.startswith("cuda") and not torch.cuda.is_available():
            log.warning("CUDA not available; aligning on the CPU")
            device = "cpu"

        path = _checkpoint()
        free = _available_memory()
        if path is not None and (free is None or free >= _NEEDED_BYTES_MAPPED):
            log.info("Loading the forced aligner (%s, memory-mapped)...", device)
            model = _load_mapped(path, device)
            _bundle = (model, MMS_FA.get_tokenizer(), MMS_FA.get_aligner(), device)
            log.info("Aligner ready")
            return _bundle

        if free is not None and free < _NEEDED_BYTES:
            raise MemoryError(
                f"only {free/1e9:.1f} GB of RAM free and the aligner needs about "
                f"{_NEEDED_BYTES/1e9:.1f} GB. Close what you can, or turn off "
                "'Measure word timing' - whisper's own timings are ~0.12 s early "
                "but everything still works."
            )

        log.info("Loading the forced aligner (%s)...", device)
        model = MMS_FA.get_model().to(device)
        model.eval()
        _bundle = (model, MMS_FA.get_tokenizer(), MMS_FA.get_aligner(), device)
        log.info("Aligner ready")
        return _bundle


def _emission(audio: np.ndarray, cfg):
    """The aligner's reading of the audio: the expensive half of alignment,
    and the half that does not need the words."""
    import torch

    model, _tokenizer, _aligner, device = _load(cfg)
    waveform = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None].to(device)
    with torch.inference_mode():
        emission, _ = model(waveform)
    return emission


def _spans(emission, tokens: list[str], cfg):
    """Match the words to an emission: quick once the emission is there."""
    import torch

    _model, tokenizer, aligner, _device = _load(cfg)
    with torch.inference_mode():
        return aligner(emission[0], tokenizer(tokens))


class Pending:
    """The audio's emission, worked out on a thread of its own.

    It does not depend on the words, so it is started as soon as a phrase
    arrives and runs while whisper listens: on the CPU it takes about 0.5 s
    for a 2 s phrase and 0.9 s for 5 s, which in turn is hidden behind
    whisper on the graphics card instead of added to every phrase.
    """

    def __init__(self, audio: np.ndarray, cfg, start: bool = True):
        self._audio = audio
        self._cfg = cfg
        self._done = threading.Event()
        self._value = None
        self._error: BaseException | None = None
        self._started = start
        if start:
            threading.Thread(target=self._run, name="aligner", daemon=True).start()

    def _run(self) -> None:
        try:
            self._value = _emission(self._audio, self._cfg)
        except BaseException as exc:  # noqa: BLE001 - re-raised in result()
            self._error = exc
        finally:
            self._done.set()

    def result(self):
        if not self._started:
            self._started = True
            self._run()
        self._done.wait()
        if self._error is not None:
            raise self._error
        return self._value


#: Characters the MMS_FA tokenizer knows. Anything else raises inside it, and
#: one such word - a name with an accent, a kana word, a stray symbol - used to
#: throw away the measured timings of every other word in the utterance.
_ALIGNABLE = set("abcdefghijklmnopqrstuvwxyz'")


def _normalise(part: str) -> str:
    """A word reduced to what the aligner can read: "Café" -> "cafe", "会議" -> ""."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", part.lower())
    kept = "".join(ch for ch in decomposed if ch in _ALIGNABLE)
    return kept.strip("'")


def _tokenize(words: list[Word]) -> tuple[list[str], list[tuple[int, int]]]:
    """Flatten words into aligner tokens, remembering which belong together.

    A lyric can hold several words after contraction expansion ("I'm" becomes
    "i am"), and the aligner wants one token per word, so spans are merged back
    afterwards. A word with nothing alignable gets an empty group and keeps
    whisper's timing.
    """
    tokens: list[str] = []
    groups: list[tuple[int, int]] = []
    for word in words:
        parts = [n for n in (_normalise(p) for p in word.text.split()) if n]
        start = len(tokens)
        tokens.extend(parts)
        groups.append((start, len(tokens)))
    return tokens, groups


def refine(
    words: list[Word], audio: np.ndarray, sample_rate: int, cfg, pending: "Pending | None" = None
) -> list[Word]:
    """Replace whisper's word spans with measured ones.

    Returns the original words unchanged if alignment is disabled or fails -
    approximate timings beat no output.
    """
    if not cfg.use_alignment or not words:
        return words
    return _measure(words, audio, sample_rate, cfg, pending)


def _measure(
    words: list[Word], audio: np.ndarray, sample_rate: int, cfg, pending: "Pending | None" = None
) -> list[Word]:
    """The aligner's span for every word; a word it cannot place keeps its own."""
    if not words:
        return words
    tokens, groups = _tokenize(words)
    if not tokens:
        return words

    try:
        emission = (pending or Pending(audio, cfg, start=False)).result()
        spans = _spans(emission, tokens, cfg)
    except Exception:
        log.warning("alignment failed; keeping whisper's timings", exc_info=True)
        return words

    if len(spans) != len(tokens):
        log.warning(
            "aligner returned %d spans for %d tokens; keeping whisper's timings",
            len(spans),
            len(tokens),
        )
        return words

    # Emission frames are evenly spaced across the audio.
    seconds_per_frame = audio.shape[0] / emission.shape[1] / sample_rate

    refined: list[Word] = []
    shifts: list[float] = []
    for word, (first, last) in zip(words, groups):
        if first >= last:  # a lyric with no alignable tokens
            refined.append(word)
            continue
        start = spans[first][0].start * seconds_per_frame
        end = spans[last - 1][-1].end * seconds_per_frame
        if end <= start:
            refined.append(word)
            continue
        shifts.append(start - word.start)
        refined.append(Word(text=word.text, start=float(start), end=float(end)))

    if shifts:
        log.debug(
            "aligned %d word(s); whisper was off by %+.3fs on average",
            len(shifts),
            float(np.mean(shifts)),
        )
    return refined


#: How far a boundary may move, and the least each word keeps per syllable.
#: Past these the aligner has lost its place, which on accented English
#: happens on short words.
BOUNDARY_MAX_MOVE = 0.3
BOUNDARY_MIN_SYLLABLE = 0.07
#: Words further apart than this have a pause between them, which whisper and
#: the loudness trimming already place.
BOUNDARY_TOUCHING = 0.05


def on_gpu(cfg) -> bool:
    """Whether the aligner would run on the graphics card: about 0.06 s a
    phrase there, about 1 s on the CPU (measured on a 5 s phrase)."""
    if not str(getattr(cfg, "align_device", "cuda")).startswith("cuda"):
        return False
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def boundaries(
    words: list[Word], audio: np.ndarray, sample_rate: int, cfg, pending: Pending | None = None
) -> list[Word]:
    """Whisper's spans, with each boundary between two touching words moved to
    where the aligner hears the one word end and the next begin.

    Whisper hands the end of a long word to the next one: "control" came out
    0.1 s short and the "it" after it twice as long as said, so she sang
    "control" in a hurry. Moving every start and end to the aligner's (the
    `use_alignment` option) fixes that but costs words on accented English,
    where the aligner misplaces whole words; here only the line between two
    words moves, and only when both keep a sensible length, so where a phrase
    starts and stops - what whisper gets right - stays.
    """
    if not getattr(cfg, "align_boundaries", True) or cfg.use_alignment or len(words) < 2:
        return words
    from .notes import syllables

    measured = _measure(words, audio, sample_rate, cfg, pending)
    out = list(words)
    for i in range(len(out) - 1):
        word, after = out[i], out[i + 1]
        heard, heard_after = measured[i], measured[i + 1]
        if after.start - word.end > BOUNDARY_TOUCHING:
            continue
        if (heard.start, heard.end) == (words[i].start, words[i].end):
            continue  # the aligner could not place it
        if (heard_after.start, heard_after.end) == (words[i + 1].start, words[i + 1].end):
            continue
        line = (heard.end + heard_after.start) / 2
        if abs(line - word.end) > BOUNDARY_MAX_MOVE:
            continue
        if line - word.start < BOUNDARY_MIN_SYLLABLE * syllables(word.text):
            continue
        if after.end - line < BOUNDARY_MIN_SYLLABLE * syllables(after.text):
            continue
        out[i] = Word(text=word.text, start=word.start, end=line)
        out[i + 1] = Word(text=after.text, start=line, end=after.end)
    return out


_VOWELS = set("aiueo")


def _romaji(mora: str) -> str:
    """A mora in the aligner's alphabet: せ -> se, ん -> n, みゅ -> myu."""
    from .translit import _load_kakasi

    kakasi = _load_kakasi()
    if kakasi is None:
        return ""
    return _normalise("".join(item["hepburn"] for item in kakasi.convert(mora)))


def vowel_onsets(
    morae: list[str], audio: np.ndarray, sample_rate: int, cfg, pending: Pending | None = None
) -> list[float | None]:
    """Where each mora's vowel starts in the recording, or None where unknown.

    Whisper times words, and roughly - worse on singing - and a word's morae
    were spread evenly inside it, so syllables landed off the melody (the
    user heard Senbonzakura's pronunciation out of time). Aligned sound by
    sound, each mora's vowel start is measured; the note starts there and the
    voicebank sings the consonant before it, as parts are written. An
    extension ("+") or anything unalignable is None.
    """
    if not getattr(cfg, "align_morae", True) or not morae:
        return [None] * len(morae)
    tokens, owners = [], []
    for index, mora in enumerate(morae):
        roman = _romaji(mora) if mora and mora[0] not in "+-" else ""
        if roman:
            tokens.append(roman)
            owners.append(index)
    if not tokens:
        return [None] * len(morae)
    try:
        emission = (pending or Pending(audio, cfg, start=False)).result()
        spans = _spans(emission, tokens, cfg)
    except Exception:
        log.warning("mora alignment failed; keeping the even spacing", exc_info=True)
        return [None] * len(morae)
    if len(spans) != len(tokens):
        return [None] * len(morae)
    seconds_per_frame = audio.shape[0] / emission.shape[1] / sample_rate
    out: list[float | None] = [None] * len(morae)
    for token, token_spans, owner in zip(tokens, spans, owners):
        chars = list(zip(token, token_spans))
        vowel = next((span for char, span in chars if char in _VOWELS), None)
        start = (vowel or chars[0][1]).start
        out[owner] = float(start * seconds_per_frame)
    return out
