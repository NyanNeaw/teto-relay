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
        self.home = Path(self.tmp.name)
        self._old = os.environ.get("TETO_RELAY_HOME")
        os.environ["TETO_RELAY_HOME"] = str(self.home)
        return self.home

    def __exit__(self, *exc):
        import os

        if self._old is None:
            os.environ.pop("TETO_RELAY_HOME", None)
        else:
            os.environ["TETO_RELAY_HOME"] = self._old
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
        self.assertNotIn("D:\\", defaults.out_dir)


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
            exe_dir = Path(tmp, "TetoRelay")
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
        self.assertIn("/nowhere/OpenUtau", result.fix)

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


if __name__ == "__main__":
    unittest.main()
