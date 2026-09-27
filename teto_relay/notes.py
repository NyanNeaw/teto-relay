"""Merge transcribed words with detected pitch into singable notes.

This is where the reference implementation's two weakest guesses get replaced:
its duration was `len(word) * 100` ticks and its tone was a random integer in a
four-semitone range. Both now come from measurements of the actual audio.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import japanese as jp
from . import pitch as pitch_mod
from . import pronunciations as pron
from . import translit
from .stt import Word

log = logging.getLogger(__name__)

MIN_NOTE_SECONDS = 0.06  # below this a note is inaudible and confuses the resampler


@dataclass
class Note:
    lyric: str
    start: float  # seconds, relative to the utterance
    end: float
    tone: int  # MIDI note number
    contour: list[tuple[float, float]] = field(default_factory=list)
    detected_midi: float | None = None  # pre-shift, for logging and diagnostics
    shift: int = 0  # octaves applied, so the caller can keep it stable
    # Space-separated X-SAMPA. When set, the phonemizer uses these sounds
    # instead of looking the lyric up in its English dictionary.
    phonetic_hint: str | None = None
    # May touch the previous note (one continuous phrase). Only the `legato`
    # option sets it; otherwise every note keeps a gap before it.
    legato: bool = False
    # Vibrato as OpenUtau describes it (length/in/out in percent, period in
    # ms, depth in cents); None is none. Only the sung style sets it.
    vibrato: dict | None = None
    # When the word was actually said (seconds), which the note's own span
    # may not be - dynamics are measured over this.
    spoken: tuple[float, float] | None = None
    # How long before the note starts the glide from the previous note
    # begins, in ms (teto_relay.performance). None is OpenUtau's usual 40.
    lead_in_ms: float | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


_VOWEL_GROUP = re.compile(r"[aeiouy]+")


_KANA = re.compile(r"[぀-ヿ]")


def syllables(lyric: str) -> int:
    """Rough syllable count - vowel groups, with a silent trailing 'e' ignored.

    Only needs to be good enough to tell "I" from "kasane"; the phonemizer does
    the real work.

    Kana is counted in morae instead: it has no Latin vowels, so the vowel-group
    rule would call every Japanese word one syllable and size its note far too
    short.
    """
    word = lyric.strip().lower()
    if not word:
        return 1
    if _KANA.search(word):
        return max(1, sum(len(jp.split_morae(part)) for part in word.split()))
    total = 0
    for part in word.split():
        groups = len(_VOWEL_GROUP.findall(part))
        if part.endswith("e") and groups > 1 and not part.endswith(("le", "ee", "ye")):
            groups -= 1
        total += max(1, groups)
    return max(1, total)


def required_seconds(lyric: str, cfg) -> float:
    """How long this particular lyric needs in order to be sung clearly.

    Kana is measured per mora rather than per syllable. The two are not
    interchangeable: one English syllable is often three or four morae
    ("strength" is すとれんくす), so charging each of them a whole syllable's
    worth of time stretched every utterance well past the speech it came from
    and, because the floor then caught nearly every note, gave them all an
    identical length. Preserving the measured rhythm is what makes it sound
    like the speaker rather than a metronome.
    """
    if _KANA.search(lyric):
        # Kana notes are already sized from the aligner in `build_notes`; all
        # that is wanted here is the floor, so the measurement survives.
        return syllables(lyric) * cfg.min_mora_seconds
    return max(cfg.min_note_seconds, syllables(lyric) * cfg.seconds_per_syllable)


#: The most a phrase's last note may ring on into the rest after it.
MAX_RELEASE = 0.15
#: Bursts of sound this close together are one word (see _tighten_to_sound).
JOIN_GAP = 0.12
#: Shorter than this is a click or a breath, not a word.
MIN_BLOCK = 0.04


def note_floor(lyric: str, cfg, mora_floor: float | None = None) -> float:
    """The shortest this lyric can be sung: each syllable (or mora) needs its
    consonant and some vowel. Below it a note is all consonant."""
    per = float(mora_floor if mora_floor is not None else cfg.min_mora_seconds)
    return max(MIN_NOTE_SECONDS, syllables(lyric) * per)


def _dedupe_spans(
    words: list[Word], cfg, legato: list[bool] | None = None, mora_floor: float | None = None
) -> list[tuple[Word, tuple[float, float], bool]]:
    """Lay the notes out: when each starts and ends, and which ones touch.

    Returns (note span, spoken span, touches the previous note) per word. The
    spoken span is kept because pitch must be measured over what was said.

    **Onsets are the rhythm, so they stay where they were said.** A note may
    fill the time up to the next word, never push it: giving every note its
    ideal length first (0.22 s a syllable) and shoving the rest along made
    "every night I look up at the stars" arrive 0.6 s late by the last word.
    A later word only moves when the one before cannot be sung any shorter
    (`note_floor`).

    **Phrases are sung connected, rests are kept.** With `legato`, a word said
    within `phrase_gap_ms` of the previous one touches it, so the voicebank
    joins them as a singer would. A longer pause is a rest: the note before
    it ends where the word ended, plus a short release (`pause_borrow`, at
    most MAX_RELEASE) - it no longer stretches into the silence.

    `legato[i]` joins note i to the previous one regardless (the morae of one
    word).
    """
    flags = legato if legato is not None else [False] * len(words)
    ordered = [(w, flag) for w, flag in zip(words, flags) if w.text]
    gap = cfg.note_gap_ms / 1000.0
    phrase_gap = cfg.phrase_gap_ms / 1000.0
    connect = bool(getattr(cfg, "legato", True))

    starts = [w.start for w, _ in ordered]
    out: list[tuple[Word, tuple[float, float], bool]] = []
    for i, (w, joined) in enumerate(ordered):
        start = starts[i]
        floor = note_floor(w.text, cfg, mora_floor if _KANA.search(w.text) else None)
        want = max(floor, required_seconds(w.text, cfg))
        touches = bool(out) and out[-1][2] is not None and (
            joined or (connect and w.start - ordered[i - 1][0].end < phrase_gap))
        if i + 1 == len(ordered):
            end = max(w.end, start + want)
        else:
            nxt_word, nxt_joined = ordered[i + 1]
            nxt = starts[i + 1]
            next_touches = nxt_joined or (connect and nxt_word.start - w.end < phrase_gap)
            if next_touches:
                end = nxt  # legato: sing right up to the next word
                # A word squeezed below its natural length may borrow a little
                # of the next word's start - whisper's boundaries between
                # quick words are rough, and "good" in "good morning" got 0.11 s
                # and was swallowed. Bounded, so the drift cannot build up.
                if end - start < want:
                    end = min(start + want, nxt + cfg.onset_push_ms / 1000.0)
            else:
                room = nxt - gap
                pause = max(0.0, nxt_word.start - w.end)
                release = min(pause * cfg.pause_borrow, MAX_RELEASE)
                end = min(max(w.end + release, start + want), room)
            end = max(end, start + floor)
            # Only a note that cannot be sung shorter moves the next one.
            if end > nxt - (0.0 if next_touches else gap):
                starts[i + 1] = end + (0.0 if next_touches else gap)
                if next_touches:
                    end = starts[i + 1]
        out.append((Word(text=w.text, start=start, end=end), (w.start, w.end), touches))
    return out


def _sound(track, audio, sample_rate: int):
    """Frame times and whether each 10 ms frame has sound in it.

    From the recording's loudness when there is one - that catches unvoiced
    consonants too - otherwise from the pitch tracker's voicing.
    """
    import numpy as np

    if audio is None or len(audio) < sample_rate // 50:
        return np.asarray(track.times), np.asarray(track.voiced, dtype=bool)
    hop = sample_rate // 100
    frames = len(audio) // hop
    chunk = np.asarray(audio[: frames * hop], dtype=np.float64).reshape(frames, hop)
    db = 20 * np.log10(np.sqrt(np.mean(chunk ** 2, axis=1)) + 1e-9)
    threshold = max(np.percentile(db, 10) + 10.0, np.percentile(db, 99) - 35.0)
    active = db > threshold
    return np.arange(frames) / 100.0, active  # frame start times, as in F0Track


def _tighten_to_sound(words: list[Word], times, active) -> list[Word]:
    """Trim each word's span to where there is actually sound.

    Whisper's word timestamps run into the silence around a word: "that" in
    "I wanted to say ... that I love you" was given 1.32-1.88 s, eating most
    of a 0.7 s pause, so the pause was sung over. The forced aligner fixes
    this but needs 1.6 GB of free RAM; this needs none. Spans only ever
    shrink, and a word with too little sound in it is left alone.
    """
    import numpy as np

    out = []
    for w in words:
        mask = (times >= w.start) & (times < w.end) & active
        idx = np.flatnonzero(mask)
        if idx.size < 3:
            out.append(w)
            continue
        # The word is its last real block of sound. Bursts closer than
        # JOIN_GAP belong together (a stop consonant is silence inside a
        # word). Whisper folds a pause into the *start* of the word after it
        # while its word ends are good, so what comes early in a long span is
        # the previous word's tail or a click: "that" was given 1.90-3.84 s
        # for a word said at 3.62, after a 1.7 s pause with a click in it.
        blocks: list[list[int]] = [[idx[0], idx[0]]]
        for i in idx[1:]:
            if times[i] - times[blocks[-1][1]] <= JOIN_GAP:
                blocks[-1][1] = i
            else:
                blocks.append([i, i])
        real = [b for b in blocks if times[b[1]] - times[b[0]] >= MIN_BLOCK] or blocks
        first, last = real[-1]
        start = max(w.start, float(times[first]))
        end = min(w.end, float(times[last]) + 0.01)  # to the end of the last frame
        out.append(Word(text=w.text, start=start, end=end) if end - start >= 0.05 else w)
    return out


_KANJI = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff々]+$")


def join_kanji_compounds(words: list[Word], max_gap: float = 0.1) -> list[Word]:
    """Rejoin kanji that whisper's word timestamps split apart.

    A kanji compound is read as a whole, not character by character: 明日 is
    あした, but 明 + 日 read separately is めい + にち, which is what was sung.
    Adjacent words made only of kanji, said without a pause, are one word.
    """
    out: list[Word] = []
    for w in words:
        if (out and _KANJI.match(out[-1].text) and _KANJI.match(w.text)
                and w.start - out[-1].end <= max_gap):
            out[-1] = Word(text=out[-1].text + w.text, start=out[-1].start, end=w.end)
        else:
            out.append(w)
    return out


def build_notes(
    words: list[Word],
    track: pitch_mod.F0Track,
    cfg,
    previous_shift: int | None = None,
    target_tone: float | None = None,
    baseline: float | None = None,
    japanese_lyrics: bool = False,
    mora_floor: float | None = None,
    singing_state: dict | None = None,
    audio=None,
    sample_rate: int = 16000,
) -> list[Note]:
    """Combine words and the F0 track into notes ready for the ustx writer.

    `mora_floor` is the shortest note the voicebank can sing (see
    `voicebank.mora_floor`); it falls back to the configured minimum.
    `singing_state` carries the sung style's key between phrases. `audio` is
    the recording the words came from, used to trim their spans to where there
    was sound; without it the pitch track's voicing is used.
    """
    # Exact phonemes beat a respelling, so check for a hint first: "kasane" is
    # k A s A n E rather than an approximation built from other English words.
    # Only fall back to respelling when there is no hint.
    #
    # Respelling happens before the notes are sized, because "kah sah neh" has
    # more syllables than "kasane" and so needs a longer note.
    table = pron.load()
    hints = pron.load_hints()
    source = translit.source_language(cfg)
    respelled: list[Word] = []
    word_hints: list[str | None] = []
    joined: list[bool] = []  # legato: follows the previous note with no gap
    use_legato = bool(getattr(cfg, "legato", True))
    ordered = sorted((w for w in words if w.text), key=lambda x: x.start)
    ordered = _tighten_to_sound(ordered, *_sound(track, audio, sample_rate))
    if japanese_lyrics:
        ordered = join_kanji_compounds(ordered)
    floor = float(mora_floor if mora_floor is not None else cfg.min_mora_seconds)
    # Where the last word may sing to. The F0 track spans the whole chunk, so
    # its final frame is the end of the audio rather than the end of the speech.
    utterance_end = float(track.times[-1]) if getattr(track.times, "size", 0) else 0.0

    for position, w in enumerate(ordered):
        if japanese_lyrics:
            # A Japanese bank sings one mora per note - that is how Japanese
            # UTAU parts are written, and the phonemizer treats a whole
            # multi-mora lyric as one unknown phoneme. So each word expands into
            # several notes, sharing out the time it was spoken over.
            # Hints and respellings are English-specific and do not apply.
            # Converted word by word: a lyric can hold several ("i am" from
            # "I'm", "twenty one" from "21"), and looked up as one string it
            # was never in the dictionary and came out as romanised letters.
            parts = [translit.to_kana(part, source) for part in w.text.split()]
            kana = "".join(p for p in parts if p)
            if not kana:
                log.info("%r is not in the dictionary; leaving it as-is", w.text)
                respelled.append(w)
                word_hints.append(None)
                joined.append(False)
                continue

            morae = jp.split_morae(kana)
            # Lay the morae out from this word's onset up to the *next* word's
            # onset, so they may use the pause that follows it. Squeezing them
            # inside the word itself left 30-80 ms each - under the floor, so
            # every note was rounded to the same length and the rhythm went
            # flat. The onsets stay exactly where the aligner put them, which is
            # the part that is heard as timing; only the release is borrowed
            # from silence that was not being used.
            next_onset = (
                ordered[position + 1].start
                if position + 1 < len(ordered)
                else max(utterance_end, w.end)
            )
            # A word's morae share its own span, plus the gap before the next
            # word when that is too short to be a rest. Borrowing half of every
            # pause stretched the word into the silence after it, which is what
            # leaving a gap sounded like; the release before a rest is added
            # once, by the layout below.
            pause = max(0.0, next_onset - w.end)
            allowance = (w.end - w.start) + (pause if pause < cfg.phrase_gap_ms / 1000.0 else 0.0)
            step = min(cfg.max_mora_seconds, max(floor, allowance / len(morae)))
            for index, mora in enumerate(morae):
                start = w.start + index * step
                respelled.append(Word(text=mora, start=start, end=start + step))
                word_hints.append(None)
                # The morae of one word are sung connected (with `legato`).
                joined.append(use_legato and index > 0)
            continue

        # A non-English source on an English bank is romanised and sung from
        # explicit phonemes: the bank's dictionary has never seen "swatdi".
        if source != "en":
            lyric, sounds = translit.to_english(w.text, source)
            if sounds:
                log.info("%s %r -> %r (%s)", source, w.text, lyric, sounds)
                respelled.append(Word(text=lyric, start=w.start, end=w.end))
                word_hints.append(sounds)
                joined.append(False)
                continue

        hint = pron.hint_for(w.text, hints)
        if hint:
            log.info("Using exact phonemes for %r: %s", w.text, hint)
            respelled.append(w)
            word_hints.append(hint)
            joined.append(False)
            continue
        lyric = pron.apply(w.text, table)
        if lyric != w.text:
            log.info("Respelling %r as %r so it can be sung", w.text, lyric)
        respelled.append(Word(text=lyric, start=w.start, end=w.end))
        word_hints.append(None)
        joined.append(False)

    adjusted = _dedupe_spans(respelled, cfg, joined, floor if japanese_lyrics else None)
    if not adjusted:
        return []

    words = [w for w, _, _ in adjusted]
    joined = [touches for _, _, touches in adjusted]
    # Pitch comes from what was actually said. A note that had to be lengthened
    # covers audio belonging to the next word, so measuring over it would read
    # the wrong pitch.
    spans = [spoken for _, spoken, _ in adjusted]
    raw = pitch_mod.assign_tones(spans, track, cfg)

    voiced_count = sum(1 for t in raw if t is not None)
    if voiced_count == 0:
        log.warning("No voiced frames in this utterance; falling back to tone %d", cfg.default_tone)

    filled = pitch_mod.fill_gaps(raw, cfg)
    # Before anything is derived from these, pull out octave detection errors -
    # a single stray word drags the median and swings the whole shift.
    filled = pitch_mod.correct_octaves(filled, cfg, baseline)
    target = target_tone if target_tone is not None else float(cfg.target_tone or 60)
    octaves = pitch_mod.compute_shift(filled, cfg, target, previous_shift)
    shift = octaves + cfg.transpose
    if shift and octaves != previous_shift:
        log.info(
            "Shifting %+d semitones onto the voicebank's recorded pitch (MIDI %.1f)",
            shift,
            target,
        )

    notes: list[Note] = []
    for w, (spoken_start, spoken_end), detected, hint, legato in zip(
        words, spans, filled, word_hints, joined
    ):
        tone = pitch_mod.clamp_tone(detected + shift, cfg)
        notes.append(
            Note(
                # Timing is the note's, which may have been lengthened...
                lyric=w.text,
                start=w.start,
                end=w.end,
                tone=tone,
                # ...but the inflection is read from what was actually said,
                # and against this word's own pitch before the transposition,
                # so the curve is inflection rather than the shift.
                contour=pitch_mod.contour_points(
                    track, spoken_start, spoken_end, detected, cfg
                ),
                detected_midi=detected,
                spoken=(spoken_start, spoken_end),
                shift=octaves,
                phonetic_hint=hint,
                legato=legato,
            )
        )

    if (getattr(cfg, "singing_style", "speech") or "speech").lower() == "sung":
        from .singing import musicalize

        musicalize(notes, cfg, singing_state)

    log.info(
        "Built %d note(s): %s",
        len(notes),
        " ".join(f"{n.lyric}@{n.tone}" for n in notes),
    )
    return notes
