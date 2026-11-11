"""The round-trip delay of singing along, and measuring it.

A take's lines are placed by when the microphone heard them, against a
clock that starts when the music's first sample is handed to the headphones
(teto_relay.music.Backing). The 2 seconds she takes to sing a line are no part
of that. What is left is the round trip of the sound itself: the music
reaches your ears a little after the driver says it does (a lot after, on
Bluetooth), and your voice reaches the program a little after you sing. So a
line sung on the beat is stamped that much late.

`studio_offset_ms` is that delay, taken off every new line's time. This module
measures it: a short run of clicks plays on the Hear myself device, the
microphone records it, and the two are lined up by cross-correlation
(`estimate_delay`). The microphone has to hear the headphones for that, so
one side is held against it for the few seconds it takes.

The playing and the recording are stamped the way a take stamps them (the
music by `first_sample_time`, the microphone by when its audio arrives), so
what is measured is what a take is off by.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from .errors import TetoRelayError

log = logging.getLogger(__name__)

#: The rate the clicks are made and compared at.
RATE = 16000
#: When each click starts, s. Uneven on purpose: with even spacing a delay of
#: one gap would line up as well as the right one.
CLICK_TIMES = (0.30, 0.62, 0.89, 1.27, 1.52, 1.93, 2.21, 2.66)
CLICK_SECONDS = 0.03
#: Silence after the last click, so a long delay still fits in the recording.
TAIL_SECONDS = 0.5
#: The delays looked for. A little before zero, because the driver's own idea
#: of its latency can overshoot; Bluetooth runs to 0.3 s and more.
SEARCH = (-0.1, 1.5)
#: How far the best alignment must stand above the next best to be believed.
HEARD = 1.5
#: How far the setting may be from zero, ms (as config.RANGES has it).
LIMIT_MS = 2000.0


class DelayError(TetoRelayError):
    """The delay could not be measured. The message says what to do."""


def click_track(rate: int = RATE) -> np.ndarray:
    """The clicks: short rising chirps (800 to 3200 Hz, where even a headset
    microphone hears well), CLICK_TIMES apart, as float32 mono."""
    n = int(round(CLICK_SECONDS * rate))
    t = np.arange(n) / rate
    sweep = 2 * np.pi * (800.0 * t + (3200.0 - 800.0) / (2 * CLICK_SECONDS) * t * t)
    click = (0.6 * np.sin(sweep) * np.hanning(n)).astype(np.float32)
    out = np.zeros(int(round((CLICK_TIMES[-1] + CLICK_SECONDS + TAIL_SECONDS) * rate)), dtype=np.float32)
    for at in CLICK_TIMES:
        start = int(round(at * rate))
        out[start:start + n] = click
    return out


def estimate_delay(played: np.ndarray, recorded: np.ndarray, rate: int,
                   search: tuple[float, float] = SEARCH) -> tuple[float, float]:
    """How much later `played` is heard in `recorded`, when sample 0 of each
    is the same moment: (seconds, strength).

    The two are cross-correlated and the envelope of that is searched over
    `search` for its highest point, so the answer does not depend on the
    microphone's polarity or on how loud the clicks came through. `strength`
    is that point against the highest one elsewhere (more than 30 ms away):
    about 1 when the clicks are not in the recording at all, several when
    they are. Below HEARD it should not be trusted.
    """
    played = np.asarray(played, dtype=np.float64).reshape(-1)
    recorded = np.asarray(recorded, dtype=np.float64).reshape(-1)
    if not len(played) or not len(recorded):
        return 0.0, 0.0
    played = played - played.mean()
    recorded = recorded - recorded.mean()
    first, last = int(round(search[0] * rate)), int(round(search[1] * rate))
    # corr[k] = sum(recorded[n + k] * played[n]), for k from -pad up.
    pad = max(0, -first)
    size = 1
    while size < len(recorded) + len(played) + pad:
        size *= 2
    spectrum = np.fft.rfft(np.concatenate([np.zeros(pad), recorded]), size) * np.conj(np.fft.rfft(played, size))
    # The envelope (the analytic signal's size): one smooth hill per
    # alignment instead of the ripple of the chirp's own cycles.
    full = np.zeros(size, dtype=np.complex128)
    full[:len(spectrum)] = spectrum
    full[1:len(spectrum) - 1] *= 2
    envelope = np.abs(np.fft.ifft(full))[:last + pad + 1]
    if not len(envelope) or not np.isfinite(envelope).all():
        return 0.0, 0.0
    best = int(np.argmax(envelope))
    peak = float(envelope[best])
    guard = int(round(0.03 * rate))
    rest = np.concatenate([envelope[:max(0, best - guard)], envelope[best + guard:]])
    other = float(rest.max()) if len(rest) else 0.0
    if peak <= 0:
        return 0.0, 0.0
    return (best - pad) / rate, (peak / other if other > 0 else float("inf"))


def recording_start(stamps: list[float], frames: list[int], rate: int) -> float:
    """When (time.monotonic) the recording's first sample was heard, from
    when each block of it arrived (`stamps`) and how many frames each held.
    A block arrives as its last sample is heard, so each one gives an answer;
    the middle one is taken, as blocks arrive in fits and starts."""
    counted = np.cumsum(np.asarray(frames, dtype=np.float64))
    return float(np.median(np.asarray(stamps, dtype=np.float64) - counted / rate))


def solve(recorded: np.ndarray, rate: int, started: float, heard_at: float) -> tuple[float, float]:
    """The delay, from a recording whose first sample was heard at `started`
    and clicks whose first sample was handed to the headphones at `heard_at`
    (both time.monotonic): (milliseconds, strength)."""
    from .playback import _resample

    recorded = np.asarray(recorded, dtype=np.float32).reshape(-1)
    if rate != RATE:
        recorded = _resample(recorded[:, None], int(rate), RATE)[:, 0]
    # Line the two up on the clock: drop (or pad) the recording's head so
    # that its sample 0 is the moment the clicks began.
    lead = int(round((heard_at - started) * RATE))
    recorded = recorded[lead:] if lead >= 0 else np.concatenate([np.zeros(-lead, np.float32), recorded])
    seconds, strength = estimate_delay(click_track(), recorded, RATE)
    return seconds * 1000.0, strength


def measure(cfg) -> dict:
    """Play the clicks on the Hear myself device, record the microphone, and
    work out the delay: {"ms", "strength", "output", "input"}.

    Raises DelayError (or another TetoRelayError) with what to do when it
    cannot be measured: no device, the cable, or clicks the microphone never
    heard. Takes about five seconds.
    """
    from . import music
    from .capture import input_rate
    from .devices import default_input, resolve_input, sd
    from .playback import _device_format, _fit, monitor_device

    out_device = monitor_device(cfg)
    if out_device is None:
        raise DelayError("There is nothing to play the clicks on. Plug in your headphones.")
    try:
        out_name = str(sd().query_devices(out_device)["name"])
    except Exception:  # noqa: BLE001
        out_name = ""
    music.refuse_cable(out_name)
    mic = resolve_input(cfg) or default_input()
    mic_index = mic.index if mic else None
    rate = input_rate(cfg, mic_index)
    # The same blocks a take is recorded in.
    block = max(1, int(round(cfg.frame_samples * rate / cfg.sample_rate)))
    clicks = click_track()
    audio, out_rate = _fit(clicks[:, None], RATE, *_device_format(out_device))

    blocks: list[np.ndarray] = []
    stamps: list[float] = []

    def on_audio(indata, frames, time_info, status) -> None:
        stamps.append(time.monotonic())
        blocks.append(indata[:, 0].copy())

    backing = music.Backing(audio, out_rate, out_device)
    try:
        with sd().InputStream(samplerate=rate, channels=1, dtype="float32", blocksize=block,
                              device=mic_index, callback=on_audio):
            time.sleep(0.25)
            heard_at = backing.start()
            time.sleep(len(clicks) / RATE + SEARCH[1] - TAIL_SECONDS + 0.3)
    except TetoRelayError:
        raise
    except Exception as exc:  # noqa: BLE001 - a device that will not open
        raise DelayError(f"The microphone or the headphones could not be opened ({exc}).") from exc
    finally:
        backing.stop()
    if not blocks:
        raise DelayError("The microphone sent no sound. Check it under Microphone.")
    started = recording_start(stamps, [len(b) for b in blocks], rate)
    ms, strength = solve(np.concatenate(blocks), rate, started, heard_at)
    log.info("Delay measured: %.0f ms (strength %.1f) from %s to %s", ms, strength,
             out_name or f"device {out_device}", mic.name if mic else "the default microphone")
    if strength < HEARD:
        raise DelayError(
            "The microphone did not hear the clicks. Hold one side of your headphones against the "
            "microphone, turn the volume up, and try again."
        )
    return {"ms": round(float(ms)), "strength": round(float(min(strength, 99.0)), 1),
            "output": out_name, "input": mic.name if mic else ""}
