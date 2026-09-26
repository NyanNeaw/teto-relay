"""System-tray front end so the relay can run with no window at all.

Launch with pythonw.exe to avoid a console:

    .venv\\Scripts\\pythonw.exe -m teto_relay --tray

With no console, the tray is the only place a problem can be shown, so a
failed start turns the icon into a warning, puts the reason at the top of the
menu, and offers Retry and Open log - instead of showing a live icon over a
relay that never started.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

from . import paths
from .app import TetoRelay
from .config import Config
from .errors import TetoRelayError, describe

log = logging.getLogger(__name__)

TETO_RED = (204, 41, 54)
IDLE_GREY = (120, 120, 128)
WARN_AMBER = (230, 150, 20)


def _icon_image(state: str):
    """Filled red when live, a grey ring when paused or starting, amber on error."""
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    box = (8, 8, size - 8, size - 8)
    if state == "live":
        draw.ellipse(box, fill=TETO_RED)
    elif state == "error":
        draw.ellipse(box, fill=WARN_AMBER)
        draw.rectangle((29, 16, 35, 38), fill=(255, 255, 255))
        draw.rectangle((29, 43, 35, 49), fill=(255, 255, 255))
    else:
        draw.ellipse(box, outline=IDLE_GREY, width=6)
    return image


def _open(path: str) -> None:
    """Open a file or folder with whatever Windows uses for it."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - opening the user's own log
        else:
            import subprocess

            subprocess.Popen(["xdg-open", path])
    except Exception:  # noqa: BLE001
        log.warning("could not open %s", path, exc_info=True)


class TrayApp:
    """The relay plus the state the tray menu shows. Kept apart from pystray
    so the start/failure logic can be tested without a desktop."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.relay: TetoRelay | None = None
        self.state = "starting"  # starting | live | paused | error
        self.error = ""
        self.on_change = lambda: None

    def start(self) -> None:
        """Build and start the relay; on failure, record why. Never raises."""
        self.state, self.error = "starting", ""
        self.on_change()
        try:
            if self.relay is None:
                self.relay = TetoRelay(self.cfg)
            self.relay.start()
            self.state = "live"
        except Exception as exc:  # noqa: BLE001 - shown in the tray instead
            # A traceback only for the unexpected; a known problem's message
            # already says what to do.
            log.error("Teto Relay could not start: %s", exc,
                      exc_info=not isinstance(exc, TetoRelayError))
            self.relay = None
            self.state, self.error = "error", describe(exc, self.cfg.log_file)
        self.on_change()

    def toggle_pause(self) -> None:
        if self.relay is None or self.state not in ("live", "paused"):
            return
        if self.relay.paused:
            self.relay.resume()
            self.state = "live"
        else:
            self.relay.pause()
            self.state = "paused"
        self.on_change()

    def status_line(self) -> str:
        if self.state == "error":
            return f"Not running: {self.error[:70]}"
        if self.state == "starting":
            return "Starting - loading models..."
        problems = self.relay.health()["problems"] if self.relay else []
        if problems:
            return problems[0][:80]
        last = self.relay.last_text[:40] if self.relay else ""
        return f"Last: {last or '-'}"

    def quit(self) -> None:
        if self.relay is not None:
            self.relay.stop()


def run_tray(cfg: Config) -> int:
    import pystray

    app = TrayApp(cfg)

    def start_in_background() -> None:
        threading.Thread(target=app.start, name="relay-start", daemon=True).start()

    def choose_bank(key: str):
        def handler(icon, item) -> None:
            try:
                if app.relay is not None:
                    app.relay.set_voicebank(key)
                cfg.voicebank = key
            except Exception:
                log.exception("could not switch to voicebank %s", key)
            icon.update_menu()

        return handler

    def bank_items():
        banks = app.relay.banks if app.relay is not None else []
        current = app.relay.bank.key if app.relay is not None and app.relay.bank else cfg.voicebank
        return [
            pystray.MenuItem(
                b.key, choose_bank(b.key),
                checked=(lambda key: (lambda item: key == current))(b.key), radio=True,
            )
            for b in banks
        ] or [pystray.MenuItem("(none found)", None, enabled=False)]

    def quit_relay(icon, item) -> None:
        app.quit()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: app.status_line(), None, enabled=False),
        pystray.MenuItem(
            lambda item: "Resume" if app.state == "paused" else "Pause",
            lambda icon, item: app.toggle_pause(), default=True,
            visible=lambda item: app.state in ("live", "paused"),
        ),
        pystray.MenuItem("Retry start", lambda icon, item: start_in_background(),
                         visible=lambda item: app.state == "error"),
        pystray.MenuItem("Voicebank", pystray.Menu(lambda: bank_items())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open log", lambda icon, item: _open(cfg.log_file)),
        pystray.MenuItem("Open data folder", lambda icon, item: _open(str(paths.data_dir()))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_relay),
    )

    icon = pystray.Icon("teto-relay", _icon_image("starting"), "Teto Relay", menu)

    def refresh() -> None:
        icon.icon = _icon_image(app.state)
        icon.title = "Teto Relay" if app.state != "error" else "Teto Relay - not running"
        try:
            icon.update_menu()
            if app.state == "error":
                icon.notify(app.error[:250], "Teto Relay could not start")
        except Exception:  # noqa: BLE001 - some backends lack notify
            log.debug("tray refresh failed", exc_info=True)

    app.on_change = refresh
    start_in_background()
    icon.run()
    return 0
