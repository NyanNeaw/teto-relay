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


if __name__ == "__main__":
    unittest.main()
