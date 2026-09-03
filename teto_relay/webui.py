"""A small local control panel.

Deliberately dependency-free: it runs on `http.server` from the standard
library, so it adds nothing to disk. Function over decoration - the form is
generated from the Config dataclass, so any setting added later (including the
voice-conversion mode) appears here without touching this file.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

# The handful of settings on the main screen. Everything else lives behind
# "Show advanced", because a person who wants to sing through Teto needs a
# microphone, an output, a key to hold and a transpose - not a phonemizer.
# `voicebank` is not here either: it has its own picker beside the character,
# and unlike these it applies to a running relay.
#
# Ordered as devices, then what she sings, then how she sounds. `mode` (Engine)
# switches between the two pipelines, which is the biggest choice on the page.
ESSENTIALS: list[str] = [
    "input_device", "output_device", "ptt_key",
    "mode", "renderer_backend", "lyric_mode", "language", "whisper_model",
    "transpose", "playback_gain",
]

# Which settings to show, grouped. Anything not listed still appears, under
# "Other", so new options are never silently hidden.
GROUPS: dict[str, list[str]] = {
    "Mode": ["mode", "renderer_backend", "lyric_mode"],
    "Capture": ["capture_mode", "ptt_key", "input_device", "silence_ms", "min_chunk_ms", "max_chunk_ms"],
    # Speed vs accuracy lives here: device and compute type are the two biggest
    # levers on how long whisper takes, and neither was reachable before.
    "Transcription": [
        "whisper_device", "whisper_compute_type", "initial_prompt", "beam_size",
        "no_speech_threshold", "use_alignment", "align_device",
    ],
    "Pitch": [
        "pitch_method", "crepe_model", "crepe_device", "target_tone", "shift_mode",
        "stable_shift", "shift_tolerance", "transpose", "max_shift",
        "fix_octave_errors", "f0_min", "f0_max",
    ],
    "Expression": ["emit_contour", "contour_smooth_ms", "contour_points", "contour_range_cents"],
    "Timing": [
        "min_note_seconds", "seconds_per_syllable", "note_gap_ms",
        # Japanese mode only - a note there is one mora, not one word.
        "min_mora_seconds", "max_mora_seconds", "pause_borrow",
    ],
    "Voice conversion (RVC)": [
        "rvc_f0_method", "rvc_pitch", "rvc_index_rate", "rvc_protect",
        "rvc_filter_radius", "rvc_rms_mix_rate", "rvc_device",
        "rvc_model", "rvc_index",
    ],
    "Output": ["output_device", "playback_gain"],
}

HIDE = {"out_dir", "log_file", "openutau_dir", "voicebank_root", "queue_size", "keep_files"}

# Settings the pipeline reads once, when it builds a model or opens a device.
# Everything else is read per utterance, so changing it applies immediately -
# `language`, for one, is passed to whisper on every transcribe call. Only
# these need the relay stopped and started again.
LOADED_ONCE = {
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
}

# Settings whose value is a duration in seconds, shown as ms on the slider
# readout because that is how they are discussed everywhere else.
SECONDS = {
    "min_note_seconds", "seconds_per_syllable", "min_mora_seconds", "max_mora_seconds",
}

# Names people recognise, and a line of help where the name is not enough.
# Anything missing falls back to the field name with its underscores removed.
LABELS: dict[str, list[str]] = {
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
    "whisper_device": ["Listen on", "cuda is much faster than cpu, if it starts."],
    "whisper_compute_type": ["Listening precision", "int8 is fastest; float16 hears best on cuda."],
    "rvc_f0_method": ["Pitch tracking", "crepe is accurate; pm is fastest and rougher."],
    "rvc_index_rate": ["Voice likeness", "Higher leans on the model's index: closer to her, less like you."],
    "rvc_protect": ["Protect consonants", "Higher keeps your breath and consonants intact."],
    "rvc_pitch": ["Pitch shift", "Semitones. +12 is an octave up."],
    "rvc_device": ["Convert on", ""],
    "rvc_model": ["Voice model", ""],
    "rvc_index": ["Voice index", "Optional. Improves timbre; missing is a warning, not an error."],
    "rvc_filter_radius": ["Smooth pitch", "Higher is smoother and less breathy."],
    "rvc_rms_mix_rate": ["Keep your dynamics", "0 uses her loudness curve, 1 keeps yours."],
    "language": ["Language", "Needs a multilingual speech model - the .en ones only hear English."],
    "initial_prompt": ["Vocabulary hint", "Words to expect, so they are not misheard."],
    "beam_size": ["Search width", "Higher is more accurate and slower."],
    "no_speech_threshold": ["Silence cutoff", "Higher discards more as background noise."],
    "use_alignment": ["Measure word timing", "Corrects the ~0.12 s error in the timings."],
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
    "min_note_seconds": ["Shortest word", ""],
    "seconds_per_syllable": ["Time per syllable", "English banks only."],
    "note_gap_ms": ["Gap between notes", "Zero collapses the phonemizer."],
    "min_mora_seconds": ["Shortest mora", "Japanese banks. Below ~100 ms consonants swallow the vowel."],
    "max_mora_seconds": ["Longest mora", "Japanese banks. Caps how far a word spreads into a pause."],
    "pause_borrow": ["Sing into pauses", "0 keeps every pause, 1 uses them all up."],
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

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Teto Relay</title>
<style>
/* Light is the default; dark is a media query the toggle can override, so the
   page follows the system until someone says otherwise. */
:root{
  /* Native widgets - the select popup, scrollbars, focus rings - are drawn by
     the browser, not by this stylesheet. Without color-scheme the open
     dropdown list stays white on a dark page and is unreadable. */
  color-scheme:light;
  --solid:#fff;
  --ground:#efeaf2; --tint-a:rgba(232,36,79,.30); --tint-b:rgba(109,91,255,.26);
  --glass:rgba(255,255,255,.52); --glass-strong:rgba(255,255,255,.72);
  --edge:rgba(23,17,28,.09); --sheen:rgba(255,255,255,.85);
  --text:#1a1424; --muted:#6b6178; --faint:#9d94a9;
  --crimson:#d31b45; --violet:#5b46f0;
  --sink:rgba(23,17,28,.05); --shadow:0 18px 44px -22px rgba(23,17,28,.5);
  --grain:.05;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  color-scheme:dark;
  --solid:#191521;
  --ground:#0c0a11; --tint-a:rgba(232,36,79,.34); --tint-b:rgba(109,91,255,.30);
  --glass:rgba(255,255,255,.055); --glass-strong:rgba(255,255,255,.085);
  --edge:rgba(255,255,255,.075); --sheen:rgba(255,255,255,.16);
  --text:#f3eff6; --muted:#9c93a8; --faint:#6d6479;
  --crimson:#ff4a70; --violet:#8f7dff;
  --sink:rgba(0,0,0,.22); --shadow:0 22px 50px -26px #000;
  --grain:.055;
}}
:root[data-theme=dark]{
  color-scheme:dark;
  --solid:#191521;
  --ground:#0c0a11; --tint-a:rgba(232,36,79,.34); --tint-b:rgba(109,91,255,.30);
  --glass:rgba(255,255,255,.055); --glass-strong:rgba(255,255,255,.085);
  --edge:rgba(255,255,255,.075); --sheen:rgba(255,255,255,.16);
  --text:#f3eff6; --muted:#9c93a8; --faint:#6d6479;
  --crimson:#ff4a70; --violet:#8f7dff;
  --sink:rgba(0,0,0,.22); --shadow:0 22px 50px -26px #000;
  --grain:.055;
}
*{box-sizing:border-box}
html{background-color:var(--ground)}
/* background-color, not the `background` shorthand: transitioning the
   shorthand leaves it pinned to the old value when the variable behind it
   changes, so the page kept its dark ground after switching to light. */
body{margin:0;background-color:var(--ground);color:var(--text);font-size:14px;
  font-family:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;min-height:100vh;overflow-x:hidden;
  transition:background-color .4s,color .4s}
/* The light behind the glass. Off-centre on purpose - centred blobs read as a
   template, and the panels need an uneven field to refract. */
.lamp{position:fixed;border-radius:50%;filter:blur(84px);z-index:0;pointer-events:none}
.lamp.a{width:52vw;height:52vw;left:-12vw;top:-16vw;background:var(--tint-a)}
.lamp.b{width:44vw;height:44vw;right:-10vw;top:32vh;background:var(--tint-b)}
/* Grain. Glass without it looks like a CSS demo. */
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:99;opacity:var(--grain);
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E")}
/* Two columns: who is singing on the left, how she sings on the right. The
   left column is fixed and narrow because it is an identity card - portrait,
   name, picker - and the right takes the rest because settings are the long
   tail. Below 900px they stack, identity first. */
.wrap{position:relative;z-index:2;max-width:1180px;margin:0 auto;padding:0 28px 64px;
  display:grid;grid-template-columns:336px minmax(0,1fr);gap:16px;align-items:start}
.col-voice{display:flex;flex-direction:column;gap:16px;position:sticky;top:96px}
.col-set{display:flex;flex-direction:column;gap:16px;min-width:0}
@media(max-width:900px){
  .wrap{grid-template-columns:minmax(0,1fr);padding:0 16px 56px}
  .col-voice{position:static}
}

/* Hardware silkscreen, not a tracked-out uppercase eyebrow. */
.tag{font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;font-size:10.5px;
  letter-spacing:.04em;color:var(--faint);margin:0}

.pane{background:var(--glass);backdrop-filter:blur(22px) saturate(150%);
  -webkit-backdrop-filter:blur(22px) saturate(150%);border:1px solid var(--edge);
  border-radius:18px;box-shadow:var(--shadow),inset 0 1px 0 var(--sheen)}

/* ---------- engine tabs ----------
   The biggest choice on the page, so it is the first thing and it looks like a
   switch on a device rather than a dropdown. Each engine is a different
   machine: utau sings your words as notes, voice replays your delivery in her
   timbre. Bahnschrift ships with Windows and is the one condensed face here -
   it reads as panel silkscreen where Segoe would read as a web page. */
.tabs{display:flex;gap:3px;padding:3px;background:var(--sink);border-radius:13px}
.tabs button{font-family:Bahnschrift,"Segoe UI Semibold",system-ui,sans-serif;
  font-size:13px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
  background-color:transparent;color:var(--muted);padding:8px 18px;border-radius:10px;
  transition:filter .15s}
.tabs button[aria-selected=true]{background-color:var(--crimson);color:#fff}
.tabs button:not([aria-selected=true]):hover{color:var(--text)}

/* ---------- the voice card ---------- */
.voice{padding:16px 16px 18px;display:flex;flex-direction:column;gap:13px}
.portrait{position:relative;aspect-ratio:1;border-radius:13px;overflow:hidden;
  background-color:var(--sink);display:grid;place-items:center}
.portrait img{width:100%;height:100%;object-fit:cover;display:block}
/* The accent is read from this artwork, so letting it bleed up from the floor
   ties the colour to its source instead of looking applied. */
.portrait::after{content:"";position:absolute;inset:auto 0 0 0;height:38%;
  background:linear-gradient(to top,var(--tint-a),transparent);pointer-events:none}
.vname{font-family:Bahnschrift,"Segoe UI Semibold",system-ui,sans-serif;font-size:19px;
  font-weight:600;letter-spacing:.01em;margin:0;line-height:1.2}
.vmeta{font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;font-size:10.5px;
  color:var(--faint);margin:2px 0 0}
.voice select{width:100%;background-color:var(--sink);border:1px solid var(--edge);
  color:var(--text);font-family:inherit;font-size:13.5px;padding:10px 12px;
  border-radius:11px;cursor:pointer}
/* A file input is unstyleable and says "No file chosen"; the label is the
   button and the real input sits behind it. */
.addvoice{position:relative;display:block}
.addvoice input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%}
.addvoice span{display:block;text-align:center;font-size:12.5px;color:var(--muted);
  padding:9px;border:1px dashed var(--edge);border-radius:11px}
.addvoice:hover span{color:var(--text);border-color:var(--crimson)}

/* ---------- bar ---------- */
.bar{position:sticky;top:14px;z-index:20;max-width:1000px;margin:14px auto 22px;
  display:flex;align-items:center;gap:14px;padding:9px 9px 9px 18px;border-radius:16px}
.mark{font-size:13.5px;font-weight:600;letter-spacing:.01em;display:flex;align-items:center;gap:9px;
  white-space:nowrap}
.mark i{width:7px;height:7px;border-radius:50%;background:var(--faint);font-style:normal;
  transition:background .3s}
.live .mark i{background:var(--crimson);box-shadow:0 0 0 4px rgba(211,27,69,.16)}
.picker{display:flex;align-items:center;gap:9px;margin-left:6px;padding:4px 10px 4px 4px;
  background:var(--sink);border-radius:12px}
.avatar{width:34px;height:34px;border-radius:9px;image-rendering:pixelated;flex:none;
  background:var(--sink)}
.live .avatar{box-shadow:0 0 0 1.5px var(--crimson)}
.picker select{background:none;border:0;color:var(--text);font-family:inherit;font-size:13px;
  cursor:pointer;padding:3px 2px}
.spacer{margin-left:auto}
/* background-color, never the `background` shorthand: transitioning the
   shorthand pins it to the old value when the variable behind it changes, so
   the button kept Teto's red after switching to a voice with its own colour.
   Same trap as the light/dark ground. */
button{font-family:inherit;font-size:13px;font-weight:600;border:0;border-radius:11px;
  padding:11px 22px;cursor:pointer;background-color:var(--crimson);color:#fff;
  transition:filter .15s}   /* no colour transition: a transitioned property
     whose value is a var() gets pinned to the old colour when the var
     changes, which is how the accent kept reverting to Teto's red */
button:hover{filter:brightness(1.08)}
button:disabled{opacity:.5;cursor:default}
button:focus-visible{outline:2px solid var(--violet);outline-offset:3px}
.live #toggle{background-color:var(--sink);color:var(--crimson);box-shadow:inset 0 0 0 1px var(--edge)}
.icon{background-color:var(--sink);color:var(--muted);padding:0;width:38px;height:38px;border-radius:11px;
  font-size:15px;line-height:1;display:grid;place-items:center}
.icon:hover{color:var(--text);filter:none;background-color:var(--glass-strong)}
.ghost{background-color:transparent;color:var(--muted);font-weight:400;padding:9px 14px}
.ghost:hover{color:var(--text);background-color:var(--sink);filter:none}

/* ---------- the utterance ---------- */
.stage{padding:26px 28px 24px;margin-bottom:16px}
.stagehead{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.state{margin-left:auto;display:flex;align-items:center;gap:7px;font-family:"Cascadia Mono",Consolas,
  ui-monospace,monospace;font-size:10.5px;color:var(--muted)}
.state i{width:6px;height:6px;border-radius:50%;background:var(--faint);font-style:normal}
.live .state i{background:var(--crimson);animation:pulse 1.9s ease-in-out infinite}
@keyframes pulse{50%{opacity:.25}}
.heard{font-family:"Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
  font-size:37px;font-weight:400;line-height:1.16;letter-spacing:-.021em;margin:0;
  overflow-wrap:break-word}
.kana{font-family:"Yu Gothic UI","Meiryo","MS Gothic",sans-serif;font-size:20px;line-height:1.55;
  color:var(--muted);margin:13px 0 0;overflow-wrap:break-word}
.sungtag{font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;font-size:9.5px;
  color:var(--crimson);background:rgba(211,27,69,.11);border-radius:5px;padding:3px 7px;
  margin-left:10px;vertical-align:middle;white-space:nowrap}
.idle{font-size:18px;font-weight:400;color:var(--faint);margin:0;line-height:1.55}
.idle kbd{font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;font-size:12.5px;
  background:var(--sink);border-radius:6px;padding:3px 8px;color:var(--muted)}

.roll{margin-top:24px;overflow-x:auto;overflow-y:hidden;padding-bottom:6px}
.roll::-webkit-scrollbar{height:5px}
.roll::-webkit-scrollbar-thumb{background:var(--edge);border-radius:3px}
.rollinner{min-width:max-content;position:relative}
.ribbon{position:absolute;left:0;top:6px;pointer-events:none;overflow:visible}
.ribbon path{fill:none;stroke:var(--crimson);stroke-width:1.4;stroke-linejoin:round;
  stroke-linecap:round;opacity:.55}
/* The roll is the signature: every mora sits at the pitch it is sung at, so a
   phrase reads as a melody instead of a list of numbers. Blocks are absolutely
   positioned inside a fixed-height lane - x is note order (the payload carries
   no durations, so pretending to show time would be a lie), y is the tone. */
.notes{position:relative;height:132px;margin-top:6px}
.lane{position:absolute;left:0;right:0;height:1px;background:var(--edge);opacity:.5}
.lane s{position:absolute;left:0;top:-14px;font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;
  font-size:9px;color:var(--faint);text-decoration:none}
.note{position:absolute;background-color:var(--sink);border-radius:8px;text-align:center;
  padding:5px 0 4px;box-shadow:inset 0 0 0 1px var(--edge);animation:rise .3s backwards}
@keyframes rise{from{opacity:0;transform:translateY(6px)}}
.note b{font-family:"Yu Gothic UI","Meiryo",sans-serif;font-size:15px;font-weight:400;display:block;
  line-height:1.15}
.note s{font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;font-size:9px;color:var(--faint);
  display:block;text-decoration:none}
.meters{display:flex;flex-wrap:wrap;gap:28px;margin:24px 0 0;padding-top:18px;
  border-top:1px solid var(--edge)}
.meters div{min-width:62px}
.meters dt{font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;font-size:10px;
  color:var(--faint)}
.meters dd{margin:5px 0 0;font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;font-size:15px}

/* ---------- controls ---------- */
.deck{padding:20px 28px 24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(224px,1fr));
  gap:6px 28px}
.ctl{padding:12px 0}
.ctl label{display:block;font-size:12.5px;color:var(--muted);margin-bottom:9px}
.ctl .help{display:block;font-size:11px;color:var(--faint);margin-top:7px;line-height:1.45}
.slide{display:flex;align-items:center;gap:13px}
.readout{font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;font-size:12.5px;
  min-width:50px;text-align:right;color:var(--text)}
input[type=range]{-webkit-appearance:none;appearance:none;flex:1;height:20px;background:none;
  cursor:pointer;min-width:0}
input[type=range]::-webkit-slider-runnable-track{height:3px;border-radius:2px;
  background:linear-gradient(90deg,var(--crimson) var(--pct,0%),var(--edge) var(--pct,0%))}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:15px;height:15px;
  border-radius:50%;background:#fff;margin-top:-6px;border:0;
  box-shadow:0 1px 4px rgba(0,0,0,.35),0 0 0 1px var(--edge)}
input[type=range]:focus-visible{outline:2px solid var(--violet);outline-offset:4px}
input[type=text],input[type=number],select.box{width:100%;background:var(--sink);color:var(--text);
  border:1px solid transparent;border-radius:10px;padding:10px 11px;font-family:inherit;font-size:13px}
select.box{cursor:pointer}
/* The popup list is opaque even though the closed control is glass - a
   translucent list over a blurred page is unreadable. */
select option{background:var(--solid);color:var(--text)}
input:focus-visible,select.box:focus-visible{outline:0;border-color:var(--violet)}
input[type=checkbox]{appearance:none;width:38px;height:21px;border-radius:11px;background:var(--sink);
  position:relative;cursor:pointer;margin:0;flex:none}
input[type=checkbox]::after{content:"";position:absolute;top:3px;left:3px;width:15px;height:15px;
  border-radius:50%;background:var(--faint);transition:transform .2s,background .2s}
input[type=checkbox]:checked{background-color:rgba(211,27,69,.18)}
input[type=checkbox]:checked::after{transform:translateX(17px);background:var(--crimson)}

/* ---------- advanced ---------- */
.advbar{margin-top:18px;display:flex;align-items:center;gap:12px}
.adv{display:none;margin-top:16px;padding:6px 28px 24px}
.open .adv{display:block}
.advtoggle span{display:inline-block;transition:transform .22s;margin-right:9px;font-size:10px}
.open .advtoggle span{transform:rotate(90deg)}
.group{padding:20px 0;border-top:1px solid var(--edge)}
.group:first-child{border-top:0}
.grouprows{display:grid;grid-template-columns:repeat(auto-fit,minmax(224px,1fr));gap:0 28px}
#log{margin:12px 0 0;background:var(--sink);border-radius:12px;padding:14px;height:200px;
  overflow:auto;font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;font-size:11px;
  line-height:1.7;white-space:pre-wrap;color:var(--muted)}
.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(8px);z-index:60;
  padding:12px 20px;font-size:13px;border-radius:12px;opacity:0;pointer-events:none;
  transition:opacity .22s,transform .22s;background:var(--glass-strong);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
  border:1px solid var(--edge);box-shadow:var(--shadow)}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media (max-width:720px){
  .wrap{padding:0 16px 48px} .bar{margin:10px 16px 18px;padding-left:14px}
  .heard{font-size:26px} .kana{font-size:17px}
  .stage,.deck,.adv{padding-left:18px;padding-right:18px}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>

<div class="lamp a"></div><div class="lamp b"></div>

<div id="app">
<header class="bar pane">
  <span class="mark"><i></i>Teto Relay</span>
  <div class="tabs" role="tablist" aria-label="Engine">
    <button role="tab" id="tab-utau" aria-selected="true">UTAU</button>
    <button role="tab" id="tab-voice" aria-selected="false">Voice</button>
  </div>
  <span class="spacer"></span>
  <span class="state"><i></i><span id="stateText">stopped</span></span>
  <button class="icon" id="theme" title="Switch theme" aria-label="Switch theme"></button>
  <button id="toggle">Start</button>
</header>

<div class="wrap">
  <div class="col-voice">
    <section class="voice pane">
      <div class="portrait"><img id="avatar" alt=""></div>
      <div>
        <p class="vname" id="vname">&nbsp;</p>
        <p class="vmeta" id="vmeta"></p>
      </div>
      <select id="bank" aria-label="Choose voicebank"></select>
      <label class="addvoice" id="addvoice">
        <input type="file" id="up-bank" accept=".zip">
        <span id="addvoicetext">Add a voicebank</span>
      </label>
      <!-- Same control, different engine: in Voice mode the identity is an RVC
           model, so the picker and the upload change with the tab. -->
      <label class="addvoice" id="addrvc" hidden>
        <input type="file" id="up-rvc" accept=".pth,.index">
        <span>Add an RVC voice (.pth)</span>
      </label>
      <p class="help" id="up-status"></p>
    </section>

    <section class="stage pane">
      <div class="stagehead">
        <p class="tag" id="stagetag">what she sang</p>
      </div>
      <div id="hero"></div>
      <div class="roll" id="roll" hidden>
        <div class="rollinner">
          <svg class="ribbon" id="ribbon" aria-hidden="true"></svg>
          <div class="notes" id="notes"></div>
        </div>
      </div>
      <dl class="meters" id="meters"></dl>
    </section>
  </div>

  <div class="col-set">
    <section class="deck pane" id="deck"></section>
    <div class="advbar">
      <button class="ghost advtoggle" id="advtoggle"><span>&#9654;</span>Show all settings</button>
      <span class="spacer"></span>
      <button class="ghost" id="save">Save settings</button>
    </div>
    <div class="adv pane" id="adv">
      <div id="groups"></div>
      <div class="group">
        <p class="tag">activity</p>
        <pre id="log">Waiting for the relay to start.</pre>
      </div>
    </div>
  </div>
</div>
</div>
<div class="toast" id="toast"></div>

<script>
let cfg={}, meta={}, lastKey='';
const $=id=>document.getElementById(id);
const app=()=>$('app');

/* ---------- theme: follow the system until told otherwise ---------- */
/* Drawn rather than typed: the sun and moon code points render as a weak
   asterisk in the Windows UI font. */
const SUN='<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"'+
  ' stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/>'+
  '<path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2M5.3 5.3l1.6 1.6'+
  'M17.1 17.1l1.6 1.6M18.7 5.3l-1.6 1.6M6.9 17.1l-1.6 1.6"/></svg>';
const MOON='<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"'+
  ' stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'+
  '<path d="M20 14.2A8.2 8.2 0 0 1 9.8 4a8.4 8.4 0 1 0 10.2 10.2z"/></svg>';
function applyTheme(mode){
  if(mode) document.documentElement.setAttribute('data-theme',mode);
  else document.documentElement.removeAttribute('data-theme');
  const dark=(mode==='dark')||(!mode&&matchMedia('(prefers-color-scheme:dark)').matches);
  $('theme').innerHTML=dark?SUN:MOON;
  $('theme').title=dark?'Switch to light':'Switch to dark';
}
(function(){let saved=null;try{saved=localStorage.getItem('teto.theme');}catch(e){}
  applyTheme(saved);})();
$('theme').onclick=()=>{
  const dark=document.documentElement.getAttribute('data-theme')==='dark'||
    (!document.documentElement.hasAttribute('data-theme')&&
      matchMedia('(prefers-color-scheme:dark)').matches);
  const next=dark?'light':'dark';
  applyTheme(next);
  try{localStorage.setItem('teto.theme',next);}catch(e){}
};

function toast(msg){
  const t=$('toast'); t.textContent=msg; t.classList.add('show');
  clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove('show'),2800);
}
function labelOf(k){return (meta.labels&&meta.labels[k])?meta.labels[k][0]:k.replace(/_/g,' ');}
function helpOf(k){return (meta.labels&&meta.labels[k])?meta.labels[k][1]:'';}
function fmt(k,v){
  if(meta.seconds&&meta.seconds.includes(k)) return Math.round(v*1000)+' ms';
  if(k.endsWith('_ms')) return v+' ms';
  if(k==='transpose') return (v>0?'+':'')+v;
  return String(Math.round(v*100)/100);
}
function paint(el){const a=+el.min,b=+el.max;el.style.setProperty('--pct',((el.value-a)/(b-a)*100)+'%');}

async function load(){
  const d=await (await fetch('api/config')).json();
  cfg=d.config; meta=d.meta;
  renderPicker(); renderDeck(); renderGroups(); paintEngine();
}
function renderPicker(){
  const pick=$('bank'); pick.innerHTML='';
  for(const b of (meta.banks||[])){
    const o=document.createElement('option'); o.value=b.key; o.textContent=b.key;
    if(b.key===cfg.voicebank) o.selected=true; pick.appendChild(o);
  }
  showBank(cfg.voicebank);
}
function showBank(key){
  const b=(meta.banks||[]).find(x=>x.key===key), img=$('avatar');
  img.src='api/bank-image?bank='+encodeURIComponent(key)+'&t='+Date.now();
  img.alt=b?('Voicebank '+b.key):'';
  img.title='';
  /* The name and what it is were a tooltip nobody would find. */
  $('vname').textContent=b?b.name:key;
  $('vmeta').textContent=b?(b.flavour+' · '+b.entries+' samples · sings '+b.lyrics):'';
  applyAccent(b&&b.accent);
}
/* Each voice tints the panel with the colour of its own artwork, so a bank you
   add brings its own look instead of borrowing Teto's red. */
function applyAccent(hex){
  const root=document.documentElement;
  if(!hex){root.style.removeProperty('--crimson'); root.style.removeProperty('--tint-a'); return;}
  root.style.setProperty('--crimson',hex);
  const n=parseInt(hex.slice(1),16);
  root.style.setProperty('--tint-a','rgba('+(n>>16&255)+','+(n>>8&255)+','+(n&255)+',.32)');
}

/* Devices are a name substring in config, so whatever is saved stays an option
   even when it is shorter than the full device name. */
function deviceOptions(k){
  const kind=(k==='input_device')?'input':'output';
  const list=((meta.devices||{})[kind]||[]).slice();
  const current=cfg[k]||'';
  if(current&&!list.some(n=>n===current)) list.unshift(current);
  return {list:list,blank:(kind==='input')?'System default':''};
}
function control(k){
  const wrap=document.createElement('div'); wrap.className='ctl';
  const lab=document.createElement('label'); lab.textContent=labelOf(k); lab.htmlFor='f-'+k;
  wrap.appendChild(lab);
  const range=meta.ranges?meta.ranges[k]:null;
  const isDevice=(k==='input_device'||k==='output_device');
  let inp,out=null;
  if(isDevice&&((meta.devices||{}).input||[]).length){
    const {list,blank}=deviceOptions(k);
    inp=document.createElement('select'); inp.className='box';
    if(blank!==''){const o=document.createElement('option'); o.value=''; o.textContent=blank;
      if(!cfg[k]) o.selected=true; inp.appendChild(o);}
    for(const n of list){
      const o=document.createElement('option'); o.value=n; o.textContent=n;
      if(cfg[k]===n) o.selected=true; inp.appendChild(o);
    }
    wrap.appendChild(inp);
  } else if(meta.choices[k]){
    inp=document.createElement('select'); inp.className='box';
    for(const opt of meta.choices[k]){
      const o=document.createElement('option'); o.value=opt; o.textContent=opt;
      if(String(cfg[k])===String(opt)) o.selected=true; inp.appendChild(o);
    }
    wrap.appendChild(inp);
  } else if(typeof cfg[k]==='boolean'){
    inp=document.createElement('input'); inp.type='checkbox'; inp.checked=cfg[k];
    wrap.appendChild(inp);
  } else if(range&&typeof cfg[k]==='number'){
    inp=document.createElement('input'); inp.type='range';
    inp.min=range[0]; inp.max=range[1]; inp.step=range[2]; inp.value=cfg[k];
    out=document.createElement('span'); out.className='readout'; out.textContent=fmt(k,cfg[k]);
    inp.addEventListener('input',()=>{out.textContent=fmt(k,+inp.value); paint(inp);});
    const row=document.createElement('div'); row.className='slide';
    row.appendChild(inp); row.appendChild(out); wrap.appendChild(row);
  } else {
    inp=document.createElement('input');
    inp.type=(typeof cfg[k]==='number')?'number':'text';
    if(typeof cfg[k]==='number') inp.step='any';
    inp.value=cfg[k]===null?'':cfg[k];
    wrap.appendChild(inp);
  }
  inp.id='f-'+k; inp.dataset.key=k; inp.className=(inp.className||'')+' field';
  const help=helpOf(k);
  if(help){const s=document.createElement('span'); s.className='help'; s.textContent=help;
    wrap.appendChild(s);}
  if(inp.type==='range') paint(inp);
  return wrap;
}
function renderDeck(){
  const host=$('deck'); host.innerHTML='';
  for(const k of (meta.essentials||[])){
    const node=control(k);
    /* The main screen applies as you change it, the way the voice picker and
       the engine tabs already do. Choosing a language and having nothing
       happen - because the change was sitting unsaved behind a button - is
       the single most confusing thing this page did. Advanced settings still
       wait for Save, where deliberate batching is the point. */
    node.addEventListener('change',()=>saveOne(k));
    host.appendChild(node);
  }
}
async function saveOne(k){
  const value=collect()[k];
  if(value===undefined) return;
  cfg[k]=value;
  const d=await (await fetch('api/config',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({[k]:value})})).json();
  if(!d.ok){ toast('Could not save: '+d.error); return; }
  if(k==='mode'){ paintEngine(); renderGroups(); }
  toast(d.restart&&d.restart.length ? 'Saved \\u2014 restart to apply' : 'Saved');
}
/* The two engines are different machines and most settings belong to exactly
   one of them. Showing RVC sliders while UTAU is singing - or note timing while
   it isn't - is most of why this page was hard to read. Hide what the active
   engine cannot use; nothing is lost, since switching the tab brings it back. */
const ENGINE_ONLY={
  'Pitch':'utau', 'Expression':'utau', 'Timing':'utau',
  'Voice conversion (RVC)':'voice',
};
function engineOf(){ return (cfg.mode||'utau').toLowerCase(); }
function renderGroups(){
  const host=$('groups'); host.innerHTML='';
  for(const [name,keys] of Object.entries(meta.groups)){
    if(!keys.length) continue;
    if(ENGINE_ONLY[name] && ENGINE_ONLY[name]!==engineOf()) continue;
    const sec=document.createElement('section'); sec.className='group';
    const h=document.createElement('p'); h.className='tag'; h.textContent=name.toLowerCase();
    sec.appendChild(h);
    const rows=document.createElement('div'); rows.className='grouprows';
    for(const k of keys) rows.appendChild(control(k));
    sec.appendChild(rows); host.appendChild(sec);
  }
}
function collect(){
  const out={};
  for(const el of document.querySelectorAll('.field')){
    const k=el.dataset.key;
    if(el.type==='checkbox') out[k]=el.checked;
    else if(el.type==='number'||el.type==='range') out[k]=el.value===''?null:Number(el.value);
    else out[k]=el.value;
  }
  return out;
}

function tag(t){const s=document.createElement('span'); s.className='sungtag'; s.textContent=t; return s;}
function renderHero(d){
  const key=JSON.stringify([d.heard,d.kana,d.notes,d.running,d.engine,d.last]);
  if(key===lastKey) return;
  lastKey=key;
  const hero=$('hero'), roll=$('roll');
  hero.innerHTML='';
  // Voice mode transcribes nothing and sings no notes - it replays what you
  // said in her timbre - so the note roll and the two lyric lines have nothing
  // to show. Say what happened instead of showing an empty stage.
  if(d.engine==='voice'){
    roll.hidden=true;
    const p=document.createElement('p');
    if(d.last){
      p.className='heard'; p.textContent=d.last;
      p.appendChild(tag('your delivery, her voice'));
    } else {
      p.className='idle';
      p.innerHTML=d.running
        ? 'Hold <kbd>'+String(cfg.ptt_key||'F8').toUpperCase()+'</kbd> and speak - you will come back as Teto.'
        : 'Start the relay to talk in Teto\\u2019s voice.';
    }
    hero.appendChild(p);
    return;
  }
  if(!d.notes.length){
    const p=document.createElement('p'); p.className='idle';
    if(d.running) p.innerHTML='Hold <kbd>'+String(cfg.ptt_key||'F8').toUpperCase()+'</kbd> and speak.';
    else p.textContent='Start the relay, then hold your push-to-talk key and speak.';
    hero.appendChild(p); roll.hidden=true; return;
  }
  const sung=d.lyrics==='morae';
  const en=document.createElement('p'); en.className='heard'; en.textContent=d.heard||'\\u2014';
  if(!sung) en.appendChild(tag('sung'));
  hero.appendChild(en);
  if(d.kana){
    const jp=document.createElement('p'); jp.className='kana'; jp.textContent=d.kana;
    if(sung) jp.appendChild(tag('sung'));
    hero.appendChild(jp);
  }
  roll.hidden=false;
  renderNotes(d.notes);
}
/* Where a tone sits in the lane, 0 at the bottom. Shared by the blocks and the
   ribbon so they cannot drift apart. A phrase on one note would divide by zero
   and also deserves to sit in the middle rather than on the floor, so the span
   never goes below four semitones. */
const ROLL={h:132, box:34, gap:5, pad:20};
function toneY(tone,lo,span){
  return ROLL.pad + (1-(tone-lo)/span)*(ROLL.h-ROLL.box-ROLL.pad*2);
}
function rollRange(notes){
  const t=notes.map(n=>n.tone), lo=Math.min(...t), hi=Math.max(...t);
  const span=Math.max(hi-lo,4), mid=(lo+hi)/2;
  return {lo:mid-span/2, span:span};
}
function renderNotes(notes){
  const host=$('notes'); host.innerHTML='';
  const {lo,span}=rollRange(notes);
  host.style.width=(notes.length*(ROLL.box+ROLL.gap))+'px';
  // Two rules mark the range being sung, so the heights mean something.
  [lo+span,lo].forEach((tone,i)=>{
    const l=document.createElement('div'); l.className='lane';
    l.style.top=(toneY(tone,lo,span)+ROLL.box/2)+'px';
    const s=document.createElement('s'); s.textContent=Math.round(tone);
    l.appendChild(s); host.appendChild(l);
  });
  notes.forEach((n,i)=>{
    const d=document.createElement('div'); d.className='note';
    d.style.animationDelay=Math.min(i*20,420)+'ms';
    d.style.left=(i*(ROLL.box+ROLL.gap))+'px';
    d.style.top=toneY(n.tone,lo,span)+'px';
    d.style.width=ROLL.box+'px';
    d.title=n.lyric+' at MIDI '+n.tone;
    const b=document.createElement('b'); b.textContent=n.lyric;
    const s=document.createElement('s'); s.textContent=n.tone;
    d.appendChild(b); d.appendChild(s); host.appendChild(d);
  });
  requestAnimationFrame(()=>drawRibbon(notes));
}
/* One hairline through the blocks, at the same coordinates they use, so the
   contour reads across the gaps. The old version drew a second curve above the
   row; now that the blocks carry the pitch themselves, a separate chart of the
   same numbers would just be decoration. */
function drawRibbon(notes){
  const svg=$('ribbon');
  if(!notes.length){svg.innerHTML='';return;}
  const w=notes.length*(ROLL.box+ROLL.gap), h=ROLL.h;
  svg.setAttribute('width',w); svg.setAttribute('height',h);
  svg.setAttribute('viewBox','0 0 '+w+' '+h);
  const {lo,span}=rollRange(notes);
  const pts=notes.map((n,i)=>[i*(ROLL.box+ROLL.gap)+ROLL.box/2,
    toneY(n.tone,lo,span)+ROLL.box/2]);
  const dpath=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
  svg.innerHTML='<path d="'+dpath+'"></path>';
  const line=svg.querySelector('path');
  if(!matchMedia('(prefers-reduced-motion:reduce)').matches&&line.getTotalLength){
    const len=line.getTotalLength();
    line.animate([{strokeDasharray:len,strokeDashoffset:len},{strokeDasharray:len,strokeDashoffset:0}],
      {duration:Math.min(160+notes.length*22,900),easing:'ease-out'});
  }
}
function renderMeters(s){
  const m=$('meters'); m.innerHTML='';
  for(const [k,v] of [['heard',s.speech!=null?s.speech+'s':'\\u2014'],
                      ['analysed',s.analyse!=null?s.analyse+'s':'\\u2014'],
                      ['rendered',s.render!=null?s.render+'s':'\\u2014'],
                      ['behind',s.behind!=null?s.behind+'s':'\\u2014'],
                      ['pitch',s.method||'\\u2014']]){
    const d=document.createElement('div');
    const dt=document.createElement('dt'); dt.textContent=k;
    const dd=document.createElement('dd'); dd.textContent=v;
    if(k==='pitch') dd.style.fontSize='12px';
    d.appendChild(dt); d.appendChild(dd); m.appendChild(d);
  }
}

$('bank').onchange=async e=>{
  const key=e.target.value;
  const d=await (await fetch('api/voicebank',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({bank:key})})).json();
  if(d.ok){cfg.voicebank=key; showBank(key);
    toast(d.applied?('Now singing as '+key):(key+' loads when you start the relay.'));
  } else toast('Could not switch: '+d.error);
};
$('save').onclick=async()=>{
  const d=await (await fetch('api/config',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(collect())})).json();
  if(!d.ok){ toast('Could not save: '+d.error); return; }
  toast(d.restart&&d.restart.length
    ? 'Saved — restart to apply '+d.restart.join(', ').replace(/_/g,' ')
    : 'Saved and applied');
};
$('toggle').onclick=async e=>{
  const btn=e.currentTarget, starting=btn.dataset.state!=='running';
  btn.disabled=true; btn.textContent=starting?'Starting':'Stopping';
  try{
    const d=await (await fetch(starting?'api/start':'api/stop',{method:'POST'})).json();
    if(d&&d.ok===false) toast('Could not start: '+d.error);
  }catch(err){toast('Could not reach the relay.');}
  btn.disabled=false; poll();
};
/* ---------- engine tabs ----------
   The tab is the mode setting, so it writes straight through and re-renders
   the parts of the page whose meaning depends on it: which identity the voice
   card shows, and which settings apply. */
function paintEngine(){
  const eng=engineOf(), utau=eng!=='voice';
  $('tab-utau').setAttribute('aria-selected',String(utau));
  $('tab-voice').setAttribute('aria-selected',String(!utau));
  $('bank').hidden=!utau;
  $('addvoice').hidden=!utau;
  $('addrvc').hidden=utau;
  $('stagetag').textContent=utau?'what she sang':'what she said';
  if(!utau){
    // In voice mode the identity is the RVC model, not a voicebank.
    const m=String(cfg.rvc_model||'').split(/[\\\\/]/).pop().replace(/\\.pth$/i,'');
    $('vname').textContent=m||'No voice model';
    $('vmeta').textContent=m?'RVC · your delivery, her timbre':'Add a .pth to sing in a voice';
  } else if(meta.banks){
    showBank(cfg.voicebank);
  }
}
async function setEngine(mode){
  if(engineOf()===mode) return;
  cfg.mode=mode;
  paintEngine(); renderDeck(); renderGroups();
  await fetch('api/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:mode})});
  toast(mode==='voice'?'Voice engine':'UTAU engine');
}
$('tab-utau').onclick=()=>setEngine('utau');
$('tab-voice').onclick=()=>setEngine('voice');

/* Uploads: the body is the file and the name rides in the query, so there is
   no multipart parser on either end. */
async function upload(kind, file, note){
  if(!file) return;
  const status=$('up-status');
  status.textContent='Uploading '+file.name+' ('+(file.size/1e6).toFixed(0)+' MB)…';
  try{
    const r=await fetch('api/install/'+kind+'?name='+encodeURIComponent(file.name),
      {method:'POST',body:file});
    const d=await r.json();
    if(!d.ok){status.textContent=d.error; toast('Could not install it'); return;}
    status.textContent=note(d.installed);
    toast('Installed');
    await load();                       // the picker and accents refresh
  }catch(err){ status.textContent='Upload failed: '+err; }
}
$('up-bank').onchange=e=>upload('voicebank',e.target.files[0],
  i=>'Installed '+i.name+' — '+i.samples+' samples. Pick it in the top bar.');
$('up-rvc').onchange=e=>upload('rvc',e.target.files[0],
  i=>i.kind==='index' ? 'Index saved; it will be used with the current model.'
    : 'Installed '+i.name+' — '+i.version+', '+i.sample_rate+' Hz'+(i.pitch?', pitched':'')+
      '. Set Engine to voice to use it.');

$('advtoggle').onclick=()=>{
  const open=app().classList.toggle('open');
  $('advtoggle').innerHTML='<span>&#9654;</span>'+(open?'Hide advanced':'Show advanced');
  try{localStorage.setItem('teto.adv',open?'1':'0');}catch(e){}
};
if((()=>{try{return localStorage.getItem('teto.adv')==='1';}catch(e){return false;}})()){
  app().classList.add('open');
  $('advtoggle').innerHTML='<span>&#9654;</span>Hide advanced';
}

async function poll(){
  try{
    const d=await (await fetch('api/status')).json();
    app().classList.toggle('live',d.running);
    const btn=$('toggle');
    btn.dataset.state=d.running?'running':'stopped';
    btn.textContent=d.running?'Stop':'Start';
    $('stateText').textContent=d.running?(d.paused?'paused':'listening'):'stopped';
    const voice=d.engine==='voice';
    $('stagetag').textContent=voice?'now speaking':'heard';
    // The voicebank picker does nothing in voice mode - the RVC model is the
    // voice - so it is disabled rather than left looking live.
    const pick=$('bank');
    pick.disabled=voice;
    pick.title=voice?'Voice mode uses the RVC model, not a voicebank':'';
    if(d.bank&&d.bank!==pick.value){pick.value=d.bank; showBank(d.bank);}
    renderHero(d); renderMeters(d.stats||{});
    const box=$('log');
    const stick=box.scrollTop+box.clientHeight>=box.scrollHeight-24;
    box.textContent=d.log.join('\\n')||'Waiting for the relay to start.';
    if(stick) box.scrollTop=box.scrollHeight;
  }catch(e){}
}
load().then(poll); setInterval(poll,1000);
</script>
"""


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

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.relay = None
        self._lock = threading.Lock()
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

            self.cfg = Config.load()  # pick up anything just saved
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

    def status(self) -> dict:
        relay = self.relay
        return {
            "running": self.running,
            "last": getattr(relay, "last_text", "") if relay else "",
            "heard": getattr(relay, "last_source", "") if relay else "",
            "kana": getattr(relay, "last_kana", "") if relay else "",
            "notes": [
                {"lyric": lyric, "tone": tone}
                for lyric, tone in (getattr(relay, "last_notes", []) if relay else [])
            ],
            "stats": (getattr(relay, "last_stats", {}) if relay else {}) or {},
            "bank": relay.bank.key if relay else self.cfg.voicebank,
            "engine": (relay.engine if relay else (self.cfg.mode or "utau").lower()),
            "lyrics": (
                "morae" if relay and relay.engine != "voice" and relay._japanese_lyrics()
                else "words"
            ) if relay else "",
            "paused": bool(relay and relay.paused),
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
        bank = select(discover(cfg.voicebank_root), key)
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
    handled = known | set(ESSENTIALS) | {"voicebank"}
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
        for b in discover(cfg.voicebank_root):
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
        "groups": groups,
        "essentials": [k for k in ESSENTIALS if k in everything],
        "devices": devices,
        "banks": details,
        "ranges": RANGES,
        "seconds": sorted(SECONDS),
        "labels": LABELS,
        "choices": {
            "mode": ["utau", "voice"],
            "capture_mode": ["ptt", "vad"],
            "lyric_mode": ["auto", "native", "japanese"],
            "renderer_backend": ["openutau", "null"],
            "shift_mode": ["semitone", "octave"],
            "pitch_method": ["crepe", "pyin"],
            "crepe_model": ["full", "tiny"],
            "crepe_device": ["cuda", "cpu"],
            "align_device": ["cuda", "cpu"],
            "rvc_f0_method": ["rmvpe", "harvest", "crepe", "pm"],
            "rvc_device": ["cuda:0", "cpu"],
            # Multilingual only: the .en models cannot hear Thai or Japanese,
            # and the Language box is where the language is chosen.
            "whisper_model": ["tiny", "base", "small", "medium", "large-v3"],
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

        def do_GET(self):
            route = self.path.split("?")[0].strip("/")
            if route in ("", "index.html"):
                self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif route == "api/config":
                cfg = Config.load()
                data = {f.name: getattr(cfg, f.name) for f in fields(cfg) if f.name not in HIDE}
                self._json({"config": data, "meta": _meta(cfg)})
            elif route == "api/status":
                self._json(controller.status())
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
            route = self.path.split("?")[0].strip("/")
            if route == "api/start":
                try:
                    controller.start()
                    self._json({"ok": True})
                except Exception as exc:  # noqa: BLE001 - report it on the page
                    log.exception("could not start the relay")
                    self._json({"ok": False, "error": str(exc)}, 500)
            elif route == "api/stop":
                controller.stop()
                self._json({"ok": True})
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
                        info = install_voicebank(body, name, Path(controller.cfg.voicebank_root))
                        log.info("Installed voicebank %r (%d samples)", info["name"], info["samples"])
                    else:
                        folder = Path(controller.cfg.rvc_model).parent
                        info = install_rvc_model(body, name, folder)
                        log.info("Installed RVC %s: %s", info["kind"], info["path"])
                        # A .pth is the voice; point the config at it. An index
                        # is an accessory and is only stored.
                        cfg = Config.load()
                        if info["kind"] == "model":
                            cfg.rvc_model = info["path"]
                        else:
                            cfg.rvc_index = info["path"]
                        cfg.save()
                        controller.cfg = cfg
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
                    cfg = Config.load()
                    cfg.voicebank = key
                    cfg.save()
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
                    cfg = Config.load()
                    valid = {f.name for f in fields(cfg)}
                    changed = []
                    for key, value in incoming.items():
                        if key in valid and value is not None:
                            setattr(cfg, key, value)
                            if getattr(controller.cfg, key, None) != value:
                                changed.append(key)
                            # The running relay holds controller.cfg itself and
                            # reads most settings per utterance, so applying in
                            # place takes effect now. Rebinding would not: the
                            # relay would keep the old object, which is why
                            # changing the language mid-run used to do nothing.
                            setattr(controller.cfg, key, value)
                    cfg.save()
                    stale = sorted(set(changed) & LOADED_ONCE) if controller.running else []
                    self._json({"ok": True, "restart": stale})
                except Exception as exc:  # noqa: BLE001
                    self._json({"ok": False, "error": str(exc)}, 400)
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765) -> int:
    """Run the control panel until interrupted."""
    controller = Controller(cfg)
    server = ThreadingHTTPServer((host, port), make_handler(controller))
    url = f"http://{host}:{port}/"
    print(f"Teto Relay control panel: {url}")
    log.info("control panel on %s", url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        controller.stop()
        server.server_close()
    return 0
