"""Thai speech recognition with Typhoon ASR Real-time.

A FastConformer transducer (115M parameters, SCB 10X's Typhoon, CC BY 4.0)
trained for Thai, run through onnxruntime on the CPU via PyThaiASR. It
replaced Thonburian Whisper small, which invented YouTube sign-offs when a
sung note was held after the words ("... สวัสดีครับ ขอบคุณที่ช่วยกันนะครับ"
on three of four test phrases followed by a held hum; this model, none). A
transducer only writes what it hears - it has no language model of its own
to talk on with. On 40 test phrases it made a few more character errors
(4.3% against 2.5% clean, 3.4% against 2.7% noisy, mostly how it spells
เท็ตโตะ) in a fifth of the time (0.11 s a phrase on the CPU, against 0.55 s
for the Whisper on the GPU), and the same 0.5 GB download.

Times come from when each character is emitted, in 80 ms steps; words are cut
with pythainlp's newmm. A word runs on to the next one's start - an emission
marks a point, not a span - and build_notes fits spans to the sound anyway.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

import numpy as np

from .stt import Word, clean_lyric

log = logging.getLogger(__name__)

#: PyThaiNLP's ONNX export of typhoon-ai/typhoon-asr-realtime, pinned: a
#: third party's repo, so the files are checked against these hashes.
MODEL = {
    "repo": "wannaphong/typhoon-asr-realtime-onnx",
    "revision": "04af3e7b6d822cb2807bc539301669d4f84691e7",
    "files": {
        "encoder": ("encoder-fastconformer-quran-ar.onnx",
                    "9573e8224cbad1be622b779e780cccc5638a4dd19097fd6c7258d8ec1c21cf91"),
        "decoder": ("decoder_joint-fastconformer-quran-ar.onnx",
                    "c445e567e1133506c1ff5e4722ee61b8adf830243e0449de3b796a3205ea1e4a"),
        "vocab": ("tokenizer/vocab.json",
                  "a7277aeb7b08f6c8a1ed7838b55d204cf6766dbe583d690bbb9fb0d42de0c2b1"),
    },
}
NAME = "typhoon-th"
#: Words closer than this are one run of speech: the earlier one lasts until
#: the next begins. Further apart, it ends one step after its last letter.
JOIN = 0.3
STEP = 0.08


def model_files() -> dict[str, Path]:
    """Download (once) and verify the model; returns its files by role."""
    from huggingface_hub import hf_hub_download

    out = {}
    for role, (name, digest) in MODEL["files"].items():
        path = Path(hf_hub_download(MODEL["repo"], name, revision=MODEL["revision"]))
        marker = path.with_name(path.name + ".teto-relay-verified")
        if not marker.exists() or marker.read_text().strip() != digest:
            sha = hashlib.sha256()
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    sha.update(block)
            if sha.hexdigest() != digest:
                raise RuntimeError(
                    f"The Thai speech model ({name}) did not match the expected download; "
                    "it was not used. Turn off 'Thai speech model' in Listening to use Whisper."
                )
            try:
                marker.write_text(digest)
            except OSError:
                pass
        out[role] = path
    return out


class ThaiTranscriber:
    """Typhoon ASR with the Transcriber's interface: load(), transcribe()."""

    on_gpu = False

    def __init__(self):
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            from pythaiasr.typhoon import FastConformerRNNT

            files = model_files()
            log.info("Loading the Thai speech model (Typhoon ASR, CPU)...")
            self._model = FastConformerRNNT(
                encoder_path=files["encoder"], decoder_path=files["decoder"],
                vocab_path=files["vocab"], device="cpu",
            )
            log.info("Thai speech model ready")

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> list[Word]:
        self.load()
        result = self._model.transcribe(np.asarray(audio, dtype=np.float32), sample_rate=sample_rate,
                                        return_timestamps="word")
        chunks = result.get("chunks", []) if isinstance(result, dict) else []
        return words_from_chunks(chunks)


def words_from_chunks(chunks: list[dict]) -> list[Word]:
    """Typhoon's word chunks as the relay's words, each running on to the next.

    No sign-off filter (stt.drop_outro): this model does not invent one, so a
    "ขอบคุณผู้ชมครับ" it hears was said - on a stream, quite likely.
    """
    words = []
    for chunk in chunks:
        text = clean_lyric(str(chunk.get("text", "")))
        if not text:
            continue
        start, end = float(chunk["start"]), float(chunk["end"])
        if words and start < words[-1].start + STEP:
            # Two words on one emission step ("เมื่อวาน@1.04 นี้@1.04") would
            # be two notes on one onset: the later one waits a step.
            start = words[-1].start + STEP
        words.append(Word(text=text, start=start, end=max(end, start + STEP)))
    for i in range(len(words) - 1):
        w, nxt = words[i], words[i + 1]
        if 0.0 <= nxt.start - w.end < JOIN or nxt.start < w.end:
            words[i] = Word(text=w.text, start=w.start, end=max(w.start + STEP, nxt.start))
    return words
