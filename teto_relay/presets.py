"""Style presets: a saved way of singing, in a small file people can share.

A preset is one JSON file, ``<name>.tetostyle``::

    {"format": 1, "name": "Diva", "author": "...", "description": "...",
     "settings": {"singing_style": "sung", "vibrato_depth_cents": 70, ...}}

Its settings are the ones that only change how she sings (`SETTINGS`): the
style, the key, how the melody moves, vibrato, the held last note, the doubled
voice. Never a device, a path, a key on the keyboard or the language: a preset
made on one PC must be safe to open on another, so anything not on the list is
left out when a preset is written and ignored when one is read.

A preset that is read is checked with the same checks as the settings file
(teto_relay.config): a file that is not a preset, or has a value out of range,
is refused with a reason (`PresetError`) and nothing changes.

Four presets are built in (`BUILTIN`); the ones you save or import are kept in
the data folder's ``styles`` directory.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import paths
from .config import Config, ConfigError, coerce
from .errors import TetoRelayError

log = logging.getLogger(__name__)

FORMAT = 1
EXTENSION = ".tetostyle"
#: A preset is a few hundred bytes; anything this big is something else.
MAX_BYTES = 64_000
MAX_NAME, MAX_AUTHOR, MAX_DESCRIPTION = 60, 60, 300

#: What a preset may hold: the settings that only change how she sings -
#: the melody, the vibrato, her tone, and the effects on her voice.
SETTINGS: tuple[str, ...] = (
    "singing_style", "sing_source", "scale", "scale_key", "sung_melody_range", "sung_contour_amount",
    "melody_step", "melody_min_note_seconds",
    "vibrato_min_seconds", "vibrato_depth_cents", "vibrato_period_ms", "final_hold_seconds",
    "double_voice", "double_when", "legato", "expressive",
    "emit_contour", "contour_range_cents", "contour_smooth_ms",
    "tone_gender", "tone_tension", "tone_breath", "tone_voicing",
    "note_scoop_cents", "note_fall_cents", "consonant_speed", "note_attack", "note_release",
    "fx_octave", "fx_bright", "fx_drive", "fx_growl", "fx_growl_hz", "fx_ring", "fx_crush",
    "fx_band", "fx_air", "fx_chorus", "fx_reverb", "fx_size",
)


def settings_of(preset: dict) -> dict:
    """Every setting a preset stands for: its own, and for any it does not
    name (one made before effects existed), the default. Choosing an old
    preset then gives its sound, not its sound plus the last one's growl."""
    defaults = Config()
    return {key: preset["settings"].get(key, getattr(defaults, key)) for key in SETTINGS}


class PresetError(TetoRelayError, ValueError):
    """The file is not a preset Teto Relay can use. The message says why."""


#: The base every built-in tuning starts from: her voice as you gave it.
_NATURAL = dict(
    singing_style="speech", sing_source="auto", scale="major", scale_key="auto", sung_melody_range=1.0,
    sung_contour_amount=0.35,
    melody_step=1.5, melody_min_note_seconds=0.14, vibrato_min_seconds=0.35, vibrato_depth_cents=25.0,
    vibrato_period_ms=175.0, final_hold_seconds=0.3, double_voice=0.5, double_when="singing",
    legato=True, expressive=True, emit_contour=True, contour_range_cents=300.0, contour_smooth_ms=60.0,
    tone_gender=0.0, tone_tension=0.0, tone_breath=0.0, tone_voicing=100.0,
    note_scoop_cents=0.0, note_fall_cents=0.0, consonant_speed=100.0, note_attack=100.0, note_release=0.0,
    fx_octave=0.0, fx_bright=0.0, fx_drive=0.0, fx_growl=0.0, fx_growl_hz=70.0, fx_ring=0.0, fx_crush=0.0,
    fx_band=0.0, fx_air=0.0, fx_chorus=0.0, fx_reverb=0.0, fx_size=0.3,
)
def _preset(name: str, description: str, tag: str = "", **changes) -> dict:
    """A built-in tuning: the natural base with `changes`, so each one sets
    every setting and choosing it always gives the same sound."""
    settings = {**_NATURAL, **changes}
    unknown = set(settings) - set(SETTINGS)
    assert not unknown, unknown
    return {"format": FORMAT, "name": name, "author": "Teto Relay", "description": description,
            "settings": settings, "tag": tag}


_NO_VIBRATO = dict(vibrato_depth_cents=0.0, vibrato_min_seconds=10.0)

#: The built-in tunings, by id: twelve voices that sound clearly unlike each
#: other, from a natural voice to a scream, a robot, a radio and a choir.
BUILTIN: dict[str, dict] = {
    "just-like-you": _preset(
        "Just like you", "She follows your voice exactly: your rhythm, your ups and downs. Good for talking.",
        "Natural"),
    "pop-singer": _preset(
        "Pop singer", "A bright, doubled lead: what you say becomes a tune in a major key.", "Pop",
        singing_style="sung", sung_melody_range=1.4, sung_contour_amount=0.3, vibrato_depth_cents=30.0,
        vibrato_period_ms=170.0, final_hold_seconds=0.5, contour_range_cents=200.0,
        tone_tension=25.0, fx_bright=0.3, fx_air=0.15, fx_reverb=0.12,
        note_scoop_cents=80.0, note_attack=120.0),
    "whisper": _preset(
        "Whisper", "Half voiced, close to the mic, soft as a secret.", "Soft",
        singing_style="sung", scale="pentatonic", sung_melody_range=0.8, vibrato_depth_cents=10.0,
        vibrato_period_ms=220.0, tone_voicing=45.0, tone_breath=70.0, tone_tension=-60.0,
        fx_air=0.5, fx_bright=-0.2, fx_reverb=0.2, fx_size=0.4, double_voice=0.0,
        note_attack=35.0, note_release=40.0, consonant_speed=80.0),
    "metal-scream": _preset(
        "Metal scream", "Pressed, distorted and fried, in a minor key. Every line is a scream.", "Scream",
        singing_style="sung", scale="minor", sung_melody_range=1.8, sung_contour_amount=0.6,
        final_hold_seconds=0.6, tone_tension=100.0, tone_breath=40.0, tone_gender=15.0,
        fx_drive=0.85, fx_growl=0.7, fx_growl_hz=75.0, fx_octave=-0.25, fx_bright=0.4,
        fx_reverb=0.15, fx_size=0.5, double_voice=0.4, note_attack=170.0, note_fall_cents=700.0,
        note_scoop_cents=150.0, **_NO_VIBRATO),
    "robot": _preset(
        "Robot", "Flat machine notes, ring-modulated and crushed. No bends, no vibrato.", "Robot",
        singing_style="sung", scale="chromatic", sung_melody_range=0.6, sung_contour_amount=0.0,
        final_hold_seconds=0.0, legato=False, expressive=False, emit_contour=False, contour_range_cents=0.0,
        double_voice=0.0, double_when="off", tone_tension=20.0, fx_ring=0.6, fx_crush=0.35,
        consonant_speed=160.0, note_attack=180.0, **_NO_VIBRATO),
    "diva": _preset(
        "Diva", "An opera voice: a wide minor melody, a huge slow vibrato, a hall.", "Opera",
        singing_style="sung", scale="minor", sung_melody_range=2.2, sung_contour_amount=0.15,
        vibrato_min_seconds=0.2, vibrato_depth_cents=80.0, vibrato_period_ms=190.0, final_hold_seconds=1.2,
        contour_smooth_ms=90.0, tone_tension=40.0, tone_gender=10.0, double_voice=0.3,
        fx_reverb=0.35, fx_size=0.85, fx_bright=0.1, note_scoop_cents=120.0, consonant_speed=85.0),
    "choir": _preset(
        "Choir", "Many voices in a big room: chorused, doubled, an octave above.", "Choir",
        singing_style="sung", sung_melody_range=1.2, sung_contour_amount=0.25, vibrato_depth_cents=35.0,
        vibrato_period_ms=185.0, final_hold_seconds=0.7, tone_tension=-20.0, tone_breath=15.0,
        double_voice=1.0, fx_chorus=0.8, fx_octave=0.25, fx_reverb=0.45, fx_size=1.0),
    "lo-fi-radio": _preset(
        "Lo-fi radio", "An old telephone speaker: narrow, crunchy, a little hiss.", "Lo-fi",
        singing_style="sung", scale="pentatonic", sung_contour_amount=0.3, tone_tension=-10.0,
        fx_band=0.9, fx_crush=0.2, fx_drive=0.25, fx_reverb=0.1, double_voice=0.0),
    "kawaii": _preset(
        "Kawaii", "Tiny and bright, with a fast cute vibrato and an octave up.", "Chipmunk",
        singing_style="sung", sung_melody_range=1.6, vibrato_depth_cents=40.0, vibrato_period_ms=130.0,
        tone_gender=-60.0, tone_tension=30.0, double_voice=0.3, fx_bright=0.5, fx_octave=0.3),
    "demon": _preset(
        "Demon", "A giant low voice an octave down, with a slow growl, in a cave.", "Demon",
        singing_style="sung", scale="minor", sung_melody_range=0.8, sung_contour_amount=0.2,
        tone_gender=70.0, tone_tension=40.0, tone_breath=20.0, fx_octave=-0.7, fx_growl=0.45,
        fx_growl_hz=45.0, fx_drive=0.4, fx_bright=-0.4, fx_reverb=0.3, fx_size=0.9, double_voice=0.0,
        consonant_speed=70.0, note_fall_cents=500.0, **_NO_VIBRATO),
    "lullaby": _preset(
        "Lullaby", "Slow, soft and round: a gentle five-note melody and long last notes.", "Lullaby",
        singing_style="sung", scale="pentatonic", sung_melody_range=0.8, sung_contour_amount=0.2,
        vibrato_min_seconds=0.5, vibrato_depth_cents=15.0, vibrato_period_ms=230.0, final_hold_seconds=0.9,
        contour_range_cents=150.0, contour_smooth_ms=120.0, tone_tension=-40.0, tone_breath=30.0,
        tone_voicing=85.0, double_voice=0.0, fx_bright=-0.3, fx_reverb=0.25, fx_size=0.6,
        note_attack=45.0, note_release=35.0),
    "rap": _preset(
        "Rap", "Clipped and rhythmic, nearly one note, doubled every line.", "Rap",
        singing_style="sung", scale="chromatic", sung_melody_range=0.5, sung_contour_amount=0.6,
        melody_min_note_seconds=0.1, final_hold_seconds=0.0, legato=False, tone_tension=35.0,
        double_voice=0.6, double_when="always", fx_drive=0.15, fx_bright=0.2, consonant_speed=150.0,
        note_attack=160.0, note_fall_cents=200.0, **_NO_VIBRATO),
}


# ------------------------------------------------------------ reading a preset
def _text(value, limit: int, what: str, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PresetError(f"This preset's {what} is not text.")
    value = " ".join(value.split())
    if required and not value:
        raise PresetError(f"This preset has no {what}.")
    if len(value) > limit:
        raise PresetError(f"This preset's {what} is longer than {limit} characters.")
    return value


def validate(data) -> dict:
    """A preset as Teto Relay keeps it, from what a file held.

    Settings that are not on the list (`SETTINGS`) are dropped without a
    word, as are other keys this version does not know. What is left is
    checked as the settings file is: types (`config.coerce`), then ranges and
    choices (`Config.problems`). Raises PresetError saying what is wrong.
    """
    if not isinstance(data, dict):
        raise PresetError("That is not a preset file.")
    version = data.get("format")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PresetError("That is not a preset file (it does not say which format it is).")
    if version > FORMAT:
        raise PresetError("This preset was made with a newer Teto Relay. Update the app to use it.")
    name = _text(data.get("name"), MAX_NAME, "name", required=True)
    author = _text(data.get("author"), MAX_AUTHOR, "author")
    description = _text(data.get("description"), MAX_DESCRIPTION, "description")
    given = data.get("settings")
    if not isinstance(given, dict):
        raise PresetError("This preset has no settings in it.")
    settings, problems = {}, []
    # Sing and Cover became one Sing: a "cover" preset is Sing on your notes.
    if isinstance(given.get("singing_style"), str) and given["singing_style"].strip().lower() == "cover":
        given = {**given, "singing_style": "sung", "sing_source": given.get("sing_source", "notes")}
    for key in SETTINGS:
        if key in given:
            try:
                settings[key] = coerce(key, given[key])
            except ConfigError as exc:
                problems.append(str(exc))
    if not settings and not problems:
        raise PresetError("This preset has no singing settings in it.")
    if not problems:
        trial = Config()
        for key, value in settings.items():
            setattr(trial, key, value)
        problems = trial.problems()
        settings = {key: getattr(trial, key) for key in settings}  # choices in lower case, as kept
    if problems:
        raise PresetError("This preset has settings Teto Relay can't use:\n" + "\n".join(f"  - {p}" for p in problems))
    return {"format": FORMAT, "name": name, "author": author, "description": description, "settings": settings}


def parse(raw: bytes | str) -> dict:
    """A preset from the contents of a .tetostyle file (`validate`)."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_BYTES:
            raise PresetError("That file is too big to be a preset.")
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise PresetError("That is not a preset file.") from None
    if len(raw) > MAX_BYTES:
        raise PresetError("That file is too big to be a preset.")
    try:
        data = json.loads(raw)
    except ValueError:
        raise PresetError("That is not a preset file.") from None
    return validate(data)


def dumps(preset: dict) -> str:
    """The preset as the text of a .tetostyle file."""
    return json.dumps(preset, ensure_ascii=False, indent=2) + "\n"


def from_config(cfg: Config, name: str, author: str = "", description: str = "") -> dict:
    """The way she sings now, as a preset."""
    return validate({"format": FORMAT, "name": name, "author": author, "description": description,
                     "settings": {key: getattr(cfg, key) for key in SETTINGS}})


def matches(preset: dict, cfg: Config) -> bool:
    """Whether the settings in force are exactly this preset's (with the
    defaults for any it does not name, as `settings_of`)."""
    for key, value in settings_of(preset).items():
        now = getattr(cfg, key)
        if isinstance(value, float) or isinstance(now, float):
            if isinstance(now, bool) or isinstance(value, bool) or abs(float(now) - float(value)) > 1e-6:
                return False
        elif now != value:
            return False
    return True


# ----------------------------------------------------------- the ones you keep
def folder() -> Path:
    return paths.data_dir() / "styles"


def slug(name: str) -> str:
    """A file name for a preset called `name`: plain letters, digits and
    dashes, whatever the name held (it may come from someone else's file)."""
    out = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
    if not out:
        # A name with no Latin letters (ไทย, 日本語): still one name, one file.
        import hashlib

        out = "preset-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return out


def save(preset: dict) -> str:
    """Keep a preset; returns its id. One with the same name is replaced."""
    preset = validate(preset)
    home = folder()
    home.mkdir(parents=True, exist_ok=True)
    key = slug(preset["name"])
    (home / f"{key}{EXTENSION}").write_text(dumps(preset), encoding="utf-8")
    return f"user:{key}"


def _user_file(preset_id: str) -> Path | None:
    kind, _, key = str(preset_id).partition(":")
    if kind != "user" or not re.fullmatch(r"[a-z0-9-]{1,60}", key):
        return None
    path = folder() / f"{key}{EXTENSION}"
    return path if path.is_file() else None


def get(preset_id: str) -> dict:
    """The preset with this id ("builtin:robot", "user:my-style")."""
    kind, _, key = str(preset_id).partition(":")
    if kind == "builtin" and key in BUILTIN:
        return validate(BUILTIN[key])
    path = _user_file(preset_id)
    if path is None:
        raise PresetError("That preset is gone.")
    try:
        return parse(path.read_bytes())
    except OSError as exc:
        raise PresetError(f"That preset could not be read ({exc.strerror or exc}).") from exc


def delete(preset_id: str) -> None:
    """Remove a preset you saved. The built-in ones stay."""
    if str(preset_id).startswith("builtin:"):
        raise PresetError("The built-in presets can't be deleted.")
    path = _user_file(preset_id)
    if path is None:
        raise PresetError("That preset is gone.")
    path.unlink()


def listing(cfg: Config | None = None) -> dict:
    """Every preset for the panel's list, built-in ones first, and which one
    (if any) the settings in force are: {"styles": [...], "active": id}."""
    out = [{"id": f"builtin:{key}", "builtin": True, "tag": p.get("tag", ""),
            **{k: p[k] for k in ("name", "author", "description")}}
           for key, p in BUILTIN.items()]
    presets = {f"builtin:{key}": p for key, p in BUILTIN.items()}
    home = folder()
    mine = []
    for path in sorted(home.glob(f"*{EXTENSION}")) if home.is_dir() else []:
        try:
            preset = parse(path.read_bytes())
        except (PresetError, OSError):
            log.warning("Skipping %s: it is not a preset this version can use", path.name)
            continue
        preset_id = f"user:{path.stem}"
        presets[preset_id] = preset
        mine.append({"id": preset_id, "builtin": False,
                     **{k: preset[k] for k in ("name", "author", "description")}})
    out += sorted(mine, key=lambda p: p["name"].lower())
    active = ""
    if cfg is not None:
        # Yours first: a preset saved from a built-in one is the one you meant.
        for item in sorted(out, key=lambda p: p["builtin"]):
            if matches(presets[item["id"]], cfg):
                active = item["id"]
                break
    return {"styles": out, "active": active, "settings": list(SETTINGS)}


def file_name(preset: dict) -> str:
    """What to call the preset's file when it is exported."""
    return slug(preset["name"]) + EXTENSION
