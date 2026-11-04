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
together again from the lines (`assemble`). Its notes can be edited too, as
in a small OpenUtau (`edit_notes`), and a line can be dropped (`delete_line`).

Takes are kept in the data folder's ``takes`` directory.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

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


# ------------------------------------------------------------- editing notes
#: The shortest a note can be made in the panel's note editor.
MIN_EDIT_SECONDS = 0.03
#: The tones the editor accepts (C1 to C8).
TONE_RANGE = (24, 108)
#: How long a lyric typed into the editor may be.
MAX_LYRIC = 32


def _piece_pitch(block: dict, cut_ms: float, length_ms: float, span_ms: float | None = None) -> dict:
    """The source note's pitch curve for a piece of it: the `span_ms` of it
    starting `cut_ms` into the note, stretched (or squeezed) to the piece's
    `length_ms`, so a longer note bends the same way, only slower. x is in ms
    from the note start (see teto_relay.ustx); the lead-in before the note
    keeps its own length."""
    pitch = copy.deepcopy(block.get("pitch") or {})
    points = [dict(p) for p in pitch.get("data") or []]
    scale = length_ms / span_ms if span_ms and span_ms > 0 else 1.0
    for p in points:
        x = float(p.get("x", 0.0)) - cut_ms
        p["x"] = round(x * scale if x > 0 else x, 1)
    inside = [p for p in points if p["x"] <= length_ms + 0.05]
    before = [p for p in inside if p["x"] <= 0]
    # The curve from the piece's own start on, plus the last point before it
    # as the lead-in, so a cut piece starts on the pitch she was at.
    kept = before[-1:] + [p for p in inside if p["x"] > 0]
    if len(kept) < 2:
        y = kept[0]["y"] if kept else 0.0
        kept = [{"x": -40.0, "y": y, "shape": "io"}, {"x": min(40.0, max(10.0, length_ms)), "y": y, "shape": "io"}]
    pitch["data"] = kept
    pitch.setdefault("snap_first", True)
    return pitch


def edit_notes(doc: dict, notes: list[dict], respell: Callable[[str, str | None], str] | None = None) -> dict:
    """The line's project with its notes as the panel's note editor left them.

    Each note is {src, tone, start, dur, lyric, cut, span}: `src` is the
    index of the note it came from, `start` and `dur` are seconds from the
    start of the recording (as doc_notes gives them), `cut` is how far into
    the source note a piece split from it begins, and `span` how much of the
    source it carries (its own length unless it was stretched or squeezed).
    Her tuning follows the notes: a note keeps its source's pitch curve,
    moved with it and stretched to its new length, so the inflection she
    sang survives a move, a resize or a split; vibrato stays on the last
    piece of a split note; and the line's loudness and breath curves move
    and stretch with the notes they were sung on. A note dropped from the
    list is deleted; one stretched over the next is cut back so notes never
    overlap. `respell(lyric, previous)` spells a typed lyric the way the
    voicebank needs (`previous` is the lyric of a note it touches).
    """
    if not isinstance(notes, list) or not notes:
        raise ValueError("a line needs at least one note")
    doc = copy.deepcopy(doc)
    parts = doc.get("voice_parts") or []
    if not parts:
        raise ValueError("this line has no notes to edit")
    part = parts[0]
    sources = part.get("notes") or []
    tps = float(doc["resolution"]) * float(doc["bpm"]) / 60.0
    base = int(part.get("position", 0))
    shortest = max(1, int(round(MIN_EDIT_SECONDS * tps)))

    pieces = []
    for note in notes:
        try:
            src = int(note["src"])
            tone = int(round(float(note["tone"])))
            start, dur = float(note["start"]), float(note["dur"])
            cut = float(note.get("cut") or 0.0)
            # Without one the bend keeps its timing (the editor always sends it).
            span = float(note["span"]) if note.get("span") else None
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"a note is missing something: {exc}") from None
        if not 0 <= src < len(sources):
            raise ValueError(f"no note {src} in this line")
        if (not all(math.isfinite(v) for v in (start, dur, cut, span or dur))
                or start < 0 or dur <= 0 or cut < 0 or (span is not None and span <= 0)):
            raise ValueError("a note's time is out of range")
        lyric = str(note.get("lyric", sources[src].get("lyric", ""))).strip()
        if not lyric or len(lyric) > MAX_LYRIC:
            raise ValueError("a note needs a lyric of 1 to 32 characters")
        # Where the piece's stretch of the source was sung, in ticks from the
        # recording's start: what the line's curves are laid against.
        was = base + int(sources[src].get("position", 0)) + cut * tps
        pieces.append({"src": src, "tone": min(max(tone, TONE_RANGE[0]), TONE_RANGE[1]),
                       "at": int(round(start * tps)), "length": max(shortest, int(round(dur * tps))),
                       "cut": cut, "span": span, "lyric": lyric, "was": (was, was + (span or dur) * tps)})
    pieces.sort(key=lambda p: p["at"])

    # No overlaps: a note running into the next ends where the next begins;
    # two starting together, the later one waits for the first.
    for before, piece in zip(pieces, pieces[1:]):
        end = before["at"] + before["length"]
        if piece["at"] < end:
            if piece["at"] > before["at"]:
                before["length"] = piece["at"] - before["at"]
            else:
                stop = piece["at"] + piece["length"]
                piece["at"] = end
                piece["length"] = max(shortest, stop - end)

    last_cut = {}
    for p in pieces:
        last_cut[p["src"]] = max(last_cut.get(p["src"], -1.0), p["cut"])
    ms_per_tick = 1000.0 / tps
    blocks = []
    previous = None
    for p in pieces:
        source = sources[p["src"]]
        block = copy.deepcopy(source)
        block["tone"] = p["tone"]
        block["pitch"] = _piece_pitch(source, p["cut"] * 1000.0, p["length"] * ms_per_tick,
                                      p["span"] * 1000.0 if p["span"] else None)
        if p["cut"] < last_cut[p["src"]]:
            block.setdefault("vibrato", {})["length"] = 0
        lyric = p["lyric"]
        if lyric != str(source.get("lyric", "")):
            block.pop("phonetic_hint", None)
            if respell is not None and not lyric.startswith("+"):
                touching = previous is not None and previous["at"] + previous["length"] == p["at"]
                lyric = respell(lyric, previous["lyric"] if touching else None)
        block["lyric"] = lyric
        block["position"], block["duration"] = p["at"], p["length"]
        blocks.append(block)
        previous = p

    first = min(b["position"] for b in blocks)
    new_base = min(base, first)
    for block in blocks:
        block["position"] -= new_base
    # The line's curves (loudness, breath) go where the notes went.
    warp = _time_warp([(p["was"][0], p["at"]) for p in pieces] + [(p["was"][1], p["at"] + p["length"]) for p in pieces])
    for curve in part.get("curves") or []:
        curve["xs"], curve["ys"] = _warp_curve(curve.get("xs") or [], curve.get("ys") or [], base, new_base, warp)
    part["position"] = new_base
    part["notes"] = blocks
    part["duration"] = max(b["position"] + b["duration"] for b in blocks)
    return doc


def _time_warp(anchors: list[tuple[float, float]]) -> Callable[[np.ndarray], np.ndarray]:
    """old time -> new time (ticks from the recording's start), through each
    note's (where it was sung, where it is now) at its start and end: linear
    within and between notes, and shifted like the nearest note outside them."""
    pairs: dict[float, float] = {}
    for old, new in sorted(anchors):
        pairs.setdefault(round(old, 3), new)  # one place per moment: the first note there
    olds = np.array(list(pairs.keys()), dtype=float)
    news = np.array(list(pairs.values()), dtype=float)

    def warp(x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        out = np.interp(x, olds, news)
        out = np.where(x < olds[0], x + news[0] - olds[0], out)
        return np.where(x > olds[-1], x + news[-1] - olds[-1], out)

    return warp


def _warp_curve(xs: list, ys: list, base: int, new_base: int, warp) -> tuple[list[int], list[int]]:
    """A part curve's points moved by `warp`: in ticks from the part start
    before (`base`) and after (`new_base`), one value per tick, in order."""
    if not xs:
        return [], []
    moved = warp(np.asarray(xs, dtype=float) + base) - new_base
    points = sorted(zip((int(round(x)) for x in moved), ys), key=lambda p: p[0])
    out_x: list[int] = []
    out_y: list[int] = []
    for x, y in points:
        if out_x and x <= out_x[-1]:
            out_y[-1] = int(y)  # the same tick: the later point wins
            continue
        out_x.append(max(0, x))
        out_y.append(int(y))
    return out_x, out_y


def doc_pitch(doc: dict) -> list[list[list[float]]]:
    """Each note's pitch curve as [[x ms, y tenths of a semitone], ...], for
    the panel's note editor to draw her tuning (index = the note's index)."""
    out = []
    for part in doc.get("voice_parts") or []:
        for note in part.get("notes") or []:
            out.append([[round(float(p.get("x", 0.0)), 1), round(float(p.get("y", 0.0)), 2)]
                        for p in (note.get("pitch") or {}).get("data") or []])
    return out


def delete_line(folder: Path, line_no: int) -> dict | None:
    """Remove one line from a take and put the take together again. A take
    left with no lines is deleted; then this returns None."""
    folder = Path(folder)
    info = read_info(folder)
    kept = [p for p in info["phrases"] if p.get("n") is None or int(p["n"]) != int(line_no)]
    if len(kept) == len(info["phrases"]):
        raise ValueError(f"This take has no line {line_no}.")
    if not kept:
        discard(folder)
        return None
    stem = f"{int(line_no):02d}"
    for name in (f"{stem}.wav", f"{stem}-voice.wav", f"{stem}.ustx", f"{stem}-pitch.npz"):
        (folder / LINES / name).unlink(missing_ok=True)
    info["phrases"] = kept
    assemble(folder, info)
    return detail(folder)


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
            doc = _read_doc(folder / LINES / f"{int(line['n']):02d}.ustx")
            line["notes"] = doc_notes(doc)
            line["pitch"] = doc_pitch(doc)
        except (OSError, KeyError, ValueError, TypeError):
            line["notes"], line["pitch"] = [], []
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
