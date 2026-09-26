"""Stage 1 - microphone capture, split into utterances.

Two strategies, both pure state machines over fixed-size frames, so they can be
unit tested without audio hardware:

* `PushToTalkChunker` (default) records while a key is held. The speaker marks
  the boundaries, so every chunk is a whole phrase.
* `Chunker` splits automatically on silence. Convenient, but it cuts
  mid-sentence and feeds whisper short noisy fragments, which it transcribes as
  confident nonsense.

`MicCapture` is the thread that owns the sounddevice stream and feeds whichever
chunker it was given.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .devices import sd

log = logging.getLogger(__name__)


@dataclass
class Chunk:
    """One utterance worth of mono float32 audio."""

    audio: np.ndarray
    sample_rate: int
    reason: str  # "pause" or "max_length"
    # When the utterance closed, for end-to-end latency accounting.
    captured_at: float = field(default_factory=time.monotonic)

    @property
    def duration(self) -> float:
        return len(self.audio) / self.sample_rate


def rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))


class Chunker:
    """Accumulates frames and emits a Chunk once speech is followed by a pause."""

    def __init__(self, cfg, threshold: float | None = None):
        self.cfg = cfg
        self.threshold = cfg.rms_threshold if threshold is None else threshold
        preroll_frames = max(1, cfg.preroll_ms // cfg.frame_ms)
        self._preroll: deque[np.ndarray] = deque(maxlen=preroll_frames)
        self._frames: list[np.ndarray] = []
        self._speaking = False
        self._silence_run = 0  # consecutive quiet frames while speaking
        self._voiced = 0  # loud frames in the current utterance

    # frame-count thresholds derived from the millisecond config
    @property
    def _silence_limit(self) -> int:
        return max(1, self.cfg.silence_ms // self.cfg.frame_ms)

    @property
    def _max_frames(self) -> int:
        return max(1, self.cfg.max_chunk_ms // self.cfg.frame_ms)

    @property
    def _min_voiced(self) -> int:
        return max(1, self.cfg.min_chunk_ms // self.cfg.frame_ms)

    def push(self, frame: np.ndarray) -> Chunk | None:
        loud = rms(frame) >= self.threshold

        if not self._speaking:
            # Hold recent silence so the first phoneme is not clipped off.
            self._preroll.append(frame)
            if loud:
                self._speaking = True
                self._frames = list(self._preroll)
                self._preroll.clear()
                self._silence_run = 0
                self._voiced = 1
            return None

        self._frames.append(frame)
        if loud:
            self._voiced += 1
            self._silence_run = 0
        else:
            self._silence_run += 1

        if self._silence_run >= self._silence_limit:
            return self._emit("pause")
        if len(self._frames) >= self._max_frames:
            return self._emit("max_length")
        return None

    def flush(self) -> Chunk | None:
        """Emit whatever is buffered (used when the stream stops)."""
        return self._emit("pause") if self._speaking else None

    def _emit(self, reason: str) -> Chunk | None:
        frames, silence_run, voiced = self._frames, self._silence_run, self._voiced
        self._reset()

        # Trim most of the trailing silence but keep a short tail so final
        # consonants and the pyin analysis window survive.
        keep_tail = self._silence_limit // 2
        if silence_run > keep_tail:
            frames = frames[: len(frames) - (silence_run - keep_tail)]

        if voiced < self._min_voiced or not frames:
            log.debug("dropping chunk: %d voiced frames (need %d)", voiced, self._min_voiced)
            return None
        return Chunk(np.concatenate(frames), self.cfg.sample_rate, reason)

    def _reset(self) -> None:
        self._frames = []
        self._speaking = False
        self._silence_run = 0
        self._voiced = 0
        self._preroll.clear()


class PushToTalkChunker:
    """Records only while the talk key is held.

    The VAD chunker splits on pauses, which cuts mid-sentence and hands whisper
    short, noisy fragments - and whisper answers those with confident nonsense
    rather than silence. Here the speaker decides where an utterance starts and
    ends, so every chunk is a whole phrase.

    `start`/`stop` are called from the hotkey thread while `push` runs on the
    audio thread, hence the lock and the handoff through `_pending`. That is a
    queue: a single slot lost the first phrase when the key was released,
    pressed and released again before the next audio frame arrived.
    """

    def __init__(self, cfg, threshold: float | None = None):
        self.cfg = cfg
        self.threshold = threshold  # unused; kept so both chunkers share an interface
        preroll_frames = max(1, cfg.preroll_ms // cfg.frame_ms)
        self._preroll: deque[np.ndarray] = deque(maxlen=preroll_frames)
        self._frames: list[np.ndarray] = []
        self._active = False
        self._pending: deque[Chunk] = deque()
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._active

    def start(self) -> None:
        with self._lock:
            if self._active:
                return
            # Keep a little audio from before the key went down, so a word
            # started fractionally early is not clipped.
            self._frames = list(self._preroll)
            self._preroll.clear()
            self._active = True
        log.info("Recording...")

    def stop(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._active = False
            chunk = self._finish_locked("release")
        if chunk is None:
            log.info("Too short - ignored")

    def push(self, frame: np.ndarray) -> Chunk | None:
        """Add one frame; returns a finished phrase, one per call, when there is one."""
        with self._lock:
            if not self._active:
                self._preroll.append(frame)
            else:
                self._frames.append(frame)
                if len(self._frames) >= max(1, self.cfg.max_chunk_ms // self.cfg.frame_ms):
                    self._active = False
                    log.warning("Hit the %.0fs recording limit", self.cfg.max_chunk_ms / 1000)
                    self._finish_locked("max_length")
            # Delivered even while the key is held again, so a phrase finished
            # just before a new press is not held back until that one ends.
            return self._pending.popleft() if self._pending else None

    def flush(self) -> Chunk | None:
        """Finish any recording; returns one pending phrase. Call until None."""
        with self._lock:
            if self._active:
                self._active = False
                self._finish_locked("release")
            return self._pending.popleft() if self._pending else None

    def _finish_locked(self, reason: str) -> Chunk | None:
        frames, self._frames = self._frames, []
        min_frames = max(1, self.cfg.min_chunk_ms // self.cfg.frame_ms)
        if len(frames) < min_frames:
            return None
        chunk = Chunk(np.concatenate(frames), self.cfg.sample_rate, reason)
        self._pending.append(chunk)
        return chunk


def make_chunker(cfg, threshold: float | None = None):
    """Build the chunker named by `cfg.capture_mode`."""
    mode = (cfg.capture_mode or "ptt").lower()
    if mode == "ptt":
        return PushToTalkChunker(cfg, threshold)
    if mode == "vad":
        return Chunker(cfg, threshold)
    raise ValueError(f"unknown capture_mode {cfg.capture_mode!r} (expected 'ptt' or 'vad')")


def calibrate_threshold(cfg, device: int | None = None) -> float:
    """Measure the room's noise floor and derive an RMS gate from it."""
    frames = max(1, cfg.calibrate_ms // cfg.frame_ms)
    log.info("Calibrating noise floor for %d ms - stay quiet...", cfg.calibrate_ms)
    with sd().InputStream(
        samplerate=cfg.sample_rate,
        channels=1,
        dtype="float32",
        blocksize=cfg.frame_samples,
        device=device,
    ) as stream:
        levels = []
        for _ in range(frames):
            data, overflowed = stream.read(cfg.frame_samples)
            if not overflowed:
                levels.append(rms(data[:, 0]))
    if not levels:
        return cfg.rms_threshold
    floor = float(np.median(levels))
    threshold = max(floor * cfg.calibrate_margin, cfg.rms_threshold * 0.5)
    log.info("Noise floor %.5f -> RMS threshold %.5f", floor, threshold)
    return threshold


class MicCapture(threading.Thread):
    """Owns the input stream; pushes Chunks onto `sink` until stopped.

    If the microphone cannot be opened, or stops delivering audio (unplugged,
    taken by another app in exclusive mode, driver reset), it is reopened with
    a growing delay instead of the thread quietly exiting - which used to leave
    the relay "running" with nothing listening. `state` and `last_error` say
    what is going on, for the control panel and the log.
    """

    daemon = True
    #: No audio for this long means the stream has died, even if it did not say so.
    STALL_SECONDS = 2.0
    FIRST_RETRY_SECONDS = 1.0
    MAX_RETRY_SECONDS = 10.0

    def __init__(
        self,
        cfg,
        sink: queue.Queue,
        device: int | None = None,
        threshold: float | None = None,
        chunker=None,
        tap=None,
    ):
        super().__init__(name="mic-capture")
        # When set, every frame goes to tap(frame) instead of the chunker -
        # the streaming voice mode converts audio as it arrives.
        self.tap = tap
        self.cfg = cfg
        self.sink = sink
        self.device = device
        self.chunker = chunker if chunker is not None else make_chunker(cfg, threshold)
        self._stopping = threading.Event()
        self._paused = threading.Event()
        self.state = "starting"  # starting | listening | retrying | stopped
        self.last_error = ""
        # Counted in the audio callback, reported from this thread: logging
        # from the callback itself adds jitter to the audio thread.
        self._dropped = 0
        self._overflows = 0

    def stop(self) -> None:
        self._stopping.set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def run(self) -> None:
        delay = self.FIRST_RETRY_SECONDS
        try:
            while not self._stopping.is_set():
                try:
                    if self._listen():
                        delay = self.FIRST_RETRY_SECONDS  # it worked; start the backoff over
                except Exception as exc:  # noqa: BLE001 - reported and retried
                    message = f"{type(exc).__name__}: {exc}"
                    if message != self.last_error:
                        log.warning(
                            "The microphone could not be opened (%s). Retrying every "
                            "few seconds - check it is plugged in and not in use by "
                            "another app in exclusive mode.", message,
                        )
                        log.debug("microphone open failure", exc_info=True)
                    self.last_error = message
                if self._stopping.is_set():
                    break
                self.state = "retrying"
                self._stopping.wait(delay)
                delay = min(delay * 2, self.MAX_RETRY_SECONDS)
        finally:
            self.state = "stopped"
            while (tail := self.chunker.flush()) is not None:
                self._offer(tail)
            log.info("Microphone closed")

    def _listen(self) -> bool:
        """Run one input stream until it stalls or we stop.

        Returns True if audio actually flowed, so the caller can tell a flaky
        device (reset the backoff) from one that never opens (keep backing off).
        """
        raw: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)

        def callback(indata, frames, time_info, status):
            if status:
                self._overflows += 1
            try:
                raw.put_nowait(indata[:, 0].copy())
            except queue.Full:
                self._dropped += 1

        flowed = False
        with sd().InputStream(
            samplerate=self.cfg.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.cfg.frame_samples,
            device=self.device,
            callback=callback,
        ):
            if self.last_error:
                log.info("Microphone is back")
            log.info("Microphone open (%d Hz, %d ms frames)", self.cfg.sample_rate, self.cfg.frame_ms)
            self.state, self.last_error = "listening", ""
            last_frame = time.monotonic()
            while not self._stopping.is_set():
                self._report_drops()
                try:
                    frame = raw.get(timeout=0.2)
                except queue.Empty:
                    if time.monotonic() - last_frame > self.STALL_SECONDS:
                        self.last_error = "the microphone stopped sending audio"
                        log.warning("The microphone stopped sending audio; reopening it")
                        return flowed
                    continue
                last_frame = time.monotonic()
                flowed = True
                if self._paused.is_set():
                    continue
                if self.tap is not None:
                    self.tap(frame)
                    continue
                chunk = self.chunker.push(frame)
                if chunk is not None:
                    self._offer(chunk)
        return flowed

    def _report_drops(self) -> None:
        dropped, self._dropped = self._dropped, 0
        if dropped:
            log.warning("Capture fell behind - dropped %d audio frame(s)", dropped)
        overflows, self._overflows = self._overflows, 0
        if overflows:
            log.debug("input stream reported %d overflow(s)", overflows)

    def _offer(self, chunk: Chunk) -> None:
        """Enqueue, dropping the oldest item rather than blocking the mic."""
        try:
            self.sink.put_nowait(chunk)
        except queue.Full:
            try:
                dropped = self.sink.get_nowait()
                log.warning("pipeline backlog - dropped %.2fs utterance", dropped.duration)
                self.sink.put_nowait(chunk)
            except (queue.Empty, queue.Full):
                pass
        log.info("Utterance captured: %.2fs (%s)", chunk.duration, chunk.reason)
