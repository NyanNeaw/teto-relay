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


def open_panel(url: str, as_app: bool = True) -> str:
    """Show the panel; returns "app" or "browser", whichever was used."""
    if as_app:
        browser = find_app_browser()
        if browser is not None:
            try:
                subprocess.Popen(
                    [str(browser), f"--app={url}", f"--window-size={WINDOW_SIZE}"],
                    close_fds=True,
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                )
                return "app"
            except OSError:
                log.debug("could not open %s as an app window", browser, exc_info=True)
    webbrowser.open(url)
    return "browser"
