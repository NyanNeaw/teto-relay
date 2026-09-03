"""English to Japanese-style pronunciation - "I love you" as "ai rabu yuu".

Why bother, when the English bank covers 407 of 408 CV combinations? Because
coverage is not the problem. English needs coda consonants and clusters, and
CVVC has to join them; Japanese is almost entirely clean CV morae, which
concatenate far more cleanly. Singing English *through Japanese phonology* -
the way a Japanese speaker would say it - plays to what the voicebank does
well, and it is the classic Teto sound.

Everything here targets the 104 standard morae the tandoku bank actually has,
in hiragana. That rules out the katakana normally used for foreign sounds
(ファ ティ ヴ), so those fall back to the nearest native mora: F becomes ハ行,
V becomes バ行, TH becomes サ行 - which is what Japanese does anyway.

`renzokubeta` is a poor target: it is missing every youon (きゃ しゃ ちゃ), so
`tandoku` is the default for this mode.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ARPAbet vowels to the Japanese vowel that stands in for them. Long vowels
# become two morae, which is how Japanese renders them ("you" -> ゆう).
VOWELS: dict[str, str] = {
    "AA": "a",
    "AE": "a",
    "AH": "a",     # unstressed AH is handled as a schwa below
    "AO": "oo",
    "AW": "au",
    "AY": "ai",
    "EH": "e",
    "ER": "aa",
    "EY": "ei",
    "IH": "i",
    "IY": "ii",
    "OW": "ou",
    "OY": "oi",
    "UH": "u",
    "UW": "uu",
}

# A schwa is usually rendered as a short あ, sometimes オ. あ is the safer default.
SCHWA = "a"

# Consonant + vowel, in hiragana, restricted to morae the bank has.
MORA: dict[str, dict[str, str]] = {
    "":   {"a": "あ", "i": "い", "u": "う", "e": "え", "o": "お"},
    "k":  {"a": "か", "i": "き", "u": "く", "e": "け", "o": "こ"},
    "g":  {"a": "が", "i": "ぎ", "u": "ぐ", "e": "げ", "o": "ご"},
    "s":  {"a": "さ", "i": "し", "u": "す", "e": "せ", "o": "そ"},
    "z":  {"a": "ざ", "i": "じ", "u": "ず", "e": "ぜ", "o": "ぞ"},
    "t":  {"a": "た", "i": "ち", "u": "つ", "e": "て", "o": "と"},
    "d":  {"a": "だ", "i": "ぢ", "u": "づ", "e": "で", "o": "ど"},
    "n":  {"a": "な", "i": "に", "u": "ぬ", "e": "ね", "o": "の"},
    "h":  {"a": "は", "i": "ひ", "u": "ふ", "e": "へ", "o": "ほ"},
    "b":  {"a": "ば", "i": "び", "u": "ぶ", "e": "べ", "o": "ぼ"},
    "p":  {"a": "ぱ", "i": "ぴ", "u": "ぷ", "e": "ぺ", "o": "ぽ"},
    "m":  {"a": "ま", "i": "み", "u": "む", "e": "め", "o": "も"},
    "y":  {"a": "や", "u": "ゆ", "o": "よ", "i": "い", "e": "いえ"},
    "r":  {"a": "ら", "i": "り", "u": "る", "e": "れ", "o": "ろ"},
    "w":  {"a": "わ", "i": "うい", "u": "う", "e": "うえ", "o": "を"},
    # Palatalised series, used before back vowels.
    # しぇ ちぇ じぇ are not in the bank, so those become two morae.
    "sh": {"a": "しゃ", "i": "し", "u": "しゅ", "e": "しえ", "o": "しょ"},
    "ch": {"a": "ちゃ", "i": "ち", "u": "ちゅ", "e": "ちえ", "o": "ちょ"},
    "j":  {"a": "じゃ", "i": "じ", "u": "じゅ", "e": "じえ", "o": "じょ"},
}

# Palatalised (youon) forms: consonant + Y + vowel is one mora, not two.
# Without this, "computer" came out かむぷゆうたあ instead of こんぴゅうたあ.
YOUON: dict[str, dict[str, str]] = {
    "k": {"a": "きゃ", "u": "きゅ", "o": "きょ", "i": "き", "e": "きえ"},
    "g": {"a": "ぎゃ", "u": "ぎゅ", "o": "ぎょ", "i": "ぎ", "e": "ぎえ"},
    "n": {"a": "にゃ", "u": "にゅ", "o": "にょ", "i": "に", "e": "にえ"},
    "h": {"a": "ひゃ", "u": "ひゅ", "o": "ひょ", "i": "ひ", "e": "ひえ"},
    "b": {"a": "びゃ", "u": "びゅ", "o": "びょ", "i": "び", "e": "びえ"},
    "p": {"a": "ぴゃ", "u": "ぴゅ", "o": "ぴょ", "i": "ぴ", "e": "ぴえ"},
    "m": {"a": "みゃ", "u": "みゅ", "o": "みょ", "i": "み", "e": "みえ"},
    "r": {"a": "りゃ", "u": "りゅ", "o": "りょ", "i": "り", "e": "りえ"},
    "s": {"a": "しゃ", "u": "しゅ", "o": "しょ", "i": "し", "e": "しえ"},
    "z": {"a": "じゃ", "u": "じゅ", "o": "じょ", "i": "じ", "e": "じえ"},
    "t": {"a": "ちゃ", "u": "ちゅ", "o": "ちょ", "i": "ち", "e": "ちえ"},
    "d": {"a": "ぢゃ", "u": "ぢゅ", "o": "ぢょ", "i": "ぢ", "e": "ぢえ"},
}

# ARPAbet consonant to the Japanese consonant series that carries it. English
# sounds Japanese lacks are mapped the way Japanese borrows them.
CONSONANTS: dict[str, str] = {
    "P": "p", "B": "b", "T": "t", "D": "d", "K": "k", "G": "g",
    "M": "m", "N": "n", "NG": "n",
    "F": "h",    # ファ is unavailable, so F joins ハ行
    "V": "b",    # ヴ is unavailable
    "TH": "s",   # think -> シンク
    "DH": "z",   # this  -> ジス
    "S": "s", "Z": "z",
    "SH": "sh", "ZH": "j",
    "CH": "ch", "JH": "j",
    "HH": "h",
    "L": "r",    # Japanese has no L
    "R": "r",
    "W": "w", "Y": "y",
}

# The vowel inserted after a consonant that has none. Japanese cannot end a
# syllable on most consonants, so one is added - "u" usually, "o" after t/d.
EPENTHETIC: dict[str, str] = {"t": "o", "d": "o", "ch": "i", "j": "i", "sh": "i"}
DEFAULT_EPENTHESIS = "u"


def _strip_stress(phone: str) -> tuple[str, str]:
    token = phone.strip().upper()
    if token and token[-1].isdigit():
        return token[:-1], token[-1]
    return token, ""


def _mora(consonant: str, vowel: str) -> str:
    """One consonant plus one vowel, falling back if the pair does not exist."""
    table = MORA.get(consonant, MORA[""])
    if vowel in table:
        return table[vowel]
    # Palatalised series have no /i/+back-vowel forms; approximate.
    return table.get("u", MORA[""].get(vowel, ""))


def _is_vowel(base: str | None) -> bool:
    return base in VOWELS or base == "AH"


def _schwa_vowel(phones: list[str], index: int) -> str:
    """Which Japanese vowel stands in for the unstressed schwa at `index`.

    あ is the usual answer, but two contexts reliably want something else, and
    both were audible:

    * **Before a syllable-closing nasal** - the "com-"/"con-" prefix. Japanese
      writes コン, not カン, so "computer" is こんぴゅうたあ. It was かんぴゅうたあ.
    * **Before a word-final L or R** - the "-tle"/"-ple"/"-ful" endings, where
      English has no real vowel and Japanese inserts one to carry the
      consonant. It should be the same vowel epenthesis would supply, so
      "little" is りとる (リトル) and "people" ぴいぷる (ピープル); they were
      りたる and ぴいぱる.
    """
    following = _strip_stress(phones[index + 1])[0] if index + 1 < len(phones) else None
    after = _strip_stress(phones[index + 2])[0] if index + 2 < len(phones) else None

    if following in ("M", "N", "NG") and after is not None and not _is_vowel(after):
        return "o"

    if following in ("L", "R") and index + 2 == len(phones):
        previous = _strip_stress(phones[index - 1])[0] if index else None
        series = CONSONANTS.get(previous) if previous else None
        if series:
            return EPENTHETIC.get(series, DEFAULT_EPENTHESIS)

    return SCHWA


def _is_r_coloured(phones: list[str], index: int) -> bool:
    """Whether the vowel at `index` is followed by an r that colours it.

    "car" and "sorry" both have AA R, but the r in "sorry" starts the next
    syllable (so-rry) while the r in "car" merely lengthens the vowel. An r
    with a vowel after it is an onset, not r-colouring.
    """
    if index + 1 >= len(phones) or _strip_stress(phones[index + 1])[0] != "R":
        return False
    after = _strip_stress(phones[index + 2])[0] if index + 2 < len(phones) else None
    return not _is_vowel(after)


def _vowel_string(phones: list[str], index: int, spelling: str = "") -> str:
    """The Japanese vowel(s) for the vowel phone at `index`."""
    base, stress = _strip_stress(phones[index])
    if base == "AH" and stress == "0":
        return _schwa_vowel(phones, index)

    # American English merges the vowel of "hot" and "father" into one phoneme,
    # but Japanese splits them by spelling: <o> is オ and <a> is ア. Without
    # this, "not" was なと and "stop" すたぷ rather than ノット and ストップ.
    # Words where the vowel is r-coloured ("car", "part", "harmony") stay ア,
    # which is also what keeps a stray o elsewhere in the word from reaching in.
    if base == "AA" and "o" in spelling and not _is_r_coloured(phones, index):
        return "o"

    return VOWELS.get(base, "a")


_BARE_VOWELS = frozenset(MORA[""].values())


def _collapse_redundant(morae: list[tuple[str, bool, int]]) -> list[str]:
    """Drop repeated morae that no Japanese speaker would pronounce twice.

    Each entry is (mora, came-from-epenthesis, index of the phone that produced
    it). Two kinds of repeat are dropped:

    * **Epenthetic twins.** Every consonant in a cluster Japanese cannot end on
      acquires its own vowel, so two similar consonants doubled the mora:
      "months" (M AH1 N TH S) came out まんすす and "clothes" くろうずず.
      Japanese writes one - マンス, クローズ.
    * **A bare vowel repeated across a phone boundary.** A long vowel written by
      one phone (IY -> いい) is real and stays, but when the tail of one phone
      lands on the head of the next the vowel is merely written twice: "we"
      (W IY1) came out ういい because うい already ends in い, and "around"
      (ER0 AW1 N D) came out あああ - three identical morae in a row.
    """
    out: list[str] = []
    previous: tuple[str, bool, int] | None = None
    for mora, epenthetic, phone in morae:
        if previous is not None and mora == previous[0]:
            if epenthetic and previous[1]:
                continue
            if phone != previous[2] and mora in _BARE_VOWELS:
                continue
        out.append(mora)
        previous = (mora, epenthetic, phone)
    return out


def arpabet_to_kana(phones: list[str], spelling: str = "") -> str:
    """Render ARPAbet as Japanese morae. "L AH1 V" -> らぶ.

    `spelling` is the original English word. Pronunciation alone cannot settle
    every vowel - Japanese renders the merged "hot"/"father" vowel by spelling -
    so it is used where the phones are ambiguous. Omitting it is safe; the
    ambiguous cases just fall back to their previous reading.
    """
    # Each entry is one mora, tagged with whether its vowel was inserted rather
    # than spoken and which phone produced it, so the redundant repeats can be
    # collapsed at the end. Some morae are written with two kana (the bank has
    # no ウィ, so W+i is うい), which is why entries are split rather than
    # appended whole - a repeat hiding inside one of those is still a repeat.
    out: list[tuple[str, bool, int]] = []

    def emit(text: str, phone: int, epenthetic: bool = False) -> None:
        for mora in split_morae(text):
            out.append((mora, epenthetic, phone))

    i = 0
    while i < len(phones):
        base, _stress = _strip_stress(phones[i])

        if _is_vowel(base):
            for v in _vowel_string(phones, i, spelling):
                emit(_mora("", v), i)
            i += 1
            continue

        series = CONSONANTS.get(base)
        if series is None:
            i += 1
            continue

        following = phones[i + 1] if i + 1 < len(phones) else None
        next_base = _strip_stress(following)[0] if following else None
        previous_base = _strip_stress(phones[i - 1])[0] if i else None
        next_is_vowel = _is_vowel(next_base)

        # R after a vowel is not pronounced in Japanese; it lengthens the vowel
        # instead. "morning" is モーニング, not モールニング, and "car" is カー,
        # not カル - which is what a word-final R used to produce (かる).
        #
        # Only vowels that came out as a single mora need the extra one: AO and
        # ER are already two (もお, ああ), so lengthening those would overshoot.
        if base == "R" and previous_base in VOWELS and not next_is_vowel:
            previous_vowels = _vowel_string(phones, i - 1, spelling)
            if len(previous_vowels) == 1:
                # Tagged to the R, not to the vowel it lengthens: if the vowel
                # already ended in this mora ("weird", whose うい ends in い),
                # that makes it a cross-phone repeat and it collapses.
                emit(_mora("", previous_vowels[-1]), i)
            i += 1
            continue

        # Consonant + Y + vowel is a single palatalised mora: P Y UW -> ぴゅう.
        #
        # This is checked *before* the nasal rules below, because M and N form
        # youon too (みゅ にゅ). Collapsing them to ん first swallowed the
        # palatalisation and produced a word starting with ん: "music"
        # (M Y UW1 Z IH0 K) came out んゆうじく instead of みゅうじく, and "menu"
        # めんゆう instead of めにゅう.
        if next_base == "Y" and i + 2 < len(phones) and _is_vowel(_strip_stress(phones[i + 2])[0]):
            vowels = _vowel_string(phones, i + 2, spelling)
            table = YOUON.get(series)
            if table is not None:
                emit(table.get(vowels[0], _mora(series, vowels[0])), i + 2)
                for v in vowels[1:]:
                    emit(_mora("", v), i + 2)
                i += 3
                continue

        # N and NG with no vowel after them are ん ("bank" -> ばんく). M is ん
        # only before another consonant ("computer" -> こんぴゅうたあ); at the end
        # of a word it takes a vowel like any other consonant ("name" -> ねいむ).
        if base in ("N", "NG") and not next_is_vowel:
            emit("ん", i)
            i += 1
            continue
        if base == "M" and following is not None and not next_is_vowel:
            emit("ん", i)
            i += 1
            continue

        if next_is_vowel:
            vowels = _vowel_string(phones, i + 1, spelling)
            head = _mora(series, vowels[0])
            emit(head, i + 1)
            tail = vowels[1:]
            # A CV the bank cannot write is approximated with two morae (W+i is
            # うい), and that approximation already ends in the vowel about to be
            # lengthened - so "we" (W IY1) came out ういい. Drop the duplicate.
            if tail and split_morae(head)[-1] == _mora("", tail[0]):
                tail = tail[1:]
            for v in tail:  # long vowels trail as their own mora
                emit(_mora("", v), i + 1)
            i += 2
        else:
            # No vowel follows, so Japanese supplies one.
            emit(_mora(series, EPENTHETIC.get(series, DEFAULT_EPENTHESIS)), i, epenthetic=True)
            i += 1

    return "".join(_collapse_redundant(out))


# Words that are already Japanese. cmudict has no entry for them, and running
# them through English phonemes would be wrong anyway - "teto" is てと, not a
# transliteration of an English reading.
NATIVE: dict[str, str] = {
    "teto": "てと",
    "kasane": "かさね",
    "miku": "みく",
    "hatsune": "はつね",
    "utau": "うたう",
    "vocaloid": "ぼかろいど",
    "vocalo": "ぼかろ",
    "kawaii": "かわいい",
    "arigato": "ありがと",
    "konnichiwa": "こんにちわ",
    "sayonara": "さよなら",
    "senpai": "せんぱい",
    "baka": "ばか",
    "neko": "ねこ",
}

_DICT = None


def english_to_kana(word: str) -> str | None:
    """Look a word up and render it as morae, or None if it is unknown."""
    global _DICT
    key = word.strip().lower()
    if key in NATIVE:
        return NATIVE[key]

    if _DICT is None:
        try:
            import cmudict

            _DICT = cmudict.dict()
        except Exception:
            log.warning("cmudict unavailable; cannot convert to Japanese", exc_info=True)
            _DICT = {}

    entries = _DICT.get(key)
    if not entries:
        return None
    return arpabet_to_kana(list(entries[0]), key)


def split_morae(kana: str) -> list[str]:
    """Split a kana string into morae, keeping youon (しゃ) together."""
    small = "ゃゅょ"
    out: list[str] = []
    for ch in kana:
        if ch in small and out:
            out[-1] += ch
        else:
            out.append(ch)
    return out
