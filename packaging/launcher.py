"""Entry point for the packaged app (PyInstaller).

Double-clicking TetoRelay.exe (no arguments) opens the control panel in the
browser. TetoRelayConsole.exe is the same program with a console, for
`--doctor`, `--list-devices` and the other command-line options.
"""

import sys

from teto_relay.__main__ import main

if __name__ == "__main__":
    args = sys.argv[1:] or ["--web"]
    sys.exit(main(args))
