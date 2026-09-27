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


class TestUploadedModelsAreNotUnpickled(unittest.TestCase):
    """P0-1: an uploaded .pth must not be able to run code when it is checked."""

    @unittest.skipUnless(__import__("importlib").util.find_spec("torch"), "needs torch")
    def test_a_pth_that_runs_code_on_load_is_rejected_without_running(self):
        import pickle
        import tempfile

        from teto_relay.library import install_rvc_model

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "pwned"

            class Exploit:
                def __reduce__(self):
                    return (open, (str(marker), "w"))

            data = pickle.dumps({"weight": Exploit(), "config": [], "sr": "40k"})
            with self.assertRaises(ValueError) as caught:
                install_rvc_model(data, "evil.pth", Path(tmp) / "models")
            self.assertFalse(marker.exists(), "the pickle payload ran")
            self.assertIn("not kept", str(caught.exception))
            self.assertFalse((Path(tmp) / "models" / "evil.pth").exists())

    @unittest.skipUnless(__import__("importlib").util.find_spec("torch"), "needs torch")
    def test_a_genuine_rvc_checkpoint_still_installs(self):
        import io
        import tempfile

        import torch

        from teto_relay.library import install_rvc_model

        buffer = io.BytesIO()
        torch.save({"weight": {"w": torch.zeros(2)}, "config": [1, 2], "sr": "40k",
                    "f0": 1, "version": "v2", "info": "test"}, buffer)
        with tempfile.TemporaryDirectory() as tmp:
            info = install_rvc_model(buffer.getvalue(), "teto.pth", Path(tmp))
            self.assertEqual(info["kind"], "model")
            self.assertEqual(info["version"], "v2")


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
            self.load('{"transpose": "loud", "beam_size": 0, "mode": "karaoke"}')
        message = str(caught.exception)
        for key in ("transpose", "beam_size", "mode"):
            self.assertIn(key, message)

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
            cfg = Config(pitch_method="pyin", use_alignment=False, renderer_backend="null")
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
        self.assertEqual(lyrics, "あいあむとうえんちいわん")


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
            out = align.refine(words, audio, 16000, Config())
        self.assertEqual(out[1], words[1])  # kept whisper's timing
        self.assertAlmostEqual(out[0].start, 0.02)
        self.assertAlmostEqual(out[2].start, 0.12)


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
            cache.write_text(json.dumps({str((home / "a").resolve()): 61.0}), encoding="utf-8")
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
        notes, _ = self.notes()
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

        cfg = Config(singing_style="sung", sung_contour_amount=0.5, scale_key="C")
        note = Note("la", 0.0, 0.2, 60, contour=[(0.0, 100.0), (100.0, -60.0)],
                    detected_midi=60.0)
        musicalize([note], cfg, {})
        self.assertEqual(note.contour, [(0.0, 50.0), (100.0, -30.0)])

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
        self.assertEqual(block["vibrato"]["length"], 60.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ustx(notes, Path(tmp) / "v.ustx", bank, Config())
            wav = NullRenderer(Config()).render(path, Path(tmp) / "v.wav")
            audio, _ = sf.read(wav)
        self.assertGreater(len(audio), 0)


class TestLegato(unittest.TestCase):
    """legato: the morae of one word touch; words keep their gap."""

    def build(self, legato):
        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Word

        cfg = Config(auto_octave=False, legato=legato)
        return build_notes([Word("hello", 0.0, 0.6), Word("teto", 0.8, 1.4)],
                           _flat_track(), cfg, japanese_lyrics=True)

    def test_off_by_default_every_note_has_a_gap(self):
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


class TestBlockStreamer(unittest.TestCase):
    """P3-1: block-wise conversion with SOLA crossfades."""

    def run_stream(self, convert, seconds=3.0, frame=320, **kwargs):
        import numpy as np

        from teto_relay.streaming import BlockStreamer

        rng = np.random.default_rng(1)
        x = (0.1 * rng.standard_normal(int(16000 * seconds))).astype(np.float32)
        streamer = BlockStreamer(convert, 16000, **kwargs)
        out = []
        for i in range(0, len(x), frame):
            out += streamer.feed(x[i:i + frame])
        out += streamer.flush()
        return x, np.concatenate(out), streamer

    def test_an_identity_converter_is_reconstructed_exactly(self):
        import numpy as np

        x, y, streamer = self.run_stream(lambda a, r: (a, r))
        self.assertEqual(len(y), len(x))
        self.assertLess(float(np.max(np.abs(y - x))), 1e-6)
        self.assertAlmostEqual(streamer.latency_seconds, 0.35)

    def test_output_at_another_rate(self):
        import numpy as np

        x, y, streamer = self.run_stream(lambda a, r: (np.repeat(a, 3), r * 3))
        self.assertEqual(streamer.out_rate, 48000)
        self.assertEqual(len(y), 3 * len(x))
        self.assertLess(float(np.max(np.abs(y - np.repeat(x, 3)))), 1e-6)

    def test_sola_realigns_a_converter_that_drifts(self):
        # A converter whose output is shifted a few samples compared with its
        # input - models do this - would double or drop audio at every seam
        # with a blind crossfade. SOLA finds the matching offset.
        import numpy as np

        def shifted(a, r):
            return np.concatenate([np.zeros(40, np.float32), a[:-40]]), r

        x, y, _ = self.run_stream(shifted, crossfade_ms=20, search_ms=10)
        # After the first block the output is the input, 40 samples late.
        start = 16000
        self.assertLess(float(np.max(np.abs(y[start:start + 16000] - x[start - 40:start - 40 + 16000]))),
                        1e-6)

    def test_nothing_is_converted_without_a_whole_block(self):
        import numpy as np

        from teto_relay.streaming import BlockStreamer

        calls = []
        streamer = BlockStreamer(lambda a, r: (calls.append(len(a)) or a, r), 16000, block_ms=300)
        self.assertEqual(streamer.feed(np.zeros(4000, np.float32)), [])
        self.assertEqual(calls, [])
        streamer.feed(np.zeros(1000, np.float32))
        self.assertEqual(calls, [4800 + 9600])  # the block plus its context


class TestStreamingVoice(unittest.TestCase):
    """The streaming worker: gate, flush, output and speed warning."""

    def test_push_to_talk_gates_and_flushes(self):
        import threading
        import time

        import numpy as np

        from teto_relay.config import Config
        from teto_relay.streaming import StreamingVoice

        gate = threading.Event()
        received = []
        worker = StreamingVoice(Config(), lambda a, r: (a, r),
                                lambda block, rate: received.append((len(block), rate)), gate.is_set)
        worker.start()
        frame = np.full(320, 0.1, np.float32)
        for _ in range(20):
            worker.frames.put(frame)  # key not held: ignored
        while not worker.frames.empty():
            time.sleep(0.01)
        time.sleep(0.05)  # the last one is being looked at
        gate.set()
        for _ in range(50):  # 1 s held
            worker.frames.put(frame)
        time.sleep(0.3)
        gate.clear()
        worker.frames.put(frame)  # the next frame closes the stream off
        deadline = time.monotonic() + 5
        while sum(n for n, _ in received) < 16000 and time.monotonic() < deadline:
            time.sleep(0.02)
        worker.stop()
        worker.join(timeout=5)
        total = sum(n for n, _ in received)
        # One second in, rounded up to whole 300 ms blocks by the flush.
        self.assertGreaterEqual(total, 16000)
        self.assertLessEqual(total, 16000 + 4800)
        self.assertTrue(all(rate == 16000 for _, rate in received))

    def test_warns_when_conversion_is_slower_than_real_time(self):
        import time

        import numpy as np

        from teto_relay.config import Config
        from teto_relay.streaming import StreamingVoice

        def slow(a, r):
            time.sleep(0.12)
            return a, r

        worker = StreamingVoice(Config(stream_block_ms=100, stream_context_ms=0), slow,
                                lambda b, r: None)
        worker.streamer.feed(np.zeros(1600, np.float32))
        with self.assertLogs("teto_relay.streaming", "WARNING") as logs:
            worker._check_speed()
        self.assertIn("falling behind", logs.output[0])


class TestStreamOutput(unittest.TestCase):
    def test_blocks_are_resampled_continuously_to_the_device_rate(self):
        import types
        import unittest.mock

        import numpy as np

        from teto_relay import playback

        written = []

        class FakeStream:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start(self):
                pass

            def write(self, frames):
                written.append(frames.copy())

            def stop(self):
                pass

            def close(self):
                pass

        fake_sd = types.SimpleNamespace(OutputStream=FakeStream,
                                        query_devices=lambda *a: {"default_samplerate": 48000,
                                                                  "max_output_channels": 8})
        with unittest.mock.patch.object(playback, "sd", lambda: fake_sd):
            out = playback.StreamOutput(device=1)
            for _ in range(10):
                out.write(np.full(4000, 0.2, np.float32), 40000)  # 0.1 s each
            out.close()
        frames = np.concatenate(written)
        self.assertEqual(frames.shape[1], 2)  # capped at stereo
        # 1 s at 40 kHz is about 1 s at 48 kHz, less what soxr still holds.
        self.assertGreater(len(frames), 47000)
        self.assertLessEqual(len(frames), 48000)


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

    def test_installing_an_rvc_index_keeps_live_settings_connected(self):
        import json
        import urllib.parse

        with _TempHome(), _PanelServer() as s:
            live = s.controller.cfg
            name = urllib.parse.quote("teto.index")
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
            conn.request("POST", f"/api/install/rvc?name={name}", body=b"index-bytes",
                         headers={"X-Teto-Relay": "1", "Content-Length": "11"})
            self.assertEqual(json.loads(conn.getresponse().read())["ok"], True)
            conn.close()
            self.assertIs(s.controller.cfg, live)
            self.assertTrue(live.rvc_index.endswith("teto.index"))
            s.request("POST", "/api/config", {"transpose": 7}, {"X-Teto-Relay": "1"})
            self.assertEqual(live.transpose, 7)

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

    def test_streaming_loses_nothing_with_little_or_no_context(self):
        import numpy as np

        from teto_relay.streaming import BlockStreamer

        x = np.random.default_rng(2).standard_normal(32000).astype(np.float32)
        for context in (600, 20, 0):
            streamer = BlockStreamer(lambda a, r: (a, r), 16000, 300, context, 50)
            out = []
            for i in range(0, len(x), 320):
                out += streamer.feed(x[i:i + 320])
            out += streamer.flush()
            y = np.concatenate(out)
            self.assertGreaterEqual(len(y), len(x), context)
            self.assertLess(float(np.max(np.abs(y[:len(x)] - x))), 1e-6, context)

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

    def test_stream_output_stays_closed_after_stop(self):
        import numpy as np

        out, streams = self.fake_output(1.0)
        out.close()
        out.write(np.zeros(160, np.float32), 16000)  # a block finishing after stop
        self.assertEqual(streams, [])

    def test_stream_output_follows_the_volume_setting_live(self):
        import numpy as np

        volume = {"gain": 1.0}
        out, streams = self.fake_output(lambda: volume["gain"])
        out.write(np.full(160, 0.4, np.float32), 16000)
        volume["gain"] = 0.5
        out.write(np.full(160, 0.4, np.float32), 16000)
        self.assertAlmostEqual(float(streams[0].frames[0][0, 0]), 0.4, places=5)
        self.assertAlmostEqual(float(streams[0].frames[1][0, 0]), 0.2, places=5)

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


class TestReReviewFindings(unittest.TestCase):
    """Found by a second review of the review fixes."""

    def test_no_sample_lost_when_the_converter_output_is_a_sample_short(self):
        import numpy as np

        from teto_relay.streaming import BlockStreamer

        def to_44k(a, r):  # frame-based length: floored, like real models
            n = int(len(a) * 44100 / r)
            return np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32), 44100

        streamer = BlockStreamer(to_44k, 16000, block_ms=300, context_ms=49, crossfade_ms=50)
        x = np.random.default_rng(3).standard_normal(16000 * 3).astype(np.float32)
        lengths = []
        for i in range(0, len(x), 320):
            lengths += [len(b) for b in streamer.feed(x[i:i + 320])]
        self.assertTrue(all(n == 13230 for n in lengths[1:]), lengths)

    def test_close_during_a_write_leaves_no_stream_open(self):
        import threading
        import time
        import types
        import unittest.mock

        import numpy as np

        from teto_relay import playback

        opened = []

        class FakeStream:
            def __init__(self, **kw):
                self.closed = False
                opened.append(self)

            def start(self):
                pass

            def write(self, frames):
                pass

            def stop(self):
                pass

            def close(self):
                self.closed = True

        fake_sd = types.SimpleNamespace(OutputStream=FakeStream, query_devices=lambda *a: {
            "default_samplerate": 48000, "max_output_channels": 2})
        with unittest.mock.patch.object(playback, "sd", lambda: fake_sd):
            out = playback.StreamOutput(device=0)
            real = out._resampled

            def slow(*args):
                time.sleep(0.2)  # close() arrives while this block is resampled
                return real(*args)

            out._resampled = slow
            writer = threading.Thread(target=out.write, args=(np.zeros(400, np.float32), 16000))
            writer.start()
            time.sleep(0.05)
            out.close()
            writer.join(timeout=5)
        self.assertTrue(all(s.closed for s in opened), "a stream was left open")


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

    def test_a_supported_compute_type_is_kept(self):
        import types
        import unittest.mock

        from teto_relay.stt import pick_compute_type

        fake_ct2 = types.SimpleNamespace(get_supported_compute_types=lambda d: {"float16", "int8"})
        with unittest.mock.patch.dict(sys.modules, {"ctranslate2": fake_ct2}):
            self.assertEqual(pick_compute_type("cuda", "float16"), "float16")
            self.assertEqual(pick_compute_type("cuda:0", "int8"), "int8")


if __name__ == "__main__":
    unittest.main()
