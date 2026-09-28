"""Stage 2 - speech to text with per-word timings.

The reference implementation used RealtimeSTT, which returns a bare string. We
call faster-whisper directly because we need `word_timestamps=True`: those
spans give both the note durations and the windows over which to measure pitch.
"""

from __future__ import annotations

import logging
import string
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .translit import looks_thai

log = logging.getLogger(__name__)

# Kept out of lyrics; the phonemizer has no use for punctuation.
# CJK punctuation is not in string.punctuation, so a Japanese full stop used to
# survive as a word and be sung: "。" came out as its own note.
_CJK_PUNCTUATION = "。、！？：；「」『』（）〈〉《》〔〕・…‥。／＼～－"
_STRIP = str.maketrans("", "", string.punctuation + _CJK_PUNCTUATION)
# Everything except the apostrophe, which carries meaning inside a word.
_STRIP_OUTER = string.punctuation.replace("'", "") + _CJK_PUNCTUATION
# Marks that only modify the kana before them: the long-vowel mark, the
# sokuon and the small kana (ミュ is one mora, ュ alone has no sample). Whisper
# splits katakana into single characters, and alone they are no sound; see
# Transcriber.transcribe.
_MODIFIER_MARKS = "ーｰっッゃゅょぁぃぅぇぉゎャュョァィゥェォヮ"
# A segment below min_avg_logprob is dropped only if it is also this likely to
# be no speech at all, or if it is GARBAGE_MARGIN further below (see transcribe).
UNSURE_NO_SPEECH = 0.3
GARBAGE_MARGIN = 0.5
# Around a number, "$" and "%" are part of what is said ("$5", "50%").
_STRIP_OUTER_NUMBER = _STRIP_OUTER.replace("$", "").replace("%", "")

# Contractions must be expanded before the apostrophe is stripped. "I'm"
# reduced to "im" is read as "eem"; expanded to "i am" it sings correctly.
# The expansion stays in one note, which the phonemizer handles fine.
CONTRACTIONS = {
    "i'm": "i am",
    "i've": "i have",
    "i'll": "i will",
    "i'd": "i would",
    "you're": "you are",
    "you've": "you have",
    "you'll": "you will",
    "we're": "we are",
    "we've": "we have",
    "we'll": "we will",
    "they're": "they are",
    "they've": "they have",
    "they'll": "they will",
    "he's": "he is",
    "she's": "she is",
    "it's": "it is",
    "that's": "that is",
    "there's": "there is",
    "here's": "here is",
    "what's": "what is",
    "who's": "who is",
    "let's": "let us",
    "don't": "do not",
    "doesn't": "does not",
    "didn't": "did not",
    "can't": "can not",
    "won't": "will not",
    "isn't": "is not",
    "aren't": "are not",
    "wasn't": "was not",
    "weren't": "were not",
    "haven't": "have not",
    "hasn't": "has not",
    "hadn't": "had not",
    "wouldn't": "would not",
    "shouldn't": "should not",
    "couldn't": "could not",
    "ain't": "is not",
}


@dataclass(frozen=True)
class Word:
    text: str
    start: float  # seconds from the start of the chunk
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def clean_lyric(text: str) -> str:
    """Normalise a transcribed word into something singable.

    Contractions are expanded before punctuation is removed, because the
    apostrophe is the only thing distinguishing "I'm" from "im" - and stripping
    it first leaves the phonemizer singing "eem".
    """
    word = text.strip().lower().replace("’", "'")  # curly apostrophe

    # Digits have no pronunciation in either dictionary and were sung as
    # silence. Spelled out before punctuation is stripped, because "5:30" and
    # "3.5" need their separators to be read correctly.
    if any(ch.isdigit() for ch in word):
        from .numbers import spell

        word = spell(word.strip(_STRIP_OUTER_NUMBER))

    word = word.strip(_STRIP_OUTER)  # drop surrounding punctuation, keep the apostrophe

    expanded = CONTRACTIONS.get(word)
    if expanded is not None:
        return expanded

    # Possessives and plurals ("teto's", "hours'") lose the apostrophe safely.
    return word.translate(_STRIP).strip()


def whisper_prompt(cfg) -> str | None:
    """The vocabulary hint, then the song lyrics if any.

    Speech models mishear singing: the user's Senbonzakura came back as
    全部覚悟が夜にまじで... from base, small and medium alike. Given the lyric
    (lyrics_hint) as the text that came before, all three heard it exactly.
    It leans every phrase towards those words, so it is for singing a song
    and should be cleared afterwards.
    """
    parts = [str(cfg.initial_prompt or "").strip(), str(getattr(cfg, "lyrics_hint", "") or "").strip()]
    return " ".join(p for p in parts if p) or None


def max_new_tokens(seconds: float) -> int:
    """Tokens whisper may write for this much audio (see Transcriber.transcribe)."""
    return int(16 + 12 * max(0.0, seconds))


def pick_compute_type(device: str, requested: str) -> str:
    """`requested` if the device can run it, otherwise the best type it can.

    CTranslate2 refuses a compute type the hardware has no fast path for, and
    it does so when the model loads - which is on the first utterance if the
    warm-up failed, and again on every utterance after it. float16 on a
    GTX 10xx (Pascal) is the common case: the relay said "running" and then
    every phrase failed with a ValueError.
    """
    if not requested or requested in ("default", "auto"):
        return requested
    try:
        import ctranslate2

        supported = ctranslate2.get_supported_compute_types(device.split(":")[0])
    except Exception:  # noqa: BLE001 - let WhisperModel report a broken device itself
        return requested
    if requested in supported:
        return requested
    # int8 first: on GPUs without fast float16 it is also the fastest.
    for choice in ("int8", "int8_float32", "float32"):
        if choice in supported:
            log.warning(
                "whisper_compute_type %r is not supported on %s here (supported: %s); "
                "using %r. Set it to %r to hide this warning.",
                requested, device, ", ".join(sorted(supported)), choice, choice,
            )
            return choice
    return requested


#: Whisper small fine-tuned on Thai: "Thonburian Whisper" by Mahidol
#: University's biodatlab (Apache-2.0), in faster-whisper's format. On Thai
#: test phrases it made half the errors of the standard small model at the
#: same speed. The repo is a third party's conversion, so it is pinned, and its
#: weights are checked against a conversion made here from biodatlab's own
#: release (byte-identical).
THAI_MODEL = {
    "repo": "CodeHardThailand/whisper-th-small-combined-ct2",
    "revision": "d3c0f01d45969f10ee708b5aad2c56d48f75d1a6",
    "sha256": "8cef6d502277c94f2e403a8b23ecd6fe385bb262d7044621297c496b6c473470",
}
THAI_MODEL_NAME = "thai-small"
#: The Thai model, unsure, repeats a stock phrase until the token budget runs
#: out ("... สวัสดีครับ สวัสดีครับ สวัสดีครั" after a real sentence). With this
#: penalty it stopped on every test phrase, at the same accuracy.
THAI_REPETITION_PENALTY = 1.15
#: A frame counts as sound above this share of the phrase's loud frames.
SOUND_SHARE = 0.1


def speech_envelope(audio: np.ndarray, sample_rate: int) -> tuple[np.ndarray, float]:
    """10 ms loudness frames and the level that counts as sound."""
    hop = max(1, sample_rate // 100)
    frames = audio[: len(audio) // hop * hop].reshape(-1, hop) if len(audio) >= hop else audio[None, :]
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    return rms, float(np.percentile(rms, 90)) * SOUND_SHARE if rms.size else 0.0


#: After the last sound, a word may still be finishing: a Thai final บ is a
#: silent closure, and whisper times it (and a quiet ง) after the voice stops.
CODA_GRACE = 0.25


def invented(start: float, end: float, rms: np.ndarray, level: float, speech_end: float,
             text: str = "") -> bool:
    """Whether whisper made this word up after the speaker stopped.

    Invented words (a looped "สวัสดีครับ", "thanks for watching") were all
    timed after the last sound, over silence, often squeezed to no length at
    the very end. A real last word starts while there is still sound - except
    the end of one: a piece that continues the word (no leading space, a
    letter or two, brief) just after the voice stops is its final consonant.
    """
    if level <= 1e-7 or start < speech_end - 0.05:
        # No sound at all (a muted or synthetic buffer) says nothing about
        # which words are real; whisper's own silence checks apply.
        return False
    if (start < speech_end + CODA_GRACE and text and not text[0].isspace()
            and len(text.strip()) <= 2 and end - start <= 0.2):
        return False
    i = int(start * 100)
    j = max(int(end * 100), i + 5)
    window = rms[i:j]
    return window.size == 0 or float((window > level).mean()) < 0.2


def effective_model(cfg) -> str:
    """The speech model the relay actually loads for this configuration."""
    from .translit import source_language

    if getattr(cfg, "thai_speech_model", True) and source_language(cfg) == "th":
        return THAI_MODEL_NAME
    return cfg.whisper_model


def _thai_model_path() -> str:
    """Download (once) and verify the Thai model; returns its folder."""
    import hashlib

    from huggingface_hub import snapshot_download

    folder = Path(snapshot_download(
        THAI_MODEL["repo"], revision=THAI_MODEL["revision"],
        allow_patterns=["config.json", "model.bin", "vocabulary.json"],
    ))
    marker = folder / ".teto-relay-verified"
    if not marker.exists() or marker.read_text().strip() != THAI_MODEL["sha256"]:
        digest = hashlib.sha256()
        with open(folder / "model.bin", "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != THAI_MODEL["sha256"]:
            raise RuntimeError(
                "The Thai speech model did not match the expected download; it was not "
                "used. Turn off 'Thai speech model' in Setup to use the standard one."
            )
        try:
            marker.write_text(THAI_MODEL["sha256"])
        except OSError:
            pass
    return str(folder)


def _trim_loop(words: list[Word], out_of_budget: bool = False) -> list[Word]:
    """Cut a phrase that ends in one to three words said over and over.

    Whisper, unsure, loops a stock phrase until its token budget runs out, so
    the last copy is usually cut short (สวัสดี ครับ สวัสดี ครับ สวัสดี ครั).
    The first copy is kept - it may be what was said. One word has to come
    three times to count ("ไป ไป" is speech), a longer unit twice. Only a
    loop that ran out of budget is cut - its last copy cut short, or the
    token limit reached: a sung "la la la la" ends where the singer stopped.
    """
    texts = [w.text for w in words]
    for n in (1, 2, 3):
        if len(texts) < 2 * n:
            continue
        unit, last = texts[-2 * n:-n], texts[-n:]
        if not (last[:-1] == unit[:-1] and unit[-1].startswith(last[-1])):
            continue
        if last[-1] == unit[-1] and not out_of_budget:
            continue
        end, copies = len(texts) - n, 1
        while end - n >= 0 and texts[end - n:end] == unit:
            copies += 1
            end -= n
        if copies >= (3 if n == 1 else 2):
            return words[: end + n]
    return words


def _regroup_thai(words: list[Word]) -> list[Word]:
    """Rebuild Thai words from the pieces whisper times.

    Thai has no spaces, and whisper times it in pieces of a few characters -
    "ส | ว | ั | ส | ด | ี" for สวัสดี - which, sung one piece at a time,
    was nonsense. A run of Thai pieces is joined and cut into real words, each
    timed from the first piece it starts in to the last it ends in.
    """
    from .translit import looks_thai

    out: list[Word] = []
    run: list[Word] = []

    def flush() -> None:
        if not run:
            return
        text = "".join(w.text for w in run)
        spans: list[tuple[float, float]] = []
        for w in run:
            spans.extend([(w.start, w.end)] * len(w.text))
        try:
            from pythainlp.tokenize import word_tokenize

            tokens = word_tokenize(text, keep_whitespace=False)
        except Exception:  # noqa: BLE001 - without pythainlp, keep the run whole
            log.warning("pythainlp is unavailable; Thai words cannot be separated", exc_info=True)
            tokens = [text]
        position = 0
        for token in tokens:
            at = text.find(token, position)
            if at < 0 or not token.strip():
                continue
            last = at + len(token) - 1
            out.append(Word(text=token, start=spans[at][0], end=max(spans[last][1], spans[at][0] + 0.01)))
            position = last + 1
        run.clear()

    for w in words:
        if looks_thai(w.text):
            run.append(w)
        else:
            flush()
            out.append(w)
    flush()
    return out


class Transcriber:
    """Lazily-loaded faster-whisper wrapper. Safe to call from one worker thread."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        """Load the model up front so the first utterance is not slow."""
        with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel

            compute_type = pick_compute_type(self.cfg.whisper_device, self.cfg.whisper_compute_type)
            name = effective_model(self.cfg)
            log.info("Loading whisper %r (%s, %s)...", name, self.cfg.whisper_device, compute_type)
            source = self.cfg.whisper_model
            if name == THAI_MODEL_NAME:
                try:
                    source = _thai_model_path()
                except Exception as exc:  # noqa: BLE001 - fall back, don't fail the relay
                    log.warning("The Thai speech model could not be loaded (%s); using %r",
                                exc, self.cfg.whisper_model)
            self._model = WhisperModel(
                source,
                device=self.cfg.whisper_device,
                compute_type=compute_type,
            )
            log.info("Whisper ready")

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> list[Word]:
        if sample_rate != 16000:
            raise ValueError(f"whisper expects 16 kHz audio, got {sample_rate}")
        self.load()

        budget = max_new_tokens(len(audio) / sample_rate)
        segments, _info = self._model.transcribe(
            audio.astype(np.float32),
            language=self.cfg.language or None,
            word_timestamps=True,
            # Our own chunker already bounds the utterance; a second VAD pass
            # would only shift the timestamps we depend on.
            vad_filter=False,
            # Without this, a hallucinated segment becomes the prompt for the
            # next one and the invented text compounds across an utterance.
            condition_on_previous_text=False,
            no_speech_threshold=self.cfg.no_speech_threshold,
            compression_ratio_threshold=self.cfg.compression_ratio_threshold,
            log_prob_threshold=self.cfg.min_avg_logprob,
            beam_size=self.cfg.beam_size,
            initial_prompt=whisper_prompt(self.cfg),
            # Enough for fast speech (~12 tokens a second) and no more. On
            # repetitive input ("ムーリームーリー") whisper loops until its
            # 448-token limit, fails the compression check, and retries at
            # every fallback temperature: 17.7 s for a 2 s phrase on the
            # CPU, with the next phrase queued behind it. Capped: ~5 s.
            max_new_tokens=budget,
            repetition_penalty=THAI_REPETITION_PENALTY if effective_model(self.cfg) == THAI_MODEL_NAME else 1.0,
        )

        words: list[Word] = []
        rms, level = speech_envelope(audio, sample_rate)
        loud = np.nonzero(rms > level)[0]
        speech_end = (loud[-1] + 1) / 100.0 if loud.size else 0.0
        made_up: list[str] = []
        tokens = 0
        dropped = 0
        for segment in segments:
            tokens += len(getattr(segment, "tokens", None) or [])
            # Whisper answers near-silence with confident nonsense rather than
            # nothing, so discard segments it is not actually confident about.
            if segment.no_speech_prob is not None and segment.no_speech_prob > self.cfg.no_speech_threshold:
                dropped += 1
                log.debug("dropped segment (no_speech_prob=%.2f): %r", segment.no_speech_prob, segment.text)
                continue
            # Low confidence alone is not silence - whisper itself calls a
            # segment silent only when it is unsure *and* probably not speech.
            # Dropping every unsure segment threw away whole phrases said with
            # an accent: the user's "今日はいい天気ですね" decoded at -0.97 on
            # one run and below -1.0 on the next, and vanished. Only a segment
            # far below the threshold is treated as garbage on its own.
            logprob = segment.avg_logprob
            unsure = logprob is not None and logprob < self.cfg.min_avg_logprob
            silent_ish = (segment.no_speech_prob or 0.0) > UNSURE_NO_SPEECH
            garbage = logprob is not None and logprob < self.cfg.min_avg_logprob - GARBAGE_MARGIN
            if (unsure and silent_ish) or garbage:
                dropped += 1
                log.debug("dropped segment (avg_logprob=%.2f): %r", segment.avg_logprob, segment.text)
                continue

            for w in segment.words or []:
                lyric = clean_lyric(w.word)
                if not lyric:
                    continue
                start, end = float(w.start), float(w.end)
                if invented(start, end, rms, level, speech_end, w.word):
                    made_up.append(lyric)
                    continue
                if end <= start:
                    # Thai vowel and tone marks often come back with no
                    # duration of their own; dropped, ชื่อ was sung as ช-อ.
                    if words and looks_thai(lyric):
                        prev = words[-1]
                        words[-1] = Word(text=prev.text + lyric, start=prev.start, end=prev.end)
                    continue
                # Whisper splits ムー into "ム" + "ー". A long-vowel mark (or a
                # sokuon) on its own is no sound - it only lengthens the kana
                # before it - so alone it became a note with no sample, and
                # "ムーリー" was sung む (silence) り (silence).
                if words and lyric.strip(_MODIFIER_MARKS) == "":
                    prev = words[-1]
                    words[-1] = Word(text=prev.text + lyric, start=prev.start, end=max(prev.end, end))
                    continue
                words.append(Word(text=lyric, start=start, end=end))

        if made_up:
            log.info("Ignored %d word(s) heard after you stopped speaking (whisper invents "
                     "text in silence): %s", len(made_up), "".join(made_up)[:80])
        words = _regroup_thai(words)
        trimmed = _trim_loop(words, out_of_budget=tokens >= budget - 2)
        if len(trimmed) < len(words):
            log.info("Ignored a repeated ending whisper looped on: %s",
                     " ".join(w.text for w in words[len(trimmed):]))
            words = trimmed
        if dropped:
            log.info("Discarded %d low-confidence segment(s)", dropped)
        log.info("Transcribed %d word(s): %s", len(words), " ".join(w.text for w in words))
        return words
