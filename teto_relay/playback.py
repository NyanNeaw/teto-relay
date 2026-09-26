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
import time
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

from .devices import sd

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
        info = sd().query_devices(device)
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

    def __init__(
        self,
        cfg,
        source: queue.Queue,
        device: int | None,
        on_playback: Callable[[object], None] | None = None,
    ):
        super().__init__(name="playback")
        self.cfg = cfg
        self.source = source
        self.device = device
        # Told the moment a job's audio starts, with its latency timeline
        # completed - the end of the measurement in teto_relay.latency.
        self.on_playback = on_playback
        self.target_rate, self.target_channels = _device_format(device)
        self._stopping = threading.Event()
        self._playing = threading.Event()
        # persistent_output: the open stream and the format it was opened for.
        self._stream = None
        self._stream_format: tuple[int, int] | None = None

    #: Frames written per call on the persistent stream; small enough that
    #: stop() is answered within a few tens of milliseconds.
    BLOCK = 1024

    def stop(self) -> None:
        self._stopping.set()
        if not getattr(self.cfg, "persistent_output", False):
            sd().stop()

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
            timeline = getattr(item, "timeline", None)
            if timeline is not None:
                began = time.monotonic()
                timeline.restart(getattr(item, "queued_at", 0.0) or began)
                timeline.lap("wait_output", began)
            try:
                if hasattr(item, "age"):
                    log.info(
                        'Playing "%s" - %.2fs behind the microphone',
                        item.text[:60],
                        item.age,
                    )
                self._play_file(wav_path, item if timeline is not None else None)
            except Exception:
                log.exception("failed to play %s", wav_path)
                self._close_stream()  # a broken stream is reopened next time
        self._close_stream()
        log.info("Playback thread stopped")

    def _report_start(self, job, data, sample_rate: int) -> None:
        """The audio is playing: close the job's latency timeline and report it."""
        if job is None:
            return
        from .latency import leading_silence

        job.timeline.lap("output")
        # Sound starts after any silence at the head of the file, so that is
        # latency too - it is how the render's leading silence was spotted.
        job.timeline.add("lead_silence", leading_silence(data, sample_rate))
        if self.on_playback is not None:
            try:
                self.on_playback(job)
            except Exception:  # noqa: BLE001 - reporting must not stop playback
                log.debug("playback callback failed", exc_info=True)

    def _play_file(self, path: Path, job=None) -> None:
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

        if getattr(self.cfg, "persistent_output", False):
            self._write_persistent(data, sample_rate, job)
            log.info("Played %s (%.2fs)", path.name, len(data) / sample_rate)
            return

        self._playing.set()
        try:
            sd().play(data, samplerate=sample_rate, device=self.device, blocking=False)
            self._report_start(job, data, sample_rate)
            # Poll rather than block so stop() stays responsive.
            while not self._stopping.is_set():
                if sd().get_stream() is None or not sd().get_stream().active:
                    break
                self._stopping.wait(0.05)
            if self._stopping.is_set():
                sd().stop()
        finally:
            self._playing.clear()
        log.info("Played %s (%.2fs)", path.name, len(data) / sample_rate)


    def _open_stream(self, sample_rate: int, channels: int):
        wanted = (sample_rate, channels)
        if self._stream is not None and self._stream_format == wanted:
            return self._stream
        self._close_stream()
        stream = sd().OutputStream(
            samplerate=sample_rate, channels=channels, dtype="float32", device=self.device
        )
        stream.start()
        self._stream, self._stream_format = stream, wanted
        log.info("Output stream open (%d Hz, %d ch) and kept open", sample_rate, channels)
        return stream

    def _close_stream(self) -> None:
        stream, self._stream, self._stream_format = self._stream, None, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001
            log.debug("closing the output stream failed", exc_info=True)

    def _write_persistent(self, data: np.ndarray, sample_rate: int, job) -> None:
        """Play through the long-lived stream, a block at a time."""
        self._playing.set()
        try:
            stream = self._open_stream(sample_rate, data.shape[1])
            data = np.ascontiguousarray(data, dtype=np.float32)
            for start in range(0, len(data), self.BLOCK):
                if self._stopping.is_set():
                    break
                stream.write(data[start : start + self.BLOCK])
                if start == 0:
                    self._report_start(job, data, sample_rate)
        finally:
            self._playing.clear()


class StreamOutput:
    """A kept-open output stream fed with blocks as they are made.

    Used by the streaming voice mode, where audio arrives a block at a time
    rather than as whole files. Blocks are resampled to the rate the device
    accepts (WASAPI shared mode refuses anything else) with a streaming
    resampler, so block edges do not click.
    """

    def __init__(self, device: int | None, gain: float = 1.0):
        self.device = device
        self.gain = gain
        self.rate, channels = _device_format(device)
        self.channels = min(channels or 1, 2)
        self._stream = None
        self._resampler = None
        self._resample_from: int | None = None

    def _resampled(self, block: np.ndarray, rate: int, target: int) -> np.ndarray:
        if rate == target:
            return block
        if self._resample_from != rate:
            self._resample_from = rate
            try:
                import soxr

                self._resampler = soxr.ResampleStream(rate, target, 1, dtype="float32")
            except (ImportError, AttributeError):
                self._resampler = None
        if self._resampler is not None:
            return self._resampler.resample_chunk(block)
        return _resample(block[:, None], rate, target)[:, 0]

    def write(self, block: np.ndarray, rate: int) -> None:
        target = self.rate or rate
        data = self._resampled(np.asarray(block, dtype=np.float32), rate, target)
        if self.gain != 1.0:
            data = np.clip(data * self.gain, -1.0, 1.0)
        if self._stream is None:
            self._stream = sd().OutputStream(
                samplerate=target, channels=self.channels, dtype="float32", device=self.device
            )
            self._stream.start()
        frames = np.repeat(data[:, None], self.channels, axis=1)
        self._stream.write(np.ascontiguousarray(frames))

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                log.debug("closing the stream output failed", exc_info=True)


def play_once(path: Path, device: int | None, gain: float = 1.0) -> None:
    """Blocking one-shot playback, for the command-line tools."""
    data, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    if gain != 1.0:
        data = np.clip(data * gain, -1.0, 1.0)
    target_rate, target_channels = _device_format(device)
    data, sample_rate = _fit(data, sample_rate, target_rate, target_channels)
    sd().play(data, samplerate=sample_rate, device=device, blocking=True)
