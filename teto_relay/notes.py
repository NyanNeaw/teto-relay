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


def _dedupe_spans(words: list[Word], cfg) -> list[tuple[Word, tuple[float, float]]]:
    """Space the words out so each note can be sung, keeping the spoken spans.

    Two rules, which sometimes conflict:

    * **Notes must not touch.** Two notes that meet are treated by the
      phonemizer as one legato phrase and the phoneme sequence collapses -
      "hello whats up im duty" came back as 4 phonemes instead of 15.
    * **Notes need room for their syllables.** A CVVC sample carries a
      consonant, a vowel and the transition out of it. How much room depends on
      the word: a flat minimum made one-syllable words drag like held notes.

    Each entry is returned with the *original* spoken span alongside the
    adjusted note, because pitch must be measured over what was actually said.
    Measuring over a stretched note samples the following word's audio too.
    """
    # Callers pass words already sorted by start, and must - anything running
    # alongside them (phonetic hints) is indexed positionally.
    ordered = [w for w in words if w.text]
    out: list[tuple[Word, tuple[float, float]]] = []
    gap = cfg.note_gap_ms / 1000.0

    for w in ordered:
        spoken = (w.start, w.end)
        start = w.start
        if out and start < out[-1][0].end + gap:
            start = out[-1][0].end + gap
        end = max(w.end, start + max(required_seconds(w.text, cfg), MIN_NOTE_SECONDS))
        out.append((Word(text=w.text, start=start, end=end), spoken))

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
) -> list[Note]:
    """Combine words and the F0 track into notes ready for the ustx writer.

    `mora_floor` is the shortest note the voicebank can sing (see
    `voicebank.mora_floor`); it falls back to the configured minimum.
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
    ordered = sorted((w for w in words if w.text), key=lambda x: x.start)
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
            kana = translit.to_kana(w.text, source)
            if not kana:
                log.info("%r is not in the dictionary; leaving it as-is", w.text)
                respelled.append(w)
                word_hints.append(None)
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
            # Only part of the pause is taken, so a real gap survives between
            # words; taking all of it ran them together.
            pause = max(0.0, next_onset - w.end)
            allowance = (w.end - w.start) + pause * cfg.pause_borrow
            step = min(cfg.max_mora_seconds, max(floor, allowance / len(morae)))
            for index, mora in enumerate(morae):
                start = w.start + index * step
                respelled.append(Word(text=mora, start=start, end=start + step))
                word_hints.append(None)
            continue

        # A non-English source on an English bank is romanised and sung from
        # explicit phonemes: the bank's dictionary has never seen "swatdi".
        if source != "en":
            lyric, sounds = translit.to_english(w.text, source)
            if sounds:
                log.info("%s %r -> %r (%s)", source, w.text, lyric, sounds)
                respelled.append(Word(text=lyric, start=w.start, end=w.end))
                word_hints.append(sounds)
                continue

        hint = pron.hint_for(w.text, hints)
        if hint:
            log.info("Using exact phonemes for %r: %s", w.text, hint)
            respelled.append(w)
            word_hints.append(hint)
            continue
        lyric = pron.apply(w.text, table)
        if lyric != w.text:
            log.info("Respelling %r as %r so it can be sung", w.text, lyric)
        respelled.append(Word(text=lyric, start=w.start, end=w.end))
        word_hints.append(None)

    adjusted = _dedupe_spans(respelled, cfg)
    if not adjusted:
        return []

    words = [w for w, _ in adjusted]
    # Pitch comes from what was actually said. A note that had to be lengthened
    # covers audio belonging to the next word, so measuring over it would read
    # the wrong pitch.
    spans = [spoken for _, spoken in adjusted]
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
    for w, (spoken_start, spoken_end), detected, hint in zip(words, spans, filled, word_hints):
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
                shift=octaves,
                phonetic_hint=hint,
            )
        )

    log.info(
        "Built %d note(s): %s",
        len(notes),
        " ".join(f"{n.lyric}@{n.tone}" for n in notes),
    )
    return notes
