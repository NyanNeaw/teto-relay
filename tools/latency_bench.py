"""How long does each stage take? Runs recordings through the relay as the app does.

    python tools/latency_bench.py [--code DIR] [--bank english] [--language en] [--rounds 2] WAV...

Analysis runs on one thread and OpenUtau on another, as in the app (the
render thread matters: OpenUtau's main-thread check once cost 0.65 s a
phrase only there). Prints each stage's median over every phrase and
round; the first round warms OpenUtau's caches and is left out of the
medians unless --rounds is 1. `--code` runs another checkout's teto_relay,
so two versions can be compared on the same recordings.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import threading
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wavs", nargs="+")
    ap.add_argument("--code", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--config", default=None, help="settings file to start from (default: this checkout's)")
    ap.add_argument("--bank", default=None)
    ap.add_argument("--language", default="en")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--set", nargs="*", default=[], help="config overrides, key=value")
    args = ap.parse_args()

    sys.path.insert(0, args.code)
    import torch  # noqa: F401 - torch before whisper (cuDNN load order)
    import librosa
    import soundfile as sf

    from teto_relay.app import TetoRelay
    from teto_relay.capture import Chunk
    from teto_relay.config import Config, coerce

    logging.basicConfig(level=logging.WARNING)
    # Your settings, as the app has them; only the voicebank and language
    # are chosen here, so runs compare like with like.
    cfg = Config.load(Path(args.config) if args.config else None)
    if args.bank:
        cfg.voicebank = args.bank
    cfg.language = args.language
    cfg.keep_input_audio = False
    for kv in args.set:
        key, value = kv.split("=", 1)
        setattr(cfg, key, coerce(key, value))
    relay = TetoRelay(cfg)
    relay._warmup()

    clips = []
    for path in args.wavs:
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        audio = audio.mean(axis=1)
        if rate != 16000:
            audio = librosa.resample(audio, orig_sr=rate, target_sr=16000)
        clips.append((Path(path).name, audio))

    stages: dict[str, list[float]] = {}

    from teto_relay import paths

    cache = paths.host_dir() / "Cache"

    def render(job, out: dict) -> None:
        # OpenUtau caches what it sang; every phrase is new in real use, so a
        # cache hit would make the second round look far faster than it is.
        for item in cache.glob("*") if cache.is_dir() else []:
            try:
                item.unlink()
            except OSError:
                pass
        began = time.monotonic()
        relay.renderer.render(job.ustx_path, job.ustx_path.with_suffix(".bench.wav"))
        out["render"] = time.monotonic() - began
        out.update(relay.renderer.last_timings)

    for round_no in range(args.rounds):
        for name, audio in clips:
            began = time.monotonic()
            job = relay.analyse(Chunk(audio=audio, sample_rate=16000, reason="file", captured_at=began))
            if job is None:
                continue
            analysed = time.monotonic() - began
            out: dict = {}
            worker = threading.Thread(target=render, args=(job, out), name="render")
            worker.start()
            worker.join()
            row = {k: v for k, v in getattr(job.timeline, "stages", {}).items()
                   if k not in ("speech", "wait_analyse")}
            row.update(analyse=analysed, **out)
            row["total"] = analysed + out["render"]
            if round_no or args.rounds == 1:
                for key, value in row.items():
                    stages.setdefault(key, []).append(float(value))
            print(f"round {round_no + 1} {name:14s} total {row['total']:.2f}s  "
                  + "  ".join(f"{k} {v:.2f}" for k, v in row.items() if k != "total"), flush=True)

    print("\nmedian over measured phrases:")
    for key in ("asr", "align", "pitch", "notes", "ustx", "analyse", "phonemize", "synth", "render", "total"):
        if key in stages:
            print(f"  {key:10s} {statistics.median(stages[key]):.2f}s")
    relay.renderer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
