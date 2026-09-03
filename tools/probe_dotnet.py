"""Stage 5 discovery - can OpenUtau.Core be loaded and driven headlessly?

Run:  .venv\\Scripts\\python.exe tools\\probe_dotnet.py

Prints a verdict for each capability the render backend would need. Nothing
here is imported by the app; it exists to decide the stage-5 route.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

OPENUTAU_DIR = Path(r"D:\Work\OpenUtau")
CORE = OPENUTAU_DIR / "OpenUtau.Core.dll"
BUILTIN = OPENUTAU_DIR / "OpenUtau.Plugin.Builtin.dll"


def ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def fail(msg: str, exc: BaseException | None = None) -> None:
    print(f"  [FAIL] {msg}")
    if exc is not None:
        for line in traceback.format_exception_only(type(exc), exc):
            print(f"         {line.rstrip()}")


def step(title: str) -> None:
    print(f"\n--- {title} ---")


def make_runtimeconfig() -> Path:
    """A framework-dependent runtimeconfig pointing at the system .NET 8.

    OpenUtau ships a *self-contained* runtimeconfig (includedFrameworks), which
    clr_loader will not accept; it needs a framework reference instead.
    """
    cfg = {
        "runtimeOptions": {
            "tfm": "net8.0",
            "frameworks": [
                {"name": "Microsoft.NETCore.App", "version": "8.0.0"},
                {"name": "Microsoft.WindowsDesktop.App", "version": "8.0.0"},
            ],
            "configProperties": {
                "System.Reflection.Metadata.MetadataUpdater.IsSupported": False,
                "System.Runtime.InteropServices.EnableConsumingManagedCodeFromNativeHosts": True,
            },
        }
    }
    path = Path(tempfile.gettempdir()) / "teto_relay_probe.runtimeconfig.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


def main() -> int:
    print("OpenUtau headless load probe")
    print(f"  OpenUtau dir : {OPENUTAU_DIR}")
    print(f"  Core exists  : {CORE.exists()}  ({CORE.stat().st_size:,} bytes)" if CORE.exists() else "  Core MISSING")

    if not CORE.exists():
        fail("OpenUtau.Core.dll not found; nothing to probe")
        return 1

    # Dependent assemblies and native DLLs (worldline, onnxruntime) resolve
    # relative to the process, so run from inside the OpenUtau directory.
    os.chdir(OPENUTAU_DIR)
    os.environ["PATH"] = f"{OPENUTAU_DIR}{os.pathsep}{os.environ['PATH']}"
    try:
        os.add_dll_directory(str(OPENUTAU_DIR))
    except (AttributeError, OSError):
        pass

    step("1. start CoreCLR")
    runtime = None
    for label, kwargs in [
        ("OpenUtau's own runtimeconfig", {"runtime_config": str(OPENUTAU_DIR / "OpenUtau.runtimeconfig.json")}),
        ("generated framework-dependent runtimeconfig", {"runtime_config": str(make_runtimeconfig())}),
    ]:
        try:
            from clr_loader import get_coreclr
            from pythonnet import set_runtime

            runtime = get_coreclr(**kwargs)
            set_runtime(runtime)
            ok(f"CoreCLR started via {label}")
            break
        except Exception as exc:  # noqa: BLE001 - probe reports every failure
            fail(f"CoreCLR via {label}", exc)
    if runtime is None:
        return 1

    step("2. import clr and reference the OpenUtau assemblies")
    try:
        import clr  # noqa: F401  (pythonnet injects the import hook)

        clr.AddReference(str(CORE.with_suffix("")))
        ok("referenced OpenUtau.Core")
    except Exception as exc:  # noqa: BLE001
        fail("could not reference OpenUtau.Core", exc)
        return 1

    try:
        clr.AddReference(str(BUILTIN.with_suffix("")))
        ok("referenced OpenUtau.Plugin.Builtin")
    except Exception as exc:  # noqa: BLE001
        fail("could not reference OpenUtau.Plugin.Builtin", exc)

    step("3. import the types the renderer needs")
    # Namespaces confirmed by reflecting over the shipped assemblies. Note that
    # RenderEngine is NOT public, so the backend drives IRenderer directly with
    # RenderPhrase objects instead of going through the engine.
    wanted = [
        ("OpenUtau.Core.Ustx", "UProject"),
        ("OpenUtau.Core.Ustx", "USinger"),
        ("OpenUtau.Core.Format", "Ustx"),  # the .ustx reader/writer
        ("OpenUtau.Core.Format", "Formats"),
        ("OpenUtau.Core", "DocManager"),
        ("OpenUtau.Core", "SingerManager"),
        ("OpenUtau.Core.Render", "Renderers"),
        ("OpenUtau.Core.Render", "RenderPhrase"),
        ("OpenUtau.Core.Render", "IRenderer"),
        ("OpenUtau.Classic", "WorldlineRenderer"),
        ("OpenUtau.Classic", "ClassicSingerLoader"),
        ("OpenUtau.Api", "PhonemizerFactory"),
    ]
    loaded = {}
    for ns, name in wanted:
        try:
            module = __import__(ns, fromlist=[name])
            loaded[f"{ns}.{name}"] = getattr(module, name)
            ok(f"{ns}.{name}")
        except Exception as exc:  # noqa: BLE001
            fail(f"{ns}.{name}", exc)

    step("4. does loading a type pull in a UI/display dependency?")
    if loaded:
        ok(f"{len(loaded)}/{len(wanted)} types resolved without a display")
    else:
        fail("no OpenUtau types could be resolved")

    step("VERDICT")
    # These four are what the backend actually cannot work without.
    required = [
        "OpenUtau.Core.Ustx.UProject",
        "OpenUtau.Core.Format.Ustx",
        "OpenUtau.Classic.WorldlineRenderer",
        "OpenUtau.Api.PhonemizerFactory",
    ]
    missing = [name for name in required if name not in loaded]
    if not missing:
        print("  pythonnet route VIABLE - proceed to a render smoke test.")
        return 0
    print(f"  pythonnet route BLOCKED, missing: {missing}")
    print("  Fall back to ctypes against worldline.dll.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
