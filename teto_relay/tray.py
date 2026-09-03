"""System-tray front end so the relay can run with no window at all.

Launch with pythonw.exe to avoid a console:

    .venv\\Scripts\\pythonw.exe -m teto_relay --tray
"""

from __future__ import annotations

import logging
import threading

from PIL import Image, ImageDraw

from .app import TetoRelay
from .config import Config

log = logging.getLogger(__name__)

TETO_RED = (204, 41, 54)
IDLE_GREY = (120, 120, 128)


def _icon_image(active: bool) -> Image.Image:
    """A filled circle when live, a hollow ring when paused."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    colour = TETO_RED if active else IDLE_GREY
    if active:
        draw.ellipse((8, 8, size - 8, size - 8), fill=colour)
    else:
        draw.ellipse((8, 8, size - 8, size - 8), outline=colour, width=6)
    return image


def run_tray(cfg: Config) -> int:
    import pystray

    relay = TetoRelay(cfg)
    ready = threading.Event()

    def start_relay() -> None:
        try:
            relay.start()
        except Exception:
            log.exception("relay failed to start")
        finally:
            ready.set()

    threading.Thread(target=start_relay, name="relay-start", daemon=True).start()

    def toggle(icon, item) -> None:
        if relay.paused:
            relay.resume()
        else:
            relay.pause()
        icon.icon = _icon_image(not relay.paused)
        icon.update_menu()

    def choose_bank(key: str):
        def handler(icon, item) -> None:
            try:
                relay.set_voicebank(key)
            except Exception:
                log.exception("could not switch to voicebank %s", key)
            icon.update_menu()

        return handler

    def quit_relay(icon, item) -> None:
        relay.stop()
        icon.stop()

    bank_items = [
        pystray.MenuItem(
            b.key,
            choose_bank(b.key),
            checked=(lambda key: (lambda item: relay.bank.key == key))(b.key),
            radio=True,
        )
        for b in relay.banks
    ]

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: "Resume" if relay.paused else "Pause", toggle, default=True),
        pystray.MenuItem("Voicebank", pystray.Menu(*bank_items)),
        pystray.MenuItem(lambda item: f"Last: {relay.last_text[:40] or '-'}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_relay),
    )

    icon = pystray.Icon("teto-relay", _icon_image(True), "Teto Relay", menu)
    icon.run()
    return 0
