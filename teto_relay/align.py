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


def _load(cfg):
    """Load the aligner once. Returns (model, tokenizer, aligner, device)."""
    global _bundle
    with _lock:
        if _bundle is not None:
            return _bundle

        free = _available_memory()
        if free is not None and free < _NEEDED_BYTES:
            raise MemoryError(
                f"only {free/1e9:.1f} GB of RAM free and the aligner needs about "
                f"{_NEEDED_BYTES/1e9:.1f} GB. Close what you can, or turn off "
                "'Measure word timing' - whisper's own timings are ~0.12 s early "
                "but everything still works."
            )

        import torch
        from torchaudio.pipelines import MMS_FA

        device = cfg.align_device
        if device.startswith("cuda") and not torch.cuda.is_available():
            log.warning("CUDA not available; aligning on the CPU")
            device = "cpu"

        log.info("Loading the forced aligner (%s)...", device)
        model = MMS_FA.get_model().to(device)
        model.eval()
        _bundle = (model, MMS_FA.get_tokenizer(), MMS_FA.get_aligner(), device)
        log.info("Aligner ready")
        return _bundle


def _tokenize(words: list[Word]) -> tuple[list[str], list[tuple[int, int]]]:
    """Flatten words into aligner tokens, remembering which belong together.

    A lyric can hold several words after contraction expansion ("I'm" becomes
    "i am"), and the aligner wants one token per word, so spans are merged back
    afterwards.
    """
    tokens: list[str] = []
    groups: list[tuple[int, int]] = []
    for word in words:
        parts = [p for p in word.text.split() if p]
        start = len(tokens)
        tokens.extend(parts)
        groups.append((start, len(tokens)))
    return tokens, groups


def refine(words: list[Word], audio: np.ndarray, sample_rate: int, cfg) -> list[Word]:
    """Replace whisper's word spans with measured ones.

    Returns the original words unchanged if alignment is disabled or fails -
    approximate timings beat no output.
    """
    if not cfg.use_alignment or not words:
        return words

    tokens, groups = _tokenize(words)
    if not tokens:
        return words

    try:
        import torch

        model, tokenizer, aligner, device = _load(cfg)
        waveform = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None].to(device)
        with torch.inference_mode():
            emission, _ = model(waveform)
            spans = aligner(emission[0], tokenizer(tokens))
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
