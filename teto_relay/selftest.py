"""`--selftest`: run each model once on a test signal and say where it ran.

`--doctor` checks that things are installed; this checks that they work - in
the packaged app, where a missing DLL or data file only shows when a model is
actually used. The build runs it after PyInstaller: it is what shows the GPU
build still puts whisper on the GPU after torch's CUDA libraries were dropped.
No microphone, speakers or voicebank are touched.
"""

from __future__ import annotations

import time


def run_selftest(cfg) -> int:
    """Print one line per stage; 0 if all ran, 1 if any failed."""
    import numpy as np

    rate = 16000
    t = np.arange(int(1.0 * rate)) / rate
    probe = (0.2 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    failed = 0

    def stage(name, fn):
        nonlocal failed
        began = time.monotonic()
        try:
            detail = fn()
        except Exception as exc:  # noqa: BLE001 - reported, then the next stage
            failed += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
            return
        print(f"[ OK ] {name}: {detail} ({time.monotonic() - began:.1f}s)")

    # torch-backed stages before whisper, as the relay's warm-up does.
    def pitch():
        from .pitch import track_f0

        track = track_f0(probe, rate, cfg)
        found = float(np.nanmedian(track.f0[track.voiced])) if track.voiced.any() else float("nan")
        if not 200 < found < 240:
            raise RuntimeError(f"heard {found:.0f} Hz in a 220 Hz tone")
        return f"{track.method}, 220 Hz tone heard at {found:.0f} Hz"

    def aligner():
        from .align import vowel_onsets

        import torch

        onsets = vowel_onsets(["あ"], probe, rate, cfg)
        # Where it ran, not where it was asked to: without CUDA it falls back.
        cuda = str(cfg.align_device).startswith("cuda") and torch.cuda.is_available()
        return f"{len(onsets)} onset(s) on {'cuda' if cuda else 'cpu'}"

    def thai():
        from pythainlp.tokenize import word_tokenize

        from . import thai as thai_mod

        words = word_tokenize("สวัสดีครับ", engine="newmm")
        syllables = thai_mod.syllables("ทดสอบ")
        if not syllables:
            raise RuntimeError("no pronunciation for ทดสอบ")
        return f"{'|'.join(words)}, ทดสอบ = {thai_mod.to_latin('ทดสอบ')}"

    def whisper():
        from .stt import Transcriber, effective_model

        transcriber = Transcriber(cfg)
        transcriber.load()
        transcriber.transcribe(probe, rate)
        if transcriber._thai is not None:
            return f"{effective_model(cfg)} on cpu (onnx)"
        device = getattr(transcriber._model.model, "device", "?")
        return f"{effective_model(cfg)} on {device}"

    stage("pitch", pitch)
    stage("aligner", aligner)
    stage("thai", thai)
    stage("whisper", whisper)
    print("all stages ran" if not failed else f"{failed} stage(s) failed")
    return 1 if failed else 0
