"""A small local control panel.

Deliberately dependency-free: it runs on `http.server` from the standard
library, so it adds nothing to disk. Function over decoration - the form is
generated from the Config dataclass, so any setting added later (including the
voice-conversion mode) appears here without touching this file.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import threading
from collections import deque
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, coerce
from .errors import TetoRelayError, describe

log = logging.getLogger(__name__)

# The handful of settings on the main screen, most-changed first: how high she
# sings and how loud, whether she speaks or sings, what language you speak, and
# only then the devices, which are set once. Everything else is under "All
# settings", because a person who wants to sing through Teto needs these - not
# a phonemizer. `voicebank` has its own picker beside the character, and `mode`
# is the UTAU / Voice switch in the top bar.
ESSENTIALS: list[str] = [
    "singing_style", "double_when", "transpose", "playback_gain", "language", "lyrics_hint",
    "input_device", "output_device", "ptt_key",
]

# "All settings", in order of how often they matter. Anything not listed still
# appears, under "Other", so new options are never silently hidden.
GROUPS: dict[str, list[str]] = {
    "Singing": [
        "legato", "expressive", "double_voice", "scale", "scale_key", "sung_melody_range", "sung_contour_amount",
        "vibrato_min_seconds", "vibrato_depth_cents", "vibrato_period_ms",
        "final_hold_seconds", "emit_contour",
    ],
    # Speed vs accuracy lives here: device and compute type are the two biggest
    # levers on how long whisper takes.
    "Listening": [
        "whisper_model", "thai_speech_model", "whisper_device", "whisper_compute_type", "beam_size",
        "initial_prompt", "align_morae", "use_alignment", "align_device", "no_speech_threshold",
    ],
    "Recording": ["capture_mode", "silence_ms", "min_chunk_ms", "max_chunk_ms"],
    "Voice engine (RVC)": [
        "rvc_model", "rvc_index", "rvc_pitch", "rvc_index_rate", "rvc_protect",
        "rvc_f0_method", "rvc_filter_radius", "rvc_rms_mix_rate", "rvc_device",
        "voice_streaming", "stream_block_ms", "stream_context_ms", "stream_crossfade_ms",
    ],
    # Where things are. Empty means "look in the usual places".
    "Setup": ["openutau_dir", "voicebank_root", "renderer_backend", "lyric_mode",
              "persistent_output", "keep_input_audio", "panel_window"],
    "Fine tuning: pitch": [
        "target_tone", "shift_mode", "stable_shift", "shift_tolerance", "max_shift",
        "fix_octave_errors", "contour_smooth_ms", "contour_points", "contour_range_cents",
        "pitch_method", "crepe_model", "crepe_device", "f0_min", "f0_max",
    ],
    "Fine tuning: timing": [
        "vowel_on_beat", "min_note_seconds", "seconds_per_syllable", "note_gap_ms", "phrase_gap_ms",
        "onset_push_ms",
        # Japanese mode only - a note there is one mora, not one word.
        "min_mora_seconds", "max_mora_seconds", "pause_borrow",
    ],
}

# A line under each group's title, which engine it belongs to (the page hides
# the other engine's groups), and whether it starts folded away.
GROUP_INFO: dict[str, dict] = {
    "Singing": {"note": "How sung she sounds. Singing style itself is on the main screen.",
                "engine": "utau"},
    "Listening": {"note": "Speech recognition: how well and how fast your words are heard.",
                  "engine": "utau"},
    "Recording": {"note": "When a phrase starts and ends."},
    "Voice engine (RVC)": {"note": "Only for the Voice engine: your delivery in her timbre.",
                           "engine": "voice"},
    "Setup": {"note": "Where OpenUtau and your voicebanks are, and fallbacks."},
    "Fine tuning: pitch": {"note": "Pitch tracking and how your voice is moved onto hers. "
                                   "The defaults are measured; change with care.",
                           "engine": "utau", "collapsed": True},
    "Fine tuning: timing": {"note": "Note lengths and gaps. The defaults are measured.",
                            "engine": "utau", "collapsed": True},
    "Other": {"note": "Rarely needed.", "collapsed": True},
}

# Shown elsewhere on the page, so not in "Other" either.
SHOWN_ELSEWHERE = {"mode", "voicebank"}

HIDE = {"out_dir", "log_file", "queue_size", "keep_files"}

# The panel is a local web server, and any page open in the same browser can
# send requests to it. Without these checks a random website could rewrite the
# config (paths included), start the relay, or upload a .pth - which is opened
# with torch, i.e. unpickled. Two independent guards:
#
# * Host must be this machine. That defeats DNS rebinding, where evil.example
#   is made to resolve to 127.0.0.1 so the browser treats the panel as
#   same-origin with the attacker's page.
# * Every request that changes something must carry TOKEN_HEADER. A page on
#   another origin cannot add a custom header without a CORS preflight, and
#   this server never answers one, so the browser refuses to send it.
TOKEN_HEADER = "X-Teto-Relay"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def host_allowed(host_header: str | None, port: int) -> bool:
    """Whether a Host header names this machine on the panel's port."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8765
        name, _, rest = host.partition("]")
        name += "]"
        port_part = rest[1:] if rest.startswith(":") else ""
    else:
        name, _, port_part = host.partition(":")
    if name not in LOCAL_HOSTS:
        return False
    return not port_part or port_part == str(port)


def origin_allowed(origin: str | None, port: int) -> bool:
    """A missing Origin is fine (same-origin GETs omit it); a foreign one is not."""
    if not origin:
        return True
    from urllib.parse import urlparse

    parsed = urlparse(origin)
    if parsed.scheme != "http" or not parsed.hostname:
        return False
    host = parsed.hostname
    if ":" in host:  # urlparse strips the brackets from IPv6
        host = f"[{host}]"
    return host in LOCAL_HOSTS and (parsed.port or 80) == port

# Settings the pipeline reads once, when it builds a model or opens a device.
# Everything else is read per utterance, so changing it applies immediately -
# `language`, for one, is passed to whisper on every transcribe call. Only
# these need the relay stopped and started again.
#: Of LOADED_ONCE, the settings a relay restart cannot apply.
NEEDS_PROGRAM_RESTART = {"openutau_dir"}
#: Seconds after the last start-only setting changes before the relay restarts.
RESTART_DELAY = 0.8

LOADED_ONCE = {
    # Read when push-to-talk is armed and when the relay is built.
    "ptt_key", "openutau_dir", "voicebank_root",
    "voice_streaming", "stream_block_ms", "stream_context_ms", "stream_crossfade_ms",
    "whisper_model", "whisper_device", "whisper_compute_type",
    "input_device", "output_device", "capture_mode",
    "mode", "renderer_backend",
    "pitch_method", "crepe_model", "crepe_device", "align_device",
    "rvc_model", "rvc_index", "rvc_device",
}

# Numeric settings worth a slider, with the range that is actually useful -
# `transpose` is musically meaningful to about an octave either way, and
# `pause_borrow` is a fraction. Anything numeric and not listed stays a plain
# box, because a slider over an unbounded number is worse than typing it.
RANGES: dict[str, tuple[float, float, float]] = {
    "transpose": (-12, 12, 1),
    "max_shift": (0, 36, 1),
    "shift_tolerance": (0, 12, 0.5),
    "target_tone": (0, 84, 1),
    "playback_gain": (0, 2, 0.05),
    "beam_size": (1, 10, 1),
    "no_speech_threshold": (0, 1, 0.05),
    "silence_ms": (100, 2000, 50),
    "min_chunk_ms": (100, 2000, 50),
    "max_chunk_ms": (2000, 20000, 500),
    "note_gap_ms": (0, 60, 1),
    "min_note_seconds": (0.04, 0.6, 0.01),
    "seconds_per_syllable": (0.05, 0.6, 0.01),
    "min_mora_seconds": (0.04, 0.3, 0.005),
    "max_mora_seconds": (0.08, 0.6, 0.01),
    "pause_borrow": (0, 1, 0.05),
    "contour_smooth_ms": (0, 200, 5),
    "contour_points": (2, 12, 1),
    "contour_range_cents": (0, 1200, 25),
    "f0_min": (40, 400, 5),
    "f0_max": (400, 2000, 10),
    "rvc_pitch": (-24, 24, 1),
    "rvc_index_rate": (0, 1, 0.05),
    "rvc_filter_radius": (0, 7, 1),
    "rvc_rms_mix_rate": (0, 1, 0.05),
    "rvc_protect": (0, 0.5, 0.01),
    "sung_contour_amount": (0, 1, 0.05),
    "sung_melody_range": (0.5, 3, 0.1),
    "double_voice": (0, 1, 0.05),
    "stream_block_ms": (100, 1000, 10),
    "stream_context_ms": (0, 2000, 50),
    "stream_crossfade_ms": (0, 150, 5),
    "vibrato_min_seconds": (0.1, 1.5, 0.05),
    "vibrato_depth_cents": (0, 100, 5),
    "vibrato_period_ms": (80, 400, 5),
    "final_hold_seconds": (0, 1.5, 0.05),
}

# Settings whose value is a duration in seconds, shown as ms on the slider
# readout because that is how they are discussed everywhere else.
SECONDS = {
    "vibrato_min_seconds", "final_hold_seconds",
    "min_note_seconds", "seconds_per_syllable", "min_mora_seconds", "max_mora_seconds",
}

# Names people recognise, and a line of help where the name is not enough.
# Anything missing falls back to the field name with its underscores removed.
LABELS: dict[str, list[str]] = {
    "openutau_dir": ["OpenUtau folder", "The folder with OpenUtau.exe. Empty searches the usual places."],
    "voicebank_root": ["Voicebank folder", "Where your UTAU voicebanks are. Empty searches the usual places."],
    "input_device": ["Microphone", "Blank uses whatever Windows is set to."],
    "output_device": ["Output", "Where Teto sings. VB-Cable sends her into other apps."],
    "ptt_key": ["Push-to-talk key", "Hold this while you speak."],
    "transpose": ["Transpose", "Moves her whole range, in semitones."],
    "playback_gain": ["Volume", ""],
    "lyric_mode": ["Lyrics", "Auto follows the voicebank: Japanese banks sing morae."],
    "mode": ["Engine", "utau sings your speech as notes; voice keeps your delivery in her timbre."],
    "renderer_backend": ["Renderer", "Tone synthesis is the fallback if OpenUtau fails."],
    "capture_mode": ["Recording", "Push-to-talk, or split automatically on silence."],
    "whisper_model": ["Speech model", "Bigger hears better and takes longer."],
    "thai_speech_model": ["Thai speech model", "When the language is Thai, listen with a Whisper trained on Thai "
                          "(Thonburian Whisper). It hears Thai far better; 0.5 GB download the first time."],
    "panel_window": ["Open the panel as", "Its own window (like an app) or a tab in your browser. "
                     "Takes effect the next time Teto Relay opens."],
    "whisper_device": ["Listen on", "cuda is much faster than cpu, if it starts."],
    "whisper_compute_type": ["Listening precision", "int8 is fastest; float16 needs a GTX 16xx/RTX card (older ones use int8)."],
    "rvc_f0_method": ["Pitch tracking", "crepe is accurate; pm is fastest and rougher."],
    "rvc_index_rate": ["Voice likeness", "Higher leans on the model's index: closer to her, less like you."],
    "rvc_protect": ["Protect consonants", "Higher keeps your breath and consonants intact."],
    "rvc_pitch": ["Pitch shift", "Semitones. +12 is an octave up."],
    "rvc_device": ["Convert on", ""],
    "rvc_model": ["Voice model", ""],
    "rvc_index": ["Voice index", "Optional. Improves timbre; missing is a warning, not an error."],
    "rvc_filter_radius": ["Smooth pitch", "Higher is smoother and less breathy."],
    "rvc_rms_mix_rate": ["Keep your dynamics", "0 uses her loudness curve, 1 keeps yours."],
    "language": ["Language", "The language you speak. Thai uses a Thai-trained speech model "
                 "(0.5 GB download the first time); the .en models only hear English."],
    "lyrics_hint": ["Song lyrics", "Singing a song? Paste the lines you'll sing so every word is heard right. Clear it after."],
    "initial_prompt": ["Vocabulary hint", "Words to expect, so they are not misheard."],
    "beam_size": ["Search width", "Higher is more accurate and slower."],
    "no_speech_threshold": ["Silence cutoff", "Higher discards more as background noise."],
    "use_alignment": ["Measure word timing", "English: re-times words sound by sound. Can hurt accented English."],
    "align_morae": ["Measure syllable timing", "Japanese: each syllable starts where you sang its vowel."],
    "align_device": ["Alignment on", ""],
    "pitch_method": ["Pitch tracker", "crepe is faster and steadier than pyin."],
    "crepe_model": ["Pitch model", ""],
    "crepe_device": ["Pitch on", ""],
    "target_tone": ["Target pitch", "0 uses the pitch the voicebank was recorded at."],
    "shift_mode": ["Shift by", "Semitones land closer; octaves keep your pitch class."],
    "stable_shift": ["Hold pitch between phrases", ""],
    "shift_tolerance": ["Pitch drift allowed", ""],
    "max_shift": ["Largest shift", ""],
    "fix_octave_errors": ["Fix octave slips", ""],
    "f0_min": ["Lowest pitch tracked", ""],
    "f0_max": ["Highest pitch tracked", ""],
    "emit_contour": ["Follow your intonation", "Bends each note the way you said it."],
    "contour_smooth_ms": ["Intonation smoothing", "Less smoothing is more expressive, more warbly."],
    "contour_points": ["Intonation detail", ""],
    "contour_range_cents": ["Intonation range", ""],
    "singing_style": ["Singing style", "speech follows your voice exactly; sung puts it in a key, with vibrato."],
    "scale": ["Scale", "Sung style: which notes are allowed."],
    "scale_key": ["Key", "Sung style: auto finds it from what you say, or pick one (C, F#, Bb...)."],
    "double_voice": ["Double her voice", "Layers a second take under her for a fuller sound. 0 is off."],
    "double_when": ["Double voice", "A fuller, layered sound on the phrases you sing (not the ones you speak)."],
    "sung_melody_range": ["Melody range", "Sung style: 1 keeps your intervals; higher makes the tune move more."],
    "sung_contour_amount": ["Keep your inflection", "Sung style: 0 holds each note flat, 1 keeps all of it."],
    "vibrato_min_seconds": ["Vibrato from", "Sung style: notes at least this long get vibrato."],
    "vibrato_depth_cents": ["Vibrato depth", "In cents; 100 is a semitone."],
    "vibrato_period_ms": ["Vibrato speed", "One wobble every this many ms."],
    "final_hold_seconds": ["Hold the last note", "Sung style: how much longer the phrase's last note lasts."],
    "voice_streaming": ["Convert while I talk", "Voice engine: real-time, in blocks. Experimental; needs a fast GPU."],
    "stream_block_ms": ["Block length", "Shorter is quicker but needs a faster GPU."],
    "stream_context_ms": ["Context", "Audio before each block the model also hears; more sounds better, costs time."],
    "stream_crossfade_ms": ["Crossfade", "Blend between blocks."],
    "legato": ["Connect words", "Sing each phrase joined up; a pause you leave is kept as a rest."],
    "expressive": ["Follow your loudness", "Loud and soft words, soft starts, fading breathy phrase ends."],
    "min_note_seconds": ["Shortest word", ""],
    "seconds_per_syllable": ["Time per syllable", "English banks only."],
    "note_gap_ms": ["Gap between words", "A few ms keeps words apart; 0 lets them run together."],
    "min_mora_seconds": ["Shortest mora", "Japanese banks. Below ~100 ms consonants swallow the vowel."],
    "max_mora_seconds": ["Longest mora", "Japanese banks. Caps how far a word spreads into a pause."],
    "pause_borrow": ["Sing into pauses", "0 keeps every pause, 1 uses them all up."],
    "keep_input_audio": ["Keep what I said", "Saves each phrase you speak in the out folder, next to what she sang."],
    "persistent_output": ["Keep the output open", "Skips opening the device for every phrase. Experimental."],
    "silence_ms": ["Silence ends a phrase after", ""],
    "min_chunk_ms": ["Shortest phrase", ""],
    "max_chunk_ms": ["Longest phrase", ""],
}


def _character(root: Path) -> dict[str, str]:
    """The bank's own character.txt - name, image, and the profile lines.

    UTAU banks are Shift-JIS by convention and this one carries more than a
    name: Teto's sheet says chimera, 31, fond of French bread. It is the
    voicebank's own description of itself, so the panel shows it rather than
    inventing copy.
    """
    path = root / "character.txt"
    if not path.exists():
        return {}
    text = ""
    for encoding in ("utf-8-sig", "shift_jis", "cp932", "utf-8"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except (UnicodeDecodeError, OSError):
            continue
    out: dict[str, str] = {}
    notes: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("-"):
            continue
        key, sep, value = line.partition("=")
        if sep and key.lower() in ("name", "image", "author", "web", "sample"):
            out[key.lower()] = value.strip()
        elif "：" in line or ":" in line:
            notes.append(line)
    if notes:
        out["profile"] = " · ".join(notes[:2])
    return out

#: Files beside the page that make it an installable app (see appwindow).
STATIC = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript; charset=utf-8",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}


def static_file(name: str) -> bytes:
    return (Path(__file__).resolve().parent / "web" / name).read_bytes()


def page() -> bytes:
    """The control panel page. Read from disk each time, so editing the HTML
    only needs a browser refresh; it is a few tens of kilobytes."""
    return (Path(__file__).resolve().parent / "web" / "index.html").read_bytes()


class _LogBuffer(logging.Handler):
    """Keeps the most recent lines so the page can show what is happening."""

    def __init__(self, capacity: int = 200):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = f"{record.levelname[:4]:<4} {record.getMessage()}"
            # log.exception writes a traceback, and dropping it here left the
            # panel showing "render failed" with no way to see why.
            if record.exc_info:
                import traceback

                last = traceback.format_exception(*record.exc_info)[-1].strip()
                line += f"\n     {last}"
            self.lines.append(line)
        except Exception:
            pass


class Controller:
    """Owns the relay so the page can start and stop it."""

    def __init__(self, cfg: Config, config_path: Path | None = None):
        self.cfg = cfg
        # The file the panel reads and saves. `--config` sets it; without it
        # the default config.json is used. The panel used to always read the
        # default file, so a relay started from it ignored --config.
        self.config_path = config_path
        self.relay = None
        self._lock = threading.Lock()
        # Settings read only at start apply by restarting the relay behind
        # the scenes (restart_soon): the panel shows "restarting" meanwhile.
        self.restarting = False
        self.restart_error = ""
        self._restart_timer: threading.Timer | None = None
        self.buffer = _LogBuffer()
        self.buffer.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.buffer)

    @property
    def running(self) -> bool:
        return self.relay is not None

    def start(self) -> None:
        with self._lock:
            if self.relay is not None:
                return
            from .app import TetoRelay

            self.cfg = Config.load(self.config_path)  # pick up anything just saved
            relay = TetoRelay(self.cfg)
            relay.start()
            self.relay = relay

    def stop(self) -> None:
        with self._lock:
            if self.relay is None:
                return
            try:
                self.relay.stop()
            finally:
                self.relay = None

    def restart_soon(self, delay: float = RESTART_DELAY) -> None:
        """Restart a running relay so settings it reads only at start apply.

        Waits `delay` after the last change, so dragging through a list of
        choices restarts once, not once per step.
        """
        with self._lock:
            if self._restart_timer is not None:
                self._restart_timer.cancel()
            self.restarting = True
            self.restart_error = ""
            timer = threading.Timer(delay, self._restart)
            timer.daemon = True
            self._restart_timer = timer
            timer.start()

    def _restart(self) -> None:
        try:
            if self.relay is None:
                return
            log.info("Restarting the relay to apply new settings")
            self.stop()
            self.start()
        except Exception as exc:  # noqa: BLE001 - shown in the panel, not raised
            log.exception("could not restart the relay")
            self.restart_error = describe(exc)
        finally:
            self.restarting = False

    def status(self) -> dict:
        relay = self.relay
        return {
            "version": __version__,
            "running": self.running,
            "restarting": self.restarting,
            "restart_error": self.restart_error,
            "last": getattr(relay, "last_text", "") if relay else "",
            "heard": getattr(relay, "last_source", "") if relay else "",
            "kana": getattr(relay, "last_kana", "") if relay else "",
            "notes": [
                {"lyric": lyric, "tone": tone}
                for lyric, tone in (getattr(relay, "last_notes", []) if relay else [])
            ],
            "stats": (getattr(relay, "last_stats", {}) if relay else {}) or {},
            "bank": relay.bank.key if relay and relay.bank else self.cfg.voicebank,
            "engine": (relay.engine if relay else (self.cfg.mode or "utau").lower()),
            "lyrics": (
                "morae" if relay and relay.engine != "voice" and relay._japanese_lyrics()
                else "words"
            ) if relay else "",
            "paused": bool(relay and relay.paused),
            "health": relay.health() if relay else {"microphone": "stopped", "problems": []},
            "log": list(self.buffer.lines)[-60:],
        }


_IMAGE_CACHE: dict[str, bytes] = {}
_ACCENT_CACHE: dict[str, str | None] = {}


def _bank_accent(cfg: Config, key: str) -> str | None:
    """The accent colour for a bank, read from its own icon and cached."""
    if key not in _ACCENT_CACHE:
        from .library import accent_colour

        png = _bank_image(cfg, key)
        _ACCENT_CACHE[key] = accent_colour(png) if png else None
    return _ACCENT_CACHE[key]


def _forget_library() -> None:
    """Drop the caches after something is installed."""
    _IMAGE_CACHE.clear()
    _ACCENT_CACHE.clear()


def _bank_image(cfg: Config, key: str) -> bytes | None:
    """The bank's own icon as a PNG.

    UTAU ships a 100x100 BMP, which no browser should be asked to lay out
    directly, so it is converted once and cached in memory.
    """
    if key in _IMAGE_CACHE:
        return _IMAGE_CACHE[key]
    from .voicebank import discover, select

    try:
        bank = select(discover(cfg.voicebank_path()), key)
        name = _character(bank.root).get("image") or "teto.bmp"
        path = bank.root / name
        if not path.exists():
            matches = list(bank.root.glob("*.bmp")) + list(bank.root.glob("*.png"))
            if not matches:
                return None
            path = matches[0]
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.open(path).convert("RGB").save(buffer, format="PNG")
        _IMAGE_CACHE[key] = buffer.getvalue()
        return _IMAGE_CACHE[key]
    except Exception:
        log.debug("could not load the icon for %r", key, exc_info=True)
        return None


def _meta(cfg: Config) -> dict:
    """Field groupings and the choices for enum-ish settings."""
    known = {k for keys in GROUPS.values() for k in keys}
    everything = [f.name for f in fields(cfg) if f.name not in HIDE]
    # Essentials are shown on the main screen, so they are not repeated in the
    # advanced groups - one control per setting.
    handled = known | set(ESSENTIALS) | SHOWN_ELSEWHERE
    groups = {
        name: [k for k in keys if k in everything and k not in ESSENTIALS]
        for name, keys in GROUPS.items()
    }
    # `voicebank` still travels in the config payload - the picker reads it -
    # it just has no row in the form.
    groups["Other"] = [k for k in everything if k not in handled]

    # Real device names for the two pickers. Windows lists the same physical
    # device once per host API, so they are deduplicated by name and the
    # best-ranked host API wins - the same ordering `find_device` uses to
    # resolve whatever name is saved.
    devices: dict[str, list[str]] = {"input": [], "output": []}
    try:
        from .devices import _rank, list_devices

        found = sorted(list_devices(), key=_rank)
        for kind in ("input", "output"):
            seen: list[str] = []
            for d in found:
                if (d.is_input if kind == "input" else d.is_output) and d.name not in seen:
                    seen.append(d.name)
            devices[kind] = seen
    except Exception:
        log.debug("could not list audio devices", exc_info=True)

    from .voicebank import discover

    details: list[dict] = []
    try:
        for b in discover(cfg.voicebank_path()):
            character = _character(b.root)
            details.append({
                "key": b.key,
                "name": character.get("name") or b.name,
                "flavour": b.flavour,
                "entries": b.entry_count,
                "profile": character.get("profile", ""),
                "web": character.get("web", ""),
                # An en- bank sings words; a ja- bank sings morae.
                "lyrics": "morae" if b.flavour.startswith("ja-") else "words",
                # Each voice tints the panel with the colour its own artwork is
                # built around, so a bank you add brings its own look.
                "accent": _bank_accent(cfg, b.key),
            })
        banks = [b["key"] for b in details]
    except Exception:
        banks = [cfg.voicebank]

    return {
        "groups": {name: keys for name, keys in groups.items() if keys},
        "group_info": GROUP_INFO,
        "essentials": [k for k in ESSENTIALS if k in everything],
        "devices": devices,
        "banks": details,
        "ranges": RANGES,
        "seconds": sorted(SECONDS),
        "labels": LABELS,
        "option_labels": {
            "singing_style": {"speech": "Speech", "sung": "Sung"},
            "double_when": {"off": "Off", "singing": "On"},
            "panel_window": {"app": "Its own window", "browser": "Browser tab"},
            "language": {"en": "English", "th": "ไทย", "ja": "日本語"},
        },
        "choices": {
            "mode": ["utau", "voice"],
            "capture_mode": ["ptt", "vad"],
            "lyric_mode": ["auto", "native", "japanese"],
            "renderer_backend": ["openutau", "null"],
            "shift_mode": ["semitone", "octave"],
            "singing_style": ["speech", "sung"],
            "double_when": ["off", "singing"],
            "panel_window": ["app", "browser"],
            "scale": ["major", "minor", "pentatonic", "chromatic"],
            "scale_key": ["auto", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"],
            "pitch_method": ["crepe", "pyin"],
            "crepe_model": ["full", "tiny"],
            "crepe_device": ["cuda", "cpu"],
            "align_device": ["cuda", "cpu"],
            "rvc_f0_method": ["rmvpe", "harvest", "crepe", "pm"],
            "rvc_device": ["cuda:0", "cpu"],
            # Multilingual only: the .en models cannot hear Thai or Japanese,
            # and the Language box is where the language is chosen.
            # English-only (.en) models are faster and more accurate for
            # English; the multilingual ones are needed for Thai or Japanese.
            "whisper_model": ["tiny.en", "base.en", "small.en", "medium.en",
                              "tiny", "base", "small", "medium", "large-v3"],
            "whisper_device": ["cpu", "cuda"],
            "whisper_compute_type": ["int8", "float16", "float32"],
            "language": ["en", "th", "ja"],
            "voicebank": banks,
        },
    }


def make_handler(controller: Controller):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, payload: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, obj, status: int = 200) -> None:
            self._send(json.dumps(obj).encode("utf-8"), "application/json", status)

        def _refuse_foreign(self, changes_state: bool) -> bool:
            """Answer 403 and return True if the request is not from the panel."""
            port = self.server.server_address[1]
            ok = host_allowed(self.headers.get("Host"), port) and origin_allowed(
                self.headers.get("Origin"), port
            )
            if ok and changes_state:
                ok = self.headers.get(TOKEN_HEADER) == "1"
            if not ok:
                log.warning(
                    "refused a %s %s from outside the control panel (Host=%r, Origin=%r)",
                    self.command, self.path.split("?")[0],
                    self.headers.get("Host"), self.headers.get("Origin"),
                )
                self._json({"error": "This request did not come from the Teto Relay panel."}, 403)
            return not ok

        def do_GET(self):
            if self._refuse_foreign(changes_state=False):
                return
            route = self.path.split("?")[0].strip("/")
            if route in ("", "index.html"):
                self._send(page(), "text/html; charset=utf-8")
            elif route in STATIC:
                self._send(static_file(route), STATIC[route])
            elif route == "api/config":
                cfg = Config.load(controller.config_path)
                data = {f.name: getattr(cfg, f.name) for f in fields(cfg) if f.name not in HIDE}
                self._json({"config": data, "meta": _meta(cfg)})
            elif route == "api/status":
                self._json(controller.status())
            elif route == "api/doctor":
                from .doctor import FAIL, format_checks, run_checks

                checks = run_checks(Config.load(controller.config_path))
                self._json({
                    "ok": not any(c.status == FAIL for c in checks),
                    "checks": [c.as_dict() for c in checks],
                    "text": format_checks(checks),
                })
            elif route == "api/bank-image":
                from urllib.parse import parse_qs, urlparse

                key = (parse_qs(urlparse(self.path).query).get("bank") or [""])[0]
                png = _bank_image(controller.cfg, key or controller.cfg.voicebank)
                if png is None:
                    self._json({"error": "no image"}, 404)
                else:
                    self._send(png, "image/png")
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self._refuse_foreign(changes_state=True):
                return
            route = self.path.split("?")[0].strip("/")
            if route == "api/start":
                try:
                    controller.start()
                    self._json({"ok": True})
                except TetoRelayError as exc:
                    log.error("Could not start: %s", exc)
                    self._json({"ok": False, "error": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001 - report it on the page
                    log.exception("could not start the relay")
                    self._json({"ok": False, "error": describe(exc, controller.cfg.log_file)}, 500)
            elif route == "api/stop":
                controller.stop()
                self._json({"ok": True})
            elif route == "api/quit":
                # The packaged app has no console to Ctrl+C, so the page is
                # where it is closed. serve() stops the relay on the way out.
                log.info("Quit requested from the control panel")
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif route in ("api/install/voicebank", "api/install/rvc"):
                # Raw body with the filename in the query: multipart parsing is
                # not worth pulling in for a one-field form we also write.
                from urllib.parse import parse_qs, unquote, urlparse

                from .library import MAX_UPLOAD, install_rvc_model, install_voicebank

                query = parse_qs(urlparse(self.path).query)
                name = unquote((query.get("name") or ["upload"])[0])
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > MAX_UPLOAD:
                    self._json({"ok": False, "error": "That file is empty or too large."}, 400)
                    return
                try:
                    body = self.rfile.read(length)
                    if route.endswith("voicebank"):
                        info = install_voicebank(body, name, controller.cfg.voicebank_path())
                        log.info("Installed voicebank %r (%d samples)", info["name"], info["samples"])
                    else:
                        from . import paths

                        folder = (
                            Path(controller.cfg.rvc_model).parent
                            if controller.cfg.rvc_model
                            else paths.data_dir() / "voices"
                        )
                        info = install_rvc_model(body, name, folder)
                        log.info("Installed RVC %s: %s", info["kind"], info["path"])
                        # A .pth is the voice; point the config at it. An index
                        # is an accessory and is only stored.
                        cfg = Config.load(controller.config_path)
                        if info["kind"] == "model":
                            cfg.rvc_model = info["path"]
                        else:
                            cfg.rvc_index = info["path"]
                        cfg.save(controller.config_path)
                        # Applied in place: the running relay holds
                        # controller.cfg, and rebinding it here cut every
                        # later setting change off from the relay.
                        controller.cfg.rvc_model = cfg.rvc_model
                        controller.cfg.rvc_index = cfg.rvc_index
                    _forget_library()
                    self._json({"ok": True, "installed": info})
                except ValueError as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    log.exception("install failed")
                    self._json({"ok": False, "error": str(exc)}, 500)
            elif route == "api/voicebank":
                # Saved like any setting, but also applied to a running relay -
                # a model picker that needed a restart would not be a picker.
                length = int(self.headers.get("Content-Length", 0))
                try:
                    key = (json.loads(self.rfile.read(length) or b"{}") or {}).get("bank")
                    cfg = Config.load(controller.config_path)
                    cfg.voicebank = key
                    cfg.save(controller.config_path)
                    controller.cfg.voicebank = key
                    applied = False
                    if controller.relay is not None:
                        controller.relay.set_voicebank(key)
                        applied = True
                    self._json({"ok": True, "applied": applied})
                except Exception as exc:  # noqa: BLE001
                    log.exception("could not switch the voicebank")
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/config":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    incoming = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(incoming, dict):
                        raise ConfigError("Settings must be sent as a JSON object.")
                    cfg = Config.load(controller.config_path)
                    valid = {f.name for f in fields(cfg)}
                    # Paths and internals are not the panel's to change: they
                    # are hidden from the form, so a request that sets them did
                    # not come from it.
                    updates = {
                        key: coerce(key, value)
                        for key, value in incoming.items()
                        if key in valid and key not in HIDE and value is not None
                    }
                    for key, value in updates.items():
                        setattr(cfg, key, value)
                    # Checked before anything is saved or applied, so a bad
                    # value is refused with a reason instead of crashing the
                    # relay on its next utterance.
                    cfg.validate("the settings you entered")
                    from .stt import effective_model

                    model_before = effective_model(controller.cfg)
                    changed = []
                    for key in updates:
                        value = getattr(cfg, key)
                        if getattr(controller.cfg, key, None) != value:
                            changed.append(key)
                        # The running relay holds controller.cfg itself and
                        # reads most settings per utterance, so applying in
                        # place takes effect now. Rebinding would not: the
                        # relay would keep the old object, which is why
                        # changing the language mid-run used to do nothing.
                        setattr(controller.cfg, key, value)
                    cfg.save(controller.config_path)
                    stale = set(changed) & LOADED_ONCE if controller.running else set()
                    if controller.running and effective_model(controller.cfg) != model_before:
                        stale.add("whisper_model")
                    # The relay restarts itself for these; only the OpenUtau
                    # folder needs the whole program restarted, because the
                    # .NET runtime is loaded once per process.
                    process = stale & NEEDS_PROGRAM_RESTART
                    if stale - process:
                        controller.restart_soon()
                    self._json({"ok": True, "restarting": bool(stale - process),
                                "restart": sorted(process)})
                except (ConfigError, json.JSONDecodeError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    log.exception("could not save settings")
                    self._json({"ok": False, "error": str(exc)}, 500)
            else:
                self._json({"error": "not found"}, 404)

    return Handler


class PanelServer(ThreadingHTTPServer):
    # The browser keeps connections open; with non-daemon handler threads,
    # server_close() waited on them and Ctrl+C could hang until the tab closed.
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind the same port and
    # share it silently, so a second launch was never detected. Ask for
    # exclusive use instead.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _panel_already_running(host: str, port: int) -> bool:
    """Whether a Teto Relay panel is what is holding the port."""
    import http.client

    try:
        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request("GET", "/api/status", headers={TOKEN_HEADER: "1"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status == 200 and b'"running"' in body
    except (OSError, http.client.HTTPException):
        return False


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          config_path: Path | None = None) -> int:
    """Run the control panel until interrupted."""
    from .appwindow import open_panel

    url = f"http://{host}:{port}/"
    as_app = (getattr(cfg, "panel_window", "app") or "app") == "app"
    try:
        server = PanelServer((host, port), None)
    except OSError as exc:
        # Double-clicking the app a second time lands here. If it is our own
        # panel on that port, just show it rather than failing.
        if _panel_already_running(host, port):
            print(f"Teto Relay is already running: {url}")
            if open_browser:
                open_panel(url, as_app)
            return 0
        raise TetoRelayError(
            f"The control panel could not use port {port} ({exc.strerror or exc}). "
            f"Another program is using it. Start with a different port, e.g. --port {port + 1}."
        ) from exc
    controller = Controller(cfg, config_path)
    server.RequestHandlerClass = make_handler(controller)
    print(f"Teto Relay control panel: {url}")
    log.info("control panel on %s", url)
    if open_browser:
        threading.Timer(0.5, open_panel, (url, as_app)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        controller.stop()
        server.server_close()
    return 0
