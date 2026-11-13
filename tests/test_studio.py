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
            # A project keeps its music (a recording leaves it where it was:
            # TestRecordingsAndProjects).
            take.kind = "project"
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
        # Twelve now, and each clearly unlike the others.
        from teto_relay import presets
        from teto_relay.config import Config

        self.assertEqual(list(presets.BUILTIN), [
            "just-like-you", "pop-singer", "whisper", "metal-scream", "robot", "diva", "choir", "lo-fi-radio",
            "kawaii", "demon", "lullaby", "rap"])
        seen = []
        for key, preset in presets.BUILTIN.items():
            read = presets.validate(preset)
            # Each sets everything, so picking one never depends on the last.
            self.assertEqual(set(read["settings"]), set(presets.SETTINGS), key)
            self.assertTrue(read["name"] and read["description"] and preset["tag"])
            # Any two differ in at least six settings: a different voice, not a tweak.
            for other in seen:
                differ = sum(1 for k in presets.SETTINGS if read["settings"][k] != other[k])
                self.assertGreaterEqual(differ, 6, key)
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
            self.assertEqual([s["id"] for s in first["styles"]], [f"builtin:{k}" for k in presets.BUILTIN])
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
            self.assertEqual(len(listed["styles"]), len(presets.BUILTIN) + 3)
            self.assertFalse(listed["styles"][len(presets.BUILTIN)]["builtin"])
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
            self.assertEqual(len(listed["styles"]), len(presets.BUILTIN))
            self.assertEqual(listed["active"], "builtin:just-like-you")
            self.assertEqual(listed["settings"], list(presets.SETTINGS))

            # Pick a built-in one: saved, and in force for a running relay.
            s.request("POST", "/api/config", {"output_device": "My speakers", "language": "th"}, TOKEN)
            status, body = s.request("POST", "/api/style/apply", {"id": "builtin:diva"}, TOKEN)
            self.assertEqual(status, 200, body)
            data = json.loads(body)
            self.assertEqual((data["ok"], data["name"], data["config"]["scale"]), (True, "Diva", "minor"))
            saved = Config.load()
            self.assertEqual((saved.singing_style, saved.vibrato_depth_cents), ("sung", 80.0))
            self.assertEqual(s.controller.cfg.vibrato_depth_cents, 80.0)
            self.assertEqual((saved.fx_reverb, saved.fx_size), (0.35, 0.85))
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
            self.assertEqual((back.final_hold_seconds, back.scale, back.vibrato_depth_cents), (2.0, "minor", 80.0))
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


class TestStore(unittest.TestCase):
    """The Store: presets to install and voicebanks to go and get, from a
    list on the internet that is treated as a stranger's."""

    def setUp(self):
        from teto_relay import store

        store.forget()
        self.addCleanup(store.forget)

    def catalog(self, **changes):
        base = {"format": 1, "updated": "2026-11-12",
                "presets": [{"id": "soft", "name": "Soft", "author": "someone", "description": "quiet",
                             "url": "https://example.org/store/soft.tetostyle"}],
                "voicebanks": [{"id": "some-bank", "name": "Some Bank", "owner": "Its Maker",
                                "licence": "Free to use.", "url": "https://example.org/bank/"}]}
        return {**base, **changes}

    def preset_bytes(self, name="Soft", **settings):
        return json.dumps({"format": 1, "name": name, "author": "someone", "description": "quiet",
                           "settings": settings or {"vibrato_depth_cents": 12}}).encode()

    def test_only_plain_https_links_pass(self):
        from teto_relay.store import safe_url

        self.assertEqual(safe_url("https://example.org/a/b.tetostyle", ".tetostyle"),
                         "https://example.org/a/b.tetostyle")
        self.assertEqual(safe_url("https://example.org/Page"), "https://example.org/Page")
        for bad in ("http://example.org/a.tetostyle", "javascript:alert(1)", "file:///C:/Windows/win.ini",
                    "ftp://example.org/x", "//example.org/x", "example.org/x", "https://", "https:///x",
                    "https://user:pass@example.org/x", "https://user@example.org/x",
                    " https://example.org/x", "https://example.org/a b", "https://example.org/x\n",
                    "https://example.org\\@evil.example/x", "https://example.org/" + "a" * 300,
                    "", None, 7, ["https://example.org/x"]):
            self.assertIsNone(safe_url(bad), repr(bad))
        # A preset's link must be a preset's file.
        self.assertIsNone(safe_url("https://example.org/setup.exe", ".tetostyle"))
        self.assertIsNone(safe_url("https://example.org/setup.exe?as=x.tetostyle", ".tetostyle"))
        self.assertIsNone(safe_url("https://example.org/setup.exe#x.tetostyle", ".tetostyle"))

    def test_a_catalog_is_cut_down_to_what_can_be_trusted(self):
        from teto_relay import store

        read = store.validate(self.catalog(
            presets=[
                {"id": "soft", "name": "  Soft\x00\u202e<script>  ", "author": 5, "description": "d" * 999,
                 "url": "https://example.org/store/soft.tetostyle", "run": "calc.exe", "html": "<b>hi</b>"},
                {"id": "soft", "name": "A second one with the same id", "url": "https://example.org/2.tetostyle"},
                {"id": "over-http", "name": "x", "url": "http://example.org/x.tetostyle"},
                {"id": "an-exe", "name": "x", "url": "https://example.org/x.exe"},
                {"id": "../up", "name": "x", "url": "https://example.org/x.tetostyle"},
                {"id": "no-name", "name": "", "url": "https://example.org/x.tetostyle"},
                {"id": "no-link", "name": "x"},
                "not an entry", None, 3],
            voicebanks=[
                {"id": "some-bank", "name": "Some Bank", "owner": "Its Maker", "licence": "Free.",
                 "url": "https://example.org/bank/", "download": "https://evil.example/bank.zip"},
                {"id": "a-zip-by-http", "name": "x", "url": "http://example.org/bank.zip"},
                {"id": "a-script", "name": "x", "url": "javascript:alert(1)"}],
            extra="ignored"))
        self.assertEqual(read["presets"], [{
            "id": "soft", "url": "https://example.org/store/soft.tetostyle",
            "name": "Soft <script>", "author": "", "description": "d" * 300}])
        self.assertEqual(read["voicebanks"], [{"id": "some-bank", "url": "https://example.org/bank/",
                                               "name": "Some Bank", "owner": "Its Maker", "licence": "Free."}])
        self.assertEqual(set(read), {"format", "updated", "presets", "voicebanks"})
        # Lists that are not lists are simply empty.
        self.assertEqual(store.validate(self.catalog(presets="all of them", voicebanks=None))["presets"], [])
        # Not a catalog at all, or one for another version.
        for bad in ([], "catalog", None, {}, {"format": 2}, {"format": "1"}, {"format": True}):
            with self.assertRaises(store.StoreError, msg=repr(bad)):
                store.validate(bad)
        for raw in (b"<html>404</html>", b"\xff\xfe", b"[]", b" " * 300_000):
            with self.assertRaises(store.StoreError, msg=raw[:20]):
                store.parse(raw)
        # However many it lists, only so many are taken.
        many = [{"id": f"p{i}", "name": "x", "url": "https://example.org/x.tetostyle"} for i in range(500)]
        self.assertEqual(len(store.validate(self.catalog(presets=many))["presets"]), store.MAX_PRESETS)

    def test_the_list_that_ships_is_the_published_one_and_is_sound(self):
        from teto_relay import presets, store

        docs = ROOT / "docs" / "store"
        shipped = store.bundled_folder()
        self.assertEqual(shipped, ROOT / "teto_relay" / "web" / "store")
        names = sorted(p.relative_to(docs).as_posix() for p in docs.rglob("*") if p.is_file())
        self.assertEqual(names, sorted(p.relative_to(shipped).as_posix() for p in shipped.rglob("*") if p.is_file()))
        for name in names:
            self.assertEqual((docs / name).read_bytes(), (shipped / name).read_bytes(),
                             f"{name} differs: run tools/sync_store.py")

        raw = json.loads((docs / "catalog.json").read_text(encoding="utf-8"))
        listing = store.bundled()
        self.assertEqual(listing["source"], "bundled")
        # Nothing in it had to be dropped or cut.
        self.assertEqual(len(listing["presets"]), len(raw["presets"]))
        self.assertEqual(len(listing["voicebanks"]), len(raw["voicebanks"]))
        ids = [p["id"] for p in listing["presets"]]
        # The built-in presets, and a few more.
        self.assertEqual(ids[:len(presets.BUILTIN)], list(presets.BUILTIN))
        self.assertGreaterEqual(len(ids), len(presets.BUILTIN) + 2)
        for entry in listing["presets"]:
            self.assertEqual(entry["url"], f"{store.CATALOG_URL.rsplit('/', 1)[0]}/presets/{entry['id']}.tetostyle")
            preset = presets.parse((docs / "presets" / f"{entry['id']}.tetostyle").read_bytes())
            self.assertEqual((preset["name"], preset["description"]), (entry["name"], entry["description"]))
            self.assertEqual(set(preset["settings"]), set(presets.SETTINGS))
        for key, preset in presets.BUILTIN.items():
            self.assertEqual((docs / "presets" / f"{key}.tetostyle").read_text(encoding="utf-8"),
                             presets.dumps(presets.validate(preset)))
        # Voicebanks: a name, an owner, a licence note and a page - never a file.
        self.assertGreaterEqual(len(listing["voicebanks"]), 2)
        for bank in listing["voicebanks"]:
            self.assertTrue(bank["name"] and bank["owner"] and bank["licence"], bank["id"])
            self.assertFalse(bank["url"].lower().endswith((".zip", ".uar", ".rar", ".7z", ".exe")), bank["url"])
        teto = next(b for b in listing["voicebanks"] if b["id"] == "kasane-teto")
        self.assertEqual(teto["url"], "https://kasaneteto.jp/utau/")
        # Nothing of a voicebank is in the Store's folders: only the list and presets.
        self.assertEqual({Path(n).suffix for n in names}, {".json", ".tetostyle"})

    def test_online_when_it_can_be_bundled_when_it_cannot(self):
        import unittest.mock

        from teto_relay import store

        calls = []

        def online(url, limit):
            calls.append(url)
            return json.dumps(self.catalog()).encode()

        with unittest.mock.patch.object(store, "download", online):
            first = store.catalog()
            self.assertEqual((first["source"], [p["id"] for p in first["presets"]]), ("online", ["soft"]))
            self.assertEqual(calls, [store.CATALOG_URL])
            store.catalog()
            self.assertEqual(len(calls), 1, "kept for a while, not fetched every time")
            store.catalog(refresh=True)
            self.assertEqual(len(calls), 2)
        store.forget()

        def offline(url, limit):
            raise store.StoreError("Could not reach the Store (no internet).")

        with unittest.mock.patch.object(store, "download", offline):
            shown = store.catalog()
        self.assertEqual(shown["source"], "bundled")
        self.assertIn("no internet", shown["problem"])
        self.assertGreaterEqual(len(shown["presets"]), 6)
        # A page that is not a catalog (a captive portal, a 404 page): the same.
        with unittest.mock.patch.object(store, "download", lambda url, limit: b"<html>Sign in to Wi-Fi</html>"):
            self.assertEqual(store.catalog()["source"], "bundled")
        self.assertTrue(store.CATALOG_URL.startswith("https://nyanneaw.github.io/teto-relay/store/"))

    def test_fetching_is_https_only_and_bounded(self):
        import io
        import unittest.mock
        import urllib.error
        import urllib.request

        from teto_relay import store

        for bad in ("http://example.org/catalog.json", "file:///C:/Windows/win.ini", "https://u:p@example.org/"):
            with self.assertRaises(store.StoreError, msg=bad):
                store.download(bad, 100)
        # A redirect off https is not followed.
        handler = store._HttpsOnly()
        request = urllib.request.Request("https://example.org/a")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "http://example.org/a")
        self.assertIsNotNone(handler.redirect_request(request, io.BytesIO(), 302, "Found", {},
                                                      "https://cdn.example.org/a"))

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        sent = {}

        class Opener:
            def open(self, request, timeout=None):
                sent.update(url=request.full_url, agent=request.get_header("User-agent"), timeout=timeout)
                return Response(b"x" * 500)

        with unittest.mock.patch.object(urllib.request, "build_opener", lambda *handlers: Opener()):
            self.assertEqual(store.download("https://example.org/catalog.json", 1000), b"x" * 500)
            # Nothing about this PC or its user goes with the request.
            self.assertEqual(sent, {"url": "https://example.org/catalog.json", "agent": "TetoRelay",
                                    "timeout": store.TIMEOUT})
            with self.assertRaises(store.StoreError):
                store.download("https://example.org/catalog.json", 100)

        class Down:
            def open(self, request, timeout=None):
                raise urllib.error.URLError("no route to host")

        with unittest.mock.patch.object(urllib.request, "build_opener", lambda *handlers: Down()):
            with self.assertRaises(store.StoreError) as caught:
                store.download("https://example.org/catalog.json", 100)
            self.assertIn("no route to host", str(caught.exception))

    def test_a_preset_is_fetched_by_id_or_taken_from_the_copy_that_ships(self):
        import unittest.mock

        from teto_relay import presets, store

        listing = store.validate(self.catalog())
        asked = []

        def online(url, limit):
            asked.append((url, limit))
            return self.preset_bytes()

        with unittest.mock.patch.object(store, "download", online):
            self.assertEqual(store.preset_file(listing, "soft"), self.preset_bytes())
        self.assertEqual(asked, [("https://example.org/store/soft.tetostyle", presets.MAX_BYTES)])
        for missing in ("nope", "", "../soft", "https://evil.example/x.tetostyle"):
            with self.assertRaises(store.StoreError, msg=missing):
                store.preset_file(listing, missing)

        def offline(url, limit):
            raise store.StoreError("Could not reach the Store.")

        with unittest.mock.patch.object(store, "download", offline):
            # Not one that ships with the app: it cannot be had offline.
            with self.assertRaises(store.StoreError) as caught:
                store.preset_file(listing, "soft")
            self.assertIn("Could not download Soft", str(caught.exception))
            # One that does: installed from the app's own copy.
            shipped = store.preset_file(store.bundled(), "lullaby")
            self.assertEqual(presets.parse(shipped)["name"], "Lullaby")
        self.assertEqual(store.official_page(listing, "some-bank"), "https://example.org/bank/")
        with self.assertRaises(store.StoreError):
            store.official_page(listing, "soft")

    def test_the_panel_lists_installs_and_opens(self):
        import unittest.mock
        import webbrowser

        from teto_relay import presets, store
        from teto_relay.config import Config

        def offline(url, limit):
            raise store.StoreError("Could not reach the Store (offline).")

        with _TempHome(), _PanelServer() as s, unittest.mock.patch.object(store, "download", offline):
            status, body = s.request("GET", "/api/store")
            self.assertEqual(status, 200, body)
            shown = json.loads(body)
            self.assertEqual(shown["source"], "bundled")
            self.assertIn("offline", shown["problem"])
            states = {p["id"]: p["state"] for p in shown["presets"]}
            self.assertEqual([states[k] for k in presets.BUILTIN], ["builtin"] * len(presets.BUILTIN))
            self.assertEqual(states["angel"], "")
            # The panel is given no addresses for presets: it asks by id.
            self.assertTrue(all("url" not in p for p in shown["presets"]))
            self.assertEqual(next(p for p in shown["presets"] if p["id"] == "robot")["have"], "builtin:robot")

            # The done-when: install one, and it is among the presets.
            before = Config.load().vibrato_depth_cents
            status, body = s.request("POST", "/api/store/install", {"id": "angel"}, TOKEN)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body), {"ok": True, "id": "user:angel", "name": "Angel", "config": {}})
            listed = json.loads(s.request("GET", "/api/styles")[1])
            self.assertIn("user:angel", [p["id"] for p in listed["styles"]])
            # Installed, not put on: she sings as she did until it is picked.
            self.assertEqual(Config.load().vibrato_depth_cents, before)
            shown = json.loads(s.request("GET", "/api/store")[1])
            angel = next(p for p in shown["presets"] if p["id"] == "angel")
            self.assertEqual((angel["state"], angel["have"]), ("installed", "user:angel"))

            # A voicebank is a page to open, by id; nothing is downloaded.
            opened = []
            with unittest.mock.patch.object(webbrowser, "open", opened.append):
                status, body = s.request("POST", "/api/store/open", {"id": "kasane-teto"}, TOKEN)
                self.assertEqual((status, json.loads(body)), (200, {"ok": True, "url": "https://kasaneteto.jp/utau/"}))
                for bad in ({"id": "https://evil.example/"}, {"id": "lullaby"}, {}, {"url": "https://evil.example/"}):
                    status, _ = s.request("POST", "/api/store/open", bad, TOKEN)
                    self.assertEqual(status, 400, bad)
            self.assertEqual(opened, ["https://kasaneteto.jp/utau/"])
            status, body = s.request("POST", "/api/store/install", {"id": "not-in-the-store"}, TOKEN)
            self.assertEqual(status, 400)
            self.assertIn("not in the Store", json.loads(body)["error"])
            self.assertEqual(s.request("POST", "/api/store/install", {"id": "choir"})[0], 403)

    def test_what_the_store_sends_is_checked_like_any_preset(self):
        import unittest.mock

        from teto_relay import store
        from teto_relay.config import Config

        with _TempHome() as home, _PanelServer() as s:
            before = s.config_path.read_bytes()
            for sent, reason in ((b"MZ\x90\x00 this is a program", "not a preset file"),
                                 (self.preset_bytes(vibrato_depth_cents=900), "vibrato_depth_cents"),
                                 (self.preset_bytes(output_device="CABLE Input", ptt_key="f1"), "no singing settings")):
                store.forget()
                with unittest.mock.patch.object(
                        store, "download",
                        lambda url, limit, sent=sent: json.dumps(self.catalog()).encode() if url == store.CATALOG_URL else sent):
                    status, body = s.request("POST", "/api/store/install", {"id": "soft"}, TOKEN)
                self.assertEqual(status, 400, body)
                self.assertIn(reason, json.loads(body)["error"])
            self.assertEqual(s.config_path.read_bytes(), before)
            self.assertFalse((home / "styles").exists())
            # A sound one from the same list goes in, and only its singing settings.
            store.forget()
            good = self.preset_bytes(vibrato_depth_cents=12, output_device="Someone else's speakers")
            with unittest.mock.patch.object(
                    store, "download",
                    lambda url, limit: json.dumps(self.catalog()).encode() if url == store.CATALOG_URL else good):
                status, body = s.request("POST", "/api/store/install", {"id": "soft"}, TOKEN)
            self.assertEqual((status, json.loads(body)["id"]), (200, "user:soft"))
            kept = json.loads((home / "styles" / "soft.tetostyle").read_text(encoding="utf-8"))
            self.assertEqual(kept["settings"], {"vibrato_depth_cents": 12.0})
            self.assertEqual(Config.load().output_device, "CABLE Input")

    def test_the_page_has_the_store_tab(self):
        # The Store is the Market tab in Studio's side panel now.
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        for needle in ('data-side="store"', 'id="store"', "api/store", "api/store/install", "api/store/open",
                       "function renderStore(", "Official page"):
            self.assertIn(needle, page)
        # What the catalog says is shown as text, never as markup.
        start = page.index("function renderStore(")
        body = page[start:page.index("\nfunction ", start + 10)]
        self.assertNotIn("innerHTML", body)


def _sung_track(notes, glide=0.04, wobble=0.0, vibrato=0.0, lead=0.0):
    """A pitch track of someone singing `notes` ((MIDI, seconds) each), one
    after another, gliding between them: 10 ms frames, as the tracker makes."""
    import numpy as np

    from teto_relay.pitch import F0Track, midi_to_hz

    midi = []
    for index, (tone, seconds) in enumerate(notes):
        frames = int(round(seconds * 100))
        start = len(midi)
        for k in range(frames):
            value = float(tone)
            if index and k < glide * 100:
                value = notes[index - 1][0] + (tone - notes[index - 1][0]) * (k + 1) / (glide * 100 + 1)
            t = (start + k) / 100.0
            value += wobble * np.sin(2 * np.pi * 1.7 * t) + vibrato * np.sin(2 * np.pi * 5.5 * t)
            midi.append(value)
    midi = np.array(midi)
    times = lead + np.arange(len(midi)) / 100.0
    return F0Track(times=times, f0=np.asarray(midi_to_hz(midi)), voiced=np.ones(len(midi), bool),
                   sample_rate=16000, method="test")


class TestCoverStyle(unittest.TestCase):
    """Singing a real song: she sings the notes that were sung, in the
    singer's key. A cover of Fly Me to the Moon in the Sing style came out a
    fourth low with its intervals stretched and one note for each word, so it
    was a different tune from the music under it."""

    def test_a_held_pitch_is_one_note_and_a_step_is_two(self):
        from teto_relay.pitch import melody_segments

        track = _sung_track([(60, 0.5), (64, 0.5)])
        pieces = melody_segments(track, 0.0, 1.0)
        self.assertEqual(len(pieces), 2)
        self.assertEqual((pieces[0][0], pieces[-1][1]), (0.0, 1.0))
        self.assertAlmostEqual(pieces[0][1], 0.5, delta=0.06)
        self.assertEqual(pieces[0][1], pieces[1][0], "the pieces cover the span with no gap")
        self.assertAlmostEqual(pieces[0][2], 60.0, delta=0.15)
        self.assertAlmostEqual(pieces[1][2], 64.0, delta=0.15)
        # Three notes in one word, as "a-no-ther" was sung.
        tones = [round(p[2]) for p in melody_segments(_sung_track([(55, 0.3), (60, 0.3), (66, 0.4)]), 0.0, 1.0)]
        self.assertEqual(tones, [55, 60, 66])
        # One pitch, however long: one note, at that pitch.
        self.assertEqual([round(p[2]) for p in melody_segments(_sung_track([(62, 1.5)]), 0.0, 1.5)], [62])

    def test_a_waver_or_a_vibrato_is_not_a_new_note(self):
        from teto_relay.pitch import melody_segments

        self.assertEqual(len(melody_segments(_sung_track([(62, 1.5)], wobble=0.6), 0.0, 1.5)), 1)
        self.assertEqual(len(melody_segments(_sung_track([(62, 1.5)], vibrato=0.9), 0.0, 1.5)), 1)
        # A wavering voice still changing note: two notes, not a string of them.
        pieces = melody_segments(_sung_track([(60, 0.7), (65, 0.7)], wobble=0.5, vibrato=0.4), 0.0, 1.4)
        self.assertEqual([round(p[2]) for p in pieces], [60, 65])
        # A slip to another pitch too short to be a note joins its neighbour.
        pieces = melody_segments(_sung_track([(60, 0.5), (63, 0.06), (60, 0.5)]), 0.0, 1.06)
        self.assertEqual(len(pieces), 1)

    def test_how_far_a_step_must_be_is_a_setting(self):
        from teto_relay.pitch import melody_segments

        track = _sung_track([(60, 0.6), (61, 0.6)])       # a semitone apart
        self.assertEqual(len(melody_segments(track, 0.0, 1.2)), 1, "the default follows clear steps only")
        self.assertEqual([round(p[2]) for p in melody_segments(track, 0.0, 1.2, step=0.6)], [60, 61])
        # And how short a note may be.
        quick = _sung_track([(60, 0.5), (64, 0.18), (67, 0.5)])
        self.assertEqual([round(p[2]) for p in melody_segments(quick, 0.0, 1.18, shortest=0.1)], [60, 64, 67])
        self.assertEqual([round(p[2]) for p in melody_segments(quick, 0.0, 1.18, shortest=0.3)], [60, 67])

    def test_nothing_voiced_or_too_little_is_left_whole(self):
        import numpy as np

        from teto_relay.pitch import F0Track, melody_segments

        silent = F0Track(times=np.arange(50) / 100.0, f0=np.full(50, np.nan), voiced=np.zeros(50, bool),
                         sample_rate=16000)
        self.assertEqual(melody_segments(silent, 0.0, 0.5), [(0.0, 0.5, 0.0)])
        brief = melody_segments(_sung_track([(60, 0.03), (65, 0.03)]), 0.0, 0.06)
        self.assertEqual(len(brief), 1)

    def notes(self, track, words, **settings):
        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Word

        cfg = Config(**{"emit_contour": False, **settings})
        return build_notes([Word(*w) for w in words], track, cfg, None, 61.0, None, False, None, {})

    def test_a_word_sung_over_several_notes_gets_them(self):
        # "la" on one note, then "la" carried up over two more.
        track = _sung_track([(60, 0.5), (62, 0.4), (67, 0.5)])
        words = [("la", 0.0, 0.5), ("la", 0.5, 1.4)]
        cover = self.notes(track, words, singing_style="cover")
        self.assertEqual([n.lyric for n in cover], ["la", "la", "+"])
        self.assertEqual([n.tone for n in cover], [60, 62, 67])
        # The held part is joined to its word, and starts where the pitch stepped.
        self.assertTrue(cover[2].legato)
        self.assertAlmostEqual(cover[2].start, 0.9, delta=0.06)
        self.assertEqual(cover[1].end, cover[2].start)
        # Sing making a tune still makes one note of a word, as it always did...
        self.assertEqual([n.lyric for n in self.notes(track, words, singing_style="sung", sing_source="tune")],
                         ["la", "la"])
        # ...and on auto, this was sung, so she follows the notes, as Cover did.
        self.assertEqual([n.lyric for n in self.notes(track, words, singing_style="sung")], ["la", "la", "+"])
        self.assertEqual([n.lyric for n in self.notes(track, words, singing_style="speech")], ["la", "la"])

    def test_she_stays_in_the_singers_key(self):
        # Sung around F4; the voicebank was recorded a fourth higher (A#4).
        track = _sung_track([(65, 0.5), (67, 0.5), (69, 0.5), (65, 0.5)])
        words = [("one", 0.0, 0.5), ("two", 0.5, 1.0), ("three", 1.0, 1.5), ("four", 1.5, 2.0)]

        def built(style, target, **more):
            from teto_relay.config import Config
            from teto_relay.notes import build_notes
            from teto_relay.stt import Word

            cfg = Config(singing_style=style, emit_contour=False, scale="chromatic", **more)
            return build_notes([Word(*w) for w in words], track, cfg, None, target, None, False, None, {})

        # Making a tune moves her onto the voicebank's pitch: a different key from the music.
        self.assertEqual(built("sung", 70.0, sing_source="tune")[0].shift, 4)
        # Sing on auto heard this as singing, so it keeps the key, as Cover did.
        self.assertEqual(built("sung", 70.0)[0].shift, 0)
        # Cover moves her by whole octaves only, whatever the voicebank's pitch...
        for target, shift in ((70.0, 0), (61.0, 0), (78.0, 12), (52.0, -12)):
            cover = built("cover", target)
            self.assertEqual(cover[0].shift, shift, target)
            self.assertEqual([n.tone for n in cover], [65 + shift, 67 + shift, 69 + shift, 65 + shift])
        # ...and does not stretch the tune, even with a wide Melody range set.
        self.assertEqual([n.tone for n in built("cover", 61.0, sung_melody_range=2.5)], [65, 67, 69, 65])
        self.assertNotEqual([n.tone for n in built("sung", 65.0, sung_melody_range=2.5, sing_source="tune")],
                            [65, 67, 69, 65])
        # A shift left over from another style is not an octave: it is not kept.
        from teto_relay.config import Config
        from teto_relay.notes import build_notes
        from teto_relay.stt import Word

        cfg = Config(singing_style="cover", emit_contour=False)
        kept = build_notes([Word(*w) for w in words], track, cfg, -5, 61.0, None, False, None, {})
        self.assertEqual(kept[0].shift % 12, 0)

    def test_a_key_still_pulls_stray_notes_in(self):
        # C major: the F# that was sung is moved to a note of the key.
        track = _sung_track([(60, 0.5), (66, 0.5), (67, 0.5)])
        words = [("one", 0.0, 0.5), ("two", 0.5, 1.0), ("three", 1.0, 1.5)]
        self.assertEqual([n.tone for n in self.notes(track, words, singing_style="cover", scale="chromatic")],
                         [60, 66, 67])
        tuned = self.notes(track, words, singing_style="cover", scale="major", scale_key="C")
        self.assertEqual((tuned[0].tone, tuned[2].tone), (60, 67))
        self.assertIn(tuned[1].tone, (65, 67))

    def test_cover_is_a_style_with_its_own_settings(self):
        from teto_relay import presets, store
        from teto_relay.config import Config, ConfigError

        # Cover is Sing following your notes now; an old setting still reads.
        old = Config.from_dict({"singing_style": "Cover"})
        self.assertEqual((old.singing_style, old.sing_source), ("sung", "notes"))
        self.assertEqual((Config().melody_step, Config().melody_min_note_seconds), (1.5, 0.14))
        with self.assertRaises(ConfigError):
            Config.from_dict({"melody_step": 0})
        # They travel in a preset, and the Store's Cover song uses the style.
        self.assertIn("melody_step", presets.SETTINGS)
        self.assertIn("melody_min_note_seconds", presets.SETTINGS)
        song = presets.parse((store.bundled_folder() / "presets" / "cover-song.tetostyle").read_bytes())
        self.assertEqual((song["settings"]["singing_style"], song["settings"]["sing_source"],
                          song["settings"]["sung_melody_range"], song["settings"]["final_hold_seconds"]),
                         ("sung", "notes", 1.0, 0.0))
        with _PanelServer() as s:
            meta = json.loads(s.request("GET", "/api/config")[1])["meta"]
            # One Sing: the panel offers Speak and Sing, and how Sing follows you.
            self.assertEqual(meta["choices"]["singing_style"], ["speech", "sung"])
            self.assertEqual(meta["choices"]["sing_source"], ["auto", "tune", "notes"])
            self.assertIn("sing_source", meta["groups"]["Singing"])
            self.assertIn("melody_step", meta["groups"]["Singing"])
            status, _ = s.request("POST", "/api/config", {"singing_style": "cover"}, TOKEN)
            self.assertEqual(status, 200)

    def test_the_page_points_a_take_over_music_at_cover(self):
        # Cover is part of Sing now: a take over music points at Sing.
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("cfg.singing_style!=='sung'||cfg.sing_source==='tune'", page)
        self.assertIn("Set Style to Sing", page)


def _take_named(root, name, phrases):
    """A saved take under `root` called `name`: two takes made in the same
    second would otherwise get the same folder."""
    import shutil
    import tempfile

    from teto_relay.takes import read_info

    with tempfile.TemporaryDirectory() as tmp:
        made = _take(tmp, phrases)
        folder = Path(root) / name
        folder.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(made), str(folder))
    info = read_info(folder)
    info["id"] = name
    (folder / "take.json").write_text(json.dumps(info), encoding="utf-8")
    return folder


class TestLibraryClips(unittest.TestCase):
    """Studio's library: a line from any take, dragged onto the timeline, is
    copied into the take there (or makes a new take), as a clip of its own."""

    def test_a_line_is_copied_after_the_last_one(self):
        import tempfile

        import soundfile as sf

        from teto_relay.takes import copy_line, read_info

        with tempfile.TemporaryDirectory() as tmp:
            source = _take_named(tmp, "take_20260101_000000",
                                 [_phrase(0.0, "one"), _phrase(1.0, "two", level=0.4)])
            target = _take_named(tmp, "take_20260101_000001", [_phrase(0.5, "here")])
            before = {p.name: p.read_bytes() for p in (source / "lines").iterdir()}
            out = copy_line(source, 2, target)

            self.assertEqual([(p["n"], p["text"], p["at"]) for p in out["phrases"]],
                             [(1, "here", 0.5), (2, "two", 0.6)])
            # The copy has files of its own, so it can be fixed and moved alone.
            for name in ("02.wav", "02.ustx", "02-voice.wav"):
                self.assertTrue((target / "lines" / name).is_file(), name)
            self.assertEqual(out["phrases"][1]["notes"], out["phrases"][0]["notes"])
            sung, _ = sf.read(str(target / "teto.wav"))
            self.assertEqual(len(sung), 700)
            self.assertAlmostEqual(float(sung[650]), 0.4, places=3)
            self.assertEqual(len(read_info(target)["phrases"]), 2)
            # The take it came from is untouched.
            self.assertEqual({p.name: p.read_bytes() for p in (source / "lines").iterdir()}, before)
            self.assertEqual(len(read_info(source)["phrases"]), 2)

    def test_a_copy_lands_where_it_is_dropped_or_beside_a_clip_there(self):
        import tempfile

        from teto_relay.takes import copy_line, line_span

        with tempfile.TemporaryDirectory() as tmp:
            source = _take_named(tmp, "take_20260101_000000", [_phrase(0.0, "one", origin=0.2, length=500)])
            target = _take_named(tmp, "take_20260101_000001", [_phrase(1.0, "here", length=500)])
            out = copy_line(source, 1, target, at=3.0)
            self.assertEqual(out["phrases"][1]["at"], 3.0)
            # Dropped onto the clip at 1.0-1.5: its singing starts where that ends.
            out = copy_line(source, 1, target, at=1.0)
            spans = sorted(line_span(target, p) for p in out["phrases"])
            self.assertAlmostEqual(spans[1][0], 1.5, places=3)
            self.assertEqual(sorted(p["n"] for p in out["phrases"]), [1, 2, 3])

    def test_a_copy_can_make_a_new_take_or_double_a_line(self):
        import tempfile

        from teto_relay.takes import copy_line, read_info

        with tempfile.TemporaryDirectory() as tmp:
            source = _take_named(tmp, "take_20260101_000000", [_phrase(0.0, "one")])
            fresh = Path(tmp) / "take_20260101_000009"
            out = copy_line(source, 1, fresh, at=2.0)
            self.assertEqual(out["id"], "take_20260101_000009")
            self.assertEqual([(p["n"], p["at"]) for p in out["phrases"]], [(1, 2.0)])
            self.assertTrue((fresh / "teto.wav").is_file())
            # Into its own take: a second copy, after the first.
            out = copy_line(source, 1, source)
            self.assertEqual([(p["n"], p["text"], p["at"]) for p in out["phrases"]],
                             [(1, "one", 0.0), (2, "one", 0.1)])
            self.assertEqual(len(read_info(source)["phrases"]), 2)

    def test_a_line_that_is_not_there_changes_nothing(self):
        import tempfile

        from teto_relay.takes import copy_line, read_info

        with tempfile.TemporaryDirectory() as tmp:
            source = _take_named(tmp, "take_20260101_000000", [_phrase(0.0, "one")])
            target = _take_named(tmp, "take_20260101_000001", [_phrase(0.0, "here")])
            with self.assertRaises(ValueError) as caught:
                copy_line(source, 4, target)
            self.assertIn("no line 4", str(caught.exception))
            for bad in ("later", float("nan"), True):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    copy_line(source, 1, target, at=bad)
            self.assertEqual(len(read_info(target)["phrases"]), 1)
            self.assertEqual(sorted(p.name for p in (target / "lines").iterdir())[:1], ["01-voice.wav"])
            self.assertFalse((target / "lines" / "02.wav").exists())
            # A new take that could not be made is not left half made.
            fresh = Path(tmp) / "take_20260101_000009"
            with self.assertRaises(ValueError):
                copy_line(source, 4, fresh)
            self.assertFalse(fresh.exists())

    def test_new_take_names_never_clash(self):
        import tempfile

        from teto_relay.takes import new_folder, take_folder

        with tempfile.TemporaryDirectory() as tmp:
            first = new_folder(tmp)
            first.mkdir()
            second = new_folder(tmp)
            self.assertNotEqual(first, second)
            self.assertTrue(second.name.startswith(first.name))
            second.mkdir()
            self.assertEqual(take_folder(tmp, second.name), second.resolve())

    def test_the_panel_copies_a_line(self):
        from teto_relay import music
        from teto_relay.takes import read_info

        with _TempHome() as home, _PanelServer() as s:
            source = _take_named(home / "takes", "take_20260101_000000", [_phrase(0.0, "one"), _phrase(1.0, "two")])
            target = _take_named(home / "takes", "take_20260101_000001", [_phrase(0.0, "here")])
            url = f"/api/take/line/copy?take={target.name}"
            status, body = s.request("POST", url, {"from": source.name, "line": 2, "at": 4.0}, TOKEN)
            self.assertEqual(status, 200, body)
            data = json.loads(body)
            self.assertEqual([(p["text"], p["at"]) for p in data["take"]["phrases"]], [("here", 0.0), ("two", 4.0)])
            self.assertEqual(len(read_info(target)["phrases"]), 2)
            self.assertEqual(s.controller.takes_changed, 1)
            self.assertIsNone(s.controller.relay)

            # "new": a take of its own, keeping the music waiting on the music row.
            next_folder = home / "takes" / music.NEXT
            next_folder.mkdir(parents=True)
            (next_folder / "music.wav").write_bytes(_song())
            status, body = s.request("POST", "/api/take/line/copy?take=new",
                                     {"from": source.name, "line": 1}, TOKEN)
            self.assertEqual(status, 200, body)
            made = json.loads(body)["take"]
            self.assertNotIn(made["id"], (source.name, target.name))
            self.assertEqual([p["text"] for p in made["phrases"]], ["one"])
            self.assertTrue((home / "takes" / made["id"] / "music.wav").is_file())
            self.assertIsNotNone(made["music"])

            for query, body_in, code in (
                    (target.name, {"from": source.name, "line": 9}, 400),
                    (target.name, {"from": source.name, "line": 1, "at": "x"}, 400),
                    (target.name, {"from": "take_nope", "line": 1}, 404),
                    ("take_nope", {"from": source.name, "line": 1}, 404),
                    ("../etc", {"from": source.name, "line": 1}, 404)):
                status, body = s.request("POST", f"/api/take/line/copy?take={query}", body_in, TOKEN)
                self.assertEqual(status, code, body)
                self.assertFalse(json.loads(body)["ok"])
            status, _ = s.request("POST", url, {"from": source.name, "line": 1})
            self.assertEqual(status, 403)
            self.assertEqual(len(read_info(target)["phrases"]), 2)

    def test_the_page_has_the_library(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        for needle in ("api/take/line/copy?take=", "function renderLibrary(", "application/x-teto-line"):
            self.assertIn(needle, page)


class TestLanes(unittest.TestCase):
    """The playlist has lanes: her lines can be stacked (doubles, harmonies)
    and audio files can be laid alongside."""

    def test_lines_on_different_lanes_can_sing_at_once(self):
        import tempfile

        import soundfile as sf
        import yaml

        from teto_relay.takes import copy_line, line_span, move_line, read_info

        with tempfile.TemporaryDirectory() as tmp:
            folder = _take_named(tmp, "take_20260101_000000", [_phrase(0.0, "one", level=0.25), _phrase(1.0, "two")])
            # A double of line 1 on lane 2, at the same time: not pushed aside.
            out = copy_line(folder, 1, folder, at=0.0, lane=2)
            copy = next(p for p in out["phrases"] if p["n"] == 3)
            self.assertEqual((copy["lane"], copy["at"], copy["double"]), (2, 0.0, True))
            sung, _ = sf.read(str(folder / "teto.wav"))
            self.assertAlmostEqual(float(sung[50]), 0.5, places=3)          # both heard at once
            voice, _ = sf.read(str(folder / "your-voice.wav"))
            self.assertAlmostEqual(float(voice[50]), 0.25, places=3)        # the guide only once
            # Moving it on its own lane only steps around lines on that lane.
            out = move_line(folder, 3, 1.0)
            self.assertEqual(next(p for p in out["phrases"] if p["n"] == 3)["at"], 1.0)
            # Moved back to lane 1 at the same time, it goes beside line 2
            # (1.0-1.1), just before or just after it.
            out = move_line(folder, 3, 1.0, lane=1)
            line = next(p for p in out["phrases"] if p["n"] == 3)
            self.assertEqual(line["lane"], 1)
            self.assertIn(round(line_span(folder, line)[0], 3), (0.9, 1.1))
            # Each lane is a track of its own in the OpenUtau project.
            move_line(folder, 3, 1.0, lane=2)
            doc = yaml.safe_load((folder / "teto.ustx").read_text(encoding="utf-8"))
            self.assertEqual([t["track_name"] for t in doc["tracks"]], ["Teto", "Teto 2"])
            self.assertEqual(sorted(p["track_no"] for p in doc["voice_parts"]), [0, 0, 1])
            for bad in (0, 17, 1.5, "2", True):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    move_line(folder, 3, 1.0, lane=bad)
            self.assertEqual(next(p for p in read_info(folder)["phrases"] if p["n"] == 3)["lane"], 2)

    def test_a_line_saved_before_lanes_is_on_lane_1(self):
        from teto_relay.takes import lane_of

        self.assertEqual(lane_of({}), 1)
        self.assertEqual(lane_of({"lane": 3}), 3)
        self.assertEqual(lane_of({"lane": "x"}), 1)
        self.assertEqual(lane_of({"lane": 99}), 16)

    def test_audio_is_laid_on_its_own_lanes(self):
        import io
        import tempfile
        import zipfile

        import soundfile as sf

        from teto_relay import music
        from teto_relay.takes import add_audio, delete_audio, detail, move_audio, take_file, zip_take

        with tempfile.TemporaryDirectory() as tmp:
            folder = _take_named(tmp, "take_20260101_000000", [_phrase(0.0, "one", length=300)])
            out = add_audio(folder, _song(seconds=0.5, rate=1000), "beat.wav", at=0.2, lane=1)
            clip = out["audio"][0]
            self.assertEqual((clip["id"], clip["name"], clip["file"], clip["lane"], clip["at"], clip["seconds"]),
                             (1, "beat.wav", "audio/01.wav", 1, 0.2, 0.5))
            self.assertTrue(clip["peaks"])
            self.assertEqual(take_file(folder, "audio/01.wav"), folder / "audio" / "01.wav")
            for bad in ("audio/01-peaks.json", "audio/../take.json", "audio/1.wav"):
                self.assertIsNone(take_file(folder, bad), bad)
            # A second clip dropped over the first on its lane goes beside it;
            # on another lane it stays where it was dropped.
            out = add_audio(folder, _song(seconds=0.5, rate=1000), "pad.wav", at=0.3, lane=1)
            self.assertEqual(out["audio"][1]["at"], 0.7)
            out = move_audio(folder, 2, 0.3, lane=2)
            self.assertEqual((out["audio"][1]["lane"], out["audio"][1]["at"]), (2, 0.3))
            # teto.wav is her alone; mix.wav has the clips, even with no music.
            sung, _ = sf.read(str(folder / "teto.wav"))
            self.assertEqual(len(sung), 300)
            mixed, rate = sf.read(io.BytesIO(music.mix_wav(folder)))
            self.assertEqual((rate, len(mixed)), (1000, 800))
            self.assertGreater(float(abs(mixed[600:800]).max()), 0.2)
            with zipfile.ZipFile(io.BytesIO(zip_take(folder))) as z:
                self.assertIn(f"{folder.name}/audio/01-beat.wav", z.namelist())
            out = delete_audio(folder, 1)
            self.assertEqual([c["id"] for c in out["audio"]], [2])
            self.assertFalse((folder / "audio" / "01.wav").exists())
            # Not audio, or a clip that is not there: refused, nothing kept.
            with self.assertRaises(ValueError):
                add_audio(folder, b"not sound at all", "notes.txt")
            with self.assertRaises(ValueError):
                add_audio(folder, b"RIFF....garbage", "fake.wav")
            with self.assertRaises(ValueError):
                move_audio(folder, 9, 1.0)
            with self.assertRaises(ValueError):
                add_audio(folder, _song(seconds=0.5, rate=1000), "x.wav", at="soon")
            self.assertEqual(len(detail(folder)["audio"]), 1)
            self.assertEqual(sorted(p.name for p in (folder / "audio").iterdir()), ["02-peaks.json", "02.wav"])

    def test_the_panel_lays_and_moves_audio_and_lanes(self):
        from teto_relay.takes import read_info

        with _TempHome() as home, _PanelServer() as s:
            folder = _take_named(home / "takes", "take_20260101_000000", [_phrase(0.0, "one"), _phrase(1.0, "two")])
            import http.client

            def upload(query, data, headers=TOKEN):
                conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
                conn.request("POST", f"/api/take/audio?{query}", body=data, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                conn.close()
                return resp.status, body

            status, body = upload(f"take={folder.name}&name=beat.wav&at=0.5&lane=2", _song(seconds=0.5, rate=1000))
            self.assertEqual(status, 200, body)
            clip = json.loads(body)["take"]["audio"][0]
            self.assertEqual((clip["lane"], clip["at"]), (2, 0.5))
            self.assertEqual(upload(f"take={folder.name}&name=beat.wav", b"hello")[0], 400)
            self.assertEqual(upload("take=take_nope&name=beat.wav", _song(seconds=0.5, rate=1000))[0], 404)
            self.assertEqual(upload(f"take={folder.name}&name=beat.wav", _song(seconds=0.5, rate=1000), {})[0], 403)

            url = f"/api/take/audio/move?take={folder.name}"
            status, body = s.request("POST", url, {"id": 1, "at": 2.0, "lane": 1}, TOKEN)
            self.assertEqual(status, 200, body)
            self.assertEqual(read_info(folder)["audio"][0]["at"], 2.0)
            self.assertEqual(s.request("POST", url, {"id": 1, "at": "x"}, TOKEN)[0], 400)
            self.assertEqual(s.request("POST", url, {"at": 1}, TOKEN)[0], 400)

            status, body = s.request("POST", f"/api/take/line/move?take={folder.name}", {"line": 2, "at": 0.0, "lane": 2}, TOKEN)
            self.assertEqual(status, 200, body)
            self.assertEqual(next(p for p in read_info(folder)["phrases"] if p["n"] == 2)["lane"], 2)
            status, body = s.request("POST", f"/api/take/line/copy?take={folder.name}",
                                     {"from": folder.name, "line": 1, "at": 0.0, "lane": 3}, TOKEN)
            self.assertEqual(status, 200, body)
            self.assertEqual(next(p for p in json.loads(body)["take"]["phrases"] if p["n"] == 3)["lane"], 3)

            status, body = s.request("POST", f"/api/take/audio/delete?take={folder.name}", {"id": 1}, TOKEN)
            self.assertEqual(status, 200, body)
            self.assertEqual(read_info(folder)["audio"], [])
            self.assertEqual(s.request("POST", f"/api/take/audio/delete?take={folder.name}", {"id": 1})[0], 403)

    def test_the_page_has_the_lanes(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        for needle in ("api/take/audio?take=", "api/take/audio/move?take=", "api/take/audio/delete?take=",
                       "function harmony(", "lane:"):
            self.assertIn(needle, page)


class TestPanic(unittest.TestCase):
    """PANIC: she goes quiet at once, and the relay keeps running."""

    def test_hush_cuts_the_phrase_playing_and_drops_the_waiting_ones(self):
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
                self.written = []
                streams.append(self)

            def start(self):
                pass

            def write(self, block):
                self.written.append(block.copy())
                if len(self.written) == 1:
                    player.hush()      # PANIC while the first phrase plays

            def stop(self):
                pass

            def close(self):
                pass

        fake_sd = types.SimpleNamespace(OutputStream=FakeStream, stop=lambda: None,
                                        query_devices=lambda *a: {"default_samplerate": 44100,
                                                                  "max_output_channels": 1})
        with tempfile.TemporaryDirectory() as tmp:
            q = queue.Queue()
            for i in range(3):
                path = Path(tmp) / f"relay_{i}.wav"
                sf.write(path, np.full(4000, 0.25, dtype=np.float32), 44100)
                q.put(path)
            q.put(None)
            with unittest.mock.patch.object(playback, "sd", lambda: fake_sd):
                player = playback.Player(Config(persistent_output=True), q, device=3)
                player.start()
                player.join(timeout=10)
            self.assertFalse(player.is_alive(), "the stop signal was dropped with the phrases")
        written = np.concatenate(streams[0].written)
        # One block of the first phrase, nothing of the other two.
        self.assertEqual(len(written), playback.Player.BLOCK)

    def test_a_phrase_after_the_hush_plays_in_full(self):
        import queue
        import tempfile
        import types
        import unittest.mock

        import numpy as np
        import soundfile as sf

        from teto_relay import playback
        from teto_relay.config import Config

        written = []

        class FakeStream:
            def __init__(self, **kwargs):
                pass

            def start(self):
                pass

            def write(self, block):
                written.append(len(block))

            def stop(self):
                pass

            def close(self):
                pass

        fake_sd = types.SimpleNamespace(OutputStream=FakeStream, stop=lambda: None,
                                        query_devices=lambda *a: {"default_samplerate": 44100,
                                                                  "max_output_channels": 1})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay.wav"
            sf.write(path, np.full(3000, 0.25, dtype=np.float32), 44100)
            with unittest.mock.patch.object(playback, "sd", lambda: fake_sd):
                player = playback.Player(Config(persistent_output=True), queue.Queue(), device=3)
                self.assertEqual(player.hush(), 0)
                player._play_file(path)
        self.assertEqual(sum(written), 3000)

    def test_the_panel_hushes_a_running_relay(self):
        import types

        with _PanelServer() as s:
            status, body = s.request("POST", "/api/panic", None, TOKEN)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body), {"ok": True, "hushed": False})
            hushed = []
            s.controller.relay = types.SimpleNamespace(hush=lambda: hushed.append(1) or 0)
            try:
                status, body = s.request("POST", "/api/panic", None, TOKEN)
                self.assertEqual(json.loads(body), {"ok": True, "hushed": True})
                self.assertEqual(hushed, [1])
                status, _ = s.request("POST", "/api/panic")
                self.assertEqual(status, 403)
                self.assertEqual(hushed, [1])
            finally:
                s.controller.relay = None

    def test_the_relay_hush_reaches_the_player(self):
        import types

        from teto_relay.app import TetoRelay

        relay = TetoRelay.__new__(TetoRelay)
        relay._player = types.SimpleNamespace(hush=lambda: 2)
        self.assertEqual(relay.hush(), 2)
        relay._player = None
        self.assertEqual(relay.hush(), 0)

    def test_the_page_has_the_panic_button(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("api/panic", page)
        self.assertIn('id="panic"', page)


def _bank_files(name="bank"):
    """A tiny voicebank: an oto.ini naming two samples, and the samples."""
    import io

    import numpy as np
    import soundfile as sf

    wav = io.BytesIO()
    sf.write(wav, np.zeros(441, np.float32), 44100, format="WAV")
    return {f"{name}/oto.ini": "あ.wav=あ,0,100,0,0,0\r\nい.wav=い,0,100,0,0,0\r\n".encode("cp932"),
            f"{name}/あ.wav": wav.getvalue(), f"{name}/い.wav": wav.getvalue()}


class TestAddingVoicebanks(unittest.TestCase):
    """Your own voicebank, however it came: a .zip, a .rar or .7z, or its
    folder (which the panel packs into a .zip)."""

    def test_a_7z_is_unpacked_with_a_tool_on_the_pc(self):
        import shutil
        import subprocess
        import tempfile

        from teto_relay import library

        tools = library._archive_tools()
        seven = next((t for t in tools if Path(t[0]).stem.lower() == "7z"), None)
        if seven is None:
            self.skipTest("7-Zip is not installed here")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            for name, data in _bank_files("声").items():
                (src / name).parent.mkdir(parents=True, exist_ok=True)
                (src / name).write_bytes(data)
            archive = Path(tmp) / "mine.7z"
            subprocess.run(seven + ["a", "-bd", str(archive), str(src / "声")], capture_output=True, check=True)
            info = library.install_voicebank(archive.read_bytes(), "mine.7z", Path(tmp) / "lib")
            self.assertEqual((info["name"], info["samples"], info["missing"]), ("mine", 2, 0))
            self.assertTrue((Path(tmp) / "lib" / "mine" / "あ.wav").is_file())
            # Nothing left behind but the bank.
            self.assertEqual([p.name for p in (Path(tmp) / "lib").iterdir()], ["mine"])
            shutil.rmtree(Path(tmp) / "lib")

    def test_with_no_tool_it_says_what_to_do(self):
        import tempfile
        import unittest.mock

        from teto_relay import library

        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(library, "_archive_tools", lambda: []):
            with self.assertRaises(ValueError) as caught:
                library.install_voicebank(b"Rar!\x1a\x07\x00", "bank.rar", Path(tmp))
            self.assertIn("7-Zip", str(caught.exception))
            self.assertIn("folder", str(caught.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_the_page_offers_archives_and_folders(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        for needle in ("webkitdirectory", "function makeZip(", "function addVoicebankFolder(", "webkitGetAsEntry",
                       ".rar", ".7z"):
            self.assertIn(needle, page)


class TestHearingAVoicebank(unittest.TestCase):
    """The Market plays a voicebank on this PC: the sample its character.txt
    names, else one of its own recordings."""

    def make(self, root: Path, character: str | None):
        for name, data in _bank_files("声").items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)
        if character is not None:
            (root / "声" / "character.txt").write_bytes(character.encode("cp932"))

    def test_the_named_sample_is_played(self):
        import io

        import numpy as np
        import soundfile as sf

        with _PanelServer() as s:
            root = Path(s.tmp.name)
            self.make(root, "name=テスト\r\nsample=voice.wav\r\n")
            sf.write(str(root / "声" / "voice.wav"), np.full(4410, 0.25, np.float32), 44100)
            status, body = s.request("GET", "/api/bank-sample?bank=bank")
            self.assertEqual(status, 200)
            audio, rate = sf.read(io.BytesIO(body))
            self.assertEqual((rate, len(audio)), (44100, 4410))

    def test_without_one_a_recording_is_played(self):
        from teto_relay import webui

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.make(Path(tmp), None)
            self.assertEqual(webui._sample_file(Path(tmp) / "声").name, "あ.wav")

    def test_a_sample_outside_the_bank_is_refused(self):
        from teto_relay import webui

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.make(Path(tmp), "sample=..\\secret.wav\r\n")
            (Path(tmp) / "secret.wav").write_bytes(b"x")
            self.assertEqual(webui._sample_file(Path(tmp) / "声").name, "あ.wav")

    def test_a_bank_with_nothing_to_play_says_so(self):
        with _PanelServer() as s:
            status, _ = s.request("GET", "/api/bank-sample?bank=nothing")
            self.assertEqual(status, 404)


class TestTunePreview(unittest.TestCase):
    """Tune's play button: the last phrase sung again with the settings as
    they are now, so a turned knob can be heard."""

    def relay(self, home):
        import threading
        import types

        from test_production import _bare_relay

        from teto_relay.config import Config
        from teto_relay.render.null import NullRenderer
        from teto_relay.stt import Word
        from teto_relay.voicebank import Voicebank

        cfg = Config(pitch_method="pyin", use_alignment=False, align_boundaries=False, renderer_backend="null")
        cfg.validate()
        relay = _bare_relay(cfg)
        relay.bank = Voicebank(key="english", name="Teto", root=home / "bank", flavour="en-cvvc")
        relay.transcriber = types.SimpleNamespace(
            transcribe=lambda audio, rate: [Word("hello", 0.1, 0.5), Word("there", 0.6, 1.0)])
        relay.renderer = NullRenderer(cfg)
        relay.converter = None
        relay.engine = "utau"
        relay._render_gate = threading.Lock()
        relay._octave_shift = relay._voice_baseline = None
        relay._singing_state = {}
        relay._side_pool = None
        relay._target_tone, relay._mora_floor = 61.0, 0.11
        return relay

    def test_the_last_phrase_is_sung_again_with_the_new_settings(self):
        import copy

        import numpy as np
        import soundfile as sf
        import yaml

        from teto_relay.app import TetoRelayError
        from teto_relay.capture import Chunk

        with _TempHome() as home:
            relay = self.relay(home)
            with self.assertRaises(TetoRelayError):
                relay.preview()                        # nothing sung yet
            rate = relay.cfg.sample_rate
            t = np.arange(int(1.2 * rate)) / rate
            relay.analyse(Chunk((0.3 * np.sin(2 * np.pi * 150 * t)).astype(np.float32), rate, "release"))
            relay._side_pool.shutdown()
            relay._side_pool = None
            state = copy.deepcopy(relay._singing_state)

            first = relay.preview()
            tones = [n["tone"] for p in yaml.safe_load(first.with_suffix(".ustx").read_text(encoding="utf-8"))["voice_parts"]
                     for n in p["notes"]]
            self.assertTrue(first.is_file())
            self.assertGreater(len(sf.read(str(first))[0]), 0)

            relay.cfg.transpose = 5
            again = relay.preview()
            moved = [n["tone"] for p in yaml.safe_load(again.with_suffix(".ustx").read_text(encoding="utf-8"))["voice_parts"]
                     for n in p["notes"]]
            self.assertEqual(moved, [x + 5 for x in tones])
            # A preview leaves the live key detection as it was.
            self.assertEqual(relay._singing_state, state)
            # Another tuning, heard once (the Market's play button): used for
            # this preview, and nothing in force changes.
            dry = len(sf.read(str(again))[0])     # the same file is written again below
            other = relay.preview({"transpose": 2, "fx_reverb": 0.4})
            heard = [n["tone"] for p in yaml.safe_load(other.with_suffix(".ustx").read_text(encoding="utf-8"))["voice_parts"]
                     for n in p["notes"]]
            self.assertEqual(heard, [x + 2 for x in tones])
            self.assertEqual((relay.cfg.transpose, relay.cfg.fx_reverb), (5, 0.0))
            self.assertGreater(len(sf.read(str(other))[0]), dry)   # the reverb's tail

    def test_the_panel_needs_the_relay_running(self):
        with _PanelServer() as s:
            status, body = s.request("POST", "/api/preview", None, TOKEN)
            self.assertEqual(status, 400)
            self.assertIn("Start", json.loads(body)["error"])
            self.assertEqual(s.request("POST", "/api/preview")[0], 403)

    def test_the_page_has_the_preview(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("api/preview", page)
        self.assertIn('id="tunePlay"', page)


class TestTunings(unittest.TestCase):
    """Tunings that sound clearly different: effects on her voice, her tone
    as OpenUtau shapes it, and one Sing that follows how you sang."""

    def voice(self, seconds=3.0, rate=44100):
        import numpy as np

        t = np.arange(int(seconds * rate)) / rate
        tone = 0.3 * np.sin(2 * np.pi * 220 * t) + 0.1 * np.sin(2 * np.pi * 660 * t)
        return (tone * np.minimum(1.0, t * 8)).astype(np.float32), rate

    def test_every_effect_is_off_at_zero_and_safe_when_on(self):
        import time

        import numpy as np

        from teto_relay import effects
        from teto_relay.config import Config

        audio, rate = self.voice()
        self.assertIs(effects.apply(audio, rate, Config()), audio)        # nothing on: untouched
        loud = {"fx_octave": -1.0, "fx_bright": 1.0, "fx_drive": 1.0, "fx_growl": 1.0, "fx_ring": 1.0,
                "fx_crush": 1.0, "fx_band": 1.0, "fx_air": 1.0, "fx_chorus": 1.0, "fx_reverb": 1.0, "fx_size": 1.0}
        for key in effects.CHAIN:
            out = effects.apply(audio, rate, Config(**{key: loud[key]}))
            self.assertFalse(np.isnan(out).any(), key)
            self.assertLessEqual(float(np.max(np.abs(out))), 1.0, key)
            self.assertFalse(np.allclose(out[: audio.size], audio, atol=1e-3), key)   # it did something
            extra = out.size - audio.size
            # Only reverb lengthens it, by its tail at most.
            self.assertEqual(extra if key != "fx_reverb" else 0, 0, key)
            self.assertLessEqual(extra, int(rate * effects.TAIL_SECONDS), key)
            # As loud as it went in (within a few dB), whatever the effect.
            ratio = float(np.sqrt(np.mean(out[: audio.size] ** 2)) / np.sqrt(np.mean(audio ** 2)))
            self.assertTrue(0.5 < ratio < 2.0, (key, ratio))
        # The whole chain at once on a 3 s phrase: quick enough not to be felt.
        everything = Config(**loud)
        effects.apply(audio, rate, everything)  # warm the filter design and the room
        began = time.perf_counter()
        effects.apply(audio, rate, everything, double=1.0)
        self.assertLess(time.perf_counter() - began, 0.5)

    def test_the_relay_puts_the_effects_on_her_voice(self):
        import tempfile

        import numpy as np
        import soundfile as sf

        from teto_relay.app import Job, TetoRelay
        from teto_relay.config import Config

        audio, rate = self.voice(1.0, 22050)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.wav"
            sf.write(path, audio, rate)
            relay = TetoRelay.__new__(TetoRelay)
            relay.cfg = Config(double_voice=0.0, fx_drive=0.8, fx_reverb=0.3)
            relay.finish_render(Job(captured_at=0.0, text="", wav_path=path, sung=False))
            out, _ = sf.read(path, dtype="float32")
            self.assertGreater(out.size, audio.size)                     # the reverb's tail
            self.assertFalse(np.allclose(out[: audio.size], audio, atol=1e-2))

    def test_her_tone_reaches_the_project_as_curves(self):
        from teto_relay.config import Config
        from teto_relay.notes import Note
        from teto_relay.performance import expression_curves

        notes = [Note(lyric="a", start=0.0, end=0.5, tone=60), Note(lyric="b", start=0.5, end=1.0, tone=62)]
        for n in notes:
            n.legato = True
        self.assertEqual(expression_curves(notes, None, 16000, Config(expressive=False)), {})
        flat = expression_curves(notes, None, 16000, Config(expressive=False, tone_gender=40.0, tone_tension=-60.0,
                                                            tone_breath=50.0, tone_voicing=30.0))
        self.assertEqual(flat["genc"], [(0.0, 40), (1.0, 40)])
        self.assertEqual(flat["tenc"], [(0.0, -60), (1.0, -60)])
        self.assertEqual(flat["brec"][0][1], 50)
        self.assertEqual(flat["voic"][0][1], 30)
        full = expression_curves(notes, None, 16000, Config(tone_breath=50.0, tone_voicing=50.0))
        self.assertEqual(full["brec"][0][1], 50)          # breath under the whole phrase...
        self.assertEqual(full["brec"][-1][1], 80)         # ...and the usual tail on top
        self.assertEqual(full["voic"][0][1], 50)
        self.assertEqual(full["voic"][-1][1], 35)         # the devoiced ending, scaled
        self.assertNotIn("genc", full)                    # neutral: not written

    def test_sing_follows_your_notes_when_you_sing(self):
        import numpy as np

        from teto_relay import pitch as pitch_mod
        from teto_relay.config import Config
        from teto_relay.notes import follows_notes

        n = 200
        times = np.arange(n) * 0.01
        held = pitch_mod.F0Track(times=times, f0=np.repeat([220.0, 247.0, 262.0, 294.0], n // 4),
                                 voiced=np.ones(n, bool), sample_rate=16000)
        sliding = pitch_mod.F0Track(times=times, f0=np.linspace(180.0, 260.0, n) * (1 + 0.05 * np.sin(times * 40)),
                                    voiced=np.ones(n, bool), sample_rate=16000)
        short = pitch_mod.F0Track(times=times[:30], f0=np.full(30, 220.0), voiced=np.ones(30, bool),
                                  sample_rate=16000)
        auto = Config(singing_style="sung")
        state = {}
        self.assertTrue(follows_notes("sung", held, auto, state))
        self.assertTrue(state["follow"])
        self.assertTrue(follows_notes("sung", short, auto, state))       # too short to judge: as before
        self.assertFalse(follows_notes("sung", sliding, auto, state))
        self.assertFalse(follows_notes("sung", short, auto, state))
        self.assertFalse(follows_notes("speech", held, auto, {}))         # Speak never does
        self.assertTrue(follows_notes("sung", sliding, Config(sing_source="notes"), {}))
        self.assertFalse(follows_notes("sung", held, Config(sing_source="tune"), {}))
        self.assertTrue(follows_notes("cover", sliding, auto, {}))         # the old Cover

    def test_cover_is_sing_on_your_notes(self):
        from teto_relay import presets
        from teto_relay.config import Config

        cfg = Config.from_dict({"singing_style": "Cover"})
        self.assertEqual((cfg.singing_style, cfg.sing_source), ("sung", "notes"))
        old = presets.validate({"format": 1, "name": "Old", "settings": {"singing_style": "cover"}})
        self.assertEqual((old["settings"]["singing_style"], old["settings"]["sing_source"]), ("sung", "notes"))

    def test_an_old_preset_clears_the_effects_it_never_had(self):
        from teto_relay import presets
        from teto_relay.config import Config

        old = presets.validate({"format": 1, "name": "Old", "settings": {"singing_style": "sung",
                                                                        "vibrato_depth_cents": 40.0}})
        full = presets.settings_of(old)
        self.assertEqual(set(full), set(presets.SETTINGS))
        self.assertEqual((full["fx_drive"], full["fx_growl"], full["vibrato_depth_cents"]), (0.0, 0.0, 40.0))
        screaming = Config(**presets.settings_of(presets.BUILTIN["metal-scream"]))
        self.assertFalse(presets.matches(old, screaming))
        for key, value in full.items():
            setattr(screaming, key, value)
        self.assertTrue(presets.matches(old, screaming))
        self.assertEqual(screaming.fx_drive, 0.0)


class TestSavingSettings(unittest.TestCase):
    """Windows refuses to swap a file in while something has it open; a
    knob saving often ran into that ("Access is denied")."""

    def test_a_refused_swap_is_tried_again(self):
        import tempfile
        import unittest.mock

        from teto_relay import config as config_mod
        from teto_relay.config import Config

        real = Path.replace
        refusals = []

        def flaky(self, target):
            if len(refusals) < 3:
                refusals.append(1)
                raise PermissionError(5, "Access is denied")
            return real(self, target)

        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(Path, "replace", flaky):
            path = Path(tmp) / "config.json"
            Config(transpose=5).save(path)
            self.assertEqual(Config.load(path).transpose, 5)
            self.assertEqual(len(refusals), 3)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["config.json"])

        def always(self, target):
            raise PermissionError(5, "Access is denied")

        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(Path, "replace", always), \
                unittest.mock.patch.object(config_mod.time if hasattr(config_mod, "time") else __import__("time"),
                                           "sleep", lambda s: None):
            with self.assertRaises(PermissionError):
                Config().save(Path(tmp) / "config.json")
            self.assertEqual(list(Path(tmp).iterdir()), [])     # no temp file left behind


if __name__ == "__main__":
    unittest.main()


class TestLyricsFollowTheSong(unittest.TestCase):
    """Whisper reads only the last 223 tokens of its prompt, so a whole song
    pasted in told it only the ending. It is sent the part being sung, and
    the place moves on as each phrase is heard."""

    @staticmethod
    def name(n):
        out = ""
        for _ in range(3):
            out, n = "bcdfghjklmnpqrstvwxyz"[n % 21] + "aeiou"[n % 5] + out, n // 21
        return out

    @property
    def song(self):
        # 200 lines of four made-up words, one line per row as pasted.
        return "\n".join(" ".join(self.name(4 * line + k) for k in range(4)) for line in range(200))

    def words(self, text):
        from teto_relay.stt import Word

        return [Word(w, 0.1 * i, 0.1 * i + 0.1) for i, w in enumerate(text.split())]

    def test_a_phrase_is_found_where_it_is_in_the_song(self):
        from teto_relay import lyrics

        heard = " ".join(self.name(n) for n in (200, 201, 202))
        self.assertEqual(lyrics.locate(self.words(heard), self.song), 203)
        self.assertIsNone(lyrics.locate(self.words("what are we having for dinner tonight"), self.song))

    def test_a_repeated_chorus_is_found_near_the_singer(self):
        from teto_relay import lyrics

        song = "la la chorus goes here " + "verse words " * 40 + "la la chorus goes here " + "end words"
        self.assertEqual(lyrics.locate(self.words("chorus goes here"), song), 5)
        self.assertGreater(lyrics.locate(self.words("chorus goes here"), song, near=80), 80)

    def test_the_window_is_the_pasted_text_from_just_before_the_singer(self):
        from teto_relay import lyrics

        part = lyrics.window(self.song, 400, fits=lambda text: len(text.split()) <= 60)
        self.assertEqual(len(part.split()), 60)
        self.assertTrue(part.startswith(self.name(400 - lyrics.WINDOW_BEFORE)))
        self.assertIn("\n", part)                          # lines kept as pasted

    def test_the_transcriber_sends_the_part_being_sung(self):
        import types

        from teto_relay.config import Config
        from teto_relay.stt import Transcriber

        t = Transcriber(Config(lyrics_hint=self.song, initial_prompt=""))
        t._model = types.SimpleNamespace()        # no tokenizer: the safe guess counts
        start = t._song_prompt()
        self.assertTrue(start.startswith(self.name(0)))
        self.assertLess(t._count_tokens(start), 224)
        t._follow_song(self.words(" ".join(self.name(n) for n in range(480, 484))))
        later = t._song_prompt()
        self.assertIn(self.name(484), later)
        self.assertNotIn(self.name(0), later)
        # Talking between lines leaves the place where it was.
        t._follow_song(self.words("hold on a second please"))
        self.assertEqual(t._song_prompt(), later)
        # New lyrics start again at their top.
        t.cfg.lyrics_hint = "short song"
        self.assertEqual(t._song_prompt(), "short song")


class TestMarketServer(unittest.TestCase):
    """The Market server (market-server/): its listings are checked like the
    Store's list, and the app talks to it only by id."""

    BASE = "http://127.0.0.1:8787"

    def listing(self, **item):
        f = {"url": self.BASE + "/v1/files/x-1/file", "size": 100, "type": "wav", "name": "x.wav"}
        return {"id": "x-1", "kind": "sound", "name": "Kick", "file": f, **item}

    def test_only_a_plain_https_address_or_this_pc_is_a_server(self):
        from teto_relay import market
        from teto_relay.config import Config

        for url, ok in (("https://teto-market.example.workers.dev", True), ("http://127.0.0.1:8787/", True),
                        ("http://localhost:8787", True), ("http://example.com", False), ("ftp://x", False),
                        ("https://user:pw@example.com", False), ("", False), ("https://a b", False)):
            self.assertEqual(bool(market.base_url(Config(market_url=url))), ok, url)

    def test_a_built_app_brings_its_own_server_and_the_source_does_not(self):
        import tempfile
        import unittest.mock

        from teto_relay import market, paths
        from teto_relay.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "market_server.txt"
            own.write_text("https://teto-market.example.workers.dev\n", encoding="utf-8")
            with unittest.mock.patch.object(market, "BUNDLED_SERVER", own):
                with unittest.mock.patch.object(paths, "frozen", lambda: True):
                    self.assertEqual(market.base_url(Config()), "https://teto-market.example.workers.dev")
                    # The setting, when set, wins.
                    self.assertEqual(market.base_url(Config(market_url="http://127.0.0.1:8787")), "http://127.0.0.1:8787")
                # Run from source (and in these tests): only the setting.
                self.assertEqual(market.base_url(Config()), "")
            with unittest.mock.patch.object(market, "BUNDLED_SERVER", Path(tmp) / "none.txt"), \
                    unittest.mock.patch.object(paths, "frozen", lambda: True):
                self.assertEqual(market.base_url(Config()), "")
        # The file itself is never shared (where this is a git checkout).
        import shutil
        import subprocess
        if (ROOT / ".git").exists() and shutil.which("git"):
            ignored = subprocess.run(["git", "check-ignore", "-q", "teto_relay/web/market_server.txt"], cwd=ROOT)
            self.assertEqual(ignored.returncode, 0)

    def test_listings_are_checked(self):
        import json

        from teto_relay import market

        good = self.listing()
        off_site = self.listing(id="x-2", file={**good["file"], "url": "https://evil.example/x.wav"})
        not_audio = self.listing(id="x-3", file={**good["file"], "type": "exe"})
        bad_id = self.listing(id="../x")
        tuning = {"id": "soft-1", "kind": "tuning", "name": "Soft\x00 one", "file": {**good["file"], "type": "tetostyle"}}
        link = {"id": "vb-1", "kind": "voicebank", "name": "Link", "official_url": "https://example.com/vb", "licence": "x"}
        nothing = {"id": "vb-2", "kind": "voicebank", "name": "Nothing"}
        raw = json.dumps({"format": 2, "items": [good, off_site, not_audio, bad_id, tuning, link, nothing]}).encode()
        out = market.parse(raw, self.BASE)
        self.assertEqual([s["id"] for s in out["sounds"]], ["x-1"])
        self.assertEqual(out["presets"][0]["name"], "Soft one")
        self.assertEqual([v["id"] for v in out["voicebanks"]], ["vb-1"])
        with self.assertRaises(market.MarketError):
            market.parse(json.dumps({"format": 1, "presets": []}).encode(), self.BASE)

    def test_the_official_list_stays_beside_the_community_one(self):
        from teto_relay import store

        community = {"source": "market", "server": self.BASE, "updated": "", "sounds": [],
                     "presets": [{"id": "growl-1", "name": "Growl", "url": "x"}], "voicebanks": []}
        merged = store._merge(community, store.bundled())
        origins = {p["id"]: p["origin"] for p in merged["presets"]}
        self.assertEqual(origins["growl-1"], "market")
        self.assertEqual(origins["rap"], "list")
        self.assertTrue(any(v["origin"] == "list" for v in merged["voicebanks"]))

    def test_sharing_and_getting_through_the_real_worker(self):
        """market-server/ run in Node, as on this PC: share, list, demo, get."""
        import io
        import os
        import shutil
        import socket
        import subprocess
        import tempfile
        import time
        import uuid

        import numpy as np
        import soundfile as sf

        from teto_relay import market, store

        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is not installed here")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryDirectory() as data, _TempHome():
            proc = subprocess.Popen([node, str(ROOT / "market-server" / "dev" / "serve.mjs"), str(port)],
                                    env={**os.environ, "MARKET_DATA": data}, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                for _ in range(50):
                    try:
                        market.request(base + "/v1/health", base, 1000)
                        break
                    except market.MarketError:
                        time.sleep(0.1)
                wav = io.BytesIO()
                sf.write(wav, np.zeros(4410, np.float32), 44100, format="WAV")

                def form(meta, file=None, demo=None):
                    boundary = uuid.uuid4().hex
                    out = f"--{boundary}\r\nContent-Disposition: form-data; name=\"meta\"\r\n\r\n{json.dumps(meta)}\r\n".encode()
                    for field, data in (("file", file), ("demo", demo)):
                        if data:
                            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"a.wav\"\r\n"
                                    f"Content-Type: audio/wav\r\n\r\n").encode() + data + b"\r\n"
                    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"

                rim = {"kind": "sound", "name": "Rim", "permission": True, "tags": ["drums"]}
                with self.assertRaises(market.MarketError):          # sharing needs an account
                    market.upload(base, *form(rim, wav.getvalue()))
                me = market.sign_up(base, "mika@example.com", "a good password", "mika", "Mika")
                self.assertEqual((me["signed_in"], me["handle"], me["email"]), (True, "mika", "mika@example.com"))
                self.assertNotIn("a good password", (data_dir_text := market._state_path().read_text(encoding="utf-8")))
                self.assertNotIn("password", data_dir_text)
                shared = market.upload(base, *form(rim, wav.getvalue()))
                self.assertEqual(shared["status"], "live")
                paid = market.upload(base, *form({"kind": "sound", "name": "Pack", "price": 120, "currency": "THB",
                                                  "shop_url": "https://mika.booth.pm/items/1"}, None, wav.getvalue()))
                store.forget()
                listing = store.catalog(True, base)
                self.assertEqual(listing["source"], "market")
                by_name = {s["name"]: s for s in listing["sounds"]}
                self.assertEqual(by_name["Rim"]["user"]["handle"], "mika")
                self.assertEqual((by_name["Pack"]["price"], by_name["Pack"]["currency"]), (120, "THB"))
                self.assertIsNone(by_name["Pack"]["file"])
                self.assertEqual(market.shop_page(listing, "sounds", paid["id"]), "https://mika.booth.pm/items/1")
                with self.assertRaises(market.MarketError):
                    market.download(listing, "sounds", paid["id"])
                path = market.get_sound(listing, shared["id"])
                self.assertEqual(path.read_bytes(), wav.getvalue())
                self.assertEqual(market.like(base, shared["id"], True), 1)
                page = market.person(base, "mika")
                self.assertEqual(sorted(page["items"]), sorted([shared["id"], paid["id"]]))
                self.assertEqual(market.edit_profile(base, "Mika ✿", "Thai covers", ["https://x.example/m"])["name"], "Mika ✿")
                self.assertEqual(len(market.mine(base)), 2)
                market.take_down(base, shared["id"])
                market.sign_out(base)
                self.assertFalse(market.account(base)["signed_in"])
                self.assertTrue(market.sign_in(base, "mika@example.com", "a good password")["signed_in"])
                store.forget()
                self.assertEqual([s["name"] for s in store.catalog(True, base)["sounds"]], ["Pack"])
            finally:
                proc.terminate()
                proc.wait(10)
                store.forget()


# ------------------------------------------- recordings, projects and voices
class TestRecordingsAndProjects(unittest.TestCase):
    """Record makes a recording, kept apart from the song projects; a whole
    recording can be dropped onto a project, keeping its lines' spacing."""

    def test_record_makes_a_recording_and_older_takes_are_projects(self):
        import tempfile

        from teto_relay.takes import PROJECT, RECORDING, Take, listing, read_info

        with tempfile.TemporaryDirectory() as tmp:
            take = Take(Path(tmp))
            take.project = "take_20260101_000000"
            take.phrases = [_phrase(0.0, "one")]
            take.phrases[0].voice = {"bank": "neru", "bank_name": "Neru", "tuning": "Robot",
                                     "tuning_id": "builtin:robot", "settings": {"fx_ring": 0.5}}
            info = take.finish()
            self.assertEqual(info["kind"], RECORDING)
            self.assertEqual(info["project"], "take_20260101_000000")
            self.assertEqual(info["voice"]["bank"], "neru")
            self.assertEqual(read_info(take.folder)["phrases"][0]["voice"]["tuning"], "Robot")
            old = _take_named(tmp, "take_20200101_000000", [_phrase(0.0, "old")])
            data = read_info(old)
            data.pop("kind", None)
            (old / "take.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual([t["id"] for t in listing(tmp, kind=RECORDING)], [take.folder.name])
            self.assertEqual([t["id"] for t in listing(tmp, kind=PROJECT)], [old.name])
            self.assertEqual(len(listing(tmp)), 2)

    def test_a_recording_over_music_leaves_the_music_where_it_is(self):
        import tempfile

        from teto_relay import music
        from teto_relay.takes import Take

        with tempfile.TemporaryDirectory() as tmp:
            song = Path(tmp) / music.NEXT
            song.mkdir()
            (song / "music.wav").write_bytes(_song())
            take = Take(Path(tmp))
            take.music_from, take.music_id = song, "next"
            take.phrases = [_phrase(0.0, "one")]
            info = take.finish()
            self.assertEqual(info["sung_over"], "next")
            self.assertFalse((take.folder / "music.wav").exists())

    def test_a_whole_recording_keeps_its_spacing(self):
        import tempfile

        from teto_relay.takes import copy_lines, line_span

        with tempfile.TemporaryDirectory() as tmp:
            rec = _take_named(tmp, "take_20260101_000000",
                              [_phrase(1.0, "one", length=500), _phrase(3.0, "two", length=500)])
            song = _take_named(tmp, "take_20260101_000001", [_phrase(0.0, "here", length=500)])
            # Where it was sung: the same times.
            out = copy_lines(rec, None, song)
            self.assertEqual(sorted((p["text"], p["at"]) for p in out["phrases"]),
                             [("here", 0.0), ("one", 1.0), ("two", 3.0)])
            # Dropped at 10 s on lane 2: the first at 10, the second 2 s later.
            out = copy_lines(rec, None, song, start=10.0, lane=2)
            two = sorted(line_span(song, p)[0] for p in out["phrases"] if p.get("lane") == 2)
            self.assertEqual([round(x, 3) for x in two], [10.0, 12.0])
            self.assertEqual(sorted(p["n"] for p in out["phrases"]), [1, 2, 3, 4, 5])
            # Some of its lines only, into a new project.
            fresh = Path(tmp) / "take_20260101_000009"
            out = copy_lines(rec, [2], fresh, start=0.0)
            self.assertEqual([(p["text"], p["at"]) for p in out["phrases"]], [("two", 0.0)])
            self.assertEqual(out["kind"], "project")
            with self.assertRaises(ValueError):
                copy_lines(rec, [7], song)
            with self.assertRaises(ValueError):
                copy_lines(rec, None, song, start="soon")

    def test_names_and_copies(self):
        import tempfile

        from teto_relay.takes import duplicate, read_info, rename

        with tempfile.TemporaryDirectory() as tmp:
            rec = _take_named(tmp, "take_20260101_000000", [_phrase(0.0, "one")])
            self.assertEqual(rename(rec, "  My   chorus ")["name"], "My chorus")
            self.assertNotIn("name", rename(rec, ""))
            with self.assertRaises(ValueError):
                rename(rec, 5)
            self.assertEqual(len(rename(rec, "x" * 500)["name"]), 80)
            twin = duplicate(rec, tmp, "One · Neru")
            self.assertNotEqual(twin, rec)
            self.assertEqual(read_info(twin)["id"], twin.name)
            self.assertEqual(read_info(twin)["name"], "One · Neru")
            self.assertTrue((twin / "lines" / "01.wav").is_file())

    def test_the_panel_lists_them_apart_and_copies_a_whole_recording(self):
        from teto_relay.takes import read_info

        with _TempHome() as home, _PanelServer() as s:
            rec = _take_named(home / "takes", "take_20260101_000000", [_phrase(0.0, "one"), _phrase(1.0, "two")])
            song = _take_named(home / "takes", "take_20260101_000001", [_phrase(0.0, "here")])
            data = read_info(song)
            data["kind"] = "project"
            (song / "take.json").write_text(json.dumps(data), encoding="utf-8")
            status, body = s.request("GET", "/api/takes")
            listed = json.loads(body)
            self.assertEqual([t["id"] for t in listed["recordings"]], [rec.name])
            self.assertEqual([t["id"] for t in listed["projects"]], [song.name])

            status, body = s.request("POST", f"/api/take/line/copy?take={song.name}",
                                     {"from": rec.name, "lines": "all", "start": 5.0}, TOKEN)
            self.assertEqual(status, 200, body)
            texts = [(p["text"], p["at"]) for p in json.loads(body)["take"]["phrases"]]
            self.assertEqual(texts, [("here", 0.0), ("one", 5.0), ("two", 6.0)])
            status, body = s.request("POST", f"/api/take/line/copy?take={song.name}",
                                     {"from": rec.name, "lines": ["x"]}, TOKEN)
            self.assertEqual(status, 400, body)

            status, body = s.request("POST", f"/api/take/rename?take={rec.name}", {"name": "Verse"}, TOKEN)
            self.assertEqual(json.loads(body)["take"]["name"], "Verse")
            self.assertEqual(s.request("POST", f"/api/take/rename?take={rec.name}", {"name": "x"})[0], 403)
            # Another voice needs the relay running.
            status, body = s.request("POST", f"/api/take/revoice?take={rec.name}", {"bank": "neru"}, TOKEN)
            self.assertEqual(status, 400)
            self.assertIn("Start", json.loads(body)["error"])

    def test_recorded_with_no_project_open_starts_one(self):
        from teto_relay import music
        from teto_relay.takes import Take, read_info

        with _TempHome() as home, _PanelServer() as s:
            song = home / "takes" / music.NEXT
            song.mkdir(parents=True)
            (song / "music.wav").write_bytes(_song())
            take = Take(home / "takes")
            take.music_from, take.music_id = song, "next"
            take.phrases = [_phrase(0.5, "one")]
            s.controller._took(take.finish())
            self.assertEqual(s.controller.last_recording, take.folder.name)
            project = home / "takes" / s.controller.last_take
            self.assertNotEqual(project, take.folder)
            self.assertEqual(read_info(project)["kind"], "project")
            self.assertEqual([p["at"] for p in read_info(project)["phrases"]], [0.5])
            self.assertTrue((project / "music.wav").is_file())

            # Sung for a project that is open: it waits in the Recordings.
            other = Take(home / "takes")
            other.project = project.name
            other.phrases = [_phrase(0.0, "two")]
            other.folder = home / "takes" / "take_20990101_000000"
            s.controller._took(other.finish())
            self.assertEqual(s.controller.last_take, other.folder.name)
            self.assertEqual(len(read_info(project)["phrases"]), 1)

    def test_the_page_has_projects_and_recordings(self):
        page = (ROOT / "teto_relay" / "web" / "index.html").read_text(encoding="utf-8")
        for needle in ("api/take/revoice?take=", "api/take/rename?take=", "lines:'all'",
                       "Recordings", "Projects"):
            self.assertIn(needle, page)


class TestAnotherVoice(unittest.TestCase):
    """A line keeps the voicebank and tuning that sang it, so it sounds the
    same after a restart; and it can be sung again with another voice."""

    def relay(self, home):
        relay = TestTunePreview.relay(TestTunePreview(), home)
        from teto_relay.voicebank import Voicebank

        relay.neru = Voicebank(key="neru", name="Neru", root=home / "neru", flavour="en-cvvc")
        relay.banks = [relay.bank, relay.neru]
        relay._bank_tones = {str(Path(relay.neru.root)): (66.0, 0.1)}
        relay._take = None
        return relay

    def recorded(self, relay, home):
        """One line sung with the voice in force, saved as a recording."""
        import numpy as np

        from teto_relay.capture import Chunk
        from teto_relay.takes import Take

        take = Take(home / "takes")
        relay._take = take
        rate = relay.cfg.sample_rate
        t = np.arange(int(1.2 * rate)) / rate
        job = relay.analyse(Chunk((0.3 * np.sin(2 * np.pi * 150 * t)).astype(np.float32), rate, "release",
                                  captured_at=take.started + 1.5))
        relay._side_pool.shutdown()
        relay._side_pool = None
        job.wav_path = job.ustx_path.with_suffix(".wav")
        job.wav_origin = relay._render(job.ustx_path, job.wav_path)
        take.add(job, relay.voice_now())
        relay._take = None
        info = take.finish()
        return take.folder, info

    def test_a_line_keeps_its_voice_when_the_voice_in_force_changes(self):
        import yaml

        with _TempHome() as home:
            relay = self.relay(home)
            folder, info = self.recorded(relay, home)
            voice = info["phrases"][0]["voice"]
            self.assertEqual(voice["bank"], "english")
            self.assertEqual(voice["settings"]["fx_reverb"], 0.0)
            before = yaml.safe_load((folder / "lines" / "01.ustx").read_text(encoding="utf-8"))
            tones = [n["tone"] for p in before["voice_parts"] for n in p["notes"]]

            # As after a restart with another voice and tuning picked for Live:
            relay.bank = relay.neru
            relay.cfg.fx_reverb, relay.cfg.vibrato_depth_cents = 0.4, 40.0
            fixed = relay.resing(folder, 1, ["goodbye", "there"])
            line = fixed["phrases"][0]
            self.assertEqual(line["voice"]["bank"], "english")
            self.assertEqual(line["voice"]["settings"]["fx_reverb"], 0.0)
            doc = yaml.safe_load((folder / "lines" / "01.ustx").read_text(encoding="utf-8"))
            self.assertEqual(doc["tracks"][0]["singer"], "bank")
            self.assertEqual([n["tone"] for p in doc["voice_parts"] for n in p["notes"]], tones)

    def test_sung_again_with_another_voicebank_and_tuning(self):
        import soundfile as sf
        import yaml

        from teto_relay.app import TetoRelayError

        with _TempHome() as home:
            relay = self.relay(home)
            folder, _ = self.recorded(relay, home)
            dry = len(sf.read(str(folder / "lines" / "01.wav"))[0])
            out = relay.revoice(folder, None, "neru", "builtin:choir")
            line = out["phrases"][0]
            self.assertEqual(out["skipped"], 0)
            self.assertEqual((line["voice"]["bank"], line["voice"]["bank_name"]), ("neru", "Neru"))
            self.assertEqual((line["voice"]["tuning"], line["voice"]["tuning_id"]), ("Choir", "builtin:choir"))
            self.assertEqual(out["voice"]["bank"], "neru")
            doc = yaml.safe_load((folder / "lines" / "01.ustx").read_text(encoding="utf-8"))
            self.assertEqual(doc["tracks"][0]["singer"], "neru")
            # The choir's reverb has a tail.
            self.assertGreater(len(sf.read(str(folder / "lines" / "01.wav"))[0]), dry)
            # The words and their timing are the ones that were sung.
            self.assertEqual([w["text"] for w in line["words"]], ["hello", "there"])
            # Fixing a word later keeps Neru and the choir.
            again = relay.resing(folder, 1, ["hello", "you"])["phrases"][0]
            self.assertEqual((again["voice"]["bank"], again["voice"]["tuning"]), ("neru", "Choir"))
            # The tuning alone, keeping the bank.
            out = relay.revoice(folder, [1], "", "builtin:robot")
            self.assertEqual((out["phrases"][0]["voice"]["bank"], out["phrases"][0]["voice"]["tuning"]),
                             ("neru", "Robot"))
            for bad in (("gone", ""), ("", "builtin:nope")):
                with self.assertRaises(TetoRelayError):
                    relay.revoice(folder, None, *bad)
            with self.assertRaises(TetoRelayError):
                relay.revoice(folder, [9], "neru", "")
