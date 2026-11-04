"""Song lyrics for a speech model that cannot be given them.

Whisper takes the pasted Song lyrics as a prompt (stt.whisper_prompt) and
hears a sung line right. Typhoon ASR, which hears Thai, has no prompt: a
sung Thai line came back with words close to, but not, the lyric. So the
line it heard is looked up in the lyrics instead, letter by letter, allowing
for mishearing (`snap`): when it matches a stretch of the lyrics closely
enough, that stretch's words are what she sings, each timed where its sounds
were heard. Talking that matches nothing is left as it was heard.
"""

from __future__ import annotations

import functools
import logging
import re
import unicodedata

from .stt import Word, clean_lyric

log = logging.getLogger(__name__)

#: How far the heard line may be from the lyric and still be that lyric:
#: edits (letters wrong, missing or extra) per letter heard.
MAX_ERROR = 0.35
#: A line shorter than this (in letters) is too short to tell a lyric from
#: a word that happens to be in it.
MIN_LETTERS = 4
#: The shortest a word is given, as in thai_asr.
STEP = 0.08

_THAI = re.compile(r"[฀-๿]+")


def _letters(text: str) -> str:
    """Only what is pronounced: letters, vowel signs and tone marks, lower case."""
    text = unicodedata.normalize("NFC", text).lower()
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in "LM")


@functools.lru_cache(maxsize=4)
def lyric_words(lyrics: str) -> tuple[str, ...]:
    """The pasted lyrics as words: split on spaces, and Thai runs (written
    without spaces) cut into words. Kept, since the same lyrics are looked up
    for every phrase of a song."""
    out: list[str] = []
    for piece in str(lyrics or "").split():
        rest = 0
        for m in _THAI.finditer(piece):
            if m.start() > rest:
                out.append(piece[rest:m.start()])
            out.extend(_thai_words(m.group()))
            rest = m.end()
        if rest < len(piece):
            out.append(piece[rest:])
    return tuple(w for w in (clean_lyric(w) for w in out) if _letters(w))


def _thai_words(run: str) -> list[str]:
    try:
        from pythainlp.tokenize import word_tokenize

        return [w for w in word_tokenize(run, engine="newmm", keep_whitespace=False) if w.strip()]
    except Exception:  # noqa: BLE001 - without PyThaiNLP, the run is one word
        return [run]


def _align(heard: str, ref: str) -> tuple[int, int, list[int | None]]:
    """Where `heard` best fits inside `ref`, allowing edits: the fewest edits,
    where in `ref` the fit starts, and for each letter of `ref` from there the
    heard letter it lines up with (None where the singer skipped it)."""
    import numpy as np

    h, n = len(heard), len(ref)
    ref_codes = np.array([ord(c) for c in ref], dtype=np.int64)
    steps = np.arange(n + 1)
    # cost[i, j]: edits to match heard[:i] ending at ref[:j]; a fit may start
    # anywhere in ref, so the first row costs nothing. A row at a time, as
    # array operations: a song's lyrics run to thousands of letters, and a
    # loop over every cell added a noticeable wait to each phrase.
    cost = np.zeros((h + 1, n + 1), dtype=np.int32)
    for i in range(1, h + 1):
        prev = cost[i - 1]
        row = np.empty(n + 1, dtype=np.int64)
        row[0] = i
        row[1:] = np.minimum(prev[:-1] + (ref_codes != ord(heard[i - 1])), prev[1:] + 1)
        # A letter of ref skipped: row[j] may also come from row[j-1] + 1.
        row = np.minimum.accumulate(row - steps) + steps
        cost[i] = row
    end = int(np.argmin(cost[h]))
    edits = int(cost[h, end])
    pairs: dict[int, int] = {}
    i, j = h, end
    while i > 0 and j > 0:
        here = cost[i][j]
        if here == cost[i - 1][j - 1] + (heard[i - 1] != ref[j - 1]):
            pairs[j - 1] = i - 1
            i, j = i - 1, j - 1
        elif here == cost[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    start = j
    return edits, start, [pairs.get(k) for k in range(start, end)]


def snap(words: list[Word], lyrics: str) -> list[Word]:
    """`words` as heard, or - when they are a stretch of `lyrics`, allowing
    for mishearing - that stretch's words, each timed by the heard sounds it
    lines up with."""
    if not words or not str(lyrics or "").strip():
        return words
    heard = "".join(_letters(w.text) for w in words)
    if len(heard) < MIN_LETTERS:
        return words
    song = lyric_words(lyrics)
    if not song:
        return words
    # The time each heard letter was sung: its word's span, shared evenly.
    times: list[tuple[float, float]] = []
    for w in words:
        n = len(_letters(w.text))
        for k in range(n):
            step = (w.end - w.start) / n
            times.append((w.start + k * step, w.start + (k + 1) * step))
    ref, owner = "", []
    for index, word in enumerate(song):
        letters = _letters(word)
        ref += letters
        owner.extend([index] * len(letters))
    edits, start, lined = _align(heard, ref)
    if edits > MAX_ERROR * len(heard):
        return words
    # Whole words: the fit is widened to the words it touches.
    first, last = owner[start], owner[start + len(lined) - 1] if lined else owner[start]
    by_word: dict[int, list[int]] = {}
    for offset, h in enumerate(lined):
        if h is not None:
            by_word.setdefault(owner[start + offset], []).append(h)
    out: list[Word] = []
    for index in range(first, last + 1):
        heard_at = by_word.get(index)
        if heard_at:
            begin, end = times[min(heard_at)][0], times[max(heard_at)][1]
        else:
            # Skipped in the singing: squeezed in after the word before.
            begin = out[-1].end if out else words[0].start
            end = begin + STEP
        if out and begin < out[-1].start + STEP:
            begin = out[-1].start + STEP
        out.append(Word(text=song[index], start=round(begin, 3), end=round(max(end, begin + STEP), 3)))
    # Each word runs on to the next, as heard words do (thai_asr.words_from_chunks).
    for i in range(len(out) - 1):
        if out[i + 1].start - out[i].end < 0.3:
            out[i] = Word(text=out[i].text, start=out[i].start, end=max(out[i].start + STEP, out[i + 1].start))
    log.info("Heard as the lyric: %s (was: %s)", " ".join(w.text for w in out), " ".join(w.text for w in words))
    return out
