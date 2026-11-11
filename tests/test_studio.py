"""Studio mode: the panel's second mode, a small workspace for making covers.

Like the rest of the suite, none of this needs audio hardware, OpenUtau or a
GPU.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from test_production import _PanelServer

ROOT = Path(__file__).resolve().parent.parent
TOKEN = {"X-Teto-Relay": "1"}


class TestLiveStudioSwitch(unittest.TestCase):
    """The panel has two modes, and opens in the one it was left in."""

    def test_the_mode_is_a_setting_with_two_values(self):
        from teto_relay.config import Config, ConfigError

        self.assertEqual(Config().panel_mode, "live")
        self.assertEqual(Config.from_dict({"panel_mode": "Studio"}).panel_mode, "studio")
        with self.assertRaises(ConfigError) as caught:
            Config.from_dict({"panel_mode": "karaoke"})
        self.assertIn("panel_mode", str(caught.exception))
        self.assertIn("live, studio", str(caught.exception))

    def test_the_mode_survives_a_restart(self):
        import tempfile

        from teto_relay.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = Config()
            cfg.panel_mode = "studio"
            cfg.save(path)
            self.assertEqual(Config.load(path).panel_mode, "studio")

    def test_the_panel_saves_the_mode_and_gives_it_back(self):
        with _PanelServer() as s:
            status, body = s.request("GET", "/api/config")
            self.assertEqual(json.loads(body)["config"]["panel_mode"], "live")
            status, body = s.request("POST", "/api/config", {"panel_mode": "studio"}, TOKEN)
            self.assertEqual(status, 200, body)
            # Switching modes never restarts the relay.
            self.assertEqual(json.loads(body), {"ok": True, "restarting": False, "restart": []})
            status, body = s.request("GET", "/api/config")
            self.assertEqual(json.loads(body)["config"]["panel_mode"], "studio")
            self.assertEqual(json.loads(s.config_path.read_text(encoding="utf-8"))["panel_mode"], "studio")
            status, body = s.request("POST", "/api/config", {"panel_mode": "karaoke"}, TOKEN)
            self.assertEqual(status, 400)
            self.assertIn("panel_mode", json.loads(body)["error"])

    def test_the_mode_is_the_switch_not_a_row_in_the_settings(self):
        with _PanelServer() as s:
            _, body = s.request("GET", "/api/config")
            meta = json.loads(body)["meta"]
            listed = set(meta["essentials"]) | {k for keys in meta["groups"].values() for k in keys}
            self.assertNotIn("panel_mode", listed)

    def test_the_page_has_the_switch_and_the_workspace(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        for needle in ('id="modeSwitch"', 'data-mode="live"', 'data-mode="studio"', "panel_mode",
                       "function setMode(", 'id="sheet"'):
            self.assertIn(needle, page)
        # The workspace is part of the page now, not a sheet floating over it.
        self.assertNotIn("<aside", page)

    def test_a_take_still_stays_off_the_output(self):
        """Studio records the way takes always did: a phrase sung into a take
        is not sent to the main output (CABLE Input)."""
        import inspect

        from teto_relay import app

        source = inspect.getsource(app.TetoRelay._start)
        self.assertIn('quiet=lambda job: bool(getattr(job, "in_take", False))', source)
        self.assertIn("job.in_take = True", inspect.getsource(app.TetoRelay._render_loop))


if __name__ == "__main__":
    unittest.main()
