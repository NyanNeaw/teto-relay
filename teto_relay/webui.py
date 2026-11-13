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
import time
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
    "input_device", "output_device", "hear_myself", "ptt_key",
]

# "All settings", in order of how often they matter. Anything not listed still
# appears, under "Other", so new options are never silently hidden.
GROUPS: dict[str, list[str]] = {
    # Her tone, the way an OpenUtau tuner sets it, then effects after the render.
    "Voice": ["tone_gender", "tone_tension", "tone_breath", "tone_voicing"],
    "Note shape": ["note_scoop_cents", "note_fall_cents", "consonant_speed", "note_attack", "note_release"],
    "Effects": ["fx_drive", "fx_growl", "fx_growl_hz", "fx_octave", "fx_bright", "fx_ring", "fx_crush",
                "fx_band", "fx_air", "fx_chorus", "fx_reverb", "fx_size"],
    "Singing": [
        "sing_source", "legato", "expressive", "double_voice", "scale", "scale_key", "sung_melody_range",
        "sung_contour_amount",
        "melody_step", "melody_min_note_seconds",
        "vibrato_min_seconds", "vibrato_depth_cents", "vibrato_period_ms",
        "final_hold_seconds", "emit_contour",
    ],
    # Speed vs accuracy lives here: device and compute type are the two biggest
    # levers on how long whisper takes.
    "Listening": [
        "whisper_model", "thai_speech_model", "whisper_device", "whisper_compute_type", "beam_size",
        "initial_prompt", "align_morae", "align_boundaries", "use_alignment", "align_device", "no_speech_threshold",
    ],
    "Recording": ["capture_mode", "silence_ms", "min_chunk_ms", "max_chunk_ms"],
    # Where things are. Empty means "look in the usual places".
    "Setup": ["monitor_device", "openutau_dir", "voicebank_root", "market_url", "renderer_backend", "lyric_mode",
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

# Each group's icon, a line under its title, and whether it starts folded away.
GROUP_INFO: dict[str, dict] = {
    "Singing": {"icon": "note"},
    "Voice": {"icon": "mic", "note": "Her tone, as OpenUtau shapes it."},
    "Note shape": {"icon": "pitch", "note": "How each note starts and ends: scoops, fall-offs, a hard or soft attack."},
    "Effects": {"icon": "wave", "note": "After she sings: scream, robot, radio, hall."},
    "Listening": {"icon": "ear"},
    "Recording": {"icon": "wave"},
    "Setup": {"icon": "folder"},
    "Fine tuning: pitch": {"icon": "pitch", "note": "Change with care.", "collapsed": True},
    "Fine tuning: timing": {"icon": "clock", "note": "Change with care.", "collapsed": True},
    "Other": {"icon": "dots", "collapsed": True},
}

# Shown elsewhere on the page, so not in "Other" either: the voicebank has its
# picker, the mode is the Live | Studio switch in the top bar, and the
# recording delay is in Studio, beside the button that measures it.
SHOWN_ELSEWHERE = {"voicebank", "panel_mode", "studio_offset_ms"}

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
#: After the panel says it is closing, how long a page has to come back (a
#: reload does, within a second or two) before the program quits.
CLOSE_GRACE = 4.0

LOADED_ONCE = {
    # Read when push-to-talk is armed and when the relay is built.
    "ptt_key", "openutau_dir", "voicebank_root",
    "whisper_model", "whisper_device", "whisper_compute_type",
    "input_device", "output_device", "capture_mode",
    "renderer_backend",
    "pitch_method", "crepe_model", "crepe_device", "align_device",
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
    "sung_contour_amount": (0, 1, 0.05),
    "sung_melody_range": (0.5, 3, 0.1),
    "melody_step": (0.5, 3, 0.1),
    "melody_min_note_seconds": (0.06, 0.4, 0.01),
    "double_voice": (0, 1, 0.05),
    "stream_block_ms": (100, 1000, 10),
    "stream_context_ms": (0, 2000, 50),
    "stream_crossfade_ms": (0, 150, 5),
    "vibrato_min_seconds": (0.1, 1.5, 0.05),
    "vibrato_depth_cents": (0, 100, 5),
    "vibrato_period_ms": (80, 400, 5),
    "final_hold_seconds": (0, 1.5, 0.05),
    "tone_gender": (-100, 100, 5),
    "tone_tension": (-100, 100, 5),
    "tone_breath": (0, 100, 5),
    "tone_voicing": (0, 100, 5),
    "note_scoop_cents": (0, 400, 10),
    "note_fall_cents": (0, 1200, 25),
    "consonant_speed": (0, 200, 5),
    "note_attack": (0, 200, 5),
    "note_release": (0, 100, 5),
    "fx_octave": (-1, 1, 0.05),
    "fx_bright": (-1, 1, 0.05),
    "fx_drive": (0, 1, 0.05),
    "fx_growl": (0, 1, 0.05),
    "fx_growl_hz": (30, 120, 5),
    "fx_ring": (0, 1, 0.05),
    "fx_crush": (0, 1, 0.05),
    "fx_band": (0, 1, 0.05),
    "fx_air": (0, 1, 0.05),
    "fx_chorus": (0, 1, 0.05),
    "fx_reverb": (0, 1, 0.05),
    "fx_size": (0, 1, 0.05),
}

# Settings whose value is a duration in seconds, shown as ms on the slider
# readout because that is how they are discussed everywhere else.
SECONDS = {
    "vibrato_min_seconds", "final_hold_seconds", "melody_min_note_seconds",
    "min_note_seconds", "seconds_per_syllable", "min_mora_seconds", "max_mora_seconds",
}

# Names people recognise, and a line of help where the name is not enough.
# Anything missing falls back to the field name with its underscores removed.
LABELS: dict[str, list[str]] = {
    "openutau_dir": ["OpenUtau folder", "Empty finds it."],
    "voicebank_root": ["Voicebank folder", "Empty finds it."],
    "market_url": ["Market server", "Where people share tunings, voicebanks and sounds. Empty: the one this app came with, if any, else the built-in list."],
    "input_device": ["Microphone", ""],
    "output_device": ["Output", "VB-Cable sends her into other apps."],
    "hear_myself": ["Hear myself", "Hear her in your headphones too. Takes are only heard here."],
    "monitor_device": ["Hear myself on", "Blank is Windows' default output."],
    "ptt_key": ["Talk key", "Hold it while you talk."],
    "transpose": ["Transpose", "Semitones."],
    "playback_gain": ["Volume", ""],
    "lyric_mode": ["Lyrics", "Auto follows the voicebank."],
    "renderer_backend": ["Renderer", ""],
    "capture_mode": ["Recording", ""],
    "whisper_model": ["Speech model", "Bigger hears better, slower."],
    "thai_speech_model": ["Thai speech model", "Typhoon ASR for Thai. 0.5 GB download once."],
    "panel_window": ["Open the panel as", "Applies next time the app opens."],
    "whisper_device": ["Listen on", "cuda (NVIDIA) is much faster."],
    "whisper_compute_type": ["Listening precision", "int8 is fastest."],
    "language": ["Language", "The language you talk in."],
    "lyrics_hint": ["Song lyrics", "Paste the song's words so each one is heard right."],
    "initial_prompt": ["Vocabulary hint", "Words to expect."],
    "beam_size": ["Search width", "Higher is more accurate, slower."],
    "no_speech_threshold": ["Silence cutoff", "Higher ignores more noise."],
    "use_alignment": ["Measure word timing", "English only."],
    "align_boundaries": ["Measure where words meet", "English only."],
    "align_morae": ["Measure syllable timing", "Japanese only."],
    "align_device": ["Alignment on", ""],
    "pitch_method": ["Pitch tracker", ""],
    "crepe_model": ["Pitch model", ""],
    "crepe_device": ["Pitch on", ""],
    "target_tone": ["Target pitch", "0 uses the voicebank's own."],
    "shift_mode": ["Shift by", ""],
    "stable_shift": ["Hold pitch between phrases", ""],
    "shift_tolerance": ["Pitch drift allowed", ""],
    "max_shift": ["Largest shift", ""],
    "fix_octave_errors": ["Fix octave slips", ""],
    "f0_min": ["Lowest pitch tracked", ""],
    "f0_max": ["Highest pitch tracked", ""],
    "emit_contour": ["Follow your intonation", ""],
    "contour_smooth_ms": ["Intonation smoothing", ""],
    "contour_points": ["Intonation detail", ""],
    "contour_range_cents": ["Intonation range", ""],
    "singing_style": ["Style", "Speak follows your voice. Sing sings: when you sing she follows your notes, "
                               "in your key; when you talk she makes a tune of it."],
    "sing_source": ["Sing follows", "Auto: your notes when you sing, a made-up tune when you talk. "
                                    "My notes: always yours (for covers). Make a tune: always a tune."],
    "double_voice": ["Double her voice", "0 is off."],
    "double_when": ["Double voice", "A fuller, layered sound in Sing style."],
    "scale": ["Scale", "Sing and Cover. Chromatic leaves every note where it was."],
    "scale_key": ["Key", "Sing and Cover. Notes outside the key are pulled into it."],
    "sung_melody_range": ["Melody range", "Sing style only."],
    "sung_contour_amount": ["Keep your inflection", "Sing and Cover. 0 is dead straight notes."],
    "melody_step": ["New note when pitch moves", "Cover style only. Semitones. Lower follows every turn of your voice."],
    "melody_min_note_seconds": ["Shortest sung note", "Cover style only. Shorter ones join their neighbour."],
    "vibrato_min_seconds": ["Vibrato from", "Notes this long get vibrato."],
    "vibrato_depth_cents": ["Vibrato depth", "100 cents is a semitone."],
    "vibrato_period_ms": ["Vibrato speed", ""],
    "final_hold_seconds": ["Hold the last note", "Sing style only."],
    "tone_gender": ["Gender", "Up: a darker, bigger voice. Down: smaller, younger."],
    "tone_tension": ["Tension", "Up: pressed, belting, powerful. Down: soft and relaxed."],
    "tone_breath": ["Breath", "Air in the voice."],
    "tone_voicing": ["Voicing", "100 is a full voice; under 60 half whispered; 0 a whisper."],
    "note_scoop_cents": ["Scoop", "Slide up into each note from this many cents below: pop and R&B singers do it."],
    "note_fall_cents": ["Fall-off", "Drop the pitch off the end of each phrase, in cents: 1200 is a scream's octave fall."],
    "consonant_speed": ["Consonants", "How fast the consonants go: under 100 slower and clearer, over 100 snappier."],
    "note_attack": ["Attack", "How hard each note starts: 0 fades in softly, 200 hits hard."],
    "note_release": ["Release", "How much each note fades at its end: 0 holds, 100 dies away."],
    "fx_octave": ["Octave layer", "Up: an octave above (chipmunk). Down: an octave below (demon)."],
    "fx_bright": ["Brightness", "Up: crisp and forward. Down: dark and muffled."],
    "fx_drive": ["Drive", "Distortion. High for a scream."],
    "fx_growl": ["Growl", "A rough, fried rattle on the voice."],
    "fx_growl_hz": ["Growl speed", "Rattles a second: about 75 for a scream, 45 for a demon."],
    "fx_ring": ["Robot", "Metallic ring modulation."],
    "fx_crush": ["Bitcrush", "Fewer bits: an old game console."],
    "fx_band": ["Radio", "A telephone or an old radio."],
    "fx_air": ["Air", "Breath noise over the voice."],
    "fx_chorus": ["Chorus", "A few voices instead of one."],
    "fx_reverb": ["Reverb", "A room around her."],
    "fx_size": ["Room size", "From a booth to a hall."],
    "stream_block_ms": ["Block length", ""],
    "stream_context_ms": ["Context", ""],
    "stream_crossfade_ms": ["Crossfade", ""],
    "legato": ["Connect words", ""],
    "expressive": ["Follow your loudness", ""],
    "min_note_seconds": ["Shortest word", ""],
    "seconds_per_syllable": ["Time per syllable", "English banks only."],
    "note_gap_ms": ["Gap between words", ""],
    "min_mora_seconds": ["Shortest mora", "Japanese banks only."],
    "max_mora_seconds": ["Longest mora", "Japanese banks only."],
    "pause_borrow": ["Sing into pauses", ""],
    "keep_input_audio": ["Keep what I said", "Saved in the out folder."],
    "persistent_output": ["Keep the output open", "Experimental."],
    "silence_ms": ["Silence ends a phrase after", ""],
    "min_chunk_ms": ["Shortest phrase", ""],
    "max_chunk_ms": ["Longest phrase (ms)", "0 is no limit."],
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
    # The mini player the app window shows while it is minimized (window.py).
    "mini.html": "text/html; charset=utf-8",
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
        # Restart bookkeeping has its own lock: start() holds _lock through
        # the whole warm-up, and a settings save waiting on it hung the page.
        self._restart_state = threading.Lock()
        self._restart_wanted = False
        # One restart at a time; a change made during one is applied by
        # another straight after it (see _restart).
        self._restart_running = threading.Lock()
        # Closing the panel window quits the program (page_closing,
        # should_quit): the window used to be only a view, and closing it left
        # the program - relay, microphone and all - running unseen.
        self.last_poll = 0.0
        self.closing_at: float | None = None
        # Takes (teto_relay.takes): one being written out, and a counter the
        # panel watches to refresh its list.
        self.take_finishing = False
        self.takes_changed = 0
        # Said once when a take ends without being kept (nothing was sung).
        self.take_notice = ""
        # The take that was just saved, which the panel opens.
        self.last_take = ""
        # While the relay starts (10-30 s of model loading), what it is doing
        # and roughly how far along: the page showed "Start" again meanwhile,
        # because all it knew was "not running yet".
        self.starting = False
        self.start_stage = ""
        self.start_progress = 0.0
        self.stopping = False
        self.buffer = _LogBuffer()
        self.buffer.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.buffer)

    @property
    def running(self) -> bool:
        return self.relay is not None

    def _progress(self, stage: str, fraction: float) -> None:
        self.start_stage = stage
        self.start_progress = max(self.start_progress, min(1.0, fraction))

    def start(self) -> None:
        with self._lock:
            if self.relay is not None:
                return
            from .app import TetoRelay

            self.starting = True
            self.start_progress = 0.0
            self._progress("Getting ready", 0.03)
            try:
                self.cfg = Config.load(self.config_path)  # pick up anything just saved
                self._progress("Loading OpenUtau and the voicebank", 0.08)
                relay = TetoRelay(self.cfg)
                relay.progress = self._progress
                relay.start()
                self.relay = relay
            finally:
                self.starting = False
                self.start_stage = ""
                self.start_progress = 0.0

    def stop(self) -> None:
        # Said while it happens (status "stopping"): saving a take and closing
        # the models takes seconds, and the page used to keep showing "Stop".
        self.stopping = True
        try:
            self._stop()
        finally:
            self.stopping = False

    def _stop(self) -> None:
        with self._lock:
            if self.relay is None:
                return
            try:
                # A take being recorded is saved, not lost, when the relay
                # stops (Stop, or a restart for a setting).
                if getattr(self.relay, "_take", None) is not None:
                    try:
                        self._took(self.relay.stop_take(wait=10.0))
                        self.takes_changed += 1
                    except Exception:  # noqa: BLE001
                        log.exception("could not save the take")
                self.relay.stop()
            finally:
                self.relay = None

    # ---------------------------------------------------------------- takes
    @staticmethod
    def takes_root() -> Path:
        from . import paths

        return paths.data_dir() / "takes"

    @staticmethod
    def _for_project(take, project: str) -> None:
        """Mark the take just started as sung for `project` (not one already
        under way, started from somewhere else)."""
        if take is not None and not getattr(take, "phrases", None) and hasattr(take, "project"):
            take.project = project

    def _took(self, info: dict | None) -> None:
        """A take was recorded. It is kept as a recording; recorded with no
        song project open, a new project is started with it too (its lines
        where they were sung, and the music it was sung over), so a cover
        sung from scratch is a song straight away. With a project open, it
        waits in the Recordings to be dragged in."""
        discarded = bool(info and info.get("discarded"))
        self.take_notice = ("Nothing was sung while recording, so that take wasn't kept."
                            if discarded else "")
        self.last_recording = "" if discarded or not info else str(info.get("id", ""))
        self.last_take = self.last_recording
        if not self.last_recording or info.get("project"):
            return
        from . import music
        from . import takes as takes_mod

        root = self.takes_root()
        source = takes_mod.take_folder(root, self.last_recording)
        if source is None:
            return
        try:
            folder = takes_mod.new_folder(root)
            takes_mod.copy_lines(source, None, folder)
            if info.get("sung_over") == "next":
                music.copy_into(root / music.NEXT, folder)
            self.last_take = folder.name
        except Exception:  # noqa: BLE001 - the recording is kept either way
            log.exception("could not start a project from the recording")

    last_recording = ""

    def current_take(self) -> dict:
        """The take being recorded, line by line, for the panel's sheet."""
        relay = self.relay
        take = getattr(relay, "_take", None) if relay is not None else None
        if take is None:
            return {"recording": False, "lines": []}
        # music: whose music is playing under it ("next", or a take's id).
        return {"recording": True, "seconds": round(take.seconds, 1), "lines": take.lines(),
                "music": getattr(take, "music_id", "") or ""}

    def music_home(self, take_id: str) -> Path | None:
        """The folder holding the music of `take_id` ("next": the music
        waiting for the next take); None if there is no such take."""
        from . import music

        return music.home(self.takes_root(), take_id)

    def resing(self, folder, line_no: int, texts: list) -> dict:
        relay = self.relay
        if relay is None or not self.running:
            raise TetoRelayError("Press Start first: fixing a word sings the line again.")
        return relay.resing(folder, line_no, texts)

    def renote(self, folder, line_no: int, notes: list) -> dict:
        relay = self.relay
        if relay is None or not self.running:
            raise TetoRelayError("Press Start first: she sings the edited notes.")
        return relay.renote(folder, line_no, notes)

    def delete_line(self, folder, line_no: int) -> dict | None:
        """Drop a line from a take; needs no relay. None: the take was its
        only line, so the take went too."""
        from . import takes as takes_mod

        with takes_mod.edit_lock(folder):
            out = takes_mod.delete_line(folder, line_no)
        self.takes_changed += 1
        return out

    def move_line(self, folder, line_no: int, new_at, lane=None) -> dict:
        """Move a line along the take's timeline, or to another lane; needs
        no relay, because nothing is sung again."""
        from . import takes as takes_mod

        with takes_mod.edit_lock(folder):
            out = takes_mod.move_line(folder, line_no, new_at, lane)
        self.takes_changed += 1
        return out

    def audio_clip(self, action: str, folder, **kw) -> dict:
        """Add, move or delete an audio clip on a take's playlist; needs no
        relay."""
        from . import takes as takes_mod

        do = {"add": takes_mod.add_audio, "move": takes_mod.move_audio, "delete": takes_mod.delete_audio}[action]
        with takes_mod.edit_lock(folder):
            out = do(folder, **kw)
        self.takes_changed += 1
        return out

    def copy_line(self, source, line_no: int, target_id: str, at=None, lane=None) -> dict:
        """Copy a line from one take into another (the Studio library, dropped
        on the timeline); needs no relay. `target_id` "new" makes a new take
        of it, which keeps the music waiting on the music row, if any."""
        from . import music
        from . import takes as takes_mod

        root = self.takes_root()
        if target_id == "new":
            folder = takes_mod.new_folder(root)
            out = takes_mod.copy_line(source, line_no, folder, at, lane)
            if music.copy_into(root / music.NEXT, folder):
                out = takes_mod.detail(folder)
        else:
            folder = takes_mod.take_folder(root, target_id)
            if folder is None:
                raise ValueError("no such take")
            with takes_mod.edit_lock(folder):
                out = takes_mod.copy_line(source, line_no, folder, at, lane)
        self.takes_changed += 1
        return out

    def copy_lines(self, source, line_nos, target_id: str, start=None, lane=None) -> dict:
        """Copy a whole recording (or some of its lines) into a project,
        keeping their spacing; "new" starts a project of them, with the
        music waiting on the music row. Needs no relay."""
        from . import music
        from . import takes as takes_mod

        root = self.takes_root()
        if target_id == "new":
            folder = takes_mod.new_folder(root)
            out = takes_mod.copy_lines(source, line_nos, folder, start, lane)
            if music.copy_into(root / music.NEXT, folder):
                out = takes_mod.detail(folder)
        else:
            folder = takes_mod.take_folder(root, target_id)
            if folder is None:
                raise ValueError("no such take")
            with takes_mod.edit_lock(folder):
                out = takes_mod.copy_lines(source, line_nos, folder, start, lane)
        self.takes_changed += 1
        return out

    def rename(self, folder, name) -> dict:
        from . import takes as takes_mod

        with takes_mod.edit_lock(folder):
            out = takes_mod.rename(folder, name)
        self.takes_changed += 1
        return out

    def revoice(self, folder, lines, bank: str, tuning: str, as_copy: bool = False, name: str = "") -> dict:
        """Sing lines again with another voicebank and/or tuning (the
        relay's revoice). With as_copy the whole take is copied first and
        the copy is sung again, so the first stays as it was."""
        relay = self.relay
        if relay is None or not self.running:
            raise TetoRelayError("Press Start first: she sings it again with the new voice.")
        from . import takes as takes_mod

        if as_copy:
            folder = takes_mod.duplicate(folder, self.takes_root(), name)
        try:
            out = relay.revoice(folder, lines, bank, tuning)
        except Exception:
            if as_copy:
                takes_mod.discard(folder)
            raise
        finally:
            self.takes_changed += 1
        return out

    def preview(self, style: str = "") -> bytes:
        """Tune's play button: the last phrase sung again with the settings
        now, as .wav bytes for the panel to play. With `style` (a preset id:
        "builtin:robot", "user:mine", or "store:angel" for one in the Market)
        that tuning is heard instead, for this once; nothing changes."""
        import re

        from . import presets, store

        relay = self.relay
        if relay is None or not self.running:
            raise TetoRelayError("Press Start and sing a phrase first: the preview sings it again.")
        settings = None
        if style:
            kind, _, key = style.partition(":")
            if kind == "store":
                path = store.bundled_folder() / "presets" / f"{key}{presets.EXTENSION}"
                if not re.fullmatch(r"[a-z0-9-]{1,60}", key) or not path.is_file():
                    raise TetoRelayError("That tuning can't be heard before it is installed.")
                preset = presets.parse(path.read_bytes())
            else:
                preset = presets.get(style)
            settings = presets.settings_of(preset)
        return Path(relay.preview(settings)).read_bytes()

    def hush(self) -> bool:
        """PANIC: she goes quiet now. What is playing is cut and phrases
        waiting to play are dropped; the relay keeps running. False: nothing
        was running to hush."""
        relay = self.relay
        if relay is None:
            return False
        relay.hush()
        return True

    def shift_lines(self, folder, milliseconds) -> tuple[dict, float]:
        """Move all of a take's lines earlier or later; needs no relay."""
        from . import takes as takes_mod

        with takes_mod.edit_lock(folder):
            out = takes_mod.shift_lines(folder, milliseconds)
        self.takes_changed += 1
        return out

    def calibrate(self) -> dict:
        """Measure Studio's recording delay with clicks (teto_relay.delay)
        and save it as studio_offset_ms. About five seconds."""
        from . import delay

        relay = self.relay
        if relay is not None and getattr(relay, "_take", None) is not None:
            raise TetoRelayError("Stop recording first: the clicks would be recorded as a line.")
        found = delay.measure(Config.load(self.config_path))
        if abs(found["ms"]) > delay.LIMIT_MS:
            raise TetoRelayError(f"That came out as {found['ms']} ms, which cannot be right. Try again.")
        cfg = Config.load(self.config_path)
        cfg.studio_offset_ms = float(found["ms"])
        cfg.save(self.config_path)
        # The running relay reads this object when the next take starts.
        self.cfg.studio_offset_ms = float(found["ms"])
        return found

    # ------------------------------------------------------------ presets
    def apply_style(self, preset: dict) -> dict:
        """Make a preset's settings the ones in force (teto_relay.presets):
        saved, and used by a running relay from its next phrase. Returns the
        settings as they now are."""
        from .presets import settings_of

        settings = settings_of(preset)   # every setting: an old preset's missing ones as defaults
        cfg = Config.load(self.config_path)
        for key, value in settings.items():
            setattr(cfg, key, value)
        cfg.validate("this preset")
        for key in settings:
            # In place: the running relay holds this object (see api/config).
            setattr(self.cfg, key, getattr(cfg, key))
        cfg.save(self.config_path)
        return {key: getattr(cfg, key) for key in settings}

    def import_style(self, raw: bytes, apply: bool = True) -> dict:
        """Read a .tetostyle file, keep it, and (unless told not to) sing
        that way. A file that is not a usable preset raises before anything
        is kept or changed."""
        from . import presets

        preset = presets.parse(raw)
        settings = self.apply_style(preset) if apply else {}
        return {"id": presets.save(preset), "name": preset["name"], "config": settings}

    # -------------------------------------------------------------- store
    def store_listing(self, refresh: bool = False) -> dict:
        """The Store's list for the panel (teto_relay.store), each preset
        marked with what this PC already has of it: "builtin" (it ships with
        the app), "installed" (it is among your presets) or ""."""
        from . import market, presets

        listing = self._listing(refresh)
        mine = {s["name"].casefold(): s["id"] for s in presets.listing()["styles"] if not s["builtin"]}

        def public(entry: dict) -> dict:
            # Addresses stay here: the panel asks for things by id.
            out = {k: v for k, v in entry.items() if k not in ("url", "file", "demo", "shop_url", "user")}
            user = entry.get("user")
            out["user"] = {"handle": user["handle"], "name": user["name"], "avatar": bool(user.get("avatar"))} if user else None
            out["community"] = entry.get("origin") == "market"
            if entry.get("url"):
                from urllib.parse import urlparse

                out["host"] = urlparse(entry["url"]).hostname or ""
            file = entry.get("file")
            out["has_demo"] = bool(entry.get("demo"))
            out["size"] = int(file["size"]) if isinstance(file, dict) else 0
            return out

        shown = []
        for entry in listing["presets"]:
            if entry["id"] in presets.BUILTIN:
                state, have = "builtin", f"builtin:{entry['id']}"
            else:
                have = mine.get(entry["name"].casefold(), "")
                state = "installed" if have else ""
            shown.append(public(entry) | {"state": state, "have": have})
        banks = [public(b) | {"hosted": bool(b.get("file")), "page": bool(b.get("url"))} for b in listing["voicebanks"]]
        sounds = [public(s) | {"have": market.sound_path(s["id"]) is not None} for s in listing.get("sounds", [])]
        problem = listing.get("problem", "")
        if str(getattr(self.cfg, "market_url", "") or "").strip() and not market.base_url(self.cfg):
            problem = "The Market server address must start with https:// (or http://127.0.0.1 for one on this PC)."
        return {"source": listing["source"], "problem": problem,
                "updated": listing["updated"], "presets": shown, "voicebanks": banks, "sounds": sounds,
                "server": listing.get("server", ""), "shared": [s["id"] for s in market.shared()]}

    def _listing(self, refresh: bool = False) -> dict:
        from . import market, store

        return store.catalog(refresh, market.base_url(self.cfg))

    def store_install(self, entry_id: str) -> dict:
        """Install a preset from the Store: fetched by its id from the list
        that was checked, then imported like a dropped .tetostyle file, with
        the same checks. It joins your presets; it is not put on."""
        from . import store

        found = self.import_style(store.preset_file(self._listing(), entry_id), apply=False)
        log.info("Preset installed from the Store: %s", found["name"])
        return found

    def store_open(self, entry_id: str) -> str:
        """Open a voicebank's official download page in the browser."""
        import webbrowser

        from . import store

        url = store.official_page(self._listing(), entry_id)
        webbrowser.open(url)
        return url

    def store_demo(self, kind: str, entry_id: str) -> bytes:
        """A Market listing's demo sound, to hear before it is downloaded."""
        from . import market

        kinds = {"preset": "presets", "voicebank": "voicebanks", "sound": "sounds"}
        if kind not in kinds:
            raise market.MarketError("No such kind of listing.")
        listing = self._listing()
        if listing.get("source") != "market":
            raise market.MarketError("Demos come from the Market server.")
        if kind == "sound":
            have = market.sound_path(entry_id)
            if have is not None and not market.entry(listing, "sounds", entry_id).get("demo"):
                return have.read_bytes()
            if not market.entry(listing, "sounds", entry_id).get("demo"):
                return market.get_sound(listing, entry_id).read_bytes()
        return market.download(listing, kinds[kind], entry_id, "demo")[0]

    def store_get_bank(self, entry_id: str) -> dict:
        """Download a voicebank its creator shared in the Market and install
        it the usual way (teto_relay.library), with the usual checks."""
        from . import market
        from .library import install_voicebank

        data, meta = market.download(self._listing(), "voicebanks", entry_id)
        info = install_voicebank(data, meta.get("name") or f"{entry_id}.zip", self.cfg.voicebank_path())
        _forget_library()
        log.info("Installed voicebank %r from the Market (%d samples)", info["name"], info["samples"])
        return info

    def store_sound(self, entry_id: str) -> Path:
        """A Market sound's file on this PC (downloaded the first time)."""
        from . import market

        return market.get_sound(self._listing(), entry_id)

    def store_shop(self, kind: str, entry_id: str) -> str:
        """Open a paid listing's shop page (where it is sold) in the browser."""
        import webbrowser

        from . import market

        kinds = {"preset": "presets", "voicebank": "voicebanks", "sound": "sounds"}
        if kind not in kinds:
            raise market.MarketError("No such kind of listing.")
        url = market.shop_page(self._listing(), kinds[kind], entry_id)
        webbrowser.open(url)
        return url

    def market_server(self) -> str:
        from . import market

        server = market.base_url(self.cfg)
        if not server:
            raise market.MarketError("Set the Market server first (Settings, Setup).")
        return server

    def talk(self, down: bool) -> bool:
        """The mini player's hold-to-talk button: what holding the talk key does."""
        relay = self.relay
        chunker = getattr(getattr(relay, "_capture", None), "chunker", None) if relay else None
        if not self.running or not hasattr(chunker, "start"):
            return False
        (chunker.start if down else chunker.stop)()
        return True

    def start_take(self, music_id: str = "", project: str = "") -> None:
        """Start a take; with `music_id` ("next" or a take's id), over that
        music, played where only you hear it. It is kept as a recording;
        `project` is the song project open while it is sung, if any (see
        _took)."""
        relay = self.relay
        if relay is None or not self.running:
            raise TetoRelayError("Press Start first: a take records what Teto sings.")
        from . import takes as takes_mod

        if project and takes_mod.take_folder(self.takes_root(), project) is None:
            project = ""
        if not music_id:
            self._for_project(relay.start_take(), project)
            return
        from . import music

        folder = self.music_home(music_id)
        if folder is None or music.music_file(folder) is None:
            raise TetoRelayError("That music is gone. Add it again, or record without it.")
        try:
            self._for_project(relay.start_take(folder, music_id), project)
        except TetoRelayError:
            raise
        except Exception as exc:  # noqa: BLE001 - a device that will not open: say so, record nothing
            log.exception("could not play the music for the take")
            raise TetoRelayError(f"The music could not be played ({describe(exc)}). "
                                 "Check the Hear myself device, or record without music.") from exc

    def stop_take(self) -> None:
        """Finish the take in the background; the panel watches take_finishing."""
        relay = self.relay
        if relay is None or getattr(relay, "_take", None) is None or self.take_finishing:
            return
        self.take_finishing = True

        def finish() -> None:
            try:
                self._took(relay.stop_take())
            except Exception:  # noqa: BLE001
                log.exception("could not save the take")
            finally:
                self.take_finishing = False
                self.takes_changed += 1

        threading.Thread(target=finish, name="take", daemon=True).start()

    @property
    def busy(self) -> bool:
        """Running, or on the way back up: settings changes need a restart."""
        return self.running or self.restarting

    def restart_soon(self, delay: float | None = None) -> None:
        """Restart a running relay so settings it reads only at start apply.

        Waits `delay` after the last change, so dragging through a list of
        choices restarts once, not once per step. Asked for while a restart
        is already under way, it runs again once that one is up: the relay
        reads most start-only settings early in its start, so a change made
        later in it was saved, shown as applied, and not used.
        """
        with self._restart_state:
            if self._restart_timer is not None:
                self._restart_timer.cancel()
            self._restart_wanted = True
            self.restarting = True
            self.restart_error = ""
            timer = threading.Timer(RESTART_DELAY if delay is None else delay, self._restart)
            timer.daemon = True
            self._restart_timer = timer
            timer.start()

    def _restart(self) -> None:
        with self._restart_running:
            try:
                while True:
                    with self._restart_state:
                        if not self._restart_wanted:
                            return
                        self._restart_wanted = False
                    log.info("Restarting the relay to apply new settings")
                    self.stop()
                    self.start()
            except Exception as exc:  # noqa: BLE001 - shown in the panel, not raised
                log.exception("could not restart the relay")
                self.restart_error = describe(exc)
            finally:
                with self._restart_state:
                    if not self._restart_wanted:
                        self.restarting = False

    def page_closing(self) -> None:
        self.closing_at = time.monotonic()

    def should_quit(self) -> bool:
        """The panel was closed and no page has asked for status since."""
        closing = self.closing_at
        return (closing is not None and self.last_poll < closing
                and time.monotonic() - closing > CLOSE_GRACE)

    def status(self) -> dict:
        self.last_poll = time.monotonic()
        relay = self.relay
        return {
            "version": __version__,
            "running": self.running,
            "restarting": self.restarting,
            "starting": self.starting,
            "stopping": bool(getattr(self, "stopping", False)),
            "stage": self.start_stage,
            "progress": round(self.start_progress, 2),
            "restart_error": self.restart_error,
            "last": getattr(relay, "last_text", "") if relay else "",
            "heard": getattr(relay, "last_source", "") if relay else "",
            "kana": getattr(relay, "last_kana", "") if relay else "",
            "notes": [
                {"lyric": lyric, "tone": tone}
                for lyric, tone in (getattr(relay, "last_notes", []) if relay else [])
            ],
            "history": list(getattr(relay, "history", ())) if relay else [],
            "stats": (getattr(relay, "last_stats", {}) if relay else {}) or {},
            "bank": relay.bank.key if relay and relay.bank else self.cfg.voicebank,
            "lyrics": ("morae" if relay._japanese_lyrics() else "words") if relay else "",
            "paused": bool(relay and relay.paused),
            # The talk key (or the mini player's button) is held right now.
            "talking": bool(getattr(getattr(getattr(relay, "_capture", None), "chunker", None),
                                    "recording", False)) if relay else False,
            "health": relay.health() if relay else {"microphone": "stopped", "problems": []},
            "take": relay.take_status() if relay else {"recording": False},
            "take_finishing": self.take_finishing,
            "takes_changed": self.takes_changed,
            "take_notice": self.take_notice,
            "last_take": self.last_take,
            "last_recording": self.last_recording,
            # For the mini player's headphones button.
            "hear_myself": bool(getattr(self.cfg, "hear_myself", False)),
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


#: The longest voicebank sample the Market plays, in seconds.
SAMPLE_SECONDS = 8.0


def _sample_file(root: Path) -> Path | None:
    """The bank's own sample voice: the file its character.txt names as
    `sample=`, else a sung vowel from the bank (あ, a, or the first
    recording), so every installed voice can be heard in the Market."""
    named = _character(root).get("sample")
    if named:
        path = (root / named).resolve()
        if path.is_file() and path.is_relative_to(root.resolve()):
            return path
    for pattern in ("_あ*.wav", "あ*.wav", "*/_あ*.wav", "*/あ*.wav", "a.wav", "_a.wav", "*/a.wav", "*.wav", "*/*.wav"):
        found = sorted(p for p in root.glob(pattern) if p.is_file())
        if found:
            return found[0]
    return None


def _bank_sample(cfg: Config, key: str) -> bytes | None:
    """The bank's sample as a short 16-bit .wav, or None."""
    from io import BytesIO

    import soundfile as sf

    from .voicebank import discover, select

    try:
        bank = select(discover(cfg.voicebank_path()), key)
        path = _sample_file(bank.root)
        if path is None:
            return None
        with sf.SoundFile(str(path)) as source:
            audio = source.read(frames=int(source.samplerate * SAMPLE_SECONDS), dtype="float32")
            rate = source.samplerate
        buffer = BytesIO()
        sf.write(buffer, audio, rate, format="WAV", subtype="PCM_16")
        return buffer.getvalue()
    except Exception:
        log.debug("could not read a sample for %r", key, exc_info=True)
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

    from .voicebank import _name_key, discover, load_names

    names = load_names()
    details: list[dict] = []
    try:
        for b in discover(cfg.voicebank_path()):
            character = _character(b.root)
            custom = names.get(_name_key(b.root))
            details.append({
                "key": b.key,
                # The user's own name for it, else the bank's.
                "name": custom or character.get("name") or b.name,
                "custom_name": custom or "",
                "own_name": character.get("name") or b.name,
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
            "singing_style": {"speech": "Speak", "sung": "Sing", "cover": "Sing"},
            "sing_source": {"auto": "Auto", "tune": "Make a tune", "notes": "My notes"},
            "double_when": {"off": "Off", "singing": "On", "always": "Always"},
            "panel_window": {"app": "Its own window", "browser": "Browser tab"},
            "language": {"en": "English", "th": "ไทย", "ja": "日本語"},
            "capture_mode": {"ptt": "Hold the talk key", "vad": "Split on silence"},
            "renderer_backend": {"openutau": "OpenUtau", "null": "Test tone"},
            "lyric_mode": {"auto": "Auto", "native": "Words", "japanese": "Japanese syllables"},
        },
        "choices": {
            "capture_mode": ["ptt", "vad"],
            "lyric_mode": ["auto", "native", "japanese"],
            "renderer_backend": ["openutau", "null"],
            "shift_mode": ["semitone", "octave"],
            # Sing and Cover are one Sing now; how it follows you is sing_source.
            "singing_style": ["speech", "sung"],
            "sing_source": ["auto", "tune", "notes"],
            "double_when": ["off", "singing", "always"],
            "panel_window": ["app", "browser"],
            "scale": ["major", "minor", "pentatonic", "chromatic"],
            "scale_key": ["auto", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"],
            "pitch_method": ["crepe", "pyin"],
            "crepe_model": ["full", "tiny"],
            "crepe_device": ["cuda", "cpu"],
            "align_device": ["cuda", "cpu"],
            # Multilingual only: the .en models cannot hear Thai or Japanese,
            # and the Language box is where the language is chosen.
            # English-only (.en) models are faster and more accurate for
            # English; the multilingual ones are needed for Thai or Japanese.
            "whisper_model": ["tiny.en", "base.en", "small.en", "medium.en",
                              "tiny", "base", "small", "medium", "large-v3"],
            "whisper_device": ["auto", "cuda", "cpu"],
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

        def _download(self, payload: bytes, filename: str, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _take_query(self):
            from urllib.parse import parse_qs, urlparse

            from .takes import take_folder

            query = parse_qs(urlparse(self.path).query)
            take_id = (query.get("take") or [""])[0]
            return take_folder(controller.takes_root(), take_id), (query.get("file") or [""])[0]

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
            elif route == "api/takes":
                from .takes import PROJECT, RECORDING, listing

                everything = listing(controller.takes_root())
                self._json({"takes": everything,
                            "projects": [t for t in everything if t["kind"] == PROJECT],
                            "recordings": [t for t in everything if t["kind"] == RECORDING]})
            elif route == "api/take":
                from .takes import detail

                folder, _ = self._take_query()
                if folder is None:
                    self._json({"error": "no such take"}, 404)
                else:
                    self._json(detail(folder))
            elif route == "api/take/current":
                self._json(controller.current_take())
            elif route == "api/store":
                from urllib.parse import parse_qs, urlparse

                refresh = (parse_qs(urlparse(self.path).query).get("refresh") or ["0"])[0] == "1"
                try:
                    self._json(controller.store_listing(refresh))
                except Exception as exc:  # noqa: BLE001 - even the bundled list failed
                    log.exception("could not show the Store")
                    self._json({"error": str(exc)}, 500)
            elif route in ("api/market/account", "api/market/mine", "api/market/person", "api/market/avatar"):
                from urllib.parse import parse_qs, urlparse

                from . import market

                handle = (parse_qs(urlparse(self.path).query).get("handle") or [""])[0]
                try:
                    server = controller.market_server()
                    if route == "api/market/account":
                        self._json({"ok": True, "server": server, **market.account(server)})
                    elif route == "api/market/mine":
                        self._json({"ok": True, "items": market.mine(server)})
                    elif route == "api/market/person":
                        self._json({"ok": True, **market.person(server, handle)})
                    else:
                        data = market.avatar(server, handle)
                        kind = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/webp"
                        self._send(data, kind)
                except (TetoRelayError, ValueError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400 if route != "api/market/avatar" else 404)
            elif route in ("api/store/demo", "api/store/sound"):
                # A listing's demo, or a Market sound's file (fetched once and
                # kept in the data folder's sounds folder), by id.
                from urllib.parse import parse_qs, urlparse

                query = parse_qs(urlparse(self.path).query)
                entry_id = (query.get("id") or [""])[0]
                try:
                    if route == "api/store/demo":
                        data = controller.store_demo((query.get("kind") or [""])[0], entry_id)
                        name = "demo"
                    else:
                        path = controller.store_sound(entry_id)
                        data, name = path.read_bytes(), path.name
                    kind = {b"RIFF": "audio/wav", b"OggS": "audio/ogg", b"fLaC": "audio/flac"}.get(data[:4], "audio/mpeg")
                    self.send_response(200)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("X-File-Name", name)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                except (TetoRelayError, ValueError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/styles":
                from . import presets

                self._json(presets.listing(Config.load(controller.config_path)))
            elif route == "api/style/export":
                from urllib.parse import parse_qs, urlparse

                from . import presets

                wanted = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
                try:
                    preset = presets.get(wanted)
                    self._download(presets.dumps(preset).encode("utf-8"), presets.file_name(preset),
                                   "application/json")
                except presets.PresetError as exc:
                    self._json({"error": str(exc)}, 404)
            elif route in ("api/take/music", "api/take/music/sound"):
                # The music row: what is on it and its waveform, or (sound)
                # the music itself, decoded here so the panel's player hears
                # exactly what the recording was sung over.
                from urllib.parse import parse_qs, urlparse

                from . import music

                folder = controller.music_home((parse_qs(urlparse(self.path).query).get("take") or [""])[0])
                try:
                    found = music.info(folder) if folder is not None else None
                    if folder is None:
                        self._json({"error": "no such take"}, 404)
                    elif route == "api/take/music":
                        if found is not None:
                            found["peaks"] = music.peaks(folder)
                        self._json({"music": found})
                    elif found is None:
                        self._json({"error": "no music"}, 404)
                    else:
                        self._send(music.wav_bytes(folder), "audio/wav")
                except Exception as exc:  # noqa: BLE001 - a file that stopped being readable
                    log.exception("could not read the music")
                    self._json({"error": str(exc)}, 500)
            elif route == "api/take/file":
                from .takes import take_file, zip_take

                folder, name = self._take_query()
                path = take_file(folder, name) if folder is not None else None
                if folder is None:
                    self._json({"error": "no such take"}, 404)
                elif name == "zip":
                    self._download(zip_take(folder), f"{folder.name}.zip", "application/zip")
                elif name == "mix.wav":
                    # Her singing over the take's music, made now, not kept.
                    from . import music
                    from .takes import edit_lock

                    try:
                        with edit_lock(folder):
                            mixed = music.mix_wav(folder)
                        self._download(mixed, f"{folder.name}-mix.wav", "audio/wav")
                    except (ValueError, OSError) as exc:
                        self._json({"error": str(exc)}, 404)
                elif path is not None:
                    kind = "audio/wav" if name.endswith(".wav") else "application/octet-stream"
                    self._download(path.read_bytes(), f"{folder.name}-{name.replace('/', '-')}", kind)
                else:
                    self._json({"error": "no such file"}, 404)
            elif route == "api/bank-image":
                from urllib.parse import parse_qs, urlparse

                key = (parse_qs(urlparse(self.path).query).get("bank") or [""])[0]
                png = _bank_image(controller.cfg, key or controller.cfg.voicebank)
                if png is None:
                    self._json({"error": "no image"}, 404)
                else:
                    self._send(png, "image/png")
            elif route == "api/bank-sample":
                from urllib.parse import parse_qs, urlparse

                key = (parse_qs(urlparse(self.path).query).get("bank") or [""])[0]
                wav = _bank_sample(controller.cfg, key or controller.cfg.voicebank)
                if wav is None:
                    self._json({"error": "This voicebank has no sample to play."}, 404)
                else:
                    self._send(wav, "audio/wav")
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            route = self.path.split("?")[0].strip("/")
            if route == "api/closing":
                # Sent by the page as it goes away (navigator.sendBeacon),
                # which cannot add the token header; the origin check still
                # applies, and all it does is start the grace period.
                if not self._refuse_foreign(changes_state=False):
                    controller.page_closing()
                    self._json({"ok": True})
                return
            if self._refuse_foreign(changes_state=True):
                return
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
            elif route == "api/talk":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    down = bool((json.loads(self.rfile.read(length) or b"{}") or {}).get("down"))
                except ValueError:
                    down = False
                self._json({"ok": controller.talk(down)})
            elif route == "api/take/start":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                    music_id = body.get("music") if isinstance(body, dict) else None
                    project = body.get("project") if isinstance(body, dict) else None
                    controller.start_take(music_id if isinstance(music_id, str) else "",
                                          project if isinstance(project, str) else "")
                    self._json({"ok": True})
                except (TetoRelayError, ValueError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/take/music":
                # The raw file, its name in the query, as voicebanks arrive.
                from urllib.parse import parse_qs, unquote, urlparse

                from . import music

                query = parse_qs(urlparse(self.path).query)
                folder = controller.music_home((query.get("take") or [""])[0])
                name = unquote((query.get("name") or ["music"])[0])
                length = int(self.headers.get("Content-Length", 0))
                if folder is None:
                    self._json({"ok": False, "error": "no such take"}, 404)
                elif length <= 0 or length > music.MAX_UPLOAD:
                    self.close_connection = True  # its body is not read
                    self._json({"ok": False, "error": "That file is empty or too large."}, 400)
                else:
                    try:
                        found = music.store(self.rfile.read(length), name, folder)
                        found["peaks"] = music.peaks(folder)
                        controller.takes_changed += 1
                        self._json({"ok": True, "music": found})
                    except ValueError as exc:
                        self._json({"ok": False, "error": str(exc)}, 400)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("could not add the music")
                        self._json({"ok": False, "error": str(exc)}, 500)
            elif route == "api/take/music/delete":
                from urllib.parse import parse_qs, urlparse

                from . import music

                folder = controller.music_home((parse_qs(urlparse(self.path).query).get("take") or [""])[0])
                if folder is None:
                    self._json({"ok": False, "error": "no such take"}, 404)
                else:
                    try:
                        music.remove(folder)
                        controller.takes_changed += 1
                        self._json({"ok": True})
                    except OSError as exc:
                        self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/take/stop":
                controller.stop_take()
                self._json({"ok": True})
            elif route == "api/take/line":
                folder, _ = self._take_query()
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                    texts = body.get("words")
                    if folder is None:
                        self._json({"ok": False, "error": "no such take"}, 404)
                    elif not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                        self._json({"ok": False, "error": "words must be a list of strings"}, 400)
                    else:
                        self._json({"ok": True, "take": controller.resing(folder, int(body.get("line", 0)), texts)})
                except (TetoRelayError, ValueError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001 - a render that failed: say so
                    log.exception("could not sing the line again")
                    self._json({"ok": False, "error": f"Could not sing it again: {exc}"}, 500)
            elif route == "api/take/notes":
                folder, _ = self._take_query()
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                    notes = body.get("notes")
                    if folder is None:
                        self._json({"ok": False, "error": "no such take"}, 404)
                    elif not isinstance(notes, list) or not all(isinstance(n, dict) for n in notes):
                        self._json({"ok": False, "error": "notes must be a list of notes"}, 400)
                    else:
                        self._json({"ok": True, "take": controller.renote(folder, int(body.get("line", 0)), notes)})
                except (TetoRelayError, ValueError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001 - a render that failed: say so
                    log.exception("could not sing the edited notes")
                    self._json({"ok": False, "error": f"Could not sing it: {exc}"}, 500)
            elif route == "api/take/line/delete":
                folder, _ = self._take_query()
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                    if folder is None:
                        self._json({"ok": False, "error": "no such take"}, 404)
                    else:
                        take = controller.delete_line(folder, int(body.get("line") or 0))
                        self._json({"ok": True, "take": take, "gone": take is None})
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/take/line/move":
                folder, _ = self._take_query()
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                    if folder is None:
                        self._json({"ok": False, "error": "no such take"}, 404)
                    else:
                        take = controller.move_line(folder, int(body.get("line") or 0), body.get("at"), body.get("lane"))
                        self._json({"ok": True, "take": take})
                except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/take/line/copy":
                # ?take= is where it goes: a take's id, or "new".
                from urllib.parse import parse_qs, urlparse

                from .takes import take_folder

                target = (parse_qs(urlparse(self.path).query).get("take") or [""])[0]
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                    source = take_folder(controller.takes_root(), str(body.get("from") or ""))
                    if source is None or (target != "new" and take_folder(controller.takes_root(), target) is None):
                        self._json({"ok": False, "error": "no such take"}, 404)
                    else:
                        if "lines" in body:
                            # A whole recording (or some of its lines), keeping their spacing:
                            # "start" is where the first one's singing begins; none, where sung.
                            wanted = body.get("lines")
                            if wanted is not None and wanted != "all":
                                if not isinstance(wanted, list) or not all(
                                        isinstance(n, int) and not isinstance(n, bool) for n in wanted):
                                    raise ValueError("lines is a list of line numbers.")
                            take = controller.copy_lines(source, None if wanted in (None, "all") else wanted,
                                                         target, body.get("start"), body.get("lane"))
                        else:
                            take = controller.copy_line(source, int(body.get("line") or 0), target, body.get("at"),
                                                        body.get("lane"))
                        self._json({"ok": True, "take": take})
                except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route in ("api/take/revoice", "api/take/rename"):
                folder, _ = self._take_query()
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                    if not isinstance(body, dict):
                        raise ValueError("Send an object.")
                    if folder is None:
                        self._json({"ok": False, "error": "no such take"}, 404)
                    elif route == "api/take/rename":
                        self._json({"ok": True, "take": controller.rename(folder, body.get("name"))})
                    else:
                        lines = body.get("lines")
                        if lines is not None and (not isinstance(lines, list) or not all(
                                isinstance(n, int) and not isinstance(n, bool) for n in lines)):
                            raise ValueError("lines is a list of line numbers.")
                        bank, tuning, name = body.get("bank") or "", body.get("tuning") or "", body.get("name") or ""
                        if not all(isinstance(v, str) for v in (bank, tuning, name)):
                            raise ValueError("bank, tuning and name are text.")
                        take = controller.revoice(folder, lines, bank, tuning, bool(body.get("copy")), name)
                        self._json({"ok": True, "take": take})
                except (TetoRelayError, ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/take/audio":
                # An audio file laid on the playlist: the raw file, its name,
                # time and lane in the query (as the music arrives).
                from urllib.parse import parse_qs, unquote, urlparse

                from . import music

                folder, _ = self._take_query()
                query = parse_qs(urlparse(self.path).query)
                length = int(self.headers.get("Content-Length", 0))
                if folder is None:
                    self.close_connection = True
                    self._json({"ok": False, "error": "no such take"}, 404)
                elif length <= 0 or length > music.MAX_UPLOAD:
                    self.close_connection = True  # its body is not read
                    self._json({"ok": False, "error": "That file is empty or too large."}, 400)
                else:
                    try:
                        lane = float((query.get("lane") or ["1"])[0])
                        take = controller.audio_clip("add", folder, data=self.rfile.read(length),
                                                     filename=unquote((query.get("name") or ["audio"])[0]),
                                                     at=(query.get("at") or ["0"])[0], lane=lane)
                        self._json({"ok": True, "take": take})
                    except ValueError as exc:
                        self._json({"ok": False, "error": str(exc)}, 400)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("could not add the audio")
                        self._json({"ok": False, "error": str(exc)}, 500)
            elif route in ("api/take/audio/move", "api/take/audio/delete"):
                folder, _ = self._take_query()
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                    if folder is None:
                        self._json({"ok": False, "error": "no such take"}, 404)
                    elif route == "api/take/audio/move":
                        take = controller.audio_clip("move", folder, clip_id=body.get("id"), at=body.get("at"),
                                                     lane=body.get("lane"))
                        self._json({"ok": True, "take": take})
                    else:
                        self._json({"ok": True, "take": controller.audio_clip("delete", folder, clip_id=body.get("id"))})
                except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/preview":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                    style = str(body.get("style") or "") if isinstance(body, dict) else ""
                    self._send(controller.preview(style), "audio/wav")
                except ValueError as exc:   # a broken preset (PresetError) or body
                    self._json({"ok": False, "error": str(exc)}, 400)
                except TetoRelayError as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001 - a render that failed: say so
                    log.exception("could not sing the preview")
                    self._json({"ok": False, "error": f"Could not sing it: {exc}"}, 500)
            elif route == "api/panic":
                self._json({"ok": True, "hushed": controller.hush()})
            elif route == "api/take/shift":
                folder, _ = self._take_query()
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                    if folder is None:
                        self._json({"ok": False, "error": "no such take"}, 404)
                    else:
                        take, moved = controller.shift_lines(folder, body.get("ms"))
                        self._json({"ok": True, "take": take, "moved": moved})
                except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/studio/calibrate":
                try:
                    self._json({"ok": True, **controller.calibrate()})
                except TetoRelayError as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001 - say it on the page
                    log.exception("could not measure the delay")
                    self._json({"ok": False, "error": describe(exc, controller.cfg.log_file)}, 500)
            elif route in ("api/store/install", "api/store/open"):
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                    entry_id = str(body.get("id") or "") if isinstance(body, dict) else ""
                    if route == "api/store/install":
                        self._json({"ok": True, **controller.store_install(entry_id)})
                    else:
                        self._json({"ok": True, "url": controller.store_open(entry_id)})
                except (TetoRelayError, ValueError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route in ("api/store/get-bank", "api/store/get-sound", "api/store/like", "api/store/report",
                           "api/store/takedown"):
                from . import market

                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") if 0 < length < 20_000 else {}
                    body = body if isinstance(body, dict) else {}
                    entry_id = str(body.get("id") or "")
                    if route == "api/store/get-bank":
                        self._json({"ok": True, "installed": controller.store_get_bank(entry_id)})
                    elif route == "api/store/get-sound":
                        self._json({"ok": True, "file": controller.store_sound(entry_id).name})
                    elif route == "api/store/like":
                        self._json({"ok": True, "likes": market.like(controller.market_server(), entry_id,
                                                                      bool(body.get("on", True)))})
                    elif route == "api/store/report":
                        market.report(controller.market_server(), entry_id, str(body.get("reason") or ""))
                        self._json({"ok": True})
                    else:
                        market.take_down(controller.market_server(), entry_id)
                        self._json({"ok": True})
                except (TetoRelayError, ValueError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route in ("api/market/signup", "api/market/login", "api/market/logout", "api/market/profile",
                           "api/market/close", "api/store/shop"):
                # The password is passed on to the Market server and not kept.
                from . import market

                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") if 0 < length < 20_000 else {}
                    body = body if isinstance(body, dict) else {}
                    if route == "api/store/shop":
                        self._json({"ok": True, "url": controller.store_shop(str(body.get("kind") or ""),
                                                                              str(body.get("id") or ""))})
                        return
                    server = controller.market_server()
                    if route == "api/market/signup":
                        out = market.sign_up(server, body.get("email") or "", body.get("password") or "",
                                             body.get("handle") or "", body.get("name") or "")
                    elif route == "api/market/login":
                        out = market.sign_in(server, body.get("email") or "", body.get("password") or "")
                    elif route == "api/market/logout":
                        market.sign_out(server)
                        out = {"signed_in": False}
                    elif route == "api/market/close":
                        market.close_account(server)
                        out = {"signed_in": False}
                    else:
                        links = body.get("links") if isinstance(body.get("links"), list) else []
                        out = market.edit_profile(server, body.get("name") or "", body.get("bio") or "", links)
                    self._json({"ok": True, **out})
                except (TetoRelayError, ValueError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/open-link":
                # A creator's link (their shop, their channel), in the browser:
                # only a plain https address, as the Store's links.
                import webbrowser

                from .store import safe_url

                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") if 0 < length < 4000 else {}
                    url = safe_url(body.get("url") if isinstance(body, dict) else None)
                    if url is None:
                        self._json({"ok": False, "error": "That is not a plain https address."}, 400)
                    else:
                        webbrowser.open(url)
                        self._json({"ok": True})
                except ValueError as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/market/picture":
                from . import market

                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 300 * 1024:
                    self.close_connection = True
                    self._json({"ok": False, "error": "Use a picture of at most 300 KB."}, 400)
                    return
                try:
                    self._json({"ok": True, **market.set_avatar(controller.market_server(), self.rfile.read(length))})
                except (TetoRelayError, ValueError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/market/upload":
                # Shared from the panel: its multipart form (meta, file, demo)
                # goes to the Market server as it came.
                from . import market

                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > market.MAX_UPLOAD:
                    self.close_connection = True   # its body is not read
                    self._json({"ok": False, "error": "That upload is empty or too big: at most 95 MB."}, 400)
                    return
                try:
                    data = self.rfile.read(length)
                    self._json({"ok": True, **market.upload(controller.market_server(), data,
                                                            self.headers.get("Content-Type", ""))})
                except (TetoRelayError, ValueError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route in ("api/style/apply", "api/style/save", "api/style/delete", "api/style/import"):
                from urllib.parse import parse_qs, urlparse

                from . import presets

                length = int(self.headers.get("Content-Length", 0))
                try:
                    if length > presets.MAX_BYTES:
                        # Its body is not read, so nothing more can come down this connection.
                        self.close_connection = True
                        raise presets.PresetError("That file is too big to be a preset.")
                    raw = self.rfile.read(length)
                    if route == "api/style/import":
                        # The file itself is the body, as voicebanks and music arrive.
                        query = parse_qs(urlparse(self.path).query)
                        found = controller.import_style(raw, (query.get("apply") or ["1"])[0] != "0")
                        log.info("Preset imported: %s", found["name"])
                        self._json({"ok": True, **found})
                        return
                    body = json.loads(raw or b"{}") or {}
                    if not isinstance(body, dict):
                        raise ValueError("Send a JSON object.")
                    if route == "api/style/apply":
                        preset = presets.get(str(body.get("id") or ""))
                        self._json({"ok": True, "name": preset["name"], "config": controller.apply_style(preset)})
                    elif route == "api/style/save":
                        preset = presets.from_config(Config.load(controller.config_path), body.get("name"),
                                                     body.get("author"), body.get("description"))
                        self._json({"ok": True, "id": presets.save(preset), "name": preset["name"]})
                    else:
                        presets.delete(str(body.get("id") or ""))
                        self._json({"ok": True})
                except (TetoRelayError, ValueError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/take/delete":
                from .takes import discard

                folder, _ = self._take_query()
                if folder is None:
                    self._json({"ok": False, "error": "no such take"}, 404)
                else:
                    try:
                        discard(folder)
                        controller.takes_changed += 1
                        self._json({"ok": True})
                    except OSError as exc:
                        self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/take/open":
                folder, _ = self._take_query()
                if folder is None:
                    self._json({"ok": False, "error": "no such take"}, 404)
                else:
                    try:
                        import os

                        os.startfile(str(folder))  # noqa: S606 - our own data folder
                        self._json({"ok": True})
                    except (AttributeError, OSError) as exc:
                        self._json({"ok": False, "error": str(exc)}, 400)
            elif route == "api/quit":
                # The packaged app has no console to Ctrl+C, so the page is
                # where it is closed. serve() stops the relay on the way out.
                log.info("Quit requested from the control panel")
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif route == "api/install/voicebank":
                # Raw body with the filename in the query: multipart parsing is
                # not worth pulling in for a one-field form we also write.
                from urllib.parse import parse_qs, unquote, urlparse

                from .library import MAX_UPLOAD, install_voicebank

                query = parse_qs(urlparse(self.path).query)
                name = unquote((query.get("name") or ["upload"])[0])
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > MAX_UPLOAD:
                    self._json({"ok": False, "error": "That file is empty or too large."}, 400)
                    return
                try:
                    body = self.rfile.read(length)
                    info = install_voicebank(body, name, controller.cfg.voicebank_path())
                    log.info("Installed voicebank %r (%d samples)", info["name"], info["samples"])
                    _forget_library()
                    self._json({"ok": True, "installed": info})
                except ValueError as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
                except Exception as exc:  # noqa: BLE001
                    log.exception("install failed")
                    self._json({"ok": False, "error": str(exc)}, 500)
            elif route == "api/voicebank/rename":
                from .voicebank import discover, rename, select

                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                    bank = select(discover(controller.cfg.voicebank_path()), str(body.get("bank") or ""))
                    shown = rename(bank, str(body.get("name") or ""))
                    _forget_library()
                    log.info("Voicebank %s is now called %r", bank.key, shown or bank.name)
                    self._json({"ok": True, "name": shown})
                except (ValueError, TetoRelayError) as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
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
                    stale = set(changed) & LOADED_ONCE if controller.busy else set()
                    if controller.busy and effective_model(controller.cfg) != model_before:
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
        def watch() -> None:
            while True:
                time.sleep(1.0)
                if controller.should_quit():
                    log.info("The control panel was closed; quitting")
                    server.shutdown()
                    return

        threading.Thread(target=watch, name="panel-watch", daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        controller.stop()
        server.server_close()
    return 0
