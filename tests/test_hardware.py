"""Tests that render through the real OpenUtau engine.

They need OpenUtau, the .NET 8 runtime and a Japanese CV voicebank, so they
only run when asked:

    set TETO_RELAY_HARDWARE_TESTS=1
    .venv\\Scripts\\python.exe -m unittest tests.test_hardware

Each one pins down something that unit tests with mocks cannot see, because
it is about what the engine does with what we hand it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENABLED = os.environ.get("TETO_RELAY_HARDWARE_TESTS") == "1"


@unittest.skipUnless(ENABLED, "set TETO_RELAY_HARDWARE_TESTS=1 to render through OpenUtau")
class TestOpenUtauRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from teto_relay.config import Config
        from teto_relay.render import make_renderer
        from teto_relay.voicebank import discover

        saved = Config.load()
        cls.cfg = Config(voicebank_root=saved.voicebank_root, openutau_dir=saved.openutau_dir)
        banks = [b for b in discover(cls.cfg.voicebank_path()) if b.flavour == "ja-cv"]
        if not banks:
            raise unittest.SkipTest("no Japanese CV voicebank found")
        cls.bank = banks[0]
        cls.renderer = make_renderer(cls.cfg, cls.bank)
        if cls.renderer.name != "openutau":
            raise unittest.SkipTest("OpenUtau did not start")

    def sung_midi(self, notes):
        import librosa
        import numpy as np
        import soundfile as sf

        from teto_relay.ustx import write_ustx

        with tempfile.TemporaryDirectory() as tmp:
            path = write_ustx(notes, Path(tmp) / "t.ustx", self.bank, self.cfg)
            audio, rate = sf.read(str(self.renderer.render(path, Path(tmp) / "t.wav")),
                                  dtype="float32", always_2d=True)
        audio = librosa.resample(audio.mean(axis=1), orig_sr=rate, target_sr=16000)
        f0, voiced, _ = librosa.pyin(audio, fmin=150, fmax=1200, sr=16000,
                                     frame_length=1024, hop_length=160)
        return librosa.hz_to_midi(f0)

    def test_pitch_stays_on_the_notes_when_the_part_starts_late(self):
        # Every real utterance's part starts where the first word was said.
        # OpenUtau placed the phonemes relative to the part but read the pitch
        # at project time, so everything was sung that much late: 3.6
        # semitones of error with the part at 1.0 s, against 0.2 at 0 s.
        import numpy as np

        from teto_relay.notes import Note

        tones = [57, 59, 61, 62, 64, 67, 64, 59]
        start, step = 0.7, 0.2
        notes = [Note("ら", start + i * step, start + (i + 1) * step, tone, legato=i > 0)
                 for i, tone in enumerate(tones)]
        sung = self.sung_midi(notes)
        # The render starts at the first sound, i.e. at the first note.
        grid = np.array([tones[min(int(i * 0.01 / step), len(tones) - 1)] for i in range(len(sung))], float)
        error = float(np.nanmean(np.abs(grid - sung)))
        self.assertLess(error, 0.6, f"sung {error:.2f} semitones away from the notes")

    def test_pitch_curve_units(self):
        # OpenUtau's pitch points are tenths of a semitone: the contour used to
        # go out in cents and every inflection was ten times too big.
        import numpy as np

        from teto_relay.notes import Note

        flat = Note("あ", 0.0, 1.2, 60, contour=[(0.0, 300.0), (1200.0, 300.0)])  # +3 st, in cents
        sung = self.sung_midi([flat])
        middle = sung[len(sung) // 3: 2 * len(sung) // 3]
        self.assertAlmostEqual(float(np.nanmedian(middle)), 63.0, delta=0.4)


if __name__ == "__main__":
    unittest.main()
