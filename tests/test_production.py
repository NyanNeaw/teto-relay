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


if __name__ == "__main__":
    unittest.main()
