"""Numbers to words, so they can be sung.

Whisper writes numbers as digits ("2", "10", "5:30", "1st", "3.5", "50%"), and
neither the English phonemizer's dictionary nor cmudict has an entry for a
digit, so every number used to be sung as silence. This spells them out the
way they are usually said. English only, no dependencies.
"""

from __future__ import annotations

import re

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]

_ORDINAL_WORDS = {
    "one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
    "nine": "ninth", "twelve": "twelfth",
}

_HAS_DIGIT = re.compile(r"\d")
_ORDINAL = re.compile(r"^(\d+)(st|nd|rd|th)$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_DECIMAL = re.compile(r"^(\d+)\.(\d+)$")
_INTEGER = re.compile(r"^\d+$")


def _under_thousand(n: int) -> list[str]:
    words: list[str] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        words += [_ONES[hundreds], "hundred"]
    if rest >= 20:
        tens, ones = divmod(rest, 10)
        words.append(_TENS[tens])
        if ones:
            words.append(_ONES[ones])
    elif rest or not words:
        words.append(_ONES[rest])
    return words


def cardinal(n: int) -> str:
    """12 -> "twelve", 1234 -> "one thousand two hundred thirty four"."""
    if n < 0:
        return "minus " + cardinal(-n)
    if n == 0:
        return "zero"
    words: list[str] = []
    for value, name in _SCALES:
        if n >= value:
            count, n = divmod(n, value)
            words += _under_thousand(count) + [name]
    if n:
        words += _under_thousand(n)
    return " ".join(words)


def year(n: int) -> str:
    """1999 -> "nineteen ninety nine", 2024 -> "twenty twenty four"."""
    high, low = divmod(n, 100)
    if low == 0:
        return f"{cardinal(high)} hundred"
    if low < 10:
        return f"{cardinal(high)} oh {cardinal(low)}"
    return f"{cardinal(high)} {cardinal(low)}"


def ordinal(n: int) -> str:
    """3 -> "third", 21 -> "twenty first"."""
    words = cardinal(n).split()
    last = words[-1]
    if last in _ORDINAL_WORDS:
        words[-1] = _ORDINAL_WORDS[last]
    elif last.endswith("y"):
        words[-1] = last[:-1] + "ieth"
    else:
        words[-1] = last + "th"
    return " ".join(words)


def _digits(text: str) -> str:
    return " ".join(_ONES[int(d)] for d in text)


def spell(token: str) -> str:
    """Spell out a transcribed token that contains digits; others unchanged.

    `token` is lower-cased with the surrounding punctuation already removed,
    so "5:30" and "3.5" still carry their inner separators.
    """
    if not _HAS_DIGIT.search(token):
        return token
    text = token.replace(",", "")  # 1,000
    suffix = ""
    if text.startswith("$"):
        text, suffix = text[1:], " dollars"
    if text.endswith("%"):
        text, suffix = text[:-1], " percent"

    if match := _ORDINAL.match(text):
        return ordinal(int(match.group(1))) + suffix
    if match := _TIME.match(text):
        hours, minutes = int(match.group(1)), int(match.group(2))
        if minutes == 0:
            return f"{cardinal(hours)} o clock"  # both words the dictionaries know
        if minutes < 10:
            return f"{cardinal(hours)} oh {cardinal(minutes)}"
        return f"{cardinal(hours)} {cardinal(minutes)}"
    if match := _DECIMAL.match(text):
        return f"{cardinal(int(match.group(1)))} point {_digits(match.group(2))}{suffix}"
    if _INTEGER.match(text):
        n = int(text)
        # A four-digit number said on its own is usually a year.
        if not suffix and "," not in token and (1100 <= n <= 1999 or 2010 <= n <= 2099):
            return year(n)
        if len(text) > 1 and text.startswith("0"):
            return _digits(text)  # "007"
        return cardinal(n) + suffix
    # Mixed letters and digits ("mp3", "4k"): spell the digits where they are.
    spelled = re.sub(r"\d+", lambda m: f" {cardinal(int(m.group()))} ", text)
    return " ".join(spelled.split()) + suffix
