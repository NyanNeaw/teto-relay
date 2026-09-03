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

import numpy as np

log = logging.getLogger(__name__)

# Kept out of lyrics; the phonemizer has no use for punctuation.
# CJK punctuation is not in string.punctuation, so a Japanese full stop used to
# survive as a word and be sung: "。" came out as its own note.
_CJK_PUNCTUATION = "。、！？：；「」『』（）〈〉《》〔〕・…‥。／＼～－"
_STRIP = str.maketrans("", "", string.punctuation + _CJK_PUNCTUATION)
# Everything except the apostrophe, which carries meaning inside a word.
_STRIP_OUTER = string.punctuation.replace("'", "") + _CJK_PUNCTUATION

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
    word = word.strip(_STRIP_OUTER)  # drop surrounding punctuation, keep the apostrophe

    expanded = CONTRACTIONS.get(word)
    if expanded is not None:
        return expanded

    # Possessives and plurals ("teto's", "hours'") lose the apostrophe safely.
    return word.translate(_STRIP).strip()


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

            log.info(
                "Loading whisper %r (%s, %s)...",
                self.cfg.whisper_model,
                self.cfg.whisper_device,
                self.cfg.whisper_compute_type,
            )
            self._model = WhisperModel(
                self.cfg.whisper_model,
                device=self.cfg.whisper_device,
                compute_type=self.cfg.whisper_compute_type,
            )
            log.info("Whisper ready")

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> list[Word]:
        if sample_rate != 16000:
            raise ValueError(f"whisper expects 16 kHz audio, got {sample_rate}")
        self.load()

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
            initial_prompt=self.cfg.initial_prompt or None,
        )

        words: list[Word] = []
        dropped = 0
        for segment in segments:
            # Whisper answers near-silence with confident nonsense rather than
            # nothing, so discard segments it is not actually confident about.
            if segment.no_speech_prob is not None and segment.no_speech_prob > self.cfg.no_speech_threshold:
                dropped += 1
                log.debug("dropped segment (no_speech_prob=%.2f): %r", segment.no_speech_prob, segment.text)
                continue
            if segment.avg_logprob is not None and segment.avg_logprob < self.cfg.min_avg_logprob:
                dropped += 1
                log.debug("dropped segment (avg_logprob=%.2f): %r", segment.avg_logprob, segment.text)
                continue

            for w in segment.words or []:
                lyric = clean_lyric(w.word)
                if not lyric:
                    continue
                start, end = float(w.start), float(w.end)
                if end <= start:
                    continue
                words.append(Word(text=lyric, start=start, end=end))

        if dropped:
            log.info("Discarded %d low-confidence segment(s)", dropped)
        log.info("Transcribed %d word(s): %s", len(words), " ".join(w.text for w in words))
        return words
