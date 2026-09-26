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

    def test_paths_cannot_be_set_through_the_panel_api(self):
        with _PanelServer() as s:
            status, _ = s.request("POST", "/api/config",
                                  {"log_file": "C:/Windows/evil.log", "openutau_dir": "X:/"},
                                  self.TOKEN)
            self.assertEqual(status, 200)
            saved = self.saved(s)
            self.assertNotEqual(saved["log_file"], "C:/Windows/evil.log")
            self.assertNotEqual(saved["openutau_dir"], "X:/")

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


if __name__ == "__main__":
    unittest.main()
