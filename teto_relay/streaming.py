"""Real-time voice conversion: converting while you are still talking.

Everything else in Teto Relay waits for the end of a phrase before it starts,
so the first sound comes a couple of seconds after you finish. With
`mode: voice` and `voice_streaming: true`, the microphone is converted in short
blocks as it arrives instead (the approach real-time RVC voice changers use):

* every `stream_block_ms` of new audio is converted together with
  `stream_context_ms` of the audio before it, because the model needs context
  to sound right at the start of a block;
* only the part of the output that belongs to the new block is kept;
* consecutive blocks are joined with a short crossfade, after searching a few
  milliseconds for the offset where the two overlap best (SOLA), so the seam
  does not phase or click.

The delay is one block plus the crossfade plus however long the model takes
to convert a block - which must be less than a block, or it falls behind.

`BlockStreamer` is pure numpy and takes any `convert(audio, rate) -> (audio,
rate)` function, so it is tested with a stand-in converter. Running it with
RVC on a real GPU, microphone and VB-Cable is untested.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

Converter = Callable[[np.ndarray, int], "tuple[np.ndarray, int]"]


class BlockStreamer:
    """Turns a stream of input frames into a stream of converted blocks."""

    def __init__(
        self,
        convert: Converter,
        sample_rate: int,
        block_ms: float = 300.0,
        context_ms: float = 600.0,
        crossfade_ms: float = 50.0,
        search_ms: float = 10.0,
    ):
        self.convert = convert
        self.rate = int(sample_rate)
        self.block = max(1, int(self.rate * block_ms / 1000))
        self.context = max(0, int(self.rate * context_ms / 1000))
        # The crossfade overlaps the end of the previous block, which is only
        # available inside the context window, and so is the search around
        # it. With less context than that, samples were dropped at every seam.
        context_s = self.context / self.rate
        self.fade_s = min(crossfade_ms / 1000.0, context_s)
        self.search_s = max(0.0, min(search_ms / 1000.0, context_s - self.fade_s))
        self._pending = np.zeros(0, dtype=np.float32)  # input not yet converted
        self._history = np.zeros(self.context, dtype=np.float32)  # input already converted
        self._tail: np.ndarray | None = None  # output held back for the next crossfade
        self.out_rate: int | None = None
        # For the log: how long each conversion took, relative to the block.
        self.last_convert_seconds = 0.0

    @property
    def latency_seconds(self) -> float:
        """Input-to-output delay, not counting how long conversion takes."""
        return self.block / self.rate + self.fade_s

    def feed(self, frame: np.ndarray) -> list[np.ndarray]:
        """Add input; returns any output blocks now ready, in order."""
        self._pending = np.concatenate([self._pending, np.asarray(frame, dtype=np.float32).ravel()])
        out = []
        while len(self._pending) >= self.block:
            block, self._pending = self._pending[: self.block], self._pending[self.block :]
            out.append(self._process(block))
        return out

    def flush(self) -> list[np.ndarray]:
        """Convert what is left (padded with silence) and release the held tail."""
        out = []
        if len(self._pending):
            pad = np.zeros(self.block - len(self._pending), dtype=np.float32)
            out.append(self._process(np.concatenate([self._pending, pad])))
            self._pending = np.zeros(0, dtype=np.float32)
        if self._tail is not None and len(self._tail):
            out.append(self._tail)
        self._tail = None
        self._history = np.zeros(self.context, dtype=np.float32)
        return out

    def _process(self, block: np.ndarray) -> np.ndarray:
        window = np.concatenate([self._history, block]) if self.context else block
        began = time.monotonic()
        converted, out_rate = self.convert(window, self.rate)
        self.last_convert_seconds = time.monotonic() - began
        converted = np.asarray(converted, dtype=np.float32).ravel()
        self.out_rate = int(out_rate)
        if self.context:
            self._history = window[-self.context :]

        ratio = self.out_rate / self.rate
        block_out = int(round(self.block * ratio))
        fade = int(round(self.fade_s * self.out_rate))
        search = int(round(self.search_s * self.out_rate))
        if self.context:
            # Measured in output samples, which a converter's frame-based
            # length can make a sample shorter than the clamp in __init__
            # (done in input seconds) assumed.
            available = max(0, len(converted) - block_out)
            fade = min(fade, available)
            search = min(search, available - fade)
        # The new block's audio is the end of the output; take a little more
        # before it to overlap with the previous block's held-back tail.
        lead = min(len(converted) - block_out, fade + search) if len(converted) > block_out else 0
        piece = converted[len(converted) - block_out - lead :]

        if self._tail is None or fade == 0:
            emit = piece[lead:]
        else:
            # Crossfade the held tail into the new piece, starting where the
            # two line up best rather than blindly at the nominal position.
            offset = self._best_offset(self._tail, piece, lead, fade)
            head = piece[offset : offset + fade]
            n = min(len(head), len(self._tail))
            ramp = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32)
            joined = self._tail[:n] * (1.0 - ramp) + head[:n] * ramp
            emit = np.concatenate([joined, piece[offset + n :]])
        # Hold back the last `fade` samples: they are crossfaded with the next
        # block rather than played now.
        if fade and len(emit) > fade:
            self._tail = emit[-fade:].copy()
            emit = emit[:-fade]
        else:
            self._tail = None
        return emit

    @staticmethod
    def _best_offset(tail: np.ndarray, piece: np.ndarray, lead: int, fade: int) -> int:
        """Where in the new piece the held tail's continuation lines up best.

        The nominal position is `lead - fade` (the tail covers the `fade`
        samples just before the new block). Offsets within the search window
        either side are tried, scored by normalised correlation.
        """
        nominal = max(0, lead - fade)
        candidates = range(0, min(len(piece) - fade, 2 * nominal) + 1)
        best, best_score = nominal, -np.inf
        # Nearest to the nominal position first, so ties keep the timing.
        for offset in sorted(candidates, key=lambda o: abs(o - nominal)):
            window = piece[offset : offset + fade]
            if len(window) < len(tail):
                continue
            denom = float(np.linalg.norm(window) * np.linalg.norm(tail)) or 1.0
            score = float(np.dot(window, tail)) / denom
            if score > best_score + 1e-6:
                best, best_score = offset, score
        return best


class StreamingVoice(threading.Thread):
    """Mic frames in, converted audio out, continuously.

    `frames` is fed by the microphone thread; `output(block, rate)` receives
    each converted block (the relay writes it to a kept-open output stream).
    While `gate()` returns False - push-to-talk not held - input is ignored and
    whatever was in progress is finished off.
    """

    daemon = True

    def __init__(self, cfg, convert: Converter, output: Callable[[np.ndarray, int], None],
                 gate: Callable[[], bool] = lambda: True):
        super().__init__(name="voice-stream")
        self.frames: queue.Queue = queue.Queue(maxsize=500)
        self.streamer = BlockStreamer(
            convert, cfg.sample_rate, cfg.stream_block_ms, cfg.stream_context_ms,
            cfg.stream_crossfade_ms,
        )
        self.output = output
        self.gate = gate
        self._stopping = threading.Event()
        self._behind_warned = False

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        log.info(
            "Streaming voice conversion: %.0f ms blocks, about %.2f s behind plus conversion time",
            self.streamer.block / self.streamer.rate * 1000, self.streamer.latency_seconds,
        )
        active = False
        while not self._stopping.is_set():
            try:
                frame = self.frames.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self.gate():
                    active = True
                    blocks = self.streamer.feed(frame)
                elif active:
                    active = False
                    blocks = self.streamer.flush()
                else:
                    continue
                for block in blocks:
                    if len(block):
                        self.output(block, self.streamer.out_rate or self.streamer.rate)
                self._check_speed()
            except Exception:  # noqa: BLE001 - one bad block must not end the stream
                log.exception("streaming conversion failed for a block")

    def _check_speed(self) -> None:
        block_s = self.streamer.block / self.streamer.rate
        took = self.streamer.last_convert_seconds
        if took > block_s and not self._behind_warned:
            self._behind_warned = True
            log.warning(
                "Converting a %.0f ms block took %.0f ms, so the stream is falling behind. "
                "Raise stream_block_ms, lower stream_context_ms, or use a faster GPU.",
                block_s * 1000, took * 1000,
            )
