"""Stage 4 - write an OpenUtau .ustx project.

The reference implementation built this file with `str.replace` on a template,
which corrupts the YAML the moment a lyric contains a quote or colon. We build
a plain dict and let PyYAML serialise it.

Only the fields OpenUtau genuinely needs are written. The large `expressions`
block is deliberately omitted: OpenUtau repopulates defaults on load, and
hand-copying a version-specific block is a good way to produce a file that
loads on one build and not the next. `tools/validate_ustx.py` round-trips the
output through OpenUtau's own reader to prove this holds.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .notes import Note
from .voicebank import Voicebank

log = logging.getLogger(__name__)

USTX_VERSION = "0.6"


#: OpenUtau's pitch points are in tenths of a semitone, not cents: y=30 sings
#: three semitones up. Note.contour is in cents, so it is divided on the way
#: out. Writing cents as-is made every inflection ten times too big - a
#: "hello" shown as tone 62 was sung at MIDI 86.7.
CENTS_PER_PITCH_UNIT = 10.0


def _pitch_block(note: Note, cfg) -> dict:
    """The note's pitch envelope.

    x is milliseconds relative to the note start and may be negative (the
    lead-in from the previous note); y is in OpenUtau's unit, tenths of a
    semitone away from the note's tone (see CENTS_PER_PITCH_UNIT).
    """
    # With snap_first, the first point sits on the previous note's pitch, so
    # the glide from it takes lead-in + the next point's x.
    lead = float(note.lead_in_ms) if note.lead_in_ms is not None else 40.0
    if not note.contour:
        # The flat two-point envelope OpenUtau writes for an untouched note.
        data = [{"x": -lead, "y": 0.0, "shape": "io"}, {"x": max(lead, 10.0), "y": 0.0, "shape": "io"}]
    else:
        points = list(note.contour)
        # Guarantee a lead-in point at or before the note start.
        if points[0][0] > -lead:
            points.insert(0, (-lead, points[0][1]))
        data = [{"x": float(x), "y": float(y) / CENTS_PER_PITCH_UNIT, "shape": "io"}
                for x, y in points]
    return {"data": data, "snap_first": True}


def _vibrato_block(note: Note) -> dict:
    """OpenUtau's vibrato fields. Length 0 (the default) means no vibrato."""
    block = {"length": 0, "period": 175, "depth": 25, "in": 10, "out": 10,
             "shift": 0, "drift": 0, "vol_link": 0}
    if note.vibrato:
        block.update({k: v for k, v in note.vibrato.items() if k in block})
    return block


def _note_block(note: Note, part_start: float, cfg) -> dict:
    position = cfg.seconds_to_ticks(note.start - part_start)
    duration = max(1, cfg.seconds_to_ticks(note.duration))
    block = {
        "position": position,
        "duration": duration,
        "tone": int(note.tone),
        "lyric": note.lyric,
        "pitch": _pitch_block(note, cfg),
        "vibrato": _vibrato_block(note),
        "phoneme_expressions": [],
        "phoneme_overrides": [],
    }
    if note.phonetic_hint:
        # Our own extension to the format. OpenUtau ignores unknown keys, and
        # the renderer feeds this to the phonemizer as a phonetic hint so the
        # English dictionary is bypassed for this word.
        block["phonetic_hint"] = note.phonetic_hint
    return block


def _space_in_ticks(notes: list[Note], blocks: list[dict], cfg) -> list[dict]:
    """Keep the spacing the notes were given, after rounding to ticks.

    Position and duration are each rounded to a tick, so a gap of a tick or
    two in seconds could round to zero - notes touching, which collapses the
    phonemizer - or even to an overlap. So:

    * notes with a gap in seconds keep at least one tick between them;
    * notes that touch in seconds (or are marked legato) may touch;
    * notes never overlap.
    """
    previous_end = None
    previous_note = None
    for note, block in zip(notes, blocks):
        if previous_end is not None:
            gapped = note.start > previous_note.end and not note.legato
            earliest = previous_end + (1 if gapped else 0)
            if block["position"] < earliest:
                moved = earliest - block["position"]
                block["position"] = earliest
                block["duration"] = max(1, block["duration"] - moved)
        previous_end = block["position"] + block["duration"]
        previous_note = note
    return blocks


def _curve_blocks(curves: dict | None, part_start: float, cfg) -> list[dict]:
    """Part curves (teto_relay.performance) in OpenUtau's form: ticks from the
    part start, integer values, one value per tick."""
    blocks = []
    for abbr, points in (curves or {}).items():
        xs, ys = [], []
        for seconds, value in points:
            x = cfg.seconds_to_ticks(seconds - part_start)
            if xs and x <= xs[-1]:
                xs[-1], ys[-1] = xs[-1], int(round(value))  # same tick: later wins
                continue
            xs.append(x)
            ys.append(int(round(value)))
        if xs:
            blocks.append({"xs": xs, "ys": ys, "abbr": abbr})
    return blocks


def build_project(notes: list[Note], bank: Voicebank, cfg, curves: dict | None = None) -> dict:
    """Assemble the .ustx document as a plain dict."""
    if not notes:
        raise ValueError("cannot build a project with no notes")

    part_start = notes[0].start
    note_blocks = _space_in_ticks(notes, [_note_block(n, part_start, cfg) for n in notes], cfg)
    part_duration = max(b["position"] + b["duration"] for b in note_blocks)
    phonemizer = cfg.phonemizer or bank.phonemizer

    return {
        "name": "Teto Relay",
        "comment": "",
        "output_dir": "Vocal",
        "cache_dir": "UCache",
        "ustx_version": USTX_VERSION,
        "resolution": cfg.resolution,
        "bpm": cfg.bpm,
        "beat_per_bar": 4,
        "beat_unit": 4,
        "time_signatures": [{"bar_position": 0, "beat_per_bar": 4, "beat_unit": 4}],
        "tempos": [{"position": 0, "bpm": cfg.bpm}],
        "tracks": [
            {
                # OpenUtau resolves a singer by its folder id first.
                "singer": bank.root.name,
                "phonemizer": phonemizer,
                "renderer_settings": {"renderer": cfg.renderer},
                "track_name": "Track1",
                "track_color": "Blue",
                "mute": False,
                "solo": False,
                "volume": 0.0,
                "pan": 0.0,
            }
        ],
        "voice_parts": [
            {
                "name": "Relay",
                "comment": "",
                "track_no": 0,
                "position": cfg.seconds_to_ticks(part_start),
                "duration": part_duration,
                "notes": note_blocks,
                "curves": _curve_blocks(curves, part_start, cfg),
            }
        ],
        "wave_parts": [],
    }


def write_ustx(notes: list[Note], path: Path, bank: Voicebank, cfg, curves: dict | None = None) -> Path:
    """Serialise a project to `path` and return it."""
    project = build_project(notes, bank, cfg, curves)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(project, allow_unicode=True, sort_keys=False, default_flow_style=False)
    path.write_text(text, encoding="utf-8")
    log.info("Wrote %s (%d notes, singer=%s)", path.name, len(notes), bank.root.name)
    return path


def load_ustx(path: Path) -> dict:
    """Read a .ustx back as a dict (used by tests and validation)."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
