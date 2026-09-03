"""Audio device discovery and the feedback-loop guard.

On Windows, sounddevice lists the same physical device once per host API, and
MME truncates names to 31 characters ("CABLE Input (VB-Audio Virtual "). We
therefore match on a substring and rank candidates by host API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sounddevice as sd

log = logging.getLogger(__name__)

# Best first. WASAPI gives the lowest latency and untruncated names; MME is the
# most universally present fallback.
HOSTAPI_PREFERENCE = ["Windows WASAPI", "MME", "Windows DirectSound", "Windows WDM-KS"]


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    hostapi: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float

    @property
    def is_input(self) -> bool:
        return self.max_input_channels > 0

    @property
    def is_output(self) -> bool:
        return self.max_output_channels > 0

    def __str__(self) -> str:
        kind = "in" if self.is_input else ""
        kind += "/" if self.is_input and self.is_output else ""
        kind += "out" if self.is_output else ""
        return f"[{self.index:>2}] {self.name}  ({self.hostapi}, {kind})"


def list_devices() -> list[DeviceInfo]:
    hostapis = sd.query_hostapis()
    out: list[DeviceInfo] = []
    for i, d in enumerate(sd.query_devices()):
        out.append(
            DeviceInfo(
                index=i,
                name=d["name"],
                hostapi=hostapis[d["hostapi"]]["name"],
                max_input_channels=d["max_input_channels"],
                max_output_channels=d["max_output_channels"],
                default_samplerate=d["default_samplerate"],
            )
        )
    return out


def _rank(dev: DeviceInfo) -> int:
    try:
        return HOSTAPI_PREFERENCE.index(dev.hostapi)
    except ValueError:
        return len(HOSTAPI_PREFERENCE)


def find_device(substring: str, kind: str, devices: list[DeviceInfo] | None = None) -> DeviceInfo | None:
    """Best device whose name contains `substring`. `kind` is 'input' or 'output'."""
    if kind not in ("input", "output"):
        raise ValueError(f"kind must be 'input' or 'output', got {kind!r}")
    devices = devices if devices is not None else list_devices()
    needle = substring.casefold()
    matches = [
        d
        for d in devices
        if needle in d.name.casefold() and (d.is_input if kind == "input" else d.is_output)
    ]
    if not matches:
        return None
    return sorted(matches, key=_rank)[0]


def resolve_output(cfg) -> DeviceInfo:
    dev = find_device(cfg.output_device, "output")
    if dev is None:
        available = "\n".join(str(d) for d in list_devices() if d.is_output)
        raise RuntimeError(
            f"No output device matching {cfg.output_device!r}.\n"
            f"Install VB-Cable, or set output_device in config.json to one of:\n{available}"
        )
    return dev


def resolve_input(cfg) -> DeviceInfo | None:
    """None means 'use the system default microphone'."""
    if not cfg.input_device:
        return None
    dev = find_device(cfg.input_device, "input")
    if dev is None:
        available = "\n".join(str(d) for d in list_devices() if d.is_input)
        raise RuntimeError(
            f"No input device matching {cfg.input_device!r}. Available inputs:\n{available}"
        )
    return dev


def default_input() -> DeviceInfo | None:
    try:
        idx = sd.default.device[0]
    except (TypeError, IndexError):
        return None
    if idx is None or idx < 0:
        return None
    for d in list_devices():
        if d.index == idx:
            return d
    return None


def feedback_warning(mic: DeviceInfo | None, out: DeviceInfo) -> str | None:
    """Detect the classic VB-Cable loop: listening to the cable we sing into.

    CABLE Output is the *capture* side of the virtual cable. If the mic is set
    to it while we play into CABLE Input, every rendered utterance is heard as
    fresh speech and the relay feeds itself forever.
    """
    if mic is None:
        return (
            "Input device is the system default. If that default is "
            "'CABLE Output', the relay will hear its own singing and loop. "
            "Set input_device in config.json to your real microphone."
        )
    mic_n, out_n = mic.name.casefold(), out.name.casefold()
    if "cable output" in mic_n and "cable input" in out_n:
        return (
            f"FEEDBACK LOOP: input is {mic.name!r} and output is {out.name!r}. "
            "CABLE Output carries whatever is played into CABLE Input, so the "
            "relay will re-hear itself. Point input_device at your real mic."
        )
    if mic.index == out.index:
        return f"Input and output are the same device ({mic.name!r}); expect feedback."
    return None
