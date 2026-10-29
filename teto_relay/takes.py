"""Takes: record a stretch of singing as one audio file and one OpenUtau project.

For a song cover: press Record in the panel and sing over the backing track
(no key needed while recording: each pause ends a line), press it again.
Every line lands on one timeline at the moment it was sung, so the result
lines up with the music:

* ``teto.wav``       - Teto's singing, the whole take (with Double voice
                       where it applied);
* ``teto.ustx``      - an OpenUtau project: each line is a part on one
                       track at the time it was sung, notes, lyrics and pitch
                       curves included, to re-tune or re-voice in OpenUtau;
* ``your-voice.wav`` - what the microphone heard, on the same timeline, as a
                       guide track;
* ``take.json``      - when it was made and what was sung.

Each line also keeps its own files in ``lines/`` - her singing, its project,
what the microphone heard and its pitch - so a word whisper got wrong can be
fixed afterwards: the line is built again from the corrected words, with the
timing and the pitch it was sung with, sung again, and the whole take is put
together again from the lines (`assemble`).

Takes are kept in the data folder's ``takes`` directory.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

#: The files a finished take holds, in the order the panel offers them.
FILES = ("teto.wav", "teto.ustx", "your-voice.wav")
LINES = "lines"
#: A line's own files, as the panel may ask for them: lines/03.wav.
LINE_FILE = re.compile(r"^lines/(\d{2,3})\.wav$")


@dataclass
class Phrase:
    offset: float          # where the phrase's recording starts in the take, s
    text: str              # what was said (the words), for the list
    doc: dict              # its .ustx document
    sung: np.ndarray       # her singing
    sung_rate: int
    sung_origin: float     # where in the phrase's recording the singing starts, s
    heard: np.ndarray      # the microphone
    heard_rate: int
    # What it takes to build the line again from corrected words (TetoRelay.resing):
    words: list = field(default_factory=list)      # [{"text", "start", "end"}]
    track: object = None                           # pitch.F0Track
    context: dict = field(default_factory=dict)    # build_notes' running state
    style_sung: bool = False                       # Double voice applies


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
        words = [{"text": w.text, "start": float(w.start), "end": float(w.end)}
                 for w in (getattr(job, "words", None) or [])]
        heard = getattr(job, "input_audio", None)
        phrase = Phrase(
            offset=max(0.0, start - self.started),
            text=" ".join(w["text"] for w in words) or job.text, doc=doc,
            sung=sung, sung_rate=int(rate), sung_origin=float(getattr(job, "wav_origin", 0.0)),
            heard=np.asarray(heard if heard is not None else np.zeros(0), dtype=np.float32),
            heard_rate=int(getattr(job, "input_rate", 16000) or 16000),
            words=words, track=getattr(job, "track", None),
            context=dict(getattr(job, "context", None) or {}),
            style_sung=bool(getattr(job, "sung", False)),
        )
        with self._lock:
            self.phrases.append(phrase)

    def lines(self) -> list[dict]:
        """The lines so far, for the panel while recording."""
        with self._lock:
            phrases = sorted(self.phrases, key=lambda p: p.offset)
        return [{"n": n, "at": round(p.offset, 3), "text": p.text, "notes": doc_notes(p.doc)}
                for n, p in enumerate(phrases, 1)]

    # ------------------------------------------------------------------ write
    def finish(self) -> dict:
        """Write the take's files; returns its summary (as take.json holds it).

        A take nothing was sung in is not kept: there would be nothing to
        download. Its summary says so ("discarded")."""
        with self._lock:
            phrases = sorted(self.phrases, key=lambda p: p.offset)
        info = {
            "id": self.folder.name,
            "created": self.created.isoformat(timespec="seconds"),
            "seconds": round(self.seconds, 2),
            "phrases": [],
            "files": [],
        }
        if not phrases:
            log.info("Take not kept: nothing was sung during it (%.1fs)", info["seconds"])
            return {**info, "discarded": True}
        import soundfile as sf

        lines = self.folder / LINES
        lines.mkdir(parents=True, exist_ok=True)
        for n, p in enumerate(phrases, 1):
            stem = f"{n:02d}"
            sf.write(str(lines / f"{stem}.wav"), p.sung, p.sung_rate, subtype="PCM_16")
            if p.heard.size:
                sf.write(str(lines / f"{stem}-voice.wav"), p.heard, p.heard_rate, subtype="PCM_16")
            _write_doc(p.doc, lines / f"{stem}.ustx")
            if p.track is not None:
                save_track(p.track, lines / f"{stem}-pitch.npz")
            info["phrases"].append({
                "n": n, "at": round(p.offset, 3), "text": p.text, "words": p.words,
                "origin": p.sung_origin, "sung": p.style_sung, "context": p.context,
                "editable": bool(p.words) and p.track is not None and p.heard.size > 0,
                "edited": False,
            })
        info = assemble(self.folder, info)
        log.info("Take saved: %s (%d phrase(s))", self.folder, len(phrases))
        return info


# ---------------------------------------------------------------- the files
def assemble(folder: Path, info: dict | None = None) -> dict:
    """Put the take together from its lines: teto.wav, your-voice.wav,
    teto.ustx and take.json. Run when it is first written and after a line
    is sung again."""
    import soundfile as sf

    folder = Path(folder)
    info = info if info is not None else read_info(folder)
    lines = folder / LINES
    sung, voice, parts = [], [], []
    rate = voice_rate = None
    for line in info["phrases"]:
        stem = f"{int(line['n']):02d}"
        audio, rate = sf.read(str(lines / f"{stem}.wav"), dtype="float32", always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        sung.append((line["at"] + float(line.get("origin", 0.0)), audio))
        heard = lines / f"{stem}-voice.wav"
        if heard.is_file():
            audio, voice_rate = sf.read(str(heard), dtype="float32", always_2d=False)
            voice.append((line["at"], audio))
        parts.append((line["at"], line["text"], _read_doc(lines / f"{stem}.ustx")))
    mixed, rate = mix(sung, rate)
    sf.write(str(folder / "teto.wav"), mixed, rate, subtype="PCM_16")
    if voice:
        heard, voice_rate = mix(voice, voice_rate)
        sf.write(str(folder / "your-voice.wav"), heard, voice_rate, subtype="PCM_16")
    write_project(parts, folder / "teto.ustx")
    info["files"] = [name for name in FILES if (folder / name).is_file()]
    info["length"] = round(len(mixed) / rate, 2)
    (folder / "take.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def read_info(folder: Path) -> dict:
    return json.loads((Path(folder) / "take.json").read_text(encoding="utf-8"))


def _write_doc(doc: dict, path: Path) -> None:
    import yaml

    Path(path).write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False),
                          encoding="utf-8")


def _read_doc(path: Path) -> dict:
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def save_track(track, path: Path) -> None:
    np.savez_compressed(str(path), times=track.times, f0=track.f0, voiced=track.voiced,
                        sample_rate=np.int64(track.sample_rate), method=np.array(str(track.method or "")))


def load_track(path: Path):
    from .pitch import F0Track

    with np.load(str(path), allow_pickle=False) as data:
        return F0Track(times=data["times"], f0=data["f0"], voiced=data["voiced"].astype(bool),
                       sample_rate=int(data["sample_rate"]), method=str(data["method"]))


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


def write_project(parts: list, path: Path) -> Path:
    """One OpenUtau project: each line a part at the time it was sung.

    `parts` are (offset, text, doc) - or Phrases. A line's part sits where
    its first word was said inside its own recording; on the take's timeline
    that moves by when the recording started. Lines sung with another
    voicebank go on a track of their own.
    """
    parts = [(p.offset, p.text, p.doc) if isinstance(p, Phrase) else p for p in parts]
    template = parts[0][2]
    project = {k: copy.deepcopy(v) for k, v in template.items() if k not in ("tracks", "voice_parts", "wave_parts")}
    project["name"] = "Teto Relay take"
    tracks: list[dict] = []
    singers: dict[str, int] = {}
    voice_parts: list[dict] = []
    for number, (offset, text, doc) in enumerate(parts, 1):
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
            part["position"] = int(part["position"]) + int(round(offset * ticks_per_second))
            part["name"] = f"{number}. {text}"[:60]
            voice_parts.append(part)
    project["tracks"] = tracks
    project["voice_parts"] = voice_parts
    project["wave_parts"] = []
    path = Path(path)
    _write_doc(project, path)
    return path


def doc_notes(doc: dict) -> list[dict]:
    """A line's notes in seconds from the start of its recording, for the
    panel's piano roll: [{lyric, tone, start, dur}]."""
    ticks_per_second = float(doc["resolution"]) * float(doc["bpm"]) / 60.0
    out = []
    for part in doc.get("voice_parts") or []:
        base = int(part.get("position", 0))
        for note in part.get("notes") or []:
            out.append({"lyric": str(note.get("lyric", "")), "tone": int(note.get("tone", 60)),
                        "start": round((base + int(note["position"])) / ticks_per_second, 3),
                        "dur": round(int(note["duration"]) / ticks_per_second, 3)})
    return out


# ------------------------------------------------------------- fixing words
def respace(words: list[dict], texts: list[str]) -> list[dict]:
    """The line's words with the corrected `texts`, one per original word.

    A text of several words shares its word's time evenly; an empty one
    drops the word. Timing otherwise stays as it was sung.
    """
    if len(texts) != len(words):
        raise ValueError(f"expected {len(words)} word(s), got {len(texts)}")
    out = []
    for word, text in zip(words, texts):
        parts = str(text).split()
        if not parts:
            continue
        start, end = float(word["start"]), float(word["end"])
        step = (end - start) / len(parts)
        for i, part in enumerate(parts):
            out.append({"text": part, "start": round(start + i * step, 4),
                        "end": round(end if i == len(parts) - 1 else start + (i + 1) * step, 4)})
    if not out:
        raise ValueError("a line needs at least one word")
    return out


_EDIT_LOCKS: dict[str, threading.Lock] = {}
_EDIT_LOCKS_GUARD = threading.Lock()


def edit_lock(folder: Path) -> threading.Lock:
    """One edit at a time per take: lines are re-sung and the take re-mixed."""
    with _EDIT_LOCKS_GUARD:
        return _EDIT_LOCKS.setdefault(str(Path(folder).resolve()), threading.Lock())


def detail(folder: Path) -> dict:
    """A finished take with each line's words and notes, for the panel."""
    folder = Path(folder)
    info = read_info(folder)
    info["files"] = [name for name in FILES if (folder / name).is_file()]
    for line in info["phrases"]:
        try:
            line["notes"] = doc_notes(_read_doc(folder / LINES / f"{int(line['n']):02d}.ustx"))
        except (OSError, KeyError, ValueError, TypeError):
            line["notes"] = []
        line.pop("context", None)
    return info


# ------------------------------------------------------------------ the list
def listing(root: Path, limit: int = 20) -> list[dict]:
    """Finished takes, newest first, from their take.json."""
    root = Path(root)
    out = []
    for folder in sorted(root.glob("take_*"), reverse=True)[:limit]:
        try:
            info = read_info(folder)
        except (OSError, ValueError):
            continue
        info["files"] = [name for name in FILES if (folder / name).is_file()]
        for line in info.get("phrases", []):
            line.pop("context", None)
            line.pop("words", None)
        out.append(info)
    return out


def take_folder(root: Path, take_id: str) -> Path | None:
    """The folder for `take_id`, or None if that is not one of ours."""
    root = Path(root).resolve()
    if not take_id or not take_id.startswith("take_") or "/" in take_id or "\\" in take_id or ".." in take_id:
        return None
    folder = (root / take_id).resolve()
    return folder if folder.parent == root and folder.is_dir() else None


def take_file(folder: Path, name: str) -> Path | None:
    """One of the take's files the panel may fetch, or None."""
    if name in FILES or LINE_FILE.match(name):
        path = Path(folder) / name
        return path if path.is_file() else None
    return None


def discard(folder: Path) -> None:
    """Delete a take (the panel's Discard)."""
    shutil.rmtree(folder)


def zip_take(folder: Path) -> bytes:
    """The take's files in one .zip, for a single download."""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES + ("take.json",):
            if (folder / name).is_file():
                z.write(folder / name, f"{folder.name}/{name}")
    return buffer.getvalue()
