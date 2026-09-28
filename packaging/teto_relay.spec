# PyInstaller spec for Teto Relay - run through packaging/build.ps1 on Windows.
#
#   pyinstaller --noconfirm --distpath dist --workpath build packaging/teto_relay.spec
#
# Produces dist/TetoRelay/ (onedir) holding two programs that share one set of
# files:
#
#   TetoRelay.exe         no console; opens the control panel in the browser
#   TetoRelayConsole.exe  with a console; --doctor, --list-devices, logs
#
# onedir rather than onefile: onefile unpacks everything (hundreds of MB with
# torch) to a temp folder on every start, which is slow and makes antivirus
# unhappy. The installer and the portable zip both wrap this folder.
#
# UNTESTED: this has not been built on Windows yet. The notes below say which
# packages are known to need help from PyInstaller; if the build or the first
# start fails, the traceback in TetoRelayConsole.exe names the missing module.

import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is set by PyInstaller
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "teto_relay" / "__init__.py").read_text()).group(1)


def optional(fn, *names):
    """Collect from packages that may not be installed (the GPU extras)."""
    out = []
    for name in names:
        try:
            out += fn(name)
        except Exception:  # noqa: BLE001 - not installed: nothing to collect
            pass
    return out


datas = [
    # The control panel page is read from next to the webui module.
    (str(ROOT / "teto_relay" / "web"), "teto_relay/web"),
    # Default pronunciations; the user's own copy goes in the data folder.
    (str(ROOT / "pronunciations.json"), "."),
]
# Data files the libraries read at run time.
datas += optional(collect_data_files, "cmudict", "pykakasi", "librosa", "faster_whisper",
                  "_sounddevice_data", "pythonnet", "clr_loader", "torchcrepe",
                  # Thai word lists and the G2P model's code (teto_relay.thai).
                  "pythainlp")

# Packages that read their own version through importlib.metadata when
# imported. cmudict does, and without its metadata the import fails - which
# `--doctor` caught on the first test build: Japanese mode would have been
# silent in the packaged app.
datas += optional(copy_metadata, "cmudict", "pykakasi", "faster_whisper", "librosa",
                  "ctranslate2", "tokenizers", "huggingface_hub", "pythonnet",
                  "torch", "torchaudio", "torchcrepe", "pythainlp")

binaries = []
# ctranslate2 (faster-whisper) ships its own DLLs, including cuDNN/cuBLAS
# loaders; pythonnet/clr_loader load the .NET host through native helpers.
binaries += optional(collect_dynamic_libs, "ctranslate2", "clr_loader", "_sounddevice_data")

hiddenimports = [
    # pynput and pystray pick their Windows backend at run time.
    "pynput.keyboard._win32", "pynput.mouse._win32", "pystray._win32",
    # pythonnet imports the runtime by name.
    "clr", "pythonnet", "clr_loader",
]
hiddenimports += collect_submodules("teto_relay")
hiddenimports += optional(collect_submodules, "pykakasi", "cmudict")
# pythainlp picks its tokenizer and transliteration engines by name at run time.
hiddenimports += optional(collect_submodules, "pythainlp.tokenize", "pythainlp.transliterate",
                          "pythainlp.corpus")

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "pytest", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

common = dict(
    exclude_binaries=True,
    icon=str(ROOT / "packaging" / "teto_relay.ico"),
    # UPX compression of torch/CUDA DLLs breaks them; leave everything as is.
    upx=False,
)
gui = EXE(pyz, a.scripts, [], name="TetoRelay", console=False, **common)  # noqa: F821
cli = EXE(pyz, a.scripts, [], name="TetoRelayConsole", console=True, **common)  # noqa: F821

COLLECT(gui, cli, a.binaries, a.datas, name="TetoRelay", upx=False)  # noqa: F821

# Written for build.ps1, which names the zip and the installer after it.
os.makedirs(ROOT / "build", exist_ok=True)
(ROOT / "build" / "version.txt").write_text(VERSION, encoding="utf-8")
