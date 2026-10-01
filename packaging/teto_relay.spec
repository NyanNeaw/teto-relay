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
# Size: torch was 3.5 GB of a 4.2 GB build, almost all CUDA libraries for
# crepe and the aligner (and the RVC voice engine, since removed). build.ps1
# -WithGpu installs a CPU torch and gives whisper, the one stage the GPU speeds
# up, NVIDIA's cuBLAS wheel, collected below.

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
    # The panel window's title-bar icon (teto_relay.window).
    (str(ROOT / "packaging" / "teto_relay.ico"), "."),
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


def nvidia_dlls():
    """cuBLAS from NVIDIA's pip wheel, beside the other DLLs.

    CTranslate2 loads it by name, and the packaged app's own folder is on its
    DLL search path. Whisper needs nothing else from CUDA: with every cuDNN
    DLL hidden it transcribed the Thai test set identically and as fast.
    """
    import glob

    try:
        import nvidia
    except ImportError:
        return []
    out = []
    for base in nvidia.__path__:
        for dll in glob.glob(os.path.join(base, "cublas", "bin", "*.dll")):
            out.append((dll, "."))
    return out


binaries += nvidia_dlls()
# ctranslate2 (faster-whisper) ships its own DLLs, including cuDNN/cuBLAS
# loaders; pythonnet/clr_loader load the .NET host through native helpers.
binaries += optional(collect_dynamic_libs, "ctranslate2", "clr_loader", "_sounddevice_data")

# pythainlp ships 61 MB of data for taggers, NER, WordNet and Wikipedia titles.
# The relay uses its word list (word_tokenize), syllable list and catalogue.
PYTHAINLP_UNUSED = {
    "wikipedia_titles_th.txt", "wordnet_th.db", "pos_orchid_perceptron.json",
    "sentenceseg_crfcut.model", "tdtb-pt_tagger.json", "volubilis_words_th.txt",
    "pos_tud_perceptron.json", "thai2rom_decoder.onnx", "thai2rom_encoder.onnx",
    "phupha_word_freqs.txt", "pos_ud_perceptron-v0.2.json", "thainer_crf_1_5_1.model",
    "words_th_thai2fit_201810.txt", "crfchunk_orchidpp.model", "orst_words_th.txt",
    "pos_orchid_unigram.json", "tdtb-unigram_tagger.json", "han_solo.crfsuite",
    "blackboard-cls_v1.0.crfsuite", "wikipedia_titles_th.txt",
}
datas = [(src, dst) for src, dst in datas
         if not ("pythainlp" in dst and os.path.basename(src) in PYTHAINLP_UNUSED)]

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
# PyInstaller's own hook for the nvidia packages copies their DLLs again into
# nvidia/<lib>/bin: 700 MB of duplicates of the ones placed above.
a.binaries = [entry for entry in a.binaries  # noqa: F821
              if not entry[0].replace("\\", "/").startswith("nvidia/")]
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
