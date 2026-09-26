"""Finding OpenUtau and the voicebanks without being told where they are.

The config used to default to one machine's folders (`D:\\Work\\OpenUtau`,
`D:\\Claude`), so everyone else's first start failed. Empty settings now mean
"look in the usual places"; a path that is set is always used as given, and
the error says which places were searched when nothing is found.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import paths

CORE_DLL = "OpenUtau.Core.dll"


def _env_path(name: str, *parts: str) -> Path | None:
    base = os.environ.get(name)
    return Path(base, *parts) if base else None


def openutau_candidates() -> list[Path]:
    """Places OpenUtau is commonly installed or unzipped to."""
    candidates = [
        _env_path("OPENUTAU_DIR"),
        # The installer puts it under the user's local app data; older and
        # newer installers differ on the `current` subfolder.
        _env_path("LOCALAPPDATA", "OpenUtau", "current"),
        _env_path("LOCALAPPDATA", "OpenUtau"),
        _env_path("LOCALAPPDATA", "Programs", "OpenUtau"),
        _env_path("ProgramFiles", "OpenUtau"),
        _env_path("ProgramFiles(x86)", "OpenUtau"),
        # The portable zip is usually unpacked somewhere under the profile.
        Path.home() / "OpenUtau",
        Path.home() / "Desktop" / "OpenUtau",
        Path.home() / "Downloads" / "OpenUtau",
        paths.install_dir() / "OpenUtau",
    ]
    return [c for c in candidates if c is not None]


def find_openutau(configured: str = "") -> Path | None:
    """The OpenUtau folder: the configured one if set, else the first found."""
    if configured:
        return Path(configured)
    for candidate in openutau_candidates():
        if (candidate / CORE_DLL).exists():
            return candidate
    return None


def voicebank_candidates() -> list[Path]:
    """Folders that usually hold UTAU voicebanks."""
    candidates = [paths.data_dir() / "voicebanks"]
    openutau = find_openutau()
    if openutau is not None:
        candidates.append(openutau / "Singers")
    documents = Path.home() / "Documents"
    candidates += [documents / "OpenUtau" / "Singers", documents / "UTAU" / "voice"]
    return candidates


def _has_bank(folder: Path) -> bool:
    from .voicebank import find_singer_roots

    try:
        return folder.is_dir() and bool(find_singer_roots(folder))
    except OSError:
        return False


def find_voicebank_root(configured: str = "") -> Path:
    """The voicebank folder: the configured one if set, else the first with a bank.

    Falls back to the `voicebanks` folder beside the config (created on
    demand), which is where the panel installs uploaded banks.
    """
    if configured:
        return Path(configured)
    for candidate in voicebank_candidates():
        if _has_bank(candidate):
            return candidate
    return paths.ensure(paths.data_dir() / "voicebanks")
