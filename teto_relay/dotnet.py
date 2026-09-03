"""CoreCLR bootstrap so Python can drive OpenUtau's own assemblies.

Two things here are not obvious and cost real debugging time:

1. OpenUtau ships a *self-contained* runtimeconfig.json (`includedFrameworks`).
   clr_loader rejects it with "Initialization for self-contained components is
   not supported", so we generate a framework-dependent config pointing at the
   system .NET 8 runtime instead.
2. OpenUtau.Core resolves its dependencies (and native worldline.dll /
   onnxruntime.dll) relative to the process working directory, so we chdir into
   the install directory and put it on PATH before touching any type.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_started = False
_lock = threading.Lock()


def _runtimeconfig() -> Path:
    # rollForward must stay inside 8.0.x. Left at the default the host happily
    # loads .NET 9, and then WindowsDesktop's assemblies ask for
    # System.IO.Packaging 9.0.0.0 while OpenUtau ships the 8.x copy next to
    # OpenUtau.Core.dll - which surfaces as a FileLoadException the moment you
    # call Ustx.Load.
    cfg = {
        "runtimeOptions": {
            "tfm": "net8.0",
            "rollForward": "latestPatch",
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
    # Kept inside the project rather than TEMP: the host derives
    # AppContext.BaseDirectory from this file's directory, and PathManager
    # builds DataPath (Cache/, Dictionaries/, Singers/) from that. Left in TEMP
    # - or worse, defaulted to the Python install - OpenUtau would scatter its
    # working directories through somebody else's folders.
    host_dir = PROJECT_ROOT / ".openutau-host"
    host_dir.mkdir(parents=True, exist_ok=True)
    path = host_dir / "teto_relay.runtimeconfig.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


def start(openutau_dir: Path | str) -> Path:
    """Start CoreCLR and reference the OpenUtau assemblies. Idempotent."""
    global _started
    openutau_dir = Path(openutau_dir)

    with _lock:
        if _started:
            return openutau_dir

        core = openutau_dir / "OpenUtau.Core.dll"
        if not core.exists():
            raise FileNotFoundError(
                f"OpenUtau.Core.dll not found in {openutau_dir}. "
                "Set openutau_dir in config.json to your OpenUtau install."
            )

        os.chdir(openutau_dir)
        os.environ["PATH"] = f"{openutau_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        try:
            os.add_dll_directory(str(openutau_dir))
        except (AttributeError, OSError):
            pass

        from clr_loader import get_coreclr
        from pythonnet import set_runtime

        set_runtime(get_coreclr(runtime_config=str(_runtimeconfig())))

        import clr

        clr.AddReference(str(core.with_suffix("")))
        builtin = openutau_dir / "OpenUtau.Plugin.Builtin.dll"
        if builtin.exists():
            clr.AddReference(str(builtin.with_suffix("")))

        _preload_references(openutau_dir, core)
        if builtin.exists():
            _preload_references(openutau_dir, builtin)

        _set_entry_assembly(openutau_dir)
        _register_codepages()
        _redirect_paths()
        _init_docmanager()
        _init_tools()

        _started = True
        log.info("CoreCLR started against %s", openutau_dir)
        return openutau_dir


def _set_entry_assembly(openutau_dir: Path) -> None:
    """Give the hosted runtime an entry assembly.

    This is the keystone of the whole headless route. clr_loader starts CoreCLR
    with no managed entry point, so `Assembly.GetEntryAssembly()` returns null -
    and `PathManager..ctor()` dereferences it, throwing NullReferenceException.

    Because PathManager is a singleton behind Lazy<T>, that one failure cascades:
    `SingerManager.SearchAllSingers()` throws, `VoicebankLoader.SearchAll()`
    quietly returns zero banks, `Phonemizer.DictionariesPath` throws, and the
    phonemizer's async dictionary init faults without ever clearing
    `isDictionaryLoading` - which finally shows up as one empty phoneme per
    note, several layers away from the actual problem.

    .NET 8 exposes an internal `Assembly.SetEntryAssembly`, so we point it at
    OpenUtau's own app assembly and PathManager resolves its paths normally.
    """
    try:
        import clr
        from System.Reflection import Assembly, BindingFlags

        if Assembly.GetEntryAssembly() is not None:
            return

        # PathManager derives DataPath from AppContext.BaseDirectory, which in a
        # hosted process is Python's own directory - so caches and dictionaries
        # would land inside the Python install. AppContext reads this key, so
        # setting it before anything touches PathManager redirects it to the
        # OpenUtau folder (where installed.txt lives).
        from System import AppDomain

        AppDomain.CurrentDomain.SetData("APP_CONTEXT_BASE_DIRECTORY", str(openutau_dir) + "\\")

        app_dll = openutau_dir / "OpenUtau.dll"
        if not app_dll.exists():
            app_dll = openutau_dir / "OpenUtau.Core.dll"
        entry = Assembly.LoadFrom(str(app_dll))

        flags = BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Static
        setter = clr.GetClrType(Assembly).GetMethod("SetEntryAssembly", flags)
        if setter is None:
            log.warning("Assembly.SetEntryAssembly not available; PathManager will fail")
            return
        setter.Invoke(None, [entry])
        log.debug("entry assembly set to %s", app_dll.name)
    except Exception:
        log.warning("could not set the entry assembly", exc_info=True)


def _register_codepages() -> None:
    """Enable Shift-JIS and friends.

    .NET Core ships only Unicode and Latin-1; legacy codepages live in
    System.Text.Encoding.CodePages and must be registered explicitly.
    OpenUtau.exe does this during its own startup, which we never run.

    Without it, VoicebankLoader.LoadInfo throws "'shift_jis' is not a supported
    encoding name" the moment it reads a character.txt - and every UTAU
    voicebank is Shift-JIS. It is also why VoicebankLoader.SearchAll() quietly
    returns zero banks rather than raising.
    """
    try:
        import clr

        clr.AddReference("System.Text.Encoding.CodePages")
    except Exception:  # noqa: BLE001 - the type may already be reachable
        log.debug("could not AddReference System.Text.Encoding.CodePages", exc_info=True)

    try:
        from System.Text import CodePagesEncodingProvider, Encoding

        Encoding.RegisterProvider(CodePagesEncodingProvider.Instance)
        Encoding.GetEncoding("shift_jis")  # prove it took
        log.debug("registered legacy codepages (shift_jis available)")
    except Exception:
        log.warning("could not register legacy codepages; Shift-JIS voicebanks will fail", exc_info=True)


def _preload_references(openutau_dir: Path, assembly_path: Path) -> None:
    """Load an assembly's dependencies from the OpenUtau folder, not the shared framework.

    OpenUtau is published self-contained, so it carries its own copies of
    libraries that also exist in the installed runtime - and they are not the
    same versions. OpenUtau.Core references System.IO.Packaging 9.0.0.0 while
    the shared Microsoft.WindowsDesktop.App 8.x ships 8.0.0.0.

    If we leave that to resolve on demand, pythonnet's AssemblyResolve handler
    fires, finds the shared framework's 8.x copy first, and throws a
    FileLoadException that surfaces from the middle of Ustx.Load. Loading the
    bundled copies up front means the request is already satisfied and the
    resolve event never runs.
    """
    from System.Reflection import Assembly

    try:
        assembly = Assembly.LoadFrom(str(assembly_path))
        references = list(assembly.GetReferencedAssemblies())
    except Exception:  # noqa: BLE001
        log.debug("could not enumerate references of %s", assembly_path.name, exc_info=True)
        return

    loaded = 0
    for ref in references:
        candidate = openutau_dir / f"{ref.Name}.dll"
        if not candidate.exists():
            continue
        try:
            Assembly.LoadFrom(str(candidate))
            loaded += 1
        except Exception:  # noqa: BLE001 - a dependency we cannot preload is not fatal
            log.debug("preload skipped for %s", candidate.name, exc_info=True)
    log.debug("preloaded %d/%d dependencies for %s", loaded, len(references), assembly_path.name)


def _init_docmanager() -> None:
    """Bring up OpenUtau's document manager.

    Phonemizers are not standalone: EnXSampaPhonemizer.SetSinger loads its
    dictionary and reports progress through DocManager.ExecuteCmd. In a hosted
    process DocManager has never been initialised, so PostOnUIThread is null
    and ExecuteCmd throws NullReferenceException from inside SetSinger - which
    surfaces as "the phonemizer produced no phonemes".

    OpenUtau.exe calls Initialize during startup; we do the same, with the
    current thread standing in for the UI thread and a pass-through dispatcher.
    """
    try:
        from System.Threading import Thread
        from System.Threading.Tasks import TaskScheduler

        from OpenUtau.Core import DocManager

        DocManager.Inst.Initialize(Thread.CurrentThread, TaskScheduler.Default)
        log.debug("DocManager initialised")
    except Exception:
        log.warning("DocManager.Initialize failed; phonemizers will not work", exc_info=True)
        return

    _install_ui_queue()


# Callbacks OpenUtau wants run on its "UI thread", waiting to be drained.
_ui_queue = None


def _redirect_paths() -> None:
    """Point OpenUtau's working directories at this project.

    PathManager derives DataPath from `Environment.ProcessPath`, which in a
    hosted process is python.exe - so Cache/, Dictionaries/ and Singers/ would
    be created inside the Python installation. Overriding AppContext's base
    directory does not help, because PathManager never consults it.

    The backing fields are plain strings, which marshal through reflection
    without the boxing problem that afflicts the numeric ones.
    """
    host_dir = PROJECT_ROOT / ".openutau-host"
    cache_dir = host_dir / "Cache"
    try:
        import clr
        from System.Reflection import BindingFlags

        from OpenUtau.Core import PathManager

        host_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)

        flags = BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Instance
        clr_type = clr.GetClrType(PathManager)
        instance = PathManager.Inst
        for field_name, value in (
            ("<DataPath>k__BackingField", str(host_dir)),
            ("<CachePath>k__BackingField", str(cache_dir)),
        ):
            field = clr_type.GetField(field_name, flags)
            if field is not None:
                field.SetValue(instance, value)

        log.debug("OpenUtau data path redirected to %s", host_dir)
    except Exception:
        log.warning("could not redirect OpenUtau's data paths", exc_info=True)


def _init_tools() -> None:
    """Populate the resampler registry.

    WORLDLINE is registered here under the name "worldline". Without it,
    ResamplerItem's constructor indexes the registry directly and throws
    KeyNotFoundException the moment rendering starts.
    """
    try:
        from OpenUtau.Classic import ToolsManager

        ToolsManager.Inst.Initialize()
        log.debug("resamplers registered: %s", [str(r) for r in ToolsManager.Inst.Resamplers])
    except Exception:
        log.warning("ToolsManager.Initialize failed; rendering will not work", exc_info=True)


def _install_ui_queue() -> None:
    """Capture DocManager's UI callbacks in a native queue we drain ourselves.

    ExecuteCmd hands notifications to PostOnUIThread, and getting this delegate
    right took three attempts:

    * A Python lambda that invokes the callback overflows the stack.
    * A Python lambda that ignores it crashes instead - PhonemizerRunner calls
      it from a background thread, where pythonnet cannot marshal the Action
      argument ("Failed to create Python type for System.Action"), which kills
      the phonemizer loop and shows up as zero phonemes.
    * A native no-op survives, but silently discards the results.

    ConcurrentQueue<Action>.Enqueue happens to have exactly the signature
    Action<Action> needs, so we bind the delegate straight to a queue instance.
    That is entirely native - no Python runs on OpenUtau's threads - and the
    callbacks are preserved for us to run on our own thread via `drain_ui`.
    """
    global _ui_queue

    try:
        import clr
        from System import Action, Delegate
        from System.Collections.Concurrent import ConcurrentQueue

        from OpenUtau.Core import DocManager

        queue_type = ConcurrentQueue[Action]
        _ui_queue = queue_type()
        enqueue = clr.GetClrType(queue_type).GetMethod("Enqueue")
        DocManager.Inst.PostOnUIThread = Delegate.CreateDelegate(
            clr.GetClrType(Action[Action]), _ui_queue, enqueue
        )
        log.debug("installed a native queueing PostOnUIThread dispatcher")
    except Exception:
        log.warning("could not set PostOnUIThread", exc_info=True)
        _ui_queue = None


def drain_ui(limit: int = 256) -> int:
    """Run any queued UI callbacks on the calling thread. Returns how many ran."""
    if _ui_queue is None:
        return 0
    ran = 0
    while ran < limit:
        ok, action = _ui_queue.TryDequeue()
        if not ok:
            break
        try:
            action.Invoke()
        except Exception:
            log.debug("queued UI callback failed", exc_info=True)
        ran += 1
    return ran


def started() -> bool:
    return _started
