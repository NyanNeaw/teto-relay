"""Pipeline orchestration.

Four threads joined by three bounded queues:

    mic -> [chunks] -> analyse -> [ustx] -> render -> [wav] -> playback

Every queue drops its oldest item when full. A slow renderer therefore costs
you the occasional utterance instead of accumulating unbounded lag - for a live
relay, being a few seconds behind is worse than missing a phrase.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import align
from . import devices as devices_mod
from . import japanese as jp_mod
from . import pitch as pitch_mod
from . import translit
from . import voicebank as vb_mod
from .capture import MicCapture, calibrate_threshold, make_chunker
from .config import Config
from .errors import TetoRelayError
from .hotkey import PushToTalkListener
from .latency import Timeline, append_csv
from . import paths
from .notes import build_notes
from .performance import expression_curves
from .render import make_renderer
from .playback import Player
from .stt import Transcriber, Word
from .ustx import write_ustx

log = logging.getLogger(__name__)


@dataclass
class Job:
    """One utterance travelling through the pipeline, with its timings."""

    captured_at: float  # when the utterance closed at the microphone
    text: str
    ustx_path: Path | None = None
    wav_path: Path | None = None
    analyse_seconds: float = 0.0
    render_seconds: float = 0.0
    # Where the time went, stage by stage; see teto_relay.latency.
    timeline: Timeline = field(default_factory=Timeline)
    # When the job was last put on a queue, so the wait can be charged.
    queued_at: float = 0.0
    # The phrase was sung rather than spoken (pitch.held_share).
    sung: bool = False
    # For takes (teto_relay.takes): when the phrase's recording started
    # (monotonic), what the microphone heard while a take is on, and where in
    # that recording the rendered audio starts.
    audio_start: float | None = None
    input_audio: object = None
    input_rate: int = 16000
    wav_origin: float = 0.0
    # Also only while a take records: what it takes to build the line again
    # from corrected words (resing) - the words with their timing, the pitch
    # track, and build_notes' running state as it was for this phrase.
    words: list | None = None
    track: object = None
    context: dict | None = None
    # Sung into a take: kept off the output (VB-Cable), so a cover being
    # recorded does not go out as your microphone too. Hear myself still plays it.
    in_take: bool = False

    @property
    def age(self) -> float:
        return time.monotonic() - self.captured_at


def _drop_oldest_put(q: queue.Queue, item, label: str) -> None:
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
            q.put_nowait(item)
            log.warning("%s queue full - dropped the oldest item", label)
        except (queue.Empty, queue.Full):
            pass


def warm_thai() -> None:
    """Load pythainlp's word list and Thai G2P model (see teto_relay.thai)."""
    from pythainlp.tokenize import word_tokenize

    from . import thai

    word_tokenize("สวัสดีครับ", engine="newmm")
    thai.syllables("ทดสอบ")


#: How long stop() waits for the phrase being analysed or rendered to finish.
STOP_WAIT_SECONDS = 30.0
#: How many earlier phrases the panel can list.
HISTORY_SIZE = 6


class TetoRelay:
    """Owns the whole pipeline. `start()` is non-blocking; `stop()` joins."""

    # Takes (teto_relay.takes): the one being recorded, and the phrases still
    # on their way through the pipeline.
    _take = None
    _busy_phrases = 0
    _take_lock = threading.Lock()
    # A render and the origin it leaves behind (renderer.last_origin) are
    # read together: a line sung again from the panel renders too.
    _render_gate = threading.Lock()

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config.load()
        root = self.cfg.voicebank_path()
        self.banks = vb_mod.discover(root)
        self.bank = vb_mod.select_or_default(self.banks, self.cfg.voicebank, root)

        self.chunk_q: queue.Queue = queue.Queue(maxsize=self.cfg.queue_size)
        self.ustx_q: queue.Queue = queue.Queue(maxsize=self.cfg.queue_size)
        self.wav_q: queue.Queue = queue.Queue(maxsize=self.cfg.queue_size)

        self.transcriber = Transcriber(self.cfg)
        self.renderer = make_renderer(self.cfg, self.bank)

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # Told what start() is doing, for the panel: progress(stage, 0..1).
        self.progress = lambda stage, fraction: None
        self._capture: MicCapture | None = None
        self._player: Player | None = None
        self._hotkey: PushToTalkListener | None = None
        # Carried between utterances so the character's pitch stays put.
        self._octave_shift: int | None = None
        # The speaker's usual pitch, learned as they talk. Gives one- and
        # two-word utterances something to check their octave against.
        self._voice_baseline: float | None = None
        # The sung style's key, held between phrases (teto_relay.singing).
        self._singing_state: dict = {}
        # The pitch this voicebank was recorded at; rendering near it keeps the
        # voice's body, which is what makes it sound sung rather than breathy.
        self._target_tone: float = float(self.cfg.target_tone or 60)
        # The shortest note this bank can sing, measured from its oto.
        self._mora_floor: float = float(self.cfg.min_mora_seconds)
        if self.bank is not None:
            self._target_tone = float(self.cfg.target_tone) or vb_mod.estimate_pitch(
                self.bank, self.cfg
            )
            self._mora_floor = vb_mod.mora_floor(self.bank, self.cfg)
        self.last_text = ""
        # What the control panel shows: the words as heard, their Japanese
        # reading, the notes actually sung with their tones, and the timings
        # behind the log line.
        self.last_source = ""
        self.last_kana = ""
        self.last_notes: list[tuple[str, int]] = []
        self.last_stats: dict[str, float | str] = {}
        # The last few phrases as heard, newest last, for the panel's list.
        self.history: deque[dict] = deque(maxlen=HISTORY_SIZE)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Open the devices, warm the models and start the workers.

        If anything fails part-way, whatever did start is stopped again before
        the error is raised - otherwise a failed start left the microphone open
        and threads running, and a second Start opened a second set.
        """
        try:
            self._start()
        except BaseException:
            log.info("Start failed; cleaning up what had started")
            self.stop()
            raise

    def _start(self) -> None:
        cfg = self.cfg
        log.info("Voicebank: %s", self.bank)
        # All three Teto banks share one character.txt name, so the folder is
        # the only unambiguous way to tell which one is actually loaded.
        log.info("  folder:  %s", self.bank.root)
        for sub in self.bank.subbanks:
            log.info("  subbank: %s (%d entries)", sub.path.name, sub.entry_count)
        log.info("Renderer:  %s", self.renderer.name)
        log.info(
            "Lyrics:    %s (lyric_mode=%s)",
            "japanese morae" if self._japanese_lyrics() else "native English",
            self.cfg.lyric_mode or "auto",
        )
        self._warn_on_lyric_mismatch()
        workers = [("analyse", self._analyse_loop), ("render", self._render_loop)]

        out_dev = devices_mod.resolve_output(cfg)
        in_dev = devices_mod.resolve_input(cfg) or devices_mod.default_input()
        log.info("Input:  %s", in_dev if in_dev else "<system default>")
        log.info("Output: %s", out_dev)

        warning = devices_mod.feedback_warning(in_dev, out_dev)
        if warning:
            log.warning(warning)

        # Warm everything before opening the mic, so the first phrase is not
        # lost to model loading. Order matters - see _warmup.
        self._warmup()
        self._preload_in_background()
        self.progress("Opening the microphone", 0.95)

        push_to_talk = (cfg.capture_mode or "ptt").lower() == "ptt"

        threshold = None
        # Calibration only matters to the silence-detecting chunker; with
        # push-to-talk the key decides what counts as speech.
        if cfg.auto_calibrate and not push_to_talk:
            try:
                threshold = calibrate_threshold(cfg, in_dev.index if in_dev else None)
            except Exception:
                log.exception("calibration failed; using the configured threshold")

        chunker = make_chunker(cfg, threshold)
        if hasattr(chunker, "live_factory") and (cfg.pitch_method or "crepe").lower() == "crepe":
            # Pitch is worked out while the key is held (teto_relay.live).
            from .live import LivePitch

            chunker.live_factory = lambda: LivePitch(self.cfg, self.cfg.sample_rate)
        self._player = Player(cfg, self.wav_q, out_dev.index, on_playback=self._on_playback,
                              quiet=lambda job: bool(getattr(job, "in_take", False)))
        self._capture = MicCapture(
            cfg, self.chunk_q, in_dev.index if in_dev else None, threshold, chunker=chunker
        )

        if push_to_talk:
            try:
                self._hotkey = PushToTalkListener(cfg.ptt_key, chunker.start, chunker.stop)
                self._hotkey.start()
            except Exception:
                log.exception(
                    "could not arm push-to-talk on key %r; falling back to silence detection",
                    cfg.ptt_key,
                )
                self._capture.chunker = make_chunker(Config(**{**self.cfg.__dict__, "capture_mode": "vad"}))

        self._threads = [
            threading.Thread(target=fn, name=name, daemon=True) for name, fn in workers
        ]
        for t in self._threads:
            t.start()
        self._player.start()
        self._capture.start()
        mic_name = in_dev.name if in_dev else "the default mic"
        if push_to_talk and self._hotkey is not None:
            log.info("Teto Relay running. Hold [%s] and speak into %s.", cfg.ptt_key.upper(), mic_name)
        else:
            log.info("Teto Relay running. Speak into %s.", mic_name)

    def _pitch_for(self, chunk):
        """The phrase's pitch: finished from what was worked out while it was
        recorded (teto_relay.live), or tracked now."""
        live = getattr(chunk, "live", None)
        track = live.finish(chunk.audio) if live is not None else None
        track = track or pitch_mod.track_f0(chunk.audio, chunk.sample_rate, self.cfg)
        return pitch_mod.fix_octave_jumps(track, self.cfg)

    def _side_work(self):
        """One worker thread for analysis that can run beside whisper."""
        if getattr(self, "_side_pool", None) is None:
            from concurrent.futures import ThreadPoolExecutor

            self._side_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="side")
        return self._side_pool

    def _preload_in_background(self) -> None:
        """Load the forced aligner now, quietly, whatever the language.

        The warm-up only loads what the current language and voicebank need.
        Switching to Japanese or English while running then loaded the aligner
        on the first phrase after: a 6-10 s wait the user heard as the app
        hanging. Loaded here instead, behind the scenes, it is ready by then.
        """
        cfg = self.cfg
        if not (cfg.use_alignment or getattr(cfg, "align_boundaries", False) or cfg.align_morae):
            return

        def load() -> None:
            try:
                align._load(cfg)
                log.debug("aligner preloaded")
            except Exception:  # noqa: BLE001 - loaded on first use instead
                log.debug("could not preload the aligner", exc_info=True)

        threading.Thread(target=load, name="preload", daemon=True).start()

    def _warmup(self) -> None:
        """Pay the one-time initialisation costs before the microphone opens.

        Loading the whisper model is not enough: CTranslate2 defers work to the
        first inference, and librosa.pyin is numba-compiled, so its first call
        triggers a JIT compile. Together those made the first real utterance
        take ~20s while later ones took ~2s - and three more utterances piled
        up in the queue behind it.

        **Order is not arbitrary.** faster-whisper (via ctranslate2) and torch
        each bundle their own cuDNN, and whichever initialises first wins. Load
        whisper first and the next CUDA convolution - crepe, or the aligner -
        dies with "Could not load symbol cudnnGetLibConfig" and takes the
        process with it, with no Python traceback. So every torch-backed model
        is touched before whisper is loaded.
        """
        import numpy as np

        began = time.monotonic()
        sample_rate = self.cfg.sample_rate
        t = np.arange(int(0.5 * sample_rate)) / sample_rate
        probe = (0.2 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)

        timings: list[str] = []
        failed: list[str] = []

        # What the panel shows while each stage loads, and how far along that
        # is (whisper is the slow one, so it is given the widest step).
        shown = {
            "pitch": ("Warming up pitch tracking", 0.25),
            "aligner": ("Loading syllable timing", 0.35),
            "whisper": ("Loading the speech model", 0.45),
            "lyrics": ("Loading the dictionary", 0.85),
            "thai": ("Loading Thai pronunciation", 0.85),
        }

        def stage(name: str, fn) -> None:
            """Run one warmup stage, timing it and reporting failure loudly.

            A stage that fails here does not stop the relay - it defers its cost
            to the first real utterance, which is how a 1.68s phrase once took
            14.30s. Cold load is ~9s for crepe and ~9s for the aligner, so a
            silent failure here is the one thing that reproduces that spike.
            """
            began_stage = time.monotonic()
            label, fraction = shown.get(name, (f"Loading {name}", 0.5))
            self.progress(label, fraction)
            try:
                fn()
            except Exception:
                failed.append(name)
                log.warning(
                    "%s warmup FAILED - its load cost will land on the first "
                    "utterance instead",
                    name,
                    exc_info=True,
                )
                return
            timings.append(f"{name} {time.monotonic() - began_stage:.1f}s")

        # 1. torch-backed models first, to claim cuDNN.
        stage("pitch", lambda: pitch_mod.track_f0(probe, sample_rate, self.cfg))

        if self.cfg.use_alignment or self._aligns_boundaries():
            stage("aligner", lambda: align._measure([Word("test", 0.0, 0.4)], probe, sample_rate, self.cfg))
        elif self.cfg.align_morae and self._japanese_lyrics():
            stage("aligner", lambda: align.vowel_onsets(["あ"], probe, sample_rate, self.cfg))

        # 2. whisper last.
        def _whisper() -> None:
            self.transcriber.load()
            self.transcriber.transcribe(probe, sample_rate)

        stage("whisper", _whisper)

        # 3. the lyric dictionary. The probe above is a tone, so whisper returns
        # no words and the loop exits before ever reaching the lyric stage -
        # which left cmudict to load lazily on the first real utterance. The new
        # per-stage timings caught it: notes+ustx took 0.84s on the first phrase
        # and 0.00s on the second.
        if self._japanese_lyrics():
            stage("lyrics", lambda: jp_mod.english_to_kana("hello"))
        # Thai: the word list and the pronunciation model load (and, the first
        # time, download) on first use - which was the first Thai phrase.
        if translit.source_language(self.cfg) == "th":
            stage("thai", warm_thai)

        elapsed = time.monotonic() - began
        if failed:
            log.warning(
                "Warmed up analysis in %.1fs, but %s did not warm (%s) - expect "
                "the first utterance to be slow",
                elapsed,
                " and ".join(failed),
                ", ".join(timings) or "nothing warmed",
            )
        else:
            log.info("Warmed up analysis in %.1fs (%s)", elapsed, ", ".join(timings))

    def stop(self) -> None:
        """Stop everything that is running. Never raises; safe to call twice.

        It runs on the failure path of `start()` as well, where some parts
        were never created, and one part failing to stop must not leave the
        others running - nor hide the error that caused the stop.
        """
        log.info("Stopping...")

        def attempt(label: str, fn) -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 - keep stopping the rest
                log.debug("stopping %s failed", label, exc_info=True)

        stop_event = getattr(self, "_stop", None)
        if stop_event is not None:
            stop_event.set()
        hotkey = getattr(self, "_hotkey", None)
        capture = getattr(self, "_capture", None)
        player = getattr(self, "_player", None)
        attempt("music", self._stop_backing)
        if hotkey:
            attempt("push-to-talk", hotkey.stop)
        if capture:
            attempt("microphone", capture.stop)
        if player:
            attempt("playback", player.stop)
        # The phrase in progress is finished, not abandoned. After 2 s the old
        # relay's analysis carried on beside the next relay's start - a
        # settings restart while a slow phrase was being transcribed - and the
        # two fought over the GPU: one phrase took 126 s to transcribe.
        for t in getattr(self, "_threads", []):
            attempt(t.name, lambda t=t: t.join(timeout=STOP_WAIT_SECONDS))
            if t.is_alive():
                log.warning("%s did not finish within %.0fs of stopping", t.name, STOP_WAIT_SECONDS)
        if capture:
            attempt("microphone", lambda: capture.join(timeout=2.0))
        if player:
            attempt("playback", lambda: player.join(timeout=2.0))
        renderer = getattr(self, "renderer", None)
        if renderer is not None:
            attempt(type(renderer).__name__, renderer.close)
        pool = getattr(self, "_side_pool", None)
        if pool is not None:
            self._side_pool = None
            attempt("side work", lambda: pool.shutdown(wait=False, cancel_futures=True))
        log.info("Stopped")

    # ------------------------------------------------------------- controls
    def pause(self) -> None:
        if self._capture:
            self._capture.pause()
            log.info("Paused - microphone ignored")

    def resume(self) -> None:
        if self._capture:
            self._capture.resume()
            log.info("Resumed")

    def hush(self) -> int:
        """PANIC: she stops singing now. The phrase playing is cut and the
        ones waiting to play are dropped; listening carries on. A phrase still
        being worked on plays when it is ready. Returns how many were dropped."""
        player = getattr(self, "_player", None)
        return player.hush() if player is not None else 0

    @property
    def paused(self) -> bool:
        return bool(self._capture and self._capture.paused)

    def health(self) -> dict[str, str]:
        """What is wrong right now, for the panel. Empty values mean fine."""
        capture = getattr(self, "_capture", None)
        mic_state = capture.state if capture else "stopped"
        problems = []
        if capture and mic_state == "retrying":
            problems.append(
                f"The microphone is not available ({capture.last_error}). "
                "Retrying - check it is plugged in and not used by another app."
            )
        renderer = getattr(self, "renderer", None)
        if renderer is not None and getattr(renderer, "name", "") == "null" and \
                (self.cfg.renderer_backend or "").lower() == "openutau":
            problems.append(
                "OpenUtau did not start, so you are hearing plain tones. "
                "Run --doctor or see the log for why."
            )
        return {"microphone": mic_state, "problems": problems}

    def _warn_on_lyric_mismatch(self) -> None:
        """Flag a `lyric_mode` that contradicts the voicebank.

        The two settings are independent, and the wrong pairing renders silently
        wrong rather than failing: kana lyrics on an English CVVC bank, or
        English words on a Japanese CV bank, both reach the phonemizer as
        nonsense. `auto` cannot get this wrong, which is why it is the default.
        """
        mode = (self.cfg.lyric_mode or "auto").lower()
        japanese_bank = self.bank.flavour.startswith("ja-")
        if mode == "japanese" and not japanese_bank:
            log.warning(
                "lyric_mode=japanese but %r is a %s bank - it cannot sing kana. "
                "Set lyric_mode to 'auto', or pick a ja- voicebank.",
                self.bank.key, self.bank.flavour,
            )
        elif mode == "native" and japanese_bank:
            log.warning(
                "lyric_mode=native but %r is a %s bank - it cannot sing English "
                "phonemes. Set lyric_mode to 'auto', or pick the english bank.",
                self.bank.key, self.bank.flavour,
            )

    def _aligns_boundaries(self) -> bool:
        """English sung by an English bank: the case align.boundaries was
        measured on (the aligner reads Latin letters only). Only on a
        graphics card: the aligner's, or whisper's, behind which the aligner's
        CPU work is hidden (align.Pending). With both on the CPU they would
        compete, adding about half a second a phrase."""
        return (
            bool(getattr(self.cfg, "align_boundaries", True))
            and translit.source_language(self.cfg) == "en"
            and not self._japanese_lyrics()
            and (align.on_gpu(self.cfg) or bool(getattr(self.transcriber, "on_gpu", False)))
        )

    def _japanese_lyrics(self) -> bool:
        """Whether to convert what was said into Japanese-style pronunciation.

        On "auto" this follows the voicebank: a Japanese bank cannot sing
        English phonemes, so picking one implies the conversion.
        """
        mode = (self.cfg.lyric_mode or "auto").lower()
        if mode == "japanese":
            return True
        if mode == "native":
            return False
        return self.bank.flavour.startswith("ja-")

    def set_voicebank(self, key: str) -> None:
        """Switch banks at runtime; takes effect on the next utterance."""
        try:
            bank = vb_mod.select(self.banks, key)
        except ValueError:
            # Installed or copied in since the relay started.
            self.banks = vb_mod.discover(self.cfg.voicebank_path())
            bank = vb_mod.select(self.banks, key)
        set_bank = getattr(self.renderer, "set_bank", None)
        if set_bank is not None:
            set_bank(bank)
        self.bank = bank
        self.cfg.voicebank = self.bank.key
        # Each bank is recorded at its own pitch, so retarget and let the shift
        # settle again rather than carrying the previous bank's offset over.
        self._target_tone = float(self.cfg.target_tone) or vb_mod.estimate_pitch(
            self.bank, self.cfg
        )
        self._mora_floor = vb_mod.mora_floor(self.bank, self.cfg)
        self._octave_shift = None
        log.info("Voicebank switched to %s (recorded at MIDI %.1f)", self.bank, self._target_tone)
        # On `auto` this switch also changes the lyric path; on an explicit
        # setting it may have just invalidated it.
        log.info(
            "Lyrics now: %s", "japanese morae" if self._japanese_lyrics() else "native English"
        )
        self._warn_on_lyric_mismatch()

    # ---------------------------------------------------------------- stages
    def _analyse_loop(self) -> None:
        """Chunk -> words + F0 -> notes -> .ustx on disk."""
        while not self._stop.is_set():
            try:
                chunk = self.chunk_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._stop.is_set():
                break  # stopping: nobody will play it
            self._in_progress(+1)
            job = None
            try:
                job = self.analyse(chunk)
                if job is not None:
                    _drop_oldest_put(self.ustx_q, job, "ustx")
            except Exception:
                log.exception("analysis failed for a %.2fs chunk", chunk.duration)
            finally:
                if job is None:
                    self._in_progress(-1)

    def analyse(self, chunk) -> "Job | None":
        """One utterance: words + F0 -> notes -> .ustx on disk, ready to render.

        The whole of the analysis stage, apart from the queues, so that tools
        (tools/tuning_eval.py) score exactly what the relay sings. None when
        there is nothing to sing.
        """
        began = time.monotonic()
        stamp = datetime.now().strftime("%H%M%S_%f")[:-3]
        if self.cfg.keep_input_audio:
            self._save_input(chunk, stamp)
        # Per-stage timings, so a slow utterance says which stage was slow.
        # Steady state is roughly stt 2.0s, align 0.06s, pitch 0.2s; a stage
        # an order of magnitude above that is a model loading late.
        timeline = Timeline(released_at=chunk.captured_at)
        timeline.add("speech", chunk.duration)
        timeline.lap("wait_analyse", began)

        # The aligner's half that needs no words runs while whisper listens.
        pending = (
            align.Pending(chunk.audio, self.cfg)
            if self.cfg.use_alignment or self._aligns_boundaries()
            or (self.cfg.align_morae and self._japanese_lyrics())
            else None
        )
        # Pitch needs only the audio, so it is measured while whisper listens:
        # 0.2-0.6 s of every phrase that used to come after it.
        pitch = self._side_work().submit(self._pitch_for, chunk)
        words = self.transcriber.transcribe(chunk.audio, chunk.sample_rate)
        timeline.lap("asr")
        if not words:
            log.info("No speech recognised in a %.2fs chunk", chunk.duration)
            return None

        # Measure when each word was actually said. Whisper's timings
        # are systematically early, and both the note length and the
        # pitch window are taken from these spans.
        if self.cfg.use_alignment:
            words = align.refine(words, chunk.audio, chunk.sample_rate, self.cfg, pending)
            timeline.lap("align")
        elif self._aligns_boundaries():
            # Only where one word hands over to the next (align.boundaries):
            # whisper gives the end of a long word to the one after it.
            words = align.boundaries(words, chunk.audio, chunk.sample_rate, self.cfg, pending)
            timeline.lap("align")

        track = pitch.result()
        timeline.lap("pitch")  # only what is left of it after whisper
        if not track.any_voiced:
            log.info("No voiced frames; skipping this utterance")
            return None
        context = {"previous_shift": self._octave_shift, "target_tone": self._target_tone,
                   "baseline": self._voice_baseline, "mora_floor": self._mora_floor}

        notes = build_notes(
            words, track, self.cfg, self._octave_shift, self._target_tone,
            self._voice_baseline, self._japanese_lyrics(), self._mora_floor,
            self._singing_state, audio=chunk.audio, sample_rate=chunk.sample_rate,
            mora_timer=lambda morae: align.vowel_onsets(morae, chunk.audio, chunk.sample_rate, self.cfg, pending),
        )
        if not notes:
            return None
        timeline.lap("notes")
        self._octave_shift = notes[0].shift

        # Learn the speaker's usual pitch so short utterances have a
        # reference for octave correction. Only phrases long enough to
        # have self-corrected contribute - otherwise a lone mis-detected
        # "hello" defines the baseline and every later one agrees with
        # it. Weighted towards history so one reading cannot move it far.
        measured = [n.detected_midi for n in notes if n.detected_midi is not None]
        if len(measured) >= 3:
            centre = float(sorted(measured)[len(measured) // 2])
            self._voice_baseline = (
                centre
                if self._voice_baseline is None
                else 0.8 * self._voice_baseline + 0.2 * centre
            )

        self.last_text = " ".join(n.lyric for n in notes)
        self.last_notes = [(n.lyric, n.tone) for n in notes]
        self.last_source = " ".join(w.text for w in words)
        # The Japanese reading is shown even on an English bank, where
        # it is a caption rather than what is sung - the panel labels
        # the two lines so they cannot be confused.
        try:
            self.last_kana = " ".join(
                jp_mod.english_to_kana(w.text) or w.text for w in words
            )
        except Exception:
            self.last_kana = ""
        if getattr(self, "history", None) is None:  # a relay built without __init__ (tests, tools)
            self.history = deque(maxlen=HISTORY_SIZE)
        self.history.append({"heard": self.last_source, "sung": self.last_text, "count": len(notes)})
        # What Tune's play button sings again (preview): the phrase as heard,
        # before any setting turned it into notes.
        # The aligner's measure of the recording goes with it: a preview
        # that worked it out again ran the 1.6 GB model on every change,
        # seconds each on a CPU, and on a PC short of memory it stalled
        # everything (picking a tuning on Tune, with "Play after every change").
        self._last_phrase = {"words": list(words), "track": track, "audio": chunk.audio,
                             "rate": chunk.sample_rate, "context": dict(context), "pending": pending,
                             "sung": pitch_mod.held_share(track) >= pitch_mod.SUNG_HELD_SHARE}
        path = self.cfg.out_path / f"relay_{stamp}.ustx"
        curves = expression_curves(notes, chunk.audio, chunk.sample_rate, self.cfg)
        write_ustx(notes, path, self.bank, self.cfg, curves)
        done = timeline.lap("ustx")

        job = Job(
            captured_at=chunk.captured_at,
            text=self.last_text,
            ustx_path=path,
            analyse_seconds=done - began,
            timeline=timeline,
            queued_at=done,
            sung=pitch_mod.held_share(track) >= pitch_mod.SUNG_HELD_SHARE,
            audio_start=chunk.captured_at - chunk.duration,
            # Kept only while a take is recording: it is the guide track.
            input_audio=chunk.audio if getattr(self, "_take", None) is not None else None,
            input_rate=chunk.sample_rate,
        )
        if getattr(self, "_take", None) is not None:
            job.words, job.track, job.context = list(words), track, context
        log.info(
            "Analysed %.2fs of speech in %.2fs [%s] via %s",
            chunk.duration,
            job.analyse_seconds,
            " ".join(
                f"{name} {timeline.stages[name]:.2f}s"
                for name in ("asr", "align", "pitch", "notes", "ustx")
                if name in timeline.stages
            ),
            track.method or "unknown",
        )
        self.last_stats = {
            "speech": round(chunk.duration, 2),
            "analyse": round(job.analyse_seconds, 2),
            "method": track.method or "unknown",
        }
        return job

    def finish_render(self, job: "Job", cfg=None) -> None:
        """Effects on the rendered audio (teto_relay.effects): the tuning's
        drive, growl, filters, chorus and reverb, and the doubled lead on
        sung phrases (or always, with double_when). `cfg` is the settings to
        use (a preview of another tuning); the ones in force otherwise.
        Never costs the phrase."""
        from . import effects

        cfg = cfg if cfg is not None else self.cfg
        amount = float(cfg.double_voice)
        when = cfg.double_when
        double = amount if amount > 0 and when != "off" and (job.sung or when == "always") else 0.0
        if double <= 0 and not effects.active(cfg):
            return
        try:
            import soundfile as sf

            audio, rate = sf.read(str(job.wav_path), dtype="float32")
            sf.write(str(job.wav_path), effects.apply(audio, rate, cfg, double), rate)
        except Exception:  # noqa: BLE001
            log.debug("could not finish %s", job.wav_path, exc_info=True)

    def _save_input(self, chunk, stamp: str) -> None:
        """keep_input_audio: the phrase as the microphone heard it. Never raises."""
        try:
            import soundfile as sf

            sf.write(str(self.cfg.out_path / f"relay_{stamp}_in.wav"), chunk.audio,
                     chunk.sample_rate, subtype="PCM_16")
        except Exception:  # noqa: BLE001 - a diagnostic must not cost the phrase
            log.debug("could not save the input audio", exc_info=True)

    def _render_loop(self) -> None:
        """.ustx -> .wav."""
        while not self._stop.is_set():
            try:
                job = self.ustx_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._stop.is_set():
                break  # stopping: nobody will play it

            began = time.monotonic()
            job.timeline.restart(job.queued_at or began)
            job.timeline.lap("wait_render", began)
            try:
                job.wav_path = job.ustx_path.with_suffix(".wav")
                job.wav_origin = self._render(job.ustx_path, job.wav_path)
                self.finish_render(job)
                take = getattr(self, "_take", None)
                if take is not None:
                    take.add(job, self.voice_now())
                    job.in_take = True
                done = job.timeline.lap("render")
                for stage, seconds in (getattr(self.renderer, "last_timings", None) or {}).items():
                    job.timeline.add(stage, seconds)
                job.render_seconds = done - began
                job.queued_at = done
                log.info(
                    "Rendered in %.2fs - %.2fs behind the microphone at playback",
                    job.render_seconds,
                    job.age,
                )
                self.last_stats.update(
                    render=round(job.render_seconds, 2), behind=round(job.age, 2)
                )
                _drop_oldest_put(self.wav_q, job, "wav")
            except Exception:
                log.exception("render failed for %s", job.ustx_path)
            finally:
                self._in_progress(-1)
                self._trim_output()

    def _render(self, ustx_path: Path, wav_path: Path, bank=None) -> float:
        """Render; returns where in the phrase's recording the audio starts.
        With `bank` (an edited take line), that voice sings it, and the one
        selected is back for the next live phrase."""
        with self._render_gate:
            set_bank = getattr(self.renderer, "set_bank", None)
            swap = (bank is not None and set_bank is not None and self.bank is not None
                    and Path(bank.root) != Path(self.bank.root))
            if swap:
                set_bank(bank)
            try:
                self.renderer.render(ustx_path, wav_path)
                return float(getattr(self.renderer, "last_origin", 0.0) or 0.0)
            finally:
                if swap:
                    set_bank(self.bank)

    def preview(self, settings: dict | None = None) -> Path:
        """Tune's play button: the last phrase sung again with the settings
        as they are now - its words, pitch and recording kept from when it
        was heard - to hear what a change does. With `settings` (a tuning
        from the Market) those are used for this once instead, and nothing
        is changed. Returns the .wav; it is not sent to the output. Raises
        TetoRelayError when nothing has been sung yet, or the settings leave
        nothing to sing."""
        import copy

        last = getattr(self, "_last_phrase", None)
        if not last:
            raise TetoRelayError("Sing a phrase first: the preview sings your last one again.")
        cfg = self.cfg
        if settings:
            cfg = copy.copy(self.cfg)
            for key, value in settings.items():
                setattr(cfg, key, value)
        context, audio, rate = last["context"], last["audio"], last["rate"]
        japanese = self._japanese_lyrics()
        if japanese and cfg.align_morae and last.get("pending") is None:
            # Measured once for this phrase, then kept for every preview of it.
            last["pending"] = align.Pending(audio, cfg, start=False)
        pending = last.get("pending")
        notes = build_notes(
            last["words"], last["track"], cfg, context.get("previous_shift"), context.get("target_tone"),
            context.get("baseline"), japanese, context.get("mora_floor"),
            # A copy: a preview must not move the live key detection.
            copy.deepcopy(self._singing_state), audio=audio, sample_rate=rate,
            mora_timer=(lambda morae: align.vowel_onsets(morae, audio, rate, cfg, pending))
            if japanese and cfg.align_morae else None,
        )
        if not notes:
            raise TetoRelayError("Nothing to sing in that phrase with these settings.")
        ustx = self.cfg.out_path / "preview.ustx"
        wav = ustx.with_suffix(".wav")
        write_ustx(notes, ustx, self.bank, cfg, expression_curves(notes, audio, rate, cfg))
        self._render(ustx, wav)
        self.finish_render(Job(captured_at=time.monotonic(), text="", wav_path=wav, sung=last["sung"]), cfg)
        log.info("Preview sung: %s", " ".join(n.lyric for n in notes))
        return wav

    # ---------------------------------------------------------------- takes
    # Every line of a take remembers the voice that sang it (takes.voice_of):
    # the voicebank, and the tuning - the settings that only change how she
    # sings (presets.SETTINGS), kept in full, with the preset's name if they
    # were one. A line fixed or edited later is sung by that voice again, not
    # by whichever one is picked now, so a song keeps its sound when the app
    # is restarted or the voice is changed for Live. `revoice` gives lines
    # another voicebank or tuning on purpose.
    def voice_now(self) -> dict:
        """The voicebank and tuning in force, as a line keeps them."""
        from . import presets

        bank = self.bank
        settings = {key: getattr(self.cfg, key) for key in presets.SETTINGS}
        tuning, tuning_id = "Custom", ""
        try:
            listed = presets.listing(self.cfg)
            found = next((s for s in listed["styles"] if s["id"] == listed["active"]), None)
            if found is not None:
                tuning, tuning_id = found["name"], found["id"]
        except Exception:  # noqa: BLE001 - a name is a nicety; the settings are what count
            log.debug("could not name the tuning", exc_info=True)
        return {"bank": bank.key if bank is not None else "",
                "bank_name": self._bank_title(bank),
                "tuning": tuning, "tuning_id": tuning_id, "settings": settings}

    @staticmethod
    def _bank_title(bank) -> str:
        if bank is None:
            return ""
        try:
            return vb_mod.custom_name(bank) or bank.name
        except Exception:  # noqa: BLE001
            return bank.name

    def _bank_by_key(self, key: str):
        """The installed voicebank `key`, or None (removed since)."""
        if not key:
            return None
        for bank in [self.bank] + list(getattr(self, "banks", None) or []):
            if bank is not None and bank.key == key:
                return bank
        try:
            self.banks = vb_mod.discover(self.cfg.voicebank_path())
            return vb_mod.select(self.banks, key)
        except (ValueError, OSError):
            return None

    def _voice_cfg(self, settings: dict | None):
        """The settings in force with a line's tuning on top, for that line
        alone: the relay's own are not changed."""
        import copy

        from . import presets

        cfg = copy.copy(self.cfg)
        for key, value in (settings or {}).items():
            if key in presets.SETTINGS:
                setattr(cfg, key, value)
        if settings:
            try:
                cfg.validate("this tuning")
            except Exception as exc:  # noqa: BLE001 - a hand-edited take.json
                raise TetoRelayError(f"That line's tuning can't be used ({exc}).") from exc
        return cfg

    def _line_voice(self, folder: Path, line: dict):
        """The bank and settings a line is sung with again: its own, kept
        with it; for a line saved before voices were kept, the bank its
        project names and the settings in force (then kept from now on)."""
        from . import takes as takes_mod

        voice = takes_mod.voice_of(line)
        bank = self._bank_by_key(voice["bank"]) if voice else None
        if bank is None:
            try:
                doc = takes_mod._read_doc(Path(folder) / takes_mod.LINES / f"{int(line['n']):02d}.ustx")
                bank = self._bank_named(str(doc["tracks"][0].get("singer", "")))
            except (OSError, KeyError, IndexError, TypeError, ValueError):
                bank = None
        bank = bank or self.bank
        if voice:
            settings = dict(voice.get("settings") or {})
        else:
            settings = self.voice_now()["settings"]
        return bank, settings, voice

    def _japanese_for(self, bank) -> bool:
        mode = (self.cfg.lyric_mode or "auto").lower()
        if mode == "japanese":
            return True
        if mode == "native":
            return False
        return bank.flavour.startswith("ja-")

    def _tone_of(self, bank, cfg) -> tuple[float, float]:
        """A bank's recorded pitch and shortest note (as set_voicebank finds
        them), measured once per bank."""
        if bank is self.bank or (self.bank is not None and Path(bank.root) == Path(self.bank.root)):
            return self._target_tone, self._mora_floor
        tones = self.__dict__.setdefault("_bank_tones", {})
        key = str(Path(bank.root))
        if key not in tones:
            tone = float(cfg.target_tone) or vb_mod.estimate_pitch(bank, cfg)
            tones[key] = (tone, vb_mod.mora_floor(bank, cfg))
        return tones[key]

    def _sing_line(self, folder: Path, line: dict, words: list, bank, settings: dict,
                   shift: int | None = None, keep_shift: bool = True, state: dict | None = None) -> int | None:
        """Build one line's notes again from `words` and the pitch and
        recording it was sung with, and sing them with `bank` and the tuning
        `settings`. Writes the line's .ustx and .wav; returns the shift used.
        `keep_shift` keeps the octave the line was first sung in (same bank);
        else `shift` (the line before it, in a revoice) or a new one."""
        import copy

        import soundfile as sf

        from . import takes as takes_mod

        cfg = self._voice_cfg(settings)
        stem = Path(folder) / takes_mod.LINES / f"{int(line['n']):02d}"
        track = takes_mod.load_track(stem.with_name(stem.name + "-pitch.npz"))
        audio, rate = sf.read(str(stem.with_name(stem.name + "-voice.wav")), dtype="float32")
        context = line.get("context") or {}
        japanese = self._japanese_for(bank)
        if keep_shift:
            # The voice it was sung with: the pitch it was aimed at, and its octave.
            target, floor = context.get("target_tone"), context.get("mora_floor")
            previous = context.get("previous_shift")
            if target is None or floor is None:
                tone, shortest = self._tone_of(bank, cfg)
                target = tone if target is None else target
                floor = shortest if floor is None else floor
        else:
            target, floor = self._tone_of(bank, cfg)
            previous = shift
        notes = build_notes(
            words, track, cfg, previous, target,
            context.get("baseline"), japanese, floor,
            # A copy: singing a line again must not move the live key detection.
            # (A revoice carries its own from line to line.)
            copy.deepcopy(self._singing_state) if state is None else state, audio=audio, sample_rate=rate,
            mora_timer=(lambda morae: align.vowel_onsets(morae, audio, rate, cfg))
            if japanese and cfg.align_morae else None,
        )
        if not notes:
            raise TetoRelayError("Nothing to sing in that line with these words.")
        ustx = stem.with_suffix(".ustx")
        wav = stem.with_suffix(".wav")
        write_ustx(notes, ustx, bank, cfg, expression_curves(notes, audio, rate, cfg))
        line["origin"] = self._render(ustx, wav, bank)
        self.finish_render(Job(captured_at=time.monotonic(), text="", wav_path=wav,
                               sung=bool(line.get("sung"))), cfg)
        return notes[0].shift

    def resing(self, folder: Path, line_no: int, texts: list[str]) -> dict:
        """Fix the words of one line of a finished take and sing it again.

        The line is built again from the corrected words with the timing and
        the pitch it was sung with (what analyse kept for it), rendered with
        the voicebank and tuning it was sung with, and the take is put
        together again. Returns the take's detail.
        """
        from . import takes as takes_mod

        folder = Path(folder)
        with takes_mod.edit_lock(folder):
            info = takes_mod.read_info(folder)
            line = next((p for p in info["phrases"] if int(p["n"]) == int(line_no)), None)
            if line is None:
                raise TetoRelayError(f"This take has no line {line_no}.")
            if not line.get("editable"):
                raise TetoRelayError("This line was recorded before words could be fixed.")
            words = [Word(w["text"], w["start"], w["end"])
                     for w in takes_mod.respace(line["words"], texts)]
            bank, settings, voice = self._line_voice(folder, line)
            self._sing_line(folder, line, words, bank, settings)
            if voice is None:
                line["voice"] = self._voice_record(bank, settings)
            line["words"] = [{"text": w.text, "start": w.start, "end": w.end} for w in words]
            line["text"] = " ".join(w.text for w in words)
            line["edited"] = True
            # New notes from the words: any note edits are gone.
            line["notes_edited"] = False
            takes_mod.assemble(folder, info)
            log.info("Sang line %d of %s again: %s", line_no, folder.name, line["text"])
        return takes_mod.detail(folder)

    def _voice_record(self, bank, settings: dict, tuning: str | None = None, tuning_id: str = "") -> dict:
        """A line's voice as take.json keeps it."""
        from . import presets

        if tuning is None:
            tuning = "Custom"
            try:
                for item in presets.listing()["styles"]:
                    if presets.matches(presets.get(item["id"]), self._voice_cfg(settings)):
                        tuning, tuning_id = item["name"], item["id"]
                        break
            except Exception:  # noqa: BLE001
                pass
        return {"bank": bank.key, "bank_name": self._bank_title(bank), "tuning": tuning,
                "tuning_id": tuning_id, "settings": dict(settings)}

    def revoice(self, folder: Path, line_nos=None, bank_key: str = "", tuning: str = "") -> dict:
        """Sing lines of a take again with another voicebank and/or tuning:
        the same singing (its words, its pitch, its timing), another voice.
        `line_nos` None is every line. `bank_key` "" keeps each line's own
        bank; `tuning` is a preset's id, "current" for the settings in force,
        or "" to keep each line's own. Lines are sung in order, each carrying
        the octave to the next, as they were when recorded. Lines recorded
        before their singing was kept cannot be: they are left as they are.
        Returns the take's detail, with "skipped" (how many were left)."""
        from . import presets
        from . import takes as takes_mod

        folder = Path(folder)
        new_bank = None
        if bank_key:
            new_bank = self._bank_by_key(bank_key)
            if new_bank is None:
                raise TetoRelayError("That voicebank isn't installed any more.")
        new_settings, tuning_name, tuning_id = None, None, ""
        if tuning == "current":
            now = self.voice_now()
            new_settings, tuning_name, tuning_id = now["settings"], now["tuning"], now["tuning_id"]
        elif tuning:
            try:
                preset = presets.get(tuning)
            except presets.PresetError as exc:
                raise TetoRelayError(str(exc)) from exc
            new_settings = presets.settings_of(preset)
            tuning_name, tuning_id = preset["name"], tuning
        with takes_mod.edit_lock(folder):
            info = takes_mod.read_info(folder)
            wanted = None if line_nos is None else {int(n) for n in line_nos}
            chosen = [p for p in info["phrases"] if p.get("n") is not None
                      and (wanted is None or int(p["n"]) in wanted)]
            if not chosen:
                raise TetoRelayError("There are no lines to sing again.")
            chosen.sort(key=lambda p: takes_mod.line_span(folder, p)[0])
            skipped, sung, shift = 0, 0, None
            state: dict = {}
            for line in chosen:
                if not line.get("editable"):
                    skipped += 1
                    continue
                bank, settings, voice = self._line_voice(folder, line)
                changes_bank = new_bank is not None and Path(new_bank.root) != Path(bank.root)
                bank = new_bank or bank
                settings = new_settings if new_settings is not None else settings
                words = [Word(w["text"], w["start"], w["end"]) for w in line.get("words") or []]
                shift = self._sing_line(folder, line, words, bank, settings, shift,
                                        keep_shift=not changes_bank, state=state)
                keep_name = new_settings is None and voice is not None
                line["voice"] = self._voice_record(
                    bank, settings,
                    tuning=voice.get("tuning", "Custom") if keep_name else tuning_name,
                    tuning_id=voice.get("tuning_id", "") if keep_name else tuning_id)
                if changes_bank:
                    # Sung for another voice: the pitch it is aimed at is that voice's.
                    context = dict(line.get("context") or {})
                    context["target_tone"], context["mora_floor"] = self._tone_of(bank, self._voice_cfg(settings))
                    context["previous_shift"] = shift
                    line["context"] = context
                line["notes_edited"] = False
                sung += 1
            if not sung:
                raise TetoRelayError("These lines were recorded before they could be sung again with another voice.")
            if wanted is None:
                info["voice"] = dict(chosen[0].get("voice") or {})
            takes_mod.assemble(folder, info)
            log.info("Sang %d line(s) of %s again with %s", sung, folder.name,
                     (new_bank.key if new_bank else "their own voicebank"))
        out = takes_mod.detail(folder)
        out["skipped"] = skipped
        return out

    def renote(self, folder: Path, line_no: int, notes: list) -> dict:
        """Sing one line of a finished take again from notes edited in the
        panel's note editor (takes.edit_notes): moved, stretched, split,
        joined, re-lyricked or deleted. Returns the take's detail."""
        from . import takes as takes_mod

        folder = Path(folder)
        with takes_mod.edit_lock(folder):
            info = takes_mod.read_info(folder)
            line = next((p for p in info["phrases"] if int(p["n"]) == int(line_no)), None)
            if line is None:
                raise TetoRelayError(f"This take has no line {line_no}.")
            stem = folder / takes_mod.LINES / f"{int(line_no):02d}"
            ustx = stem.with_suffix(".ustx")
            doc = takes_mod._read_doc(ustx)
            # The voice that sang the line sings its edited notes: its lyrics
            # are spelled for that bank (an English bank's words are silence
            # on a Japanese one), with its own tuning. If that bank is gone,
            # the one selected now.
            bank, settings, voice = self._line_voice(folder, line)
            cfg = self._voice_cfg(settings)
            doc = takes_mod.edit_notes(doc, notes, lambda lyric, previous: self._respell(lyric, previous, bank))
            track = doc["tracks"][0]
            track["singer"] = bank.root.name
            track["phonemizer"] = self.cfg.phonemizer or bank.phonemizer
            takes_mod._write_doc(doc, ustx)
            wav = stem.with_suffix(".wav")
            line["origin"] = self._render(ustx, wav, bank)
            self.finish_render(Job(captured_at=time.monotonic(), text="", wav_path=wav,
                                   sung=bool(line.get("sung"))), cfg)
            if voice is None:
                line["voice"] = self._voice_record(bank, settings)
            line["edited"] = line["notes_edited"] = True
            takes_mod.assemble(folder, info)
            log.info("Sang line %d of %s again from edited notes", line_no, folder.name)
        return takes_mod.detail(folder)

    def _bank_named(self, folder_name: str):
        """The installed voicebank whose folder is `folder_name` (how a
        .ustx names its singer), or None."""
        if not folder_name:
            return None
        for bank in [self.bank] + list(getattr(self, "banks", None) or []):
            if bank is not None and Path(bank.root).name == folder_name:
                return bank
        return None

    def _respell(self, lyric: str, previous: str | None, bank=None) -> str:
        """A lyric typed into the note editor, spelled as the voicebank needs
        it (what ustx.build_project does for the lyrics it writes): kana for a
        Japanese bank, and its own alias for the plain phonemizer."""
        bank = bank or self.bank
        if not bank.flavour.startswith("ja-"):
            return lyric
        lyric = bank.singable(lyric)
        if (self.cfg.phonemizer or bank.phonemizer).endswith("DefaultPhonemizer") and " " not in lyric:
            from .translit import vowel_of

            before = previous.split()[-1] if previous else None
            vowel = (vowel_of(before) or ("n" if before in ("ん", "ン") else None)) if before else None
            lyric = bank.alias_for(lyric, vowel)
        return lyric

    def _in_progress(self, change: int) -> None:
        """Phrases between the microphone and a finished render."""
        with self._take_lock:
            self._busy_phrases = max(0, getattr(self, "_busy_phrases", 0) + change)

    def start_take(self, music: Path | None = None, music_id: str = ""):
        """Start recording a take (teto_relay.takes).

        With `music` (a folder that holds a music track, teto_relay.music) it
        plays on the Hear myself device from its first sample, to sing along
        to, and the take's clock starts when that sample is heard, so a line
        sung on the beat is placed on the beat. Raises (and starts nothing) if
        the music cannot be played where only you hear it.
        """
        from .takes import Take

        if getattr(self, "_take", None) is not None:
            return self._take
        # Decoded before the lock: a song takes a second, and the lock is
        # the one every phrase passing through the pipeline takes.
        backing = self._backing_for(music) if music is not None else None
        with self._take_lock:
            if getattr(self, "_take", None) is None:
                take = Take(paths.data_dir() / "takes")
                take.delay = float(getattr(self.cfg, "studio_offset_ms", 0.0) or 0.0) / 1000.0
                if backing is not None:
                    take.started = backing.start()
                    take.music_from, take.music_id = Path(music), music_id
                    self._backing = backing
                self._take = take
                log.info("Recording a take%s", " over its music" if backing is not None else "")
                self._hands_free(True)
            return self._take

    # The music playing under the take being recorded (teto_relay.music.Backing).
    _backing = None

    def _backing_for(self, music: Path):
        from . import music as music_mod

        return music_mod.backing_for(music, self.cfg)

    def _stop_backing(self) -> None:
        backing, self._backing = getattr(self, "_backing", None), None
        if backing is not None:
            try:
                backing.stop()
            except Exception:  # noqa: BLE001 - the take is what matters
                log.debug("stopping the music failed", exc_info=True)

    def stop_take(self, wait: float = 30.0) -> dict | None:
        """Stop the take, let the phrases already said finish, write it out."""
        take = getattr(self, "_take", None)
        if take is None:
            return None
        take.stopped = time.monotonic()
        self._stop_backing()
        # A line still being sung hands-free is finished and kept.
        self._hands_free(False)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            idle = self.chunk_q.empty() and self.ustx_q.empty() and not getattr(self, "_busy_phrases", 0)
            if idle and not self._recording_now():
                break
            time.sleep(0.1)
        with self._take_lock:
            self._take = None
        return take.finish()

    def take_status(self) -> dict:
        take = getattr(self, "_take", None)
        if take is None:
            return {"recording": False}
        return {"recording": True, "seconds": round(take.seconds, 1), "phrases": len(take.phrases),
                "hands_free": bool(getattr(getattr(getattr(self, "_capture", None), "chunker", None),
                                           "hands_free", False))}

    def _recording_now(self) -> bool:
        """The talk key is still held, or a phrase has not reached analysis
        yet: it belongs to the take too."""
        chunker = getattr(getattr(self, "_capture", None), "chunker", None)
        return bool(getattr(chunker, "busy", getattr(chunker, "recording", False)))

    def _hands_free(self, on: bool) -> None:
        """While a take records, lines are heard without holding the key."""
        chunker = getattr(getattr(self, "_capture", None), "chunker", None)
        if hasattr(chunker, "set_hands_free"):
            chunker.set_hands_free(on)

    def _on_playback(self, job: "Job") -> None:
        """Called by the player the moment a job's audio starts playing."""
        timeline = job.timeline
        log.info("Latency %s", timeline.summary())
        self.last_stats.update(latency=timeline.as_dict(), behind=round(timeline.total, 2))
        append_csv(paths.data_dir() / "latency.csv", timeline, job.text)

    def _trim_output(self) -> None:
        """Keep out/ from growing without bound. Never raises.

        It runs in the workers' `finally:` blocks, so an exception here would
        escape the loop and end the thread - which is what happened when a file
        vanished between listing the folder and reading its timestamp. The
        relay then never produced audio again, with nothing on screen to say so.

        `keep_files` counts utterances, not files: each one leaves a .ustx and
        a .wav, so counting files kept only half as many as asked. Utterances
        still queued for rendering or playback are always kept.
        """
        try:
            keep = self.cfg.keep_files
            if keep <= 0:
                return
            keep = max(keep, 2 * self.cfg.queue_size + 2)
            newest: dict[str, float] = {}
            files: dict[str, list[Path]] = {}
            for path in self.cfg.out_path.glob("relay_*.*"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue  # already gone
                files.setdefault(path.stem, []).append(path)
                newest[path.stem] = max(mtime, newest.get(path.stem, 0.0))
            for stem in sorted(newest, key=newest.get, reverse=True)[keep:]:
                for stale in files[stem]:
                    try:
                        stale.unlink()
                    except OSError:
                        pass
        except Exception:  # noqa: BLE001 - housekeeping must not stop the relay
            log.debug("could not trim %s", self.cfg.out_dir, exc_info=True)


def run(cfg: Config | None = None) -> None:
    """Run until Ctrl-C. Used by `python -m teto_relay`."""
    relay = TetoRelay(cfg)
    relay.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        relay.stop()
