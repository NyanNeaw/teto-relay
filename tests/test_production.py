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


if __name__ == "__main__":
    unittest.main()
