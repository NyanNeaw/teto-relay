"""Per-utterance latency accounting.

Every utterance carries a `Timeline` from the microphone to the speaker. Each
stage adds how long it took, and so does each wait in a queue between stages,
so the numbers add up to what you actually experience: the time from letting
go of the key (or the pause that ended the phrase) to the first audible sound.

When playback starts, one line is logged:

    Latency 2.31s release->sound | speech 1.84s | wait 0.00s | asr 0.81s | ...

and a row is appended to `latency.csv` in the data folder, so a session can be
measured and compared in a spreadsheet.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Column order for the CSV, and the order stages are printed in. Stages that
#: did not run for an utterance (e.g. `align` with alignment off) are blank.
STAGES = [
    "speech",        # how long you spoke - not part of the delay after release
    "wait_analyse",  # chunk sat in the queue waiting for the analysis worker
    "asr",           # whisper
    "align",         # forced alignment
    "pitch",         # F0 tracking
    "notes",         # words + pitch -> notes (lyrics, octave shift, contour)
    "ustx",          # writing the project file
    "convert",       # voice mode: RVC conversion (replaces asr..render)
    "wait_render",
    "render",        # whole render call
    "phonemize",     # ...of which: building the project and phonemizing
    "synth",         # ...of which: WORLDLINE synthesis and mixing
    "wait_output",
    "output",        # reading the wav, resampling, opening the stream
    "lead_silence",  # silence at the start of the rendered audio
]

#: Sub-stages already counted inside another stage, so not added to the total.
NESTED = {"phonemize", "synth"}


@dataclass
class Timeline:
    """Stage durations for one utterance, in seconds."""

    released_at: float = field(default_factory=time.monotonic)
    stages: dict[str, float] = field(default_factory=dict)
    _last: float = 0.0

    def __post_init__(self) -> None:
        self._last = self.released_at

    def add(self, stage: str, seconds: float) -> None:
        self.stages[stage] = self.stages.get(stage, 0.0) + max(0.0, float(seconds))

    def lap(self, stage: str, now: float | None = None) -> float:
        """Charge the time since the previous lap to `stage`; returns now."""
        now = time.monotonic() if now is None else now
        self.add(stage, now - self._last)
        self._last = now
        return now

    def restart(self, now: float | None = None) -> None:
        """Start the next lap from `now` without charging the gap to anything."""
        self._last = time.monotonic() if now is None else now

    @property
    def total(self) -> float:
        """Release to first sound: every stage after the speech itself."""
        return sum(v for k, v in self.stages.items() if k != "speech" and k not in NESTED)

    def summary(self) -> str:
        parts = [f"{k} {self.stages[k]:.2f}s" for k in STAGES if k in self.stages]
        return f"{self.total:.2f}s release->sound | " + " | ".join(parts)

    def as_dict(self) -> dict[str, float]:
        out = {k: round(v, 3) for k, v in self.stages.items()}
        out["total"] = round(self.total, 3)
        return out


_csv_lock = threading.Lock()


def append_csv(path: Path, timeline: Timeline, text: str = "") -> None:
    """Add one row to the latency CSV, writing the header on first use."""
    try:
        with _csv_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if new:
                    writer.writerow(["time", "total", *STAGES, "text"])
                writer.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{timeline.total:.3f}",
                    *[f"{timeline.stages[k]:.3f}" if k in timeline.stages else "" for k in STAGES],
                    text[:80],
                ])
    except OSError:
        log.debug("could not write %s", path, exc_info=True)


def leading_silence(samples, sample_rate: int, threshold: float = 1e-3) -> float:
    """Seconds before the first sample louder than `threshold`."""
    import numpy as np

    data = np.asarray(samples)
    if data.ndim > 1:
        data = np.max(np.abs(data), axis=1)
    else:
        data = np.abs(data)
    loud = np.flatnonzero(data > threshold)
    if loud.size == 0:
        return len(data) / sample_rate if sample_rate else 0.0
    return float(loud[0]) / sample_rate
