"""How she sings it: the expression a vocal-synth tuner draws by hand.

The note builder decides *what* is sung - which notes, when, at what pitch.
This module decides *how*, the way someone tuning a VOCALOID or UTAU part in
a song would, working from what you actually did:

Pitch (the sung style; speech keeps your own contour)
    * a **scoop** into the first note of every phrase - it is approached from
      a little below instead of starting dead on pitch (shakuri);
    * **portamento** between notes, longer for bigger intervals;
    * **overshoot** on leaps: past the new note, then settling;
    * a **fall** off the end of every phrase, not just the last one;
    * **vibrato** that waits, then grows, on notes long enough to carry it.

Dynamics and breath (both styles) - part-level curves for WORLDLINE-R
    * ``dyn``: each note as loud as you said it relative to the loudest word
      in its phrase, compressed the way a mix would (DYN_RATIO) - accents
      survive, nothing jumps out - with a soft attack at a phrase start and a
      fade at its end;
    * ``brec``: a breathier tail as each phrase ends.

Units were measured on WORLDLINE-R rather than assumed (see NOTES.md): dyn is
tenths of a dB and clips a little above +5 dB, so it only ever cuts; brec
+50 costs ~6 dB of harmonics-to-noise, which is audible without whispering.

All of this works on the Note list and plain numbers, so it is unit tested;
how it sounds needs ears.
"""

from __future__ import annotations

import numpy as np

# ------------------------------------------------------------------ pitch
SCOOP_CENTS = 70.0       # how far below the first note of a phrase it starts
SCOOP_MS = 90.0          # and how long it takes to arrive
PORTA_MIN_MS = 35.0      # glide from the previous note: a step...
PORTA_PER_SEMITONE = 9.0  # ...plus this per semitone of interval...
PORTA_MAX_MS = 95.0      # ...up to this
OVERSHOOT_MIN_INTERVAL = 2
OVERSHOOT_PER_SEMITONE = 7.0
OVERSHOOT_MAX_CENTS = 35.0
OVERSHOOT_SETTLE_MS = 110.0  # after landing
FALL_CENTS = 90.0        # the drop off the end of a phrase
FALL_MS = 130.0
VIBRATO_DELAY = 0.35     # fraction of the note held steady before vibrato
VIBRATO_MIN_DELAY_S = 0.18

# ------------------------------------------------------------- dynamics
DYN_RATIO = 0.5          # 1.0 keeps your loudness differences, 0 flattens them
DYN_FLOOR = -90.0        # tenths of a dB: never quieter than -9 dB
ATTACK_DYN = -60.0       # a phrase's first 60 ms rise from this
ATTACK_MS = 60.0
FADE_DYN = -120.0        # and its end fades to this
FADE_MS = 180.0
BREATH_TAIL = 45.0       # brec at the very end of a phrase
BREATH_MS = 250.0


def phrases(notes: list) -> list[list]:
    """Runs of notes sung without a break: a note that does not touch the one
    before it (`legato`) starts a new phrase."""
    out: list[list] = []
    for note in notes:
        if not out or not note.legato:
            out.append([note])
        else:
            out[-1].append(note)
    return out


def _add(points: list[tuple[float, float]], shape: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Add a shape (cents) on top of a curve (cents); x in ms from the note start."""
    base = sorted(points) if points else [(0.0, 0.0)]
    xs = sorted({x for x, _ in base} | {x for x, _ in shape})
    bx, by = [x for x, _ in base], [y for _, y in base]
    sx, sy = [x for x, _ in shape], [y for _, y in shape]
    return [(x, round(float(np.interp(x, bx, by)) + float(np.interp(x, sx, sy, left=0.0, right=0.0)), 1))
            for x in xs]


def shape_pitch(notes: list, cfg) -> list:
    """Scoops, portamento, overshoot, falls and vibrato, in place."""
    for phrase in phrases(notes):
        for i, note in enumerate(phrase):
            span = note.duration * 1000.0
            prev = phrase[i - 1] if i else None
            if prev is None:
                if span >= SCOOP_MS * 2:
                    note.contour = _add(note.contour, [(0.0, -SCOOP_CENTS), (SCOOP_MS, 0.0)])
            else:
                interval = note.tone - prev.tone
                glide = min(PORTA_MAX_MS, PORTA_MIN_MS + PORTA_PER_SEMITONE * abs(interval))
                # The glide straddles the boundary: it leaves the previous note
                # a little before this one starts and lands a little after.
                note.lead_in_ms = glide * 0.5
                land = glide * 0.5
                if abs(interval) >= OVERSHOOT_MIN_INTERVAL and span >= land + OVERSHOOT_SETTLE_MS * 1.3:
                    peak = min(OVERSHOOT_MAX_CENTS, OVERSHOOT_PER_SEMITONE * abs(interval))
                    sign = 1.0 if interval > 0 else -1.0
                    note.contour = _add(note.contour, [
                        (land, 0.0), (land + OVERSHOOT_SETTLE_MS * 0.4, sign * peak),
                        (land + OVERSHOOT_SETTLE_MS, 0.0)])
            if note is phrase[-1] and span >= FALL_MS * 2:
                note.contour = _add(note.contour, [(span - FALL_MS, 0.0), (span, -FALL_CENTS)])

            if note.duration >= cfg.vibrato_min_seconds:
                steady = max(VIBRATO_MIN_DELAY_S, VIBRATO_DELAY * note.duration)
                length = max(0.0, 100.0 * (1.0 - steady / note.duration))
                depth = float(cfg.vibrato_depth_cents) * (1.3 if note is phrase[-1] else 1.0)
                note.vibrato = {
                    # Percent of the note, from its end; `in` grows it gradually.
                    "length": round(length, 1),
                    "period": float(cfg.vibrato_period_ms),
                    "depth": round(depth, 1),
                    "in": 40.0,
                    "out": 20.0,
                }
    return notes


# ------------------------------------------------------------- dynamics
def _loudness(audio, rate: int):
    """Per-10 ms loudness in dB."""
    hop = rate // 100
    frames = len(audio) // hop
    if frames == 0:
        return np.zeros(0), np.zeros(0)
    chunk = np.asarray(audio[: frames * hop], dtype=np.float64).reshape(frames, hop)
    db = 20 * np.log10(np.sqrt(np.mean(chunk ** 2, axis=1)) + 1e-9)
    return np.arange(frames) / 100.0, db


def expression_curves(notes: list, audio, rate: int, cfg) -> dict[str, list[tuple[float, float]]]:
    """Part-level curves, as {abbr: [(seconds, value)]} in the utterance's time.

    Empty when `expressive` is off or there is no recording to follow.
    """
    if not getattr(cfg, "expressive", True) or not notes:
        return {}
    times, db = _loudness(audio, rate) if audio is not None else (np.zeros(0), np.zeros(0))

    def level(note) -> float | None:
        a, b = note.spoken or (note.start, note.end)
        mask = (times >= a) & (times < b)
        return float(np.percentile(db[mask], 75)) if mask.any() else None

    dyn: list[tuple[float, float]] = []
    brec: list[tuple[float, float]] = []
    for phrase in phrases(notes):
        levels = [level(n) for n in phrase]
        known = [v for v in levels if v is not None]
        loudest = max(known) if known else 0.0
        values = [
            max(DYN_FLOOR, min(0.0, (v - loudest) * DYN_RATIO * 10.0)) if v is not None else 0.0
            for v in levels
        ]
        first, last = phrase[0], phrase[-1]
        attack = min(ATTACK_MS / 1000.0, first.duration / 2)
        fade = min(FADE_MS / 1000.0, last.duration / 2)
        points = [(first.start, values[0] + ATTACK_DYN), (first.start + attack, values[0])]
        for note, value in zip(phrase, values):
            # Each note holds its level; the curve moves between neighbours
            # over their boundary instead of stepping.
            edge = min(0.03, note.duration / 4)
            opening, closing = note.start + edge, note.end - edge
            if note is first:
                opening = max(opening, first.start + attack)
            if note is last:
                closing = min(closing, last.end - fade)
            if closing > opening:
                points += [(opening, value), (closing, value)]
        points += [(last.end - fade, values[-1]), (last.end, max(2 * DYN_FLOOR, values[-1] + FADE_DYN))]
        dyn.extend(points)

        tail = min(BREATH_MS / 1000.0, last.duration * 0.6)
        brec.extend([(last.end - tail, 0.0), (last.end, BREATH_TAIL)])

    return {"dyn": _monotonic(dyn), "brec": _monotonic(brec)}


def _monotonic(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sorted by time, one value per instant (the later one wins)."""
    out: dict[float, float] = {}
    for x, y in sorted(points, key=lambda p: p[0]):
        out[round(x, 4)] = y
    return sorted(out.items())
