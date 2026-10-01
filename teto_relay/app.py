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


class TetoRelay:
    """Owns the whole pipeline. `start()` is non-blocking; `stop()` joins."""

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
        self._player = Player(cfg, self.wav_q, out_dev.index, on_playback=self._on_playback)
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

        def stage(name: str, fn) -> None:
            """Run one warmup stage, timing it and reporting failure loudly.

            A stage that fails here does not stop the relay - it defers its cost
            to the first real utterance, which is how a 1.68s phrase once took
            14.30s. Cold load is ~9s for crepe and ~9s for the aligner, so a
            silent failure here is the one thing that reproduces that spike.
            """
            began_stage = time.monotonic()
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

        if self.cfg.use_alignment:
            stage("aligner", lambda: align.refine([Word("test", 0.0, 0.4)], probe, sample_rate, self.cfg))
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
            try:
                job = self.analyse(chunk)
                if job is not None:
                    _drop_oldest_put(self.ustx_q, job, "ustx")
            except Exception:
                log.exception("analysis failed for a %.2fs chunk", chunk.duration)

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

        words = self.transcriber.transcribe(chunk.audio, chunk.sample_rate)
        timeline.lap("asr")
        if not words:
            log.info("No speech recognised in a %.2fs chunk", chunk.duration)
            return None

        # Measure when each word was actually said. Whisper's timings
        # are systematically early, and both the note length and the
        # pitch window are taken from these spans.
        if self.cfg.use_alignment:
            words = align.refine(words, chunk.audio, chunk.sample_rate, self.cfg)
            timeline.lap("align")

        track = pitch_mod.track_f0(chunk.audio, chunk.sample_rate, self.cfg)
        timeline.lap("pitch")
        if not track.any_voiced:
            log.info("No voiced frames; skipping this utterance")
            return None

        notes = build_notes(
            words, track, self.cfg, self._octave_shift, self._target_tone,
            self._voice_baseline, self._japanese_lyrics(), self._mora_floor,
            self._singing_state, audio=chunk.audio, sample_rate=chunk.sample_rate,
            mora_timer=lambda morae: align.vowel_onsets(morae, chunk.audio, chunk.sample_rate, self.cfg),
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
        )
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

    def finish_render(self, job: "Job") -> None:
        """Effects on the rendered audio: the doubled lead, on sung phrases
        (or always, with double_when). Never costs the phrase."""
        amount = float(self.cfg.double_voice)
        when = self.cfg.double_when
        if amount <= 0 or when == "off" or not (job.sung or when == "always"):
            return
        try:
            import soundfile as sf

            from .performance import double_voice

            audio, rate = sf.read(str(job.wav_path), dtype="float32")
            sf.write(str(job.wav_path), double_voice(audio, rate, amount), rate)
        except Exception:  # noqa: BLE001
            log.debug("could not double %s", job.wav_path, exc_info=True)

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
                self.renderer.render(job.ustx_path, job.wav_path)
                self.finish_render(job)
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
                self._trim_output()

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
