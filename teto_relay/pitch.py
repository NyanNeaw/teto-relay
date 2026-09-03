"""Stage 3 - fundamental frequency extraction.

This is the piece the reference implementation faked with
`random.randint(61, 64)`. We track real F0 with librosa's pyin, take a robust
per-word centre, and additionally sample the intra-word contour so the
synthesised note follows the speaker's prosody instead of sitting flat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

A4_HZ = 440.0
A4_MIDI = 69


def hz_to_midi(hz: float | np.ndarray) -> float | np.ndarray:
    return A4_MIDI + 12.0 * np.log2(np.asarray(hz, dtype=float) / A4_HZ)


def midi_to_hz(midi: float | np.ndarray) -> float | np.ndarray:
    return A4_HZ * np.power(2.0, (np.asarray(midi, dtype=float) - A4_MIDI) / 12.0)


def cents_between(hz: float, reference_midi: float) -> float:
    """Signed cents from a MIDI note to a frequency."""
    return float((hz_to_midi(hz) - reference_midi) * 100.0)


@dataclass
class F0Track:
    """Frame-wise F0 over one utterance. `f0` is NaN where unvoiced."""

    times: np.ndarray
    f0: np.ndarray
    voiced: np.ndarray
    sample_rate: int
    # Which tracker actually produced this. `track_f0` falls back to pyin when
    # crepe throws, so the configured method is not proof of what ran.
    method: str = ""

    @property
    def any_voiced(self) -> bool:
        return bool(np.any(self.voiced))

    def span(self, t0: float, t1: float) -> np.ndarray:
        """Voiced F0 values whose frame centres fall inside [t0, t1)."""
        if t1 <= t0:
            return np.empty(0)
        mask = (self.times >= t0) & (self.times < t1) & self.voiced
        return self.f0[mask]

    def median_midi(self, t0: float, t1: float) -> float | None:
        """Median MIDI over a span, or None when the span has no voiced frames.

        The median rather than the mean because pyin occasionally emits octave
        errors, and a single halved/doubled frame would drag a mean off by
        enough to change the note.
        """
        values = self.span(t0, t1)
        if values.size == 0:
            return None
        return float(hz_to_midi(np.median(values)))


def _track_pyin(audio: np.ndarray, sample_rate: int, cfg) -> F0Track:
    """librosa's pyin. No GPU needed, but slow and markedly variable: measured
    between 0.97s and 3.17s on similar-length speech."""
    import librosa  # imported lazily; it is slow to load

    # 10 ms hop gives enough resolution to see prosody inside a short word.
    hop_length = max(1, sample_rate // 100)
    frame_length = 1024
    if audio.size < frame_length:
        audio = np.pad(audio, (0, frame_length - audio.size))

    f0, voiced, _ = librosa.pyin(
        audio.astype(np.float32),
        fmin=cfg.f0_min,
        fmax=cfg.f0_max,
        sr=sample_rate,
        frame_length=frame_length,
        hop_length=hop_length,
        fill_na=np.nan,
    )
    times = librosa.times_like(f0, sr=sample_rate, hop_length=hop_length)
    voiced = np.asarray(voiced, dtype=bool) & np.isfinite(f0)
    return F0Track(
        times=times,
        f0=np.asarray(f0, dtype=float),
        voiced=voiced,
        sample_rate=sample_rate,
        method="pyin",
    )


_CREPE_WARNED = False


def _track_crepe(audio: np.ndarray, sample_rate: int, cfg) -> F0Track:
    """torchcrepe on the GPU. ~0.09s per utterance and consistent.

    crepe is trained at 16 kHz, which is what the capture stage produces, so no
    resampling is needed on the normal path.
    """
    global _CREPE_WARNED
    import torch
    import torchcrepe

    device = cfg.crepe_device
    if device.startswith("cuda") and not torch.cuda.is_available():
        if not _CREPE_WARNED:
            log.warning("CUDA not available; running crepe on the CPU")
            _CREPE_WARNED = True
        device = "cpu"

    hop_length = max(1, sample_rate // 100)  # 10 ms, matching the pyin path
    # crepe needs at least one full analysis window (1024 samples).
    if audio.size < 1024:
        audio = np.pad(audio, (0, 1024 - audio.size))

    tensor = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None].to(device)
    f0, periodicity = torchcrepe.predict(
        tensor,
        sample_rate,
        hop_length,
        fmin=cfg.f0_min,
        fmax=cfg.f0_max,
        model=cfg.crepe_model,
        batch_size=512,
        device=device,
        return_periodicity=True,
    )
    f0 = f0[0].detach().cpu().numpy().astype(float)
    voiced = periodicity[0].detach().cpu().numpy() > cfg.crepe_voiced_threshold
    voiced &= np.isfinite(f0)
    times = np.arange(len(f0)) * hop_length / sample_rate
    return F0Track(
        times=times,
        f0=f0,
        voiced=voiced,
        sample_rate=sample_rate,
        method=f"crepe/{cfg.crepe_model}@{device}",
    )


def track_f0(audio: np.ndarray, sample_rate: int, cfg) -> F0Track:
    """Extract the F0 track for one utterance using the configured method."""
    method = (cfg.pitch_method or "crepe").lower()
    if method == "pyin":
        return _track_pyin(audio, sample_rate, cfg)
    if method == "crepe":
        try:
            return _track_crepe(audio, sample_rate, cfg)
        except Exception:
            log.warning("crepe failed; falling back to pyin for this utterance", exc_info=True)
            track = _track_pyin(audio, sample_rate, cfg)
            # Name the fallback explicitly: an utterance tracked by pyin while
            # the config says crepe is exactly the case worth spotting in a log.
            track.method = "pyin (crepe fell back)"
            return track
    raise ValueError(f"unknown pitch_method {cfg.pitch_method!r} (expected 'crepe' or 'pyin')")


def compute_shift(
    midis: list[float],
    cfg,
    target: float,
    previous: int | None = None,
) -> int:
    """Semitones to move the voice onto the voicebank's recorded pitch.

    A UTAU sample is recorded at one pitch and resampled to whatever is asked
    for. The further from the original, the thinner the result: the English
    Teto bank sits at C#4, and rendering it at A3 sounds breathy while A4
    sounds strained. So we aim at `target` rather than merely landing inside a
    permitted range.

    Shifting every note by the same amount preserves all the intervals between
    them; only the key moves, which does not matter for speech. `shift_mode`
    can be set to "octave" to preserve pitch class instead, at the cost of
    landing further from the recorded pitch.
    """
    if not cfg.auto_octave or not midis:
        return 0

    centre = float(np.median(midis))

    if cfg.stable_shift and previous is not None:
        # Keep the previous shift while it still lands close to the target, so
        # the character's pitch does not lurch between phrases.
        if abs(centre + previous - target) <= cfg.shift_tolerance:
            return previous

    if (cfg.shift_mode or "semitone").lower() == "octave":
        shift = int(round((target - centre) / 12.0)) * 12
    else:
        shift = int(round(target - centre))

    return int(np.clip(shift, -cfg.max_shift, cfg.max_shift))


def octave_shift(midis: list[float], cfg, previous: int | None = None) -> int:
    """Whole octaves needed to land the utterance inside the bank's range.

    Teto sits well above a typical speaking voice. Shifting by whole octaves
    preserves pitch class and every relative interval, so the melody survives
    intact - unlike clamping, which would flatten the contour against the range
    limit.

    Two properties matter for intelligibility:

    * **Minimal, not centred.** Aiming at the middle of the range pushed a
      normal speaking voice up two full octaves when one was enough. The bigger
      the resample, the more artefact-ridden the voicebank sample sounds.
    * **Stable.** `previous` is reused whenever it still lands this utterance in
      range, so the character's pitch does not jump between phrases just
      because one came out slightly higher.
    """
    if not cfg.auto_octave or not midis:
        return 0

    centre = float(np.median(midis))

    def in_range(shift: int) -> bool:
        return cfg.midi_min <= centre + shift <= cfg.midi_max

    if cfg.stable_shift and previous is not None and in_range(previous):
        return previous
    if in_range(0):
        return 0

    # Smallest whole-octave move that brings the median into range.
    for octaves in range(1, 5):
        for shift in (octaves * 12, -octaves * 12):
            if in_range(shift):
                return shift
    # Nothing fits; fall back to the closest edge.
    target = cfg.midi_min if centre < cfg.midi_min else cfg.midi_max
    return int(round((target - centre) / 12.0)) * 12


def correct_octaves(midis: list[float], cfg, baseline: float | None = None) -> list[float]:
    """Pull octave-detection errors back in line with the rest of the phrase.

    pyin occasionally reports half (or double) the true frequency, which lands
    a word exactly ~12 semitones out. Live, this showed up as
    "hello@50 what is@51 up@61 my@61" - two words an octave below the rest -
    and because the stray values drag the median, the whole utterance's shift
    moved with them: +17 on one phrase, +6 on the next.

    Speech within one utterance rarely spans an octave, so a value a whole
    octave from the median is far more likely to be a detection error than a
    real leap.
    """
    if not cfg.fix_octave_errors or not midis:
        return list(midis)

    # Fewer than three words gives nothing within the utterance to be an
    # outlier *from*: with two, the median sits between them and a genuine wide
    # interval looks exactly like an octave error. A one-word utterance has
    # nothing at all - which is how a lone "hello" detected an octave low set a
    # +17 shift for the whole session. `baseline` (the speaker's running pitch)
    # gives those short utterances a reference.
    if len(midis) < 3:
        if baseline is None:
            return list(midis)
        corrected = []
        for value in midis:
            octaves = round((baseline - value) / 12.0)
            near_octave = abs(abs(baseline - value) - abs(octaves) * 12) <= cfg.octave_snap_cents / 100.0
            corrected.append(value + octaves * 12 if octaves and near_octave else value)
        changed = sum(1 for a, b in zip(midis, corrected) if a != b)
        if changed:
            log.info("Corrected %d octave error(s) against your usual pitch", changed)
        return corrected

    corrected = list(midis)
    for _ in range(2):  # a second pass settles values dragged by the first
        moved = False
        for i, value in enumerate(corrected):
            # Compare against the *other* words, so a stray value cannot vote
            # for itself by pulling the median towards it.
            others = corrected[:i] + corrected[i + 1 :]
            centre = float(np.median(others))
            octaves = round((centre - value) / 12.0)
            if octaves == 0:
                continue
            candidate = value + octaves * 12
            # Only correct if it really is close to a whole octave out.
            if abs(abs(centre - value) - abs(octaves) * 12) <= cfg.octave_snap_cents / 100.0:
                if abs(candidate - centre) < abs(value - centre):
                    corrected[i] = candidate
                    moved = True
        if not moved:
            break

    changed = sum(1 for a, b in zip(midis, corrected) if a != b)
    if changed:
        log.info("Corrected %d octave detection error(s)", changed)
    return corrected


def clamp_tone(midi: float, cfg) -> int:
    return int(np.clip(round(midi), cfg.midi_min, cfg.midi_max))


def _median_filter(values: np.ndarray, kernel: int) -> np.ndarray:
    """Median filter that kills pyin's occasional octave jumps."""
    if kernel < 3 or values.size < kernel:
        return values
    if kernel % 2 == 0:
        kernel += 1
    half = kernel // 2
    padded = np.pad(values, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, kernel)
    return np.median(windows, axis=-1)


def _moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if window < 2 or values.size < 2:
        return values
    window = min(window, values.size)
    kernel = np.ones(window) / window
    padded = np.pad(values, (window // 2, window - 1 - window // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def contour_points(
    track: F0Track, t0: float, t1: float, reference_midi: float, cfg
) -> list[tuple[float, float]]:
    """Sample the intra-note pitch deviation, smoothed.

    `reference_midi` must be the note's own **pre-shift** median, not its final
    tone. The curve is stored relative to the note, and the note has already
    been transposed onto the voicebank's pitch - so measuring the raw F0
    against the shifted tone yields the transposition distance (~1600 cents)
    rather than the inflection, and every point lands on the clamp. That bug
    turned the contour into a constant six-semitone drop below the note.

    Returns (x, y) pairs where x is milliseconds from note start and y is cents
    away from the note's tone. An empty list means "leave the note flat".

    Raw speech F0 carries two things at once: the intonation you actually hear,
    and frame-level jitter (plus pyin's occasional octave slip). Feeding both
    into a sung note made it warble and measurably roughened the tone; dropping
    the contour entirely fixed that but left every word monotone and robotic.

    So the contour is median-filtered (removing pitch spikes) and then
    averaged (removing jitter), keeping the shape of the inflection without the
    noise that rode on top of it.
    """
    if not cfg.emit_contour or t1 <= t0:
        return []

    mask = (track.times >= t0) & (track.times < t1) & track.voiced
    values = track.f0[mask]
    times = track.times[mask]
    if values.size < 2:
        return []

    frame_ms = 10.0  # track_f0 uses a 10 ms hop
    kernel = max(3, int(cfg.contour_smooth_ms / frame_ms))
    smoothed = _moving_average(_median_filter(values, kernel), kernel)

    cents = (hz_to_midi(smoothed) - reference_midi) * 100.0
    cents = np.clip(cents, -cfg.contour_range_cents, cfg.contour_range_cents)

    # Resample the smoothed curve to the requested number of points.
    n = max(2, cfg.contour_points)
    wanted = np.linspace(times[0], times[-1], n)
    sampled = np.interp(wanted, times, cents)

    points = [
        (round((t - t0) * 1000.0, 1), round(float(c), 1))
        for t, c in zip(wanted, sampled)
    ]

    # Flat enough not to matter? Leave the note alone.
    if max(abs(y) for _, y in points) - min(abs(y) for _, y in points) < 5.0:
        if max(abs(y) for _, y in points) < 15.0:
            return []
    return points


def assign_tones(spans: list[tuple[float, float]], track: F0Track, cfg) -> list[int | None]:
    """Median MIDI per span, before any octave shift. None where unvoiced."""
    return [track.median_midi(t0, t1) for t0, t1 in spans]


def fill_gaps(tones: list[float | None], cfg) -> list[float]:
    """Replace unvoiced spans by interpolating between their voiced neighbours.

    Unvoiced words are usually short function words or plosive-only fragments;
    carrying the surrounding pitch through them sounds far better than dropping
    to a fixed default.
    """
    known = [(i, t) for i, t in enumerate(tones) if t is not None]
    if not known:
        return [float(cfg.default_tone)] * len(tones)

    out: list[float] = []
    for i, t in enumerate(tones):
        if t is not None:
            out.append(float(t))
            continue
        before = [(j, v) for j, v in known if j < i]
        after = [(j, v) for j, v in known if j > i]
        if before and after:
            j0, v0 = before[-1]
            j1, v1 = after[0]
            ratio = (i - j0) / (j1 - j0)
            out.append(float(v0 + (v1 - v0) * ratio))
        elif before:
            out.append(float(before[-1][1]))
        else:
            out.append(float(after[0][1]))
    return out
