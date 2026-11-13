"""A slow, low-memory PC: a busy moment must not look like a dead microphone,
and the aligner is not loaded ahead of need when memory is short.

On a 6 GB laptop the 1.6 GB aligner was loaded in the background just as the
microphone opened. The PC swapped, the audio callback could not run, the
capture thread took that for a dead microphone and kept reopening it, and the
app fell over ("reopening the microphone", then gone).
"""

import queue
import threading
import time
import types
import unittest
import unittest.mock

import numpy as np


class _Clock:
    """time.monotonic, plus jumps: a jump is time the PC spent elsewhere."""

    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        return time.monotonic() + self.offset


def _capture(stream_factory, clock=None, stall=0.6):
    from teto_relay import capture
    from teto_relay.config import Config

    pushed = []

    class Recorder:
        def push(self, frame):
            pushed.append(frame)

        def flush(self):
            return None

    mic = capture.MicCapture(Config(capture_mode="vad"), queue.Queue(), chunker=Recorder())
    mic.STALL_SECONDS, mic.FIRST_RETRY_SECONDS, mic.MAX_RETRY_SECONDS = stall, 0.05, 0.1
    fake_sd = types.SimpleNamespace(InputStream=stream_factory)
    patches = [unittest.mock.patch.object(capture, "sd", lambda: fake_sd),
               unittest.mock.patch.object(capture, "input_rate", lambda cfg, device=None: cfg.sample_rate)]
    if clock is not None:
        patches.append(unittest.mock.patch.object(
            capture, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=time.sleep)))
    return mic, pushed, patches


class _Stream:
    """Feeds `frames` blocks, calling `at_frame(i)` before each, then goes quiet."""

    def __init__(self, frames, at_frame=None, **kwargs):
        self.frames, self.at_frame = frames, at_frame
        self.callback, self.blocksize = kwargs["callback"], kwargs["blocksize"]
        self.aborted = False

    def __enter__(self):
        def feed():
            for i in range(self.frames):
                if self.at_frame is not None:
                    self.at_frame(i)
                self.callback(np.full((self.blocksize, 1), 0.1, dtype=np.float32), self.blocksize, None, None)
                time.sleep(0.005)

        threading.Thread(target=feed, daemon=True).start()
        return self

    def abort(self):
        self.aborted = True

    def __exit__(self, *exc):
        return False


class TestBusyMoment(unittest.TestCase):
    def run_until(self, mic, patches, until, seconds=8):
        for p in patches:
            p.start()
        try:
            mic.start()
            deadline = time.monotonic() + seconds
            while not until() and time.monotonic() < deadline:
                time.sleep(0.02)
            mic.stop()
            mic.join(timeout=5)
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertFalse(mic.is_alive())

    def test_a_busy_pc_does_not_reopen_the_microphone(self):
        clock = _Clock()
        opened = []

        def at_frame(i):
            if i == 40:
                # Five seconds elsewhere (swapping, a model loading), then the
                # audio carries on as if nothing happened.
                clock.offset += 5.0
                time.sleep(0.4)

        def factory(**kw):
            stream = _Stream(400, at_frame, **kw)
            opened.append(stream)
            return stream

        mic, pushed, patches = _capture(factory, clock)
        self.run_until(mic, patches, lambda: len(pushed) >= 300)
        self.assertGreaterEqual(len(pushed), 300)
        self.assertEqual(len(opened), 1, "a busy moment was taken for a dead microphone")

    def test_a_really_quiet_microphone_is_aborted_then_reopened(self):
        opened = []

        def factory(**kw):
            stream = _Stream(20, **kw)
            opened.append(stream)
            return stream

        mic, _, patches = _capture(factory, stall=0.3)
        self.run_until(mic, patches, lambda: len(opened) >= 2)
        self.assertGreaterEqual(len(opened), 2)
        self.assertTrue(opened[0].aborted, "a stalled stream must be aborted, not stopped")


class TestAlignerPreload(unittest.TestCase):
    def reason(self, checkpoint, free):
        from teto_relay import align
        from teto_relay.config import Config

        with unittest.mock.patch.object(align, "_bundle", None), \
                unittest.mock.patch.object(align, "_checkpoint", lambda: checkpoint), \
                unittest.mock.patch.object(align, "_available_memory", lambda: free):
            return align.preload_skip_reason(Config())

    def test_not_downloaded_is_not_downloaded_in_the_background(self):
        self.assertIn("not downloaded", self.reason(None, 16e9))

    def test_short_of_memory_waits_for_first_use(self):
        self.assertIn("1.0 GB", self.reason("mms.pt", 1.0e9))

    def test_plenty_of_memory_preloads(self):
        self.assertIsNone(self.reason("mms.pt", 8e9))
        self.assertIsNone(self.reason("mms.pt", None))   # unknown: as before

    def test_the_relay_skips_the_preload_with_a_reason(self):
        from teto_relay import align, app
        from teto_relay.config import Config

        started = []
        fake_self = types.SimpleNamespace(cfg=Config(align_boundaries=True))
        with unittest.mock.patch.object(align, "preload_skip_reason", lambda cfg: "only 1.0 GB of memory is free"), \
                unittest.mock.patch.object(app.threading, "Thread",
                                           lambda **kw: types.SimpleNamespace(start=lambda: started.append(kw))), \
                self.assertLogs("teto_relay", level="INFO") as seen:
            app.TetoRelay._preload_in_background(fake_self)
        self.assertEqual(started, [])
        self.assertIn("1.0 GB", "\n".join(seen.output))

    def test_the_relay_preloads_when_it_is_safe(self):
        from teto_relay import align, app
        from teto_relay.config import Config

        started = []
        fake_self = types.SimpleNamespace(cfg=Config(align_boundaries=True))
        with unittest.mock.patch.object(align, "preload_skip_reason", lambda cfg: None), \
                unittest.mock.patch.object(app.threading, "Thread",
                                           lambda **kw: types.SimpleNamespace(start=lambda: started.append(kw))):
            app.TetoRelay._preload_in_background(fake_self)
        self.assertEqual(len(started), 1)


if __name__ == "__main__":
    unittest.main()
