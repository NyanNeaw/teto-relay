"""Voicebank discovery - find every Teto bank on disk and describe it.

The three banks that ship with this setup do not share a layout: renzokubeta
keeps character.txt and oto.ini together at its root, while the English and
tandoku banks nest a `重音テト音声ライブラリー` singer root that contains one or
more sub-banks. Discovery therefore looks for singer roots (character.txt) and
then for sub-banks (oto.ini) beneath them, rather than assuming a fixed depth.

UTAU metadata files are almost always Shift-JIS, so every read goes through
`_read_text`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import TetoRelayError

log = logging.getLogger(__name__)

# UTAU tooling predates UTF-8; cp932 is the practical default.
_ENCODINGS = ("utf-8-sig", "cp932", "utf-8", "latin-1")

# Phonemizers that ship in OpenUtau.Plugin.Builtin, keyed by bank flavour.
PHONEMIZERS = {
    "en-cvvc": "OpenUtau.Plugin.Builtin.EnXSampaPhonemizer",
    # ARPAbet aliases ("- hh", "aa", "aa -", "eh r"): OpenUtau's "EN ARPA".
    "en-arpa": "OpenUtau.Plugin.Builtin.ArpasingPhonemizer",
    "ja-vcv": "OpenUtau.Plugin.Builtin.JapaneseVCVPhonemizer",
    "ja-cv": "OpenUtau.Core.DefaultPhonemizer",
}


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


@dataclass(frozen=True)
class OtoEntry:
    """One line of an oto.ini. All timings are milliseconds.

    `cutoff` is UTAU's oddity: positive means "trim this much from the end of
    the file", negative means "the sample ends this many ms after offset".
    """

    wav: str
    alias: str
    offset: float
    consonant: float
    cutoff: float
    preutterance: float
    overlap: float

    def duration_ms(self, file_ms: float) -> float:
        if self.cutoff <= 0:
            return -self.cutoff
        return max(0.0, file_ms - self.offset - self.cutoff)


def parse_oto(path: Path) -> list[OtoEntry]:
    """Parse an oto.ini. Malformed lines are skipped with a debug note."""
    entries: list[OtoEntry] = []
    for lineno, line in enumerate(_read_text(path).splitlines(), 1):
        line = line.strip()
        if not line or "=" not in line:
            continue
        wav, _, rest = line.partition("=")
        parts = rest.split(",")
        if len(parts) < 6:
            log.debug("%s:%d malformed oto line: %r", path, lineno, line)
            continue
        alias = parts[0].strip()
        try:
            nums = [float(p) if p.strip() else 0.0 for p in parts[1:6]]
        except ValueError:
            log.debug("%s:%d non-numeric oto timings: %r", path, lineno, line)
            continue
        entries.append(
            OtoEntry(
                wav=wav.strip(),
                # An empty alias means "use the filename stem".
                alias=alias or Path(wav.strip()).stem,
                offset=nums[0],
                consonant=nums[1],
                cutoff=nums[2],
                preutterance=nums[3],
                overlap=nums[4],
            )
        )
    return entries


@dataclass(frozen=True)
class SubBank:
    """A directory holding one oto.ini plus its samples."""

    name: str
    path: Path
    entry_count: int
    sample_aliases: tuple[str, ...]

    @property
    def oto_path(self) -> Path:
        return self.path / "oto.ini"


@dataclass
class Voicebank:
    key: str  # short selector used in config and the CLI
    name: str  # human name from character.txt
    root: Path  # the singer root OpenUtau should load
    subbanks: list[SubBank] = field(default_factory=list)
    flavour: str = "unknown"  # en-cvvc | en-arpa | ja-vcv | ja-cv | unknown

    @property
    def phonemizer(self) -> str:
        return PHONEMIZERS.get(self.flavour, PHONEMIZERS["ja-cv"])

    @property
    def entry_count(self) -> int:
        return sum(s.entry_count for s in self.subbanks)

    def __str__(self) -> str:
        subs = ", ".join(s.name for s in self.subbanks)
        return f"{self.key:<12} {self.name}  [{self.flavour}, {self.entry_count} entries: {subs}]"


def _character_name(root: Path) -> str:
    char = root / "character.txt"
    if not char.exists():
        return root.name
    for line in _read_text(char).splitlines():
        if line.lower().startswith("name="):
            return line.partition("=")[2].strip()
    return root.name


# Aliases like "a い" (VCV) vs "- あ" / "* あ" (CV with a prefix marker) vs
# "_b{_b{_b-" / "d+ju" (English X-SAMPA).
_XSAMPA_HINT = re.compile(r"[{@+}]|\b(?:d\+|t\+|s\+)")
_KANA_HINT = re.compile(r"[぀-ヿ]")
# A genuine VCV alias is "<vowel> <mora>". The leading token must be a vowel or
# n - a "-" or "*" marker means CV, which is what tripped the first version of
# this heuristic on the tandoku bank.
_VCV_ALIAS = re.compile(r"^[aiueon]\s+\S")
# ARPAbet, as ARPAsing banks spell their aliases: "- hh", "aa", "aa -", "eh r".
_ARPABET = frozenset(
    "aa ae ah ao aw ax ay eh er ey ih iy ow oy uh uw "
    "b ch d dh dx el f g hh jh k l m n ng p q r s sh t th v w y z zh".split()
)


def _looks_arpabet(aliases: list[str]) -> bool:
    """Most aliases are ARPAbet phones, with "-" as the word-edge marker."""
    tokens = [a.split() for a in aliases if a.strip()]
    if not tokens:
        return False
    arpa = sum(1 for t in tokens if all(x == "-" or x in _ARPABET for x in t)
               and any(x in _ARPABET for x in t))
    return arpa > len(tokens) * 0.8


def _detect_flavour(bank_dir: Path, aliases: list[str]) -> str:
    blob = " ".join(aliases[:400])
    path_str = str(bank_dir)
    name_blob = path_str.lower()

    # Before the name check: an ARPAsing bank is often called "English" too,
    # and sung through the X-SAMPA phonemizer it found no samples at all.
    if _looks_arpabet(aliases):
        return "en-arpa"
    if "english" in name_blob or "英語" in path_str or _XSAMPA_HINT.search(blob):
        return "en-cvvc"
    # Explicit naming beats sampling when the bank says what it is.
    if "renzoku" in name_blob or "連続" in path_str:
        return "ja-vcv"
    if "tandoku" in name_blob or "単独" in path_str:
        return "ja-cv"
    if _KANA_HINT.search(blob):
        vcv = sum(1 for a in aliases if _VCV_ALIAS.match(a.strip()))
        return "ja-vcv" if vcv > len(aliases) * 0.15 else "ja-cv"
    return "unknown"


def _make_key(root: Path, taken: set[str]) -> str:
    """Short, stable, human-typable selector derived from the folder name."""
    stem = root.name
    # Prefer the distinctive part of names like "TETO-renzokubeta-091020".
    parts = [p for p in re.split(r"[-_\s]+", stem) if p]
    candidates = [p.lower() for p in parts if not p.isdigit() and p.lower() != "teto"]
    key = candidates[0] if candidates else stem.lower()
    key = re.sub(r"[^a-z0-9]+", "", key) or "bank"
    base, n = key, 2
    while key in taken:
        key = f"{base}{n}"
        n += 1
    return key


# Folders that never hold voicebanks but can hold a great many files: a
# voicebank root that also contains a project checkout, its virtualenv and a
# few GB of model caches used to be walked file by file on every start.
_SKIP_DIRS = {
    "__pycache__", "node_modules", "site-packages", "venv", "env",
    "UCache", "Cache", "Dictionaries", "Plugins", "Resamplers", "Wavtools",
}


def _walk(root: Path, max_depth: int):
    """os.walk that stops `max_depth` folders down and skips junk folders.

    Pruning happens while walking, not after - the old rglob visited every file
    under the root and only then discarded the deep ones.
    """
    import os

    root = Path(root)
    base_depth = len(root.parts)
    for current, dirs, files in os.walk(root):
        here = Path(current)
        depth = len(here.parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        else:
            # Hidden folders (.venv, .git, .cache, .installing-*) and known
            # non-bank folders are not descended into.
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
        yield here, files


def _find_files(root: Path, name: str, max_depth: int) -> list[Path]:
    wanted = name.lower()
    return [folder / f for folder, files in _walk(root, max_depth) for f in files if f.lower() == wanted]


def find_singer_roots(search_root: Path, max_depth: int = 3) -> list[Path]:
    """A singer root has character.txt; failing that, a bare oto.ini directory.

    Only `max_depth` folders below `search_root` are searched.
    """
    roots: list[Path] = []
    seen: set[Path] = set()

    for char in _find_files(search_root, "character.txt", max_depth):
        root = char.parent
        if root not in seen:
            seen.add(root)
            roots.append(root)

    # Banks with no character.txt at all - fall back to oto.ini directories that
    # are not already covered by a discovered singer root.
    for oto in _find_files(search_root, "oto.ini", max_depth):
        root = oto.parent
        if any(root == r or r in root.parents for r in seen):
            continue
        if root not in seen:
            seen.add(root)
            roots.append(root)

    return roots


class VoicebankError(TetoRelayError):
    """No usable voicebank. The message says what to do about it."""


def discover(search_root: Path | str) -> list[Voicebank]:
    """Find every voicebank under `search_root`."""
    search_root = Path(search_root)
    if not search_root.is_dir():
        raise VoicebankError(
            f"The voicebank folder {search_root} does not exist. Set voicebank_root "
            "in the control panel (Setup) to the folder that holds your UTAU "
            "voicebanks, or leave it empty to use the default folder."
        )

    banks: list[Voicebank] = []
    taken: set[str] = set()

    for root in sorted(find_singer_roots(search_root)):
        # Sub-banks sit a level or two inside the singer root.
        oto_dirs = sorted({p.parent for p in _find_files(root, "oto.ini", 3)})
        if not oto_dirs:
            continue

        subbanks: list[SubBank] = []
        all_aliases: list[str] = []
        for d in oto_dirs:
            entries = parse_oto(d / "oto.ini")
            if not entries:
                continue
            aliases = [e.alias for e in entries]
            all_aliases.extend(aliases)
            subbanks.append(
                SubBank(
                    name=d.name if d != root else root.name,
                    path=d,
                    entry_count=len(entries),
                    sample_aliases=tuple(aliases[:12]),
                )
            )
        if not subbanks:
            continue

        key = _make_key(root if root.name != "重音テト音声ライブラリー" else root.parent, taken)
        taken.add(key)
        banks.append(
            Voicebank(
                key=key,
                name=_character_name(root),
                root=root,
                subbanks=subbanks,
                flavour=_detect_flavour(root, all_aliases),
            )
        )

    return banks


def mora_floor(bank: Voicebank, cfg) -> float:
    """The shortest note this bank can actually sing, in seconds.

    Every UTAU sample begins with a preutterance - the consonant run-up before
    the vowel starts. A note shorter than that is all attack and no vowel, which
    is unintelligible however well the rest of the pipeline behaves, so it is a
    property of the recording rather than a matter of taste.

    This bank measures 40 ms at the median and 128 ms at the worst, so the 75th
    percentile is used: low enough to let most measured mora lengths through
    untouched, high enough that the majority of samples still reach their vowel.
    """
    values = sorted(
        entry.preutterance
        for sub in bank.subbanks
        for entry in parse_oto(sub.oto_path)
        if entry.preutterance > 0
    )
    if not values:
        return float(cfg.min_mora_seconds)
    p75 = values[min(len(values) - 1, int(len(values) * 0.75))] / 1000.0
    return max(float(cfg.min_mora_seconds), p75)


def estimate_pitch(bank: Voicebank, cfg, samples: int = 12) -> float:
    """The MIDI note the voicebank was actually recorded at.

    UTAU samples are recorded at one pitch and resampled to whatever you ask
    for. Asking for something far from the original thins the timbre out - the
    English Teto bank sits at C#4, so rendering it at A3 sounds breathy and
    weak, and at A4 sounds strained. Targeting the recorded pitch keeps the
    voice's body intact.

    Measuring takes a few seconds, so the result is cached per bank.
    """
    import json

    from . import paths

    cache_path = paths.host_dir() / "bank_pitch.json"
    cache: dict[str, float] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    # Keyed by folder, not by the short key: a different bank installed under
    # the same key (or the same key rediscovered in another folder) used to
    # reuse the old bank's pitch.
    # "v2": estimates made before sampling was spread across the bank are
    # measured again (see below).
    cache_key = "v2:" + str(Path(bank.root).resolve())
    if cache_key in cache:
        return float(cache[cache_key])

    import numpy as np
    import soundfile as sf

    from . import pitch as pitch_mod

    # Spread the samples across the whole bank. The first few by name can
    # all be one kind: an ARPAsing bank lists its consonants first, and a
    # voiced "b"/"d"/"g" reads an octave low - Miku measured MIDI 54.0 from
    # them against 65.9 from her vowels, so she sang an octave too low.
    found: list[float] = []
    for sub in bank.subbanks:
        wavs = sorted(sub.path.glob("*.wav"))
        if len(wavs) > samples:
            wavs = [wavs[int(i)] for i in np.linspace(0, len(wavs) - 1, samples)]
        for wav in wavs:
            try:
                audio, sr = sf.read(wav, dtype="float32", always_2d=True)
                mono = audio[:, 0]
                if len(mono) < sr // 4:
                    continue
                middle = mono[len(mono) // 4 : 3 * len(mono) // 4]
                track = pitch_mod.track_f0(middle, sr, cfg)
                midi = track.median_midi(0.0, len(middle) / sr)
                if midi is not None:
                    found.append(midi)
            except Exception:  # noqa: BLE001 - a bad sample must not stop discovery
                continue
        if found:
            break

    if not found:
        log.warning("could not measure %s's pitch; assuming C4", bank.key)
        return 60.0

    # The odd sample still reads an octave out; don't let it vote.
    estimate = float(np.median(pitch_mod.correct_octaves(found, cfg)))
    log.info("%s was recorded at about MIDI %.1f", bank.key, estimate)

    cache[cache_key] = estimate
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass
    return estimate


def select_or_default(banks: list[Voicebank], key: str, search_root: Path | str = "") -> Voicebank:
    """Like `select`, but a first start with an unknown key still gets a voice.

    The default key is "english"; someone whose only bank is a Japanese one
    should hear it, with a warning, rather than get a crash.
    """
    if not banks:
        where = f" in {search_root}" if search_root else ""
        raise VoicebankError(
            f"No UTAU voicebanks found{where}. A voicebank is a folder with an "
            "oto.ini and .wav samples. Copy one there, install one from the "
            "control panel, or point voicebank_root at the folder you keep them in."
        )
    try:
        return select(banks, key)
    except ValueError as exc:
        chosen = banks[0]
        log.warning("%s - using %r instead.", exc, chosen.key)
        return chosen


def select(banks: list[Voicebank], key: str) -> Voicebank:
    """Resolve a bank by key, name, or unambiguous prefix."""
    needle = key.casefold()
    for b in banks:
        if b.key.casefold() == needle:
            return b
    partial = [b for b in banks if needle in b.key.casefold() or needle in b.name.casefold()]
    if len(partial) == 1:
        return partial[0]
    available = ", ".join(b.key for b in banks)
    if len(partial) > 1:
        raise ValueError(f"{key!r} is ambiguous; matches {[b.key for b in partial]}")
    raise ValueError(f"no voicebank matching {key!r}. Available: {available}")
