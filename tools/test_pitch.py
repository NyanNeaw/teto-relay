"""Offline pitch check - the standalone verification for stage 3.

    # synthetic tones with known answers, no microphone needed
    .venv\\Scripts\\python.exe tools\\test_pitch.py --selftest

    # a real recording
    .venv\\Scripts\\python.exe tools\\test_pitch.py voice.wav

    # record from the mic for N seconds, then analyse
    .venv\\Scripts\\python.exe tools\\test_pitch.py --record 4
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teto_relay import pitch as pitch_mod  # noqa: E402
from teto_relay.config import Config  # noqa: E402

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(midi: float) -> str:
    m = int(round(midi))
    return f"{NOTE_NAMES[m % 12]}{m // 12 - 1}"


def tone(freq: float, seconds: float, sr: int) -> np.ndarray:
    """A harmonic-rich tone; a pure sine is unrealistically easy for pyin."""
    t = np.arange(int(seconds * sr)) / sr
    wave = np.zeros_like(t)
    for mult, amp in ((1, 1.0), (2, 0.4), (3, 0.2), (4, 0.1)):
        wave += amp * np.sin(2 * np.pi * freq * mult * t)
    return (wave / np.max(np.abs(wave)) * 0.8).astype(np.float32)


def selftest(cfg: Config) -> int:
    sr = cfg.sample_rate
    cases = [("A4", 440.0, 69), ("A3", 220.0, 57), ("C4", 261.63, 60), ("E4", 329.63, 64)]

    label = cfg.pitch_method
    if label == "crepe":
        label = f"torchcrepe ({cfg.crepe_model}, {cfg.crepe_device})"
    print(f"Analysing synthetic tones at {sr} Hz with {label}\n")
    print(f"  {'tone':<6} {'expected':>9} {'detected Hz':>12} {'MIDI':>6} {'note':>6} {'err':>7}")
    print("  " + "-" * 52)

    failures = 0
    for label, freq, expected_midi in cases:
        audio = tone(freq, 1.0, sr)
        track = pitch_mod.track_f0(audio, sr, cfg)
        midi = track.median_midi(0.1, 0.9)
        if midi is None:
            print(f"  {label:<6} {expected_midi:>9} {'-':>12} {'-':>6} {'unvoiced':>6}")
            failures += 1
            continue
        hz = float(pitch_mod.midi_to_hz(midi))
        err = midi - expected_midi
        flag = "" if abs(err) < 0.5 else "  <-- OFF"
        print(f"  {label:<6} {expected_midi:>9} {hz:>12.2f} {midi:>6.2f} {note_name(midi):>6} {err:>+7.2f}{flag}")
        if abs(err) >= 0.5:
            failures += 1

    print("\nShift behaviour (onto the voicebank's recorded pitch):")
    low_voice = [45.0, 46.0, 44.0]  # ~110 Hz speaking voice
    target = float(cfg.target_tone) or 61.0  # the English bank sits at C#4
    shift = pitch_mod.compute_shift(low_voice, cfg, target)
    landed = 45 + shift
    print(
        f"  a {note_name(45)} speaking voice shifts {shift:+d} semitones "
        f"-> {note_name(landed)} (target {note_name(target)})"
    )
    if abs(landed - target) > 6:
        print("  ERROR: the shift did not land near the voicebank's pitch")
        failures += 1

    print("\nPASS" if not failures else f"\n{failures} FAILURE(S)")
    return 1 if failures else 0


def analyse_file(path: Path, cfg: Config) -> int:
    import soundfile as sf

    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    audio = audio[:, 0]
    if sr != cfg.sample_rate:
        import librosa

        audio = librosa.resample(audio, orig_sr=sr, target_sr=cfg.sample_rate)
        sr = cfg.sample_rate

    print(f"{path.name}: {len(audio) / sr:.2f}s at {sr} Hz")
    track = pitch_mod.track_f0(audio, sr, cfg)
    if not track.any_voiced:
        print("  no voiced frames found")
        return 1

    voiced_hz = track.f0[track.voiced]
    median_midi = float(pitch_mod.hz_to_midi(np.median(voiced_hz)))
    print(f"  voiced frames : {int(track.voiced.sum())}/{len(track.voiced)}")
    print(f"  median pitch  : {np.median(voiced_hz):.1f} Hz = MIDI {median_midi:.2f} ({note_name(median_midi)})")
    print(f"  range         : {voiced_hz.min():.1f} - {voiced_hz.max():.1f} Hz")
    print(f"  octave shift  : {pitch_mod.octave_shift([median_midi], cfg):+d} semitones")

    print("\n  per 250 ms window:")
    step = 0.25
    t = 0.0
    while t < len(audio) / sr:
        midi = track.median_midi(t, t + step)
        if midi is None:
            print(f"    {t:>5.2f}s  unvoiced")
        else:
            print(f"    {t:>5.2f}s  {float(pitch_mod.midi_to_hz(midi)):>7.1f} Hz  MIDI {midi:>6.2f}  {note_name(midi)}")
        t += step
    return 0


def record(seconds: float, cfg: Config) -> Path:
    import sounddevice as sd
    import soundfile as sf

    print(f"Recording {seconds:.0f}s - hum or speak now...")
    audio = sd.rec(int(seconds * cfg.sample_rate), samplerate=cfg.sample_rate, channels=1, dtype="float32")
    sd.wait()
    path = cfg.out_path / "pitch_test.wav"
    sf.write(path, audio, cfg.sample_rate)
    print(f"Saved {path}\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline pitch detection check")
    parser.add_argument("wav", nargs="?", type=Path)
    parser.add_argument("--selftest", action="store_true", help="synthetic tones with known answers")
    parser.add_argument("--record", type=float, metavar="SECONDS")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    cfg = Config.load()

    if args.selftest:
        return selftest(cfg)
    if args.record:
        return analyse_file(record(args.record, cfg), cfg)
    if args.wav:
        return analyse_file(args.wav, cfg)
    return selftest(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
