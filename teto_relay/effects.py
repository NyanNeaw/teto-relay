"""Effects on her rendered voice: the tones a tuning needs that the renderer
cannot make on its own.

A UTAU voicebank sung by WORLDLINE-R is a clean voice. A metal scream, a
robot, a telephone, a choir in a hall are made by producers after the render,
with distortion, modulation, filters, chorus and reverb. Each effect here is
one pure function on mono float32 audio, off at 0 (it then returns its input
untouched), and cheap: the whole chain on a 3 s phrase is a few tens of
milliseconds on a CPU, so it never holds up the relay.

`apply` runs them in a fixed order (`CHAIN`), lays the doubled voice in where
it belongs, then matches the loudness of what went in and soft-limits, so a
tuning with heavy drive is not twice as loud as one without. Only reverb makes
the audio longer: its tail, at most `TAIL_SECONDS`.
"""

from __future__ import annotations

import functools

import numpy as np

#: The effect settings, in the order they are applied. All default to 0.
CHAIN = ("fx_octave", "fx_bright", "fx_drive", "fx_growl", "fx_ring", "fx_crush",
         "fx_band", "fx_air", "fx_chorus", "fx_reverb")
#: The most reverb may add to the end of a phrase.
TAIL_SECONDS = 0.8
LIMIT = 0.98


def _sos(kind: str, hz, rate: int, order: int = 2):
    from scipy.signal import butter

    nyq = rate / 2.0
    if isinstance(hz, (tuple, list)):
        hz = [min(max(h, 20.0), nyq * 0.95) for h in hz]
    else:
        hz = min(max(float(hz), 20.0), nyq * 0.95)
    return butter(order, hz, btype=kind, fs=rate, output="sos")


def _filter(x, kind: str, hz, rate: int, order: int = 2):
    from scipy.signal import sosfilt

    return sosfilt(_sos(kind, hz, rate, order), x)


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0


def _wet(dry, wet, amount: float):
    return dry * (1.0 - amount) + wet * amount


# ----------------------------------------------------------------- effects
def octave(x, rate: int, value: float):
    """A layer an octave up (value > 0) or down (< 0), mixed in at |value|:
    two crossfaded taps reading the voice through a 40 ms sliding delay."""
    if not value:
        return x
    ratio = 2.0 if value > 0 else 0.5
    grain = int(rate * 0.04)
    n = np.arange(x.size, dtype=np.float64)
    layer = np.zeros(x.size)
    for k in (0, 1):
        d = np.mod(n * (1.0 - ratio) + k * grain / 2.0, grain)
        layer += np.sin(np.pi * d / grain) ** 2 * np.interp(n - d, n, x, left=0.0)
    return x + abs(value) * layer


def bright(x, rate: int, value: float):
    """Tilt: brighter (> 0, up to +9 dB above 3 kHz) or darker (< 0, a
    lowpass falling from 8 kHz to 2.5 kHz)."""
    if not value:
        return x
    if value > 0:
        return x + (10 ** (9.0 * value / 20.0) - 1.0) * _filter(x, "highpass", 3000.0, rate)
    cutoff = 8000.0 * (2500.0 / 8000.0) ** (-value)
    return _wet(x, _filter(x, "lowpass", cutoff, rate, 1), min(1.0, -value * 1.5))


def drive(x, rate: int, amount: float):
    """Distortion: an asymmetric tanh (even harmonics, a growlier edge) on
    the voice without its lowest lows, its fizz taken off above 7 kHz."""
    if amount <= 0:
        return x
    body = _filter(x, "highpass", 150.0, rate)
    peak = float(np.max(np.abs(body))) or 1.0
    bias = 0.15 * amount
    hot = np.tanh(body / peak * (1.0 + 30.0 * amount) + bias) - np.tanh(bias)
    hot = _filter(hot, "lowpass", 7000.0, rate) * peak
    return _wet(x, hot, np.sqrt(amount))


def growl(x, rate: int, amount: float, hz: float = 70.0):
    """A rough, fried edge: the voice chopped `hz` times a second with a
    little jitter - vocal fry, false-cord rattle. 75 Hz screams, 45 Hz is a
    demon."""
    if amount <= 0:
        return x
    rng = np.random.default_rng(7)
    jitter = np.repeat(rng.uniform(-0.08, 0.08, x.size // max(1, rate // 20) + 1), max(1, rate // 20))[: x.size]
    jitter = np.convolve(jitter, np.ones(64) / 64, mode="same")
    phase = np.cumsum(np.pi * float(hz) * (1.0 + jitter) / rate)
    return x * (1.0 - amount + amount * np.abs(np.sin(phase)) ** 0.5)


def ring(x, rate: int, amount: float):
    """Metallic: ring modulation at 90 Hz and a 6 ms comb. A robot."""
    if amount <= 0:
        return x
    t = np.arange(x.size) / rate
    y = _wet(x, x * np.sin(2 * np.pi * 90.0 * t), amount)
    delay = int(rate * 0.006)
    comb = y.copy()
    comb[delay:] += 0.5 * amount * y[:-delay]
    return comb


def crush(x, rate: int, amount: float):
    """Fewer samples and fewer bits: a cheap old game console."""
    if amount <= 0:
        return x
    hold = 1 + int(round(9 * amount))
    y = x[(np.arange(x.size) // hold) * hold]
    steps = 2 ** (12 - 8 * amount - 1)
    return np.round(y * steps) / steps


def band(x, rate: int, amount: float):
    """A telephone or an old radio: 300-3400 Hz and a little hiss."""
    if amount <= 0:
        return x
    narrow = _filter(x, "bandpass", (300.0, 3400.0), rate)
    hiss = np.random.default_rng(3).standard_normal(x.size) * (10 ** (-50 / 20.0))
    return _wet(x, narrow * 1.6, amount) + hiss * amount


def _envelope(x, rate: int, seconds: float = 0.02):
    width = max(1, int(rate * seconds))
    power = np.convolve(np.square(x), np.ones(width) / width, mode="same")
    return np.sqrt(np.maximum(power, 0.0))


def air(x, rate: int, amount: float):
    """Breath over the voice: noise above 5 kHz that follows her loudness."""
    if amount <= 0:
        return x
    noise = _filter(np.random.default_rng(5).standard_normal(x.size), "highpass", 5000.0, rate)
    noise /= (_rms(noise) or 1.0)
    return x + 0.35 * amount * noise * _envelope(x, rate)


def chorus(x, rate: int, amount: float):
    """Three slightly moving copies: one voice becomes a few."""
    if amount <= 0:
        return x
    n = np.arange(x.size, dtype=np.float64)
    t = n / rate
    out = x.copy()
    for delay_ms, hz in ((12.0, 0.6), (19.0, 0.9), (27.0, 1.3)):
        d = rate * (delay_ms + 3.0 * np.sin(2 * np.pi * hz * t)) / 1000.0
        out += 0.4 * amount * np.interp(n - d, n, x, left=0.0)
    return out


@functools.lru_cache(maxsize=8)
def _impulse(rate: int, size: float):
    """A room: decaying noise, 15 ms before it starts, its rumble removed."""
    tau = 0.15 + 0.9 * size
    length = int(rate * min(TAIL_SECONDS + 0.4, 4.0 * tau))
    t = np.arange(length) / rate
    ir = np.random.default_rng(11).standard_normal(length) * np.exp(-t / tau)
    ir = np.concatenate([np.zeros(int(rate * 0.015)), _filter(ir, "highpass", 250.0, rate)])
    return ir / (np.sqrt(np.sum(ir ** 2)) or 1.0)


def reverb(x, rate: int, amount: float, size: float = 0.3):
    """A room around her, from a booth (size 0) to a hall (1). Adds a tail
    of up to TAIL_SECONDS."""
    if amount <= 0:
        return x
    from scipy.signal import fftconvolve

    wet = fftconvolve(x, _impulse(int(rate), round(float(size), 2)))
    tail = min(int(rate * TAIL_SECONDS), wet.size - x.size)
    out = np.concatenate([x, np.zeros(tail)])
    wet = wet[: out.size]
    # The tail fades out instead of stopping.
    if tail > 0:
        wet[-tail:] *= np.linspace(1.0, 0.0, tail) ** 2
    return out + 0.6 * amount * wet


# ------------------------------------------------------------------ chain
def active(cfg) -> bool:
    """Whether any effect is on."""
    return any(float(getattr(cfg, key, 0.0) or 0.0) != 0.0 for key in CHAIN)


def apply(audio, rate: int, cfg, double: float = 0.0):
    """Her rendered voice through every effect that is on, in CHAIN order,
    with the doubled voice (`double`, from performance.double_voice) laid in
    before the reverb. The result is as loud as what went in and never
    clips. A 2-D (frames x channels) input is done channel by channel."""
    from .performance import double_voice

    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        chans = [apply(audio[:, c], rate, cfg, double) for c in range(audio.shape[1])]
        width = max(c.size for c in chans)
        return np.stack([np.pad(c, (0, width - c.size)) for c in chans], axis=1)
    if audio.size == 0 or (not active(cfg) and double <= 0):
        return audio
    get = lambda key, default=0.0: float(getattr(cfg, key, default) or 0.0)  # noqa: E731
    x = audio.astype(np.float64)
    before = _rms(x)
    x = octave(x, rate, get("fx_octave"))
    x = bright(x, rate, get("fx_bright"))
    x = drive(x, rate, get("fx_drive"))
    x = growl(x, rate, get("fx_growl"), get("fx_growl_hz", 70.0) or 70.0)
    x = ring(x, rate, get("fx_ring"))
    x = crush(x, rate, get("fx_crush"))
    x = band(x, rate, get("fx_band"))
    x = air(x, rate, get("fx_air"))
    x = chorus(x, rate, get("fx_chorus"))
    if double > 0:
        x = double_voice(x.astype(np.float32), rate, double).astype(np.float64)
    x = reverb(x, rate, get("fx_reverb"), get("fx_size", 0.3))
    # As loud as it came in, then a soft ceiling: drive and reverb would
    # otherwise jump several dB from one tuning to the next.
    after = _rms(x)
    if after > 0 and before > 0:
        x *= min(4.0, max(0.25, before / after))
    if float(np.max(np.abs(x))) > LIMIT:
        x = LIMIT * np.tanh(x / LIMIT)
    return np.nan_to_num(x).astype(np.float32)
