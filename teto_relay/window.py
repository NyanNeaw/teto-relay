"""The control panel's own window (`--window URL`), run as a process of its own.

Opened as an Edge app window (appwindow), the panel's taskbar button was
Edge's: pinning it pinned Edge, with Edge's icon. Here the window is ours -
WebView2 through pywebview, the same Chromium engine Edge uses - so its
taskbar button is Teto Relay's, and pinning it pins TetoRelay.exe.

It runs in a process of its own because pywebview hosts WebView2 through
pythonnet on the .NET Framework, and the relay's process has already loaded
.NET 8 for OpenUtau: one process can host only one .NET runtime.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: The taskbar identity shared by everything that is Teto Relay: the window,
#: the installer's shortcuts. Windows groups and pins by it.
APP_ID = "KasaneTeto.TetoRelay"
TITLE = "Teto Relay"
SIZE = (1180, 860)


def relaunch_command() -> tuple[str, str]:
    """What a pinned button runs, and the icon it shows."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        # The windowed program: started from a pin, it opens the panel.
        gui = exe.with_name("TetoRelay.exe")
        target = gui if gui.exists() else exe
        return f'"{target}"', f"{target},0"
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else python
    project = Path(__file__).resolve().parent.parent
    icon = project / "packaging" / "teto_relay.ico"
    # Development: a pin starts in some other folder, where "-m teto_relay"
    # would not be found, so the project folder is put on the path first.
    command = (f'"{runner}" -c "import sys; sys.path.insert(0, r\'{project}\'); '
               f'from teto_relay.__main__ import main; main([\'--web\'])"')
    return command, f"{icon},0" if icon.exists() else f"{runner},0"


def set_process_app_id() -> None:
    """Group this process's windows under Teto Relay, not Python or Edge."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:  # noqa: BLE001 - cosmetic; the window still works
        log.debug("could not set the AppUserModelID", exc_info=True)


def set_window_relaunch(hwnd: int) -> bool:
    """Tell the taskbar what to pin for this window: TetoRelay.exe, its icon.

    Without this a pinned button relaunches whatever process owns the window -
    in development that is python.exe with no arguments.
    """
    if os.name != "nt" or not hwnd:
        return False
    try:
        from win32com.propsys import propsys, pscon  # type: ignore[import-not-found]
    except ImportError:
        return _set_window_relaunch_ctypes(hwnd)
    try:
        command, icon = relaunch_command()
        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
        store.SetValue(pscon.PKEY_AppUserModel_RelaunchCommand, propsys.PROPVARIANTType(command))
        store.SetValue(pscon.PKEY_AppUserModel_RelaunchDisplayNameResource, propsys.PROPVARIANTType(TITLE))
        store.SetValue(pscon.PKEY_AppUserModel_RelaunchIconResource, propsys.PROPVARIANTType(icon))
        store.Commit()
        return True
    except Exception:  # noqa: BLE001
        log.debug("could not set the window's relaunch properties", exc_info=True)
        return False


def _set_window_relaunch_ctypes(hwnd: int) -> bool:
    """The same as set_window_relaunch, without pywin32: raw COM through ctypes."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        def __init__(self, text: str):
            import uuid

            u = uuid.UUID(text)
            super().__init__(u.time_low, u.time_mid, u.time_hi_version,
                             (ctypes.c_ubyte * 8)(*u.bytes[8:]))

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.WORD), ("r2", wintypes.WORD),
                    ("r3", wintypes.WORD), ("pwszVal", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]

    VT_LPWSTR = 31
    appusermodel = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"
    keys = {
        "id": PROPERTYKEY(GUID(appusermodel), 5),
        "command": PROPERTYKEY(GUID(appusermodel), 2),
        "icon": PROPERTYKEY(GUID(appusermodel), 3),
        "name": PROPERTYKEY(GUID(appusermodel), 4),
    }
    iid_store = GUID("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        store = ctypes.c_void_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(GUID),
                                                        ctypes.POINTER(ctypes.c_void_p)]
        if shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid_store), ctypes.byref(store)) != 0:
            return False
        vtable = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0],
                             ctypes.POINTER(ctypes.c_void_p))
        # IPropertyStore: QueryInterface, AddRef, Release, GetCount, GetAt,
        # GetValue, SetValue (6), Commit (7).
        set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
                                       ctypes.POINTER(PROPVARIANT))(vtable[6])
        commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        command, icon = relaunch_command()
        values = {"id": APP_ID, "command": command, "icon": icon, "name": TITLE}
        ok = True
        for name in ("id", "command", "name", "icon"):
            pv = PROPVARIANT(VT_LPWSTR, 0, 0, 0, values[name], None)
            ok &= set_value(store, ctypes.byref(keys[name]), ctypes.byref(pv)) == 0
        ok &= commit(store) == 0
        release(store)
        return ok
    except Exception:  # noqa: BLE001
        log.debug("could not set the window's relaunch properties", exc_info=True)
        return False


def _icon_path() -> str | None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for candidate in (base / "teto_relay.ico", base / "packaging" / "teto_relay.ico"):
        if candidate.exists():
            return str(candidate)
    return None


def _tell_panel_closed(url: str) -> None:
    """The window is gone: the program quits unless another panel is open."""
    import urllib.request

    try:
        request = urllib.request.Request(url.rstrip("/") + "/api/closing", data=b"", method="POST",
                                         headers={"Origin": url.rstrip("/")})
        urllib.request.urlopen(request, timeout=3).read()
    except Exception:  # noqa: BLE001 - the program may already be gone
        log.debug("could not tell the panel it closed", exc_info=True)


def run_window(url: str) -> int:
    """Show the panel in our own window until it is closed. 0, or 1 if no
    window could be made (no WebView2) - the caller falls back to Edge."""
    set_process_app_id()
    try:
        import webview
    except Exception:  # noqa: BLE001
        log.warning("pywebview is not available; using an Edge window instead", exc_info=True)
        return 1

    window = webview.create_window(TITLE, url, width=SIZE[0], height=SIZE[1],
                                   min_size=(420, 520), background_color="#ffffff")

    def shown() -> None:
        hwnd = _find_hwnd()
        if hwnd:
            set_window_relaunch(hwnd)

    window.events.shown += shown
    try:
        webview.start(gui="edgechromium", icon=_icon_path(), private_mode=False,
                      storage_path=str(_storage_folder()))
    except Exception:  # noqa: BLE001 - no WebView2 runtime, most likely
        log.warning("could not open the panel window (WebView2); using Edge instead", exc_info=True)
        return 1
    _tell_panel_closed(url)
    return 0


def _storage_folder() -> Path:
    """Where WebView2 keeps the page's local storage (theme, folded groups)."""
    from . import paths

    folder = paths.data_dir() / "window"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _find_hwnd() -> int:
    """This process's visible top-level window."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pid = os.getpid()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else 0
