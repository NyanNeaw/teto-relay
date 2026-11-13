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
in a small OpenUtau (`edit_notes`), and a line can be dropped (`delete_line`)
or moved along the timeline (`move_line`), which needs no singing at all.

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

#: What a take folder is kept as, in take.json's "kind". A recording is what
#: Record makes: a part you sang, with the voicebank and tuning it was sung
#: with, kept on its own so it can be dropped onto any song. A project is a
#: song: lines dropped onto its playlist (copies, so a recording can be used
#: in many), its music and its audio clips. A take saved before there were
#: kinds is a project: it was opened as one.
RECORDING, PROJECT = "recording", "project"
KINDS = (RECORDING, PROJECT)
#: The longest name a project or a recording can be given.
MAX_NAME = 80


def kind_of(info: dict) -> str:
    return RECORDING if info.get("kind") == RECORDING else PROJECT


def voice_of(item: dict) -> dict | None:
    """The voicebank and tuning a line (or a recording) was sung with:
    {"bank", "bank_name", "tuning", "tuning_id", "settings"}, or None for one
    saved before that was kept."""
    voice = item.get("voice")
    return voice if isinstance(voice, dict) and voice.get("bank") else None


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
    voice: dict | None = None                      # the voicebank and tuning that sang it (voice_of)


class Take:
    """One take while it is being recorded; `finish` writes it out."""

    def __init__(self, root: Path):
        self.started = time.monotonic()
        self.created = datetime.now()
        self.folder = Path(root) / self.created.strftime("take_%Y%m%d_%H%M%S")
        self.phrases: list[Phrase] = []
        self.stopped: float | None = None
        # The music it is recorded over (teto_relay.music): the folder it is
        # played from, which the saved take copies, and what the panel calls
        # that folder ("next", or a take's id). `started` is then the moment
        # the music's first sample was heard.
        self.music_from: Path | None = None
        self.music_id = ""
        # How late a line sung on the beat is stamped, s (the Studio delay,
        # cfg.studio_offset_ms): each line is placed that much earlier.
        self.delay = 0.0
        # What it is kept as (see KINDS): a recording, which the Studio's
        # browser offers to drag onto a song project; and the project that
        # was open when it was recorded, if any.
        self.kind = RECORDING
        self.project = ""
        self._lock = threading.Lock()

    @property
    def seconds(self) -> float:
        return time.monotonic() - self.started

    def add(self, job, voice: dict | None = None) -> None:
        """A phrase has been sung: keep it, if it was recorded during the take.
        `voice` is the voicebank and tuning it was sung with (voice_of)."""
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
            offset=max(0.0, start - self.started - self.delay),
            text=" ".join(w["text"] for w in words) or job.text, doc=doc,
            sung=sung, sung_rate=int(rate), sung_origin=float(getattr(job, "wav_origin", 0.0)),
            heard=np.asarray(heard if heard is not None else np.zeros(0), dtype=np.float32),
            heard_rate=int(getattr(job, "input_rate", 16000) or 16000),
            words=words, track=getattr(job, "track", None),
            context=dict(getattr(job, "context", None) or {}),
            style_sung=bool(getattr(job, "sung", False)),
            voice=copy.deepcopy(voice) if voice else None,
        )
        with self._lock:
            self.phrases.append(phrase)

    def lines(self) -> list[dict]:
        """The lines so far, for the panel while recording."""
        with self._lock:
            phrases = sorted(self.phrases, key=lambda p: p.offset)
        # origin and length: where her singing sits in the line's recording,
        # so the panel's timeline can draw the line as a clip.
        return [{"n": n, "at": round(p.offset, 3), "text": p.text, "notes": doc_notes(p.doc),
                 "origin": round(p.sung_origin, 3),
                 "length": round(len(p.sung) / p.sung_rate, 3) if p.sung_rate else 0.0}
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
            "kind": self.kind,
            "phrases": [],
            "files": [],
        }
        if self.project:
            info["project"] = self.project
        if self.music_from is not None and self.music_id:
            # Whose music it was sung over ("next", or a project's id).
            info["sung_over"] = self.music_id
        voices = [p.voice for p in phrases if p.voice]
        if voices:
            info["voice"] = copy.deepcopy(voices[0])
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
                **({"voice": copy.deepcopy(p.voice)} if p.voice else {}),
            })
        # A recording leaves its music where it is (the project it was sung
        # for, or the music waiting for the next take): its lines keep their
        # times against it, so they line up when dropped onto that project.
        if self.music_from is not None and self.kind != RECORDING:
            from . import music

            music.copy_into(self.music_from, self.folder)
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
        # How long her singing is: with "at" and "origin", the line's clip on
        # the timeline (line_span).
        line["length"] = round(len(audio) / rate, 3)
        heard = lines / f"{stem}-voice.wav"
        # A double of a line in this take (a harmony, a stack) was not sung
        # again by you: its guide would be the same voice twice.
        if heard.is_file() and not line.get("double"):
            audio, voice_rate = sf.read(str(heard), dtype="float32", always_2d=False)
            # A line moved so its singing starts at 0:00 has a recording that
            # starts before it; that much of the guide is cut, not shifted.
            early = int(round(-min(0.0, float(line["at"])) * voice_rate))
            voice.append((max(0.0, float(line["at"])), audio[early:]))
        parts.append((line["at"], line["text"], _read_doc(lines / f"{stem}.ustx"), lane_of(line)))
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

    `parts` are (offset, text, doc) or (offset, text, doc, lane) - or
    Phrases. A line's part sits where its first word was said inside its own
    recording; on the take's timeline that moves by when the recording
    started. Each lane of the Studio's playlist is a track of its own, in
    lane order, and so are lines sung with another voicebank.
    """
    parts = [(p.offset, p.text, p.doc, 1) if isinstance(p, Phrase) else (tuple(p) + (1,))[:4] for p in parts]
    template = parts[0][2]
    project = {k: copy.deepcopy(v) for k, v in template.items() if k not in ("tracks", "voice_parts", "wave_parts")}
    project["name"] = "Teto Relay take"
    tracks: list[dict] = []
    singers: dict[str, int] = {}
    voice_parts: list[dict] = []

    def track_key(doc, lane):
        track = doc["tracks"][0]
        return f"{int(lane)}|{track.get('singer')}|{track.get('phonemizer')}"

    # The tracks first, lane by lane, so lane 1 is the first track whatever
    # order the lines are heard in.
    for _, _, doc, lane in sorted(parts, key=lambda p: int(p[3])):
        key = track_key(doc, lane)
        if key not in singers:
            track = copy.deepcopy(doc["tracks"][0])
            singers[key] = len(tracks)
            track["track_name"] = f"Teto {len(tracks) + 1}" if tracks else "Teto"
            tracks.append(track)
    for number, (offset, text, doc, lane) in enumerate(parts, 1):
        key = track_key(doc, lane)
        ticks_per_second = float(doc["resolution"]) * float(doc["bpm"]) / 60.0
        for part in doc["voice_parts"]:
            part = copy.deepcopy(part)
            part["track_no"] = singers[key]
            part["position"] = max(0, int(part["position"]) + int(round(offset * ticks_per_second)))
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


# ------------------------------------------------------- moving lines about
#: How many lanes the Studio's playlist offers, for her lines and for audio.
MAX_LANES = 16


def lane_of(item: dict) -> int:
    """The lane a line (or an audio clip) is on; 1 for anything saved before
    there were lanes."""
    try:
        lane = int(item.get("lane") or 1)
    except (TypeError, ValueError):
        return 1
    return min(MAX_LANES, max(1, lane))


def _lane(value) -> int:
    """A lane asked for by the panel: a whole number from 1 to MAX_LANES."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ValueError("A lane is a whole number.")
    if not 1 <= int(value) <= MAX_LANES:
        raise ValueError(f"Lanes go from 1 to {MAX_LANES}.")
    return int(value)


def _sung_seconds(folder: Path, line: dict) -> float:
    """How long a line's singing is, for a take saved before that was kept."""
    try:
        import soundfile as sf

        return round(float(sf.info(str(Path(folder) / LINES / f"{int(line['n']):02d}.wav")).duration), 3)
    except Exception:  # noqa: BLE001 - no file to measure: go by the notes
        notes = line.get("notes") or []
        end = max((n["start"] + n["dur"] for n in notes), default=0.0)
        return round(max(0.05, end - float(line.get("origin", 0.0))), 3)


def line_span(folder: Path, line: dict) -> tuple[float, float]:
    """Where a line's singing starts and ends on the take's timeline, in
    seconds: its clip. ("at" is where its recording starts, which is earlier
    by the quiet before the first sound.)"""
    length = line.get("length")
    if length is None:
        length = _sung_seconds(folder, line)
    start = float(line["at"]) + float(line.get("origin", 0.0))
    return start, start + float(length)


def free_spot(start: float, length: float, others: list[tuple[float, float]]) -> float:
    """The start nearest to `start` at which a clip `length` long fits: not
    before 0:00 and not over any of `others` (start, end). Clips may touch.
    The panel's timeline does the same while a clip is dragged."""
    busy: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in others):
        if busy and a <= busy[-1][1] + 1e-9:
            busy[-1][1] = max(busy[-1][1], b)
        else:
            busy.append([a, b])
    best: float | None = None
    gap_from = 0.0
    for a, b in busy + [[math.inf, math.inf]]:
        if a - gap_from >= length - 1e-6:
            spot = min(max(start, gap_from), a - length)
            if best is None or abs(spot - start) < abs(best - start):
                best = spot
        gap_from = max(gap_from, b)
    return max(0.0, best if best is not None else gap_from)


def move_line(folder: Path, line_no: int, new_at: float, lane=None) -> dict:
    """Put one line somewhere else on the take's timeline and put the take
    together again. `new_at` is where the line's recording should start (its
    "at"); it lands as near to that as it can without its singing starting
    before 0:00 or running over another line on its lane. `lane` moves it to
    another lane of the playlist (a double, a harmony). Nothing is sung
    again: only teto.wav, your-voice.wav and teto.ustx are rebuilt. Returns
    the take's detail, its lines in the order they are now heard."""
    folder = Path(folder)
    info = read_info(folder)
    line = next((p for p in info["phrases"] if p.get("n") is not None and int(p["n"]) == int(line_no)), None)
    if line is None:
        raise ValueError(f"This take has no line {line_no}.")
    try:
        new_at = math.nan if isinstance(new_at, bool) else float(new_at)
    except (TypeError, ValueError):
        new_at = math.nan
    if not math.isfinite(new_at):
        raise ValueError("A line needs a time to move to.")
    if lane is not None:
        line["lane"] = _lane(lane)
    origin = float(line.get("origin", 0.0))
    start, end = line_span(folder, line)
    others = [line_span(folder, p) for p in info["phrases"] if p is not line and lane_of(p) == lane_of(line)]
    line["at"] = round(free_spot(new_at + origin, end - start, others) - origin, 3)
    info["phrases"].sort(key=lambda p: line_span(folder, p)[0])
    assemble(folder, info)
    return detail(folder)


def shift_lines(folder: Path, milliseconds: float) -> tuple[dict, float]:
    """Move every line of a take earlier (negative) or later by the same
    amount, and put the take together again: for a take recorded before the
    delay was set right. The lines keep their distances, so if the first one
    would start before 0:00 they all go only as far as that allows. Returns
    the take's detail and how far they went, in milliseconds."""
    folder = Path(folder)
    info = read_info(folder)
    try:
        milliseconds = math.nan if isinstance(milliseconds, bool) else float(milliseconds)
    except (TypeError, ValueError):
        milliseconds = math.nan
    if not math.isfinite(milliseconds) or abs(milliseconds) > 600_000:
        raise ValueError("Shift needs a number of milliseconds.")
    if not info["phrases"]:
        raise ValueError("This take has no lines to shift.")
    first = min(line_span(folder, p)[0] for p in info["phrases"])
    seconds = max(milliseconds / 1000.0, -max(0.0, first))
    for line in info["phrases"]:
        line["at"] = round(float(line["at"]) + seconds, 3)
    assemble(folder, info)
    return detail(folder), round(seconds * 1000.0, 1)


#: The most lines a take can hold: a line's files are numbered 01 to 999.
MAX_LINES = 999
#: A line's own files, by the suffix after its number.
LINE_SUFFIXES = (".wav", "-voice.wav", ".ustx", "-pitch.npz")


def new_folder(root: Path) -> Path:
    """A name for a take that does not exist yet, as a recorded take is
    named (take_YYYYMMDD_HHMMSS), with -2, -3... if that second is taken."""
    root = Path(root)
    base = datetime.now().strftime("take_%Y%m%d_%H%M%S")
    folder, n = root / base, 1
    while folder.exists():
        n += 1
        folder = root / f"{base}-{n}"
    return folder


def _new_info(folder: Path, kind: str = PROJECT) -> dict:
    return {"id": Path(folder).name, "created": datetime.now().isoformat(timespec="seconds"),
            "seconds": 0.0, "kind": kind, "phrases": [], "files": []}


def _number(at, what: str) -> float:
    try:
        at = math.nan if isinstance(at, bool) else float(at)
    except (TypeError, ValueError):
        at = math.nan
    if not math.isfinite(at):
        raise ValueError(what)
    return at


def _line_of(info: dict, line_no) -> dict:
    line = next((p for p in info["phrases"]
                 if p.get("n") is not None and int(p["n"]) == int(line_no)), None)
    if line is None:
        raise ValueError(f"That take has no line {line_no}.")
    return line


def copy_line(source: Path, line_no: int, folder: Path, at=None, lane=None) -> dict:
    """Put a copy of one line of `source` into the take in `folder`: the
    Studio's library, dragged onto the timeline, or a double of a line on
    another lane. Its files are copied under the next free number, so it can
    be fixed and moved like any other line, and the take is put together
    again. Nothing is sung again.

    `at` is where the copy's recording should start; it lands as near to
    that as fits on its `lane` (`free_spot`; lane 1 unless asked). Without
    one it goes after the last line there. `folder` may not exist yet: then
    it becomes a new take holding just this line. `source` and `folder` may
    be the same take: a double, whose guide voice is not heard twice.
    Returns the take's detail."""
    if at is not None:
        at = _number(at, "A line needs a time to go to.")
    source = Path(source)
    line = _line_of(read_info(source), line_no)
    start = None if at is None else at + float(line.get("origin", 0.0))
    return copy_lines(source, [line_no], folder, start, lane, after_last=at is None)


def copy_lines(source: Path, line_nos, folder: Path, start=None, lane=None, after_last: bool = False) -> dict:
    """Put copies of lines of `source` into the take in `folder`, all at once:
    a whole recording dropped onto a song project. `line_nos` None is every
    line. They keep their distances from each other: `start` is where the
    first one's singing should begin, and without it each goes where it was
    sung (a part recorded over this project's music lines up with it). Each
    lands as near to that as fits on `lane` (lane 1 unless asked); with
    `after_last` they go after the last line there instead. `folder` may not
    exist yet: then it becomes a new project. Returns the take's detail."""
    source, folder = Path(source), Path(folder)
    lane = 1 if lane is None else _lane(lane)
    src_info = read_info(source)
    if line_nos is None:
        chosen = [p for p in src_info["phrases"] if p.get("n") is not None]
    else:
        chosen = [_line_of(src_info, n) for n in line_nos]
    if not chosen:
        raise ValueError("That recording has no lines.")
    for line in chosen:
        stem = f"{int(line['n']):02d}"
        if not (source / LINES / f"{stem}.wav").is_file() or not (source / LINES / f"{stem}.ustx").is_file():
            raise ValueError("That line's files are missing, so it cannot be copied.")
    if start is not None:
        start = _number(start, "A line needs a time to go to.")

    info = read_info(folder) if (folder / "take.json").is_file() else _new_info(folder)
    taken = [int(p["n"]) for p in info["phrases"] if p.get("n") is not None]
    first_n = max(taken, default=0) + 1
    if first_n + len(chosen) - 1 > MAX_LINES:
        raise ValueError(f"A take holds at most {MAX_LINES} lines.")
    same = source.resolve() == folder.resolve()
    spans = [line_span(source, line) for line in chosen]
    first = min(a for a, _ in spans)
    others = [line_span(folder, p) for p in info["phrases"] if lane_of(p) == lane]
    if after_last:
        start = max((b for _, b in others), default=0.0)
    shift = 0.0 if start is None else start - first

    made = not folder.exists()
    lines = folder / LINES
    lines.mkdir(parents=True, exist_ok=True)
    stems: list[str] = []
    try:
        for i, (line, (a, b)) in enumerate(sorted(zip(chosen, spans), key=lambda x: x[1][0])):
            stem, n = f"{int(line['n']):02d}", first_n + i
            copy_ = copy.deepcopy(line)
            for key in ("notes", "pitch"):
                copy_.pop(key, None)
            copy_["n"], copy_["lane"] = n, lane
            if same:
                copy_["double"] = True
            else:
                copy_.pop("double", None)
            origin = float(copy_.get("origin", 0.0))
            spot = free_spot(a + shift, b - a, others)
            copy_["at"] = round(spot - origin, 3)
            others.append((spot, spot + b - a))
            new_stem = f"{n:02d}"
            stems.append(new_stem)
            for suffix in LINE_SUFFIXES:
                if (source / LINES / f"{stem}{suffix}").is_file():
                    shutil.copy2(source / LINES / f"{stem}{suffix}", lines / f"{new_stem}{suffix}")
            info["phrases"].append(copy_)
        info["phrases"].sort(key=lambda p: line_span(folder, p)[0])
        assemble(folder, info)
    except Exception:
        # A half-made new take would show in the list with no take.json.
        if made:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            for new_stem in stems:
                for suffix in LINE_SUFFIXES:
                    (lines / f"{new_stem}{suffix}").unlink(missing_ok=True)
        raise
    return detail(folder)


def rename(folder: Path, name) -> dict:
    """Give a project or a recording a name of its own ("" goes back to its
    date). Returns its detail."""
    if not isinstance(name, str):
        raise ValueError("A name is text.")
    name = " ".join(name.split())[:MAX_NAME]
    folder = Path(folder)
    info = read_info(folder)
    if name:
        info["name"] = name
    else:
        info.pop("name", None)
    _save_info(folder, info)
    return detail(folder)


def duplicate(folder: Path, root: Path, name: str = "") -> Path:
    """A copy of a whole take, under a new id, with its own name: a
    recording to sing again with another voice, keeping the first one."""
    folder = Path(folder)
    target = new_folder(root)
    shutil.copytree(folder, target)
    try:
        info = read_info(target)
        info["id"] = target.name
        info["created"] = datetime.now().isoformat(timespec="seconds")
        if name:
            info["name"] = " ".join(name.split())[:MAX_NAME]
        _save_info(target, info)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


# ----------------------------------------------------- audio clips (layers)
#: Audio dropped on the Studio's playlist - a beat, a bounce from a DAW, a
#: backing vocal - kept in the take's own folder as audio/NN.<ext>, with
#: where it sits (lane, at) in take.json under "audio". It plays with the
#: take and goes into mix.wav; teto.wav stays her singing alone.
AUDIO = "audio"
AUDIO_FILE = re.compile(r"^audio/(\d{2,3})\.(wav|mp3|flac|ogg)$")
MAX_AUDIO = 99


def _clip_span(clip: dict) -> tuple[float, float]:
    start = float(clip["at"])
    return start, start + float(clip["seconds"])


def _find_clip(info: dict, clip_id) -> dict:
    clip = next((c for c in info.get("audio") or [] if int(c["id"]) == int(clip_id)), None)
    if clip is None:
        raise ValueError(f"This take has no audio clip {clip_id}.")
    return clip


def _time(at) -> float:
    try:
        at = math.nan if isinstance(at, bool) else float(at)
    except (TypeError, ValueError):
        at = math.nan
    if not math.isfinite(at):
        raise ValueError("An audio clip needs a time to go to.")
    return at


def _save_info(folder: Path, info: dict) -> None:
    (Path(folder) / "take.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")


def add_audio(folder: Path, data: bytes, filename: str, at=0.0, lane=1) -> dict:
    """Lay an audio file on the take's playlist, on audio `lane`, as near to
    `at` seconds as fits without running over another clip there. The file
    is checked as music is (music.save_audio); its waveform is kept beside
    it. Returns the take's detail."""
    from . import music

    folder = Path(folder)
    info = read_info(folder)
    lane, at = _lane(lane), _time(at)
    clips = info.setdefault("audio", [])
    n = max((int(c["id"]) for c in clips), default=0) + 1
    if n > MAX_AUDIO:
        raise ValueError(f"A take holds at most {MAX_AUDIO} audio clips.")
    found = music.save_audio(data, filename, folder / AUDIO / f"{n:02d}")
    path = folder / AUDIO / found["file"]
    try:
        audio, rate = read_audio(path)
        seconds = round(len(audio) / rate, 3)
        count = min(music.MAX_PEAKS, max(1, int(round(seconds * music.PEAKS_PER_SECOND))))
        (folder / AUDIO / f"{n:02d}-peaks.json").write_text(json.dumps(music.peaks_of(audio, count)), encoding="utf-8")
        others = [_clip_span(c) for c in clips if lane_of(c) == lane]
        clips.append({"id": n, "name": found["name"], "file": f"{AUDIO}/{found['file']}", "seconds": seconds,
                      "lane": lane, "at": round(free_spot(at, seconds, others), 3)})
        _save_info(folder, info)
    except Exception:
        path.unlink(missing_ok=True)
        (folder / AUDIO / f"{n:02d}-peaks.json").unlink(missing_ok=True)
        raise
    log.info("Audio added to %s: %s on lane %d", folder.name, found["name"], lane)
    return detail(folder)


def read_audio(path: Path):
    """An audio file as float32 frames x channels, and its rate."""
    import soundfile as sf

    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    return audio, int(rate)


def move_audio(folder: Path, clip_id, at, lane=None) -> dict:
    """Put an audio clip somewhere else: another time, another audio lane."""
    folder = Path(folder)
    info = read_info(folder)
    clip = _find_clip(info, clip_id)
    at = _time(at)
    if lane is not None:
        clip["lane"] = _lane(lane)
    others = [_clip_span(c) for c in info["audio"] if c is not clip and lane_of(c) == lane_of(clip)]
    clip["at"] = round(free_spot(at, float(clip["seconds"]), others), 3)
    _save_info(folder, info)
    return detail(folder)


def delete_audio(folder: Path, clip_id) -> dict:
    """Take an audio clip off the playlist and delete its file."""
    folder = Path(folder)
    info = read_info(folder)
    clip = _find_clip(info, clip_id)
    info["audio"] = [c for c in info["audio"] if c is not clip]
    path = folder / str(clip["file"])
    if AUDIO_FILE.match(str(clip["file"])):
        path.unlink(missing_ok=True)
    (folder / AUDIO / f"{int(clip['id']):02d}-peaks.json").unlink(missing_ok=True)
    _save_info(folder, info)
    return detail(folder)


def audio_layers(folder: Path) -> list:
    """The take's audio clips as (seconds, audio, rate), for mix.wav."""
    folder = Path(folder)
    try:
        clips = read_info(folder).get("audio") or []
    except (OSError, ValueError):
        return []
    out = []
    for clip in clips:
        path = folder / str(clip.get("file", ""))
        if AUDIO_FILE.match(str(clip.get("file", ""))) and path.is_file():
            audio, rate = read_audio(path)
            out.append((float(clip["at"]), audio, rate))
    return out


def _audio_detail(folder: Path, clips: list) -> list:
    out = []
    for clip in clips:
        clip = dict(clip)
        try:
            clip["peaks"] = json.loads((Path(folder) / AUDIO / f"{int(clip['id']):02d}-peaks.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            clip["peaks"] = []
        out.append(clip)
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
    info["kind"] = kind_of(info)
    info["files"] = [name for name in FILES if (folder / name).is_file()]
    for line in info["phrases"]:
        try:
            doc = _read_doc(folder / LINES / f"{int(line['n']):02d}.ustx")
            line["notes"] = doc_notes(doc)
            line["pitch"] = doc_pitch(doc)
        except (OSError, KeyError, ValueError, TypeError):
            line["notes"], line["pitch"] = [], []
        if line.get("length") is None:
            line["length"] = _sung_seconds(folder, line)
        line.pop("context", None)
    info["music"] = _music(folder)
    info["audio"] = _audio_detail(folder, info.get("audio") or [])
    return info


def _music(folder: Path) -> dict | None:
    """The take's music track, if it has one: {name, seconds, ...}."""
    from . import music

    try:
        return music.info(folder)
    except Exception:  # noqa: BLE001 - a music file that will not open is no music
        return None


# ------------------------------------------------------------------ the list
def listing(root: Path, limit: int = 200, kind: str | None = None) -> list[dict]:
    """Finished takes, newest first, from their take.json; with `kind`,
    only the recordings or only the projects."""
    root = Path(root)
    out = []
    for folder in sorted(root.glob("take_*"), reverse=True):
        if len(out) >= limit:
            break
        try:
            info = read_info(folder)
        except (OSError, ValueError):
            continue
        info["kind"] = kind_of(info)
        if kind is not None and info["kind"] != kind:
            continue
        info["files"] = [name for name in FILES if (folder / name).is_file()]
        for line in info.get("phrases", []):
            line.pop("context", None)
            line.pop("words", None)
        info["music"] = _music(folder)
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
    if name in FILES or LINE_FILE.match(name) or AUDIO_FILE.match(name):
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
        from .music import music_file

        song = music_file(folder)
        for name in FILES + ("take.json",) + ((song.name,) if song is not None else ()):
            if (folder / name).is_file():
                z.write(folder / name, f"{folder.name}/{name}")
        # The audio clips laid on its playlist, under the names they came with.
        for clip in (read_info(folder).get("audio") or []) if (folder / "take.json").is_file() else []:
            path = folder / str(clip.get("file", ""))
            if AUDIO_FILE.match(str(clip.get("file", ""))) and path.is_file():
                z.write(path, f"{folder.name}/{AUDIO}/{int(clip['id']):02d}-{Path(str(clip['name'])).name}")
    return buffer.getvalue()
