"""Studio mode: the panel's second mode, a small workspace for making covers.

Like the rest of the suite, none of this needs audio hardware, OpenUtau or a
GPU.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from test_production import _PanelServer, _phrase_doc, _TempHome

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


def _phrase(offset, text="hi", origin=0.0, length=100, rate=1000, level=0.5):
    """A sung line `length` ms long whose recording starts at `offset` s."""
    import numpy as np

    from teto_relay.takes import Phrase

    return Phrase(offset=offset, text=text, doc=_phrase_doc(), sung=np.full(length, level, np.float32),
                  sung_rate=rate, sung_origin=origin, heard=np.full(length, 0.25, np.float32),
                  heard_rate=rate)


def _take(root, phrases):
    """A saved take with these lines, in a folder under `root`."""
    from teto_relay.takes import Take

    take = Take(Path(root))
    take.phrases = list(phrases)
    take.finish()
    return take.folder


class TestTimelineMoves(unittest.TestCase):
    """Studio's timeline: a line is a clip that can be dragged somewhere else."""

    def test_a_clip_goes_to_the_nearest_free_place(self):
        from teto_relay.takes import free_spot

        # Nothing in the way: where it was asked to go.
        self.assertAlmostEqual(free_spot(3.0, 1.0, [(0.0, 1.0), (6.0, 7.0)]), 3.0)
        # Never before 0:00.
        self.assertAlmostEqual(free_spot(-2.0, 1.0, []), 0.0)
        # Dropped on another clip: beside it, on the nearer side. Clips may touch.
        self.assertAlmostEqual(free_spot(1.8, 1.0, [(2.0, 4.0)]), 1.0)
        self.assertAlmostEqual(free_spot(3.4, 1.0, [(2.0, 4.0)]), 4.0)
        # A gap too small to fit in is passed over.
        self.assertAlmostEqual(free_spot(2.1, 1.0, [(0.0, 2.0), (2.5, 4.0)]), 4.0)
        # Pushed against 0:00 with no room before the clip there: after it.
        self.assertAlmostEqual(free_spot(0.0, 1.0, [(0.5, 2.0)]), 2.0)
        # Clips that already overlap each other count as one.
        self.assertAlmostEqual(free_spot(2.8, 1.0, [(1.0, 3.0), (2.0, 5.0)]), 5.0)

    def test_moving_a_line_moves_it_in_the_audio_and_the_project(self):
        import tempfile

        import soundfile as sf
        import yaml

        from teto_relay.takes import move_line, read_info

        with tempfile.TemporaryDirectory() as tmp:
            folder = _take(tmp, [_phrase(0.0, "one"), _phrase(1.0, "two", level=0.4), _phrase(2.0, "three")])
            lines = sorted((folder / "lines").iterdir())
            before = {p.name: p.read_bytes() for p in lines}
            out = move_line(folder, 2, 3.0)

            # The second line is the last one heard now; it keeps its files (n).
            self.assertEqual([(p["n"], p["text"], p["at"]) for p in out["phrases"]],
                             [(1, "one", 0.0), (3, "three", 2.0), (2, "two", 3.0)])
            self.assertEqual([p["at"] for p in read_info(folder)["phrases"]], [0.0, 2.0, 3.0])
            sung, rate = sf.read(str(folder / "teto.wav"))
            self.assertEqual(len(sung), 3100)
            self.assertEqual(float(sung[1050]), 0.0)            # gone from where it was sung
            self.assertAlmostEqual(float(sung[3050]), 0.4, places=3)
            voice, _ = sf.read(str(folder / "your-voice.wav"))
            self.assertEqual(float(voice[1050]), 0.0)
            self.assertAlmostEqual(float(voice[3050]), 0.25, places=3)
            doc = yaml.safe_load((folder / "teto.ustx").read_text(encoding="utf-8"))
            # 960 ticks a second; the part sits 480 ticks into its recording.
            self.assertEqual([p["position"] for p in doc["voice_parts"]], [480, 480 + 1920, 480 + 2880])
            self.assertEqual([p["name"] for p in doc["voice_parts"]], ["1. one", "2. three", "3. two"])
            self.assertEqual(out["length"], 3.1)
            # Nothing was sung again: the lines' own files are untouched.
            self.assertEqual({p.name: p.read_bytes() for p in sorted((folder / "lines").iterdir())}, before)

    def test_a_line_cannot_land_on_another_or_before_the_start(self):
        import tempfile

        import soundfile as sf

        from teto_relay.takes import line_span, move_line

        with tempfile.TemporaryDirectory() as tmp:
            # The second line's singing starts 0.3 s into its recording.
            folder = _take(tmp, [_phrase(0.0, "one", length=500), _phrase(1.0, "two", origin=0.3, length=500)])
            # Dropped in the middle of the first: it ends up touching it.
            out = move_line(folder, 2, 0.0)
            spans = [line_span(folder, p) for p in out["phrases"]]
            self.assertAlmostEqual(spans[1][0], spans[0][1], places=3)
            self.assertAlmostEqual(out["phrases"][1]["at"], 0.2, places=3)
            # The first, dragged far to the left of 0:00, starts at 0:00.
            out = move_line(folder, 1, -30.0)
            self.assertEqual(out["phrases"][0]["at"], 0.0)

        with tempfile.TemporaryDirectory() as tmp:
            # Alone, the line with the quiet lead-in can sing from 0:00: its
            # recording then starts before the take does, and that part of
            # the guide track is cut off rather than pushed later.
            folder = _take(tmp, [_phrase(1.0, "two", origin=0.3, length=500)])
            out = move_line(folder, 1, -5.0)
            self.assertAlmostEqual(out["phrases"][0]["at"], -0.3, places=3)
            self.assertEqual(line_span(folder, out["phrases"][0]), (0.0, 0.5))
            sung, _ = sf.read(str(folder / "teto.wav"))
            self.assertEqual(len(sung), 500)
            voice, _ = sf.read(str(folder / "your-voice.wav"))
            self.assertEqual(len(voice), 200)

    def test_a_line_that_is_not_there_or_a_time_that_is_not_one(self):
        import tempfile

        from teto_relay.takes import move_line, read_info

        with tempfile.TemporaryDirectory() as tmp:
            folder = _take(tmp, [_phrase(0.0, "one")])
            with self.assertRaises(ValueError) as caught:
                move_line(folder, 7, 1.0)
            self.assertIn("no line 7", str(caught.exception))
            for bad in (None, "later", float("nan"), float("inf"), True, [1]):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    move_line(folder, 1, bad)
            self.assertEqual(read_info(folder)["phrases"][0]["at"], 0.0)

    def test_a_take_saved_before_clips_still_has_them(self):
        """Older take.json files do not say how long each line is."""
        import tempfile

        from teto_relay.takes import detail, line_span, move_line, read_info

        with tempfile.TemporaryDirectory() as tmp:
            folder = _take(tmp, [_phrase(0.0, "one", length=400), _phrase(1.0, "two", length=400)])
            info = read_info(folder)
            for line in info["phrases"]:
                del line["length"]
            (folder / "take.json").write_text(json.dumps(info), encoding="utf-8")
            self.assertEqual([p["length"] for p in detail(folder)["phrases"]], [0.4, 0.4])
            self.assertEqual(line_span(folder, info["phrases"][1]), (1.0, 1.4))
            out = move_line(folder, 2, 0.1)
            self.assertAlmostEqual(out["phrases"][1]["at"], 0.4, places=3)

    def test_the_lines_being_recorded_say_where_their_clips_are(self):
        import tempfile

        from teto_relay.takes import Take

        with tempfile.TemporaryDirectory() as tmp:
            take = Take(Path(tmp))
            take.phrases = [_phrase(1.5, "two", origin=0.25, length=300), _phrase(0.0, "one")]
            self.assertEqual([(l["n"], l["at"], l["origin"], l["length"]) for l in take.lines()],
                             [(1, 0.0, 0.0, 0.1), (2, 1.5, 0.25, 0.3)])

    def test_the_panel_moves_a_line(self):
        from teto_relay.takes import read_info

        with _TempHome() as home, _PanelServer() as s:
            folder = _take(home / "takes", [_phrase(0.0, "one"), _phrase(1.0, "two")])
            url = f"/api/take/line/move?take={folder.name}"
            status, body = s.request("POST", url, {"line": 2, "at": 2.5}, TOKEN)
            self.assertEqual(status, 200, body)
            data = json.loads(body)
            self.assertTrue(data["ok"])
            self.assertEqual([p["at"] for p in data["take"]["phrases"]], [0.0, 2.5])
            self.assertEqual(read_info(folder)["phrases"][1]["at"], 2.5)
            # The panel is told the takes changed, without the relay running.
            self.assertEqual(s.controller.takes_changed, 1)
            self.assertIsNone(s.controller.relay)

            for body_in, code in (({"line": 9, "at": 1.0}, 400), ({"line": 2}, 400), ({"line": 2, "at": "x"}, 400)):
                status, body = s.request("POST", url, body_in, TOKEN)
                self.assertEqual(status, code, body)
                self.assertFalse(json.loads(body)["ok"])
            status, _ = s.request("POST", "/api/take/line/move?take=take_nope", {"line": 1, "at": 1.0}, TOKEN)
            self.assertEqual(status, 404)
            # Like everything that changes something, only from the panel itself.
            status, _ = s.request("POST", url, {"line": 2, "at": 4.0})
            self.assertEqual(status, 403)
            self.assertEqual(read_info(folder)["phrases"][1]["at"], 2.5)

    def test_the_page_has_the_timeline(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        for needle in ("api/take/line/move?take=", "function timeline(", "function freeSpot(",
                       "tl-ruler", "tl-music", "tl-voice", "tl-head"):
            self.assertIn(needle, page)


class TestHandsFreeOnAQuietMicrophone(unittest.TestCase):
    """Recording a take hears lines by their level. A headset whose singing
    arrived at 0.013-0.019 (a normal microphone: 0.1 and up) was under the
    fixed 0.015 level most of the time, and 13 s of singing came out as three
    scraps. The level now follows the voice."""

    def setUp(self):
        from teto_relay.capture import PushToTalkChunker
        from teto_relay.config import Config

        self.cfg = Config()
        self.chunker = PushToTalkChunker(self.cfg)

    def tone(self, level):
        """One frame of a sung note whose RMS is `level`."""
        import numpy as np

        t = np.arange(self.cfg.frame_samples) / self.cfg.sample_rate
        return (level * np.sqrt(2) * np.sin(2 * np.pi * 250 * t)).astype(np.float32)

    def feed(self, level, seconds):
        out = []
        for _ in range(int(seconds * 1000 / self.cfg.frame_ms)):
            chunk = self.chunker.push(self.tone(level))
            if chunk is not None:
                out.append(chunk)
        return out

    def test_quiet_singing_is_one_line_not_scraps(self):
        self.feed(0.001, 1.0)                      # the room, before Record
        self.chunker.set_hands_free(True)
        # A line as that headset delivered it: hovering around the old level.
        got = []
        for level in (0.016, 0.013, 0.018, 0.012, 0.017, 0.014):
            got += self.feed(level, 0.5)
        got += self.feed(0.003, 0.3)               # a breath
        got += self.feed(0.015, 1.5)
        self.assertEqual(got, [], "the line is still being sung")
        got = self.feed(0.001, 1.0)
        self.assertEqual(len(got), 1)
        self.assertGreater(got[0].duration, 4.8)
        self.assertLess(got[0].duration, 5.6)
        # The next line, after a real pause, is a line of its own.
        got = self.feed(0.014, 1.0) + self.feed(0.001, 1.0)
        self.assertEqual(len(got), 1)
        self.assertLess(got[0].duration, 1.8)

    def test_a_normal_microphone_is_heard_as_before(self):
        from teto_relay.capture import HANDS_FREE_MIN_LEVEL

        self.feed(0.001, 1.0)
        self.chunker.set_hands_free(True)
        self.assertAlmostEqual(self.chunker._vad.threshold, HANDS_FREE_MIN_LEVEL)
        self.feed(0.2, 1.0)
        # Her voice has been heard: the level is back where it always was.
        self.assertAlmostEqual(self.chunker._vad.threshold, self.cfg.rms_threshold)
        self.assertEqual(len(self.feed(0.001, 1.0)), 1)
        # Music leaking from the headphones, well under the voice, is no line...
        self.assertEqual(self.feed(0.01, 3.0), [])
        self.assertFalse(self.chunker.busy)
        # ...and the voice is remembered for the next take.
        self.chunker.set_hands_free(False)
        self.chunker.set_hands_free(True)
        self.assertAlmostEqual(self.chunker._vad.threshold, self.cfg.rms_threshold)
        self.assertEqual(self.feed(0.01, 3.0), [])

    def test_a_loud_room_still_sets_the_level(self):
        self.feed(0.03, 1.0)                       # music playing in the room
        self.chunker.set_hands_free(True)
        self.assertAlmostEqual(self.chunker._vad.threshold, 0.09, places=3)
        self.assertEqual(self.feed(0.03, 2.0), [])
        # Singing over it is heard, and does not pull the level under the room.
        self.feed(0.2, 1.0)
        self.assertAlmostEqual(self.chunker._vad.threshold, 0.09, places=3)
        self.assertEqual(len(self.feed(0.03, 1.0)), 1)


if __name__ == "__main__":
    unittest.main()
