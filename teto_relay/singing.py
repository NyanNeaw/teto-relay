"""Making the notes sound sung rather than spoken (`singing_style: "sung"`).

The default pipeline reproduces speech: each note sits exactly on the pitch you
spoke at, rounded to a semitone, with your intonation as a pitch curve, and is
held for as long as the word took to say. That is faithful, and it sounds like
someone reading aloud through a synthesiser.

The sung style keeps your melody's shape but makes it musical:

* **In a key.** Every note is moved to the nearest note of a scale. The key is
  found from the phrase itself (the key that needs the smallest total nudge)
  and then held between phrases, so the tune does not change key every time
  you speak.
* **Steadier notes.** The spoken pitch curve inside each note is scaled down
  (`sung_contour_amount`), so notes hold a pitch instead of sliding around.
* **Vibrato** on notes long enough to carry it (`vibrato_min_seconds`).
* **A held last note** (`final_hold_seconds`), the way a sung phrase ends.

Nothing here runs unless `singing_style` is "sung". It works on the Note list
only, so it is fully unit-testable; how OpenUtau renders the vibrato is the
part that needs real hardware.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

#: Semitones above the tonic that belong to each scale.
SCALES: dict[str, tuple[int, ...]] = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "pentatonic": (0, 2, 4, 7, 9),
    "chromatic": tuple(range(12)),
}

#: How much worse (in total semitones of nudging) the held key may fit a new
#: phrase before a better key replaces it.
KEY_STICKINESS = 1.5


def _degrees(tonic: int, scale: str) -> tuple[int, ...]:
    return tuple((tonic + step) % 12 for step in SCALES[scale])


def snap(pitch: float, tonic: int, scale: str) -> int:
    """The scale note nearest to `pitch` (a MIDI number, possibly fractional)."""
    allowed = _degrees(tonic, scale)
    base = int(round(pitch))
    best, best_distance = base, None
    for candidate in range(base - 6, base + 7):
        if candidate % 12 in allowed:
            distance = abs(candidate - pitch)
            if best_distance is None or distance < best_distance - 1e-9:
                best, best_distance = candidate, distance
    return best


def fit_cost(pitches: list[float], tonic: int, scale: str) -> float:
    """Total distance the pitches must move to land in this key."""
    return sum(abs(snap(p, tonic, scale) - p) for p in pitches)


def choose_key(pitches: list[float], scale: str, held: int | None = None) -> int:
    """The tonic (0 = C) that fits these pitches best, preferring `held`."""
    costs = {tonic: fit_cost(pitches, tonic, scale) for tonic in range(12)}
    best = min(costs, key=lambda t: (costs[t], t))
    if held is not None and costs[held] <= costs[best] + KEY_STICKINESS:
        return held
    return best


def key_from_config(value: str) -> int | None:
    """"auto" -> None, "F#" -> 6. Accepts flats too ("Bb")."""
    text = (value or "auto").strip()
    if text.lower() == "auto":
        return None
    flats = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
    name = flats.get(text[:1].upper() + text[1:], text[:1].upper() + text[1:])
    if name not in NOTE_NAMES:
        raise ValueError(f"scale_key {value!r} is not a note name (C, C#, D, ... B) or 'auto'")
    return NOTE_NAMES.index(name)


#: Hand-tuning conventions for UTAU/vocal synths, applied as pitch-curve shapes
#: (cents; x in ms from the note start). On a jump of at least
#: OVERSHOOT_MIN_INTERVAL semitones the voice passes the new note by
#: OVERSHOOT_CENTS at OVERSHOOT_PEAK_MS and settles by OVERSHOOT_SETTLE_MS;
#: the phrase's last note falls by END_FALL_CENTS over its last END_FALL_MS.
OVERSHOOT_MIN_INTERVAL = 2
OVERSHOOT_CENTS = 30.0
OVERSHOOT_PEAK_MS = 70.0
OVERSHOOT_SETTLE_MS = 160.0
END_FALL_CENTS = 80.0
END_FALL_MS = 150.0


def _add_shape(note, shape: list[tuple[float, float]]) -> None:
    """Add a pitch shape (cents) on top of the note's own contour."""
    import numpy as np

    base = sorted(note.contour) if note.contour else [(0.0, 0.0)]
    xs = sorted({x for x, _ in base} | {x for x, _ in shape})
    bx, by = [x for x, _ in base], [y for _, y in base]
    sx, sy = [x for x, _ in shape], [y for _, y in shape]
    note.contour = [
        (x, round(float(np.interp(x, bx, by)) + float(np.interp(x, sx, sy, left=0.0, right=0.0)), 1))
        for x in xs
    ]


def shape_transitions(notes: list) -> list:
    """Overshoot on jumps between notes, and a fall at the end of the phrase.

    These are what tuners draw by hand to stop a synth sounding "placed":
    notes that land dead on pitch and stop dead read as a machine, while a
    voice passes a note it leaps to and lets go of the last one.
    """
    for previous, note in zip(notes, notes[1:]):
        interval = note.tone - previous.tone
        if abs(interval) < OVERSHOOT_MIN_INTERVAL:
            continue
        span = note.duration * 1000.0
        if span < OVERSHOOT_SETTLE_MS * 1.5:
            continue  # too short to land, pass and settle
        direction = 1.0 if interval > 0 else -1.0
        _add_shape(note, [(0.0, 0.0), (OVERSHOOT_PEAK_MS, direction * OVERSHOOT_CENTS),
                          (OVERSHOOT_SETTLE_MS, 0.0)])
    if notes:
        last = notes[-1]
        span = last.duration * 1000.0
        if span >= END_FALL_MS * 2:
            _add_shape(last, [(span - END_FALL_MS, 0.0), (span, -END_FALL_CENTS)])
    return notes


def musicalize(notes: list, cfg, state: dict | None = None) -> list:
    """Apply the sung style to built notes, in place; returns them.

    `state` carries the chosen key between phrases (the pipeline passes the
    same dict every time). Notes must already carry `detected_midi` and
    `shift`, as `notes.build_notes` produces them.
    """
    if not notes:
        return notes
    state = state if state is not None else {}
    from .pitch import clamp_tone

    scale = cfg.scale if cfg.scale in SCALES else "major"
    exact = [
        (n.detected_midi if n.detected_midi is not None else float(n.tone - n.shift - cfg.transpose))
        + n.shift + cfg.transpose
        for n in notes
    ]

    fixed = key_from_config(cfg.scale_key)
    if fixed is not None:
        tonic = fixed
    elif len(notes) >= 3 or "tonic" not in state:
        tonic = choose_key(exact, scale, state.get("tonic"))
    else:
        tonic = state["tonic"]  # too few notes to judge; keep the key
    if tonic != state.get("tonic"):
        log.info("Singing in %s %s", NOTE_NAMES[tonic], scale)
    state["tonic"] = tonic

    amount = max(0.0, min(1.0, float(cfg.sung_contour_amount)))
    for note, pitch in zip(notes, exact):
        note.tone = clamp_tone(snap(pitch, tonic, scale), cfg)
        if note.contour:
            note.contour = [(x, round(y * amount, 1)) for x, y in note.contour]
            if max((abs(y) for _, y in note.contour), default=0.0) < 5.0:
                note.contour = []

    last = notes[-1]
    last.end += max(0.0, float(cfg.final_hold_seconds))

    shape_transitions(notes)

    for note in notes:
        if note.duration >= cfg.vibrato_min_seconds:
            note.vibrato = {
                # Percent of the note, from its end: the start stays steady.
                "length": 60.0,
                "period": float(cfg.vibrato_period_ms),
                "depth": float(cfg.vibrato_depth_cents),
                "in": 25.0,
                "out": 15.0,
            }
    return notes
