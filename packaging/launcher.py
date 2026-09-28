"""Entry point for the packaged app (PyInstaller).

Double-clicking TetoRelay.exe (no arguments) opens the control panel in the
browser. TetoRelayConsole.exe is the same program with a console, for
`--doctor`, `--list-devices` and the other command-line options.
"""

import sys
from pathlib import Path

from teto_relay.__main__ import main

# Options that make sense without a console window.
WINDOWED_MODES = {"--web", "--tray"}

if __name__ == "__main__":
    args = sys.argv[1:] or ["--web"]
    # TetoRelay.exe has no console, so without --web or --tray it ran the relay
    # headless and invisible - listening to the mic with no window to stop it
    # (TetoRelay.exe --config x.json did exactly that). It always has a face.
    if Path(sys.executable).stem.lower() == "tetorelay" and not WINDOWED_MODES & set(args):
        args = ["--web", *args]
    sys.exit(main(args))
