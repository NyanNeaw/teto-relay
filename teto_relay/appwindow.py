"""Open the control panel in a window of its own, like a desktop app.

The panel is a web page the program serves on 127.0.0.1. Opened as a browser
tab it sat among the user's other tabs and was easy to close by accident.
Edge and Chrome can show a page as an app instead (`--app=URL`): its own
window, no address bar or tabs, and its own taskbar button with the panel's
icon. Every Windows 10/11 PC has Edge, so this almost always works; without
either browser the default browser opens a tab as before.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

log = logging.getLogger(__name__)

WINDOW_SIZE = "1180,860"


def _registered(exe: str) -> Path | None:
    """Where Windows says `exe` is installed (App Paths), if anywhere."""
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                value, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        path = Path(str(value).strip('"'))
        if path.exists():
            return path
    return None


def find_app_browser() -> Path | None:
    """Edge or Chrome - a browser that can open a page as an app window."""
    for exe in ("msedge.exe", "chrome.exe"):
        found = _registered(exe)
        if found:
            return found
    candidates = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                           Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"]
    return next((p for p in candidates if p.exists()), None)


def _window_command(url: str) -> list[str]:
    """Teto Relay's own panel window (teto_relay.window), as a new process."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        gui = exe.with_name("TetoRelay.exe")
        return [str(gui if gui.exists() else exe), "--window", url]
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else python), "-m", "teto_relay", "--window", url]


def _launch(args: list[str]):
    """Start another program as if we were not a packaged app.

    PyInstaller's bootloader points the DLL search at its own folder
    (SetDllDirectory) and sets variables of its own, and a child process
    inherits both. Edge started from the packaged TetoRelay.exe loaded our
    bundled DLLs instead of its own and exited without a window - the panel
    never appeared on a double-click. So the DLL directory is cleared just for
    the launch, and the child gets an environment without PyInstaller's.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI", "_MEI"))}
    previous = None
    kernel32 = None
    if getattr(sys, "frozen", False) and os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(32768)
        if kernel32.GetDllDirectoryW(len(buf), buf):
            previous = buf.value
        kernel32.SetDllDirectoryW(None)
    try:
        return subprocess.Popen(args, env=env, close_fds=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    finally:
        if kernel32 is not None:
            kernel32.SetDllDirectoryW(previous)


#: How long the panel window gets to come up before Edge is used instead.
WINDOW_GRACE = 6.0


def open_panel(url: str, as_app: bool = True) -> str:
    """Show the panel; returns "window", "app" or "browser", whichever was used.

    Teto Relay's own window first: an Edge app window's taskbar button is
    Edge's, so pinning it pinned Edge. Edge's app mode if there is no
    WebView2 for our window, a browser tab if there is no Edge or Chrome.
    """
    if as_app:
        import time

        try:
            child = _launch(_window_command(url))
            deadline = time.monotonic() + WINDOW_GRACE
            while time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.2)
            if child.poll() is None or child.returncode == 0:
                log.info("Opened the control panel in its own window")
                return "window"
            log.info("The panel window could not open (exit %s); trying Edge", child.returncode)
        except OSError:
            log.warning("could not start the panel window", exc_info=True)
        browser = find_app_browser()
        if browser is not None:
            try:
                _launch([str(browser), f"--app={url}", f"--window-size={WINDOW_SIZE}"])
                log.info("Opened the control panel in its own window (%s)", browser.name)
                return "app"
            except OSError:
                log.warning("could not open %s as an app window", browser, exc_info=True)
    webbrowser.open(url)
    log.info("Opened the control panel in the browser: %s", url)
    return "browser"
