"""The crash log: a native crash must leave a trace, not just close the app."""

import faulthandler
import logging
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


class TestCrashLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._hook = threading.excepthook
        self._fault = faulthandler.is_enabled()

    def tearDown(self):
        from teto_relay import __main__ as entry

        faulthandler.disable()
        if self._fault:
            faulthandler.enable()
        threading.excepthook = self._hook
        if entry._crash_file is not None:
            entry._crash_file.close()
            entry._crash_file = None
        self.tmp.cleanup()

    def cfg(self):
        from teto_relay.config import Config

        cfg = Config()
        cfg.log_file = str(self.home / "logs" / "teto-relay.log")
        return cfg

    def test_it_sits_beside_the_log_and_marks_each_start(self):
        from teto_relay.__main__ import record_crashes

        path = record_crashes(self.cfg())
        self.assertEqual(path, self.home / "logs" / "teto-relay-crash.log")
        self.assertTrue(faulthandler.is_enabled())
        record_crashes(self.cfg())
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("=== Teto Relay started"), 2)

    def test_a_big_old_file_is_started_over(self):
        from teto_relay.__main__ import record_crashes

        path = self.home / "logs" / "teto-relay-crash.log"
        path.parent.mkdir(parents=True)
        path.write_text("x" * 300_000, encoding="utf-8")
        record_crashes(self.cfg())
        self.assertLess(path.stat().st_size, 1000)

    def test_an_unwritable_folder_does_not_stop_the_app(self):
        from teto_relay.__main__ import record_crashes

        blocker = self.home / "logs"
        blocker.write_text("a file where the folder should be", encoding="utf-8")
        self.assertIsNone(record_crashes(self.cfg()))

    def test_a_dying_worker_thread_is_logged(self):
        from teto_relay.__main__ import record_crashes

        record_crashes(self.cfg())
        with self.assertLogs("teto_relay", level="CRITICAL") as seen:
            def boom():
                raise ZeroDivisionError("worker broke")

            import io
            import contextlib

            with contextlib.redirect_stderr(io.StringIO()):
                t = threading.Thread(target=boom, name="render")
                t.start()
                t.join()
        self.assertIn("render", seen.output[0])
        self.assertIn("ZeroDivisionError", "\n".join(seen.output))

    def test_a_native_crash_writes_the_stack(self):
        # A real crash, in a child process: the parent only reads the file.
        log_file = self.home / "teto-relay.log"
        code = (
            "import ctypes, faulthandler\n"
            "from teto_relay.config import Config\n"
            "from teto_relay.__main__ import record_crashes\n"
            "cfg = Config(); cfg.log_file = %r\n"
            "record_crashes(cfg)\n"
            "def opening_the_microphone():\n"
            "    faulthandler._sigsegv()\n"
            "opening_the_microphone()\n"
        ) % str(log_file)
        root = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, timeout=120)
        text = (self.home / "teto-relay-crash.log").read_text(encoding="utf-8")
        self.assertIn("opening_the_microphone", text)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
