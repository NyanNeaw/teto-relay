"""Regression tests for the productionization work (see ROADMAP.md).

Like test_pipeline.py, nothing here needs audio hardware, OpenUtau, .NET, a
voicebank or a GPU: those boundaries are replaced with fakes.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestAudioImportIsLazy(unittest.TestCase):
    """P0-4: importing the app must not load PortAudio."""

    def test_modules_import_without_sounddevice(self):
        # A fresh interpreter where `import sounddevice` fails the way it does
        # on a machine with no PortAudio library.
        code = (
            "import sys\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'sounddevice':\n"
            "            raise OSError('PortAudio library not found')\n"
            "sys.meta_path.insert(0, Block())\n"
            "import teto_relay.capture, teto_relay.playback, teto_relay.devices\n"
            "from teto_relay.devices import AudioUnavailable, sd\n"
            "try:\n"
            "    sd()\n"
            "except AudioUnavailable as exc:\n"
            "    print('friendly:', 'requirements.txt' in str(exc))\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("friendly: True", out.stdout)


class _PanelServer:
    """The real control-panel handler on a spare port, with config in a temp dir."""

    def __enter__(self):
        import tempfile
        import threading
        import unittest.mock
        from http.server import ThreadingHTTPServer

        from teto_relay import config as config_mod
        from teto_relay import webui

        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "config.json"
        self._patch = unittest.mock.patch.object(config_mod, "CONFIG_PATH", self.config_path)
        self._patch.start()
        cfg = config_mod.Config(voicebank_root=self.tmp.name)
        cfg.save()
        self.controller = webui.Controller(cfg)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webui.make_handler(self.controller))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def request(self, method, path, body=None, headers=None):
        import http.client
        import json

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def __exit__(self, *exc):
        import logging

        self.server.shutdown()
        self.server.server_close()
        logging.getLogger().removeHandler(self.controller.buffer)
        self._patch.stop()
        self.tmp.cleanup()


class TestControlPanelIsNotDrivableFromOtherSites(unittest.TestCase):
    """P0-1: a web page must not be able to drive the local panel."""

    TOKEN = {"X-Teto-Relay": "1"}

    def saved(self, server) -> dict:
        import json

        return json.loads(server.config_path.read_text(encoding="utf-8"))

    def test_post_without_the_panel_header_is_refused(self):
        # What a cross-site <form> or fetch(..., {mode: 'no-cors'}) can send.
        with _PanelServer() as s:
            status, _ = s.request("POST", "/api/config", {"transpose": 7},
                                  {"Content-Type": "text/plain", "Origin": "https://evil.example"})
            self.assertEqual(status, 403)
            status, _ = s.request("POST", "/api/config", {"transpose": 7})
            self.assertEqual(status, 403)
            self.assertEqual(self.saved(s)["transpose"], 0)

    def test_a_foreign_origin_is_refused_even_with_the_header(self):
        with _PanelServer() as s:
            status, _ = s.request("POST", "/api/start", None,
                                  {**self.TOKEN, "Origin": "http://evil.example"})
            self.assertEqual(status, 403)
            self.assertFalse(s.controller.running)

    def test_dns_rebinding_host_is_refused(self):
        with _PanelServer() as s:
            status, _ = s.request("GET", "/api/config", None, {"Host": f"evil.example:{s.port}"})
            self.assertEqual(status, 403)
            status, _ = s.request("POST", "/api/config", {"transpose": 7},
                                  {**self.TOKEN, "Host": f"evil.example:{s.port}"})
            self.assertEqual(status, 403)

    def test_the_panel_itself_still_works(self):
        with _PanelServer() as s:
            status, _ = s.request("GET", "/api/status")
            self.assertEqual(status, 200)
            status, _ = s.request(
                "POST", "/api/config", {"transpose": 3},
                {**self.TOKEN, "Content-Type": "application/json",
                 "Origin": f"http://127.0.0.1:{s.port}"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(self.saved(s)["transpose"], 3)

    def test_internal_paths_cannot_be_set_through_the_panel_api(self):
        # openutau_dir and voicebank_root are settable: a new user has to be
        # able to point the panel at their install. Where logs and output are
        # written is not the panel's business.
        with _PanelServer() as s:
            status, _ = s.request("POST", "/api/config",
                                  {"log_file": "C:/Windows/evil.log", "out_dir": "C:/Windows"},
                                  self.TOKEN)
            self.assertEqual(status, 200)
            saved = self.saved(s)
            self.assertNotEqual(saved["log_file"], "C:/Windows/evil.log")
            self.assertNotEqual(saved["out_dir"], "C:/Windows")

    def test_bad_values_are_refused_with_a_reason_and_not_saved(self):
        import json

        with _PanelServer() as s:
            status, body = s.request("POST", "/api/config", {"beam_size": 0, "transpose": "2"},
                                     self.TOKEN)
            self.assertEqual(status, 400)
            self.assertIn("beam_size", json.loads(body)["error"])
            self.assertEqual(self.saved(s)["transpose"], 0)
            status, _ = s.request("POST", "/api/config", {"transpose": "2"}, self.TOKEN)
            self.assertEqual(status, 200)
            self.assertEqual(self.saved(s)["transpose"], 2)
            self.assertEqual(s.controller.cfg.transpose, 2)

    def test_setup_check_is_available_to_the_panel(self):
        import json

        with _PanelServer() as s:
            status, body = s.request("GET", "/api/doctor")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("Voicebanks", data["text"])
        self.assertIn(data["ok"], (True, False))

    def test_status_carries_health(self):
        import json

        with _PanelServer() as s:
            status, body = s.request("GET", "/api/status")
        self.assertEqual(json.loads(body)["health"], {"microphone": "stopped", "problems": []})

    def test_the_page_is_served_from_its_own_file(self):
        with _PanelServer() as s:
            status, body = s.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body)
        self.assertIn(b"X-Teto-Relay", body)
        self.assertEqual(body, (ROOT / "teto_relay" / "web" / "index.html").read_bytes())

    def test_the_panel_uses_the_config_file_it_was_started_with(self):
        # --web --config other.json used to read and save the default file.
        import json
        import threading
        from http.server import ThreadingHTTPServer

        from teto_relay import webui
        from teto_relay.config import Config

        with _PanelServer() as s:
            other = Path(s.tmp.name) / "other.json"
            Config(transpose=4).save(other)
            controller = webui.Controller(Config.load(other), other)
            server = ThreadingHTTPServer(("127.0.0.1", 0), webui.make_handler(controller))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                import http.client

                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", "/api/config")
                self.assertEqual(json.loads(conn.getresponse().read())["config"]["transpose"], 4)
                conn.request("POST", "/api/config", body=json.dumps({"transpose": 6}),
                             headers=self.TOKEN)
                self.assertEqual(conn.getresponse().status, 200)
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                import logging

                logging.getLogger().removeHandler(controller.buffer)
            self.assertEqual(json.loads(other.read_text(encoding="utf-8"))["transpose"], 6)
            self.assertEqual(self.saved(s)["transpose"], 0)  # the default file is untouched

    def test_host_and_origin_rules(self):
        from teto_relay.webui import host_allowed, origin_allowed

        self.assertTrue(host_allowed("127.0.0.1:8765", 8765))
        self.assertTrue(host_allowed("localhost:8765", 8765))
        self.assertTrue(host_allowed("[::1]:8765", 8765))
        self.assertFalse(host_allowed("127.0.0.1:9999", 8765))
        self.assertFalse(host_allowed("127.0.0.1.evil.example:8765", 8765))
        self.assertFalse(host_allowed(None, 8765))
        self.assertTrue(origin_allowed(None, 8765))
        self.assertTrue(origin_allowed("http://localhost:8765", 8765))
        self.assertFalse(origin_allowed("null", 8765))
        self.assertFalse(origin_allowed("https://127.0.0.1:8765", 8765))


class _TempHome:
    """Point the data folder (TETO_RELAY_HOME) at a temporary directory."""

    def __enter__(self):
        import os
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        # resolve(): on Windows %TEMP% is often an 8.3 short path
        # (C:\Users\WINDOW~1), and the app resolves it to the long name.
        self.home = Path(self.tmp.name).resolve()
        self._old = os.environ.get("TETO_RELAY_HOME")
        os.environ["TETO_RELAY_HOME"] = str(self.home)
        return self.home

    def __exit__(self, *exc):
        import os

        if self._old is None:
            os.environ.pop("TETO_RELAY_HOME", None)
        else:
            os.environ["TETO_RELAY_HOME"] = self._old
        # Windows can't delete a file that is still open, and setup_logging
        # leaves its log file open until the next call. Close any handler
        # writing into this folder first.
        import logging

        root = logging.getLogger()
        for handler in list(root.handlers):
            filename = getattr(handler, "baseFilename", None)
            if filename and Path(filename).resolve().is_relative_to(self.home):
                root.removeHandler(handler)
                handler.close()
        self.tmp.cleanup()


class TestConfigLoading(unittest.TestCase):
    """P0-5: config mistakes are explained, not crashed on."""

    def load(self, text: str):
        from teto_relay.config import Config

        with _TempHome() as home:
            path = home / "config.json"
            path.write_text(text, encoding="utf-8")
            return Config.load(path)

    def test_missing_file_gives_defaults(self):
        from teto_relay.config import Config

        with _TempHome() as home:
            cfg = Config.load(home / "nope.json")
            self.assertEqual(cfg.voicebank, "english")
            self.assertTrue(str(cfg.out_dir).startswith(str(home)))

    def test_invalid_json_says_where_and_what_to_do(self):
        from teto_relay.config import ConfigError

        with self.assertRaises(ConfigError) as caught:
            self.load('{"transpose": 3,,}')
        message = str(caught.exception)
        self.assertIn("line 1", message)
        self.assertIn("delete the file", message)

    def test_unknown_keys_are_ignored_with_a_warning(self):
        with self.assertLogs("teto_relay.config", "WARNING") as logs:
            cfg = self.load('{"transpose": 2, "from_the_future": true}')
        self.assertEqual(cfg.transpose, 2)
        self.assertIn("from_the_future", "\n".join(logs.output))

    def test_strings_from_hand_edits_are_coerced(self):
        cfg = self.load('{"transpose": "3", "use_alignment": "false", "playback_gain": "0.5"}')
        self.assertEqual(cfg.transpose, 3)
        self.assertIs(cfg.use_alignment, False)
        self.assertEqual(cfg.playback_gain, 0.5)

    def test_bad_values_are_all_reported_at_once(self):
        from teto_relay.config import ConfigError

        with self.assertRaises(ConfigError) as caught:
            self.load('{"transpose": "loud", "beam_size": 0, "capture_mode": "karaoke"}')
        message = str(caught.exception)
        for key in ("transpose", "beam_size", "capture_mode"):
            self.assertIn(key, message)

    def test_settings_of_the_removed_voice_engine_are_dropped_quietly(self):
        # Every config written before 0.3.0 lists mode and rvc_*; they must
        # neither stop the app nor warn on every start.
        with self.assertNoLogs("teto_relay.config", level="WARNING"):
            cfg = self.load('{"mode": "voice", "rvc_model": "x.pth", "voice_streaming": true,'
                            ' "transpose": 2}')
        self.assertEqual(cfg.transpose, 2)
        self.assertFalse(hasattr(cfg, "rvc_model"))

    def test_choices_are_case_insensitive(self):
        cfg = self.load('{"capture_mode": "VAD", "lyric_mode": "Japanese"}')
        self.assertEqual(cfg.capture_mode, "vad")
        self.assertEqual(cfg.lyric_mode, "japanese")

    def test_relationships_are_checked(self):
        from teto_relay.config import ConfigError

        with self.assertRaises(ConfigError):
            self.load('{"f0_min": 500, "f0_max": 400}')
        with self.assertRaises(ConfigError):
            self.load('{"sample_rate": 44100}')

    def test_relative_paths_are_made_absolute_against_the_data_folder(self):
        # The OpenUtau host chdirs into its own folder; a relative path left
        # relative would start pointing there.
        from teto_relay.config import Config

        with _TempHome() as home:
            path = home / "config.json"
            path.write_text('{"out_dir": "renders", "voicebank_root": "banks"}', encoding="utf-8")
            cfg = Config.load(path)
            self.assertEqual(Path(cfg.out_dir), (home / "renders").resolve())
            self.assertEqual(Path(cfg.voicebank_root), (home / "banks").resolve())

    def test_save_round_trips_and_leaves_no_temp_file(self):
        from teto_relay.config import Config

        with _TempHome() as home:
            cfg = Config(transpose=5)
            path = cfg.save(home / "config.json")
            self.assertEqual(Config.load(path).transpose, 5)
            self.assertFalse((home / "config.json.tmp").exists())

    def test_the_repo_ships_no_personal_config(self):
        # P0-3: the committed config.json named a bank that only existed on
        # one machine, so everyone else's first start crashed.
        tracked = subprocess.run(
            ["git", "ls-files", "config.json"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(tracked, "")
        from teto_relay.config import Config

        defaults = Config()
        self.assertEqual(defaults.voicebank_root, "")
        self.assertEqual(defaults.openutau_dir, "")
        # Not a hard-coded folder: output goes in the data folder, wherever
        # this checkout happens to be.
        from teto_relay import paths

        self.assertEqual(Path(defaults.out_dir), paths.data_dir() / "out")


class TestDataFolders(unittest.TestCase):
    """P1-5: packaged builds keep settings somewhere writable."""

    def with_frozen(self, exe_dir: Path, fn):
        import unittest.mock

        from teto_relay import paths

        with unittest.mock.patch.object(sys, "frozen", True, create=True), \
                unittest.mock.patch.object(sys, "executable", str(exe_dir / "TetoRelay.exe")):
            return fn(paths)

    def test_source_checkout_uses_the_project_folder(self):
        import os
        import unittest.mock

        from teto_relay import paths

        with unittest.mock.patch.dict(os.environ, {"TETO_RELAY_HOME": ""}):
            self.assertEqual(paths.data_dir(), ROOT)

    def test_installed_build_uses_local_app_data(self):
        import os
        import tempfile
        import unittest.mock

        with tempfile.TemporaryDirectory() as tmp:
            exe_dir, appdata = Path(tmp, "Program Files", "TetoRelay"), Path(tmp, "AppData")
            exe_dir.mkdir(parents=True)
            with unittest.mock.patch.dict(os.environ, {"TETO_RELAY_HOME": "", "LOCALAPPDATA": str(appdata)}):
                self.assertEqual(self.with_frozen(exe_dir, lambda p: p.data_dir()), appdata / "TetoRelay")

    def test_portable_build_keeps_data_beside_the_exe(self):
        import os
        import tempfile
        import unittest.mock

        with tempfile.TemporaryDirectory() as tmp:
            exe_dir = Path(tmp).resolve() / "TetoRelay"
            exe_dir.mkdir()
            (exe_dir / "portable.txt").write_text("", encoding="utf-8")
            with unittest.mock.patch.dict(os.environ, {"TETO_RELAY_HOME": ""}):
                self.assertEqual(self.with_frozen(exe_dir, lambda p: p.data_dir()), exe_dir / "data")


class TestLocate(unittest.TestCase):
    """P0-3: no personal paths - OpenUtau and the banks are found instead."""

    def test_openutau_is_found_from_the_environment(self):
        import os
        import tempfile
        import unittest.mock

        from teto_relay.locate import find_openutau

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "OpenUtau.Core.dll").write_bytes(b"")
            with unittest.mock.patch.dict(os.environ, {"OPENUTAU_DIR": tmp}):
                self.assertEqual(find_openutau(""), Path(tmp))
            self.assertEqual(find_openutau("X:/configured"), Path("X:/configured"))

    def test_voicebank_root_falls_back_to_the_data_folder(self):
        from teto_relay.locate import find_voicebank_root

        with _TempHome() as home:
            root = find_voicebank_root("")
            self.assertTrue(root.is_dir())
            self.assertTrue(str(root).startswith(str(home)))

    def test_a_folder_with_a_bank_is_preferred(self):
        from teto_relay.locate import find_voicebank_root

        with _TempHome() as home:
            bank = home / "voicebanks" / "Teto"
            bank.mkdir(parents=True)
            (bank / "oto.ini").write_text("a.wav=a,0,0,0,0,0\n", encoding="utf-8")
            self.assertEqual(find_voicebank_root(""), home / "voicebanks")


class TestVoicebankErrors(unittest.TestCase):
    """P1-4: voicebank problems say what to do."""

    def test_missing_folder_is_explained(self):
        from teto_relay.voicebank import VoicebankError, discover

        with self.assertRaises(VoicebankError) as caught:
            discover("/definitely/not/here")
        self.assertIn("voicebank_root", str(caught.exception))

    def test_no_banks_is_explained(self):
        from teto_relay.voicebank import VoicebankError, select_or_default

        with self.assertRaises(VoicebankError) as caught:
            select_or_default([], "english", "/somewhere")
        self.assertIn("oto.ini", str(caught.exception))

    def test_an_unknown_key_falls_back_to_the_first_bank(self):
        from teto_relay.voicebank import Voicebank, select_or_default

        bank = Voicebank(key="tandoku", name="Teto", root=Path("/x"), flavour="ja-cv")
        with self.assertLogs("teto_relay.voicebank", "WARNING"):
            self.assertIs(select_or_default([bank], "english"), bank)


def _bare_relay(cfg):
    """A TetoRelay with only a config: no voicebank, models or devices."""
    from teto_relay.app import TetoRelay

    relay = object.__new__(TetoRelay)
    relay.cfg = cfg
    return relay


class TestOutputTrimming(unittest.TestCase):
    """P0-7 / P2-13: trimming out/ must never kill the worker thread."""

    def make(self, folder: Path, stems: list[str]):
        import os
        import time

        now = time.time()
        for i, stem in enumerate(stems):
            for suffix in (".ustx", ".wav"):
                path = folder / f"{stem}{suffix}"
                path.write_bytes(b"x")
                os.utime(path, (now + i, now + i))

    def test_keeps_whole_utterances_newest_first(self):
        import tempfile

        from teto_relay.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(out_dir=tmp, keep_files=12, queue_size=1)
            self.make(Path(tmp), [f"relay_{i:02d}" for i in range(20)])
            _bare_relay(cfg)._trim_output()
            left = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(len(left), 24)  # 12 utterances x (.ustx + .wav)
            self.assertIn("relay_19.wav", left)
            self.assertNotIn("relay_07.wav", left)

    def test_never_trims_below_what_is_still_queued(self):
        import tempfile

        from teto_relay.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(out_dir=tmp, keep_files=1, queue_size=4)
            self.make(Path(tmp), [f"relay_{i:02d}" for i in range(20)])
            _bare_relay(cfg)._trim_output()
            self.assertEqual(len(list(Path(tmp).iterdir())), 2 * (2 * 4 + 2))

    def test_a_file_vanishing_mid_trim_is_not_an_error(self):
        import tempfile
        import unittest.mock

        from teto_relay.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(out_dir=tmp, keep_files=1, queue_size=1)
            self.make(Path(tmp), [f"relay_{i:02d}" for i in range(10)])
            real_stat = Path.stat

            def flaky_stat(path, *args, **kwargs):
                if path.name == "relay_03.wav":
                    raise FileNotFoundError(path)
                return real_stat(path, *args, **kwargs)

            with unittest.mock.patch.object(Path, "stat", flaky_stat):
                _bare_relay(cfg)._trim_output()  # must not raise

    def test_an_unreadable_folder_is_not_an_error(self):
        import unittest.mock

        from teto_relay.config import Config

        relay = _bare_relay(Config(keep_files=5))
        with unittest.mock.patch.object(Path, "glob", side_effect=PermissionError("denied")):
            relay._trim_output()  # must not raise


class _FakeTask:
    def __init__(self, finishes: bool, samples=None, position=0.0, leading=0.0):
        import types

        self.finishes = finishes
        self.waited_ms = None
        self.Result = types.SimpleNamespace(samples=samples, positionMs=position, leadingMs=leading)

    def Wait(self, ms):
        self.waited_ms = ms
        return self.finishes


class _FakeCancellation:
    cancelled = False

    def Cancel(self):
        self.cancelled = True


def _bare_openutau(cfg, tasks):
    """An OpenUtauRenderer with the .NET engine replaced by fakes."""
    import types

    from teto_relay.render.openutau import OpenUtauRenderer

    renderer = object.__new__(OpenUtauRenderer)
    renderer.cfg = cfg
    queue = list(tasks)
    renderer.renderer = types.SimpleNamespace(Render=lambda *a: queue.pop(0))
    renderer._progress = lambda: None
    return renderer


class TestOpenUtauWorkIsCached(unittest.TestCase):
    """P1-11: the typed setter is compiled once per field, not per utterance."""

    def test_setter_is_compiled_once(self):
        import types
        import unittest.mock

        from teto_relay.render import openutau

        compiled = []
        fake_type = types.SimpleNamespace(FullName="OpenUtau.Api.PhonemizerRequest")
        with unittest.mock.patch.object(openutau, "_SETTERS", {}), \
                unittest.mock.patch.object(openutau, "_compile_int64_field_setter",
                                           side_effect=lambda t, f: compiled.append(f) or object()):
            first = openutau._int64_field_setter(fake_type, "timestamp")
            second = openutau._int64_field_setter(fake_type, "timestamp")
        self.assertIs(first, second)
        self.assertEqual(compiled, ["timestamp"])


class TestRenderTimeout(unittest.TestCase):
    """P0-6: a stalled synthesis engine costs one utterance, not the relay."""

    def test_a_stalled_phrase_is_cancelled_and_reported(self):
        from teto_relay.config import Config
        from teto_relay.render.base import RenderError

        stalled = _FakeTask(finishes=False)
        cancel = _FakeCancellation()
        renderer = _bare_openutau(Config(render_timeout_seconds=2.5), [stalled])
        with self.assertRaises(RenderError) as caught:
            renderer._synthesise(["phrase"], cancel)
        self.assertEqual(stalled.waited_ms, 2500)
        self.assertTrue(cancel.cancelled)
        self.assertIn("restart", str(caught.exception))

    def test_finished_phrases_are_collected(self):
        from teto_relay.config import Config

        tasks = [_FakeTask(True, [0.1, 0.2], position=500, leading=40),
                 _FakeTask(True, [0.3], position=900, leading=10)]
        renderer = _bare_openutau(Config(), tasks)
        segments = renderer._synthesise(["a", "b"], _FakeCancellation())
        self.assertEqual([offset for offset, _ in segments], [460.0, 890.0])


class TestFriendlyErrors(unittest.TestCase):
    """P1-4: problems a person can fix are explained, not dumped."""

    def run_main(self, argv):
        import contextlib
        import io

        from teto_relay.__main__ import main

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(argv)
        return code, err.getvalue()

    def test_a_broken_config_file_is_explained(self):
        with _TempHome() as home:
            bad = home / "config.json"
            bad.write_text("{oops", encoding="utf-8")
            code, err = self.run_main(["--config", str(bad), "--list-banks"])
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)
        self.assertNotIn("Traceback", err)

    def test_a_bad_command_line_value_is_explained(self):
        with _TempHome():
            code, err = self.run_main(["--key", " ", "--list-banks"])
        self.assertEqual(code, 2)
        self.assertIn("ptt_key", err)

    def test_known_errors_exit_cleanly_with_their_message(self):
        import unittest.mock

        from teto_relay.voicebank import VoicebankError

        with _TempHome(), unittest.mock.patch(
            "teto_relay.__main__._run", side_effect=VoicebankError("No UTAU voicebanks found.")
        ):
            code, err = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn("No UTAU voicebanks found.", err)
        self.assertNotIn("Traceback", err)

    def test_unexpected_errors_point_at_the_log(self):
        import unittest.mock

        with _TempHome(), unittest.mock.patch(
            "teto_relay.__main__._run", side_effect=ZeroDivisionError("boom")
        ):
            code, err = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn("unexpected error", err)
        self.assertIn(".log", err)
        self.assertIn("--doctor", err)

    def test_version(self):
        import contextlib
        import io

        from teto_relay import __version__
        from teto_relay.__main__ import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--version"]), 0)
        self.assertIn(__version__, out.getvalue())


class TestFailedStartCleansUp(unittest.TestCase):
    """P1-2: a start that fails part-way stops whatever it had started."""

    def test_devices_and_threads_are_stopped_when_start_fails(self):
        import threading
        import unittest.mock

        from teto_relay.config import Config

        relay = _bare_relay(Config())
        relay._stop = threading.Event()
        relay._threads, relay._hotkey, relay._capture = [], None, None
        relay.renderer = unittest.mock.Mock()
        relay.converter = None
        player = unittest.mock.Mock()

        def half_start():
            relay._player = player  # the player got going...
            raise RuntimeError("...and then the microphone would not open")

        relay._player = None
        relay._start = half_start
        with self.assertRaises(RuntimeError):
            relay.start()
        player.stop.assert_called_once()
        relay.renderer.close.assert_called_once()
        self.assertTrue(relay._stop.is_set())


class TestPanelStartup(unittest.TestCase):
    """P1-13: a busy port is explained; a second launch shows the first."""

    def test_port_used_by_something_else(self):
        import socket

        from teto_relay.config import Config
        from teto_relay.errors import TetoRelayError
        from teto_relay.webui import serve

        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        try:
            with _TempHome(), self.assertRaises(TetoRelayError) as caught:
                serve(Config(), port=port, open_browser=False)
            self.assertIn(f"--port {port + 1}", str(caught.exception))
        finally:
            blocker.close()

    def test_quit_from_the_panel_stops_the_server(self):
        import http.client
        import threading

        from teto_relay.config import Config
        from teto_relay.webui import serve

        with _TempHome():
            import socket

            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            result = {}
            server = threading.Thread(
                target=lambda: result.setdefault("code", serve(Config(), port=port, open_browser=False)),
                daemon=True,
            )
            server.start()
            for _ in range(100):
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    conn.request("POST", "/api/quit", headers={"X-Teto-Relay": "1"})
                    self.assertEqual(conn.getresponse().status, 200)
                    break
                except OSError:
                    threading.Event().wait(0.05)
            server.join(timeout=10)
        self.assertFalse(server.is_alive())
        self.assertEqual(result.get("code"), 0)

    def test_second_launch_finds_the_running_panel(self):
        from teto_relay.webui import serve

        with _PanelServer() as running:
            self.assertEqual(serve(running.controller.cfg, port=running.port, open_browser=False), 0)


class TestDoctor(unittest.TestCase):
    """P1-9: the setup check runs anywhere and says what to fix."""

    def test_runs_without_hardware_and_reports_every_group(self):
        from teto_relay.config import Config
        from teto_relay.doctor import format_checks, run_checks

        with _TempHome():
            checks = run_checks(Config())
        names = {c.name for c in checks}
        for expected in ("Python", "Data folder", "Voicebanks", "OpenUtau"):
            self.assertIn(expected, names)
        text = format_checks(checks)
        for check in checks:
            if check.status != "ok" and check.fix:
                self.assertIn(check.fix, text)

    def test_missing_openutau_names_the_places_searched(self):
        import os
        import unittest.mock

        from teto_relay.config import Config
        from teto_relay.doctor import check_openutau

        with unittest.mock.patch.dict(os.environ, {"OPENUTAU_DIR": "/nowhere/OpenUtau"}), \
                unittest.mock.patch("teto_relay.locate.find_openutau", return_value=None):
            result = check_openutau(Config())[0]
        self.assertEqual(result.status, "fail")
        self.assertIn(str(Path("/nowhere/OpenUtau")), result.fix)

    def test_exit_code_reflects_failures(self):
        import contextlib
        import io
        import unittest.mock

        from teto_relay import doctor
        from teto_relay.config import Config

        fine = [doctor.Check(doctor.OK, "x", "fine")]
        broken = fine + [doctor.Check(doctor.FAIL, "y", "broken", "fix it")]
        with contextlib.redirect_stdout(io.StringIO()):
            with unittest.mock.patch.object(doctor, "run_checks", return_value=fine):
                self.assertEqual(doctor.run_doctor(Config()), 0)
            with unittest.mock.patch.object(doctor, "run_checks", return_value=broken):
                self.assertEqual(doctor.run_doctor(Config()), 1)


class _FakeInputStream:
    """Stands in for sounddevice.InputStream, following a script per open."""

    def __init__(self, plan, callback, blocksize, **_):
        import threading

        self.plan, self.callback, self.blocksize = plan, callback, blocksize
        self._thread = threading.Thread(target=self._feed, daemon=True)

    def __enter__(self):
        if self.plan == "fail":
            raise OSError("Device unavailable [PaErrorCode -9985]")
        self._thread.start()
        return self

    def _feed(self):
        import time

        import numpy as np

        for _ in range(self.plan):
            self.callback(np.full((self.blocksize, 1), 0.2, dtype=np.float32), self.blocksize, None, None)
            time.sleep(0.005)
        # ...and then nothing, like an unplugged USB mic.

    def __exit__(self, *exc):
        return False


class TestMicrophoneRecovery(unittest.TestCase):
    """P1-1: a microphone that fails or dies is reopened, and says so."""

    def run_capture(self, plans, until):
        import queue
        import time
        import types
        import unittest.mock

        from teto_relay import capture
        from teto_relay.config import Config

        opened = []

        def input_stream(**kwargs):
            plan = plans[min(len(opened), len(plans) - 1)]
            opened.append(plan)
            return _FakeInputStream(plan, **kwargs)

        fake_sd = types.SimpleNamespace(InputStream=input_stream)
        cfg = Config(capture_mode="vad")
        pushed = []

        class Recorder:
            def push(self, frame):
                pushed.append(frame)

            def flush(self):
                return None

        mic = capture.MicCapture(cfg, queue.Queue(), chunker=Recorder())
        mic.STALL_SECONDS, mic.FIRST_RETRY_SECONDS, mic.MAX_RETRY_SECONDS = 0.3, 0.05, 0.1
        with unittest.mock.patch.object(capture, "sd", lambda: fake_sd):
            mic.start()
            deadline = time.monotonic() + 10
            while not until(opened, pushed, mic) and time.monotonic() < deadline:
                time.sleep(0.02)
            states = mic.state
            error = mic.last_error
            mic.stop()
            mic.join(timeout=5)
        self.assertFalse(mic.is_alive(), "capture thread did not stop")
        return opened, pushed, states, error

    def test_a_mic_that_will_not_open_is_retried_until_it_does(self):
        opened, pushed, _, _ = self.run_capture(
            ["fail", "fail", 50], until=lambda o, p, m: len(p) >= 50
        )
        self.assertEqual(opened[:3], ["fail", "fail", 50])
        self.assertGreaterEqual(len(pushed), 50)

    def test_a_mic_that_goes_quiet_is_reopened(self):
        opened, pushed, _, _ = self.run_capture(
            [20, 20], until=lambda o, p, m: len(o) >= 2 and len(p) >= 40
        )
        self.assertGreaterEqual(len(opened), 2)
        self.assertGreaterEqual(len(pushed), 40)

    def test_the_relay_reports_a_missing_microphone(self):
        import types

        from teto_relay.config import Config

        relay = _bare_relay(Config())
        relay._capture = types.SimpleNamespace(state="retrying", last_error="PaErrorCode -9985",
                                               paused=False)
        relay.renderer = types.SimpleNamespace(name="openutau")
        health = relay.health()
        self.assertEqual(health["microphone"], "retrying")
        self.assertTrue(any("PaErrorCode" in p for p in health["problems"]))

    def test_state_says_what_is_wrong_while_retrying(self):
        _, _, state, error = self.run_capture(
            ["fail"], until=lambda o, p, m: len(o) >= 2 and m.state == "retrying"
        )
        self.assertEqual(state, "retrying")
        self.assertIn("PaErrorCode", error)


class TestTray(unittest.TestCase):
    """P1-3: with no console, a failed start must show in the tray."""

    def test_a_failed_start_is_shown_and_can_be_retried(self):
        import unittest.mock

        from teto_relay import tray
        from teto_relay.config import Config
        from teto_relay.voicebank import VoicebankError

        app = tray.TrayApp(Config())
        changes = []
        app.on_change = lambda: changes.append(app.state)
        with unittest.mock.patch.object(tray, "TetoRelay",
                                        side_effect=VoicebankError("No UTAU voicebanks found.")):
            app.start()
        self.assertEqual(app.state, "error")
        self.assertIn("No UTAU voicebanks found.", app.status_line())
        self.assertEqual(changes, ["starting", "error"])

        working = unittest.mock.Mock(paused=False, last_text="hello",
                                     health=lambda: {"problems": []})
        with unittest.mock.patch.object(tray, "TetoRelay", return_value=working):
            app.start()
        self.assertEqual(app.state, "live")
        self.assertEqual(app.status_line(), "Last: hello")
        app.toggle_pause()
        working.pause.assert_called_once()
        self.assertEqual(app.state, "paused")

    def test_icon_images_for_every_state(self):
        from teto_relay import tray

        for state in ("starting", "live", "paused", "error"):
            self.assertEqual(tray._icon_image(state).size, (64, 64))


class TestLatencyTimeline(unittest.TestCase):
    """P1-6: per-stage timings add up to release->sound."""

    def test_laps_waits_and_totals(self):
        from teto_relay.latency import Timeline

        t = Timeline(released_at=100.0)
        t.add("speech", 1.5)
        t.lap("wait_analyse", 100.1)
        t.lap("asr", 100.9)
        t.restart(101.0)
        t.lap("wait_render", 101.2)
        t.lap("render", 101.7)
        t.add("phonemize", 0.1)  # nested inside render, not added again
        t.add("synth", 0.4)
        self.assertAlmostEqual(t.stages["asr"], 0.8)
        self.assertAlmostEqual(t.total, 0.1 + 0.8 + 0.2 + 0.5)
        self.assertIn("release->sound", t.summary())
        self.assertIn("phonemize 0.10s", t.summary())

    def test_csv_has_one_header_and_a_row_per_utterance(self):
        import csv

        from teto_relay.latency import STAGES, Timeline, append_csv

        with _TempHome() as home:
            path = home / "latency.csv"
            for _ in range(2):
                t = Timeline(released_at=0.0)
                t.add("asr", 0.5)
                append_csv(path, t, "hello")
            rows = list(csv.reader(path.open(encoding="utf-8")))
        self.assertEqual(rows[0], ["time", "total", *STAGES, "text"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][rows[0].index("asr")], "0.500")

    def test_leading_silence(self):
        import numpy as np

        from teto_relay.latency import leading_silence

        audio = np.concatenate([np.zeros(4410), np.full(100, 0.5)])
        self.assertAlmostEqual(leading_silence(audio, 44100), 0.1, places=3)
        self.assertAlmostEqual(leading_silence(np.zeros((10, 2)), 10), 1.0)


class TestRenderedAudioStartsAtTheFirstSound(unittest.TestCase):
    """P1-7: the mix must not begin with the part's offset as silence."""

    def mix(self, segments, trim=True):
        from teto_relay.config import Config

        renderer = _bare_openutau(Config(trim_leading_silence=trim), [])
        return renderer._mix(segments)

    def test_leading_silence_is_trimmed_and_spacing_kept(self):
        import numpy as np

        from teto_relay.render.openutau import WORLDLINE_SAMPLE_RATE as SR

        a, b = np.full(441, 0.5, dtype=np.float32), np.full(441, 0.25, dtype=np.float32)
        out = self.mix([(400.0, a), (600.0, b)])
        self.assertEqual(out[0], 0.5)  # sound from the very first sample
        gap_start = int(0.2 * SR)
        self.assertAlmostEqual(float(out[gap_start]), 0.25, places=6)
        self.assertEqual(len(out), gap_start + 441)

    def test_without_trimming_the_old_layout_is_kept(self):
        import numpy as np

        from teto_relay.render.openutau import WORLDLINE_SAMPLE_RATE as SR

        out = self.mix([(400.0, np.full(10, 0.5, dtype=np.float32))], trim=False)
        self.assertEqual(float(out[int(0.4 * SR) - 1]), 0.0)
        self.assertEqual(float(out[int(0.4 * SR)]), 0.5)

    def test_a_negative_offset_moves_every_phrase_together(self):
        import numpy as np

        from teto_relay.render.openutau import WORLDLINE_SAMPLE_RATE as SR

        a, b = np.full(10, 0.5, dtype=np.float32), np.full(10, 0.25, dtype=np.float32)
        out = self.mix([(-50.0, a), (100.0, b)], trim=False)
        # b must stay 150 ms after a, not 100 ms (the old clamp).
        self.assertAlmostEqual(float(out[int(round(0.15 * SR))]), 0.25, places=6)


class TestPipelineEndToEnd(unittest.TestCase):
    """The whole utterance path with only the hardware and models faked:
    chunk -> (fake) whisper -> pyin -> notes -> .ustx -> tone renderer ->
    player (fake sounddevice) -> latency line and CSV row."""

    def test_an_utterance_reaches_the_speaker_with_a_latency_report(self):
        import queue
        import threading
        import time
        import types
        import unittest.mock

        import numpy as np

        from teto_relay import playback
        from teto_relay.capture import Chunk
        from teto_relay.config import Config
        from teto_relay.render.null import NullRenderer
        from teto_relay.stt import Word
        from teto_relay.voicebank import Voicebank

        with _TempHome() as home:
            cfg = Config(pitch_method="pyin", use_alignment=False, align_boundaries=False, renderer_backend="null")
            cfg.validate()
            relay = _bare_relay(cfg)
            relay.bank = Voicebank(key="english", name="Teto", root=home / "bank", flavour="en-cvvc")
            relay.transcriber = types.SimpleNamespace(
                transcribe=lambda audio, rate: [Word("hello", 0.1, 0.5), Word("there", 0.6, 1.0)]
            )
            relay.renderer = NullRenderer(cfg)
            relay.converter = None
            relay.engine = "utau"
            relay.chunk_q, relay.ustx_q, relay.wav_q = queue.Queue(), queue.Queue(), queue.Queue()
            relay._stop = threading.Event()
            relay._octave_shift = relay._voice_baseline = None
            relay._singing_state = {}
            relay._target_tone, relay._mora_floor = 61.0, 0.11
            relay.last_text = relay.last_source = relay.last_kana = ""
            relay.last_notes, relay.last_stats = [], {}

            played = threading.Event()
            fake_sd = types.SimpleNamespace(
                play=lambda *a, **k: None, stop=lambda: None, get_stream=lambda: None,
                query_devices=lambda *a: {"default_samplerate": 48000, "max_output_channels": 2},
            )
            reports = []

            def on_playback(job):
                relay._on_playback(job)
                reports.append(job)
                played.set()

            with unittest.mock.patch.object(playback, "sd", lambda: fake_sd):
                player = playback.Player(cfg, relay.wav_q, None, on_playback=on_playback)
                workers = [threading.Thread(target=relay._analyse_loop, daemon=True),
                           threading.Thread(target=relay._render_loop, daemon=True), player]
                for w in workers:
                    w.start()
                rate = cfg.sample_rate
                t = np.arange(int(1.2 * rate)) / rate
                audio = (0.3 * np.sin(2 * np.pi * 150 * t)).astype(np.float32)
                relay.chunk_q.put(Chunk(audio, rate, "release", captured_at=time.monotonic()))
                self.assertTrue(played.wait(60), "the utterance never reached playback")
                relay._stop.set()
                player.stop()
                for w in workers:
                    w.join(timeout=5)

            stages = reports[0].timeline.stages
            for stage in ("speech", "wait_analyse", "asr", "pitch", "notes", "ustx",
                          "wait_render", "render", "wait_output", "output", "lead_silence"):
                self.assertIn(stage, stages)
            self.assertAlmostEqual(stages["speech"], 1.2, places=2)
            self.assertLess(stages["lead_silence"], 0.05)
            self.assertIn("latency", relay.last_stats)
            rows = (home / "latency.csv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 2)
            self.assertIn("hello", rows[1])


class TestVoicebankDiscoveryIsBounded(unittest.TestCase):
    """P1-8: discovery must not walk virtualenvs, caches or deep trees."""

    def make_bank(self, folder: Path, name="Teto"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "character.txt").write_text(f"name={name}\n", encoding="utf-8")
        (folder / "oto.ini").write_text("a.wav=a,0,0,0,0,0\n", encoding="utf-8")

    def test_finds_the_usual_layouts(self):
        import tempfile

        from teto_relay.voicebank import discover

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_bank(root / "TETO-renzokubeta-091020")
            # English/tandoku layout: a nested singer root with sub-banks.
            singer = root / "TETO-tandoku" / "重音テト音声ライブラリー"
            singer.mkdir(parents=True)
            (singer / "character.txt").write_text("name=Teto\n", encoding="utf-8")
            for sub in ("normal", "power"):
                (singer / sub).mkdir()
                (singer / sub / "oto.ini").write_text("a.wav=- あ,0,0,0,0,0\n", encoding="utf-8")
            banks = {b.key: b for b in discover(root)}
        self.assertEqual(set(banks), {"renzokubeta", "tandoku"})
        self.assertEqual(len(banks["tandoku"].subbanks), 2)

    def test_skips_venvs_caches_and_hidden_folders_without_entering_them(self):
        import os
        import tempfile
        import unittest.mock

        from teto_relay import voicebank

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_bank(root / "teto")
            for junk in (".venv/lib/site-packages/pkg", ".cache/hf", "teto-relay/__pycache__",
                         ".installing-x/bank"):
                self.make_bank(root / junk)
            entered = []
            real_walk = os.walk

            def spy(top, *args, **kwargs):
                for current, dirs, files in real_walk(top, *args, **kwargs):
                    entered.append(current)
                    yield current, dirs, files

            with unittest.mock.patch.object(os, "walk", spy):
                banks = voicebank.discover(root)
        self.assertEqual([b.root.name for b in banks], ["teto"])
        self.assertFalse(any(".venv" in p or ".cache" in p or "__pycache__" in p for p in entered))

    def test_stops_at_the_depth_limit(self):
        import tempfile

        from teto_relay.voicebank import find_singer_roots

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_bank(root / "a" / "b" / "c")
            self.make_bank(root / "a" / "b" / "c" / "d" / "e")
            found = find_singer_roots(root, max_depth=3)
        self.assertEqual(found, [root / "a" / "b" / "c"])


def _flat_track(hz: float = 220.0, seconds: float = 3.0):
    import numpy as np

    from teto_relay import pitch as pitch_mod

    n = int(seconds * 100)
    return pitch_mod.F0Track(times=np.arange(n) / 100.0, f0=np.full(n, hz),
                             voiced=np.ones(n, dtype=bool), sample_rate=16000)


class TestNumbersAreSung(unittest.TestCase):
    """P2-1: digits used to be sung as silence."""

    def test_spelling(self):
        from teto_relay.numbers import spell

        cases = {
            "2": "two", "21": "twenty one", "105": "one hundred five", "1,000": "one thousand",
            "1234567": "one million two hundred thirty four thousand five hundred sixty seven",
            "1999": "nineteen ninety nine", "2024": "twenty twenty four", "2005": "two thousand five",
            "5:30": "five thirty", "5:05": "five oh five", "3:00": "three o clock",
            "1st": "first", "22nd": "twenty second", "12th": "twelfth", "40th": "fortieth",
            "3.5": "three point five", "50%": "fifty percent", "$5": "five dollars",
            "007": "zero zero seven", "mp3": "mp three", "hello": "hello",
        }
        for token, expected in cases.items():
            self.assertEqual(spell(token), expected, token)

    def test_transcribed_numbers_become_words(self):
        from teto_relay.stt import clean_lyric

        self.assertEqual(clean_lyric(" 21,"), "twenty one")
        self.assertEqual(clean_lyric("5:30."), "five thirty")
        self.assertEqual(clean_lyric("$5"), "five dollars")
        self.assertEqual(clean_lyric("Hello,"), "hello")  # unchanged path

    def test_multi_word_lyrics_are_converted_word_by_word_in_japanese_mode(self):
        # "I'm" -> "i am" used to be looked up as one string, missed, and
        # romanised letter by letter.
        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Word

        cfg = Config(auto_octave=False)
        notes = build_notes([Word("i am", 0.0, 0.6), Word("twenty one", 0.8, 1.6)],
                            _flat_track(), cfg, japanese_lyrics=True)
        lyrics = "".join(n.lyric for n in notes)
        self.assertEqual(lyrics, "あいあむとうえんち+わん")  # ちい: the い holds ち's vowel


class TestSmallKana(unittest.TestCase):
    """P2-2: small vowels no longer become silent notes of their own."""

    def test_small_vowels_become_full_size(self):
        from teto_relay import translit

        self.assertEqual(translit.expand_long_vowels("ふぁみりー"), "ふあみりい")
        self.assertEqual(translit.to_kana("ファミリー", "ja"), "ふあみりい")
        self.assertEqual(translit.to_kana("パーティー", "ja"), "ぱあていい")
        self.assertEqual(translit.to_kana("ヴァイオリン", "ja"), "ぶあいおりん")

    def test_every_mora_is_one_the_bank_can_sing(self):
        from teto_relay import japanese as jp
        from teto_relay import translit

        singable = set()
        for table in list(jp.MORA.values()) + list(jp.YOUON.values()):
            for kana in table.values():
                singable.update(jp.split_morae(kana))
        singable.add("ん")
        for word in ("ファミリー", "ウェディング", "ヴァイオリン", "ディズニー", "フォーク"):
            for mora in jp.split_morae(translit.to_kana(word, "ja")):
                self.assertIn(mora, singable, f"{word}: {mora}")


class TestAlignmentSurvivesOddWords(unittest.TestCase):
    """P2-3: one unalignable word must not cost the others their timings."""

    def test_unalignable_words_get_no_tokens(self):
        from teto_relay import align
        from teto_relay.stt import Word

        words = [Word("café", 0, 1), Word("会議", 1, 2), Word("don't", 2, 3), Word("i am", 3, 4)]
        tokens, groups = align._tokenize(words)
        self.assertEqual(tokens, ["cafe", "don't", "i", "am"])
        self.assertEqual(groups, [(0, 1), (1, 1), (1, 2), (2, 4)])

    @unittest.skipUnless(__import__("importlib").util.find_spec("torch"), "needs torch")
    def test_other_words_are_still_aligned(self):
        import types
        import unittest.mock

        import numpy as np

        from teto_relay import align
        from teto_relay.config import Config
        from teto_relay.stt import Word

        vocab = align._ALIGNABLE

        def tokenizer(tokens):
            for token in tokens:  # the real one raises on unknown characters
                if set(token) - vocab:
                    raise KeyError(token)
            return tokens

        class Emission:
            shape = (1, 100)

            def __getitem__(self, i):
                return self

        def aligner(emission, tokens):
            span = types.SimpleNamespace
            return [[span(start=10 * i + 2, end=10 * i + 8)] for i, _ in enumerate(tokens)]

        model = lambda waveform: (Emission(), None)  # noqa: E731
        fake = (model, tokenizer, aligner, "cpu")
        words = [Word("hello", 0.0, 0.5), Word("会議", 0.5, 1.0), Word("there", 1.0, 1.5)]
        audio = np.zeros(16000, dtype=np.float32)
        with unittest.mock.patch.object(align, "_load", return_value=fake):
            out = align.refine(words, audio, 16000, Config(use_alignment=True))
        self.assertEqual(out[1], words[1])  # kept whisper's timing
        self.assertAlmostEqual(out[0].start, 0.02)
        self.assertAlmostEqual(out[2].start, 0.12)


class TestWordBoundariesFollowTheSound(unittest.TestCase):
    """Whisper gave the end of "control" to the "it" after it, so she sang
    "control" in a hurry; align.boundaries moves only the line between them."""

    def _run(self, words, measured, **cfg):
        import unittest.mock

        import numpy as np

        from teto_relay import align
        from teto_relay.config import Config

        audio = np.zeros(16000, dtype=np.float32)
        with unittest.mock.patch.object(align, "_measure", return_value=measured):
            return align.boundaries(words, audio, 16000, Config(**cfg))

    def test_line_moves_to_where_the_sound_changes(self):
        from teto_relay.stt import Word

        words = [Word("control", 0.80, 1.14), Word("it", 1.14, 1.56)]
        measured = [Word("control", 0.85, 1.22), Word("it", 1.26, 1.40)]
        out = self._run(words, measured)
        self.assertAlmostEqual(out[0].end, 1.24)
        self.assertAlmostEqual(out[1].start, 1.24)
        # Where the phrase starts and stops stays whisper's.
        self.assertEqual((out[0].start, out[1].end), (0.80, 1.56))

    def test_a_pause_between_words_is_left_alone(self):
        from teto_relay.stt import Word

        words = [Word("stars", 1.0, 1.3), Word("and", 1.6, 1.9)]
        measured = [Word("stars", 1.0, 1.5), Word("and", 1.7, 1.9)]
        self.assertEqual(self._run(words, measured), words)

    def test_a_lost_aligner_moves_nothing(self):
        from teto_relay.stt import Word

        words = [Word("i", 0.2, 0.38), Word("want", 0.38, 0.58), Word("to", 0.58, 0.8)]
        # Too far, and one that would leave "want" no time at all.
        far = [Word("i", 0.2, 0.9), Word("want", 0.95, 1.0), Word("to", 1.0, 1.1)]
        self.assertEqual(self._run(words, far), words)
        squeezed = [Word("i", 0.2, 0.55), Word("want", 0.57, 0.58), Word("to", 0.6, 0.8)]
        out = self._run(words, squeezed)
        # i/want would leave "want" 0.02 s: kept. want/to is sensible: moved.
        self.assertEqual((out[0].end, out[1].start), (0.38, 0.38))
        self.assertAlmostEqual(out[1].end, 0.59)

    def test_unplaced_words_and_the_switch(self):
        from teto_relay.stt import Word

        words = [Word("hello", 0.0, 0.5), Word("会議", 0.5, 1.0)]
        self.assertEqual(self._run(words, [Word("hello", 0.0, 0.6), words[1]]), words)
        moved = [Word("hello", 0.0, 0.6), Word("there", 0.6, 1.0)]
        plain = [Word("hello", 0.0, 0.5), Word("there", 0.5, 1.0)]
        self.assertEqual(self._run(plain, moved, align_boundaries=False), plain)
        self.assertEqual(self._run(plain, moved, use_alignment=True), plain)
        self.assertNotEqual(self._run(plain, moved), plain)


class TestAlignerRunsWhileWhisperListens(unittest.TestCase):
    """align.Pending: the aligner's word-free half starts with the phrase."""

    def test_result_and_errors_come_back_from_the_thread(self):
        import threading
        import unittest.mock

        from teto_relay import align
        from teto_relay.config import Config

        threads = []

        def fake(audio, cfg):
            threads.append(threading.current_thread().name)
            return "emission"

        with unittest.mock.patch.object(align, "_emission", side_effect=fake):
            self.assertEqual(align.Pending([0.0], Config()).result(), "emission")
            self.assertEqual(align.Pending([0.0], Config(), start=False).result(), "emission")
        self.assertEqual(threads[0], "aligner")
        self.assertEqual(threads[1], threading.current_thread().name)

        with unittest.mock.patch.object(align, "_emission", side_effect=RuntimeError("no model")):
            pending = align.Pending([0.0], Config())
            with self.assertRaises(RuntimeError):
                pending.result()


class TestTheProcessEndsWhenClosed(unittest.TestCase):
    """pythonnet's .NET shutdown kept TetoRelay.exe alive for over a minute
    after the window closed; with OpenUtau loaded the process ends directly."""

    def test_without_dotnet_it_is_a_normal_exit(self):
        import unittest.mock

        from teto_relay import dotnet

        with unittest.mock.patch.object(dotnet, "_started", False):
            with self.assertRaises(SystemExit) as caught:
                dotnet.leave(3)
        self.assertEqual(caught.exception.code, 3)

    def test_with_dotnet_it_skips_the_shutdown_and_keeps_the_code(self):
        import unittest.mock

        from teto_relay import dotnet

        with unittest.mock.patch.object(dotnet, "_started", True),                 unittest.mock.patch.object(dotnet, "_flush") as flush,                 unittest.mock.patch.object(dotnet.os, "_exit") as exit_:
            dotnet.leave(2)
            flush.assert_called_once()
            exit_.assert_called_once_with(2)

            exit_.reset_mock()
            with unittest.mock.patch.object(dotnet, "_failed", False):
                dotnet._leave_at_exit()
            exit_.assert_called_once_with(0)
            exit_.reset_mock()
            with unittest.mock.patch.object(dotnet, "_failed", True):
                dotnet._leave_at_exit()
            exit_.assert_called_once_with(1)

    def test_entry_points_leave_through_it(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("teto_relay/__main__.py", "packaging/launcher.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn("leave(main(", text, name)


class TestInstallerModels(unittest.TestCase):
    """The installer downloads models into models/ (installer.iss); the app
    must find them there, and both must name the same pinned files."""

    def test_whisper_uses_the_installed_copy_when_whole(self):
        from teto_relay.stt import whisper_source

        with _TempHome() as home:
            folder = home / "models" / "faster-whisper-base"
            folder.mkdir(parents=True)
            for part in ("model.bin", "config.json", "tokenizer.json"):
                (folder / part).write_text("x")
            self.assertEqual(whisper_source("base"), "base")  # vocabulary.txt missing
            (folder / "vocabulary.txt").write_text("x")
            self.assertEqual(whisper_source("base"), str(folder))
            self.assertEqual(whisper_source("small"), "small")

    def test_thai_uses_the_installed_copy_and_checks_it(self):
        import hashlib
        import unittest.mock

        from teto_relay import thai_asr

        with _TempHome() as home:
            folder = home / "models" / thai_asr.LOCAL_FOLDER
            folder.mkdir(parents=True)
            files = {}
            for role, (name, _) in thai_asr.MODEL["files"].items():
                body = f"{role} weights".encode()
                (folder / Path(name).name).write_bytes(body)
                files[role] = (name, hashlib.sha256(body).hexdigest())
            model = {**thai_asr.MODEL, "files": files}
            with unittest.mock.patch.object(thai_asr, "MODEL", model),                     unittest.mock.patch.object(thai_asr, "_download") as download:
                got = thai_asr.model_files()
                download.assert_not_called()
                self.assertEqual(got["vocab"], folder / "vocab.json")
                # Damaged: fetched again rather than used.
                (folder / "vocab.json").write_bytes(b"broken")
                (folder / "vocab.json.teto-relay-verified").unlink()
                thai_asr.model_files()
                download.assert_called_once()

    def test_the_installer_pins_what_the_app_expects(self):
        from teto_relay import thai_asr
        from teto_relay.config import Config

        iss = (Path(__file__).resolve().parent.parent / "packaging" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn(f"{thai_asr.MODEL['repo']}/resolve/{thai_asr.MODEL['revision']}/", iss)
        for name, digest in thai_asr.MODEL["files"].values():
            self.assertIn(digest, iss, name)
            self.assertIn(Path(name).name, iss)
        self.assertIn(f"{thai_asr.LOCAL_FOLDER}\\", iss)
        # The speech model it downloads is the one the app starts with.
        self.assertIn(f"faster-whisper-{Config().whisper_model}/resolve/", iss)
        self.assertIn(f"faster-whisper-{Config().whisper_model}\model.bin", iss)
        self.assertIn(r"\.cache\torch\hub\checkpoints\model.pt", iss)
        # Every extra is ticked: none is marked unchecked.
        self.assertNotIn("unchecked", iss)


class TestPushToTalkKeepsEveryPhrase(unittest.TestCase):
    """P2-4: two phrases finished between audio frames are both delivered."""

    def test_quick_release_press_release(self):
        import numpy as np

        from teto_relay.capture import PushToTalkChunker
        from teto_relay.config import Config

        cfg = Config(min_chunk_ms=20, frame_ms=20)
        chunker = PushToTalkChunker(cfg)
        frame = np.full(cfg.frame_samples, 0.1, dtype=np.float32)
        delivered = []
        for label in ("first", "second"):
            chunker.start()
            for _ in range(5):
                out = chunker.push(frame)
                if out is not None:
                    delivered.append(out)
            chunker.stop()
        chunker.start()  # a third press before any further frame arrives
        while (out := chunker.push(frame)) is not None:
            delivered.append(out)
        chunker.stop()
        while (out := chunker.flush()) is not None:
            delivered.append(out)
        self.assertEqual(len(delivered), 3)


class TestPronunciationsAreCached(unittest.TestCase):
    """P2-6: the file is parsed once, and again only when it changes."""

    def test_parsed_once_until_changed(self):
        import json
        import os
        import tempfile
        import unittest.mock

        from teto_relay import pronunciations as pron

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps({"respellings": {"foo": "fu"}}), encoding="utf-8")
            with unittest.mock.patch.object(pron.json, "loads", wraps=json.loads) as parse:
                self.assertEqual(pron.load(path)["foo"], "fu")
                self.assertEqual(pron.load_hints(path).get("foo"), None)
                self.assertEqual(parse.call_count, 1)
                path.write_text(json.dumps({"respellings": {"foo": "fooo"}}), encoding="utf-8")
                stat = path.stat()
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
                self.assertEqual(pron.load(path)["foo"], "fooo")
                self.assertEqual(parse.call_count, 2)


class TestBankPitchCache(unittest.TestCase):
    """P2-7: the measured pitch belongs to a folder, not to a short key."""

    def test_two_folders_with_one_key_are_measured_separately(self):
        import json
        import unittest.mock

        from teto_relay import voicebank
        from teto_relay.config import Config

        with _TempHome() as home:
            a = voicebank.Voicebank(key="teto", name="A", root=home / "a")
            b = voicebank.Voicebank(key="teto", name="B", root=home / "b")
            cache = home / ".openutau-host" / "bank_pitch.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({
                "v2:" + str((home / "a").resolve()): 61.0,
                # An estimate from before sampling was spread across the bank
                # (unversioned key) is measured again, not reused.
                str((home / "b").resolve()): 54.0,
            }), encoding="utf-8")
            self.assertEqual(voicebank.estimate_pitch(a, Config()), 61.0)
            with unittest.mock.patch.object(voicebank, "log"):
                self.assertEqual(voicebank.estimate_pitch(b, Config()), 60.0)  # nothing to measure


class TestZipBombs(unittest.TestCase):
    """P2-8: a zip that claims an enormous unpacked size is refused unopened."""

    def test_refused_before_extracting(self):
        import io
        import tempfile
        import unittest.mock
        import zipfile

        from teto_relay import library

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("bank/oto.ini", "a.wav=a,0,0,0,0,0\n")
            z.writestr("bank/a.wav", b"\0" * 100_000)
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(library, "MAX_UNPACKED", 50_000):
            with self.assertRaises(ValueError) as caught:
                library.install_voicebank(buffer.getvalue(), "bank.zip", Path(tmp))
            self.assertIn("unpack", str(caught.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_not_a_zip(self):
        import tempfile

        from teto_relay import library

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as caught:
                library.install_voicebank(b"not a zip", "bank.zip", Path(tmp))
        self.assertIn("not a valid", str(caught.exception))


class TestTickSpacing(unittest.TestCase):
    """P2-10: gaps survive rounding to ticks; legato notes may touch."""

    def blocks(self, notes, **cfg):
        from teto_relay.config import Config
        from teto_relay.ustx import build_project
        from teto_relay.voicebank import Voicebank

        bank = Voicebank(key="english", name="Teto", root=Path("/x/Teto"), flavour="en-cvvc")
        return build_project(notes, bank, Config(**cfg))["voice_parts"][0]["notes"]

    def test_a_sub_tick_gap_does_not_round_to_touching(self):
        from teto_relay.notes import Note

        # 0.0006 s is about half a tick: rounded independently these touch.
        notes = [Note("a", 0.0, 0.2504, 60), Note("b", 0.251, 0.5, 60)]
        a, b = self.blocks(notes)
        self.assertGreaterEqual(b["position"], a["position"] + a["duration"] + 1)

    def test_overlapping_notes_never_overlap_in_the_file(self):
        from teto_relay.notes import Note

        a, b = self.blocks([Note("a", 0.0, 0.4, 60), Note("b", 0.3, 0.6, 60)])
        self.assertGreater(b["position"], a["position"] + a["duration"] - 1)

    def test_legato_notes_may_touch(self):
        from teto_relay.notes import Note

        notes = [Note("か", 0.0, 0.2, 60), Note("さ", 0.2, 0.4, 60, legato=True)]
        a, b = self.blocks(notes)
        self.assertEqual(b["position"], a["position"] + a["duration"])


class TestPersistentOutput(unittest.TestCase):
    """P2-12: persistent_output keeps one stream for many phrases."""

    def test_one_stream_for_several_files(self):
        import queue
        import tempfile
        import types
        import unittest.mock

        import numpy as np
        import soundfile as sf

        from teto_relay import playback
        from teto_relay.config import Config

        streams = []

        class FakeStream:
            def __init__(self, **kwargs):
                self.kwargs, self.written, self.closed = kwargs, [], False
                streams.append(self)

            def start(self):
                pass

            def write(self, block):
                self.written.append(block.copy())

            def stop(self):
                pass

            def close(self):
                self.closed = True

        fake_sd = types.SimpleNamespace(OutputStream=FakeStream, stop=lambda: None,
                                        query_devices=lambda *a: {"default_samplerate": 44100,
                                                                  "max_output_channels": 2})
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for i in range(2):
                path = Path(tmp) / f"relay_{i}.wav"
                sf.write(path, np.full(3000, 0.25 * (i + 1), dtype=np.float32), 44100)
                files.append(path)
            q = queue.Queue()
            with unittest.mock.patch.object(playback, "sd", lambda: fake_sd):
                player = playback.Player(Config(persistent_output=True), q, device=3)
                player.start()
                for path in files:
                    q.put(path)
                q.put(None)
                player.join(timeout=10)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].kwargs["device"], 3)
        written = np.concatenate(streams[0].written)
        self.assertEqual(written.shape, (6000, 2))
        self.assertAlmostEqual(float(written[-1, 0]), 0.5, places=3)
        self.assertTrue(streams[0].closed)


class TestSungStyle(unittest.TestCase):
    """singing_style "sung": in a key, steadier, vibrato, held ending."""

    def notes(self, **cfg_values):
        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Word

        cfg = Config(auto_octave=False, **cfg_values)
        cfg.validate()
        words = [Word("la", 0.0, 0.2), Word("la", 0.3, 0.9), Word("la", 1.0, 1.2)]
        # 61.4, 63.6, 66.3 in MIDI: between semitones, so snapping shows.
        track = _flat_track()
        import numpy as np

        from teto_relay import pitch as pitch_mod

        f0 = np.array(track.f0)
        for (t0, t1), midi in zip([(0.0, 0.2), (0.3, 0.9), (1.0, 1.2)], (61.4, 63.6, 66.3)):
            f0[(track.times >= t0) & (track.times < t1)] = float(pitch_mod.midi_to_hz(midi))
        track.f0 = f0
        state = {}
        return build_notes(words, track, cfg, singing_state=state), state

    def test_speech_style_is_unchanged(self):
        notes, _ = self.notes(legato=False)
        self.assertEqual([n.tone for n in notes], [61, 64, 66])
        self.assertTrue(all(n.vibrato is None and not n.legato for n in notes))

    def test_sung_notes_are_in_the_chosen_key(self):
        from teto_relay.singing import SCALES

        notes, state = self.notes(singing_style="sung", scale_key="C", final_hold_seconds=0.0)
        self.assertEqual(state["tonic"], 0)
        for n in notes:
            self.assertIn(n.tone % 12, SCALES["major"])
        # 61.4 -> D, 63.6 -> E, 66.3 -> G (0.7 away, nearer than F at 1.3).
        self.assertEqual([n.tone for n in notes], [62, 64, 67])

    def test_vibrato_only_on_long_notes_and_the_last_note_is_held(self):
        spoken, _ = self.notes()
        notes, _ = self.notes(singing_style="sung", vibrato_min_seconds=0.5,
                              final_hold_seconds=0.4)
        self.assertIsNone(notes[0].vibrato)
        self.assertIsNotNone(notes[1].vibrato)
        self.assertAlmostEqual(notes[-1].duration, spoken[-1].duration + 0.4, places=6)
        self.assertIsNotNone(notes[-1].vibrato)  # 0.6 s once held

    def test_the_key_is_held_between_phrases(self):
        from teto_relay.singing import choose_key

        c_major = [60, 62, 64, 65, 67]
        self.assertEqual(choose_key(c_major, "major"), 0)
        # A phrase that fits G a little better still stays in C...
        slightly_g = [67, 69, 71, 66.2]
        self.assertEqual(choose_key(slightly_g, "major", held=0), 0)
        # ...but one that clearly does not fit moves.
        self.assertNotEqual(choose_key([61, 63, 66, 68, 70], "major", held=0), 0)

    def test_contour_is_narrowed(self):
        from teto_relay.config import Config
        from teto_relay.notes import Note
        from teto_relay.singing import musicalize

        # No final hold: a 0.2 s last note is too short for the phrase-end
        # fall (tested separately), so only the narrowing shows.
        cfg = Config(singing_style="sung", sung_contour_amount=0.5, scale_key="C",
                     final_hold_seconds=0.0)
        note = Note("la", 0.0, 0.2, 60, contour=[(0.0, 100.0), (100.0, -60.0)],
                    detected_midi=60.0)
        musicalize([note], cfg, {})
        # Narrowed to half, then the phrase-start scoop drawn on top of it.
        from teto_relay.performance import SCOOP_CENTS, SCOOP_HOLD_MS, SCOOP_MS, _add

        expected = _add([(0.0, 50.0), (100.0, -30.0)],
                        [(0.0, -SCOOP_CENTS), (SCOOP_HOLD_MS, -SCOOP_CENTS), (SCOOP_MS, 0.0)])
        self.assertEqual(note.contour, expected)

    def test_scale_key_is_validated(self):
        from teto_relay.config import Config, ConfigError

        self.assertEqual(Config.from_dict({"scale_key": "Bb"}).scale_key, "Bb")
        with self.assertRaises(ConfigError):
            Config.from_dict({"scale_key": "H"})

    def test_vibrato_reaches_the_ustx_and_the_tone_renderer(self):
        import tempfile

        import soundfile as sf

        from teto_relay.config import Config
        from teto_relay.render.null import NullRenderer
        from teto_relay.ustx import build_project, write_ustx
        from teto_relay.voicebank import Voicebank

        notes, _ = self.notes(singing_style="sung")
        bank = Voicebank(key="english", name="Teto", root=Path("/x/Teto"), flavour="en-cvvc")
        block = build_project(notes, bank, Config())["voice_parts"][0]["notes"][1]
        # Vibrato on the long middle note, after a steady start.
        from teto_relay.performance import VIBRATO_MIN_DELAY_S

        self.assertGreater(block["vibrato"]["length"], 0.0)
        self.assertLessEqual(block["vibrato"]["length"],
                             100.0 * (1 - VIBRATO_MIN_DELAY_S / notes[1].duration) + 0.1)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ustx(notes, Path(tmp) / "v.ustx", bank, Config())
            wav = NullRenderer(Config()).render(path, Path(tmp) / "v.wav")
            audio, _ = sf.read(wav)
        self.assertGreater(len(audio), 0)


class TestLegato(unittest.TestCase):
    """legato: a phrase is sung connected; a pause of phrase_gap_ms is a rest."""

    def build(self, legato, pause=0.4):
        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Word

        cfg = Config(auto_octave=False, legato=legato)
        return build_notes([Word("hello", 0.0, 0.6), Word("teto", 0.6 + pause, 1.2 + pause)],
                           _flat_track(), cfg, japanese_lyrics=True)

    def test_on_by_default(self):
        from teto_relay.config import Config

        self.assertTrue(Config().legato)

    def test_words_said_without_a_pause_touch(self):
        notes = self.build(True, pause=0.1)
        self.assertTrue(all(n.legato for n in notes[1:]))
        for a, b in zip(notes, notes[1:]):
            self.assertAlmostEqual(a.end, b.start)

    def test_off_every_note_has_a_gap(self):
        notes = self.build(False)
        for a, b in zip(notes, notes[1:]):
            self.assertLess(a.end, b.start)

    def test_on_morae_of_a_word_touch(self):
        notes = self.build(True)
        firsts = [n for n in notes if not n.legato]
        self.assertEqual(len(firsts), 2)  # one note per word starts a phrase
        for a, b in zip(notes, notes[1:]):
            if b.legato:
                self.assertAlmostEqual(a.end, b.start)
            else:
                self.assertLess(a.end, b.start)

    def test_touching_survives_into_the_ustx(self):
        from teto_relay.config import Config
        from teto_relay.ustx import build_project
        from teto_relay.voicebank import Voicebank

        notes = self.build(True)
        bank = Voicebank(key="tandoku", name="Teto", root=Path("/x/Teto"), flavour="ja-cv")
        blocks = build_project(notes, bank, Config())["voice_parts"][0]["notes"]
        for note, a, b in zip(notes[1:], blocks, blocks[1:]):
            end = a["position"] + a["duration"]
            if note.legato:
                self.assertEqual(b["position"], end)
            else:
                self.assertGreater(b["position"], end)


class TestMicTap(unittest.TestCase):
    def test_frames_go_to_the_tap_not_the_chunker(self):
        import queue
        import time
        import types
        import unittest.mock

        from teto_relay import capture
        from teto_relay.config import Config

        tapped = []

        class NoChunks:
            def push(self, frame):
                raise AssertionError("the chunker should not see frames")

            def flush(self):
                return None

        fake_sd = types.SimpleNamespace(InputStream=lambda **kw: _FakeInputStream(30, **kw))
        mic = capture.MicCapture(Config(), queue.Queue(), chunker=NoChunks(), tap=tapped.append)
        mic.STALL_SECONDS = 5
        with unittest.mock.patch.object(capture, "sd", lambda: fake_sd):
            mic.start()
            deadline = time.monotonic() + 5
            while len(tapped) < 30 and time.monotonic() < deadline:
                time.sleep(0.01)
            mic.stop()
            mic.join(timeout=5)
        self.assertEqual(len(tapped), 30)


class TestReviewFindings(unittest.TestCase):
    """Bugs found by the independent code review of this branch."""

    def test_a_relative_config_path_is_made_absolute(self):
        import contextlib
        import io
        import os
        import unittest.mock

        seen = {}
        with _TempHome() as home:
            (home / "my.json").write_text('{"transpose": 5}', encoding="utf-8")
            old = os.getcwd()
            os.chdir(home)
            try:
                with unittest.mock.patch("teto_relay.__main__._run",
                                         side_effect=lambda args, cfg: seen.update(path=args.config) or 0), \
                        contextlib.redirect_stderr(io.StringIO()):
                    from teto_relay.__main__ import main

                    self.assertEqual(main(["--config", "my.json"]), 0)
            finally:
                os.chdir(old)
        self.assertTrue(seen["path"].is_absolute())

    def test_a_moved_portable_folder_keeps_working(self):
        import os
        import shutil
        import tempfile
        import unittest.mock

        from teto_relay.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "usbA"), Path(tmp, "usbB")
            with unittest.mock.patch.dict(os.environ, {"TETO_RELAY_HOME": str(a)}):
                Config(transpose=3).save(a / "config.json")
            shutil.move(str(a), str(b))
            with unittest.mock.patch.dict(os.environ, {"TETO_RELAY_HOME": str(b)}):
                cfg = Config.load(b / "config.json")
            self.assertEqual(Path(cfg.out_dir), (b / "out").resolve())
            self.assertEqual(Path(cfg.log_file), (b / "teto-relay.log").resolve())
            self.assertEqual(cfg.transpose, 3)

    def test_very_large_numbers_are_read_digit_by_digit(self):
        from teto_relay.stt import clean_lyric

        self.assertEqual(clean_lyric("5551234567890"),
                         "five five five one two three four five six seven eight nine zero")
        self.assertTrue(clean_lyric("2,000,000,000,000").startswith("two zero"))
        self.assertTrue(clean_lyric("1000000000000.5").endswith("point five"))

    def test_an_unwritable_data_folder_does_not_stop_the_app_from_starting(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp, "afile")
            blocker.write_text("", encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("TORCH_HOME", "HF_HOME", "HUGGINGFACE_HUB_CACHE")}
            env["TETO_RELAY_HOME"] = str(blocker / "data")
            out = subprocess.run([sys.executable, "-m", "teto_relay", "--version"], cwd=ROOT,
                                 env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Teto Relay", out.stdout)

    def fake_output(self, gain):
        import types
        import unittest.mock

        from teto_relay import playback

        streams = []

        class FakeStream:
            def __init__(self, **kw):
                self.frames = []
                streams.append(self)

            def start(self):
                pass

            def write(self, frames):
                self.frames.append(frames.copy())

            def stop(self):
                pass

            def close(self):
                pass

        fake_sd = types.SimpleNamespace(OutputStream=FakeStream, query_devices=lambda *a: {
            "default_samplerate": 16000, "max_output_channels": 1})
        patch = unittest.mock.patch.object(playback, "sd", lambda: fake_sd)
        patch.start()
        self.addCleanup(patch.stop)
        return playback.StreamOutput(device=0, gain=gain), streams

    def test_tray_retry_reads_the_config_again(self):
        import unittest.mock

        from teto_relay import tray
        from teto_relay.config import Config
        from teto_relay.voicebank import VoicebankError

        fixed = Config(transpose=9)
        app = tray.TrayApp(Config(), reload=lambda: fixed)
        with unittest.mock.patch.object(tray, "TetoRelay", side_effect=VoicebankError("x")):
            app.start()
        built = []
        with unittest.mock.patch.object(tray, "TetoRelay",
                                        side_effect=lambda cfg: built.append(cfg) or unittest.mock.Mock(
                                            paused=False, health=lambda: {"problems": []})):
            app.start()
        self.assertIs(built[0], fixed)
        self.assertEqual(app.state, "live")


class TestHardwareFindings(unittest.TestCase):
    """Found running the branch on the target PC (Windows, GTX 1060)."""

    def test_an_unsupported_whisper_compute_type_falls_back(self):
        # float16 on a Pascal GPU: CTranslate2 raised ValueError when the
        # model loaded, so every utterance failed while the relay said running.
        import types
        import unittest.mock

        from teto_relay.config import Config
        from teto_relay.stt import Transcriber

        fake_ct2 = types.SimpleNamespace(
            get_supported_compute_types=lambda device: {"int8", "int8_float32", "float32"}
        )
        model = unittest.mock.MagicMock()
        fake_fw = types.SimpleNamespace(WhisperModel=model)
        cfg = Config()
        cfg.whisper_device, cfg.whisper_compute_type = "cuda", "float16"
        with unittest.mock.patch.dict(sys.modules, {"ctranslate2": fake_ct2, "faster_whisper": fake_fw}), \
                self.assertLogs("teto_relay.stt", "WARNING") as logs:
            Transcriber(cfg).load()
        self.assertEqual(model.call_args.kwargs["compute_type"], "int8")
        self.assertIn("float16", "\n".join(logs.output))

    def test_touching_notes_are_phonemized_as_separate_groups(self):
        # legato on OpenUtau: 18 touching morae came back as 7 phonemes,
        # because each run of touching notes was sent as one group and the
        # phonemizer sings a group's first lyric across all of it.
        import types

        from teto_relay.render.openutau import phonemizer_groups

        def note(lyric, position, duration=240):
            return types.SimpleNamespace(lyric=lyric, position=position, duration=duration)

        touching = [note("か", 0), note("さ", 240), note("ね", 480)]
        self.assertEqual([len(g) for g in phonemizer_groups(touching)], [1, 1, 1])
        gapped = [note("か", 0), note("さ", 250)]
        self.assertEqual([len(g) for g in phonemizer_groups(gapped)], [1, 1])
        # OpenUtau's own extension notes ("+", "+~") do belong to the note before.
        extended = [note("か", 0), note("+~", 240), note("さ", 480)]
        self.assertEqual([[n.lyric for n in g] for g in phonemizer_groups(extended)],
                         [["か", "+~"], ["さ"]])

    def test_pitch_contour_is_written_in_openutau_units(self):
        # OpenUtau's pitch points are tenths of a semitone (measured: y=30
        # sings +3 semitones, y=100 sings +10). The contour went out in cents,
        # so a "hello" shown as tone 62 was sung at MIDI 86.7.
        import tempfile

        import numpy as np
        import soundfile as sf

        from teto_relay.config import Config
        from teto_relay.notes import Note
        from teto_relay.render.null import NullRenderer
        from teto_relay.ustx import build_project, write_ustx
        from teto_relay.voicebank import Voicebank

        note = Note(lyric="a", start=0.0, end=1.0, tone=60,
                    contour=[(0.0, 300.0), (1000.0, 300.0)])  # +3 semitones, in cents
        bank = Voicebank(key="tandoku", name="Teto", root=Path("/x/Teto"), flavour="ja-cv")
        block = build_project([note], bank, Config())["voice_parts"][0]["notes"][0]
        self.assertEqual({p["y"] for p in block["pitch"]["data"]}, {30.0})

        # The tone renderer reads the unit back: +3 semitones, not +30 or +0.3.
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ustx([note], Path(tmp) / "p.ustx", bank, Config())
            wav = NullRenderer(Config()).render(path, Path(tmp) / "p.wav")
            audio, rate = sf.read(wav)
        audio = audio if audio.ndim == 1 else audio.mean(axis=1)
        middle = audio[len(audio) // 4: 3 * len(audio) // 4]
        spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle))))
        peak = np.fft.rfftfreq(len(middle), 1 / rate)[np.argmax(spectrum)]
        midi = 69 + 12 * np.log2(peak / 440.0)
        self.assertAlmostEqual(midi, 63.0, delta=0.3)

    def test_a_lone_long_vowel_mark_joins_the_word_before(self):
        # Whisper heard ムーリー as "ム ー リ ー"; each ー became a note with no
        # sample. Joined to the kana before, it is sung as the held vowel.
        import types

        import numpy as np

        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Transcriber

        def word(text, start, end):
            return types.SimpleNamespace(word=text, start=start, end=end)

        segment = types.SimpleNamespace(
            no_speech_prob=0.0, avg_logprob=-0.1,
            words=[word("ム", 0.0, 0.2), word("ー", 0.2, 0.4), word("リ", 0.4, 0.6), word("ー", 0.6, 0.8)])
        transcriber = Transcriber(Config(language="ja"))
        transcriber._model = types.SimpleNamespace(transcribe=lambda *a, **k: ([segment], None))
        words = transcriber.transcribe(np.zeros(16000, np.float32), 16000)
        self.assertEqual([(w.text, w.start, w.end) for w in words], [("ムー", 0.0, 0.4), ("リー", 0.4, 0.8)])

        # Small kana too: whisper split ミュウリ into ミ ュ ウ リ, and ュ alone
        # was a note with no sample.
        segment.words = [word("ミ", 0.0, 0.1), word("ュ", 0.1, 0.2), word("ウ", 0.2, 0.4), word("リ", 0.4, 0.6)]
        self.assertEqual([w.text for w in transcriber.transcribe(np.zeros(16000, np.float32), 16000)],
                         ["ミュ", "ウ", "リ"])

        cfg = Config(auto_octave=False, language="ja")
        notes = build_notes(words, _flat_track(), cfg, japanese_lyrics=True)
        self.assertNotIn("ー", [n.lyric for n in notes])
        # Sung as the held vowel: an extension of む and り, not a new sample.
        self.assertEqual([n.lyric for n in notes], ["む", "+", "り", "+"])

    def test_start_only_settings_restart_the_relay_once(self):
        # The user: changing a setting should just apply. Settings read only
        # at start now restart the relay behind the scenes - once, however
        # many are changed in a row.
        import time

        from teto_relay.config import Config
        from teto_relay.webui import Controller

        controller = Controller(Config())
        calls = []
        controller.relay = object()                      # "running"
        controller.stop = lambda: calls.append("stop") or setattr(controller, "relay", None)
        controller.start = lambda: calls.append("start") or setattr(controller, "relay", object())
        for _ in range(3):
            controller.restart_soon(delay=0.05)
        self.assertTrue(controller.restarting)
        deadline = time.monotonic() + 3
        while controller.restarting and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(calls, ["stop", "start"])
        self.assertFalse(controller.restarting)
        self.assertTrue(controller.running)

        # A restart that fails says why instead of leaving it half-started.
        def broken():
            raise RuntimeError("no microphone")
        controller.start = broken
        controller.restart_soon(delay=0.01)
        deadline = time.monotonic() + 3
        while controller.restarting and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("no microphone", controller.status()["restart_error"])

    def test_morae_start_at_their_measured_vowels_and_stay_connected(self):
        # Senbonzakura's pronunciation was out of time: morae were spread
        # evenly inside whisper's rough words. Timed from the aligner, each
        # note starts at its vowel; moving a word's start must not open a
        # hole before it (it did: dropouts went from 1% to 9%).
        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Word

        measured = {"せ": 0.08, "ん": 0.30, "ぼ": 0.36, "よ": 0.95, "る": 1.20}
        timer = lambda morae: [measured.get(m) for m in morae]  # noqa: E731
        words = [Word("千本", 0.0, 0.8), Word("夜", 0.85, 1.5)]
        notes = build_notes(words, _flat_track(), Config(auto_octave=False, language="ja"),
                            japanese_lyrics=True, mora_timer=timer)
        starts = {n.lyric: round(n.start, 2) for n in notes}
        self.assertEqual([n.lyric for n in notes], ["せ", "ん", "ぼ", "ん", "よ", "る"])
        self.assertEqual((starts["せ"], starts["よ"], starts["る"]), (0.08, 0.95, 1.2))
        for a, b in zip(notes, notes[1:]):
            self.assertAlmostEqual(a.end, b.start, places=6, msg=f"{a.lyric} {b.lyric}")

    def test_the_aligner_loads_memory_mapped(self):
        # torchaudio's loader needs ~2.5 GB of RAM at once; the 8 GB target PC
        # never had it free, so the aligner had never run there.
        import unittest.mock

        from teto_relay import align
        from teto_relay.config import Config

        if __import__("importlib.util").util.find_spec("torch") is None:
            self.skipTest("needs torch")
        with unittest.mock.patch.object(align, "_bundle", None), \
                unittest.mock.patch.object(align, "_checkpoint", return_value=Path("model.pt")), \
                unittest.mock.patch.object(align, "_available_memory", return_value=600_000_000), \
                unittest.mock.patch.object(align, "_load_mapped", return_value="mapped") as mapped:
            model, *_ = align._load(Config(align_device="cpu"))
        self.assertEqual(model, "mapped")
        mapped.assert_called_once()

    def test_a_held_note_lasts_as_long_as_it_is_sung(self):
        # Senbonzakura: 桜's ら was held into よる, whisper ended 桜 early, the
        # gap looked like a pause, and Teto stopped while the singer went on;
        # then its last mora was capped at 0.25 s besides.
        import numpy as np

        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Word

        rate = 16000
        t = np.arange(int(3.0 * rate)) / rate
        voice = 0.3 * np.sin(2 * np.pi * 220 * t)
        voice[(t > 1.6) & (t < 2.0)] = 0.0              # a real pause before 夜
        audio = voice.astype(np.float32)
        words = [Word("桜", 0.0, 0.6), Word("夜", 2.0, 2.4)]  # whisper ended 桜 at 0.6; sung to 1.6
        notes = build_notes(words, _flat_track(seconds=3.0), Config(auto_octave=False, language="ja"),
                            japanese_lyrics=True, audio=audio)
        sakura = [n for n in notes if n.start < 1.9]
        self.assertEqual("".join(n.lyric for n in sakura), "さくら")
        self.assertGreaterEqual(sakura[-1].end, 1.55)   # ら held as long as it was sung
        self.assertLess(sakura[-1].end, 2.0)            # but the real pause is still a rest
        self.assertFalse(notes[len(sakura)].legato)

    def test_breath_and_voicing_touch_only_each_phrase_end(self):
        # With points only at phrase ends, voic and brec ramped across the
        # whole next phrase: it was sung half whispered.
        import numpy as np

        from teto_relay.config import Config
        from teto_relay.notes import Note
        from teto_relay.performance import expression_curves

        notes = [Note("a", 0.0, 1.0, 60), Note("b", 2.0, 3.0, 60)]  # two phrases
        curves = expression_curves(notes, np.zeros(48000, np.float32), 16000, Config())
        for abbr, neutral in (("voic", 100.0), ("brec", 0.0)):
            xs, ys = zip(*curves[abbr])
            for x in (2.0, 2.3, 2.6):   # the second phrase, before its tail
                self.assertAlmostEqual(float(np.interp(x, xs, ys)), neutral, msg=f"{abbr} at {x}")

    def test_sung_phrases_are_told_from_spoken_ones(self):
        import numpy as np

        from teto_relay import pitch as pitch_mod

        n = 300
        times = np.arange(n) / 100.0
        held = np.repeat([220.0, 247.0, 262.0], 100)                  # three held notes
        sliding = 220.0 * 2 ** (np.sin(np.arange(n) / 9.0) * 3 / 12)   # speech-like glides
        for f0, sung in ((held, True), (sliding, False)):
            track = pitch_mod.F0Track(times=times, f0=f0, voiced=np.ones(n, bool), sample_rate=16000)
            share = pitch_mod.held_share(track)
            self.assertEqual(share >= pitch_mod.SUNG_HELD_SHARE, sung, share)

    def test_doubling_only_on_sung_phrases_by_default(self):
        import tempfile

        import numpy as np
        import soundfile as sf

        from teto_relay.app import Job, TetoRelay
        from teto_relay.config import Config

        tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(22050) / 22050)).astype(np.float32)
        with tempfile.TemporaryDirectory() as tmp:
            def doubled(sung, **cfg):
                relay = TetoRelay.__new__(TetoRelay)
                relay.cfg = Config(**{"double_voice": 0.5, **cfg})
                path = Path(tmp) / "x.wav"
                sf.write(path, tone, 22050)
                relay.finish_render(Job(captured_at=0.0, text="", wav_path=path, sung=sung))
                return not np.allclose(sf.read(path, dtype="float32")[0], tone, atol=1e-3)

            self.assertTrue(doubled(True))
            self.assertFalse(doubled(False))
            self.assertTrue(doubled(False, double_when="always"))
            self.assertFalse(doubled(True, double_voice=0.0))
            self.assertFalse(doubled(True, double_when="off"))  # the panel's Off switch

    def test_japanese_is_read_in_context(self):
        # Senbonzakura as whisper splits it: 紛|レ was sung ふん れ and 届|カ
        # とどけ か; 君 alone was くん. Read as a phrase, then shared back out.
        from teto_relay.translit import japanese_to_kana, phrase_kana

        words = ["千本桜", "夜", "ニ", "紛", "レ", "君", "ノ", "声", "モ", "届", "カ", "ナイヨ"]
        self.assertEqual(phrase_kana(words), ["せんぼんざくら", "よる", "に", "まぎ", "れ", "きみ", "の",
                                              "こえ", "も", "とど", "か", "ないよ"])
        self.assertEqual(phrase_kana(["kasane", "テト"]), [None, "てと"])  # Latin: read on its own
        for text, reading in [("今日は晴れ", "きょうははれ"), ("雨の日", "あめのひ"), ("月が", "つきが"),
                              ("人は", "ひとは"), ("愛してる", "あいしてる"), ("明日", "あした")]:
            self.assertEqual(japanese_to_kana(text), reading)

    def test_song_lyrics_reach_whisper_after_the_vocabulary(self):
        import types

        import numpy as np

        from teto_relay.config import Config
        from teto_relay.stt import Transcriber

        seen = {}

        def transcribe(audio, **kwargs):
            seen.update(kwargs)
            return [], None

        cfg = Config(initial_prompt="Kasane Teto.", lyrics_hint="千本桜 夜ニ紛レ")
        t = Transcriber(cfg)
        t._model = types.SimpleNamespace(transcribe=transcribe)
        t.transcribe(np.zeros(16000, np.float32), 16000)
        self.assertEqual(seen["initial_prompt"], "Kasane Teto. 千本桜 夜ニ紛レ")
        cfg.lyrics_hint, cfg.initial_prompt = "", ""
        t.transcribe(np.zeros(16000, np.float32), 16000)
        self.assertIsNone(seen["initial_prompt"])

    def test_whisper_output_is_bounded_by_the_audio_length(self):
        # A repetitive Japanese phrase made whisper loop to its 448-token
        # limit at every fallback temperature: 17.7 s for 2 s of speech.
        import types

        import numpy as np

        from teto_relay.config import Config
        from teto_relay.stt import Transcriber

        seen = {}

        def transcribe(audio, **kwargs):
            seen.update(kwargs)
            return [], None

        transcriber = Transcriber(Config())
        transcriber._model = types.SimpleNamespace(transcribe=transcribe)
        transcriber.transcribe(np.zeros(2 * 16000, np.float32), 16000)
        two_seconds = seen["max_new_tokens"]
        transcriber.transcribe(np.zeros(10 * 16000, np.float32), 16000)
        # Room for fast speech (well over 10 tokens a second)...
        self.assertGreaterEqual(two_seconds, 30)
        self.assertGreater(seen["max_new_tokens"], two_seconds)
        # ...but nowhere near the 448 a runaway loop fills.
        self.assertLess(two_seconds, 100)

    def test_bank_pitch_is_sampled_across_the_whole_bank(self):
        # Miku's ARPAsing bank lists 25 consonants before its vowels; the first
        # 12 files read an octave low, so she was aimed an octave too low.
        import numpy as np
        import soundfile as sf

        from teto_relay.config import Config
        from teto_relay.voicebank import SubBank, Voicebank, estimate_pitch

        with _TempHome() as home:
            folder = home / "bank"
            folder.mkdir()
            t = np.arange(16000) / 16000
            for i in range(25):  # "consonants" first by name, an octave low
                sf.write(folder / f"a_{i:03d}.wav", 0.3 * np.sin(2 * np.pi * 110.0 * t), 16000)
            for i in range(25):  # the vowels: A3
                sf.write(folder / f"b_{i:03d}.wav", 0.3 * np.sin(2 * np.pi * 220.0 * t), 16000)
            bank = Voicebank(key="m", name="m", root=folder,
                             subbanks=[SubBank(name="m", path=folder, entry_count=50, sample_aliases=())])
            estimate = estimate_pitch(bank, Config(pitch_method="pyin"))
        self.assertAlmostEqual(estimate, 57.0, delta=0.5)

    def test_touching_notes_touch_exactly_in_ticks(self):
        # "to sing": rounding left "to" ending one tick before "sing", the
        # phonemizer took that for a rest ("u -" + "- sI"): a hard cut.
        from teto_relay.config import Config
        from teto_relay.notes import Note
        from teto_relay.ustx import build_project
        from teto_relay.voicebank import Voicebank

        cfg = Config()
        bank = Voicebank(key="english", name="Teto", root=Path("/x/Teto"), flavour="en-cvvc")
        tick = 1.0 / cfg.seconds_to_ticks(1.0)
        # Start and length both 0.4 of a tick past a whole tick: each rounds
        # down, and the next note's start (0.8 past) rounds up - a gap.
        start, length = 96.4 * tick, 100.4 * tick
        notes = [Note("used", 0.0, start, 60),
                 Note("to", start, start + length, 60, legato=True),
                 Note("sing", start + length, start + length + 0.5, 60, legato=True)]
        blocks = build_project(notes, bank, cfg)["voice_parts"][0]["notes"]
        for a, b in zip(blocks, blocks[1:]):
            self.assertEqual(a["position"] + a["duration"], b["position"])

    def test_doubling_lays_late_detuned_copies_without_clipping(self):
        import numpy as np

        from teto_relay.performance import DOUBLES, double_voice

        rate = 44100
        t = np.arange(rate) / rate
        voice = (0.9 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        self.assertIs(double_voice(voice, rate, 0.0), voice)          # off: untouched
        doubled = double_voice(voice, rate, 1.0)
        self.assertLessEqual(float(np.max(np.abs(doubled))), 1.0)     # never clips
        # Until the first copy arrives it is the lead alone, times one gain.
        first_copy = int(rate * min(d for _, d in DOUBLES) / 1000)
        gain = doubled[100] / voice[100]
        self.assertTrue(np.allclose(doubled[:first_copy], voice[:first_copy] * gain, atol=1e-5))
        self.assertFalse(np.allclose(doubled[rate // 2:], voice[rate // 2:], atol=0.05))

    def test_the_vowel_is_on_the_beat_and_the_consonant_before_it(self):
        # A voicebank sings a note's consonant before the note; the note must
        # start at the vowel, or every consonant is early.
        import numpy as np

        from teto_relay import pitch as pitch_mod
        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Word

        n = 200
        voiced = np.ones(n, bool)
        voiced[50:58] = False    # "stars": s-t unvoiced for 80 ms from 0.50 s
        track = pitch_mod.F0Track(times=np.arange(n) / 100.0, f0=np.full(n, 220.0), voiced=voiced,
                                  sample_rate=16000)
        words = [Word("look", 0.0, 0.4), Word("stars", 0.5, 0.9)]
        # The recording has sound throughout (the "st" is noise, not silence).
        audio = (0.1 * np.sin(2 * np.pi * 220 * np.arange(32000) / 16000)).astype(np.float32)
        on = build_notes(words, track, Config(auto_octave=False), audio=audio)
        off = build_notes(words, track, Config(auto_octave=False, vowel_on_beat=False), audio=audio)
        self.assertAlmostEqual(on[1].start, 0.58, places=2)
        self.assertAlmostEqual(off[1].start, 0.50, places=2)
        self.assertAlmostEqual(on[0].start, 0.0, places=2)  # "look" starts voiced: unmoved

    def test_melody_range_widens_intervals_around_the_phrase(self):
        from teto_relay.config import Config
        from teto_relay.notes import Note
        from teto_relay.singing import musicalize

        def tones(widen):
            notes = [Note("a", 0.0, 0.3, 60, detected_midi=60.0), Note("b", 0.3, 0.6, 62, detected_midi=62.0),
                     Note("c", 0.6, 0.9, 64, detected_midi=64.0)]
            musicalize(notes, Config(singing_style="sung", scale_key="C", scale="chromatic",
                                     sung_melody_range=widen), {})
            return [n.tone for n in notes]

        self.assertEqual(tones(1.0), [60, 62, 64])
        self.assertEqual(tones(2.0), [58, 62, 66])  # twice the steps, same centre

    def test_an_unsure_phrase_is_sung_not_thrown_away(self):
        # The user's accented Japanese decoded at avg_logprob -0.97 on one run
        # and just below -1.0 on another, and the whole phrase vanished.
        import types

        import numpy as np

        from teto_relay.config import Config
        from teto_relay.stt import Transcriber

        def run(no_speech, logprob):
            word = types.SimpleNamespace(word="ねえ", start=0.0, end=0.5)
            segment = types.SimpleNamespace(no_speech_prob=no_speech, avg_logprob=logprob, words=[word],
                                            text="ねえ")
            t = Transcriber(Config(language="ja"))
            t._model = types.SimpleNamespace(transcribe=lambda *a, **k: ([segment], None))
            return t.transcribe(np.zeros(16000, np.float32), 16000)

        self.assertTrue(run(0.06, -1.02))    # spoken, just unsure: kept
        self.assertFalse(run(0.5, -1.02))    # unsure and probably not speech: dropped
        self.assertFalse(run(0.06, -1.8))    # far too unsure: garbage
        self.assertFalse(run(0.9, -0.2))     # confident nonsense over silence: dropped

    def test_a_click_in_a_pause_does_not_keep_the_pause_in_the_word(self):
        # The user's "I wanted to say ... that I love you": whisper gave "that"
        # 1.90-3.84 s; a click at 2.13 s kept the 1.7 s pause inside the word.
        import numpy as np

        from teto_relay.notes import _tighten_to_sound
        from teto_relay.stt import Word

        times = np.arange(500) / 100.0
        active = np.zeros(500, bool)
        active[190:206] = True   # the tail of "say" (whisper ended it early)
        active[213:216] = True   # a click
        active[362:384] = True   # "that"
        word = _tighten_to_sound([Word("that", 1.90, 3.84)], times, active)[0]
        self.assertAlmostEqual(word.start, 3.62, places=2)
        self.assertAlmostEqual(word.end, 3.84, places=2)

    def test_kanji_split_by_whisper_is_read_as_one_word(self):
        # 明日 was split into 明 + 日 and sung めい にち instead of あした.
        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Word

        cfg = Config(auto_octave=False, language="ja")
        notes = build_notes([Word("明", 0.0, 0.2), Word("日", 0.2, 0.4)], _flat_track(), cfg,
                            japanese_lyrics=True)
        self.assertEqual("".join(n.lyric for n in notes), "あした")

    def test_performance_scoops_glides_overshoots_and_falls(self):
        from teto_relay.config import Config
        from teto_relay.notes import Note
        from teto_relay.performance import (FALL_CENTS, OVERSHOOT_MAX_CENTS, SCOOP_CENTS,
                                            shape_pitch)

        notes = [Note("a", 0.0, 0.5, 60),
                 Note("b", 0.5, 1.0, 64, legato=True),   # a leap up
                 Note("c", 1.0, 1.5, 63, legato=True),   # a step
                 Note("d", 2.0, 2.5, 63)]                # after a rest: a new phrase
        shape_pitch(notes, Config())
        first = dict(notes[0].contour)
        self.assertAlmostEqual(first[0.0], -SCOOP_CENTS)         # scooped into
        leap = [y for _, y in notes[1].contour]
        self.assertGreater(max(leap), 0.0)                        # overshoots...
        self.assertLessEqual(max(leap), OVERSHOOT_MAX_CENTS)      # ...but not by much
        self.assertGreater(notes[1].lead_in_ms, notes[2].lead_in_ms)  # bigger interval, longer glide
        self.assertAlmostEqual(dict(notes[2].contour)[500.0], -FALL_CENTS)  # phrase end falls
        self.assertAlmostEqual(dict(notes[3].contour)[0.0], -SCOOP_CENTS)  # new phrase scoops again

    def test_dynamics_follow_the_speaker_within_limits(self):
        import numpy as np

        from teto_relay.config import Config
        from teto_relay.notes import Note
        from teto_relay.performance import DYN_FLOOR, expression_curves

        rate = 16000
        t = np.arange(int(1.2 * rate)) / rate
        # A loud word, then one 12 dB quieter.
        audio = np.where(t < 0.6, 0.4, 0.1) * np.sin(2 * np.pi * 200 * t)
        notes = [Note("a", 0.0, 0.6, 60, spoken=(0.0, 0.6)),
                 Note("b", 0.6, 1.2, 60, legato=True, spoken=(0.6, 1.2))]
        curves = expression_curves(notes, audio.astype(np.float32), rate, Config())
        dyn = dict(curves["dyn"])
        xs, ys = zip(*curves["dyn"])
        at = lambda x: float(np.interp(x, xs, ys))  # noqa: E731 - the curve OpenUtau draws
        self.assertEqual(at(0.3), 0.0)                  # the loud word is the reference
        self.assertAlmostEqual(at(0.8), -60.0, delta=5)  # 12 dB quieter, halved: -6 dB
        self.assertTrue(all(v <= 0 for v in dyn.values()))          # never boosts (it clips)
        self.assertGreaterEqual(min(dyn.values()), 2 * DYN_FLOOR)
        self.assertGreater(dict(curves["brec"])[1.2], 0)            # breathy phrase end
        self.assertEqual(expression_curves(notes, audio, rate, Config(expressive=False)), {})

    def test_curves_reach_the_ustx_in_ticks(self):
        from teto_relay.config import Config
        from teto_relay.notes import Note
        from teto_relay.ustx import build_project
        from teto_relay.voicebank import Voicebank

        cfg = Config()
        notes = [Note("a", 0.5, 1.0, 60)]
        bank = Voicebank(key="tandoku", name="Teto", root=Path("/x/Teto"), flavour="ja-cv")
        curves = {"dyn": [(0.5, -60.0), (0.56, 0.0), (1.0, -120.0)]}
        block = build_project(notes, bank, cfg, curves)["voice_parts"][0]["curves"][0]
        self.assertEqual(block["abbr"], "dyn")
        self.assertEqual(block["xs"][0], 0)  # from the part start
        self.assertEqual(block["xs"][-1], cfg.seconds_to_ticks(0.5))
        self.assertEqual(block["ys"], [-60, 0, -120])

    def test_keep_input_audio_saves_the_phrase_beside_the_render(self):
        import numpy as np
        import soundfile as sf

        from teto_relay.app import TetoRelay
        from teto_relay.capture import Chunk
        from teto_relay.config import Config

        with _TempHome() as home:
            relay = TetoRelay.__new__(TetoRelay)  # only the saving is under test
            relay.cfg = Config(out_dir=str(home / "out"), keep_input_audio=True)
            audio = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
            relay._save_input(Chunk(audio=audio, sample_rate=16000, reason="release"), "120000_000")
            saved, rate = sf.read(home / "out" / "relay_120000_000_in.wav", dtype="float32")
        self.assertEqual(rate, 16000)
        self.assertEqual(len(saved), 1600)

    def test_a_supported_compute_type_is_kept(self):
        import types
        import unittest.mock

        from teto_relay.stt import pick_compute_type

        fake_ct2 = types.SimpleNamespace(get_supported_compute_types=lambda d: {"float16", "int8"})
        with unittest.mock.patch.dict(sys.modules, {"ctranslate2": fake_ct2}):
            self.assertEqual(pick_compute_type("cuda", "float16"), "float16")
            self.assertEqual(pick_compute_type("cuda:0", "int8"), "int8")


def _bank(folder: Path, aliases: list[str], wavs: list[str] | None = None,
          character: str | None = "character.txt", extra: dict | None = None) -> Path:
    """A voicebank on disk: an oto.ini (Shift-JIS, as UTAU writes it), stub samples."""
    folder.mkdir(parents=True, exist_ok=True)
    wavs = wavs or [f"s{i}.wav" for i in range(len(aliases))]
    lines = [f"{w}={a},0,100,-200,50,10" for w, a in zip(wavs, aliases)]
    (folder / "oto.ini").write_bytes("\r\n".join(lines).encode("cp932"))
    for w in set(wavs):
        (folder / w).write_bytes(b"RIFF")
    if character == "character.txt":
        (folder / "character.txt").write_bytes("name=Someone\r\n".encode("cp932"))
    elif character == "character.yaml":
        (folder / "character.yaml").write_text("name: Yaml Singer\n", encoding="utf-8")
    for name, text in (extra or {}).items():
        (folder / name).write_bytes(text.encode("cp932"))
    return folder


class TestOtherPeoplesVoicebanks(unittest.TestCase):
    """Banks made by other people are laid out and spelled in many ways.

    Each of these sang silence, or plain tones, before; each was found by
    rendering a copy of Defoko rearranged that way through OpenUtau.
    """

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def discover_one(self):
        from teto_relay.voicebank import discover

        banks = discover(self.root)
        self.assertEqual(len(banks), 1, [b.root for b in banks])
        return banks[0]

    def test_a_romaji_bank_is_japanese_and_lyrics_find_its_aliases(self):
        # "ka", "- ka", "a ka" instead of か: kana lyrics matched nothing.
        _bank(self.root / "r", ["- sa", "a sa", "- ku", "a ku", "u ra", "- shi", "i tsu", "- n", "a", "i"])
        bank = self.discover_one()
        self.assertTrue(bank.flavour.startswith("ja-"))
        self.assertTrue(bank.romaji)
        self.assertTrue(bank.phonemizer.endswith("DefaultPhonemizer"))
        self.assertEqual(bank.alias_for("さ"), "- sa")
        self.assertEqual(bank.alias_for("く", "a"), "a ku")
        self.assertEqual(bank.alias_for("つ", "i"), "i tsu")
        self.assertEqual(bank.alias_for("ん"), "- n")

    def test_kunrei_spellings_are_found(self):
        _bank(self.root / "k", ["si", "tu", "hu", "ka", "sa", "ta", "a", "i", "u"])
        bank = self.discover_one()
        self.assertEqual([bank.alias_for(k) for k in "しつふ"], ["si", "tu", "hu"])

    def test_a_kana_bank_that_already_matches_is_left_alone(self):
        _bank(self.root / "t", ["あ", "- あ", "さ", "- さ", "く", "ら"])
        bank = self.discover_one()
        self.assertFalse(bank.romaji)
        self.assertEqual(bank.alias_for("さ"), "さ")
        self.assertEqual(bank.alias_for("さ", "a"), "さ")

    def test_the_project_spells_lyrics_the_bank_s_way(self):
        from teto_relay.config import Config
        from teto_relay.notes import Note
        from teto_relay.ustx import build_project

        _bank(self.root / "r", ["- sa", "a ku", "u ra", "- ra", "a", "- a"])
        bank = self.discover_one()
        notes = [Note("さ", 0.0, 0.3, 60), Note("く", 0.3, 0.6, 60, legato=True),
                 Note("ら", 0.6, 0.9, 60, legato=True), Note("ら", 1.5, 1.8, 60)]
        doc = build_project(notes, bank, Config())
        lyrics = [n["lyric"] for n in doc["voice_parts"][0]["notes"]]
        # After a rest, a phrase starts afresh.
        self.assertEqual(lyrics, ["- sa", "a ku", "u ra", "- ra"])

    def test_a_japanese_cvvc_bank_gets_the_cvvc_phonemizer(self):
        _bank(self.root / "c", ["- か", "か", "a k", "- さ", "さ", "a s", "o t", "u r", "a", "い"])
        self.assertEqual(self.discover_one().flavour, "ja-cvvc")

    def test_pitch_suffixes_from_prefix_map_are_not_part_of_the_alias(self):
        names = ["C4", "D4", "E4"]
        _bank(self.root / "p", ["あ_C4", "さ_C4", "sa_C4"],
              extra={"prefix.map": "\r\n".join(f"{n}\t\t_C4" for n in names)})
        bank = self.discover_one()
        self.assertIn("さ", bank.aliases)
        self.assertNotIn("さ_C4", bank.aliases)

    def test_a_bank_with_only_character_yaml_is_one_singer_with_its_name(self):
        _bank(self.root / "y" / "high", ["あ", "い"], character=None)
        _bank(self.root / "y" / "low", ["あ", "い"], character=None)
        (self.root / "y" / "character.yaml").write_text("name: Yaml Singer\n", encoding="utf-8")
        bank = self.discover_one()
        self.assertEqual(bank.name, "Yaml Singer")
        self.assertEqual(len(bank.subbanks), 2)
        self.assertEqual(bank.character_file.name, "character.yaml")

    def test_a_bank_with_no_character_file_gets_one_written_for_openutau(self):
        # OpenUtau reloads a bank from its character file's folder, so a
        # stand-in kept elsewhere loaded no samples: plain tones.
        from teto_relay.render.openutau import _add_character_file

        _bank(self.root / "bare", ["あ", "い"], character=None)
        bank = self.discover_one()
        self.assertIsNone(bank.character_file)
        path = _add_character_file(bank)
        self.assertEqual(path.parent, bank.root)
        self.assertIn("name=", path.read_bytes().decode("cp932"))

    def _zip(self, entries: dict, encoding: str | None = None) -> bytes:
        import io
        import zipfile

        class Legacy(zipfile.ZipInfo):
            # Japanese zip tools write Shift-JIS names without the UTF-8 flag.
            def _encodeFilenameFlags(self):
                return self.filename.encode(encoding), self.flag_bits

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, payload in entries.items():
                info = Legacy(name) if encoding else zipfile.ZipInfo(name)
                archive.writestr(info, payload)
        return buffer.getvalue()

    def test_a_shift_jis_zip_installs_with_its_real_file_names(self):
        # あ.wav came out as "âJüK.wav": oto.ini matched no sample at all.
        from teto_relay.library import install_voicebank
        from teto_relay.voicebank import discover, parse_oto

        data = self._zip({
            "デフォ子/oto.ini": "あ.wav=あ,0,100,-200,50,10\r\nか.wav=か,0,100,-200,50,10".encode("cp932"),
            "デフォ子/character.txt": "name=デフォ子".encode("cp932"),
            "デフォ子/あ.wav": b"RIFF",
            "デフォ子/か.wav": b"RIFF",
        }, encoding="cp932")
        info = install_voicebank(data, "defoko.zip", self.root / "lib")
        self.assertEqual(info["missing"], 0)
        bank = discover(self.root / "lib")[0]
        for entry in parse_oto(bank.root / "oto.ini"):
            self.assertTrue((bank.root / entry.wav).exists(), entry.wav)

    def test_a_multi_pitch_bank_inside_an_author_folder_installs_whole(self):
        # The first oto.ini was taken as the bank: one pitch installed.
        from teto_relay.library import install_voicebank

        oto = "a.wav=あ,0,100,-200,50,10".encode("cp932")
        data = self._zip({
            "author/Singer/character.txt": b"name=Singer",
            "author/Singer/A3/oto.ini": oto, "author/Singer/A3/a.wav": b"RIFF",
            "author/Singer/D4/oto.ini": oto, "author/Singer/D4/a.wav": b"RIFF",
        })
        info = install_voicebank(data, "singer.zip", self.root / "lib")
        installed = Path(info["path"])
        self.assertTrue((installed / "A3" / "oto.ini").exists())
        self.assertTrue((installed / "D4" / "oto.ini").exists())

    def test_an_installed_bank_without_character_txt_gets_one(self):
        from teto_relay.library import install_voicebank

        data = self._zip({"b/oto.ini": b"a.wav=a,0,1,0,1,1", "b/a.wav": b"RIFF"})
        info = install_voicebank(data, "bare.zip", self.root / "lib")
        self.assertTrue((Path(info["path"]) / "character.txt").exists())

    def test_a_bank_whose_samples_are_missing_is_refused_with_a_reason(self):
        from teto_relay.library import install_voicebank

        data = self._zip({"b/oto.ini": b"x.wav=a,0,1,0,1,1\r\ny.wav=i,0,1,0,1,1", "b/other.wav": b"RIFF"})
        with self.assertRaises(ValueError) as caught:
            install_voicebank(data, "b.zip", self.root / "lib")
        self.assertIn("samples", str(caught.exception))

    def test_archives_and_voices_it_cannot_use_say_what_to_do(self):
        from teto_relay.library import install_voicebank

        with self.assertRaises(ValueError) as caught:
            install_voicebank(b"Rar!\x1a\x07", "bank.rar", self.root / "lib")
        self.assertIn("Unpack", str(caught.exception))
        data = self._zip({"ds/dsconfig.yaml": b"phonemes: x", "ds/model.onnx": b"x"})
        with self.assertRaises(ValueError) as caught:
            install_voicebank(data, "ds.zip", self.root / "lib")
        self.assertIn("DiffSinger", str(caught.exception))

    def test_switching_bank_while_running_changes_the_singer(self):
        # Only the lyrics followed the switch: OpenUtau kept singing with the
        # bank the relay started with, and a newly installed bank was unknown.
        from teto_relay.app import TetoRelay
        from teto_relay.config import Config

        _bank(self.root / "first", ["あ", "い"])
        relay = TetoRelay.__new__(TetoRelay)
        relay.cfg = Config(voicebank_root=str(self.root))
        from teto_relay.voicebank import discover

        relay.banks = discover(self.root)
        relay.bank = relay.banks[0]
        sung_with = []
        relay.renderer = type("R", (), {"set_bank": lambda self, b: sung_with.append(b.key)})()
        _bank(self.root / "newcomer", ["か", "き"])  # installed while running
        import unittest.mock

        with unittest.mock.patch("teto_relay.voicebank.estimate_pitch", return_value=60.0):
            relay.set_voicebank("newcomer")
        self.assertEqual(sung_with, ["newcomer"])
        self.assertEqual(relay.bank.key, "newcomer")


class TestThai(unittest.TestCase):
    """Thai speech to a Japanese or English voicebank.

    Checked on 20 Thai phrases (tools/tuning_eval.py, judged by whisper
    medium): Thai was sung from whisper's character fragments, spelled
    through a romanisation that read ท as English "th".
    """

    def test_whisper_pieces_are_joined_into_thai_words(self):
        from teto_relay.stt import Word, _regroup_thai

        pieces = [Word("ส", 0.0, 0.3), Word("วั", 0.3, 0.42), Word("สดี", 0.42, 0.58),
                  Word("ครับ", 0.58, 0.82), Word("teto", 0.9, 1.2)]
        words = _regroup_thai(pieces)
        self.assertEqual([w.text for w in words], ["สวัสดี", "ครับ", "teto"])
        self.assertEqual((words[0].start, words[0].end), (0.0, 0.58))
        self.assertEqual((words[1].start, words[1].end), (0.58, 0.82))

    def test_a_mark_with_no_duration_stays_with_its_consonant(self):
        # ื่ came back with start == end and was dropped: ชื่อ sang as ช-อ.
        import types

        from teto_relay.config import Config
        from teto_relay.stt import Transcriber

        seg = types.SimpleNamespace(
            no_speech_prob=0.0, avg_logprob=-0.2, text="ชื่อ",
            words=[types.SimpleNamespace(word="ช", start=1.0, end=1.2),
                   types.SimpleNamespace(word="ื่", start=1.2, end=1.2),
                   types.SimpleNamespace(word="อ", start=1.2, end=1.28)])
        t = Transcriber(Config(language="th"))
        t._model = types.SimpleNamespace(transcribe=lambda *a, **k: ([seg], None))
        import numpy as np

        words = t.transcribe(np.zeros(16000, dtype=np.float32), 16000)
        self.assertEqual([w.text for w in words], ["ชื่อ"])

    def test_thai_listens_with_the_thai_model(self):
        from teto_relay.config import Config
        from teto_relay.stt import THAI_MODEL_NAME, effective_model

        self.assertEqual(effective_model(Config(language="th", whisper_model="small")), THAI_MODEL_NAME)
        self.assertEqual(effective_model(Config(language="th", thai_speech_model=False,
                                                whisper_model="small")), "small")
        self.assertEqual(effective_model(Config(language="en", whisper_model="small")), "small")

    def test_syllables_become_the_sounds_a_japanese_bank_has(self):
        from teto_relay import thai

        def kana(ipa):
            return "".join(thai.syllable_kana(s) for s in thai.parse(ipa))

        self.assertEqual(kana("kʰ w aː m ˧ . r a k̚ ˦˥"), "くわあんらっ")   # ความรัก
        self.assertEqual(kana("kʰ r a p̚ ˦˥"), "くらっ")                   # ครับ; っ is a rest
        self.assertEqual(kana("kʰ aː w ˥˩"), "かあお")                    # ข้าว
        self.assertEqual(kana("r i a̯ n ˧"), "りあん")                     # เรียน
        self.assertEqual(kana("tʰ ɤː ˧"), "たあ")                          # เธอ: ท is t, not "th"
        self.assertEqual(kana("d iː ˧"), "でぃい")                         # ดี, not ぢ
        self.assertEqual(kana("f aː ˦˥"), "ふぁあ")                        # ฟ้า

    def test_english_banks_get_the_whole_syllable(self):
        from teto_relay import thai

        found = thai.parse("kʰ r a p̚ ˦˥ . pʰ o m ˩˩˦")
        self.assertEqual(" ".join(p for s in found for p in thai.syllable_xsampa(s)), "k r A p p oU m")

    def test_a_looping_answer_is_not_used(self):
        # The G2P model can repeat itself on a long word: kʰun kʰun nun nun ...
        import unittest.mock

        from teto_relay import thai

        looped = "kʰ ɔː p̚ ˨˩ . kʰ u n ˧ . n u n ˧ . n u n ˧ . n u n ˧ . n u n ˧"
        answers = {"ขอบคุณ": "kʰ ɔː p̚ ˨˩ . kʰ u n ˧", "มาก": "m aː k̚ ˥˩", "ครับ": "kʰ r a p̚ ˦˥"}

        def fake(text, engine):
            return answers.get(text, looped)

        thai.syllables.cache_clear()
        with unittest.mock.patch("pythainlp.transliterate.transliterate", fake):
            found = thai.syllables("ขอบคุณมากครับ")
        thai.syllables.cache_clear()
        self.assertEqual(len(found), 4)

    def test_an_unreleased_stop_is_a_short_silence(self):
        # รัก: the vowel, then its stop's slot left silent; the next word
        # starts afresh rather than joining over the gap.
        import numpy as np

        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.pitch import F0Track
        from teto_relay.stt import Word

        times = np.arange(0, 1.2, 0.01)
        track = F0Track(times=times, f0=np.full_like(times, 220.0), voiced=np.ones_like(times, bool),
                        sample_rate=16000)
        cfg = Config(language="th", align_morae=False, vowel_on_beat=False)
        notes = build_notes([Word("รัก", 0.1, 0.5), Word("กัน", 0.5, 0.9)], track, cfg,
                            japanese_lyrics=True)
        from teto_relay.ustx import build_project
        from teto_relay.voicebank import Voicebank

        # The stop holds its slot through the layout, so the vowel is not
        # stretched over it ...
        ra = next(n for n in notes if n.lyric == "ら")
        stop = notes[notes.index(ra) + 1]
        self.assertEqual(stop.lyric, "っ")
        self.assertGreater(stop.end - stop.start, 0.05)
        # ... and is a rest in the project.
        bank = Voicebank("t", "t", Path("."), flavour="ja-cv")
        sung = build_project(notes, bank, cfg)["voice_parts"][0]["notes"]
        self.assertEqual([n["lyric"] for n in sung], ["ら", "か", "ん"])
        self.assertGreater(sung[1]["position"], sung[0]["position"] + sung[0]["duration"])

    def test_arpasing_banks_get_their_own_phonemes(self):
        # Thai reached Miku as "sawatdii" with no hint: silence.
        from teto_relay.render.openutau import xsampa_to_arpabet

        self.assertEqual(xsampa_to_arpabet("k r A p p oU m"), "k r aa p p ow m")
        self.assertEqual(xsampa_to_arpabet("tS aI"), "ch ay")
        self.assertIsNone(xsampa_to_arpabet("k Q"))

    def test_teh_i_is_one_mora_and_falls_back_on_banks_without_it(self):
        from teto_relay.japanese import split_morae
        from teto_relay.translit import vowel_of
        from teto_relay.voicebank import Voicebank

        self.assertEqual(split_morae("でぃいふぁ"), ["でぃ", "い", "ふぁ"])
        self.assertEqual(vowel_of("でぃ"), "i")
        with_it = Voicebank("a", "a", Path("."), aliases=frozenset({"でぃ", "い"}))
        without = Voicebank("b", "b", Path("."), aliases=frozenset({"ぢ", "い", "a い"}))
        self.assertEqual(with_it.singable("でぃ"), "でぃ")
        self.assertEqual(without.singable("でぃ"), "ぢ")


class TestAppWindow(unittest.TestCase):
    """The panel opens as a window of its own, and can be installed as an app."""

    @staticmethod
    def _child(exit_code):
        """A started process: still running (None), or exited with exit_code."""
        import unittest.mock

        child = unittest.mock.Mock()
        child.poll.return_value = exit_code
        child.returncode = exit_code
        return child

    def test_the_panel_opens_in_teto_relays_own_window(self):
        # An Edge app window's taskbar button is Edge's: pinning it pinned Edge.
        import unittest.mock

        from teto_relay import appwindow

        with unittest.mock.patch.object(appwindow, "_launch", return_value=self._child(None)) as launch, \
                unittest.mock.patch.object(appwindow, "WINDOW_GRACE", 0.1), \
                unittest.mock.patch.object(appwindow.webbrowser, "open") as tab:
            self.assertEqual(appwindow.open_panel("http://127.0.0.1:8765/"), "window")
        command = launch.call_args[0][0]
        self.assertEqual(command[-2:], ["--window", "http://127.0.0.1:8765/"])
        self.assertEqual(launch.call_count, 1)
        tab.assert_not_called()

    def test_edge_opens_the_panel_when_our_window_cannot(self):
        # No WebView2: the window process exits 1, and Edge's app mode is used.
        import unittest.mock

        from teto_relay import appwindow

        launched = [self._child(1), self._child(None)]
        with unittest.mock.patch.object(appwindow, "find_app_browser", return_value=Path("edge.exe")), \
                unittest.mock.patch.object(appwindow, "_launch", side_effect=launched) as launch, \
                unittest.mock.patch.object(appwindow.webbrowser, "open") as tab:
            self.assertEqual(appwindow.open_panel("http://127.0.0.1:8765/"), "app")
        self.assertIn("--app=http://127.0.0.1:8765/", launch.call_args[0][0])
        tab.assert_not_called()

    def test_a_pinned_window_starts_teto_relay(self):
        # The pin runs the windowed program, with its icon, whatever process
        # drew the window.
        import unittest.mock

        from teto_relay import window

        with tempfile_app() as app:
            with unittest.mock.patch.object(window.sys, "frozen", True, create=True), \
                    unittest.mock.patch.object(window.sys, "executable", str(app / "TetoRelayConsole.exe")):
                command, icon = window.relaunch_command()
        self.assertEqual(command, f'"{app / "TetoRelay.exe"}"')
        self.assertEqual(icon, f'{app / "TetoRelay.exe"},0')
        self.assertEqual(window.APP_ID, "KasaneTeto.TetoRelay")

    def test_without_edge_or_chrome_a_browser_tab_opens(self):
        import unittest.mock

        from teto_relay import appwindow

        # Our window cannot open (no WebView2) and there is no Edge or Chrome.
        with unittest.mock.patch.object(appwindow, "find_app_browser", return_value=None), \
                unittest.mock.patch.object(appwindow, "_launch", return_value=self._child(1)), \
                unittest.mock.patch.object(appwindow.webbrowser, "open") as tab:
            self.assertEqual(appwindow.open_panel("http://127.0.0.1:8765/"), "browser")
        tab.assert_called_once()

    def test_the_install_files_are_served(self):
        import json

        from teto_relay.webui import STATIC, static_file

        manifest = json.loads(static_file("manifest.webmanifest"))
        self.assertEqual(manifest["display"], "standalone")
        for icon in manifest["icons"]:
            self.assertIn(icon["src"].lstrip("/"), STATIC)
            self.assertTrue(static_file(icon["src"].lstrip("/")).startswith(b"\x89PNG"))


class TestInventedWords(unittest.TestCase):
    """Whisper writes words that were never said - the Thai model loops
    "สวัสดีครับ" - in the silence after a phrase ("... ตลอด เว้ย สวัสดี ครับ
    สวัสดี ครับ สวัสดี ครั" from the user's microphone)."""

    def _transcribe(self, pieces, audio, budget_used=0):
        import types

        import numpy as np

        from teto_relay.config import Config
        from teto_relay.stt import Transcriber

        seg = types.SimpleNamespace(
            no_speech_prob=0.0, avg_logprob=-0.2, text="", tokens=[0] * budget_used,
            words=[types.SimpleNamespace(word=w, start=a, end=b) for w, a, b in pieces])
        t = Transcriber(Config(language="th"))
        t._model = types.SimpleNamespace(transcribe=lambda *a, **k: ([seg], None))
        return [w.text for w in t.transcribe(np.asarray(audio, dtype=np.float32), 16000)]

    @staticmethod
    def _speech_then_silence(speech_s, total_s):
        import numpy as np

        rng = np.random.default_rng(0)
        audio = rng.normal(0, 0.0005, int(total_s * 16000))
        audio[: int(speech_s * 16000)] += 0.2 * np.sin(np.arange(int(speech_s * 16000)) * 0.05)
        return audio

    def test_words_over_the_silence_after_speech_are_dropped(self):
        audio = self._speech_then_silence(0.6, 3.0)
        words = self._transcribe([("ฮัลโหล", 0.0, 0.5), (" ฮัล", 0.68, 1.66), ("โหล", 3.04, 3.04)], audio)
        self.assertEqual(words, ["ฮัลโหล"])

    def test_a_final_consonant_just_after_the_voice_is_kept(self):
        # A Thai final บ is a silent closure, timed after the sound stops.
        audio = self._speech_then_silence(0.5, 2.0)
        words = self._transcribe([("ครั", 0.0, 0.5), ("บ", 0.52, 0.6)], audio)
        self.assertEqual(words, ["ครับ"])

    def test_a_noisy_tail_does_not_move_the_end_of_speech(self):
        # Room noise after speaking spikes over the sound level now and then;
        # read frame by frame, speech "ended" at the end of the recording.
        import numpy as np

        from teto_relay.stt import last_sound, speech_envelope

        rng = np.random.default_rng(1)
        audio = self._speech_then_silence(2.0, 4.5)
        audio[int(2.0 * 16000):] += rng.normal(0, 0.004, len(audio) - int(2.0 * 16000))
        audio[int(3.5 * 16000):int(3.5 * 16000) + 80] += 0.3  # a click
        rms, level = speech_envelope(audio.astype(np.float32), 16000)
        self.assertAlmostEqual(last_sound(rms, level), 2.0, delta=0.15)

    def test_a_video_intro_tacked_on_after_the_sentence_is_dropped(self):
        # "... เว้ย" then a second segment "สวัสดี ครับ คลิป นี้ เป็น ..." -
        # the shape of every invented ending on the noisy test phrases.
        import types

        import numpy as np

        from teto_relay.config import Config
        from teto_relay.stt import Transcriber

        def seg(start, logprob, words):
            return types.SimpleNamespace(
                start=start, no_speech_prob=0.0, avg_logprob=logprob, text="".join(w[0] for w in words),
                tokens=[0], words=[types.SimpleNamespace(word=w, start=a, end=b, probability=pr)
                                   for w, a, b, pr in words])

        real = seg(0.0, -0.19, [("ตลอด", 0.2, 0.9, 0.99), ("เว้ย", 0.9, 1.4, 0.95)])
        made_up = seg(1.9, -0.65, [("ส", 1.9, 2.4, 0.05), ("วัสดี", 2.4, 2.9, 0.9), ("ครับ", 2.9, 3.0, 0.9)])
        t = Transcriber(Config(language="th"))
        t._model = types.SimpleNamespace(transcribe=lambda *a, **k: ([real, made_up], None))
        audio = self._speech_then_silence(1.5, 3.2).astype(np.float32)
        self.assertEqual([w.text for w in t.transcribe(audio, 16000)], ["ตลอด", "เว้ย"])
        # A second segment that is as sure as the first is speech.
        sure = seg(1.9, -0.25, [("ส", 1.9, 2.4, 0.9), ("วัสดี", 2.4, 2.9, 0.9)])
        t._model = types.SimpleNamespace(transcribe=lambda *a, **k: ([real, sure], None))
        audio = self._speech_then_silence(3.0, 3.2).astype(np.float32)
        self.assertEqual(len(t.transcribe(audio, 16000)), 3)

    def test_a_loop_that_ran_out_of_budget_is_cut_to_one_copy(self):
        from teto_relay.stt import Word, _trim_loop

        def trim(text, budget=False):
            return [w.text for w in _trim_loop([Word(x, i, i + 1) for i, x in enumerate(text.split())], budget)]

        self.assertEqual(trim("เว้ย สวัสดี ครับ สวัสดี ครับ สวัสดี ครั"), ["เว้ย", "สวัสดี", "ครับ"])
        self.assertEqual(trim("hello hello hello hello", budget=True), ["hello"])
        # Sung repeats that end where the singer stopped are lyrics.
        self.assertEqual(trim("la la la la"), ["la", "la", "la", "la"])
        self.assertEqual(trim("ไป ไป"), ["ไป", "ไป"])

    def test_thai_speech_goes_to_typhoon_and_whisper_is_not_loaded(self):
        import types
        import unittest.mock

        import numpy as np

        from teto_relay import thai_asr
        from teto_relay.config import Config
        from teto_relay.stt import Transcriber, Word

        heard = [Word("สวัสดี", 0.1, 0.5)]
        fake = types.SimpleNamespace(load=lambda: None, transcribe=lambda audio, rate: heard)
        with unittest.mock.patch.object(thai_asr, "ThaiTranscriber", return_value=fake):
            t = Transcriber(Config(language="th"))
            self.assertEqual(t.transcribe(np.zeros(16000, dtype=np.float32), 16000), heard)
        self.assertIsNone(t._model)
        self.assertFalse(t.on_gpu)

    def test_a_broken_thai_model_falls_back_to_whisper(self):
        import sys
        import types
        import unittest.mock

        from teto_relay import thai_asr
        from teto_relay.config import Config
        from teto_relay.stt import Transcriber

        made = []
        whisper = types.SimpleNamespace(WhisperModel=lambda *a, **k: made.append(a) or "model")
        with unittest.mock.patch.object(thai_asr, "ThaiTranscriber", side_effect=RuntimeError("no model")),                 unittest.mock.patch.dict(sys.modules, {"faster_whisper": whisper}):
            t = Transcriber(Config(language="th", whisper_device="cpu", whisper_model="small"))
            t.load()
        self.assertIsNone(t._thai)
        self.assertEqual(made, [("small",)])

    def test_typhoon_words_run_on_to_the_next(self):
        from teto_relay.thai_asr import words_from_chunks

        chunks = [{"text": "หากว่า", "start": 0.40, "end": 0.64},
                  {"text": "เธอ", "start": 0.72, "end": 0.80},
                  {"text": "ผ่าน", "start": 1.60, "end": 1.68},
                  {"text": " ", "start": 1.7, "end": 1.8}]
        words = words_from_chunks(chunks)
        self.assertEqual([w.text for w in words], ["หากว่า", "เธอ", "ผ่าน"])
        self.assertEqual((words[0].start, words[0].end), (0.40, 0.72))  # on to เธอ
        self.assertEqual(words[1].end, 0.80)  # a pause before ผ่าน is kept
        # Two words on one emission step get an onset each.
        same = words_from_chunks([{"text": "เมื่อวาน", "start": 1.04, "end": 1.12},
                                  {"text": "นี้", "start": 1.04, "end": 1.12}])
        self.assertAlmostEqual(same[1].start, 1.12)
        self.assertAlmostEqual(same[0].end, 1.12)
        self.assertGreater(same[1].end, same[1].start)
        # A sign-off it hears was said: kept (whisper's are cut, drop_outro).
        said = words_from_chunks([{"text": "ขอบคุณ", "start": 0.0, "end": 0.3},
                                  {"text": "ผู้ชม", "start": 0.3, "end": 0.6}])
        self.assertEqual(len(said), 2)

    def test_whisper_sign_offs_are_cut(self):
        from teto_relay.stt import Word, drop_outro

        def run(text):
            ws = [Word(x, i, i + 1) for i, x in enumerate(text.split())]
            return " ".join(w.text for w in drop_outro(ws))

        self.assertEqual(run("หากว่า เธอ ผ่าน มา ได้ยิน เพลง หนี สวัสดี ครับ ขอบคุณ ผู้ชม ครับ แล้วก็ กลับมา"),
                         "หากว่า เธอ ผ่าน มา ได้ยิน เพลง หนี")
        self.assertEqual(run("บ คุณ ผู้ชม"), "")
        self.assertEqual(run("ฉัน ชอบ ฟัง เพลง อ่า สวัสดี ครับ ขอบคุณ ที่ ช่วยกัน นะ ครับ"),
                         "ฉัน ชอบ ฟัง เพลง อ่า")
        for kept in ("ขอบคุณ มาก ครับ", "สวัสดี ครับ ผม ชื่อ เท็ตโตะ", "ชม ดาว บน ฟ้า",
                     "thanks for watching"):
            self.assertEqual(run(kept), kept)


class TestRenamingVoicebanks(unittest.TestCase):
    """A voicebank can be given a name of the user's own; the bank's files
    are not touched and an empty name goes back to its own."""

    def test_rename_and_back(self):
        from teto_relay import voicebank as vb

        with _TempHome() as home:
            _bank(home / "banks" / "mine", ["あ", "い"])
            bank = vb.discover(home / "banks")[0]
            before = (bank.root / "character.txt").read_bytes()
            self.assertEqual(vb.rename(bank, "  My   Teto  "), "My Teto")
            self.assertEqual(vb.custom_name(bank), "My Teto")
            self.assertEqual((bank.root / "character.txt").read_bytes(), before)
            self.assertIsNone(vb.rename(bank, ""))
            self.assertIsNone(vb.custom_name(bank))
            with self.assertRaises(ValueError):
                vb.rename(bank, "x" * (vb.MAX_NAME + 1))

    def test_the_panel_shows_the_new_name(self):
        from teto_relay import voicebank as vb
        from teto_relay.config import Config
        from teto_relay.webui import _meta

        with _TempHome() as home:
            _bank(home / "banks" / "mine", ["あ", "い"])
            cfg = Config(voicebank_root=str(home / "banks"))
            vb.rename(vb.discover(home / "banks")[0], "Kasane")
            entry = _meta(cfg)["banks"][0]
        self.assertEqual((entry["name"], entry["custom_name"], entry["own_name"]), ("Kasane", "Kasane", "Someone"))


class TestReleaseReview(unittest.TestCase):
    """Found reviewing the 0.3.0 build."""

    def test_crepe_without_cuda_uses_the_tiny_model(self):
        # crepe full on the CPU took 4.8 s for a 3 s phrase; tiny 0.28 s and
        # within 10 cents of full on the user's recordings.
        import types
        import unittest.mock

        import numpy as np
        import torch

        from teto_relay import pitch
        from teto_relay.config import Config

        seen = {}

        def predict(audio, rate, hop, **kw):
            seen.update(kw)
            n = audio.shape[-1] // hop + 1
            return torch.full((1, n), 220.0), torch.ones((1, n))

        fake = types.SimpleNamespace(predict=predict)
        with unittest.mock.patch.dict(sys.modules, {"torchcrepe": fake}), \
                unittest.mock.patch.object(torch.cuda, "is_available", return_value=False):
            track = pitch._track_crepe(np.zeros(16000, np.float32), 16000,
                                       Config(crepe_model="full", crepe_device="cuda"))
        self.assertEqual((seen["model"], seen["device"]), ("tiny", "cpu"))
        self.assertEqual(track.method, "crepe/tiny@cpu")

    def test_edge_is_started_without_the_packaged_apps_dll_path_or_variables(self):
        # From the packaged TetoRelay.exe, Edge inherited PyInstaller's DLL
        # directory and exited without a window: double-click showed nothing.
        import os
        import unittest.mock

        from teto_relay import appwindow

        calls = []

        class Kernel:
            def GetDllDirectoryW(self, size, buf):
                buf.value = r"C:\app\_internal"
                return len(buf.value)

            def SetDllDirectoryW(self, value):
                calls.append(value)

        env = {"PATH": "x", "_PYI_APPLICATION_HOME_DIR": "y", "_MEIPASS2": "z"}
        with unittest.mock.patch.object(appwindow.sys, "frozen", True, create=True), \
                unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch("ctypes.windll", unittest.mock.Mock(kernel32=Kernel()), create=True), \
                unittest.mock.patch.object(appwindow.subprocess, "Popen") as popen:
            appwindow._launch(["edge.exe", "--app=http://127.0.0.1:8765/"])
        passed = popen.call_args.kwargs["env"]
        self.assertIn("PATH", passed)
        self.assertFalse([k for k in passed if k.startswith(("_PYI", "_MEI"))])
        self.assertEqual(calls, [None, r"C:\app\_internal"])  # cleared, then restored

    def test_the_windowed_exe_always_opens_the_panel(self):
        # TetoRelay.exe --config x.json ran an invisible headless relay.
        import runpy
        import unittest.mock

        seen = {}
        fake_main = types_module(main=lambda args: seen.setdefault("args", args) and 0)
        launcher = str(ROOT / "packaging" / "launcher.py")
        for exe, argv, expected in (
            (r"C:\app\TetoRelay.exe", ["--config", "x.json"], ["--web", "--config", "x.json"]),
            (r"C:\app\TetoRelay.exe", ["--tray"], ["--tray"]),
            (r"C:\app\TetoRelayConsole.exe", ["--doctor"], ["--doctor"]),
            (r"C:\app\TetoRelay.exe", [], ["--web"]),
            # The panel window itself is a windowed mode.
            (r"C:\app\TetoRelay.exe", ["--window", "http://x/"], ["--window", "http://x/"]),
        ):
            seen.clear()
            with unittest.mock.patch.object(sys, "executable", exe), \
                    unittest.mock.patch.object(sys, "argv", ["launcher", *argv]), \
                    unittest.mock.patch.dict(sys.modules, {"teto_relay.__main__": fake_main}):
                with self.assertRaises(SystemExit):
                    runpy.run_path(launcher, run_name="__main__")
            self.assertEqual(seen["args"], expected, exe)

    def test_thai_is_warmed_before_the_first_phrase(self):
        import inspect

        from teto_relay.app import TetoRelay

        source = inspect.getsource(TetoRelay._warmup)
        self.assertIn('stage("thai", warm_thai)', source)


class tempfile_app:
    """A folder holding stand-ins for the two packaged programs."""

    def __enter__(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        app = Path(self._tmp.name).resolve()
        for name in ("TetoRelay.exe", "TetoRelayConsole.exe"):
            (app / name).write_bytes(b"")
        return app

    def __exit__(self, *exc):
        self._tmp.cleanup()


def types_module(**attrs):
    import types

    module = types.ModuleType("teto_relay.__main__")
    module.__dict__.update(attrs)
    return module


class TestRestartsAndClosing(unittest.TestCase):
    """The user: the app "glitches" when settings change while it restarts,
    and closing its window left Python running."""

    def test_no_console_does_not_break_model_downloads(self):
        # TetoRelay.exe has no console: sys.stdout/stderr are None, and the
        # Hugging Face progress bar raised on them loading the Thai model,
        # then hung the next restart.
        import tempfile

        out = Path(tempfile.mkdtemp()) / "result.txt"
        code = (
            "import sys, os\n"
            "sys.stdout = sys.stderr = None\n"
            "import teto_relay\n"
            "sys.stderr.write('progress bar\\r')\n"
            f"open(r'{out}', 'w').write(str(sys.stderr is not None) + ' '"
            " + os.environ.get('HF_HUB_DISABLE_PROGRESS_BARS', ''))\n"
        )
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, timeout=60, check=True)
        self.assertEqual(out.read_text(), "True 1")

    def test_stopping_waits_for_the_phrase_in_progress(self):
        # After 2 s the old relay's analysis carried on beside the next
        # relay's start and they fought over the GPU (126 s for one phrase).
        import threading
        import time

        from teto_relay.app import TetoRelay

        relay = TetoRelay.__new__(TetoRelay)
        relay._stop = threading.Event()
        worker = threading.Thread(target=lambda: time.sleep(3.0), name="analyse", daemon=True)
        worker.start()
        relay._threads = [worker]
        relay.stop()
        self.assertFalse(worker.is_alive())

    def test_a_setting_changed_during_a_restart_is_applied(self):
        # Saved and shown as applied, but the relay coming up had already
        # read the old value, and no restart followed.
        import http.client
        import json
        import tempfile
        import threading
        import time
        import unittest.mock

        from teto_relay import webui
        from teto_relay.config import Config

        built = []

        class FakeRelay:
            def __init__(self, cfg):
                self.cfg, self.bank, self.engine = cfg, None, "utau"

            def start(self):
                used = self.cfg.ptt_key  # read early, like the microphone
                time.sleep(0.5)
                built.append(used)

            def stop(self):
                pass

        def post(port, body):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/config", json.dumps(body),
                         {"Content-Type": "application/json", webui.TOKEN_HEADER: "1"})
            return json.loads(conn.getresponse().read())

        path = Path(tempfile.mkdtemp()) / "config.json"
        Config(ptt_key="f8").save(path)
        # The real restart delay: the old code bound it as a default argument.
        with unittest.mock.patch("teto_relay.app.TetoRelay", FakeRelay):
            controller = webui.Controller(Config.load(path), path)
            server = webui.PanelServer(("127.0.0.1", 0), webui.make_handler(controller))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                controller.start()
                built.clear()
                post(port, {"ptt_key": "a"})
                time.sleep(webui.RESTART_DELAY + 0.25)  # inside the restart's start
                began = time.monotonic()
                answer = post(port, {"ptt_key": "b"})
                self.assertLess(time.monotonic() - began, 0.3)  # the save did not wait
                self.assertTrue(answer["restarting"])
                deadline = time.monotonic() + 5
                while controller.restarting and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                server.shutdown()
        self.assertEqual(built[-1], "b")
        self.assertTrue(controller.running)

    def test_closing_the_panel_quits_unless_a_page_comes_back(self):
        import time
        import unittest.mock

        from teto_relay import webui
        from teto_relay.config import Config

        with unittest.mock.patch.object(webui, "CLOSE_GRACE", 0.05):
            controller = webui.Controller(Config())
            controller.status()
            self.assertFalse(controller.should_quit())
            controller.page_closing()               # a reload...
            controller.status()                     # ...comes straight back
            time.sleep(0.1)
            self.assertFalse(controller.should_quit())
            controller.page_closing()               # the window closed
            time.sleep(0.1)
            self.assertTrue(controller.should_quit())


if __name__ == "__main__":
    unittest.main()
