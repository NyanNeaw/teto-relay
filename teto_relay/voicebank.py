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

log = logging.getLogger(__name__)

# UTAU tooling predates UTF-8; cp932 is the practical default.
_ENCODINGS = ("utf-8-sig", "cp932", "utf-8", "latin-1")

# Phonemizers that ship in OpenUtau.Plugin.Builtin, keyed by bank flavour.
PHONEMIZERS = {
    "en-cvvc": "OpenUtau.Plugin.Builtin.EnXSampaPhonemizer",
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
    flavour: str = "unknown"  # en-cvvc | ja-vcv | ja-cv | unknown

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


def _detect_flavour(bank_dir: Path, aliases: list[str]) -> str:
    blob = " ".join(aliases[:400])
    path_str = str(bank_dir)
    name_blob = path_str.lower()

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


def _find_singer_roots(search_root: Path, max_depth: int = 3) -> list[Path]:
    """A singer root has character.txt; failing that, a bare oto.ini directory."""
    roots: list[Path] = []
    seen: set[Path] = set()

    for char in search_root.rglob("character.txt"):
        try:
            depth = len(char.relative_to(search_root).parts)
        except ValueError:
            continue
        if depth > max_depth + 1:
            continue
        root = char.parent
        if root not in seen:
            seen.add(root)
            roots.append(root)

    # Banks with no character.txt at all - fall back to oto.ini directories that
    # are not already covered by a discovered singer root.
    for oto in search_root.rglob("oto.ini"):
        root = oto.parent
        if any(root == r or r in root.parents for r in seen):
            continue
        if root not in seen:
            seen.add(root)
            roots.append(root)

    return roots


def discover(search_root: Path | str) -> list[Voicebank]:
    """Find every voicebank under `search_root`."""
    search_root = Path(search_root)
    if not search_root.exists():
        raise FileNotFoundError(f"voicebank_root does not exist: {search_root}")

    banks: list[Voicebank] = []
    taken: set[str] = set()

    for root in sorted(_find_singer_roots(search_root)):
        oto_dirs = sorted({p.parent for p in root.rglob("oto.ini")})
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

    cache_path = Path(cfg.out_dir).parent / ".openutau-host" / "bank_pitch.json"
    cache: dict[str, float] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    if bank.key in cache:
        return float(cache[bank.key])

    import numpy as np
    import soundfile as sf

    from . import pitch as pitch_mod

    found: list[float] = []
    for sub in bank.subbanks:
        for wav in sorted(sub.path.glob("*.wav"))[:samples]:
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

    estimate = float(np.median(found))
    log.info("%s was recorded at about MIDI %.1f", bank.key, estimate)

    cache[bank.key] = estimate
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass
    return estimate


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
