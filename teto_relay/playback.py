"""Stage 6 - play rendered audio into the virtual cable.

Output goes to VB-Cable rather than speakers for two reasons: Discord/OBS pick
it up as a microphone, and the real microphone never hears it, so the relay
cannot feed itself. That is what let the reference implementation's second PC
be dropped.
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

log = logging.getLogger(__name__)


def _device_format(device: int | None) -> tuple[int | None, int | None]:
    """The sample rate and channel count an output device will actually accept.

    WASAPI shared mode refuses anything but the rate the device is configured
    for, so playing a 44.1 kHz render into a 48 kHz CABLE Input fails outright
    with "Invalid sample rate [PaErrorCode -9997]". We resample to match rather
    than hoping the formats line up.
    """
    if device is None:
        return None, None
    try:
        info = sd.query_devices(device)
        return int(info["default_samplerate"]), int(info["max_output_channels"])
    except Exception:
        log.debug("could not query device %s", device, exc_info=True)
        return None, None


def _resample(data: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Rate-convert float32 frames, preferring soxr and falling back to scipy."""
    if from_rate == to_rate:
        return data
    try:
        import soxr

        return soxr.resample(data, from_rate, to_rate).astype(np.float32)
    except ImportError:
        pass
    from math import gcd

    from scipy.signal import resample_poly

    divisor = gcd(int(from_rate), int(to_rate))
    return resample_poly(data, to_rate // divisor, from_rate // divisor, axis=0).astype(np.float32)


def _fit(data: np.ndarray, rate: int, target_rate: int | None, target_channels: int | None):
    """Match the audio to what the device accepts."""
    if target_rate and rate != target_rate:
        data = _resample(data, rate, target_rate)
        rate = target_rate
    if target_channels and data.shape[1] != target_channels:
        if data.shape[1] == 1:
            data = np.tile(data, (1, min(target_channels, 2)))
        else:
            data = data[:, :target_channels]
    return data, rate


class Player(threading.Thread):
    """Serialises playback so overlapping utterances queue instead of colliding."""

    daemon = True

    def __init__(self, cfg, source: queue.Queue, device: int | None):
        super().__init__(name="playback")
        self.cfg = cfg
        self.source = source
        self.device = device
        self.target_rate, self.target_channels = _device_format(device)
        self._stopping = threading.Event()
        self._playing = threading.Event()

    def stop(self) -> None:
        self._stopping.set()
        sd.stop()

    @property
    def busy(self) -> bool:
        return self._playing.is_set()

    def run(self) -> None:
        log.info("Playback thread started (device index %s)", self.device)
        while not self._stopping.is_set():
            try:
                item = self.source.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break

            # The queue carries pipeline Jobs, but plain paths are accepted so
            # the command-line tools can drive the player directly.
            wav_path = Path(getattr(item, "wav_path", None) or item)
            try:
                if hasattr(item, "age"):
                    log.info(
                        'Playing "%s" - %.2fs behind the microphone',
                        item.text[:60],
                        item.age,
                    )
                self._play_file(wav_path)
            except Exception:
                log.exception("failed to play %s", wav_path)
        log.info("Playback thread stopped")

    def _play_file(self, path: Path) -> None:
        data, sample_rate = sf.read(path, dtype="float32", always_2d=True)
        if data.size == 0:
            log.warning("%s is empty, skipping", path.name)
            return

        if self.cfg.playback_gain != 1.0:
            data = np.clip(data * self.cfg.playback_gain, -1.0, 1.0)

        original_rate = sample_rate
        data, sample_rate = _fit(data, sample_rate, self.target_rate, self.target_channels)
        if sample_rate != original_rate:
            log.debug("resampled %d Hz -> %d Hz for the output device", original_rate, sample_rate)

        self._playing.set()
        try:
            sd.play(data, samplerate=sample_rate, device=self.device, blocking=False)
            # Poll rather than block so stop() stays responsive.
            while not self._stopping.is_set():
                if sd.get_stream() is None or not sd.get_stream().active:
                    break
                self._stopping.wait(0.05)
            if self._stopping.is_set():
                sd.stop()
        finally:
            self._playing.clear()
        log.info("Played %s (%.2fs)", path.name, len(data) / sample_rate)


def play_once(path: Path, device: int | None, gain: float = 1.0) -> None:
    """Blocking one-shot playback, for the command-line tools."""
    data, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    if gain != 1.0:
        data = np.clip(data * gain, -1.0, 1.0)
    target_rate, target_channels = _device_format(device)
    data, sample_rate = _fit(data, sample_rate, target_rate, target_channels)
    sd.play(data, samplerate=sample_rate, device=device, blocking=True)
