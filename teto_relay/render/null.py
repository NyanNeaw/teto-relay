"""A voicebank-free renderer used to exercise the pipeline end to end.

It reads the generated .ustx and synthesises a simple harmonic tone per note,
following the pitch contour we detected. It is not meant to sound like Teto -
it exists so stages 1-4 and 6 can be verified (timing, pitch, routing, latency)
independently of the synthesis engine, and so there is always a working
fallback if the OpenUtau backend fails to start.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import soundfile as sf

from ..pitch import midi_to_hz
from ..ustx import load_ustx
from .base import RenderError

log = logging.getLogger(__name__)

# A few harmonics with falling amplitude read as "voice-like" rather than a beep.
HARMONICS = ((1.0, 1.0), (2.0, 0.35), (3.0, 0.18), (4.0, 0.08))


class NullRenderer:
    name = "null"

    def __init__(self, cfg, sample_rate: int = 44100):
        self.cfg = cfg
        self.sample_rate = sample_rate

    def close(self) -> None:  # nothing to release
        pass

    def render(self, ustx_path: Path, out_wav: Path) -> Path:
        project = load_ustx(ustx_path)
        try:
            part = project["voice_parts"][0]
            notes = part["notes"]
        except (KeyError, IndexError) as exc:
            raise RenderError(f"{ustx_path} has no voice part to render") from exc
        if not notes:
            raise RenderError(f"{ustx_path} contains no notes")

        ticks_per_second = project["resolution"] * project["bpm"] / 60.0
        total_ticks = max(n["position"] + n["duration"] for n in notes)
        total_samples = int(total_ticks / ticks_per_second * self.sample_rate) + self.sample_rate // 10
        buffer = np.zeros(total_samples, dtype=np.float64)

        for note in notes:
            start = int(note["position"] / ticks_per_second * self.sample_rate)
            length = int(note["duration"] / ticks_per_second * self.sample_rate)
            if length <= 0:
                continue
            buffer[start : start + length] += self._voice(note, length)

        peak = float(np.max(np.abs(buffer))) if buffer.size else 0.0
        if peak > 0:
            buffer = buffer / peak * 0.7

        out_wav = Path(out_wav)
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        sf.write(out_wav, buffer.astype(np.float32), self.sample_rate)
        log.info("NullRenderer wrote %s (%.2fs)", out_wav.name, len(buffer) / self.sample_rate)
        return out_wav

    def _voice(self, note: dict, length: int) -> np.ndarray:
        """One note as a harmonic stack that follows its pitch contour."""
        t = np.arange(length) / self.sample_rate
        base_midi = float(note["tone"])

        # Interpolate the cents contour across the note, so the preview carries
        # the same prosody the ustx describes.
        points = [(p["x"], p["y"]) for p in note.get("pitch", {}).get("data", [])]
        points = [(x, y) for x, y in points if x >= 0]
        if len(points) >= 2:
            xs = np.array([x / 1000.0 for x, _ in points])
            ys = np.array([y for _, y in points])
            cents = np.interp(t, xs, ys, left=ys[0], right=ys[-1])
        else:
            cents = np.zeros(length)

        freq = midi_to_hz(base_midi + cents / 100.0)
        # Integrate frequency to phase so the pitch glides instead of stepping.
        phase = 2 * np.pi * np.cumsum(np.asarray(freq, dtype=float)) / self.sample_rate

        wave = np.zeros(length, dtype=np.float64)
        for mult, amp in HARMONICS:
            wave += amp * np.sin(phase * mult)

        return wave * self._envelope(length)

    def _envelope(self, length: int) -> np.ndarray:
        """Short raised-cosine fades so notes do not click."""
        env = np.ones(length, dtype=np.float64)
        edge = min(int(0.012 * self.sample_rate), length // 2)
        if edge > 1:
            ramp = 0.5 * (1 - np.cos(np.linspace(0, np.pi, edge)))
            env[:edge] *= ramp
            env[-edge:] *= ramp[::-1]
        return env
