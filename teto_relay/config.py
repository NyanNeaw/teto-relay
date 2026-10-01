"""Central configuration for Teto Relay.

Every tunable lives here so there is exactly one place to look when the pipeline
misbehaves. Values can be overridden by a JSON file (see `Config.load`), which
the control panel writes; it lives in `paths.data_dir()`.
"""

from __future__ import annotations

import json
import logging
import typing
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import paths
from .errors import TetoRelayError

log = logging.getLogger(__name__)

PROJECT_ROOT = paths.SOURCE_ROOT
#: None means `paths.config_path()`, looked up when used. Tests point it at a
#: temporary file.
CONFIG_PATH: Path | None = None


class ConfigError(TetoRelayError, ValueError):
    """The configuration cannot be used. The message says what to change."""


@dataclass
class Config:
    # ------------------------------------------------------------------ audio
    # Device names are matched as case-insensitive substrings, so
    # "CABLE Input" matches "CABLE Input (VB-Audio Virtual Cable)".
    input_device: str | None = None  # None -> system default microphone
    output_device: str = "CABLE Input"
    # Capture, STT and pitch tracking all run at 16 kHz; whisper resamples to
    # 16 kHz internally and pyin has no need for more.
    sample_rate: int = 16000

    # ------------------------------------------------- stage 1: mic chunking
    # "ptt"  - hold a key to record, release to process. Default, because
    #          pause-splitting cuts mid-sentence and hands whisper short noisy
    #          fragments, which it answers with confident nonsense.
    # "vad"  - automatic, split on silence.
    capture_mode: str = "ptt"
    ptt_key: str = "f8"  # see teto_relay.hotkey.parse_key for accepted names
    frame_ms: int = 20
    silence_ms: int = 400  # pause length that closes an utterance
    min_chunk_ms: int = 300  # shorter than this is a cough, not a phrase
    max_chunk_ms: int = 10_000  # hard stop so one long rant cannot stall us
    preroll_ms: int = 200  # audio kept from *before* onset, so we do not clip
    rms_threshold: float = 0.015  # on float32 samples in [-1, 1]
    auto_calibrate: bool = True  # measure room noise at startup
    calibrate_ms: int = 800
    calibrate_margin: float = 3.0  # threshold = noise floor * margin

    # ------------------------------------------------------ stage 2: whisper
    # tiny < base < small < medium - bigger is more accurate and slower. The
    # multilingual models, not the .en ones: on the target user's accented
    # English, base.en got 42% of words wrong (it invents "mm mm mm", "kidding
    # I am not") against 5.8% for base and 2.8% for small, at the same speed.
    # small on a GPU (0.4 s) is the best choice when there is one.
    whisper_model: str = "base"
    # "auto": the GPU when CUDA works, else the CPU. It was "cpu", so a new
    # install on a PC with an NVIDIA card listened on the CPU (1.1 s a phrase
    # instead of ~0.2 s) until someone found the setting.
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    language: str = "en"
    # With language "th": a Whisper trained on Thai (stt.THAI_MODEL, a 0.5 GB
    # download on first use) instead of whisper_model. It halved the character
    # errors of the standard small model on Thai at the same speed.
    thai_speech_model: bool = True
    # How the program shows its control panel: "app" is a window of its own
    # (Edge or Chrome app mode, with its own taskbar button), "browser" a tab
    # in the default browser.
    panel_window: str = "app"
    beam_size: int = 5
    # Whisper's word timings come from attention and are systematically early -
    # measured at +0.06 to +0.20s per word against a forced aligner, and too
    # long. Both note length and pitch are read from those spans, so the error
    # propagates. Alignment measures them against the audio for about 0.06s per
    # utterance. Model is ~1.2GB, downloaded on first use.
    #
    # Off by default for words: on the target user's accented English it made
    # the singing harder to follow (word error 0.07 -> 0.12, twice), since it
    # aligns by spelling. It helped on native text-to-speech.
    use_alignment: bool = False
    # Only the line between two touching words, moved to where the aligner
    # hears it (align.boundaries) - whisper gave the end of "control" to the
    # "it" after it. English on an English bank; whisper still decides where
    # each phrase starts and stops. Skipped without a CUDA graphics card,
    # where the aligner costs about a second per phrase.
    align_boundaries: bool = True
    # The same aligner, timing each Japanese mora from where its vowel was
    # sung (align.vowel_onsets). On the user's Japanese takes it brought the
    # syllables closer to theirs (Senbonzakura +33%) and cut kana error.
    align_morae: bool = True
    align_device: str = "cuda"  # falls back to cpu automatically
    # Primes whisper's vocabulary. "Teto" is out-of-vocabulary and comes back
    # as "ted oh" or "cassini tito" without it - a bigger model does not fix
    # that, because the problem is an unknown proper noun, not capacity.
    initial_prompt: str = "Kasane Teto, UTAU, vocaloid, voicebank."
    # The words of the song you are about to sing, so they are heard right
    # (stt.whisper_prompt). Paste the part you sing; clear it afterwards.
    lyrics_hint: str = ""
    # Anti-hallucination gates. Whisper answers near-silence with confident
    # nonsense, so segments it is unsure about are discarded rather than sung.
    no_speech_threshold: float = 0.6
    min_avg_logprob: float = -1.0
    compression_ratio_threshold: float = 2.4

    # ------------------------------------------------------- stage 3: pitch
    # "crepe" is a neural tracker on the GPU: measured 0.09s per utterance
    # against pyin's 0.97-3.17s, and far steadier - pyin's variability is what
    # produced the 14s analysis spike seen live. "pyin" stays available and
    # needs no GPU.
    pitch_method: str = "crepe"
    crepe_model: str = "full"  # "full" or "tiny" (tiny is ~3x faster again)
    crepe_device: str = "cuda"  # falls back to cpu automatically
    crepe_voiced_threshold: float = 0.5  # periodicity below this counts as unvoiced
    # 80 Hz rather than 65: a speaking voice around 110 Hz gave pyin room to
    # report half that (55 Hz), landing the word an octave low. Raising the
    # floor removes most of the halving without cutting off real speech.
    f0_min: float = 80.0
    f0_max: float = 1047.0  # C6
    # Anything left is snapped back by correct_octaves.
    fix_octave_errors: bool = True
    octave_snap_cents: float = 350.0  # how near a whole octave counts as an error
    # Clamp only; the shift itself aims at the voicebank's recorded pitch.
    midi_min: int = 48  # C3
    midi_max: int = 84  # C6
    # 0 = measure the bank's recorded pitch and aim there. UTAU samples are
    # recorded at one pitch (the English Teto bank is C#4) and resampling far
    # from it thins the voice out - too low sounds breathy, too high strained.
    target_tone: int = 0
    # "semitone" lands exactly on the recorded pitch. "octave" preserves pitch
    # class but can leave the voice several semitones off it.
    shift_mode: str = "semitone"
    max_shift: int = 36
    # How far the carried-over shift may drift before it is recomputed. This
    # wants to be generous: at 3 semitones, ordinary variation between phrases
    # retriggered it, so speaking at a similar pitch came out several semitones
    # higher next time. Half an octave keeps the shift put and lets your own
    # pitch differences between sentences survive into the singing.
    shift_tolerance: float = 6.0
    default_tone: int = 60  # used when a word has no voiced frames at all
    # With `legato` off, the gap left between words. (Notes that touch used to
    # "collapse" - fewer phonemes than notes - but that was the relay sending
    # touching notes to the phonemizer as one group; fixed, see NOTES.md.)
    note_gap_ms: int = 2
    # A pause at least this long is a rest: the phrase ends and she stops. A
    # shorter one - the ordinary space between words - is sung through.
    phrase_gap_ms: int = 250
    # How far a word too short to sing clearly may push the next word's start
    # later. Onsets are the rhythm, so it is small and never adds up.
    onset_push_ms: int = 60
    # Put each word's vowel on the beat and let the voicebank sing its
    # consonant just before, the way sung parts are written (see
    # notes._vowels_on_the_beat). Off starts notes at the consonant.
    vowel_on_beat: bool = True
    # How long a note needs is a property of the word, not a flat number. A
    # single syllable needs far less room than three, and forcing every short
    # word up to one length made "I" and "a" drag like held notes.
    #
    # Required length = syllables * seconds_per_syllable, with min_note_seconds
    # as the floor. Words you said for longer than that keep their own length.
    min_note_seconds: float = 0.16
    seconds_per_syllable: float = 0.22
    # Japanese mode sings one note per *mora*, and its length is measured, not
    # set by a tempo: each word's morae are laid out from its aligned onset to
    # the *next word's* onset, so they may use the pause that follows. These two
    # only bound that measurement.
    #
    # A tempo number was tried first and was wrong in principle. Whatever it was
    # set to, it overrode 100% of the measured lengths - the morae of a word
    # squeezed inside the word itself come to 30-80 ms each, below any floor
    # short enough to still be singable - so every note came out identical and
    # the result was a metronome. The room has to come from the pauses instead:
    # in a typical utterance 48% of the time is silence between words.
    #
    # The floor is a property of the voicebank, not a preference: a note shorter
    # than a sample's preutterance is all consonant run-up and no vowel. See
    # `voicebank.mora_floor`, which measures it from the oto. This is its lower
    # bound. The cap stops a long pause from inflating the word before it.
    # 0.06 was tried and judged worse by ear. The bank's p75 preutterance is
    # 67 ms and its p90 is 98 ms, so morae that short are mostly consonant
    # run-up with barely any vowel - thin and hard to follow, even though they
    # tracked the speech rhythm faithfully. 0.11 clears the p90, which means it
    # also overrides most measured lengths: evenly-sung morae that reach their
    # vowel beat an accurate rhythm made of half-sounded ones.
    min_mora_seconds: float = 0.11
    max_mora_seconds: float = 0.25
    # How much of a rest the note before it may sing into, as a release - at
    # most MAX_RELEASE (0.15 s) whatever this says. It used to be uncapped, and
    # half of every pause was spent stretching the word before it: leave a gap
    # and the word dragged into it. Rests are rests now.
    pause_borrow: float = 0.5
    # Keep the octave shift steady between utterances so the character's pitch
    # does not jump around; recompute only when the voice drifts out of range.
    stable_shift: bool = True
    # The intra-note pitch curve. Raw speech F0 carries both the intonation you
    # hear and frame-level jitter; the jitter made the voice warble, but
    # dropping the curve entirely made every word monotone. It is now smoothed
    # instead - median filtered to kill pitch spikes, then averaged.
    emit_contour: bool = True
    contour_smooth_ms: float = 60.0  # smoothing window over the F0 track
    contour_points: int = 5  # curve points emitted per note
    # Roughly three semitones each way. Speech inflection inside one word
    # routinely spans that, and clamping tighter flattens the ends of a
    # falling "hello" back into a monotone.
    contour_range_cents: float = 300.0
    # A male speaking voice sits an octave or two below Teto's range. Shifting
    # by whole octaves preserves pitch class and relative melody.
    auto_octave: bool = True
    transpose: int = 0  # extra semitones applied after the octave shift

    # --------------------------------------------- singing (experimental)
    # All off by default: the current pipeline stays the default until these
    # have been listened to on real hardware. See teto_relay/singing.py.
    #
    # "speech" - notes follow your spoken pitch exactly (the original sound).
    # "sung"   - notes snap to a key, hold steadier, long ones get vibrato,
    #            and the last one is held.
    singing_style: str = "speech"
    scale: str = "major"  # major | minor | pentatonic | chromatic
    scale_key: str = "auto"  # "auto", or a note name such as "C", "F#", "Bb"
    sung_contour_amount: float = 0.35  # how much spoken inflection survives
    # How much wider than you spoke the sung melody's intervals are: speech
    # moves within a few semitones, a song much further. 1.0 keeps yours.
    sung_melody_range: float = 1.0
    vibrato_min_seconds: float = 0.35  # notes this long or longer get vibrato
    vibrato_depth_cents: float = 25.0
    vibrato_period_ms: float = 175.0
    final_hold_seconds: float = 0.3  # how much longer the last note is held
    # Sing each phrase connected, the way a singer does: the notes of words
    # said without a pause (shorter than phrase_gap_ms) touch, so the voicebank
    # joins them (VCV / CVVC transitions) instead of starting every word from
    # silence. Off leaves a note_gap_ms gap before every word.
    legato: bool = True
    # Dynamics and breath from how you said it: each word as loud as you said
    # it (compressed), a soft attack and a fade per phrase, a breathier tail
    # as each phrase ends. WORLDLINE-R curves; see teto_relay.performance.
    expressive: bool = True
    # A doubled lead: quiet, slightly detuned and delayed copies under her
    # voice for a fuller sound (teto_relay.performance.double_voice). 0 is off.
    # By default only on phrases that were sung - it thickens a sung line and
    # muddies speech (the user's verdict) - "always" doubles every phrase.
    double_voice: float = 0.5
    double_when: str = "singing"  # off | singing | always

    # -------------------------------------------------------- stage 4: ustx
    # Where the Teto banks live. Discovery walks this for character.txt/oto.ini,
    # so all three banks are found regardless of their differing layouts.
    # Left empty, the usual places are searched (see teto_relay.locate): the
    # `voicebanks` folder beside the config, then OpenUtau's Singers folders.
    voicebank_root: str = ""
    voicebank: str = "english"  # selector key; override per render
    # "native"   - sing the words as they are, through an English bank.
    # "japanese" - convert to Japanese-style pronunciation first ("i love you"
    #              becomes あい らぶ ゆう) and sing through a Japanese bank.
    #              English CVVC has to join complex codas and clusters; Japanese
    #              is almost all clean CV morae, which concatenate better.
    # "auto"     - follow the selected voicebank's flavour.
    lyric_mode: str = "auto"
    renderer: str = "WORLDLINE-R"
    # Left empty, the phonemizer is chosen from the bank's detected flavour
    # (see teto_relay.voicebank.PHONEMIZERS). Set it to force one.
    phonemizer: str = ""
    bpm: float = 120.0
    resolution: int = 480  # ticks per quarter note

    # ------------------------------------------------------ stage 5: render
    # "openutau" renders through the real voicebank; "null" is the tone
    # fallback, which is also used automatically if the engine fails to start.
    renderer_backend: str = "openutau"
    # Calling Phonemizer.SetUp ourselves before the static Phonemize path.
    # Kept as a switch because it was needed before the async dictionary wait
    # existed, and is a suspect for the intermittent "error" phoneme.
    explicit_phonemizer_setup: bool = False
    # How long one phrase may take to synthesise before the utterance is
    # abandoned. WORLDLINE normally needs a fraction of a second; without a
    # limit a stalled engine blocked the render thread forever, silently.
    render_timeout_seconds: float = 30.0
    # Start the rendered audio at the first sound instead of at the start of
    # the recording. The part begins where the first word was said - pre-roll
    # plus your reaction time after pressing the key, typically 0.2-0.5 s - and
    # that silence used to be played into VB-Cable as pure extra latency.
    trim_leading_silence: bool = True
    # Left empty, common install locations are searched (teto_relay.locate).
    openutau_dir: str = ""

    # ----------------------------------------------------- stage 6: playback
    playback_gain: float = 1.0
    # Keep one output stream open for the whole session instead of opening a
    # new one for every phrase. Opening a WASAPI stream takes time on every
    # utterance; the `output` stage of the latency line shows how much on your
    # PC. Off until it has been tried on real hardware.
    persistent_output: bool = False
    # Save what the microphone heard beside each rendered phrase in out/
    # (relay_<time>_in.wav), so a phrase can be re-rendered or reported later.
    keep_input_audio: bool = False

    # -------------------------------------------------------------- runtime
    out_dir: str = field(default_factory=lambda: str(paths.data_dir() / "out"))
    log_file: str = field(default_factory=lambda: str(paths.data_dir() / "teto-relay.log"))
    keep_files: int = 50  # trim out/ to this many recent utterances
    queue_size: int = 4  # bounded; oldest is dropped when full

    # ------------------------------------------------------------- helpers
    @property
    def frame_samples(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000)

    @property
    def ticks_per_second(self) -> float:
        """480 ticks/quarter at 120 BPM -> 960 ticks per second."""
        return self.resolution * self.bpm / 60.0

    def seconds_to_ticks(self, seconds: float) -> int:
        return int(round(seconds * self.ticks_per_second))

    @property
    def out_path(self) -> Path:
        p = Path(self.out_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def voicebank_path(self) -> Path:
        """`voicebank_root`, or the first usual place that has a bank in it."""
        from .locate import find_voicebank_root

        return find_voicebank_root(self.voicebank_root)

    def openutau_path(self) -> Path | None:
        """`openutau_dir`, or a detected OpenUtau install; None if none is found."""
        from .locate import find_openutau

        return find_openutau(self.openutau_dir)

    # ----------------------------------------------------------- loading
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Read the config file, or return defaults if there is none.

        Raises ConfigError, with a message a person can act on, when the file
        cannot be used. Keys this version does not know are logged and ignored
        rather than refused, so a config written by a newer or older version
        does not stop the app from starting.
        """
        path = Path(path or CONFIG_PATH or paths.config_path())
        if not path.exists():
            return cls()
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ConfigError(f"Could not read {path}: {exc.strerror or exc}.") from exc
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}: "
                f"{exc.msg}). Fix that line, or delete the file to go back to the defaults."
            ) from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path} should hold a JSON object ({{...}}), not {type(data).__name__}.")
        return cls.from_dict(data, source=str(path))

    @classmethod
    def from_dict(cls, data: dict, source: str = "config") -> "Config":
        known = {f.name for f in fields(cls)}
        # Settings of removed features are dropped without a word: they are
        # in every config written before, and are gone after the next save.
        data = {k: v for k, v in data.items() if k not in RETIRED}
        unknown = sorted(set(data) - known)
        if unknown:
            log.warning(
                "Ignoring settings this version does not know in %s: %s",
                source, ", ".join(unknown),
            )
        values, problems = {}, []
        for key in sorted(set(data) & known):
            try:
                values[key] = coerce(key, data[key])
            except ConfigError as exc:
                problems.append(str(exc))
        cfg = cls(**values)
        # Report every problem in one go, type errors and range errors alike,
        # rather than making someone fix and restart once per mistake.
        problems += cfg.problems()
        if problems:
            raise ConfigError(_problem_list(source, problems))
        cfg.validate(source)
        return cfg

    def validate(self, source: str = "config") -> None:
        """Check ranges, choices and relationships; raise ConfigError if any fail.

        Also makes the path settings absolute.
        """
        problems = self.problems()
        if problems:
            raise ConfigError(_problem_list(source, problems))
        # Relative paths are taken from the data folder, and made absolute
        # now: the OpenUtau host changes the working directory later.
        for key in ("out_dir", "log_file"):
            setattr(self, key, str(paths.resolve(getattr(self, key))))
        for key in ("voicebank_root", "openutau_dir"):
            if getattr(self, key):
                setattr(self, key, str(paths.resolve(getattr(self, key))))

    def problems(self) -> list[str]:
        """Everything wrong with the current values, as sentences."""
        problems: list[str] = []
        for key, allowed in CHOICES.items():
            value = getattr(self, key)
            if isinstance(value, str):
                value = value.strip().lower()
                setattr(self, key, value)
            if value not in allowed:
                problems.append(
                    f"{key} is {value!r}; it must be one of: {', '.join(sorted(allowed))}."
                )
        for key, (low, high) in RANGES.items():
            value = getattr(self, key)
            if not low <= value <= high:
                problems.append(f"{key} is {value}; it must be between {low} and {high}.")
        if self.f0_min >= self.f0_max:
            problems.append(f"f0_min ({self.f0_min}) must be below f0_max ({self.f0_max}).")
        if self.midi_min > self.midi_max:
            problems.append(f"midi_min ({self.midi_min}) must not be above midi_max ({self.midi_max}).")
        if self.min_mora_seconds > self.max_mora_seconds:
            problems.append(
                f"min_mora_seconds ({self.min_mora_seconds}) must not be above "
                f"max_mora_seconds ({self.max_mora_seconds})."
            )
        if not str(self.ptt_key or "").strip():
            problems.append("ptt_key is empty; set it to a key such as f8.")
        try:
            from .singing import key_from_config

            key_from_config(self.scale_key)
        except ValueError as exc:
            problems.append(f"{exc}.")
        return problems

    def save(self, path: Path | None = None) -> Path:
        path = Path(path or CONFIG_PATH or paths.config_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and swapped in, so a crash mid-write
        # cannot leave a half-written config that stops the next start.
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = asdict(self)
        # Paths inside the data folder are stored relative to it, so a
        # portable copy still works after being moved (another drive letter,
        # a USB stick); they are made absolute again when loaded.
        home = paths.data_dir()
        for key in ("out_dir", "log_file", "voicebank_root"):
            if data.get(key):
                try:
                    data[key] = Path(data[key]).resolve().relative_to(home).as_posix()
                except ValueError:
                    pass  # outside the data folder: kept as given
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return path


# Settings with a fixed set of values. Compared case-insensitively.
CHOICES: dict[str, set[str]] = {
    "capture_mode": {"ptt", "vad"},
    "lyric_mode": {"auto", "native", "japanese"},
    "renderer_backend": {"openutau", "null"},
    "shift_mode": {"semitone", "octave"},
    "pitch_method": {"crepe", "pyin"},
    "crepe_model": {"full", "tiny"},
    "whisper_device": {"cpu", "cuda", "auto"},
    "singing_style": {"speech", "sung"},
    "scale": {"major", "minor", "pentatonic", "chromatic"},
    "double_when": {"off", "singing", "always"},
    "panel_window": {"app", "browser"},
    "whisper_compute_type": {
        "default", "auto", "int8", "int8_float16", "int8_float32", "int8_bfloat16",
        "int16", "float16", "bfloat16", "float32",
    },
}

# Numeric settings with limits outside which the pipeline breaks or hangs.
RANGES: dict[str, tuple[float, float]] = {
    # Whisper, crepe and the aligner are all fed the capture rate directly.
    "sample_rate": (16000, 16000),
    "frame_ms": (5, 200),
    "silence_ms": (20, 10_000),
    "min_chunk_ms": (0, 60_000),
    "max_chunk_ms": (500, 120_000),
    "preroll_ms": (0, 5_000),
    "calibrate_ms": (0, 10_000),
    "calibrate_margin": (0.1, 100.0),
    "rms_threshold": (0.0, 1.0),
    "beam_size": (1, 20),
    "no_speech_threshold": (0.0, 1.0),
    "crepe_voiced_threshold": (0.0, 1.0),
    "f0_min": (20.0, 2000.0),
    "f0_max": (40.0, 4000.0),
    "octave_snap_cents": (0.0, 1200.0),
    "midi_min": (0, 127),
    "midi_max": (0, 127),
    "target_tone": (0, 127),
    "max_shift": (0, 96),
    "shift_tolerance": (0.0, 48.0),
    "default_tone": (0, 127),
    "note_gap_ms": (0, 2_000),
    "phrase_gap_ms": (0, 5_000),
    "onset_push_ms": (0, 500),
    "min_note_seconds": (0.0, 10.0),
    "seconds_per_syllable": (0.0, 10.0),
    "min_mora_seconds": (0.0, 5.0),
    "max_mora_seconds": (0.01, 5.0),
    "pause_borrow": (0.0, 1.0),
    "contour_smooth_ms": (0.0, 2_000.0),
    "contour_points": (2, 64),
    "contour_range_cents": (0.0, 2_400.0),
    "transpose": (-48, 48),
    "bpm": (20.0, 400.0),
    "resolution": (15, 3840),
    "playback_gain": (0.0, 10.0),
    "keep_files": (0, 100_000),
    "render_timeout_seconds": (1.0, 600.0),
    "sung_contour_amount": (0.0, 1.0),
    "sung_melody_range": (0.5, 3.0),
    "double_voice": (0.0, 1.0),
    "vibrato_min_seconds": (0.05, 10.0),
    "vibrato_depth_cents": (0.0, 200.0),
    "vibrato_period_ms": (40.0, 1000.0),
    "final_hold_seconds": (0.0, 5.0),
    "queue_size": (1, 64),
}

#: Settings of features that were removed. The RVC voice engine ("Voice"
#: mode) went in 0.3.0: Teto Relay is Teto singing what you say, and RVC made
#: her a filter on your own voice instead. It is at the git tag
#: before-rvc-removal.
RETIRED = {
    "mode", "rvc_model", "rvc_index", "rvc_device", "rvc_f0_method", "rvc_pitch",
    "rvc_index_rate", "rvc_filter_radius", "rvc_rms_mix_rate", "rvc_protect",
    "voice_streaming", "stream_block_ms", "stream_context_ms", "stream_crossfade_ms",
}

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def _problem_list(source: str, problems: list[str]) -> str:
    lines = "\n".join(f"  - {p}" for p in problems)
    return f"Settings in {source} need fixing:\n{lines}"


def _field_types() -> dict[str, str]:
    hints = typing.get_type_hints(Config)
    out = {}
    for f in fields(Config):
        hint = hints[f.name]
        args = set(typing.get_args(hint))
        if hint is bool:
            out[f.name] = "bool"
        elif hint is int:
            out[f.name] = "int"
        elif hint is float:
            out[f.name] = "float"
        elif str in args and type(None) in args:
            out[f.name] = "str|None"
        else:
            out[f.name] = "str"
    return out


def coerce(key: str, value):
    """Turn a JSON (or panel) value into the type the setting needs.

    Hand-edited files and form fields send "3" for 3 and "true" for true. Those
    used to be stored as given and crash deep inside the pipeline, mid-
    utterance, far from the setting that caused it.
    """
    kind = _field_types().get(key)
    if kind is None:
        raise ConfigError(f"{key} is not a setting.")
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            text = str(value).strip().lower()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
            raise ValueError
        if kind == "int":
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
            if number != int(number):
                raise ValueError
            return int(number)
        if kind == "float":
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
            if number != number or number in (float("inf"), float("-inf")):
                raise ValueError
            return number
        if kind == "str|None":
            if value is None:
                return None
            text = str(value)
            return text if text.strip() else None
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            raise ValueError
        return str(value)
    except (TypeError, ValueError, OverflowError):
        expected = {"bool": "true or false", "int": "a whole number", "float": "a number",
                    "str": "text", "str|None": "text"}[kind]
        raise ConfigError(f"{key} is {value!r}; it must be {expected}.") from None
