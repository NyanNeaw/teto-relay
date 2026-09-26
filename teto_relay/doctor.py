"""`--doctor`: check the installation and say what to fix.

Every check only *looks*: nothing opens an audio stream, starts .NET or loads
a model, so it is safe to run while the relay is running and fast enough to run
on every panel load. Each result carries the fix, written for the person
reading it, not for a developer.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    status: str  # ok | warn | fail
    name: str
    detail: str
    fix: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


PIP = "python -m pip install -r requirements.txt"

# (module, what it is for, required for the default pipeline?)
PACKAGES = [
    ("numpy", "audio maths", True),
    ("soundfile", "reading and writing .wav", True),
    ("librosa", "pyin pitch tracking", True),
    ("faster_whisper", "speech recognition", True),
    ("yaml", "writing OpenUtau projects", True),
    ("cmudict", "English -> Japanese pronunciation", True),
    ("pykakasi", "reading kanji and katakana", True),
    ("pynput", "the push-to-talk key", True),
    ("clr_loader", "hosting OpenUtau (pythonnet)", True),
    ("pystray", "the tray icon", False),
    ("PIL", "the tray icon and voicebank pictures", False),
]


def check_python() -> Check:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 10):
        return Check(FAIL, "Python", f"Python {version}", "Install Python 3.10 or newer (3.11 recommended).")
    return Check(OK, "Python", f"Python {version}")


def check_packages() -> list[Check]:
    out = []
    for module, purpose, required in PACKAGES:
        if _has(module):
            out.append(Check(OK, f"Package {module}", purpose))
        else:
            out.append(Check(FAIL if required else WARN, f"Package {module}",
                             f"missing - needed for {purpose}", f"Run: {PIP}"))
    return out


def check_gpu(cfg) -> list[Check]:
    out = []
    torch_ok = _has("torch")
    wants_gpu = cfg.pitch_method == "crepe" or cfg.use_alignment
    if not torch_ok:
        if wants_gpu:
            out.append(Check(
                WARN, "PyTorch", "not installed - pitch falls back to the slower pyin, "
                "and word timings to whisper's own (about 0.12 s early)",
                "Install torch and torchaudio for your CUDA version, then "
                "python -m pip install -r requirements-gpu.txt",
            ))
        return out
    if cfg.pitch_method == "crepe" and not _has("torchcrepe"):
        out.append(Check(WARN, "torchcrepe", "missing - pitch falls back to pyin",
                         "python -m pip install -r requirements-gpu.txt"))
    if cfg.use_alignment and not _has("torchaudio"):
        out.append(Check(WARN, "torchaudio", "missing - word alignment is skipped",
                         "Install torchaudio matching your torch version."))
    try:
        import torch

        if torch.cuda.is_available():
            out.append(Check(OK, "GPU", f"CUDA: {torch.cuda.get_device_name(0)}"))
        else:
            out.append(Check(WARN, "GPU", "PyTorch cannot see a CUDA GPU - models run on the CPU (slower)",
                             "Install the CUDA build of torch from https://pytorch.org and "
                             "an up-to-date NVIDIA driver."))
    except Exception as exc:  # noqa: BLE001 - a broken torch is itself the finding
        out.append(Check(WARN, "GPU", f"PyTorch failed to load: {exc}",
                         "Reinstall torch for your CUDA version."))
    return out


def check_audio(cfg) -> list[Check]:
    from . import devices

    try:
        found = devices.list_devices()
    except Exception as exc:  # noqa: BLE001
        return [Check(FAIL, "Audio", str(exc))]
    out = []
    try:
        out_dev = devices.resolve_output(cfg)
        out.append(Check(OK, "Output device", out_dev.name))
    except devices.DeviceError as exc:
        out_dev = None
        out.append(Check(FAIL, "Output device", str(exc).splitlines()[0],
                         "Install VB-Cable (https://vb-audio.com/Cable/), or choose another Output."))
    try:
        in_dev = devices.resolve_input(cfg) or devices.default_input()
        if in_dev is None:
            out.append(Check(FAIL, "Microphone", "no input device found",
                             "Plug in a microphone, or set it as the default in Windows sound settings."))
        else:
            out.append(Check(OK, "Microphone", in_dev.name))
    except devices.DeviceError as exc:
        in_dev = None
        out.append(Check(FAIL, "Microphone", str(exc).splitlines()[0], "Choose another Microphone."))
    if out_dev is not None and in_dev is not None:
        warning = devices.feedback_warning(in_dev, out_dev)
        if warning and "FEEDBACK" in warning:
            out.append(Check(FAIL, "Feedback loop", warning, "Set Microphone to your real mic."))
    if not found:
        out.append(Check(FAIL, "Audio", "no audio devices at all", "Check Windows sound settings."))
    return out


def check_voicebanks(cfg) -> list[Check]:
    from . import voicebank

    root = cfg.voicebank_path()
    try:
        banks = voicebank.discover(root)
    except voicebank.VoicebankError as exc:
        return [Check(FAIL if cfg.mode == "utau" else WARN, "Voicebanks", str(exc))]
    if not banks:
        return [Check(FAIL if cfg.mode == "utau" else WARN, "Voicebanks", f"none in {root}",
                      "Copy a UTAU voicebank folder (with oto.ini) there, or install one "
                      "from the control panel, or set the Voicebank folder in Setup.")]
    out = [Check(OK, "Voicebanks", f"{len(banks)} in {root}: " + ", ".join(b.key for b in banks))]
    try:
        chosen = voicebank.select(banks, cfg.voicebank)
        out.append(Check(OK, "Selected voicebank", f"{chosen.key} ({chosen.flavour})"))
        mode = (cfg.lyric_mode or "auto").lower()
        if mode == "japanese" and not chosen.flavour.startswith("ja-"):
            out.append(Check(WARN, "Lyrics", f"lyric_mode is japanese but {chosen.key} is {chosen.flavour}",
                             "Set Lyrics to auto."))
        if mode == "native" and chosen.flavour.startswith("ja-"):
            out.append(Check(WARN, "Lyrics", f"lyric_mode is native but {chosen.key} is {chosen.flavour}",
                             "Set Lyrics to auto."))
    except ValueError as exc:
        out.append(Check(WARN, "Selected voicebank", f"{exc}; the first bank will be used",
                         "Pick a voicebank in the control panel."))
    return out


def _dotnet_roots() -> list[Path]:
    roots = []
    if os.environ.get("DOTNET_ROOT"):
        roots.append(Path(os.environ["DOTNET_ROOT"]))
    for var in ("ProgramFiles", "ProgramW6432"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]) / "dotnet")
    found = shutil.which("dotnet")
    if found:
        roots.append(Path(found).resolve().parent)
    return roots


def check_openutau(cfg) -> list[Check]:
    if cfg.mode == "voice" or cfg.renderer_backend == "null":
        return [Check(OK, "OpenUtau", "not needed with the current settings")]
    from .locate import CORE_DLL, openutau_candidates

    out = []
    folder = cfg.openutau_path()
    if folder is None:
        out.append(Check(FAIL, "OpenUtau", "not found",
                         "Install OpenUtau (https://github.com/stakira/OpenUtau) and set the "
                         "OpenUtau folder in Setup. Searched: "
                         + "; ".join(str(p) for p in openutau_candidates()[:6])))
    elif not (folder / CORE_DLL).exists():
        out.append(Check(FAIL, "OpenUtau", f"{CORE_DLL} is not in {folder}",
                         "Set the OpenUtau folder to the one that contains OpenUtau.exe."))
    else:
        out.append(Check(OK, "OpenUtau", str(folder)))

    if sys.platform != "win32":
        out.append(Check(WARN, ".NET 8", "OpenUtau hosting is only supported on Windows",
                         "Use renderer_backend: null here."))
        return out
    needed = {"Microsoft.NETCore.App": False, "Microsoft.WindowsDesktop.App": False}
    for root in _dotnet_roots():
        for name in needed:
            shared = root / "shared" / name
            if shared.is_dir() and any(p.name.startswith("8.") for p in shared.iterdir()):
                needed[name] = True
    missing = [name for name, ok in needed.items() if not ok]
    if missing:
        out.append(Check(FAIL, ".NET 8", "missing: " + ", ".join(missing),
                         "Install the .NET 8 Desktop Runtime (x64) from "
                         "https://dotnet.microsoft.com/download/dotnet/8.0"))
    else:
        out.append(Check(OK, ".NET 8", "Desktop Runtime 8.x found"))
    return out


def check_voice_mode(cfg) -> list[Check]:
    if cfg.mode != "voice":
        return []
    out = []
    if not cfg.rvc_model or not Path(cfg.rvc_model).exists():
        out.append(Check(FAIL, "RVC model", cfg.rvc_model or "not set",
                         "Install a .pth voice model from the control panel."))
    else:
        out.append(Check(OK, "RVC model", cfg.rvc_model))
    if not _has("rvc"):
        out.append(Check(FAIL, "RVC package", "the rvc package is not installed",
                         "Voice conversion is an optional extra; see the README."))
    return out


def check_storage() -> list[Check]:
    folder = paths.data_dir()
    try:
        paths.ensure(folder)
        probe = folder / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return [Check(FAIL, "Data folder", f"{folder} is not writable ({exc})",
                      "Run from a folder you can write to, or set TETO_RELAY_HOME.")]
    free = shutil.disk_usage(folder).free / 1e9
    if free < 3:
        return [Check(WARN, "Data folder", f"{folder} ({free:.1f} GB free)",
                      "Models need about 3 GB on first run; free some space.")]
    return [Check(OK, "Data folder", f"{folder} ({free:.0f} GB free)")]


def run_checks(cfg) -> list[Check]:
    checks = [check_python(), *check_packages(), *check_storage()]
    for group in (check_audio, check_voicebanks, check_openutau, check_gpu, check_voice_mode):
        try:
            checks += group(cfg)
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the rest
            checks.append(Check(WARN, group.__name__.replace("check_", "").title(),
                                f"could not be checked: {exc}"))
    return checks


LABEL = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


def format_checks(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{LABEL[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != OK:
            lines.append(f"       -> {c.fix}")
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    if fails:
        lines.append(f"\n{fails} problem(s) to fix before Teto Relay can sing, {warns} warning(s).")
    elif warns:
        lines.append(f"\nReady, with {warns} warning(s).")
    else:
        lines.append("\nEverything looks ready.")
    return "\n".join(lines)


def run_doctor(cfg) -> int:
    from . import __version__

    print(f"Teto Relay {__version__} - checking your setup\n")
    checks = run_checks(cfg)
    print(format_checks(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0
