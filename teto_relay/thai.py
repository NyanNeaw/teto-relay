"""Thai pronunciation, for voicebanks that sing Japanese or English.

Thai spelling is not its sound: ทราย is "saai", the vowel of เรียน is written
around the consonant, and a final ก is an unreleased stop. The old route
romanised the spelling and respelled the letters, so ท came out as English
"th" (เธอ -> せ), finals grew vowels (รัก -> らく) and every vowel was short.

Here each word goes through pythainlp's Thai G2P model, which answers with the
sounds - syllable by syllable, with vowel length, the final and the tone:

    ความรัก -> kʰ w aː m ˧ . r a k̚ ˦˥

and those syllables are what the voicebanks are given:

* **Japanese banks** sing morae. A long vowel is two (the second becomes a
  held "+" note), a diphthong is two, a nasal final is ん, a -w/-j final is
  う/い, and an unreleased stop is left out - Japanese has no way to sing it
  that isn't a new syllable, and "ra-ku" for รัก is further from Thai than "ra".
  Consonant clusters (คร, ปล, ขว) get the vowel Japanese inserts.
* **English banks** sing phonemes, so they get the whole syllable, stops
  included, as X-SAMPA (converted to ARPAbet for ARPAsing banks).

Tone is not carried: in speech style the sung pitch follows the voice, which
carries it anyway.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache

from . import japanese as jp

log = logging.getLogger(__name__)

THAI = re.compile(r"[฀-๿]")
_TONE_CHARS = set("˥˦˧˨˩")
_CODAS = {"p̚": "p", "t̚": "t", "k̚": "k", "ʔ": "", "m": "m", "n": "n", "ŋ": "ng", "w": "w", "j": "j"}
_BASE_VOWELS = "aiɯueɛoɔɤə"


@dataclass(frozen=True)
class Syllable:
    onset: tuple[str, ...]  # IPA consonants, () for a glottal onset
    vowel: str              # "a", "ɯ", "ia", "ɯa", "ua", ...
    long: bool
    coda: str               # "", "p", "t", "k", "m", "n", "ng", "w", "j"


def parse(ipa: str) -> list[Syllable]:
    """thaig2p's output ("kʰ w aː m ˧ . r a k̚ ˦˥") as syllables."""
    out: list[Syllable] = []
    for chunk in ipa.split(" . "):
        tokens = [t for t in chunk.split() if t and not set(t) <= _TONE_CHARS]
        onset: list[str] = []
        vowel = ""
        long = False
        coda = ""
        for token in tokens:
            base = token.replace("ː", "").replace("̯", "")
            if base and base[0] in _BASE_VOWELS and len(base) == 1:
                vowel += base
                long = long or "ː" in token or bool(vowel and len(vowel) > 1)
            elif not vowel:
                if token != "ʔ":
                    onset.append(token)
            else:
                coda = _CODAS.get(token, coda)
        if vowel:
            out.append(Syllable(tuple(onset), vowel, long, coda))
    return out


def _looped(result: list[Syllable], text: str) -> bool:
    """Whether the G2P model ran away on `text`.

    It is a small seq2seq network, and on a long input it can repeat itself
    ("ขอบคุณมากครับ" -> kʰun kʰun nun nun nun ...). A Thai syllable takes at
    least two characters in all but a handful of words, and real speech does
    not say one syllable three times running.
    """
    letters = len(THAI.findall(text))
    if len(result) > letters // 2 + 1:
        return True
    return any(result[i] == result[i + 1] == result[i + 2] for i in range(len(result) - 2))


#: Words the G2P model gets wrong, as it would have answered. Common words
#: only - each was heard wrong on a test phrase.
FIXES = {
    "ไทย": "tʰ a j ˧",
    "จริง": "t͡ɕ i ŋ ˧",
    "เท็ตโตะ": "tʰ e t̚ ˨˩ . t o ʔ ˨˩",
    "ประเทศไทย": "p r a ˨˩ . tʰ eː t̚ ˥˩ . tʰ a j ˧",
    "สวัสดีครับ": "s a ˨˩ . w a t̚ ˨˩ . d iː ˧ . kʰ r a p̚ ˦˥",
    "สวัสดีค่ะ": "s a ˨˩ . w a t̚ ˨˩ . d iː ˧ . kʰ a ʔ ˥˩",
}


def _royin(word: str) -> str:
    try:
        from pythainlp.transliterate import romanize

        return romanize(word, engine="royin")
    except Exception:  # noqa: BLE001
        return ""


def _at_least(word: str) -> int:
    """A lower bound on the syllables: vowel groups in the rule-based spelling.

    It misses the vowels Thai does not write (สวัสดี is "swatdi", two groups
    for three syllables) but rarely invents one, so the model answering with
    fewer syllables than this has dropped some ("มหาวิทยาลัย" -> ma-haa-wai).
    """
    return len(re.findall(r"[aeiou]+", _royin(word)))


# Rule-based spellings, for when the model cannot be trusted with a word.
_LATIN_ONSET = [("kh", "kʰ"), ("ph", "pʰ"), ("th", "tʰ"), ("ch", "t͡ɕʰ"), ("ng", "ŋ"),
                ("k", "k"), ("p", "p"), ("t", "t"), ("b", "b"), ("d", "d"), ("s", "s"),
                ("h", "h"), ("m", "m"), ("n", "n"), ("f", "f"), ("r", "r"), ("l", "l"),
                ("w", "w"), ("y", "j")]
_LATIN_VOWEL = [("uea", "ɯa", ""), ("aeo", "ɛ", "w"), ("ia", "ia", ""), ("ua", "ua", ""),
                ("ue", "ɯ", ""), ("oe", "ɤ", ""), ("ae", "ɛ", ""), ("ai", "a", "j"),
                ("ao", "a", "w"), ("ui", "u", "j"), ("oi", "o", "j"), ("io", "i", "w"),
                ("iu", "i", "w"), ("eo", "e", "w"), ("a", "a", ""), ("i", "i", ""),
                ("u", "u", ""), ("e", "e", ""), ("o", "o", "")]
_LATIN_CODA = {"k": "k", "t": "t", "p": "p", "m": "m", "n": "n", "ng": "ng", "w": "w", "y": "j"}


def _from_royin(word: str) -> list[Syllable]:
    text = re.sub(r"[^a-z]", "", _royin(word))
    out: list[Syllable] = []
    i = 0
    while i < len(text):
        onset: list[str] = []
        while i < len(text) and text[i] not in "aeiou":
            for spelling, phone in _LATIN_ONSET:
                if text.startswith(spelling, i):
                    onset.append(phone)
                    i += len(spelling)
                    break
            else:
                i += 1
        if i >= len(text):
            if out and onset:  # trailing consonants close the last syllable
                last = out[-1]
                if not last.coda:
                    coda = "ng" if onset[0] == "ŋ" else onset[0].replace("ʰ", "")
                    out[-1] = Syllable(last.onset, last.vowel, last.long, _LATIN_CODA.get(coda, ""))
            break
        for spelling, vowel, glide in _LATIN_VOWEL:
            if text.startswith(spelling, i):
                i += len(spelling)
                break
        coda = glide
        # One consonant before the next vowel starts that syllable; with
        # two, the first closes this one.
        rest = re.match(r"[^aeiou]+", text[i:])
        if not coda and rest and (len(rest.group()) >= 2 and rest.group()[:2] not in
                                  ("kh", "ph", "th", "ch", "ng") or i + len(rest.group()) == len(text)):
            letters = "ng" if rest.group().startswith("ng") else rest.group()[0]
            if letters in _LATIN_CODA:
                coda = _LATIN_CODA[letters]
                i += len(letters)
        out.append(Syllable(tuple(onset[-2:]), vowel, False, coda))
    return out


@lru_cache(maxsize=4096)
def syllables(word: str) -> tuple[Syllable, ...] | None:
    """The word's syllables, or None if they could not be worked out."""
    if not THAI.search(word):
        return None
    try:
        from pythainlp.transliterate import transliterate
    except Exception:  # noqa: BLE001
        log.warning("pythainlp is unavailable; Thai cannot be pronounced", exc_info=True)
        return None

    def attempt(text: str) -> list[Syllable] | None:
        try:
            result = parse(transliterate(text, engine="thaig2p"))
        except Exception:  # noqa: BLE001 - a model failure falls back below
            log.debug("thaig2p failed on %r", text, exc_info=True)
            return None
        if not result or _looped(result, text):
            return None
        return result

    if word in FIXES:
        return tuple(parse(FIXES[word]))
    floor = _at_least(word)
    whole = attempt(word)
    if whole is not None and len(whole) >= floor:
        return tuple(whole)
    # A piece at a time: dictionary words, then the dictionary's syllables.
    for engine in ("words", "syllables"):
        try:
            if engine == "words":
                from pythainlp.tokenize import word_tokenize

                pieces = word_tokenize(word, engine="newmm", keep_whitespace=False)
            else:
                from pythainlp.tokenize import syllable_tokenize

                pieces = syllable_tokenize(word, engine="dict")
        except Exception:  # noqa: BLE001
            continue
        if len(pieces) < 2:
            continue
        parts = [attempt(piece) for piece in pieces]
        if all(parts) and sum(len(x) for x in parts) >= floor:
            return tuple(syl for part in parts for syl in part)
    fallback = _from_royin(word)
    if fallback:
        log.debug("Thai %r pronounced from its spelling", word)
    return tuple(fallback) or None


# ------------------------------------------------------------------ Japanese
# Thai onsets as the Japanese consonant series that carries them. Aspiration
# is not written in Japanese; ง at the start of a syllable is borrowed as ガ行.
_JA_ONSET = {
    "p": "p", "pʰ": "p", "b": "b", "t": "t", "tʰ": "t", "d": "d",
    "k": "k", "kʰ": "k", "t͡ɕ": "ch", "t͡ɕʰ": "ch", "s": "s", "h": "h",
    "m": "m", "n": "n", "ŋ": "g", "f": "f", "r": "r", "l": "r", "w": "w", "j": "y",
}
# ɤ (เธอ, เลย, เงิน) as あ rather than う. Choices here were scored on 20
# Thai phrases through Teto's tandoku bank (tools/tuning_eval.py, letter
# errors as heard by whisper medium): this mapping 0.627, ɤ as う 0.647.
_JA_VOWEL = {"a": "a", "i": "i", "ɯ": "u", "u": "u", "e": "e", "ɛ": "e",
             "o": "o", "ɔ": "o", "ɤ": "a", "ə": "a"}
#: A syllable ending in an unreleased stop (รัก, มาก, ครับ) ends in this: a
#: rest the length of a mora (notes.REST). Left out, the vowel ran on into
#: the next syllable (0.683); sung as a Japanese-style extra mora (ら-く) it
#: was worse still (0.705).
STOP = "っ"
#: Second consonants of a cluster left out for Japanese banks. None: kept
#: (ครับ as くらっ) was heard better than the spoken "khap" (0.627 vs 0.683).
CLUSTER_DROP: tuple[str, ...] = ()
# Morae the basic table lacks, which the extended kana spell: てぃ for Thai ti,
# ふぁ for fa. voicebank.simpler_mora steps down when a bank lacks one.
_EXTENDED = {
    ("t", "i"): "てぃ", ("t", "u"): "とぅ", ("d", "i"): "でぃ", ("d", "u"): "どぅ",
    ("f", "a"): "ふぁ", ("f", "i"): "ふぃ", ("f", "u"): "ふ", ("f", "e"): "ふぇ", ("f", "o"): "ふぉ",
    ("w", "i"): "うぃ", ("w", "e"): "うぇ", ("w", "o"): "うぉ", ("y", "e"): "いぇ",
    ("ch", "e"): "ちぇ",
}


def _ja_mora(series: str, vowel: str) -> str:
    return _EXTENDED.get((series, vowel)) or jp._mora(series, vowel)


def syllable_kana(s: Syllable) -> str:
    vowels = [_JA_VOWEL.get(v, "a") for v in s.vowel]
    onset = [c for i, c in enumerate(s.onset) if not (i and c in CLUSTER_DROP)]
    series = [_JA_ONSET.get(c, "") for c in onset] or [""]
    morae: list[str] = []
    # A cluster (คร, ปล, ขว): the first consonant takes the vowel Japanese
    # inserts (く), the last one starts the syllable (くら, くわ).
    for c in series[:-1]:
        if c:
            morae.append(jp._mora(c, jp.EPENTHETIC.get(c, jp.DEFAULT_EPENTHESIS)))
    morae.append(_ja_mora(series[-1], vowels[0]))
    # The rest of a diphthong, then the length: เรียน is ri-a-n, มาก is ma-a.
    morae.extend(jp._mora("", v) for v in vowels[1:])
    if s.long and len(vowels) == 1:
        morae.append(jp._mora("", vowels[0]))
    if s.coda in ("m", "n", "ng"):
        morae.append("ん")
    elif s.coda == "w":
        morae.append("お" if vowels[-1] == "a" else "う")
    elif s.coda == "j":
        morae.append("い")
    elif s.coda in ("p", "t", "k"):
        morae.append(STOP)
    return "".join(morae)


def to_kana(word: str) -> str | None:
    found = syllables(word)
    if not found:
        return None
    return "".join(syllable_kana(s) for s in found)


# ------------------------------------------------------------------ English
_XS_ONSET = {
    "p": "p", "pʰ": "p", "b": "b", "t": "t", "tʰ": "t", "d": "d",
    "k": "k", "kʰ": "k", "t͡ɕ": "tS", "t͡ɕʰ": "tS", "s": "s", "h": "h",
    # English has no syllable-initial ng; n is the nearest it can start with.
    "m": "m", "n": "n", "ŋ": "n", "f": "f", "r": "r", "l": "l", "w": "w", "j": "j",
}
_XS_VOWEL = {"a": "A", "i": "i", "ɯ": "u", "u": "u", "e": "E", "ɛ": "{",
             "o": "oU", "ɔ": "O", "ɤ": "V", "ə": "V"}
_XS_CODA = {"p": "p", "t": "t", "k": "k", "m": "m", "n": "n", "ng": "N"}
# Vowel + glide finals English sings as one diphthong.
_XS_GLIDES = {("a", "j"): "aI", ("a", "w"): "aU", ("ɔ", "j"): "OI", ("o", "j"): "OI", ("e", "j"): "eI"}


def syllable_xsampa(s: Syllable) -> list[str]:
    out = [_XS_ONSET[c] for c in s.onset if c in _XS_ONSET]
    last = s.vowel[-1]
    glide = _XS_GLIDES.get((last, s.coda)) if len(s.vowel) == 1 else None
    if glide:
        out.append(glide)
        return out
    out.extend(_XS_VOWEL.get(v, "A") for v in s.vowel[:1])
    if len(s.vowel) > 1:
        out.append("@")  # the -a of เอีย / เอือ / อัว
    if s.coda in _XS_CODA:
        out.append(_XS_CODA[s.coda])
    elif s.coda == "w":
        out.append("u")
    elif s.coda == "j":
        out.append("i")
    return out


def to_xsampa(word: str) -> str | None:
    found = syllables(word)
    if not found:
        return None
    return " ".join(p for s in found for p in syllable_xsampa(s))


def to_latin(word: str) -> str | None:
    """A readable spelling for logs and the panel ("khwaam rak")."""
    found = syllables(word)
    if not found:
        return None
    letters = {"pʰ": "ph", "tʰ": "th", "kʰ": "kh", "t͡ɕ": "ch", "t͡ɕʰ": "ch", "ŋ": "ng", "j": "y"}
    vowel = {"ɯ": "ue", "ɛ": "ae", "ɔ": "o", "ɤ": "oe", "ə": "oe"}
    parts = []
    for s in found:
        v = "".join(vowel.get(x, x) for x in s.vowel)
        if s.long and len(s.vowel) == 1:
            v += v
        coda = {"j": "i", "w": "o" if s.vowel[-1] == "a" else "u"}.get(s.coda, s.coda)
        parts.append("".join(letters.get(c, c) for c in s.onset) + v + coda)
    return "".join(parts)
