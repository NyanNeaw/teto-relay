"""Takes: record a stretch of singing as one audio file and one OpenUtau project.

For a song cover: press Record in the panel, sing over the backing track
(holding the talk key for each line as usual), press it again. Every phrase
lands on one timeline at the moment it was sung, so the result lines up with
the music:

* ``teto.wav``       - Teto's singing, the whole take (with Double voice
                       where it applied);
* ``teto.ustx``      - an OpenUtau project: each phrase is a part on one
                       track at the time it was sung, notes, lyrics and pitch
                       curves included, to re-tune or re-voice in OpenUtau;
* ``your-voice.wav`` - what the microphone heard, on the same timeline, as a
                       guide track;
* ``take.json``      - when it was made and what was sung.

Takes are kept in the data folder's ``takes`` directory.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

#: The files a finished take holds, in the order the panel offers them.
FILES = ("teto.wav", "teto.ustx", "your-voice.wav")


@dataclass
class Phrase:
    offset: float          # where the phrase's recording starts in the take, s
    text: str
    doc: dict              # its .ustx document
    sung: np.ndarray       # her singing
    sung_rate: int
    sung_origin: float     # where in the phrase's recording the singing starts, s
    heard: np.ndarray      # the microphone
    heard_rate: int


class Take:
    """One take while it is being recorded; `finish` writes it out."""

    def __init__(self, root: Path):
        self.started = time.monotonic()
        self.created = datetime.now()
        self.folder = Path(root) / self.created.strftime("take_%Y%m%d_%H%M%S")
        self.phrases: list[Phrase] = []
        self.stopped: float | None = None
        self._lock = threading.Lock()

    @property
    def seconds(self) -> float:
        return time.monotonic() - self.started

    def add(self, job) -> None:
        """A phrase has been sung: keep it, if it was recorded during the take."""
        start = getattr(job, "audio_start", None)
        if start is None or start < self.started - 1.0 or job.wav_path is None or job.ustx_path is None:
            return
        if self.stopped is not None and start > self.stopped:
            return  # said after Stop
        try:
            import soundfile as sf
            import yaml

            doc = yaml.safe_load(Path(job.ustx_path).read_text(encoding="utf-8"))
            sung, rate = sf.read(str(job.wav_path), dtype="float32", always_2d=False)
        except Exception:  # noqa: BLE001 - one phrase lost, not the take
            log.warning("could not add a phrase to the take", exc_info=True)
            return
        if sung.ndim > 1:
            sung = sung.mean(axis=1)
        phrase = Phrase(
            offset=max(0.0, start - self.started), text=job.text, doc=doc,
            sung=sung, sung_rate=int(rate), sung_origin=float(getattr(job, "wav_origin", 0.0)),
            heard=np.asarray(getattr(job, "input_audio", None) if getattr(job, "input_audio", None) is not None
                             else np.zeros(0), dtype=np.float32),
            heard_rate=int(getattr(job, "input_rate", 16000) or 16000),
        )
        with self._lock:
            self.phrases.append(phrase)

    # ------------------------------------------------------------------ write
    def finish(self) -> dict:
        """Write the take's files; returns its summary (see `summary`)."""
        with self._lock:
            phrases = sorted(self.phrases, key=lambda p: p.offset)
        self.folder.mkdir(parents=True, exist_ok=True)
        info = {
            "id": self.folder.name,
            "created": self.created.isoformat(timespec="seconds"),
            "seconds": round(self.seconds, 2),
            "phrases": [{"at": round(p.offset, 3), "text": p.text} for p in phrases],
            "files": [],
        }
        if phrases:
            import soundfile as sf

            sung, rate = mix([(p.offset + p.sung_origin, p.sung) for p in phrases], phrases[0].sung_rate)
            sf.write(str(self.folder / "teto.wav"), sung, rate, subtype="PCM_16")
            heard, heard_rate = mix([(p.offset, p.heard) for p in phrases if p.heard.size],
                                    phrases[0].heard_rate)
            if heard.size:
                sf.write(str(self.folder / "your-voice.wav"), heard, heard_rate, subtype="PCM_16")
            write_project(phrases, self.folder / "teto.ustx")
            info["files"] = [name for name in FILES if (self.folder / name).is_file()]
            info["length"] = round(len(sung) / rate, 2)
        (self.folder / "take.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Take saved: %s (%d phrase(s))", self.folder, len(phrases))
        return info


def mix(pieces: list[tuple[float, np.ndarray]], rate: int) -> tuple[np.ndarray, int]:
    """Lay each piece at its time (seconds) on one track and sum them."""
    placed = [(max(0, int(round(at * rate))), np.asarray(audio, dtype=np.float32)) for at, audio in pieces]
    total = max((start + len(audio) for start, audio in placed), default=0)
    out = np.zeros(total, dtype=np.float64)
    for start, audio in placed:
        out[start:start + len(audio)] += audio
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 1.0:
        out /= peak
    return out.astype(np.float32), rate


def write_project(phrases: list[Phrase], path: Path) -> Path:
    """One OpenUtau project: each phrase a part at the time it was sung.

    A phrase's part sits where its first word was said inside its own
    recording; on the take's timeline that moves by when the recording
    started. Phrases sung with another voicebank go on a track of their own.
    """
    import yaml

    template = phrases[0].doc
    project = {k: copy.deepcopy(v) for k, v in template.items() if k not in ("tracks", "voice_parts", "wave_parts")}
    project["name"] = "Teto Relay take"
    tracks: list[dict] = []
    singers: dict[str, int] = {}
    parts: list[dict] = []
    for number, phrase in enumerate(phrases, 1):
        doc = phrase.doc
        track = copy.deepcopy(doc["tracks"][0])
        key = f"{track.get('singer')}|{track.get('phonemizer')}"
        if key not in singers:
            singers[key] = len(tracks)
            track["track_name"] = f"Teto {len(tracks) + 1}" if tracks else "Teto"
            tracks.append(track)
        ticks_per_second = float(doc["resolution"]) * float(doc["bpm"]) / 60.0
        for part in doc["voice_parts"]:
            part = copy.deepcopy(part)
            part["track_no"] = singers[key]
            part["position"] = int(part["position"]) + int(round(phrase.offset * ticks_per_second))
            part["name"] = f"{number}. {phrase.text}"[:60]
            parts.append(part)
    project["tracks"] = tracks
    project["voice_parts"] = parts
    project["wave_parts"] = []
    path = Path(path)
    path.write_text(yaml.safe_dump(project, allow_unicode=True, sort_keys=False, default_flow_style=False),
                    encoding="utf-8")
    return path


# ------------------------------------------------------------------ the list
def listing(root: Path, limit: int = 20) -> list[dict]:
    """Finished takes, newest first, from their take.json."""
    root = Path(root)
    out = []
    for folder in sorted(root.glob("take_*"), reverse=True)[:limit]:
        try:
            info = json.loads((folder / "take.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        info["files"] = [name for name in FILES if (folder / name).is_file()]
        out.append(info)
    return out


def take_folder(root: Path, take_id: str) -> Path | None:
    """The folder for `take_id`, or None if that is not one of ours."""
    root = Path(root).resolve()
    if not take_id or not take_id.startswith("take_") or "/" in take_id or "\\" in take_id or ".." in take_id:
        return None
    folder = (root / take_id).resolve()
    return folder if folder.parent == root and folder.is_dir() else None


def zip_take(folder: Path) -> bytes:
    """The take's files in one .zip, for a single download."""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES + ("take.json",):
            if (folder / name).is_file():
                z.write(folder / name, f"{folder.name}/{name}")
    return buffer.getvalue()
