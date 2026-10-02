"""Where Teto Relay keeps its files.

A source checkout keeps everything next to the code, as it always has:
config.json, out/, the log, .cache/ and .openutau-host/ all live in the
project folder.

A packaged build cannot do that. PyInstaller's onefile mode unpacks the code
into a temporary folder that is deleted on exit, and an installed copy lives in
Program Files, which is read-only. So a packaged build uses:

* **portable**: a `portable.txt` file beside the .exe means "keep everything in
  a `data` folder next to me", so the whole install can live on a USB stick
  or in a zip;
* **installed**: otherwise `%LOCALAPPDATA%\\TetoRelay`.

`TETO_RELAY_HOME` overrides all of this, which is also what the tests use.

This module must stay importable before anything heavy: `teto_relay/__init__`
uses it to point the model caches somewhere sensible before torch loads.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "TetoRelay"
PORTABLE_MARKER = "portable.txt"

#: The folder holding the teto_relay package in a source checkout.
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def frozen() -> bool:
    """True when running from a PyInstaller build."""
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """The folder the program itself lives in (the .exe's folder when frozen)."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return SOURCE_ROOT


def resource_dir() -> Path:
    """Where read-only files shipped with the program are (web page, defaults)."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", install_dir()))
    return SOURCE_ROOT


def portable() -> bool:
    return frozen() and (install_dir() / PORTABLE_MARKER).exists()


def data_dir() -> Path:
    """The writable folder for config, output, logs and caches."""
    override = os.environ.get("TETO_RELAY_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if not frozen():
        return SOURCE_ROOT
    if portable():
        return install_dir() / "data"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def ensure(path: Path) -> Path:
    """Create a directory if needed and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return data_dir() / "config.json"


def cache_dir() -> Path:
    """Model caches: whisper, torch, HuggingFace, the 1.2 GB aligner."""
    return data_dir() / ".cache"


def models_dir() -> Path:
    """Models the installer downloaded (packaging/installer.iss), each in a
    folder of its own; looked in before anything is fetched at run time."""
    return data_dir() / "models"


def host_dir() -> Path:
    """Scratch space for the hosted OpenUtau runtime."""
    return data_dir() / ".openutau-host"


def resolve(value: str | os.PathLike, base: Path | None = None) -> Path:
    """An absolute path for a config value, relative ones taken from `base`.

    The OpenUtau host changes the process's working directory, so a relative
    path left as-is would quietly start meaning something else once the
    renderer has started.
    """
    path = Path(os.path.expandvars(os.fspath(value))).expanduser()
    if not path.is_absolute():
        path = (base or data_dir()) / path
    return path.resolve()
