"""Global push-to-talk key.

The relay runs in the background with no window, so the key has to be captured
system-wide rather than from a focused UI. pynput's listener does that without
needing administrator rights.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


#: Windows virtual-key codes of the punctuation keys, where they sit on a US
#: keyboard. Letters and digits are their own upper-case character code.
_OEM_VK = {"`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
           ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF}


def _key_vk(char: str) -> int | None:
    """The physical key (Windows virtual-key code) that types `char` on a US
    keyboard, or None."""
    upper = char.upper()
    if "A" <= upper <= "Z" or "0" <= upper <= "9":
        return ord(upper)
    return _OEM_VK.get(char)


def parse_key(name: str):
    """Resolve a config string like "f8", "ctrl_r" or "`" to a pynput key.

    Function keys and modifiers come from pynput.keyboard.Key; anything else is
    a character key, kept with the physical key that types it (see _matches).
    """
    from pynput import keyboard

    name = (name or "").strip().lower()
    if not name:
        raise ValueError("ptt_key is empty")
    if hasattr(keyboard.Key, name):
        return getattr(keyboard.Key, name)
    if len(name) == 1:
        vk = _key_vk(name)
        return keyboard.KeyCode(vk=vk, char=name) if vk is not None else keyboard.KeyCode.from_char(name)
    raise ValueError(
        f"unrecognised ptt_key {name!r}. Use a single character, or one of: "
        f"{', '.join(sorted(k for k in dir(keyboard.Key) if not k.startswith('_')))}"
    )


def _matches(key, target) -> bool:
    """Compare keys, tolerating the char/vk variations pynput reports.

    A character key is matched by the physical key first: with the keyboard
    switched to Thai, the P key types "ย" and was not the talk key any more -
    push-to-talk went dead until the layout was switched back. The character
    is the fallback where an event carries no key code.
    """
    target_vk = getattr(target, "vk", None)
    key_vk = getattr(key, "vk", None)
    if target_vk is not None and key_vk is not None:
        return key_vk == target_vk
    if key == target:
        return True
    target_char = getattr(target, "char", None)
    key_char = getattr(key, "char", None)
    if target_char is not None and key_char is not None:
        return key_char.lower() == target_char.lower()
    return False


class PushToTalkListener:
    """Calls `on_press` when the key goes down and `on_release` when it comes up.

    pynput repeats key-down events while a key is held, so both callbacks fire
    exactly once per physical press.
    """

    def __init__(self, key_name: str, on_press: Callable[[], None], on_release: Callable[[], None]):
        self.key_name = key_name
        self._target = parse_key(key_name)
        self._on_press = on_press
        self._on_release = on_release
        self._held = threading.Event()
        self._listener = None

    @property
    def held(self) -> bool:
        return self._held.is_set()

    def start(self) -> None:
        from pynput import keyboard

        def handle_press(key):
            if _matches(key, self._target) and not self._held.is_set():
                self._held.set()
                try:
                    self._on_press()
                except Exception:
                    log.exception("push-to-talk press handler failed")

        def handle_release(key):
            if _matches(key, self._target) and self._held.is_set():
                self._held.clear()
                try:
                    self._on_release()
                except Exception:
                    log.exception("push-to-talk release handler failed")

        self._listener = keyboard.Listener(on_press=handle_press, on_release=handle_release)
        self._listener.daemon = True
        self._listener.start()
        log.info("Push-to-talk armed: hold [%s] to record", self.key_name.upper())

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
