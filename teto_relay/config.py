"""Central configuration for Teto Relay.

Every tunable lives here so there is exactly one place to look when the pipeline
misbehaves. Values can be overridden by a JSON file (see `Config.load`).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"


@dataclass
class Config:
    # ------------------------------------------------------------------ mode
    # "utau"  - transcribe, pitch-detect, build notes, sing through the
    #           voicebank. Teto singing your words.
    # "voice" - RVC voice conversion: your audio, Teto's timbre, your delivery
    #           kept intact. Not built yet; selecting it falls back to "utau".
    mode: str = "utau"

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
    # tiny.en < base.en < small.en < medium.en - bigger is more accurate and
    # slower. See the latency table in the README before changing this.
    whisper_model: str = "base.en"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    language: str = "en"
    beam_size: int = 5
    # Whisper's word timings come from attention and are systematically early -
    # measured at +0.06 to +0.20s per word against a forced aligner, and too
    # long. Both note length and pitch are read from those spans, so the error
    # propagates. Alignment measures them against the audio for about 0.06s per
    # utterance. Model is ~1.2GB, downloaded on first use.
    use_alignment: bool = True
    align_device: str = "cuda"  # falls back to cpu automatically
    # Primes whisper's vocabulary. "Teto" is out-of-vocabulary and comes back
    # as "ted oh" or "cassini tito" without it - a bigger model does not fix
    # that, because the problem is an unknown proper noun, not capacity.
    initial_prompt: str = "Kasane Teto, UTAU, vocaloid, voicebank."
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
    # Notes must not touch. Butt two words together and the phonemizer treats
    # them as one legato phrase and collapses the phoneme sequence - five words
    # came back as 4 phonemes instead of 15. Any gap at all avoids it.
    # Notes must not *touch* - at exactly zero the phonemizer collapses the
    # sequence (English 15 phonemes -> 4, Japanese 6 -> 2). But only literal
    # zero breaks it: one tick is enough for both banks.
    #
    # This was 30 ms, chosen off a coarse 0/10/30/50/100 grid. Inaudible
    # between words, clearly audible between morae, where notes are ~0.2 s and
    # every syllable got a gap. 2 ms is one tick above the proven minimum.
    note_gap_ms: int = 2
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
    # How much of the pause after a word its morae may sing into. 1.0 uses the
    # whole gap, which matches the speech's total length but leaves no silence
    # between words and runs them together. 0.0 confines each word to its own
    # span, which is where the flat-rhythm problem came from. Half keeps an
    # audible gap while still giving the morae room to be sung.
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

    # -------------------------------------------------------- stage 4: ustx
    # Where the Teto banks live. Discovery walks this for character.txt/oto.ini,
    # so all three banks are found regardless of their differing layouts.
    voicebank_root: str = r"D:\Claude"
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
    openutau_dir: str = r"D:\Work\OpenUtau"

    # --------------------------------------------- voice conversion (RVC)
    # Used when mode == "voice". Your audio goes in and comes out with Teto's
    # timbre, keeping your own timing, pitch and delivery - so none of the
    # transcription, note or phoneme settings above apply.
    rvc_model: str = r"D:\Claude\Kasane%20Teto\Kasane Teto.pth"
    rvc_index: str = r"D:\Claude\Kasane%20Teto\added_IVF1367_Flat_nprobe_1_Kasane Teto_v2.index"
    rvc_device: str = "cuda:0"  # "cpu" works but is far slower
    # rmvpe is the most robust pitch extractor and the one least prone to the
    # octave errors that plagued the UTAU path.
    # crepe rather than rmvpe: torchcrepe is already installed and already
    # warmed for the UTAU path, so voice mode reuses a loaded model instead of
    # downloading another 180 MB. rmvpe still works if you fetch rmvpe.pt into
    # the model folder.
    rvc_f0_method: str = "crepe"
    # Semitones. Teto is a high female voice, so a lower speaking voice usually
    # needs shifting up; 12 is a reasonable starting point for a male voice.
    rvc_pitch: int = 12
    # How much of the index (the speaker's characteristic timbre) to blend in.
    rvc_index_rate: float = 0.75
    rvc_filter_radius: int = 3  # median-filters the pitch curve, reducing breathiness
    rvc_rms_mix_rate: float = 0.25  # 0 keeps your dynamics, 1 uses the model's
    rvc_protect: float = 0.33  # protects consonants from being over-converted

    # ----------------------------------------------------- stage 6: playback
    playback_gain: float = 1.0

    # -------------------------------------------------------------- runtime
    out_dir: str = str(PROJECT_ROOT / "out")
    log_file: str = str(PROJECT_ROOT / "teto-relay.log")
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

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        if not path.exists():
            return cls()
        known = {f.name for f in fields(cls)}
        data = json.loads(path.read_text(encoding="utf-8"))
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown config keys in {path}: {sorted(unknown)}")
        return cls(**data)

    def save(self, path: Path | None = None) -> Path:
        path = path or CONFIG_PATH
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        return path
