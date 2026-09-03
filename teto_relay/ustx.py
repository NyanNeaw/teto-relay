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


def _pitch_block(note: Note, cfg) -> dict:
    """The note's pitch envelope.

    x is milliseconds relative to the note start and may be negative (the
    lead-in from the previous note); y is cents away from the note's tone.
    """
    if not note.contour:
        # The flat two-point envelope OpenUtau writes for an untouched note.
        data = [{"x": -40.0, "y": 0.0, "shape": "io"}, {"x": 40.0, "y": 0.0, "shape": "io"}]
    else:
        points = list(note.contour)
        # Guarantee a lead-in point at or before the note start.
        if points[0][0] > -40.0:
            points.insert(0, (-40.0, points[0][1]))
        data = [{"x": float(x), "y": float(y), "shape": "io"} for x, y in points]
    return {"data": data, "snap_first": True}


def _note_block(note: Note, part_start: float, cfg) -> dict:
    position = cfg.seconds_to_ticks(note.start - part_start)
    duration = max(1, cfg.seconds_to_ticks(note.duration))
    block = {
        "position": position,
        "duration": duration,
        "tone": int(note.tone),
        "lyric": note.lyric,
        "pitch": _pitch_block(note, cfg),
        "vibrato": {
            "length": 0,
            "period": 175,
            "depth": 25,
            "in": 10,
            "out": 10,
            "shift": 0,
            "drift": 0,
            "vol_link": 0,
        },
        "phoneme_expressions": [],
        "phoneme_overrides": [],
    }
    if note.phonetic_hint:
        # Our own extension to the format. OpenUtau ignores unknown keys, and
        # the renderer feeds this to the phonemizer as a phonetic hint so the
        # English dictionary is bypassed for this word.
        block["phonetic_hint"] = note.phonetic_hint
    return block


def build_project(notes: list[Note], bank: Voicebank, cfg) -> dict:
    """Assemble the .ustx document as a plain dict."""
    if not notes:
        raise ValueError("cannot build a project with no notes")

    part_start = notes[0].start
    note_blocks = [_note_block(n, part_start, cfg) for n in notes]
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
                "curves": [],
            }
        ],
        "wave_parts": [],
    }


def write_ustx(notes: list[Note], path: Path, bank: Voicebank, cfg) -> Path:
    """Serialise a project to `path` and return it."""
    project = build_project(notes, bank, cfg)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(project, allow_unicode=True, sort_keys=False, default_flow_style=False)
    path.write_text(text, encoding="utf-8")
    log.info("Wrote %s (%d notes, singer=%s)", path.name, len(notes), bank.root.name)
    return path


def load_ustx(path: Path) -> dict:
    """Read a .ustx back as a dict (used by tests and validation)."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
