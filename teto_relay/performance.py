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
    * ``dyn``: your own loudness through each phrase, relative to its loudest
      part and compressed the way a mix would (DYN_RATIO) - your swells and
      fades, not a fixed shape;
    * ``brec``: a breathier tail as each phrase ends;
    * ``voic``: and its last moment half-voiced, the way a line lets go.

Units were measured on WORLDLINE-R rather than assumed (see NOTES.md): dyn is
tenths of a dB and clips a little above +5 dB, so it only ever cuts; brec
+50 costs ~6 dB of harmonics-to-noise, which is audible without whispering.

All of this works on the Note list and plain numbers, so it is unit tested;
how it sounds needs ears.
"""

from __future__ import annotations

import numpy as np

# ------------------------------------------------------------------ pitch
# The scoop into a phrase: held a little below, then a quick rise - the
# "__/----" entry tuners draw, rather than a slow slide.
SCOOP_CENTS = 150.0      # how far below the first note of a phrase it starts
SCOOP_HOLD_MS = 25.0     # held there this long
SCOOP_MS = 85.0          # and on pitch by this point
PORTA_MIN_MS = 30.0      # glide from the previous note: a step...
PORTA_PER_SEMITONE = 6.0  # ...plus this per semitone of interval...
PORTA_MAX_MS = 75.0      # ...up to this
# How much of the glide happens before the new note starts. Low, so each
# note holds its pitch until the last moment and then moves decisively -
# a pitch that sags early toward the next note sounds unsure.
PORTA_LEAD = 0.3
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
ENVELOPE_STEP = 0.04     # seconds between points of the loudness curve
ENVELOPE_WINDOW = 21     # 10 ms frames: the phrasing is read over ~200 ms
BREATH_TAIL = 30.0       # brec at the very end of a phrase
BREATH_MS = 250.0
# And the last moment of a phrase half-voiced, the way a sung line lets go
# (a devoiced ending, [a_0] in VOCALOID terms). voic 50 measured -6 dB and
# airier on WORLDLINE-R; 0 is a whisper.
DEVOICE_TO = 70.0
DEVOICE_MS = 90.0


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
                    note.contour = _add(note.contour, [
                        (0.0, -SCOOP_CENTS), (SCOOP_HOLD_MS, -SCOOP_CENTS), (SCOOP_MS, 0.0)])
            else:
                interval = note.tone - prev.tone
                glide = min(PORTA_MAX_MS, PORTA_MIN_MS + PORTA_PER_SEMITONE * abs(interval))
                # The glide straddles the boundary: it leaves the previous note
                # a little before this one starts and lands a little after.
                note.lead_in_ms = glide * PORTA_LEAD
                land = glide * (1.0 - PORTA_LEAD)
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

    ``dyn`` follows the speaker's own loudness through each phrase, every
    ENVELOPE_STEP, relative to the phrase's loudest part and compressed
    (DYN_RATIO) - their swells and fades, not a fixed shape. A fixed -12 dB
    fade at every phrase end made a sung line sound as if it faded out
    mid-song. ``brec`` and ``voic`` touch only the last moment of each phrase
    and are held neutral from its start: with points only at phrase ends
    they had ramped across the whole next phrase, which was sung half
    whispered and breathy. Empty when `expressive` is off.
    """
    if not getattr(cfg, "expressive", True) or not notes:
        return {}
    times, db = _loudness(audio, rate) if audio is not None else (np.zeros(0), np.zeros(0))
    if db.size >= ENVELOPE_WINDOW:
        # The phrasing, not the syllables: the upper envelope over 200 ms.
        # Following every 40 ms copied the dips of the speaker's consonants
        # onto hers - which land at other moments - and turned her
        # consonants down: English word error went from 0.09 to 0.18.
        half = ENVELOPE_WINDOW // 2
        padded = np.pad(db, half, mode="edge")
        windows = np.lib.stride_tricks.sliding_window_view(padded, ENVELOPE_WINDOW)
        db = np.percentile(windows, 80, axis=1)[: len(db)]

    dyn: list[tuple[float, float]] = []
    brec: list[tuple[float, float]] = []
    voic: list[tuple[float, float]] = []
    for phrase in phrases(notes):
        start, end = phrase[0].start, phrase[-1].end
        spoken = (times >= start) & (times < end)
        reference = float(np.percentile(db[spoken], 95)) if spoken.any() else None
        for x in list(np.arange(start, end, ENVELOPE_STEP)) + [end]:
            if reference is None:
                value = 0.0
            else:
                heard = float(np.interp(x, times, db))
                value = max(DYN_FLOOR, min(0.0, (heard - reference) * DYN_RATIO * 10.0))
            dyn.append((float(x), round(value, 1)))

        last = phrase[-1]
        tail = min(BREATH_MS / 1000.0, last.duration * 0.6)
        brec += [(start, 0.0), (end - tail, 0.0), (end, BREATH_TAIL)]
        devoice = min(DEVOICE_MS / 1000.0, last.duration * 0.3)
        voic += [(start, 100.0), (end - devoice, 100.0), (end, DEVOICE_TO)]

    return {"dyn": _monotonic(dyn), "brec": _monotonic(brec), "voic": _monotonic(voic)}


def _monotonic(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sorted by time, one value per instant (the later one wins)."""
    out: dict[float, float] = {}
    for x, y in sorted(points, key=lambda p: p[0]):
        out[round(x, 4)] = y
    return sorted(out.items())


# ------------------------------------------------------------- doubling
#: The copies laid under the lead: (cents detuned, ms late). Slightly sharp
#: and slightly flat, a little behind - two takes that are nearly the same.
DOUBLES = ((7.0, 21.0), (-9.0, 32.0))


def double_voice(audio, rate: int, amount: float):
    """Lay quiet, slightly detuned and delayed copies under the voice.

    Tuners double a lead with a second take for a fuller sound. Rendering her
    twice would double the render time - the slowest stage there is - so
    this is an effect on the finished audio instead. `amount` 0 is off; 1 lays
    each copy at half the lead's level.
    """
    audio = np.asarray(audio, dtype=np.float32)
    if amount <= 0 or audio.size == 0:
        return audio
    n = audio.size
    out = audio.astype(np.float64).copy()
    for cents, delay_ms in DOUBLES:
        ratio = 2.0 ** (cents / 1200.0)
        # Reading the audio slightly faster raises its pitch by `cents`.
        shifted = np.interp(np.arange(n) * ratio, np.arange(n), audio, right=0.0)
        delay = int(rate * delay_ms / 1000.0)
        copy = np.zeros(n)
        copy[delay:] = shifted[: n - delay]
        out += copy * (0.5 * amount)
    peak = float(np.max(np.abs(out)))
    if peak > 1.0:
        out /= peak
    return out.astype(np.float32)
