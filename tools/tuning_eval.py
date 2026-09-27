"""Score how naturally a set of spoken phrases is sung, so tuning can be judged.

    .venv\\Scripts\\python.exe tools\\tuning_eval.py --bank english --label now phrase1.wav phrase2.wav
    .venv\\Scripts\\python.exe tools\\tuning_eval.py --bank tandoku --set singing_style=sung --label sung out\\relay_*_in.wav

Each phrase goes through exactly what the relay does (TetoRelay.analyse, then
the renderer), and the rendered singing is scored on:

* **heard** - an independent speech model (whisper small, not the one the
  relay listens with) transcribes her; word error rate against what you said
  (English), or kana error rate (Japanese). Lower is more intelligible.
* **stretch** - how long her singing lasts against how long you spoke. Near
  1.0 keeps your rhythm; well above it drags.
* **pauses** - your pauses of 0.25 s or more, and how many she kept.
* **missing** - phonemes the voicebank had no sample for.

Renders are kept in D:\\Claude\\_tmp\\eval\\<label>\\ (or --out) for listening.
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from teto_relay.config import Config, coerce  # noqa: E402


class _Missing(logging.Handler):
    """Counts the renderer's "have no sample" warnings."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record):
        message = record.getMessage()
        if "have no sample" in message:
            self.count += int(message.split()[0]) if message.split()[0].isdigit() else 1


def _load(path: str) -> np.ndarray:
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if rate != 16000:
        import librosa

        audio = librosa.resample(audio, orig_sr=rate, target_sr=16000)
    return audio.astype(np.float32)


def _activity(audio: np.ndarray, rate: int, floor_db: float = -35.0) -> np.ndarray:
    """Per 10 ms frame: is there sound, relative to the loudest part."""
    hop = rate // 100
    frames = len(audio) // hop
    if frames == 0:
        return np.zeros(0, bool)
    rms = np.sqrt(np.mean(audio[: frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms / rms.max())
    return db > floor_db


def _span_and_pauses(active: np.ndarray, min_pause: float) -> tuple[float, int]:
    """Seconds from first to last sound, and pauses of at least min_pause inside it."""
    idx = np.flatnonzero(active)
    if idx.size == 0:
        return 0.0, 0
    inner = active[idx[0]: idx[-1] + 1]
    pauses, run = 0, 0
    for on in inner:
        if on:
            if run * 0.01 >= min_pause:
                pauses += 1
            run = 0
        else:
            run += 1
    return (idx[-1] - idx[0] + 1) * 0.01, pauses


def _edit_distance(a: list, b: list) -> int:
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def _units(text: str, japanese: bool) -> list[str]:
    """Words (English) or kana (Japanese), normalised the way the relay does."""
    from teto_relay.stt import clean_lyric

    if japanese:
        from teto_relay import translit

        kana = "".join(translit.to_kana(w, "ja") or "" for w in text.split())
        return [ch for ch in kana if not ch.isspace()]
    words = []
    for w in text.split():
        cleaned = clean_lyric(w)
        words.extend(cleaned.split())
    return words


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wavs", nargs="+")
    ap.add_argument("--bank")
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default=r"D:\Claude\_tmp\eval")
    ap.add_argument("--set", nargs="*", default=[], help="config overrides, key=value")
    ap.add_argument("--judge", default="small", help="whisper model that judges intelligibility")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    missing = _Missing()
    logging.getLogger("teto_relay.render.openutau").addHandler(missing)

    cfg = Config.load()
    for kv in args.set:
        key, value = kv.split("=", 1)
        setattr(cfg, key, coerce(key, value))
    if args.bank:
        cfg.voicebank = args.bank
    cfg.keep_input_audio = False
    cfg.mode = "utau"
    cfg.validate()

    from teto_relay.app import TetoRelay
    from teto_relay.capture import Chunk

    relay = TetoRelay(cfg)
    # The relay's own warm-up, for its load order: torch's models before
    # whisper's, or the first CUDA convolution dies on cuDNN (see _warmup).
    relay._warmup()
    japanese = relay._japanese_lyrics()
    from faster_whisper import WhisperModel

    judge = WhisperModel(args.judge, device="cuda", compute_type="int8")
    out = Path(args.out) / args.label
    out.mkdir(parents=True, exist_ok=True)

    files = [f for pattern in args.wavs for f in (glob.glob(pattern) or [pattern])]
    rows = []
    for path in files:
        audio = _load(path)
        before = missing.count
        job = relay.analyse(Chunk(audio=audio, sample_rate=16000, reason="file"))
        if job is None:
            print(f"{Path(path).name}: nothing recognised")
            continue
        said = relay.last_source
        wav = relay.renderer.render(job.ustx_path, out / (Path(path).stem + ".wav"))
        sung, rate = sf.read(str(wav), dtype="float32", always_2d=True)
        sung = sung.mean(axis=1)

        segments, _ = judge.transcribe(
            sung if rate == 16000 else _resample(sung, rate), language="ja" if japanese else "en",
            beam_size=5, condition_on_previous_text=False)
        heard = " ".join(s.text for s in segments)
        ref, hyp = _units(said, japanese), _units(heard, japanese)
        error = _edit_distance(ref, hyp) / max(1, len(ref))

        spoken_len, spoken_pauses = _span_and_pauses(_activity(audio, 16000), 0.25)
        sung_len, sung_pauses = _span_and_pauses(_activity(sung, rate), 0.15)
        rows.append((error, sung_len / max(spoken_len, 1e-3), spoken_pauses, min(sung_pauses, spoken_pauses),
                     missing.count - before))
        print(f"{Path(path).name:28s} error {error:5.2f}  stretch {sung_len / max(spoken_len, 1e-3):4.2f}  "
              f"pauses {min(sung_pauses, spoken_pauses)}/{spoken_pauses}  missing {missing.count - before}")
        print(f"    said : {said}")
        print(f"    heard: {heard.strip()}")

    if rows:
        r = np.array(rows, dtype=float)
        kept = r[:, 3].sum() / r[:, 2].sum() if r[:, 2].sum() else 1.0
        print(f"\n[{args.label}] mean error {r[:, 0].mean():.3f}  mean stretch {r[:, 1].mean():.2f}  "
              f"pauses kept {kept:.0%}  missing {int(r[:, 4].sum())}  ({len(rows)} phrases)")
    relay.renderer.close()
    return 0


def _resample(audio: np.ndarray, rate: int) -> np.ndarray:
    import librosa

    return librosa.resample(audio, orig_sr=rate, target_sr=16000).astype(np.float32)


if __name__ == "__main__":
    raise SystemExit(main())
