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
def _split(lyrics: str) -> tuple[tuple[str, ...], tuple[tuple[int, int], ...]]:
    """The lyrics' words, and for each word the span of the pasted text it
    came from (several words share one span: a Thai run, "97")."""
    words: list[str] = []
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"\S+", str(lyrics or "")):
        piece, parts, rest = m.group(), [], 0
        for t in _THAI.finditer(piece):
            if t.start() > rest:
                parts.append(piece[rest:t.start()])
            parts.extend(_thai_words(t.group()))
            rest = t.end()
        if rest < len(piece):
            parts.append(piece[rest:])
        for word in (clean_lyric(p) for p in parts):
            if _letters(word):
                words.append(word)
                spans.append(m.span())
    return tuple(words), tuple(spans)


def lyric_words(lyrics: str) -> tuple[str, ...]:
    """The pasted lyrics as words: split on spaces, and Thai runs (written
    without spaces) cut into words. Kept, since the same lyrics are looked up
    for every phrase of a song."""
    return _split(lyrics)[0]


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


#: Lyric words kept before where the singer is, when the lyrics are cut to
#: what whisper can read (`window`): the line just sung leads into the next.
WINDOW_BEFORE = 12


def _ref(song: tuple[str, ...], first: int) -> tuple[str, list[int]]:
    ref, owner = "", []
    for index in range(first, len(song)):
        letters = _letters(song[index])
        ref += letters
        owner.extend([index] * len(letters))
    return ref, owner


def locate(words: list[Word], lyrics: str, near: int = 0) -> int | None:
    """Where in the lyrics a heard phrase ends: the index of the lyric word
    after it, or None when it is no stretch of the lyrics (talking).

    Looked for from a little before `near` (where the singer was) first, so
    a chorus sung again is found where the song is, not where it was first
    sung; then anywhere, for a singer who went back."""
    heard = "".join(_letters(w.text) for w in words)
    song = lyric_words(lyrics)
    if len(heard) < MIN_LETTERS or not song:
        return None
    starts = (max(0, near - WINDOW_BEFORE), 0) if near > WINDOW_BEFORE else (0,)
    for first in starts:
        ref, owner = _ref(song, first)
        if not ref:
            continue
        edits, start, lined = _align(heard, ref)
        if lined and edits <= MAX_ERROR * len(heard):
            return owner[start + len(lined) - 1] + 1
    return None


def window(lyrics: str, cursor: int, fits=None) -> str:
    """The lyrics from a few words before `cursor` on, as many as `fits(text)`
    allows.

    Whisper reads at most 223 tokens of prompt and keeps the end of a longer
    one, so a whole song pasted in told it only how the song ends: the verse
    being sung was not in what it read. Following the singer, it reads the
    part being sung and what comes next. The text is cut from the lyrics as
    pasted, lines and all."""
    song, spans = _split(lyrics)
    if not song:
        return ""
    start = max(0, min(cursor, len(song)) - WINDOW_BEFORE)
    if start >= len(song) - 1:
        start = 0          # the end of the song: the next phrase may start it again
    text = str(lyrics)
    cut = lambda count: text[spans[start][0]:spans[start + count - 1][1]].strip()  # noqa: E731
    if fits is None:
        return cut(len(song) - start)
    lo, hi = 1, len(song) - start
    while lo < hi:          # the most words that fit
        mid = (lo + hi + 1) // 2
        if fits(cut(mid)):
            lo = mid
        else:
            hi = mid - 1
    return cut(lo)


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
