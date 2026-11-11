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


def _song(seconds=2.0, rate=22050, channels=2, fmt="WAV", level=0.5):
    """A song as the bytes of an audio file: a steady tone."""
    import io

    import numpy as np
    import soundfile as sf

    t = np.arange(int(seconds * rate)) / rate
    wave = (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, np.stack([wave] * channels, axis=1), rate, format=fmt)
    return buffer.getvalue()


class TestMusicTrack(unittest.TestCase):
    """Studio's music row: the backing track of a cover, kept with its take."""

    def test_a_song_is_kept_in_any_of_its_formats(self):
        import tempfile

        import soundfile as sf

        from teto_relay import music

        for fmt, name in (("WAV", "My Song.wav"), ("FLAC", "song.FLAC"), ("OGG", "song.ogg"), ("MP3", "song.mp3")):
            if fmt not in sf.available_formats():
                continue
            with tempfile.TemporaryDirectory() as tmp:
                found = music.store(_song(fmt=fmt), name, Path(tmp) / "home")
                self.assertEqual(found["name"], name)
                self.assertEqual(found["file"], "music" + Path(name).suffix.lower())
                self.assertAlmostEqual(found["seconds"], 2.0, delta=0.1)
                self.assertEqual((found["rate"], found["channels"]), (22050, 2))
                self.assertEqual(music.info(Path(tmp) / "home"), found)
                audio, rate = music.load(Path(tmp) / "home")
                self.assertEqual((audio.ndim, audio.shape[1], rate), (2, 2, 22050))

    def test_what_is_not_music_is_refused_and_changes_nothing(self):
        import tempfile

        from teto_relay import music

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            music.store(_song(), "first.wav", home)
            for data, name in ((b"MZ\x90\x00 not a song", "virus.exe"), (b"PK\x03\x04", "bank.zip"),
                               (b"these are words, not sound", "notes.mp3"), (b"RIFF....WAVEjunk", "broken.wav"),
                               (b"", "empty.wav"), (_song(), "song")):
                with self.assertRaises(ValueError, msg=name) as caught:
                    music.store(data, name, home)
                self.assertTrue(str(caught.exception))
            self.assertIn("wav, .mp3, .flac or .ogg", music.NOT_AUDIO)
            # The song that was there is still there, and nothing was left behind.
            self.assertEqual(music.info(home)["name"], "first.wav")
            self.assertEqual(sorted(p.name for p in home.iterdir()), ["music.json", "music.wav"])

    def test_a_new_song_takes_the_place_of_the_old_and_can_be_removed(self):
        import tempfile

        import soundfile as sf

        from teto_relay import music

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            music.store(_song(seconds=1.0), "one.wav", home)
            self.assertEqual(len(music.peaks(home)), 50)
            if "FLAC" in sf.available_formats():
                music.store(_song(seconds=3.0, fmt="FLAC"), "two.flac", home)
                self.assertFalse((home / "music.wav").exists())
                self.assertEqual(music.info(home)["name"], "two.flac")
                # Not the first song's waveform, nor its sound.
                self.assertEqual(len(music.peaks(home)), 150)
                self.assertEqual(len(music.load(home)[0]), 3 * 22050)
            self.assertTrue(music.remove(home))
            self.assertIsNone(music.info(home))
            self.assertEqual(list(home.iterdir()), [])
            self.assertFalse(music.remove(home))
            self.assertEqual(music.peaks(home), [])

    def test_the_waveform_is_the_loudest_of_each_stretch(self):
        import numpy as np

        from teto_relay.music import peaks_of

        audio = np.array([0, 0.5, -1.0, 0.25, 0, 0, 0.1, 0.2], np.float32)
        self.assertEqual(peaks_of(audio, 4), [50, 100, 0, 20])
        # Stereo: whichever channel is louder. Never more points than samples.
        self.assertEqual(peaks_of(np.array([[0.1, -0.4], [0.2, 0.0]], np.float32), 9), [100, 50])
        self.assertEqual(peaks_of(np.zeros(10, np.float32), 2), [0, 0])
        self.assertEqual(peaks_of(np.zeros(0, np.float32), 5), [])

    def test_the_waveform_is_worked_out_once(self):
        import tempfile
        import unittest.mock

        from teto_relay import music

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            music.store(_song(seconds=1.0), "one.wav", home)
            first = music.peaks(home)
            self.assertEqual(max(first), 100)
            with unittest.mock.patch.object(music, "load", side_effect=AssertionError("decoded again")):
                self.assertEqual(music.peaks(home), first)

    def test_the_mix_is_her_voice_over_the_music(self):
        import numpy as np

        from teto_relay.music import mix_tracks

        music_audio = np.full((100, 2), 0.25, np.float32)
        music_audio[:, 1] = -0.25
        voice = np.zeros(300, np.float32)
        voice[100:] = 0.5
        mixed, rate = mix_tracks(music_audio, 1000, voice, 2000)
        # At her rate; as long as the longer of the two; the music keeps its sides.
        self.assertEqual((rate, mixed.shape), (2000, (300, 2)))
        self.assertAlmostEqual(float(mixed[100, 0]), 0.75, places=2)
        self.assertAlmostEqual(float(mixed[100, 1]), 0.25, places=2)
        self.assertAlmostEqual(float(mixed[250, 0]), 0.5, places=3)
        # Too loud together: turned down, not clipped.
        loud, _ = mix_tracks(np.full((10, 1), 0.9, np.float32), 1000, np.full(10, 0.9, np.float32), 1000)
        self.assertAlmostEqual(float(np.max(np.abs(loud))), 1.0, places=5)

    def test_a_take_keeps_its_music_and_teto_wav_stays_free_of_it(self):
        import io
        import tempfile
        import zipfile

        import soundfile as sf

        from teto_relay import music
        from teto_relay.takes import Take, detail, listing, move_line, zip_take

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            waiting = music.home(root, "next")
            self.assertEqual(waiting, root / "_next")
            music.store(_song(seconds=1.0, rate=1000), "song.wav", waiting)
            take = Take(root)
            take.phrases = [_phrase(0.2, "one", level=0.25)]
            take.music_from, take.music_id = waiting, "next"
            take.finish()
            self.assertEqual(detail(take.folder)["music"]["name"], "song.wav")
            self.assertEqual(listing(root)[0]["music"]["name"], "song.wav")
            # The music still waits for the take after this one.
            self.assertEqual(music.info(waiting)["name"], "song.wav")
            # Her singing alone, even after the take is put together again.
            move_line(take.folder, 1, 0.5)
            sung, _ = sf.read(str(take.folder / "teto.wav"))
            self.assertEqual(float(abs(sung[:500]).max()), 0.0)
            mixed, rate = sf.read(io.BytesIO(music.mix_wav(take.folder)))
            self.assertEqual((rate, mixed.shape), (1000, (1000, 2)))
            self.assertGreater(float(abs(mixed[100:400]).max()), 0.3)        # the music alone
            self.assertGreater(float(abs(mixed[500:600]).max()), 0.6)        # and her over it
            with zipfile.ZipFile(io.BytesIO(zip_take(take.folder))) as z:
                self.assertIn("music.wav", [Path(n).name for n in z.namelist()])

            # A take without music has none, and no mix.
            plain = Take(root)
            plain.folder = root / "take_20260101_000000"
            plain.phrases = [_phrase(0.0, "one")]
            plain.finish()
            self.assertIsNone(detail(plain.folder)["music"])
            with self.assertRaises(ValueError):
                music.mix_wav(plain.folder)
            # Nothing sung: no take, so nothing to copy the music into.
            empty = Take(root)
            empty.folder = root / "take_20260101_000001"
            empty.music_from = waiting
            self.assertTrue(empty.finish()["discarded"])
            self.assertFalse(empty.folder.exists())

    def test_only_a_take_or_next_holds_music(self):
        import tempfile

        from teto_relay import music

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "take_20260101_000000").mkdir()
            self.assertEqual(music.home(root, "take_20260101_000000"), (root / "take_20260101_000000").resolve())
            for bad in ("", "..", "_next", "take_missing", "take_../x", "next/..", "C:\\"):
                self.assertIsNone(music.home(root, bad), bad)


class TestSingingAlong(unittest.TestCase):
    """While a take records its music plays where only you hear it, and the
    take's clock starts at the music's first sample."""

    def test_the_clock_starts_when_the_first_sample_is_heard(self):
        from teto_relay.music import first_sample_time

        # The driver says the block is heard 30 ms from now.
        self.assertAlmostEqual(first_sample_time(100.0, 5.0, 5.03, 0.2), 100.03)
        # It says nothing (MME often): the stream's stated latency.
        self.assertAlmostEqual(first_sample_time(100.0, 0.0, 0.0, 0.2), 100.2)
        # Nonsense from either is not believed.
        self.assertAlmostEqual(first_sample_time(100.0, 5.0, 99.0, 9.0), 100.0)
        self.assertAlmostEqual(first_sample_time(100.0, 5.0, 4.0, 0.05), 100.05)

    def test_the_music_plays_from_its_first_sample_and_stops(self):
        import time
        import types

        import numpy as np

        from teto_relay.music import Backing, MusicError

        audio = np.arange(10, dtype=np.float32).reshape(5, 2)
        opened = {}

        class Stream:
            latency = 0.04

            def __init__(self, **kwargs):
                opened.update(kwargs)
                self.state = "open"

            def start(self):
                self.state = "started"
                opened["callback"](np.zeros((4, 2), np.float32), 4,
                                   types.SimpleNamespace(currentTime=0.0, outputBufferDacTime=0.0), None)

            def stop(self):
                self.state = "stopped"

            def close(self):
                self.state = "closed"

        backing = Backing(audio, 48000, 3, open_stream=Stream)
        before = time.monotonic()
        started = backing.start()
        self.assertEqual((opened["samplerate"], opened["channels"], opened["device"]), (48000, 2, 3))
        # No clock from the driver: now, plus the stream's latency.
        self.assertGreaterEqual(started, before + 0.04)
        self.assertLess(started, time.monotonic() + 0.05)
        self.assertEqual(backing.position, 4)
        out = np.ones((4, 2), np.float32)
        self.assertFalse(backing.fill(out, 4), "the song is over")
        self.assertEqual(out.tolist(), [[8.0, 9.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]])
        stream = backing._stream
        backing.stop()
        backing.stop()
        self.assertEqual(stream.state, "closed")

        class Silent(Stream):
            def start(self):
                self.state = "started"                     # never asks for sound

        with self.assertRaises(MusicError):
            Backing(audio, 48000, 3, open_stream=Silent).start(wait=0.05)

    def test_the_music_never_plays_into_the_cable(self):
        import types
        import unittest.mock

        import numpy as np

        from teto_relay import music, playback
        from teto_relay.config import Config

        for name in ("CABLE Input (VB-Audio Virtual Cable)", "CABLE Input (VB-Audio Virtual C", "VoiceMeeter Input"):
            with self.assertRaises(music.MusicError) as caught:
                music.refuse_cable(name)
            self.assertIn("Hear myself on", str(caught.exception))
        music.refuse_cable("Headphones (Nubwo X700)")
        music.refuse_cable("")

        def backing(device_name, channels=2, rate=48000):
            fake_sd = types.SimpleNamespace(
                query_devices=lambda *a: {"name": device_name, "default_samplerate": rate,
                                          "max_output_channels": channels})
            with unittest.mock.patch.object(playback, "sd", lambda: fake_sd), \
                    unittest.mock.patch("teto_relay.devices.sd", lambda: fake_sd), \
                    unittest.mock.patch.object(playback, "monitor_device", lambda c: 5), \
                    unittest.mock.patch.object(music, "load", lambda f: (np.zeros((2205, 1), np.float32), 22050)):
                return music.backing_for(Path("anywhere"), Config())

        with self.assertRaises(music.MusicError):
            backing("CABLE Input (VB-Audio Virtual C")
        # Fitted to what the headphones take: their rate, and both ears.
        ready = backing("Headphones (Nubwo X700)")
        self.assertEqual((ready.device, ready.rate, ready.audio.shape), (5, 48000, (4800, 2)))

    def test_a_take_over_music_starts_its_clock_with_the_music(self):
        import queue
        import types

        from test_production import _bare_relay

        from teto_relay.capture import PushToTalkChunker
        from teto_relay.config import Config

        with _TempHome() as home:
            relay = _bare_relay(Config())
            relay._capture = types.SimpleNamespace(chunker=PushToTalkChunker(relay.cfg))
            relay.chunk_q, relay.ustx_q = queue.Queue(), queue.Queue()
            relay._busy_phrases = 0
            calls = []
            fake = types.SimpleNamespace(start=lambda: calls.append("start") or 1234.5,
                                         stop=lambda: calls.append("stop"))
            relay._backing_for = lambda folder: calls.append(("for", folder)) or fake
            take = relay.start_take(home / "takes" / "_next", "next")
            self.assertEqual(calls, [("for", home / "takes" / "_next"), "start"])
            self.assertEqual(take.started, 1234.5)
            self.assertEqual((take.music_from, take.music_id), (home / "takes" / "_next", "next"))
            self.assertTrue(relay._capture.chunker.hands_free)
            # Pressing Record again changes nothing; stopping stops the music.
            self.assertIs(relay.start_take(home / "takes" / "_next", "next"), take)
            self.assertTrue(relay.stop_take(wait=0.5)["discarded"])
            self.assertEqual(calls[-1], "stop")
            self.assertIsNone(relay._backing)

            # Music that cannot be played: no take is started at all.
            from teto_relay.music import MusicError

            def refuse(folder):
                raise MusicError("no headphones")

            relay._backing_for = refuse
            with self.assertRaises(MusicError):
                relay.start_take(home / "takes" / "_next", "next")
            self.assertIsNone(relay._take)
            self.assertFalse(relay._capture.chunker.hands_free)

    def test_the_panel_keeps_serves_and_removes_music(self):
        import http.client
        import io

        import soundfile as sf

        from teto_relay import music

        def upload(s, take, name, data, token=True):
            conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
            conn.request("POST", f"/api/take/music?take={take}&name={name}", body=data,
                         headers=TOKEN if token else {})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            return resp.status, json.loads(body)

        with _TempHome() as home, _PanelServer() as s:
            # Nothing waiting yet.
            status, body = s.request("GET", "/api/take/music?take=next")
            self.assertEqual((status, json.loads(body)), (200, {"music": None}))
            status, _ = s.request("GET", "/api/take/music/sound?take=next")
            self.assertEqual(status, 404)

            status, data = upload(s, "next", "My%20Song.wav", _song(seconds=1.0))
            self.assertEqual(status, 200, data)
            self.assertEqual(data["music"]["name"], "My Song.wav")
            self.assertEqual(len(data["music"]["peaks"]), 50)
            self.assertTrue((home / "takes" / "_next" / "music.wav").is_file())
            status, body = s.request("GET", "/api/take/music?take=next")
            self.assertEqual(json.loads(body)["music"]["peaks"], data["music"]["peaks"])
            status, body = s.request("GET", "/api/take/music/sound?take=next")
            self.assertEqual(status, 200)
            heard, rate = sf.read(io.BytesIO(body))
            self.assertEqual((rate, heard.shape), (22050, (22050, 2)))

            # Not music: said clearly, and the song stays.
            status, data = upload(s, "next", "notes.mp3", b"these are words")
            self.assertEqual(status, 400)
            self.assertEqual(data["error"], music.NOT_AUDIO)
            status, data = upload(s, "next", "bank.zip", b"PK\x03\x04")
            self.assertEqual((status, data["error"]), (400, music.NOT_AUDIO))
            self.assertEqual(music.info(home / "takes" / "_next")["name"], "My Song.wav")
            # Only the panel may add it, and only to a real take.
            status, _ = upload(s, "next", "x.wav", _song(), token=False)
            self.assertEqual(status, 403)
            status, _ = upload(s, "take_nope", "x.wav", _song())
            self.assertEqual(status, 404)

            # A saved take: its own music, the mix, and the take's detail.
            folder = _take(home / "takes", [_phrase(0.0, "one")])
            status, body = s.request("GET", f"/api/take/file?take={folder.name}&file=mix.wav")
            self.assertEqual(status, 404, "no music, no mix")
            status, data = upload(s, folder.name, "song.wav", _song(seconds=1.0, rate=1000))
            self.assertEqual(status, 200, data)
            status, body = s.request("GET", f"/api/take?take={folder.name}")
            self.assertEqual(json.loads(body)["music"]["name"], "song.wav")
            conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
            conn.request("GET", f"/api/take/file?take={folder.name}&file=mix.wav")
            resp = conn.getresponse()
            mixed, rate = sf.read(io.BytesIO(resp.read()))
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertIn(f"{folder.name}-mix.wav", resp.getheader("Content-Disposition"))
            self.assertEqual((rate, mixed.shape), (1000, (1000, 2)))

            status, body = s.request("POST", f"/api/take/music/delete?take={folder.name}", None, TOKEN)
            self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
            status, body = s.request("GET", f"/api/take?take={folder.name}")
            self.assertIsNone(json.loads(body)["music"])
            self.assertEqual(music.info(home / "takes" / "_next")["name"], "My Song.wav")

    def test_record_names_its_music_and_needs_the_relay(self):
        import types

        from teto_relay import music

        with _TempHome() as home, _PanelServer() as s:
            status, body = s.request("POST", "/api/take/start", {"music": "next"}, TOKEN)
            self.assertEqual(status, 400)
            self.assertIn("Start", json.loads(body)["error"])

            started = []
            s.controller.relay = types.SimpleNamespace(
                start_take=lambda *a: started.append(a), _take=None, stop=lambda: None,
                take_status=lambda: {"recording": False})
            # Music that is not there is said, and nothing is recorded.
            status, body = s.request("POST", "/api/take/start", {"music": "next"}, TOKEN)
            self.assertEqual(status, 400)
            self.assertIn("music is gone", json.loads(body)["error"])
            self.assertEqual(started, [])
            music.store(_song(), "song.wav", home / "takes" / "_next")
            status, body = s.request("POST", "/api/take/start", {"music": "next"}, TOKEN)
            self.assertEqual(status, 200, body)
            self.assertEqual(started, [(home / "takes" / "_next", "next")])
            # No music named (the mini player's button): a plain take, as before.
            status, _ = s.request("POST", "/api/take/start", None, TOKEN)
            self.assertEqual(status, 200)
            self.assertEqual(started[-1], ())
            s.controller.relay = None

    def test_the_page_has_the_music_row(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        for needle in ("api/take/music?take=", "api/take/music/sound?take=", "api/take/music/delete?take=",
                       "mix.wav", "function addMusic(", "headphones", "tl-wave"):
            self.assertIn(needle, page)


class TestRecordingDelay(unittest.TestCase):
    """Studio's delay fix: a line sung on the beat is stamped late by the
    sound's round trip; that is measured with clicks and taken off."""

    def heard(self, seconds, gain=0.05, noise=0.01, total=5.0, rate=None, seed=1):
        """The clicks as a poor microphone hears them `seconds` late: quiet,
        upside down, dull, and in a noisy room."""
        import numpy as np

        from teto_relay import delay

        rng = np.random.default_rng(seed)
        clicks = delay.click_track()
        recorded = rng.normal(0, noise, int(total * delay.RATE))
        start = int(round(seconds * delay.RATE))
        if start >= 0:
            recorded[start:start + len(clicks)] -= gain * clicks[:len(recorded) - start]
        else:
            recorded[:len(clicks) + start] -= gain * clicks[-start:]
        return np.convolve(recorded, np.ones(4) / 4, mode="same")

    def test_the_clicks_are_short_and_unevenly_spaced(self):
        import numpy as np

        from teto_relay import delay

        clicks = delay.click_track()
        self.assertEqual(clicks.dtype, np.float32)
        self.assertLessEqual(float(np.abs(clicks).max()), 0.6)
        self.assertEqual(len(clicks), int(round((2.66 + 0.03 + 0.5) * delay.RATE)))
        # No two clicks the same distance apart: only one delay lines them all up.
        times = delay.CLICK_TIMES
        gaps = [round(b - a, 3) for i, a in enumerate(times) for b in times[i + 1:]]
        self.assertEqual(len(gaps), len(set(gaps)))

    def test_the_delay_is_found_by_lining_the_clicks_up(self):
        import numpy as np

        from teto_relay import delay

        clicks = delay.click_track()
        for seconds in (0.0, 0.037, 0.137, 0.412, 1.2, -0.05):
            found, strength = delay.estimate_delay(clicks, self.heard(seconds), delay.RATE)
            self.assertAlmostEqual(found, seconds, delta=0.001, msg=seconds)
            self.assertGreater(strength, delay.HEARD, seconds)
        # Clicks five times quieter, under the room's noise, are still found.
        found, strength = delay.estimate_delay(clicks, self.heard(0.137, gain=0.01), delay.RATE)
        self.assertAlmostEqual(found, 0.137, delta=0.001)
        self.assertGreater(strength, delay.HEARD)
        # A room with no clicks in it, or a steady note, is not mistaken for them.
        rng = np.random.default_rng(7)
        for trial in range(5):
            _, strength = delay.estimate_delay(clicks, rng.normal(0, 0.02, 5 * delay.RATE), delay.RATE)
            self.assertLess(strength, delay.HEARD)
        note = 0.3 * np.sin(2 * np.pi * 440 * np.arange(5 * delay.RATE) / delay.RATE)
        self.assertLess(delay.estimate_delay(clicks, note, delay.RATE)[1], delay.HEARD)
        self.assertEqual(delay.estimate_delay(clicks, np.zeros(0), delay.RATE), (0.0, 0.0))
        self.assertEqual(delay.estimate_delay(clicks, np.zeros(8000), delay.RATE), (0.0, 0.0))

    def test_the_two_clocks_are_lined_up_first(self):
        import numpy as np

        from teto_relay import delay
        from teto_relay.playback import _resample

        # Blocks of 20 ms arriving in fits and starts: the middle one counts.
        stamps = [100.02, 100.04, 100.09, 100.08, 100.10]
        self.assertAlmostEqual(delay.recording_start(stamps, [320] * 5, 16000), 100.0, places=6)
        # A 48 kHz microphone that began recording 0.25 s before the clicks
        # were handed to the headphones, and heard them 90 ms after that.
        recorded = _resample(self.heard(0.25 + 0.09)[:, None].astype(np.float32), delay.RATE, 48000)[:, 0]
        ms, strength = delay.solve(recorded, 48000, started=500.0, heard_at=500.25)
        self.assertAlmostEqual(ms, 90.0, delta=1.5)
        self.assertGreater(strength, delay.HEARD)
        # The driver said later than it was: the clicks are found before "zero".
        ms, _ = delay.solve(self.heard(0.25 - 0.04), delay.RATE, started=500.0, heard_at=500.25)
        self.assertAlmostEqual(ms, -40.0, delta=1.5)

    def measure(self, true_delay, mic_latency=0.0, gain=0.05, device="Headphones (Nubwo X700)"):
        """delay.measure in a made-up world: headphones that sound
        `true_delay` s after the driver says, a microphone `mic_latency` late."""
        import types
        import unittest.mock

        import numpy as np

        from teto_relay import capture, delay, devices, music, playback
        from teto_relay.config import Config

        world = types.SimpleNamespace(now=1000.0, callback=None, clicks_at=None)
        clicks = delay.click_track()
        rng = np.random.default_rng(3)

        def sound(at, frames):
            """What reaches the microphone from `at` on."""
            out = rng.normal(0, 0.01, frames)
            if world.clicks_at is not None:
                first = int(round((at - world.clicks_at) * delay.RATE))
                a, b = max(0, first), min(len(clicks), first + frames)
                if b > a:
                    out[a - first:b - first] += gain * clicks[a:b]
            return out.astype(np.float32)

        def sleep(seconds):
            for _ in range(int(round(seconds / 0.02))):
                world.now += 0.02
                if world.callback is not None:
                    block = sound(world.now - 0.02 - mic_latency, 320)
                    world.callback(block[:, None], 320, None, None)

        class Input:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def __enter__(self):
                world.callback = self.kwargs["callback"]

            def __exit__(self, *exc):
                world.callback = None

        class Backing:
            def __init__(self, audio, rate, device_index):
                world.played = (audio.shape, rate, device_index)

            def start(self):
                world.clicks_at = world.now + 0.05 + true_delay    # 50 ms: the latency the driver admits to
                return world.now + 0.05

            def stop(self):
                world.stopped = True

        fake_sd = types.SimpleNamespace(
            InputStream=Input, check_input_settings=lambda **k: None,
            query_devices=lambda *a: {"name": device, "default_samplerate": 48000, "max_output_channels": 2})
        with unittest.mock.patch.object(delay, "time", types.SimpleNamespace(monotonic=lambda: world.now, sleep=sleep)), \
                unittest.mock.patch.object(devices, "sd", lambda: fake_sd), \
                unittest.mock.patch.object(capture, "sd", lambda: fake_sd), \
                unittest.mock.patch.object(playback, "sd", lambda: fake_sd), \
                unittest.mock.patch.object(playback, "monitor_device", lambda c: 4), \
                unittest.mock.patch.object(devices, "resolve_input", lambda c: None), \
                unittest.mock.patch.object(devices, "default_input", lambda: None), \
                unittest.mock.patch.object(music, "Backing", Backing):
            return delay.measure(Config()), world

    def test_measuring_plays_clicks_and_hears_them_back(self):
        from teto_relay import delay, music

        found, world = self.measure(true_delay=0.085, mic_latency=0.04)
        # What a take is off by: the headphones' own lag and the microphone's.
        self.assertAlmostEqual(found["ms"], 125, delta=2)
        self.assertGreater(found["strength"], delay.HEARD)
        self.assertEqual(found["output"], "Headphones (Nubwo X700)")
        # The clicks went to the Hear myself device, at its rate, on both ears.
        self.assertEqual(world.played, ((len(delay.click_track()) * 3, 2), 48000, 4))
        self.assertTrue(world.stopped)
        # Bluetooth: a long delay is measured just the same.
        found, _ = self.measure(true_delay=0.31)
        self.assertAlmostEqual(found["ms"], 310, delta=2)

        # A microphone that cannot hear the headphones: said, with what to do.
        with self.assertRaises(delay.DelayError) as caught:
            self.measure(true_delay=0.085, gain=0.0)
        self.assertIn("Hold one side of your headphones against the microphone", str(caught.exception))
        # And never into the cable.
        with self.assertRaises(music.MusicError):
            self.measure(true_delay=0.085, device="CABLE Input (VB-Audio Virtual C")

    def test_a_new_line_is_placed_earlier_by_the_delay(self):
        import tempfile
        import types

        import numpy as np
        import soundfile as sf
        import yaml

        from teto_relay.takes import Take

        with tempfile.TemporaryDirectory() as tmp:
            wav, ustx = Path(tmp) / "p.wav", Path(tmp) / "p.ustx"
            sf.write(str(wav), np.zeros(100, np.float32), 1000)
            ustx.write_text(yaml.safe_dump(_phrase_doc()), encoding="utf-8")
            take = Take(Path(tmp) / "takes")
            take.delay = 0.12

            def job(start):
                return types.SimpleNamespace(audio_start=start, wav_path=wav, ustx_path=ustx, text="x",
                                             wav_origin=0.0, input_audio=np.zeros(10, np.float32),
                                             input_rate=16000)

            take.add(job(take.started + 2.0))
            take.add(job(take.started + 0.05))      # sung at once: not before 0:00
            self.assertAlmostEqual(take.phrases[0].offset, 1.88)
            self.assertEqual(take.phrases[1].offset, 0.0)

    def test_the_delay_is_a_setting_the_next_take_uses(self):
        import queue
        import types

        from test_production import _bare_relay

        from teto_relay.capture import PushToTalkChunker
        from teto_relay.config import Config, ConfigError

        self.assertEqual(Config().studio_offset_ms, 0.0)
        self.assertEqual(Config.from_dict({"studio_offset_ms": "85"}).studio_offset_ms, 85.0)
        with self.assertRaises(ConfigError) as caught:
            Config.from_dict({"studio_offset_ms": 5000})
        self.assertIn("studio_offset_ms", str(caught.exception))

        with _TempHome():
            relay = _bare_relay(Config(studio_offset_ms=85))
            relay._capture = types.SimpleNamespace(chunker=PushToTalkChunker(relay.cfg))
            relay.chunk_q, relay.ustx_q = queue.Queue(), queue.Queue()
            relay._busy_phrases = 0
            self.assertAlmostEqual(relay.start_take().delay, 0.085)
            relay.stop_take(wait=0.5)

        with _PanelServer() as s:
            status, body = s.request("POST", "/api/config", {"studio_offset_ms": -30}, TOKEN)
            self.assertEqual(status, 200, body)
            self.assertEqual(s.controller.cfg.studio_offset_ms, -30.0)
            _, body = s.request("GET", "/api/config")
            data = json.loads(body)
            self.assertEqual(data["config"]["studio_offset_ms"], -30.0)
            # It lives in Studio, beside Calibrate: not a row in the settings.
            listed = set(data["meta"]["essentials"]) | {k for keys in data["meta"]["groups"].values() for k in keys}
            self.assertNotIn("studio_offset_ms", listed)
            status, _ = s.request("POST", "/api/config", {"studio_offset_ms": 99999}, TOKEN)
            self.assertEqual(status, 400)

    def test_calibrate_saves_what_it_measured(self):
        import types
        import unittest.mock

        from teto_relay import delay
        from teto_relay.config import Config

        with _PanelServer() as s:
            found = {"ms": 112, "strength": 6.2, "output": "Headphones", "input": "Microphone"}
            with unittest.mock.patch.object(delay, "measure", lambda cfg: dict(found)):
                status, body = s.request("POST", "/api/studio/calibrate", None, TOKEN)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body), {"ok": True, **found})
            self.assertEqual(Config.load().studio_offset_ms, 112.0)
            self.assertEqual(s.controller.cfg.studio_offset_ms, 112.0)

            # It could not be measured: said, and the setting is left alone.
            def unheard(cfg):
                raise delay.DelayError("The microphone did not hear the clicks.")

            with unittest.mock.patch.object(delay, "measure", unheard):
                status, body = s.request("POST", "/api/studio/calibrate", None, TOKEN)
            self.assertEqual(status, 400)
            self.assertIn("did not hear", json.loads(body)["error"])
            with unittest.mock.patch.object(delay, "measure", lambda cfg: {**found, "ms": 9000}):
                status, body = s.request("POST", "/api/studio/calibrate", None, TOKEN)
            self.assertEqual(status, 400)
            self.assertEqual(Config.load().studio_offset_ms, 112.0)
            # Not while a take records: the clicks would be a line of it.
            s.controller.relay = types.SimpleNamespace(_take=object(), stop=lambda: None, stop_take=lambda **k: None)
            with unittest.mock.patch.object(delay, "measure", lambda cfg: self.fail("measured during a take")):
                status, body = s.request("POST", "/api/studio/calibrate", None, TOKEN)
            self.assertEqual(status, 400)
            self.assertIn("Stop recording first", json.loads(body)["error"])
            s.controller.relay = None
            status, _ = s.request("POST", "/api/studio/calibrate")
            self.assertEqual(status, 403)

    def test_all_the_clips_of_a_take_shift_together(self):
        import tempfile

        import soundfile as sf

        from teto_relay.takes import read_info, shift_lines

        with tempfile.TemporaryDirectory() as tmp:
            folder = _take(tmp, [_phrase(0.5, "one"), _phrase(2.0, "two", origin=0.1)])
            out, moved = shift_lines(folder, -120)
            self.assertEqual(moved, -120.0)
            self.assertEqual([p["at"] for p in out["phrases"]], [0.38, 1.88])
            sung, _ = sf.read(str(folder / "teto.wav"))
            self.assertEqual(float(sung[370]), 0.0)
            self.assertAlmostEqual(float(sung[390]), 0.5, places=3)
            self.assertAlmostEqual(float(sung[1990]), 0.5, places=3)      # 1.88 + its 0.1 lead-in
            # Later is the same, the other way.
            out, moved = shift_lines(folder, 70)
            self.assertEqual(moved, 70.0)
            self.assertEqual([p["at"] for p in out["phrases"]], [0.45, 1.95])
            # Further left than there is room: all of them go as far as the
            # first can, so they stay the same distance apart.
            out, moved = shift_lines(folder, -5000)
            self.assertEqual(moved, -450.0)
            self.assertEqual([p["at"] for p in out["phrases"]], [0.0, 1.5])
            out, moved = shift_lines(folder, -50)
            self.assertEqual(moved, 0.0)
            for bad in (None, "soon", float("nan"), True, 10 ** 9):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    shift_lines(folder, bad)
            self.assertEqual([p["at"] for p in read_info(folder)["phrases"]], [0.0, 1.5])

    def test_the_panel_shifts_a_take(self):
        from teto_relay.takes import read_info

        with _TempHome() as home, _PanelServer() as s:
            folder = _take(home / "takes", [_phrase(0.5, "one"), _phrase(2.0, "two")])
            url = f"/api/take/shift?take={folder.name}"
            status, body = s.request("POST", url, {"ms": -100}, TOKEN)
            self.assertEqual(status, 200, body)
            data = json.loads(body)
            self.assertEqual((data["ok"], data["moved"]), (True, -100.0))
            self.assertEqual([p["at"] for p in data["take"]["phrases"]], [0.4, 1.9])
            self.assertEqual([p["at"] for p in read_info(folder)["phrases"]], [0.4, 1.9])
            for bad, code in (({"ms": "x"}, 400), ({}, 400)):
                status, _ = s.request("POST", url, bad, TOKEN)
                self.assertEqual(status, code)
            status, _ = s.request("POST", "/api/take/shift?take=take_nope", {"ms": 5}, TOKEN)
            self.assertEqual(status, 404)
            status, _ = s.request("POST", url, {"ms": 5})
            self.assertEqual(status, 403)

    def test_the_page_has_the_delay_box(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        for needle in ("api/studio/calibrate", "api/take/shift?take=", "studio_offset_ms", "Calibrate",
                       "Bluetooth", "function delayBox("):
            self.assertIn(needle, page)


class TestStylePresets(unittest.TestCase):
    """A preset: a way of singing in one small file that can be shared."""

    def preset(self, **settings):
        return {"format": 1, "name": "Mine", "author": "me", "description": "a test",
                "settings": settings or {"singing_style": "sung", "vibrato_depth_cents": 40}}

    def test_a_preset_holds_only_how_she_sings(self):
        from dataclasses import fields

        from teto_relay import presets
        from teto_relay.config import Config

        known = {f.name for f in fields(Config)}
        self.assertTrue(set(presets.SETTINGS) <= known)
        for needed in ("singing_style", "scale", "scale_key", "sung_contour_amount", "sung_melody_range",
                       "vibrato_min_seconds", "vibrato_depth_cents", "vibrato_period_ms",
                       "final_hold_seconds", "double_voice", "double_when"):
            self.assertIn(needed, presets.SETTINGS)
        # Never a device, a path, a key or the language.
        for never in ("input_device", "output_device", "monitor_device", "ptt_key", "language", "voicebank",
                      "voicebank_root", "openutau_dir", "out_dir", "log_file", "lyrics_hint", "panel_mode",
                      "studio_offset_ms", "whisper_model", "playback_gain"):
            self.assertNotIn(never, presets.SETTINGS)

    def test_reading_keeps_what_is_on_the_list_and_ignores_the_rest(self):
        from teto_relay import presets

        read = presets.validate({
            "format": 1, "name": "  Wide   and\nslow ", "author": "someone", "description": "",
            "made_with": "a newer app", "settings": {
                "singing_style": "SUNG", "vibrato_depth_cents": "55", "legato": "false",
                "output_device": "Speakers", "voicebank_root": "C:\\Windows", "ptt_key": "f1",
                "language": "ja", "log_file": "C:\\evil.log", "a_setting_from_the_future": 3}})
        self.assertEqual(read, {"format": 1, "name": "Wide and slow", "author": "someone", "description": "",
                                "settings": {"singing_style": "sung", "vibrato_depth_cents": 55.0,
                                             "legato": False}})

    def test_a_bad_file_says_why(self):
        from teto_relay import presets

        bad = {
            "not JSON": (b"this is not a preset", "not a preset file"),
            "not an object": (b"[1, 2, 3]", "not a preset file"),
            "no format": (json.dumps({"name": "x", "settings": {"legato": True}}).encode(), "format"),
            "a newer format": (json.dumps({**self.preset(), "format": 2}).encode(), "newer Teto Relay"),
            "no name": (json.dumps({**self.preset(), "name": "  "}).encode(), "no name"),
            "a long name": (json.dumps({**self.preset(), "name": "x" * 61}).encode(), "longer than 60"),
            "no settings": (json.dumps({"format": 1, "name": "x"}).encode(), "no settings"),
            "nothing usable": (json.dumps(self.preset(output_device="x")).encode(), "no singing settings"),
            "out of range": (json.dumps(self.preset(vibrato_depth_cents=900)).encode(),
                             "vibrato_depth_cents is 900.0; it must be between 0.0 and 200.0"),
            "not a choice": (json.dumps(self.preset(scale="blues")).encode(), "scale is 'blues'"),
            "the wrong type": (json.dumps(self.preset(legato="maybe")).encode(), "legato is 'maybe'"),
            "not a key": (json.dumps(self.preset(scale_key="H")).encode(), "H"),
            "too big": (b" " * 70_000, "too big"),
            "not text": (b"\xff\xfe\x00\x01", "not a preset file"),
        }
        for what, (raw, reason) in bad.items():
            with self.assertRaises(presets.PresetError, msg=what) as caught:
                presets.parse(raw)
            self.assertIn(reason, str(caught.exception), what)
        # Every problem at once, as the settings file reports them.
        with self.assertRaises(presets.PresetError) as caught:
            presets.parse(json.dumps(self.preset(vibrato_depth_cents=900, final_hold_seconds=-1)).encode())
        self.assertIn("vibrato_depth_cents", str(caught.exception))
        self.assertIn("final_hold_seconds", str(caught.exception))

    def test_four_presets_are_built_in_and_differ(self):
        from teto_relay import presets
        from teto_relay.config import Config

        self.assertEqual(list(presets.BUILTIN), ["just-like-you", "pop-singer", "robot", "diva"])
        seen = []
        for key, preset in presets.BUILTIN.items():
            read = presets.validate(preset)
            # Each sets everything, so picking one never depends on the last.
            self.assertEqual(set(read["settings"]), set(presets.SETTINGS), key)
            self.assertTrue(read["name"] and read["description"])
            self.assertNotIn(read["settings"], seen)
            seen.append(read["settings"])
        # A fresh install sings as the first one says.
        self.assertTrue(presets.matches(presets.BUILTIN["just-like-you"], Config()))
        self.assertEqual(presets.BUILTIN["robot"]["settings"]["vibrato_depth_cents"], 0.0)
        self.assertGreater(presets.BUILTIN["diva"]["settings"]["vibrato_depth_cents"],
                           presets.BUILTIN["pop-singer"]["settings"]["vibrato_depth_cents"])

    def test_export_change_import_brings_the_settings_back(self):
        from teto_relay import presets
        from teto_relay.config import Config

        cfg = Config(singing_style="sung", scale="minor", scale_key="F#", vibrato_depth_cents=45.0,
                     final_hold_seconds=0.9, double_voice=0.2, legato=False)
        text = presets.dumps(presets.from_config(cfg, "My style", "me", "how I like it"))
        back = presets.parse(text)
        self.assertEqual((back["name"], back["author"], back["description"]), ("My style", "me", "how I like it"))
        other = Config()
        self.assertFalse(presets.matches(back, other))
        for key, value in back["settings"].items():
            setattr(other, key, value)
        for key in presets.SETTINGS:
            self.assertEqual(getattr(other, key), getattr(cfg, key), key)
        self.assertTrue(presets.matches(back, other))

    def test_your_presets_are_kept_listed_and_deleted(self):
        from teto_relay import presets
        from teto_relay.config import Config

        with _TempHome() as home:
            first = presets.listing(Config())
            self.assertEqual([s["id"] for s in first["styles"]],
                             ["builtin:just-like-you", "builtin:pop-singer", "builtin:robot", "builtin:diva"])
            self.assertEqual(first["active"], "builtin:just-like-you")
            saved = presets.save(self.preset())
            self.assertEqual(saved, "user:mine")
            self.assertTrue((home / "styles" / "mine.tetostyle").is_file())
            # A name from someone else's file cannot name a file outside the folder.
            sneaky = presets.save({**self.preset(legato=False), "name": "..\\..\\Start Menu/evil"})
            self.assertEqual(sneaky, "user:start-menu-evil")
            thai = presets.save({**self.preset(scale="minor"), "name": "เสียงหวาน"})
            self.assertRegex(thai, r"^user:preset-[0-9a-f]{10}$")
            self.assertEqual(sorted(p.name for p in (home / "styles").iterdir()),
                             sorted(["mine.tetostyle", "start-menu-evil.tetostyle", thai[5:] + ".tetostyle"]))
            # A file in the folder that is not a preset is passed over.
            (home / "styles" / "broken.tetostyle").write_text("{", encoding="utf-8")
            listed = presets.listing(Config(singing_style="sung", vibrato_depth_cents=40.0))
            self.assertEqual(len(listed["styles"]), 7)
            self.assertFalse(listed["styles"][4]["builtin"])
            self.assertEqual(listed["active"], "user:mine")
            self.assertEqual(presets.get("user:mine")["settings"]["vibrato_depth_cents"], 40.0)
            self.assertEqual(presets.get("builtin:robot")["name"], "Robot")
            presets.delete("user:mine")
            for gone in ("user:mine", "user:../mine", "builtin:nope", "", "user:"):
                with self.assertRaises(presets.PresetError, msg=gone):
                    presets.get(gone)
            with self.assertRaises(presets.PresetError):
                presets.delete("builtin:robot")
            self.assertEqual(presets.listing(Config(singing_style="sung", vibrato_depth_cents=40.0))["active"], "")

    def test_the_panel_applies_saves_exports_and_imports(self):
        import http.client

        from teto_relay import presets
        from teto_relay.config import Config

        def post_file(s, path, data, token=True):
            conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
            conn.request("POST", path, body=data, headers=TOKEN if token else {})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            return resp.status, json.loads(body)

        with _TempHome() as home, _PanelServer() as s:
            _, body = s.request("GET", "/api/styles")
            listed = json.loads(body)
            self.assertEqual(len(listed["styles"]), 4)
            self.assertEqual(listed["active"], "builtin:just-like-you")
            self.assertEqual(listed["settings"], list(presets.SETTINGS))

            # Pick a built-in one: saved, and in force for a running relay.
            s.request("POST", "/api/config", {"output_device": "My speakers", "language": "th"}, TOKEN)
            status, body = s.request("POST", "/api/style/apply", {"id": "builtin:diva"}, TOKEN)
            self.assertEqual(status, 200, body)
            data = json.loads(body)
            self.assertEqual((data["ok"], data["name"], data["config"]["scale"]), (True, "Diva", "minor"))
            saved = Config.load()
            self.assertEqual((saved.singing_style, saved.vibrato_depth_cents), ("sung", 70.0))
            self.assertEqual(s.controller.cfg.vibrato_depth_cents, 70.0)
            # Nothing else was touched.
            self.assertEqual((saved.output_device, saved.language), ("My speakers", "th"))
            self.assertEqual(json.loads(s.request("GET", "/api/styles")[1])["active"], "builtin:diva")

            # Change something and save it as a preset of your own.
            s.request("POST", "/api/config", {"final_hold_seconds": 2.0}, TOKEN)
            self.assertEqual(json.loads(s.request("GET", "/api/styles")[1])["active"], "")
            status, body = s.request("POST", "/api/style/save",
                                     {"name": "Long notes", "author": "me", "description": "held"}, TOKEN)
            self.assertEqual((status, json.loads(body)["id"]), (200, "user:long-notes"))
            self.assertEqual(json.loads(s.request("GET", "/api/styles")[1])["active"], "user:long-notes")
            status, body = s.request("POST", "/api/style/save", {"name": ""}, TOKEN)
            self.assertEqual(status, 400)
            self.assertIn("no name", json.loads(body)["error"])

            # Export it: a .tetostyle file to download.
            conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
            conn.request("GET", "/api/style/export?id=user:long-notes")
            resp = conn.getresponse()
            exported = resp.read()
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertIn('filename="long-notes.tetostyle"', resp.getheader("Content-Disposition"))
            self.assertEqual(json.loads(exported)["settings"]["final_hold_seconds"], 2.0)
            self.assertEqual(s.request("GET", "/api/style/export?id=user:nope")[0], 404)

            # The done-when: change settings, import the file, and they are back.
            s.request("POST", "/api/style/apply", {"id": "builtin:robot"}, TOKEN)
            s.request("POST", "/api/style/delete", {"id": "user:long-notes"}, TOKEN)
            self.assertEqual(Config.load().final_hold_seconds, 0.0)
            status, data = post_file(s, "/api/style/import", exported)
            self.assertEqual(status, 200, data)
            self.assertEqual((data["id"], data["name"]), ("user:long-notes", "Long notes"))
            back = Config.load()
            self.assertEqual((back.final_hold_seconds, back.scale, back.vibrato_depth_cents), (2.0, "minor", 70.0))
            self.assertEqual(s.controller.cfg.final_hold_seconds, 2.0)
            self.assertEqual(json.loads(s.request("GET", "/api/styles")[1])["active"], "user:long-notes")

            # A bad file: a clear message, and nothing changes or is kept.
            before = s.config_path.read_bytes()
            kept = sorted(p.name for p in (home / "styles").iterdir())
            for raw, reason in ((b"MZ not a preset", "not a preset file"),
                                (json.dumps(self.preset(vibrato_depth_cents=900)).encode(), "vibrato_depth_cents"),
                                (b"x" * 70_000, "too big")):
                status, data = post_file(s, "/api/style/import", raw)
                self.assertEqual(status, 400)
                self.assertIn(reason, data["error"])
            self.assertEqual(s.config_path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in (home / "styles").iterdir()), kept)

            # Installed without being put on (what the store does).
            status, data = post_file(s, "/api/style/import?apply=0", json.dumps(self.preset()).encode())
            self.assertEqual((status, data["id"], data["config"]), (200, "user:mine", {}))
            self.assertEqual(Config.load().final_hold_seconds, 2.0)
            # Built-in presets stay; only the panel may do any of this.
            status, body = s.request("POST", "/api/style/delete", {"id": "builtin:robot"}, TOKEN)
            self.assertEqual(status, 400)
            self.assertEqual(post_file(s, "/api/style/import", exported, token=False)[0], 403)
            self.assertEqual(s.request("POST", "/api/style/apply", {"id": "builtin:robot"})[0], 403)

    def test_the_page_has_the_preset_row(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        for needle in ("api/styles", "api/style/apply", "api/style/save", "api/style/export?id=",
                       "api/style/import", "api/style/delete", ".tetostyle", "Save as preset",
                       'id="preset"', "function importStyle("):
            self.assertIn(needle, page)


if __name__ == "__main__":
    unittest.main()
