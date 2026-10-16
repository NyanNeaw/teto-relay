"""Phonetic respellings for words the English dictionary does not know.

`EnXSampaPhonemizer` looks each word up in an English G2P dictionary. Anything
it cannot find - names, Japanese words, invented ones - comes back as a single
"error" phoneme and is sung as silence.

The fix is to spell the word the way it should sound, karaoke-style, using
words the dictionary *does* know. The phonemizer happily accepts several words
inside one note's lyric, so "kasane" becomes "kah sah nay" and sings correctly.

Edit `pronunciations.json` in the project root to add your own. Keys are
matched case-insensitively against whole words.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

from . import paths

#: None means "the user's file in the data folder, else the one shipped with
#: the program" - see `default_path`. Tests point it at a temporary file.
PRONUNCIATIONS_PATH: Path | None = None


def default_path() -> Path:
    if PRONUNCIATIONS_PATH is not None:
        return PRONUNCIATIONS_PATH
    user = paths.data_dir() / "pronunciations.json"
    if user.exists():
        return user
    return paths.resource_dir() / "pronunciations.json"

# Verified against the English bank: each replacement phonemizes without an
# "error" phoneme. Keep additions verified too - a respelling that is itself
# unknown just moves the problem.
# Respellings: approximate a word using words the dictionary knows. Kept as a
# fallback and because they are easy to write by ear.
#
# Note that a word can be *in* the dictionary and still wrong: "teto" resolves
# to "- ti, i t, toU, oU -", which is the English reading TEE-toh, as in
# "veto". Japanese needs the short E of "ten", so it is respelled too.
DEFAULTS: dict[str, str] = {
    "teto": "teh toe",  # TEH-toh, not TEE-toh
    "kasane": "kah sah neh",  # -neh, not -nay
    "miku": "mee koo",
    "hatsune": "hot sue neh",
    "vocaloid": "vocal oid",
    "utau": "oo tah oo",
    "vocalo": "vocal oh",
}

# Phonetic hints: the exact sounds, in X-SAMPA, space separated. These take
# precedence over a respelling because they say precisely what is wanted
# instead of approximating it with other English words - "kasane" is
# k A s A n E, not "kah sah neh" and whatever the dictionary makes of that.
#
# See teto_relay.phonemes for the symbol set this voicebank supports.
PHONEME_HINTS: dict[str, str] = {
    "teto": "t E t oU",
    "kasane": "k A s A n E",
    "miku": "m i k u",
    "hatsune": "h A t s u n E",
    "utau": "u t A u",
    "vocaloid": "v oU k A l OI d",
    "vocalo": "v oU k A l oU",
}


# path -> ((mtime_ns, size), parsed). Both tables are read for every
# utterance; re-parsing an unchanged file each time was wasted work.
_CACHE: dict[Path, tuple[tuple[int, int], dict]] = {}


def _read_user_file(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {}
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("could not read %s; using built-in pronunciations only", path, exc_info=True)
        data = {}
    if not isinstance(data, dict):
        log.warning("%s should contain a JSON object", path)
        data = {}
    _CACHE[path] = (stamp, data)
    return data


def load(path: Path | None = None) -> dict[str, str]:
    """Respellings: defaults merged with the user's file, which wins."""
    table = dict(DEFAULTS)
    data = _read_user_file(path or default_path())
    # Either a flat {word: respelling} file (the original format) or the
    # sectioned form with "respellings" and "phonemes" keys.
    section = data.get("respellings") if "respellings" in data or "phonemes" in data else data
    if isinstance(section, dict):
        for word, respelling in section.items():
            if isinstance(word, str) and isinstance(respelling, str):
                table[word.strip().lower()] = respelling.strip()
    return table


def load_hints(path: Path | None = None) -> dict[str, str]:
    """Phonetic hints: defaults merged with the user's file, which wins."""
    table = dict(PHONEME_HINTS)
    data = _read_user_file(path or default_path())
    section = data.get("phonemes")
    if isinstance(section, dict):
        for word, hint in section.items():
            if isinstance(word, str) and isinstance(hint, str):
                table[word.strip().lower()] = hint.strip()
    return table


def apply(lyric: str, table: dict[str, str]) -> str:
    """Respell `lyric` if the dictionary is known to choke on it."""
    return table.get(lyric.strip().lower(), lyric)


def hint_for(lyric: str, hints: dict[str, str]) -> str | None:
    """The exact phonemes for `lyric`, if we have them."""
    return hints.get(lyric.strip().lower())


def write_default_file(path: Path | None = None) -> Path:
    """Create a starter pronunciations.json if the user has none."""
    path = path or paths.data_dir() / "pronunciations.json"
    if path.exists():
        return path
    starter = {"phonemes": PHONEME_HINTS, "respellings": DEFAULTS}
    path.write_text(json.dumps(starter, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
