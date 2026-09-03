"""Getting other languages into a writing the voicebank can sing.

A voicebank sings one alphabet. The Japanese banks sing morae; the English bank
sings English phonemes. Whatever you speak has to arrive as one of those, so
each source language is routed to the writing its bank wants:

    speech    bank        route
    ------    ----        -----
    english   japanese    cmudict -> morae            (japanese.english_to_kana)
    english   english     as spoken                   (no conversion)
    thai      japanese    romanise -> morae           th -> en -> ja
    thai      english     romanise, with phonemes     th -> en
    japanese  japanese    straight to hiragana        (kanji is read out)
    japanese  english     romanise, with phonemes     ja -> en

Romanisation is the hinge: it is the one writing every source can reach and
that both banks can be driven from. It is lossy - Thai tone is gone, and
"khrappm" is not a spelling anyone would choose - but a bank with 104 morae was
never going to carry Thai tone anyway.
"""

from __future__ import annotations

import logging
import re

from . import japanese as jp

log = logging.getLogger(__name__)

THAI = re.compile(r"[฀-๿]")
KANA_OR_KANJI = re.compile(r"[぀-ヿ一-鿿]")

_thai_ready = None
_kakasi = None


def source_language(cfg) -> str:
    """Which language the speech is in: 'en', 'th' or 'ja'."""
    code = (getattr(cfg, "language", "") or "en").strip().lower()
    if code.startswith("th"):
        return "th"
    if code.startswith("ja") or code.startswith("jp"):
        return "ja"
    return "en"


def looks_thai(text: str) -> bool:
    return bool(THAI.search(text))


def looks_japanese(text: str) -> bool:
    return bool(KANA_OR_KANJI.search(text))


# ------------------------------------------------------------------ romanising
def thai_to_latin(text: str) -> str:
    """Thai script to Latin, one word at a time.

    Thai is written without spaces, so whisper hands back runs of script that
    have to be segmented before they can be romanised at all.
    """
    global _thai_ready
    if _thai_ready is None:
        try:
            from pythainlp.tokenize import word_tokenize
            from pythainlp.transliterate import romanize

            _thai_ready = (word_tokenize, romanize)
        except Exception:
            log.warning("pythainlp is unavailable; Thai cannot be romanised", exc_info=True)
            _thai_ready = ()
    if not _thai_ready:
        return text
    word_tokenize, romanize = _thai_ready
    try:
        parts = [p for p in word_tokenize(text, engine="newmm") if p.strip()]
        return " ".join(romanize(p) for p in parts).strip()
    except Exception:
        log.warning("could not romanise %r", text, exc_info=True)
        return text


def japanese_to_latin(text: str) -> str:
    """Japanese to Hepburn romaji, reading any kanji on the way."""
    kakasi = _load_kakasi()
    if kakasi is None:
        return text
    return " ".join(part["hepburn"] for part in kakasi.convert(text)).strip()


# Which vowel each mora ends on, so a long-vowel mark can be turned into a
# mora the bank actually has.
_VOWEL_OF = {
    "a": "あかがさざただなはばぱまやらわ",
    "i": "いきぎしじちぢにひびぴみり",
    "u": "うくぐすずつづぬふぶぷむゆる",
    "e": "えけげせぜてでねへべぺめれ",
    "o": "おこごそぞとどのほぼぽもよろを",
}
_ENDS_ON = {kana: vowel for vowel, group in _VOWEL_OF.items() for kana in group}
_SMALL_ENDS_ON = {"ゃ": "a", "ゅ": "u", "ょ": "o"}


def expand_long_vowels(kana: str) -> str:
    """Make a kana string singable by a voicebank.

    Two characters are written in Japanese but are not morae, so no bank has a
    sample for either, and both were becoming silent notes:

    * **ー**, the long-vowel mark. ラーミング reads らーみんぐ; Japanese sings
      the held vowel out, so it becomes あ/い/う/え/お to match what it holds.
    * **っ**, the sokuon. It is a held stop before the next consonant, not a
      sound of its own - 絶対 is ぜったい, and the っ has no sample anywhere. It
      is dropped, which loses the gemination but keeps the word singing.
    """
    out: list[str] = []
    for char in kana:
        if char in "っッ":
            continue
        if char in "ーｰ―‐-" and out:
            vowel = _SMALL_ENDS_ON.get(out[-1]) or _ENDS_ON.get(out[-1])
            if vowel:
                out.append(jp.MORA[""][vowel])
            continue
        out.append(char)
    return "".join(out)


def japanese_to_kana(text: str) -> str:
    """Japanese to plain hiragana - kanji read out, so the bank can sing it."""
    kakasi = _load_kakasi()
    if kakasi is None:
        return text
    kana = "".join(part["hira"] for part in kakasi.convert(text)).strip()
    return expand_long_vowels(kana)


def _load_kakasi():
    global _kakasi
    if _kakasi is None:
        try:
            import pykakasi

            _kakasi = pykakasi.kakasi()
        except Exception:
            log.warning("pykakasi is unavailable; Japanese cannot be read", exc_info=True)
            _kakasi = False
    return _kakasi or None


# ------------------------------------------------- latin letters to Japanese
# Latin spellings to the Japanese consonant series that carries them. Digraphs
# are matched first, so "kh" is k and not k+h.
_SERIES = [
    ("kh", "k"), ("ph", "p"), ("th", "s"), ("ch", "ch"), ("sh", "sh"),
    # No "ny" here: it is n plus a y-glide, and matching it as a digraph eats
    # the glide and turns "nyu" into ぬ instead of にゅ.
    ("ts", "t"), ("ng", "N"),
    ("k", "k"), ("g", "g"), ("s", "s"), ("z", "z"), ("j", "j"),
    ("t", "t"), ("d", "d"), ("n", "n"), ("h", "h"), ("b", "b"), ("p", "p"),
    ("m", "m"), ("y", "y"), ("r", "r"), ("l", "r"), ("w", "w"),
    ("f", "h"), ("v", "b"), ("c", "k"), ("q", "k"), ("x", "s"),
]
# Latin vowel spellings to Japanese vowels. Longest first.
_VOWELS = [
    ("uea", "ua"), ("iao", "iao"),
    ("ae", "e"), ("oe", "e"), ("ue", "u"), ("eu", "u"), ("oo", "uu"),
    ("ai", "ai"), ("ao", "au"), ("au", "au"), ("ei", "ei"), ("ou", "ou"),
    ("oi", "oi"), ("ia", "ia"), ("ea", "ia"), ("ee", "ii"), ("aa", "aa"),
    ("a", "a"), ("i", "i"), ("u", "u"), ("e", "e"), ("o", "o"),
]


def _split_syllables(word: str) -> list[tuple[str, bool, str]]:
    """Break a latin word into (consonant series, y-glide, vowels) triples.

    Consonants with no vowel after them come back with an empty vowel, which is
    where Japanese inserts one. The glide is only true for a written y between
    consonant and vowel ("kyo"): palatalising every back vowel instead turns
    "phom" into ぴょむ and "khopkhun" into きょぷきゅん.
    """
    text = re.sub(r"[^a-z]", "", word.lower())
    out: list[tuple[str, bool, str]] = []
    i = 0
    while i < len(text):
        series = ""
        for spelling, mapped in _SERIES:
            if text.startswith(spelling, i):
                series, i = mapped, i + len(spelling)
                break
        glide = False
        if series and text.startswith("y", i) and i + 1 < len(text):
            glide, i = True, i + 1
        vowels = ""
        for spelling, mapped in _VOWELS:
            if text.startswith(spelling, i):
                vowels, i = mapped, i + len(spelling)
                break
        if not series and not vowels:
            i += 1
            continue
        out.append((series, glide, vowels))
    return out


def latin_to_kana(word: str) -> str:
    """Any latin spelling as Japanese morae.

    Romanised Thai carries clusters Japanese cannot hold - "swatdi", "khrappm" -
    so the same rule the English path uses applies here: a consonant with no
    vowel of its own gets the one Japanese would insert.
    """
    morae: list[str] = []
    for series, glide, vowels in _split_syllables(word):
        if series == "N":  # ng
            morae.append("ん")
            morae.extend(jp._mora("", v) for v in vowels)
        elif not series:
            morae.extend(jp._mora("", v) for v in vowels)
        elif not vowels:
            # A bare consonant: n closes the syllable, everything else takes
            # the inserted vowel.
            morae.append(
                "ん" if series == "n"
                else jp._mora(series, jp.EPENTHETIC.get(series, jp.DEFAULT_EPENTHESIS))
            )
        else:
            table = jp.YOUON.get(series) if glide else None
            head = jp._mora(series, vowels[0]) if table is None else table.get(
                vowels[0], jp._mora(series, vowels[0])
            )
            morae.append(head)
            morae.extend(jp._mora("", v) for v in vowels[1:])
    # Romanisation doubles letters that Japanese writes once ("phenng" -> ぺんん).
    out: list[str] = []
    for mora in morae:
        if mora and not (out and mora == out[-1] and mora == "ん"):
            out.append(mora)
    return "".join(out)


# X-SAMPA for the English bank, so a romanised word is sung as its sounds
# rather than guessed at by an English dictionary that has never seen it.
_XS_CONSONANT = {
    "k": "k", "g": "g", "s": "s", "z": "z", "t": "t", "d": "d", "n": "n",
    "h": "h", "b": "b", "p": "p", "m": "m", "y": "j", "r": "r", "w": "w",
    "sh": "S", "ch": "tS", "j": "dZ", "N": "N",
}
_XS_VOWEL = {"a": "A", "i": "i", "u": "u", "e": "E", "o": "oU"}


def latin_to_xsampa(word: str) -> str:
    """Space-separated X-SAMPA for a romanised word."""
    out: list[str] = []
    for series, glide, vowels in _split_syllables(word):
        if series:
            out.append(_XS_CONSONANT.get(series, series))
        if glide:
            out.append("j")
        for v in vowels:
            out.append(_XS_VOWEL.get(v, "A"))
        if series and not vowels and series not in ("n", "N"):
            out.append("u")  # the vowel that carries a stranded consonant
    return " ".join(out)


# ------------------------------------------------------------------ the routes
def to_kana(word: str, source: str) -> str | None:
    """The word as Japanese morae, whatever it started as."""
    if source == "ja" or looks_japanese(word):
        kana = japanese_to_kana(word)
        # pykakasi passes Latin straight through, so a word that comes back
        # unconverted was never Japanese - the language setting was just wrong
        # about this one. Returning it here used to hand "hello" to the mora
        # splitter, which cut it into h-e-l-l-o and asked a Japanese bank to
        # sing five letters it has no samples for. Fall through and romanise.
        if kana and looks_japanese(kana):
            return kana
    if source == "th" or looks_thai(word):
        latin = thai_to_latin(word) if looks_thai(word) else word
        return latin_to_kana(latin) or None
    # English keeps the dictionary route, which knows how a word is *said*
    # rather than how it is spelt: "you" is ゆう, not よう.
    kana = jp.english_to_kana(word)
    if kana:
        return kana
    return latin_to_kana(word) or None


def to_english(word: str, source: str) -> tuple[str, str | None]:
    """The word for an English bank: (lyric, X-SAMPA hint or None).

    A romanised Thai or Japanese word means nothing to the English dictionary,
    so it is sung from an explicit phoneme string instead of being looked up.
    """
    if source == "th" or looks_thai(word):
        latin = thai_to_latin(word) if looks_thai(word) else word
        return latin, latin_to_xsampa(latin) or None
    if source == "ja" or looks_japanese(word):
        latin = japanese_to_latin(word) if looks_japanese(word) else word
        return latin, latin_to_xsampa(latin) or None
    return word, None
