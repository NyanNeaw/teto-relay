"""Unit tests for the parts that do not need audio hardware or a voicebank."""

from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teto_relay import pitch as pitch_mod  # noqa: E402
from teto_relay import pronunciations as pron  # noqa: E402
from teto_relay.capture import Chunker, PushToTalkChunker, make_chunker, rms  # noqa: E402
from teto_relay.config import Config  # noqa: E402
from teto_relay.notes import Note, build_notes, required_seconds, syllables  # noqa: E402
from teto_relay.stt import Word, clean_lyric  # noqa: E402
from teto_relay.ustx import build_project, load_ustx, write_ustx  # noqa: E402
from teto_relay.voicebank import Voicebank, _detect_flavour, parse_oto  # noqa: E402


class TestPitchMath(unittest.TestCase):
    def test_a4_is_midi_69(self):
        self.assertAlmostEqual(float(pitch_mod.hz_to_midi(440.0)), 69.0, places=6)

    def test_octave_is_twelve_semitones(self):
        self.assertAlmostEqual(float(pitch_mod.hz_to_midi(880.0)), 81.0, places=6)
        self.assertAlmostEqual(float(pitch_mod.hz_to_midi(220.0)), 57.0, places=6)

    def test_round_trip(self):
        for midi in (48.0, 60.0, 69.0, 72.5):
            hz = float(pitch_mod.midi_to_hz(midi))
            self.assertAlmostEqual(float(pitch_mod.hz_to_midi(hz)), midi, places=6)

    def test_cents_between(self):
        # One semitone above A4 is 100 cents above MIDI 69.
        hz = float(pitch_mod.midi_to_hz(70.0))
        self.assertAlmostEqual(pitch_mod.cents_between(hz, 69), 100.0, places=4)


class TestTickMath(unittest.TestCase):
    def test_960_ticks_per_second(self):
        cfg = Config()
        self.assertAlmostEqual(cfg.ticks_per_second, 960.0)
        self.assertEqual(cfg.seconds_to_ticks(1.0), 960)
        self.assertEqual(cfg.seconds_to_ticks(0.5), 480)

    def test_zero_and_rounding(self):
        cfg = Config()
        self.assertEqual(cfg.seconds_to_ticks(0.0), 0)
        self.assertEqual(cfg.seconds_to_ticks(0.0005), 0)  # rounds down
        self.assertEqual(cfg.seconds_to_ticks(0.001), 1)


class TestShift(unittest.TestCase):
    # The English Teto bank is recorded at MIDI 61 (C#4).
    TARGET = 61.0

    def test_lands_on_the_recorded_pitch(self):
        """Rendering far from the recorded pitch is what made it sound breathy."""
        cfg = Config()
        # A ~110 Hz speaking voice is MIDI 45.
        shift = pitch_mod.compute_shift([45.0, 46.0, 44.0], cfg, self.TARGET)
        self.assertEqual(45 + shift, 61)

    def test_semitone_mode_beats_octave_mode_for_timbre(self):
        cfg = Config()
        semitone = pitch_mod.compute_shift([45.0], cfg, self.TARGET)
        octave = pitch_mod.compute_shift([45.0], Config(shift_mode="octave"), self.TARGET)
        self.assertLess(
            abs(45 + semitone - self.TARGET),
            abs(45 + octave - self.TARGET),
            "semitone mode should land closer to the recorded pitch",
        )
        self.assertEqual(octave % 12, 0)

    def test_previous_shift_is_reused_while_close_enough(self):
        cfg = Config()
        # 47 + 16 = 63, within shift_tolerance of 61, so keep the old shift.
        self.assertEqual(pitch_mod.compute_shift([47.0], cfg, self.TARGET, previous=16), 16)

    def test_previous_shift_is_dropped_once_it_drifts(self):
        cfg = Config()
        # 70 + 16 = 86, far above the target, so recompute.
        self.assertNotEqual(pitch_mod.compute_shift([70.0], cfg, self.TARGET, previous=16), 16)

    def test_stability_can_be_disabled(self):
        cfg = Config(stable_shift=False)
        self.assertEqual(pitch_mod.compute_shift([47.0], cfg, self.TARGET, previous=16), 14)

    def test_ordinary_variation_between_phrases_keeps_the_shift(self):
        """Re-normalising each utterance made the voice lurch between phrases."""
        cfg = Config()
        first = pitch_mod.compute_shift([48.0], cfg, self.TARGET)
        # A later phrase spoken a few semitones lower must not retrigger.
        second = pitch_mod.compute_shift([43.0], cfg, self.TARGET, previous=first)
        self.assertEqual(second, first)

    def test_speaking_lower_still_sings_lower(self):
        """Holding the shift is what lets your own pitch differences survive."""
        cfg = Config()
        shift = pitch_mod.compute_shift([48.0], cfg, self.TARGET)
        higher = 48 + pitch_mod.compute_shift([48.0], cfg, self.TARGET, previous=shift)
        lower = 43 + pitch_mod.compute_shift([43.0], cfg, self.TARGET, previous=shift)
        self.assertLess(lower, higher)

    def test_shift_is_clamped(self):
        cfg = Config(max_shift=6)
        self.assertEqual(pitch_mod.compute_shift([20.0], cfg, self.TARGET), 6)

    def test_disabled_shifting_returns_zero(self):
        cfg = Config(auto_octave=False)
        self.assertEqual(pitch_mod.compute_shift([45.0], cfg, self.TARGET), 0)

    def test_in_range_voice_is_left_alone(self):
        cfg = Config()
        self.assertEqual(pitch_mod.octave_shift([60.0, 62.0, 64.0], cfg), 0)

    def test_disabled(self):
        cfg = Config(auto_octave=False)
        self.assertEqual(pitch_mod.octave_shift([30.0], cfg), 0)


class TestPitchBackends(unittest.TestCase):
    def _tone(self, hz, seconds=1.0, sr=16000):
        t = np.arange(int(seconds * sr)) / sr
        return (0.8 * np.sin(2 * np.pi * hz * t)).astype(np.float32)

    def test_both_backends_find_a_known_tone(self):
        audio = self._tone(220.0)  # A3 = MIDI 57
        for method in ("crepe", "pyin"):
            cfg = Config(pitch_method=method)
            midi = pitch_mod.track_f0(audio, 16000, cfg).median_midi(0.1, 0.9)
            self.assertIsNotNone(midi, f"{method} found no pitch")
            self.assertAlmostEqual(midi, 57.0, delta=0.5, msg=f"{method} was off")

    def test_backends_agree_with_each_other(self):
        audio = self._tone(180.0)
        results = {}
        for method in ("crepe", "pyin"):
            cfg = Config(pitch_method=method)
            results[method] = pitch_mod.track_f0(audio, 16000, cfg).median_midi(0.1, 0.9)
        self.assertAlmostEqual(results["crepe"], results["pyin"], delta=0.5)

    def test_unknown_method_is_rejected(self):
        with self.assertRaises(ValueError):
            pitch_mod.track_f0(self._tone(220.0), 16000, Config(pitch_method="magic"))

    def test_crepe_is_the_default(self):
        self.assertEqual(Config().pitch_method, "crepe")

    def test_very_short_audio_does_not_crash(self):
        """crepe needs a full analysis window; short chunks must still work."""
        for method in ("crepe", "pyin"):
            cfg = Config(pitch_method=method)
            track = pitch_mod.track_f0(self._tone(220.0, seconds=0.03), 16000, cfg)
            self.assertGreater(len(track.times), 0)


class TestOctaveCorrection(unittest.TestCase):
    def test_a_word_detected_an_octave_low_is_pulled_back(self):
        """Live this looked like "hello@50 what is@51 up@61 my@61"."""
        cfg = Config()
        fixed = pitch_mod.correct_octaves([44.0, 56.0, 56.0, 57.0, 56.0], cfg)
        self.assertAlmostEqual(fixed[0], 56.0, places=6)

    def test_a_word_detected_an_octave_high_is_pulled_back(self):
        cfg = Config()
        fixed = pitch_mod.correct_octaves([56.0, 68.0, 56.0, 57.0], cfg)
        self.assertAlmostEqual(fixed[1], 56.0, places=6)

    def test_genuine_leaps_are_left_alone(self):
        """A fifth is a real interval, not a detection error."""
        cfg = Config()
        original = [56.0, 63.0, 56.0, 63.0]
        self.assertEqual(pitch_mod.correct_octaves(original, cfg), original)

    def test_the_median_is_not_dragged_by_a_stray_value(self):
        cfg = Config()
        fixed = pitch_mod.correct_octaves([44.0, 56.0, 57.0, 56.0], cfg)
        self.assertLess(max(fixed) - min(fixed), 12.0)

    def test_a_lone_word_is_corrected_against_the_running_baseline(self):
        """A single "hello" detected an octave low set a +17 shift for the session."""
        cfg = Config()
        self.assertAlmostEqual(
            pitch_mod.correct_octaves([44.0], cfg, baseline=56.0)[0], 56.0, places=6
        )

    def test_a_lone_word_is_left_alone_without_a_baseline(self):
        cfg = Config()
        self.assertEqual(pitch_mod.correct_octaves([44.0], cfg), [44.0])

    def test_baseline_does_not_flatten_genuine_pitch_changes(self):
        """Speaking a fifth higher is real, and must survive."""
        cfg = Config()
        self.assertAlmostEqual(
            pitch_mod.correct_octaves([63.0], cfg, baseline=56.0)[0], 63.0, places=6
        )

    def test_correction_can_be_disabled(self):
        cfg = Config(fix_octave_errors=False)
        original = [44.0, 56.0, 56.0]
        self.assertEqual(pitch_mod.correct_octaves(original, cfg), original)


class TestFillGaps(unittest.TestCase):
    def test_interpolates_between_neighbours(self):
        cfg = Config()
        out = pitch_mod.fill_gaps([60.0, None, 64.0], cfg)
        self.assertAlmostEqual(out[1], 62.0)

    def test_extends_at_the_edges(self):
        cfg = Config()
        out = pitch_mod.fill_gaps([None, 60.0, None], cfg)
        self.assertEqual(out, [60.0, 60.0, 60.0])

    def test_all_unvoiced_falls_back_to_default(self):
        cfg = Config()
        out = pitch_mod.fill_gaps([None, None], cfg)
        self.assertEqual(out, [float(cfg.default_tone)] * 2)


class TestChunker(unittest.TestCase):
    def _frames(self, cfg, seconds: float, amplitude: float):
        n = int(seconds * 1000 / cfg.frame_ms)
        rng = np.random.default_rng(0)
        return [rng.normal(0, amplitude, cfg.frame_samples).astype(np.float32) for _ in range(n)]

    def test_speech_then_pause_emits_one_chunk(self):
        cfg = Config(auto_calibrate=False)
        chunker = Chunker(cfg, threshold=0.02)
        emitted = []

        for frame in self._frames(cfg, 0.30, 0.0):  # leading silence
            self.assertIsNone(chunker.push(frame))
        for frame in self._frames(cfg, 1.00, 0.20):  # speech
            got = chunker.push(frame)
            if got:
                emitted.append(got)
        for frame in self._frames(cfg, 0.60, 0.0):  # trailing pause closes it
            got = chunker.push(frame)
            if got:
                emitted.append(got)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].reason, "pause")
        # Preroll is included, so the chunk is a little longer than the speech.
        self.assertGreater(emitted[0].duration, 1.0)

    def test_short_blip_is_dropped(self):
        cfg = Config(auto_calibrate=False)
        chunker = Chunker(cfg, threshold=0.02)
        emitted = []
        for frame in self._frames(cfg, 0.08, 0.20):  # 80 ms < min_chunk_ms
            got = chunker.push(frame)
            if got:
                emitted.append(got)
        for frame in self._frames(cfg, 0.60, 0.0):
            got = chunker.push(frame)
            if got:
                emitted.append(got)
        self.assertEqual(emitted, [])

    def test_max_length_forces_emit(self):
        cfg = Config(auto_calibrate=False, max_chunk_ms=500)
        chunker = Chunker(cfg, threshold=0.02)
        emitted = [c for f in self._frames(cfg, 2.0, 0.2) if (c := chunker.push(f))]
        self.assertTrue(emitted)
        self.assertEqual(emitted[0].reason, "max_length")

    def test_rms_of_silence_is_zero(self):
        self.assertEqual(rms(np.zeros(160, dtype=np.float32)), 0.0)


class TestPushToTalkChunker(unittest.TestCase):
    def _frames(self, cfg, seconds: float, amplitude: float = 0.2):
        n = int(seconds * 1000 / cfg.frame_ms)
        rng = np.random.default_rng(0)
        return [rng.normal(0, amplitude, cfg.frame_samples).astype(np.float32) for _ in range(n)]

    def test_records_only_while_held(self):
        cfg = Config(capture_mode="ptt")
        chunker = PushToTalkChunker(cfg)

        # Key up: audio is ignored (it only feeds the preroll buffer).
        for frame in self._frames(cfg, 0.5):
            self.assertIsNone(chunker.push(frame))

        chunker.start()
        self.assertTrue(chunker.recording)
        for frame in self._frames(cfg, 1.0):
            self.assertIsNone(chunker.push(frame))

        chunker.stop()
        self.assertFalse(chunker.recording)

        # The finished chunk is handed over on the next frame.
        chunk = chunker.push(self._frames(cfg, 0.02)[0])
        self.assertIsNotNone(chunk)
        self.assertEqual(chunk.reason, "release")
        self.assertGreater(chunk.duration, 1.0)

    def test_silence_is_kept_when_the_key_is_held(self):
        """A pause mid-sentence must not split the utterance."""
        cfg = Config(capture_mode="ptt")
        chunker = PushToTalkChunker(cfg)
        chunker.start()
        for frame in self._frames(cfg, 0.4, amplitude=0.2):
            self.assertIsNone(chunker.push(frame))
        for frame in self._frames(cfg, 1.0, amplitude=0.0):  # long silent gap
            self.assertIsNone(chunker.push(frame))
        for frame in self._frames(cfg, 0.4, amplitude=0.2):
            self.assertIsNone(chunker.push(frame))
        chunker.stop()

        chunk = chunker.push(self._frames(cfg, 0.02)[0])
        self.assertIsNotNone(chunk)
        self.assertGreater(chunk.duration, 1.7, "the silent gap must stay inside one chunk")

    def test_tap_is_ignored(self):
        cfg = Config(capture_mode="ptt")
        chunker = PushToTalkChunker(cfg)
        chunker.start()
        for frame in self._frames(cfg, 0.05):  # under min_chunk_ms
            chunker.push(frame)
        chunker.stop()
        self.assertIsNone(chunker.push(self._frames(cfg, 0.02)[0]))

    def test_hits_the_recording_limit(self):
        cfg = Config(capture_mode="ptt", max_chunk_ms=400)
        chunker = PushToTalkChunker(cfg)
        chunker.start()
        emitted = [c for f in self._frames(cfg, 2.0) if (c := chunker.push(f))]
        self.assertTrue(emitted)
        self.assertEqual(emitted[0].reason, "max_length")
        self.assertFalse(chunker.recording)

    def test_flush_emits_a_held_recording(self):
        cfg = Config(capture_mode="ptt")
        chunker = PushToTalkChunker(cfg)
        chunker.start()
        for frame in self._frames(cfg, 0.8):
            chunker.push(frame)
        chunk = chunker.flush()
        self.assertIsNotNone(chunk)


class TestThreadInternals(unittest.TestCase):
    def test_threads_do_not_shadow_thread_dot_stop(self):
        """`self._stop = Event()` shadows Thread._stop and breaks join().

        Thread.join() calls the real Thread._stop() when a join times out, so
        an Event stored under that name raises "'Event' object is not callable"
        from inside the standard library.
        """
        import queue as queue_mod

        from teto_relay.capture import MicCapture
        from teto_relay.playback import Player

        cfg = Config()
        for thread in (
            MicCapture(cfg, queue_mod.Queue()),
            Player(cfg, queue_mod.Queue(), None),
        ):
            self.assertTrue(
                callable(getattr(thread, "_stop")),
                f"{type(thread).__name__} shadows Thread._stop, which breaks join()",
            )


class TestMakeChunker(unittest.TestCase):
    def test_selects_by_mode(self):
        self.assertIsInstance(make_chunker(Config(capture_mode="ptt")), PushToTalkChunker)
        self.assertIsInstance(make_chunker(Config(capture_mode="vad")), Chunker)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            make_chunker(Config(capture_mode="magic"))

    def test_default_is_push_to_talk(self):
        self.assertEqual(Config().capture_mode, "ptt")


class TestLyricCleaning(unittest.TestCase):
    def test_strips_punctuation_and_case(self):
        self.assertEqual(clean_lyric(" Hello, "), "hello")
        self.assertEqual(clean_lyric("..."), "")

    def test_contractions_are_expanded(self):
        """Stripping the apostrophe first leaves "I'm" as "im", sung as "eem"."""
        self.assertEqual(clean_lyric("I'm"), "i am")
        self.assertEqual(clean_lyric("don't!"), "do not")
        self.assertEqual(clean_lyric("It's"), "it is")

    def test_curly_apostrophes_are_handled(self):
        self.assertEqual(clean_lyric("I’m"), "i am")

    def test_surrounding_punctuation_does_not_hide_a_contraction(self):
        self.assertEqual(clean_lyric('"I\'m,"'), "i am")

    def test_possessives_keep_working(self):
        self.assertEqual(clean_lyric("teto's"), "tetos")

    def test_plain_words_are_untouched(self):
        self.assertEqual(clean_lyric("Hello"), "hello")


class TestUstx(unittest.TestCase):
    def _bank(self) -> Voicebank:
        return Voicebank(
            key="test",
            name="Test Bank",
            root=Path(r"D:\Claude\TETO-English-150401\重音テト音声ライブラリー"),
            subbanks=[],
            flavour="en-cvvc",
        )

    def _notes(self) -> list[Note]:
        return [
            Note(lyric="hello", start=0.0, end=0.5, tone=62, contour=[(-40.0, 0.0), (200.0, 30.0)]),
            Note(lyric="world", start=0.5, end=1.0, tone=65),
        ]

    def test_round_trips_through_yaml(self):
        cfg = Config()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.ustx"
            write_ustx(self._notes(), path, self._bank(), cfg)
            loaded = load_ustx(path)

        self.assertEqual(loaded["resolution"], 480)
        self.assertEqual(loaded["bpm"], 120.0)
        notes = loaded["voice_parts"][0]["notes"]
        self.assertEqual([n["lyric"] for n in notes], ["hello", "world"])
        self.assertEqual([n["tone"] for n in notes], [62, 65])
        # 0.5 s at 960 ticks/s
        self.assertEqual(notes[0]["duration"], 480)
        self.assertEqual(notes[1]["position"], 480)

    def test_phonemizer_comes_from_the_bank(self):
        cfg = Config(phonemizer="")
        project = build_project(self._notes(), self._bank(), cfg)
        self.assertEqual(project["tracks"][0]["phonemizer"], "OpenUtau.Plugin.Builtin.EnXSampaPhonemizer")

    def test_explicit_phonemizer_overrides_the_bank(self):
        cfg = Config(phonemizer="Custom.Phonemizer")
        project = build_project(self._notes(), self._bank(), cfg)
        self.assertEqual(project["tracks"][0]["phonemizer"], "Custom.Phonemizer")

    def test_awkward_lyric_survives_serialisation(self):
        """The reference implementation's str.replace approach breaks here."""
        cfg = Config()
        notes = [Note(lyric="key: value", start=0.0, end=0.4, tone=60)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.ustx"
            write_ustx(notes, path, self._bank(), cfg)
            loaded = load_ustx(path)
        self.assertEqual(loaded["voice_parts"][0]["notes"][0]["lyric"], "key: value")

    def test_empty_notes_rejected(self):
        with self.assertRaises(ValueError):
            build_project([], self._bank(), Config())


class TestBuildNotes(unittest.TestCase):
    def _track(self, hz: float, seconds: float = 1.0) -> pitch_mod.F0Track:
        n = int(seconds * 100)
        return pitch_mod.F0Track(
            times=np.arange(n) / 100.0,
            f0=np.full(n, hz),
            voiced=np.ones(n, dtype=bool),
            sample_rate=16000,
        )

    def test_tone_comes_from_measured_pitch(self):
        cfg = Config(auto_octave=False)
        words = [Word("la", 0.0, 0.5)]
        notes = build_notes(words, self._track(440.0), cfg)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].tone, 69)  # A4

    def test_overlapping_words_are_separated(self):
        cfg = Config(auto_octave=False)
        words = [Word("a", 0.0, 0.5), Word("b", 0.3, 0.8)]
        notes = build_notes(words, self._track(440.0), cfg)
        self.assertLessEqual(notes[0].end, notes[1].start)

    def test_no_words_gives_no_notes(self):
        self.assertEqual(build_notes([], self._track(440.0), Config()), [])

    def test_notes_never_touch(self):
        """Touching notes collapse into one legato phrase and lose phonemes."""
        cfg = Config(auto_octave=False)
        # Whisper routinely reports words that butt up against each other.
        words = [Word("hello", 0.0, 0.5), Word("there", 0.5, 1.0), Word("teto", 1.0, 1.5)]
        notes = build_notes(words, self._track(440.0, 2.0), cfg)
        gap = cfg.note_gap_ms / 1000.0
        for earlier, later in zip(notes, notes[1:]):
            self.assertGreaterEqual(
                round(later.start - earlier.end, 6),
                round(gap, 6),
                f"{earlier.lyric!r} and {later.lyric!r} are not separated",
            )

    def test_required_length_scales_with_syllables(self):
        """A flat minimum made one-syllable words drag like held notes."""
        cfg = Config()
        self.assertLess(
            required_seconds("i", cfg),
            required_seconds("kasane", cfg),
        )

    def test_syllable_counting_is_good_enough(self):
        self.assertEqual(syllables("i"), 1)
        self.assertEqual(syllables("hello"), 2)
        self.assertEqual(syllables("kah sah neh"), 3)
        self.assertEqual(syllables(""), 1)

    def test_short_words_get_only_what_they_need(self):
        cfg = Config(auto_octave=False)
        words = [Word("i", 0.0, 0.08), Word("kasane", 0.15, 0.25)]
        notes = build_notes(words, self._track(440.0, 3.0), cfg)
        self.assertGreaterEqual(round(notes[0].duration, 6), round(required_seconds("i", cfg), 6))
        self.assertGreater(notes[1].duration, notes[0].duration)

    def test_pitch_is_read_from_the_spoken_span_not_the_stretched_note(self):
        """A lengthened note covers the next word's audio; pitch must not."""
        cfg = Config(auto_octave=False, emit_contour=False)
        n = 300
        # First 0.1s low, everything after that much higher.
        f0 = np.concatenate([np.full(10, 110.0), np.full(n - 10, 330.0)])
        track = pitch_mod.F0Track(
            times=np.arange(n) / 100.0,
            f0=f0,
            voiced=np.ones(n, dtype=bool),
            sample_rate=16000,
        )
        # A very short first word that must be lengthened well past its audio.
        notes = build_notes([Word("i", 0.0, 0.10), Word("go", 1.0, 1.4)], track, cfg)
        self.assertGreater(notes[0].duration, 0.10, "the note should have been lengthened")
        # Its tone must come from the 110 Hz it was actually spoken at (MIDI 45),
        # not the 330 Hz that follows.
        self.assertLess(notes[0].tone, 55)

    def test_long_words_keep_their_own_length(self):
        cfg = Config(auto_octave=False)
        notes = build_notes([Word("a", 0.0, 1.2)], self._track(440.0, 2.0), cfg)
        self.assertAlmostEqual(notes[0].duration, 1.2, places=6)

    def test_extending_preserves_order_and_gaps(self):
        cfg = Config(auto_octave=False)
        words = [Word("a", 0.0, 0.10), Word("b", 0.12, 0.20)]
        notes = build_notes(words, self._track(440.0, 3.0), cfg)
        gap = cfg.note_gap_ms / 1000.0
        self.assertGreaterEqual(round(notes[1].start - notes[0].end, 6), round(gap, 6))

    def _rising_track(self, low=110.0, high=140.0, seconds=1.0):
        n = int(seconds * 100)
        return pitch_mod.F0Track(
            times=np.arange(n) / 100.0,
            f0=np.linspace(low, high, n),
            voiced=np.ones(n, dtype=bool),
            sample_rate=16000,
        )

    def test_contour_measures_inflection_not_the_transposition(self):
        """The curve is relative to the word's own pitch, before the shift.

        Measuring against the shifted tone yields the transposition distance
        (~1600 cents), which pins every point to the clamp and turns the
        contour into a constant drop below the note.
        """
        cfg = Config()
        notes = build_notes(
            [Word("la", 0.0, 0.9)], self._rising_track(), cfg, target_tone=61.0
        )
        contour = notes[0].contour
        self.assertTrue(contour, "a rising word should produce a contour")
        ys = [y for _, y in contour]
        # Centred on the word's own pitch, so it spans zero rather than sitting
        # at one extreme.
        self.assertLess(min(ys), 0.0)
        self.assertGreater(max(ys), 0.0)
        self.assertFalse(
            all(abs(y) >= cfg.contour_range_cents for y in ys),
            "every point on the clamp means the reference pitch is wrong",
        )

    def test_contour_follows_the_direction_of_the_voice(self):
        cfg = Config()
        rising = build_notes([Word("la", 0.0, 0.9)], self._rising_track(), cfg, target_tone=61.0)
        falling = build_notes(
            [Word("la", 0.0, 0.9)], self._rising_track(140.0, 110.0), cfg, target_tone=61.0
        )
        self.assertLess(rising[0].contour[0][1], rising[0].contour[-1][1])
        self.assertGreater(falling[0].contour[0][1], falling[0].contour[-1][1])

    def test_contour_can_be_disabled(self):
        cfg = Config(emit_contour=False)
        notes = build_notes([Word("la", 0.0, 0.5)], self._track(220.0), cfg, target_tone=61.0)
        self.assertEqual(notes[0].contour, [])

    def test_per_word_pitch_still_varies_with_contour_off(self):
        cfg = Config(auto_octave=False, emit_contour=False)
        n = 200
        # First half low, second half a fifth higher.
        f0 = np.concatenate([np.full(n // 2, 220.0), np.full(n // 2, 330.0)])
        track = pitch_mod.F0Track(
            times=np.arange(n) / 100.0,
            f0=f0,
            voiced=np.ones(n, dtype=bool),
            sample_rate=16000,
        )
        notes = build_notes([Word("a", 0.0, 0.9), Word("b", 1.0, 1.9)], track, cfg)
        self.assertNotEqual(notes[0].tone, notes[1].tone)

    def test_shift_is_reported_on_the_notes(self):
        cfg = Config()
        notes = build_notes([Word("la", 0.0, 0.5)], self._track(110.0), cfg, target_tone=61.0)
        self.assertGreater(notes[0].shift, 0)
        # A 110 Hz voice is MIDI 45; it should land on the bank's pitch.
        self.assertEqual(notes[0].tone, 61)


class TestPronunciations(unittest.TestCase):
    def test_known_word_is_respelled(self):
        table = {"kasane": "kah sah neh"}
        self.assertEqual(pron.apply("kasane", table), "kah sah neh")

    def test_matching_is_case_insensitive(self):
        self.assertEqual(pron.apply("Kasane", {"kasane": "kah sah neh"}), "kah sah neh")

    def test_unknown_word_passes_through(self):
        self.assertEqual(pron.apply("hello", {"kasane": "kah sah neh"}), "hello")

    def test_words_in_the_dictionary_can_still_be_overridden(self):
        """"teto" resolves fine but as the English TEE-toh, so it is respelled."""
        self.assertEqual(pron.DEFAULTS["teto"], "teh toe")

    def test_user_file_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text('{"miku": "my own spelling"}', encoding="utf-8")
            table = pron.load(path)
        self.assertEqual(table["miku"], "my own spelling")
        self.assertIn("kasane", table, "defaults should still be present")

    def test_malformed_user_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text("not json at all", encoding="utf-8")
            table = pron.load(path)
        self.assertEqual(table, pron.DEFAULTS)

    def _flat_track(self):
        return pitch_mod.F0Track(
            times=np.arange(100) / 100.0,
            f0=np.full(100, 440.0),
            voiced=np.ones(100, dtype=bool),
            sample_rate=16000,
        )

    def test_exact_phonemes_beat_a_respelling(self):
        """A hint says what is wanted; a respelling only approximates it."""
        cfg = Config(auto_octave=False)
        notes = build_notes([Word("kasane", 0.0, 0.5)], self._flat_track(), cfg)
        self.assertEqual(notes[0].lyric, "kasane", "the lyric should not be respelled")
        self.assertEqual(notes[0].phonetic_hint, "k A s A n E")

    def test_words_without_a_hint_still_fall_back_to_respelling(self):
        cfg = Config(auto_octave=False)
        table = {"widget": "wih jet"}
        with unittest.mock.patch.object(pron, "load", return_value=table), \
             unittest.mock.patch.object(pron, "load_hints", return_value={}):
            notes = build_notes([Word("widget", 0.0, 0.5)], self._flat_track(), cfg)
        self.assertEqual(notes[0].lyric, "wih jet")
        self.assertIsNone(notes[0].phonetic_hint)

    def test_ordinary_words_get_no_hint(self):
        cfg = Config(auto_octave=False)
        notes = build_notes([Word("hello", 0.0, 0.5)], self._flat_track(), cfg)
        self.assertIsNone(notes[0].phonetic_hint)

    def test_hints_stay_aligned_with_their_words(self):
        """Hints are indexed positionally, so any reordering would misalign them."""
        cfg = Config(auto_octave=False)
        words = [Word("hello", 0.0, 0.4), Word("kasane", 0.5, 0.9), Word("teto", 1.0, 1.4)]
        notes = build_notes(words, self._flat_track(), cfg)
        by_lyric = {n.lyric: n.phonetic_hint for n in notes}
        self.assertIsNone(by_lyric["hello"])
        self.assertEqual(by_lyric["kasane"], "k A s A n E")
        self.assertEqual(by_lyric["teto"], "t E t oU")


class TestAlignment(unittest.TestCase):
    def _audio(self, seconds=2.0, sr=16000):
        t = np.arange(int(seconds * sr)) / sr
        return (0.5 * np.sin(2 * np.pi * 180.0 * t)).astype(np.float32)

    def test_disabled_returns_words_untouched(self):
        from teto_relay import align

        cfg = Config(use_alignment=False)
        words = [Word("hello", 0.0, 0.4), Word("there", 0.5, 0.9)]
        self.assertEqual(align.refine(words, self._audio(), 16000, cfg), words)

    def test_no_words_is_handled(self):
        from teto_relay import align

        self.assertEqual(align.refine([], self._audio(), 16000, Config()), [])

    def test_failure_falls_back_to_the_original_timings(self):
        """Approximate timings beat no output."""
        from teto_relay import align

        words = [Word("hello", 0.0, 0.4)]
        with unittest.mock.patch.object(align, "_load", side_effect=RuntimeError("boom")):
            self.assertEqual(align.refine(words, self._audio(), 16000, Config()), words)

    def test_multi_word_lyrics_are_tokenised_and_regrouped(self):
        """Contraction expansion means one lyric can hold several words."""
        from teto_relay import align

        words = [Word("hello", 0.0, 0.4), Word("i am", 0.5, 0.9), Word("teto", 1.0, 1.4)]
        tokens, groups = align._tokenize(words)
        self.assertEqual(tokens, ["hello", "i", "am", "teto"])
        self.assertEqual(groups, [(0, 1), (1, 3), (3, 4)])


class TestLibraryInstall(unittest.TestCase):
    """Adding your own voices."""

    def setUp(self):
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix="teto-lib-"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def _zip(self, entries: dict) -> bytes:
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)
        return buffer.getvalue()

    def test_a_voicebank_installs_and_is_discoverable(self):
        from teto_relay.library import install_voicebank
        from teto_relay.voicebank import discover

        data = self._zip({
            "mybank/oto.ini": "a.wav=あ,0,100,0,50,10\n",
            "mybank/character.txt": "name=My Singer\n",
            "mybank/a.wav": b"RIFF....WAVEfmt ",
        })
        info = install_voicebank(data, "mybank.zip", self.root)
        self.assertEqual(info["samples"], 1)
        self.assertTrue((self.root / "mybank" / "oto.ini").exists())
        self.assertIn("mybank", [b.key for b in discover(self.root)])

    def test_a_bank_nested_deeper_in_the_zip_is_still_found(self):
        """Archives are packed inconsistently; the oto.ini may be folders down."""
        from teto_relay.library import install_voicebank

        data = self._zip({
            "release v2/singer/oto.ini": "a.wav=あ,0,100,0,50,10\n",
            "release v2/singer/a.wav": b"RIFF",
        })
        info = install_voicebank(data, "singer.zip", self.root)
        self.assertTrue((Path(info["path"]) / "oto.ini").exists())

    def test_zip_slip_entries_are_refused(self):
        """A zip may name ../.. and write outside the library."""
        from teto_relay.library import install_voicebank

        data = self._zip({
            "../escaped.txt": "nope",
            "bank/oto.ini": "a.wav=あ,0,100,0,50,10\n",
            "bank/a.wav": b"RIFF",
        })
        install_voicebank(data, "bank.zip", self.root)
        self.assertFalse((self.root.parent / "escaped.txt").exists())

    def test_something_that_is_not_a_voicebank_is_rejected(self):
        from teto_relay.library import install_voicebank

        data = self._zip({"holiday/photo.jpg": b"\xff\xd8\xff"})
        with self.assertRaises(ValueError) as caught:
            install_voicebank(data, "holiday.zip", self.root)
        self.assertIn("oto.ini", str(caught.exception))

    def test_a_bank_with_no_samples_is_rejected(self):
        from teto_relay.library import install_voicebank

        data = self._zip({"bank/oto.ini": "a.wav=あ,0,100,0,50,10\n"})
        with self.assertRaises(ValueError) as caught:
            install_voicebank(data, "bank.zip", self.root)
        self.assertIn("sing", str(caught.exception))

    def test_installing_the_same_name_twice_is_refused(self):
        from teto_relay.library import install_voicebank

        data = self._zip({"b/oto.ini": "a.wav=あ,0,1,0,1,1\n", "b/a.wav": b"RIFF"})
        install_voicebank(data, "b.zip", self.root)
        with self.assertRaises(ValueError):
            install_voicebank(data, "b.zip", self.root)

    def test_a_pth_that_is_not_an_rvc_model_is_rejected_and_not_kept(self):
        import torch

        from teto_relay.library import install_rvc_model

        blob = self.root / "blob.pth"
        torch.save({"something": 1}, str(blob))
        with self.assertRaises(ValueError) as caught:
            install_rvc_model(blob.read_bytes(), "blob.pth", self.root / "models")
        self.assertIn("not an RVC", str(caught.exception))
        self.assertFalse((self.root / "models" / "blob.pth").exists())

    def test_the_wrong_file_type_is_refused(self):
        from teto_relay.library import install_rvc_model, install_voicebank

        with self.assertRaises(ValueError):
            install_voicebank(b"x", "bank.rar", self.root)
        with self.assertRaises(ValueError):
            install_rvc_model(b"x", "voice.wav", self.root)

    def test_accent_colour_ignores_outline_and_background(self):
        """An icon is mostly line art; tinting by that gives every bank grey."""
        from io import BytesIO

        from PIL import Image

        from teto_relay.library import accent_colour

        image = Image.new("RGB", (32, 32), (12, 12, 12))
        for x in range(32):
            for y in range(12):
                image.putpixel((x, y), (230, 60, 90))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        accent = accent_colour(buffer.getvalue())
        self.assertIsNotNone(accent)
        red = int(accent[1:3], 16)
        self.assertGreater(red, 120)


class TestTransliteration(unittest.TestCase):
    """Thai and Japanese reaching a bank that sings neither."""

    def test_source_language_from_the_whisper_setting(self):
        from teto_relay import translit

        self.assertEqual(translit.source_language(Config(language="th")), "th")
        self.assertEqual(translit.source_language(Config(language="ja")), "ja")
        self.assertEqual(translit.source_language(Config(language="en")), "en")
        self.assertEqual(translit.source_language(Config(language="")), "en")

    def test_a_y_glide_palatalises_but_a_back_vowel_alone_does_not(self):
        """Palatalising every back vowel turned "phom" into ぴょむ."""
        from teto_relay import translit

        self.assertEqual(translit.latin_to_kana("phom"), "ぽむ")
        self.assertEqual(translit.latin_to_kana("khopkhun"), "こぷくん")
        self.assertEqual(translit.latin_to_kana("kyoto"), "きょと")
        self.assertEqual(translit.latin_to_kana("nyu"), "にゅ")

    def test_clusters_take_the_vowel_japanese_would_insert(self):
        from teto_relay import translit

        self.assertEqual(translit.latin_to_kana("swatdi"), "すわとぢ")

    def test_a_doubled_nasal_collapses(self):
        from teto_relay import translit

        self.assertEqual(translit.latin_to_kana("phenng"), "ぺん")

    def test_english_keeps_the_dictionary_route(self):
        """Spelling would give よう; how it is *said* gives ゆう."""
        from teto_relay import translit

        self.assertEqual(translit.to_kana("you", "en"), "ゆう")
        self.assertEqual(translit.to_kana("music", "en"), "みゅうじく")

    def test_japanese_is_read_not_romanised_for_a_japanese_bank(self):
        from teto_relay import translit

        self.assertEqual(translit.to_kana("重音テト", "ja"), "じゅうおんてと")

    def test_a_foreign_word_on_an_english_bank_gets_explicit_phonemes(self):
        """The English dictionary has never seen a romanised Thai word."""
        from teto_relay import translit

        lyric, sounds = translit.to_english("konnichiha", "ja")
        self.assertEqual(lyric, "konnichiha")
        self.assertTrue(sounds)
        self.assertIn("tS", sounds)          # chi
        lyric, sounds = translit.to_english("hello", "en")
        self.assertIsNone(sounds)            # English needs no hint

    def test_english_words_reach_a_japanese_bank_as_morae(self):
        """The language setting said ja; the word was English.

        `source == "ja"` used to win outright, so "hello" went to pykakasi,
        came back as "hello", and the mora splitter cut it into h-e-l-l-o -
        five letters no Japanese voicebank has a sample for. The render then
        failed with "phonemizer returned nothing".
        """
        from teto_relay import japanese, translit

        for word, expected in (("hello", "はろう"), ("up", "あぷ")):
            kana = translit.to_kana(word, "ja")
            self.assertEqual(kana, expected)
            for mora in japanese.split_morae(kana):
                self.assertFalse(mora.isascii(), f"{mora!r} is not a mora")

    def test_real_japanese_still_takes_the_reading_route(self):
        from teto_relay import translit

        self.assertEqual(translit.to_kana("会議", "ja"), "かいぎ")
        self.assertEqual(translit.to_kana("こんにちは", "ja"), "こんにちは")

    def test_japanese_punctuation_is_not_sung(self):
        """string.punctuation is ASCII only, so 。 survived as its own note."""
        from teto_relay.stt import clean_lyric

        self.assertEqual(clean_lyric("。"), "")
        self.assertEqual(clean_lyric("、"), "")
        self.assertEqual(clean_lyric("ました。"), "ました")
        self.assertEqual(clean_lyric("こんにちは！"), "こんにちは")

    def test_the_long_vowel_mark_becomes_the_vowel_it_holds(self):
        """ー is not a mora any bank has; it was becoming a silent note."""
        from teto_relay import translit

        self.assertEqual(translit.expand_long_vowels("らーめん"), "らあめん")
        self.assertEqual(translit.expand_long_vowels("こーひー"), "こおひい")
        self.assertEqual(translit.expand_long_vowels("きゃー"), "きゃあ")

    def test_script_is_detected_even_if_the_language_is_wrong(self):
        from teto_relay import translit

        self.assertTrue(translit.looks_thai("สวัสดี"))
        self.assertTrue(translit.looks_japanese("テト"))
        self.assertFalse(translit.looks_thai("hello"))


class TestVoiceMode(unittest.TestCase):
    """Voice conversion, minus the 55 MB model - these run without it."""

    def test_int16_scaled_output_is_normalised(self):
        """The pipeline returns int16-scaled samples; writing them as float
        clips every one, and the file still plays."""
        from teto_relay.voice import to_float32

        loud = np.array([-28000.0, 0.0, 28000.0], dtype=np.float32)
        out = to_float32(loud)
        self.assertLessEqual(float(np.abs(out).max()), 1.0)
        self.assertAlmostEqual(float(out[2]), 28000 / 32768, places=5)

    def test_already_normalised_audio_is_left_alone(self):
        from teto_relay.voice import to_float32

        quiet = np.array([-0.5, 0.0, 0.5], dtype=np.float32)
        np.testing.assert_allclose(to_float32(quiet), quiet)

    def test_integer_input_is_converted(self):
        from teto_relay.voice import to_float32

        out = to_float32(np.array([-32768, 0, 16384], dtype=np.int16))
        self.assertEqual(out.dtype, np.float32)
        self.assertAlmostEqual(float(out[2]), 0.5, places=5)

    def test_a_missing_model_is_reported_not_crashed_into(self):
        from teto_relay.voice import VoiceConverter

        cfg = Config(mode="voice", rvc_model="D:/nope/missing.pth")
        with self.assertRaises(RuntimeError) as caught:
            VoiceConverter(cfg).load()
        self.assertIn("missing.pth", str(caught.exception))

    def test_conversion_rejects_the_wrong_sample_rate(self):
        from teto_relay.voice import VoiceConverter

        converter = VoiceConverter(Config(mode="voice"))
        converter._vc = object()  # pretend it is loaded; the check comes first
        with self.assertRaises(ValueError):
            converter.convert(np.zeros(100, dtype=np.float32), 44100)

    def test_the_fairseq_stub_satisfies_the_import(self):
        """rvc imports fairseq at module scope; nothing calls through it."""
        import sys

        from teto_relay.voice import _install_fairseq_stub

        had = sys.modules.pop("fairseq", None)
        try:
            _install_fairseq_stub()
            from fairseq import checkpoint_utils  # noqa: F401

            self.assertIn("fairseq", sys.modules)
        finally:
            if had is not None:
                sys.modules["fairseq"] = had

    def test_unknown_engine_is_refused(self):
        """A typo used to run the UTAU pipeline and look like it worked."""
        from teto_relay.app import TetoRelay

        relay = TetoRelay.__new__(TetoRelay)
        relay.cfg = Config(mode="banana")
        relay.engine = "banana"
        with self.assertRaises(RuntimeError) as caught:
            TetoRelay.start(relay)
        self.assertIn("banana", str(caught.exception))


class TestJapaneseConversion(unittest.TestCase):
    def test_the_headline_example(self):
        from teto_relay import japanese

        self.assertEqual(japanese.english_to_kana("i"), "あい")
        self.assertEqual(japanese.english_to_kana("love"), "らぶ")
        self.assertEqual(japanese.english_to_kana("you"), "ゆう")

    def test_l_becomes_r(self):
        """Japanese has no L."""
        from teto_relay import japanese

        self.assertIn("ら", japanese.english_to_kana("love"))

    def test_consonant_plus_y_forms_one_palatalised_mora(self):
        """Without this, "computer" came out かむぷゆうたあ."""
        from teto_relay import japanese

        self.assertIn("ぴゅ", japanese.english_to_kana("computer"))

    def test_m_before_a_consonant_is_a_nasal_but_not_at_the_end(self):
        from teto_relay import japanese

        self.assertTrue(japanese.english_to_kana("computer").startswith("こん"))
        self.assertTrue(japanese.english_to_kana("name").endswith("む"))

    def test_nasals_still_palatalise_before_y(self):
        """M and N form youon too; collapsing them to ん first broke the word.

        "music" came out んゆうじく - a word starting with ん, which Japanese
        cannot pronounce - and "menu" めんゆう.
        """
        from teto_relay import japanese

        self.assertEqual(japanese.english_to_kana("music"), "みゅうじく")
        self.assertEqual(japanese.english_to_kana("menu"), "めにゅう")

    def test_schwa_before_a_closing_nasal_is_o(self):
        """コン, not カン: "computer" is こんぴゅうたあ, not かんぴゅうたあ."""
        from teto_relay import japanese

        self.assertEqual(japanese.english_to_kana("computer"), "こんぴゅうたあ")
        # A schwa before a nasal that is followed by a vowel is untouched:
        # "among" is アマング, so the あ stays.
        self.assertTrue(japanese.english_to_kana("among").startswith("あま"))

    def test_schwa_before_a_final_l_uses_the_inserted_vowel(self):
        """The "-tle"/"-ple"/"-ful" endings carry a consonant, not a vowel."""
        from teto_relay import japanese

        self.assertEqual(japanese.english_to_kana("little"), "りとる")   # リトル
        self.assertEqual(japanese.english_to_kana("people"), "ぴいぷる")  # ピープル

    def test_repeated_cluster_morae_collapse(self):
        """Each consonant in a cluster gets a vowel, which doubled the mora."""
        from teto_relay import japanese

        self.assertEqual(japanese.english_to_kana("months"), "まんす")    # マンス
        self.assertEqual(japanese.english_to_kana("sixth"), "しくす")     # シックス
        self.assertEqual(japanese.english_to_kana("clothes"), "くろうず")  # クローズ

    def test_a_real_repeated_mora_is_kept(self):
        """Only inserted vowels collapse - "really" keeps both り."""
        from teto_relay import japanese

        self.assertEqual(japanese.english_to_kana("really"), "りりい")

    def test_a_vowel_repeated_across_phones_collapses(self):
        """A long vowel written by one phone is real; a repeat where one phone
        ends on the vowel the next begins with is only written twice."""
        from teto_relay import japanese

        # ER0 gives ああ and AW1 gives あう - three あ in a row.
        self.assertEqual(japanese.english_to_kana("around"), "ああうんど")
        # The bank has no ウィ, so W+i is approximated うい, which already ends
        # in the い that IY was about to lengthen.
        self.assertEqual(japanese.english_to_kana("we"), "うい")
        self.assertEqual(japanese.english_to_kana("weird"), "ういど")
        self.assertEqual(japanese.english_to_kana("between"), "びとういん")

    def test_long_vowels_from_one_phone_survive(self):
        """The collapse must not flatten a genuine long vowel."""
        from teto_relay import japanese

        self.assertEqual(japanese.english_to_kana("see"), "しい")
        self.assertEqual(japanese.english_to_kana("four"), "ほお")
        self.assertEqual(japanese.english_to_kana("car"), "かあ")
        self.assertEqual(japanese.english_to_kana("people"), "ぴいぷる")

    def test_the_hot_vowel_follows_the_spelling(self):
        """AA covers both "hot" and "father"; Japanese splits them by spelling."""
        from teto_relay import japanese

        self.assertEqual(japanese.english_to_kana("not"), "のと")      # ノット
        self.assertEqual(japanese.english_to_kana("stop"), "すとぷ")    # ストップ
        self.assertEqual(japanese.english_to_kana("job"), "じょぶ")     # ジョブ
        # No o in the spelling, so it stays ア.
        self.assertEqual(japanese.english_to_kana("father"), "はざあ")  # ファーザー

    def test_r_coloured_vowels_ignore_a_stray_o(self):
        """"harmony" is ハーモニー - the ar is ア even though the word has an o."""
        from teto_relay import japanese

        self.assertTrue(japanese.english_to_kana("harmony").startswith("はあ"))
        self.assertTrue(japanese.english_to_kana("carbon").startswith("かあ"))
        # An r with a vowel after it starts a syllable and does not colour:
        # "sorry" is ソーリー, so the spelling still decides.
        self.assertEqual(japanese.english_to_kana("sorry"), "そりい")

    def test_a_word_final_r_lengthens_instead_of_becoming_ru(self):
        """"car" is カー, not カル - it used to come out かる."""
        from teto_relay import japanese

        self.assertEqual(japanese.english_to_kana("car"), "かあ")
        self.assertEqual(japanese.english_to_kana("start"), "すたあと")  # スタート
        # AO and ER already produce two morae, so they are not lengthened again.
        self.assertEqual(japanese.english_to_kana("four"), "ほお")       # フォー
        self.assertEqual(japanese.english_to_kana("girl"), "がある")      # ガール

    def test_spelling_is_optional(self):
        """arpabet_to_kana is still callable without the original word."""
        from teto_relay import japanese

        self.assertEqual(japanese.arpabet_to_kana(["L", "AH1", "V"]), "らぶ")

    def test_r_after_a_vowel_is_dropped(self):
        """Japanese does not pronounce the r-colouring: モーニング, not モールニング."""
        from teto_relay import japanese

        self.assertNotIn("る", japanese.english_to_kana("morning"))

    def test_japanese_words_use_their_real_spelling(self):
        from teto_relay import japanese

        self.assertEqual(japanese.english_to_kana("teto"), "てと")
        self.assertEqual(japanese.english_to_kana("kasane"), "かさね")

    def test_unknown_words_return_none(self):
        from teto_relay import japanese

        self.assertIsNone(japanese.english_to_kana("zzzqqq"))

    def test_morae_split_keeps_youon_together(self):
        from teto_relay import japanese

        self.assertEqual(japanese.split_morae("きゃく"), ["きゃ", "く"])
        self.assertEqual(japanese.split_morae("あい"), ["あ", "い"])

    def test_kana_lyrics_are_counted_in_morae(self):
        """Latin vowel-group counting would call every kana word one syllable."""
        self.assertEqual(syllables("あい"), 2)
        self.assertEqual(syllables("きゃく"), 2)

    def test_each_mora_becomes_its_own_note(self):
        """A Japanese bank sings one mora per note."""
        cfg = Config(auto_octave=False)
        track = pitch_mod.F0Track(
            times=np.arange(300) / 100.0,
            f0=np.full(300, 220.0),
            voiced=np.ones(300, dtype=bool),
            sample_rate=16000,
        )
        notes = build_notes([Word("love", 0.0, 0.6)], track, cfg, japanese_lyrics=True)
        self.assertEqual([n.lyric for n in notes], ["ら", "ぶ"])

    def _long_track(self, seconds: float = 6.0) -> pitch_mod.F0Track:
        n = int(seconds * 100)
        return pitch_mod.F0Track(
            times=np.arange(n) / 100.0,
            f0=np.full(n, 220.0),
            voiced=np.ones(n, dtype=bool),
            sample_rate=16000,
        )

    def test_morae_sing_into_the_pause_after_the_word(self):
        """Squeezed inside the word itself every mora hits the floor and the
        rhythm goes flat, so a word may use the gap before the next one."""
        cfg = Config(
            auto_octave=False, min_mora_seconds=0.06, max_mora_seconds=0.25,
            pause_borrow=0.5,
        )
        # "understand" is 8 morae said in 0.4s - 0.05s each - but nothing else
        # is spoken until 1.6s.
        words = [Word("understand", 0.0, 0.4), Word("love", 1.6, 1.8)]
        notes = build_notes(words, self._long_track(), cfg, japanese_lyrics=True)

        understand = [n for n in notes if n.lyric in "あんだあすたんど"][:8]
        self.assertEqual(len(understand), 8)
        # 0.4s of speech plus half of the 1.2s pause = 1.0s over 8 morae, not
        # the 0.05s the word alone would have allowed and not the floor.
        self.assertAlmostEqual(understand[0].duration, 0.125, places=3)
        # The onset is untouched: that is what is heard as timing.
        self.assertAlmostEqual(understand[0].start, 0.0, places=3)

    def test_pause_borrow_leaves_a_gap_between_words(self):
        """Taking the whole pause ran words together; half keeps it audible."""
        words = [Word("love", 0.0, 0.2), Word("you", 1.2, 1.4)]
        track = self._long_track()

        def gap_after_first(borrow: float) -> float:
            # The cap is raised out of the way so this measures the borrow.
            cfg = Config(auto_octave=False, pause_borrow=borrow, max_mora_seconds=1.0)
            notes = build_notes(words, track, cfg, japanese_lyrics=True)
            first_word = [n for n in notes if n.lyric in "らぶ"]
            return 1.2 - first_word[-1].end

        self.assertAlmostEqual(gap_after_first(1.0), 0.0, places=2)
        self.assertGreater(gap_after_first(0.5), 0.4)
        self.assertGreater(gap_after_first(0.0), gap_after_first(0.5))

    def test_a_long_pause_does_not_inflate_the_word_before_it(self):
        cfg = Config(auto_octave=False, max_mora_seconds=0.25)
        words = [Word("love", 0.0, 0.2)]  # らぶ, then 6s of nothing
        notes = build_notes(words, self._long_track(), cfg, japanese_lyrics=True)

        for note in notes:
            self.assertLessEqual(note.duration, 0.25 + 1e-6)

    def test_the_voicebank_floor_wins_over_a_short_measurement(self):
        """A note under the sample's preutterance is all consonant, no vowel."""
        cfg = Config(auto_octave=False, min_mora_seconds=0.06)
        words = [Word("understand", 0.0, 0.1), Word("love", 0.2, 0.4)]
        notes = build_notes(words, self._long_track(), cfg, japanese_lyrics=True, mora_floor=0.09)

        self.assertAlmostEqual(notes[0].duration, 0.09, places=3)

    def test_native_mode_leaves_words_alone(self):
        cfg = Config(auto_octave=False)
        track = pitch_mod.F0Track(
            times=np.arange(100) / 100.0,
            f0=np.full(100, 220.0),
            voiced=np.ones(100, dtype=bool),
            sample_rate=16000,
        )
        notes = build_notes([Word("love", 0.0, 0.5)], track, cfg, japanese_lyrics=False)
        self.assertEqual(notes[0].lyric, "love")


class TestPhonemeMapping(unittest.TestCase):
    def test_arpabet_maps_to_the_banks_symbols(self):
        from teto_relay import phonemes

        self.assertEqual(phonemes.arpabet_to_xsampa(["HH", "AH0", "L", "OW1"]), "h @ l oU")

    def test_stress_digits_are_stripped(self):
        from teto_relay import phonemes

        self.assertEqual(phonemes.arpabet_to_xsampa(["EY1"]), "eI")
        self.assertEqual(phonemes.arpabet_to_xsampa(["EY2"]), "eI")

    def test_unstressed_ah_becomes_a_schwa(self):
        """AH0 is a schwa, which the bank samples separately from AH."""
        from teto_relay import phonemes

        self.assertEqual(phonemes.arpabet_to_xsampa(["AH0"]), "@")
        self.assertEqual(phonemes.arpabet_to_xsampa(["AH1"]), "V")

    def test_every_arpabet_phoneme_has_a_mapping(self):
        from teto_relay import phonemes

        # The 39 phonemes CMUdict and forced aligners emit.
        self.assertEqual(len(phonemes.ARPABET_TO_XSAMPA), 39)

    def test_unknown_symbols_are_dropped_not_passed_through(self):
        from teto_relay import phonemes

        self.assertEqual(phonemes.arpabet_to_xsampa(["HH", "QQ", "IY1"]), "h i")

    def test_builtin_hints_use_supported_symbols(self):
        """A hint using a symbol the bank lacks would render as silence."""
        from teto_relay import phonemes, pronunciations

        supported = set(phonemes.ARPABET_TO_XSAMPA.values()) | {phonemes.UNSTRESSED_SCHWA}
        for word, hint in pronunciations.PHONEME_HINTS.items():
            for symbol in hint.split():
                self.assertIn(symbol, supported, f"{word!r} uses unsupported {symbol!r}")


class TestOtoParsing(unittest.TestCase):
    def test_parses_a_standard_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oto.ini"
            path.write_text("_ああ.wav=a あ,100,200,-300,150,50\n", encoding="utf-8")
            entries = parse_oto(path)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e.wav, "_ああ.wav")
        self.assertEqual(e.alias, "a あ")
        self.assertEqual((e.offset, e.consonant, e.cutoff), (100.0, 200.0, -300.0))
        self.assertEqual((e.preutterance, e.overlap), (150.0, 50.0))

    def test_negative_cutoff_is_an_explicit_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oto.ini"
            path.write_text("a.wav=a,0,0,-500,0,0\n", encoding="utf-8")
            entry = parse_oto(path)[0]
        self.assertEqual(entry.duration_ms(file_ms=2000), 500.0)

    def test_blank_alias_falls_back_to_the_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oto.ini"
            path.write_text("hello.wav=,0,0,0,0,0\n", encoding="utf-8")
            self.assertEqual(parse_oto(path)[0].alias, "hello")

    def test_malformed_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oto.ini"
            path.write_text("garbage\na.wav=a,0,0,0,0,0\nb.wav=b,x,y\n", encoding="utf-8")
            self.assertEqual(len(parse_oto(path)), 1)


class TestFlavourDetection(unittest.TestCase):
    def test_vcv_needs_a_vowel_before_the_space(self):
        aliases = ["- あ", "a い", "i う", "u え", "e お"]
        self.assertEqual(_detect_flavour(Path("bank"), aliases), "ja-vcv")

    def test_cv_prefix_markers_are_not_vcv(self):
        """`- あ` and `* あ` contain spaces but are CV, not VCV."""
        aliases = ["あ", "- あ", "* あ", "い", "- い", "* い", "う", "- う"]
        self.assertEqual(_detect_flavour(Path("bank"), aliases), "ja-cv")

    def test_english_xsampa(self):
        self.assertEqual(_detect_flavour(Path("bank"), ["_b{_b{_b-", "d+ju"]), "en-cvvc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
