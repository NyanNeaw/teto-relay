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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import align
from . import devices as devices_mod
from . import japanese as jp_mod
from . import pitch as pitch_mod
from . import voicebank as vb_mod
from .capture import MicCapture, calibrate_threshold, make_chunker
from .config import Config
from .hotkey import PushToTalkListener
from .notes import build_notes
from .render import make_renderer
from .playback import Player
from .stt import Transcriber, Word
from .ustx import write_ustx
from .voice import VoiceConverter

log = logging.getLogger(__name__)


@dataclass
class Job:
    """One utterance travelling through the pipeline, with its timings."""

    captured_at: float  # when the utterance closed at the microphone
    text: str
    # Voice mode never writes a project file, so this is optional.
    ustx_path: Path | None = None
    wav_path: Path | None = None
    analyse_seconds: float = 0.0
    render_seconds: float = 0.0

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


class TetoRelay:
    """Owns the whole pipeline. `start()` is non-blocking; `stop()` joins."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config.load()
        self.banks = vb_mod.discover(self.cfg.voicebank_root)
        self.bank = vb_mod.select(self.banks, self.cfg.voicebank)

        self.chunk_q: queue.Queue = queue.Queue(maxsize=self.cfg.queue_size)
        self.ustx_q: queue.Queue = queue.Queue(maxsize=self.cfg.queue_size)
        self.wav_q: queue.Queue = queue.Queue(maxsize=self.cfg.queue_size)

        self.engine = (self.cfg.mode or "utau").lower()
        self.transcriber = Transcriber(self.cfg)
        # Voice mode never synthesises notes, so the OpenUtau host - which
        # starts CoreCLR and loads a singer - is not built at all.
        self.renderer = make_renderer(self.cfg, self.bank) if self.engine != "voice" else None
        self.converter = VoiceConverter(self.cfg) if self.engine == "voice" else None

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
        # The pitch this voicebank was recorded at; rendering near it keeps the
        # voice's body, which is what makes it sound sung rather than breathy.
        self._target_tone: float = float(self.cfg.target_tone) or vb_mod.estimate_pitch(
            self.bank, self.cfg
        )
        # The shortest note this bank can sing, measured from its oto.
        self._mora_floor: float = vb_mod.mora_floor(self.bank, self.cfg)
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
        cfg = self.cfg
        # Nothing downstream reads `mode`, so a relay configured for voice
        # conversion would quietly run the UTAU pipeline instead and look like
        # it was working. Refuse instead of lying about it.
        engine = self.engine
        if engine not in ("utau", "voice"):
            raise RuntimeError(
                f"mode={engine!r} is not a thing - use 'utau' to sing your "
                "speech as notes, or 'voice' to convert it to Teto's timbre."
            )
        if engine == "voice":
            # No voicebank, no phonemizer, no notes: the model is the voice.
            log.info("Engine:    voice conversion (your delivery, Teto's timbre)")
            log.info("  model:   %s", Path(self.cfg.rvc_model).name)
            log.info("  pitch:   %+d semitones via %s", self.cfg.rvc_pitch,
                     self.cfg.rvc_f0_method)
            workers = [("convert", self._convert_loop)]
        else:
            log.info("Engine:    utau (your speech, sung as notes)")
            log.info("Voicebank: %s", self.bank)
            # All three Teto banks share one character.txt name, so the folder
            # is the only unambiguous way to tell which one is actually loaded.
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
        self._player = Player(cfg, self.wav_q, out_dev.index)
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

        if self.engine == "voice":
            # Voice mode needs none of whisper, the aligner or the lyric
            # dictionary. It does need the voice model and the content encoder,
            # and it reuses the torchcrepe warmed just above - which is most of
            # why conversion is quick once running.
            stage("voice model", self.converter.load)
            stage("voice warmup", lambda: self.converter.convert(probe, sample_rate))
            elapsed = time.monotonic() - began
            if failed:
                log.warning(
                    "Warmed up in %.1fs, but %s did not warm (%s) - the first "
                    "utterance will be slow", elapsed, " and ".join(failed),
                    ", ".join(timings) or "nothing warmed",
                )
            else:
                log.info("Warmed up voice conversion in %.1fs (%s)", elapsed,
                         ", ".join(timings))
            return

        if self.cfg.use_alignment:
            stage("aligner", lambda: align.refine([Word("test", 0.0, 0.4)], probe, sample_rate, self.cfg))

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
        log.info("Stopping...")
        self._stop.set()
        if self._hotkey:
            self._hotkey.stop()
        if self._capture:
            self._capture.stop()
        if self._player:
            self._player.stop()
        for t in self._threads:
            t.join(timeout=2.0)
        if self._capture:
            self._capture.join(timeout=2.0)
        if self._player:
            self._player.join(timeout=2.0)
        for closable in (self.renderer, self.converter):
            if closable is None:
                continue
            try:
                closable.close()
            except Exception:
                log.debug("%s close failed", type(closable).__name__, exc_info=True)
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
        self.bank = vb_mod.select(self.banks, key)
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

            began = time.monotonic()
            # Per-stage timings, so a slow utterance says which stage was slow.
            # Steady state is roughly stt 2.0s, align 0.06s, pitch 0.2s; a stage
            # an order of magnitude above that is a model loading late.
            marks: dict[str, float] = {}

            def mark(name: str, since: float) -> float:
                now = time.monotonic()
                marks[name] = now - since
                return now

            try:
                step = began
                words = self.transcriber.transcribe(chunk.audio, chunk.sample_rate)
                step = mark("stt", step)
                if not words:
                    log.info("No speech recognised in a %.2fs chunk", chunk.duration)
                    continue

                # Measure when each word was actually said. Whisper's timings
                # are systematically early, and both the note length and the
                # pitch window are taken from these spans.
                words = align.refine(words, chunk.audio, chunk.sample_rate, self.cfg)
                step = mark("align", step)

                track = pitch_mod.track_f0(chunk.audio, chunk.sample_rate, self.cfg)
                step = mark("pitch", step)
                if not track.any_voiced:
                    log.info("No voiced frames; skipping this utterance")
                    continue

                notes = build_notes(
                    words, track, self.cfg, self._octave_shift, self._target_tone,
                    self._voice_baseline, self._japanese_lyrics(), self._mora_floor,
                )
                if not notes:
                    continue
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
                stamp = datetime.now().strftime("%H%M%S_%f")[:-3]
                path = self.cfg.out_path / f"relay_{stamp}.ustx"
                write_ustx(notes, path, self.bank, self.cfg)
                mark("notes+ustx", step)

                job = Job(
                    captured_at=chunk.captured_at,
                    text=self.last_text,
                    ustx_path=path,
                    analyse_seconds=time.monotonic() - began,
                )
                log.info(
                    "Analysed %.2fs of speech in %.2fs [%s] via %s",
                    chunk.duration,
                    job.analyse_seconds,
                    " ".join(f"{name} {secs:.2f}s" for name, secs in marks.items()),
                    track.method or "unknown",
                )
                self.last_stats = {
                    "speech": round(chunk.duration, 2),
                    "analyse": round(job.analyse_seconds, 2),
                    "method": track.method or "unknown",
                    **{name: round(secs, 2) for name, secs in marks.items()},
                }
                _drop_oldest_put(self.ustx_q, job, "ustx")
            except Exception:
                log.exception("analysis failed for a %.2fs chunk", chunk.duration)

    def _convert_loop(self) -> None:
        """Chunk -> RVC -> .wav on disk. The whole of voice mode.

        One worker instead of analyse+render: there is nothing to transcribe and
        nothing to synthesise, so the utterance goes straight through the model.
        """
        while not self._stop.is_set():
            try:
                chunk = self.chunk_q.get(timeout=0.2)
            except queue.Empty:
                continue

            began = time.monotonic()
            try:
                audio, rate = self.converter.convert(chunk.audio, chunk.sample_rate)
                if audio.size == 0:
                    log.warning("Voice conversion returned nothing for a %.2fs chunk",
                                chunk.duration)
                    continue

                import soundfile as sf

                stamp = datetime.now().strftime("%H%M%S_%f")[:-3]
                path = self.cfg.out_path / f"relay_{stamp}.wav"
                sf.write(str(path), audio, rate, subtype="PCM_16")

                elapsed = time.monotonic() - began
                self.last_text = f"{chunk.duration:.1f}s in your voice"
                self.last_source = ""
                self.last_kana = ""
                self.last_notes = []
                self.last_stats = {
                    "speech": round(chunk.duration, 2),
                    "analyse": round(elapsed, 2),
                    "method": self.cfg.rvc_f0_method or "crepe",
                }
                job = Job(
                    captured_at=chunk.captured_at,
                    text=self.last_text,
                    wav_path=path,
                    analyse_seconds=elapsed,
                )
                log.info(
                    "Converted %.2fs of speech in %.2fs (%.2fx realtime) -> %d Hz",
                    chunk.duration, elapsed, elapsed / max(chunk.duration, 1e-6), rate,
                )
                _drop_oldest_put(self.wav_q, job, "wav")
            except Exception:
                log.exception("voice conversion failed for a %.2fs chunk", chunk.duration)
            finally:
                self._trim_output()

    def _render_loop(self) -> None:
        """.ustx -> .wav."""
        while not self._stop.is_set():
            try:
                job = self.ustx_q.get(timeout=0.2)
            except queue.Empty:
                continue

            began = time.monotonic()
            try:
                job.wav_path = job.ustx_path.with_suffix(".wav")
                self.renderer.render(job.ustx_path, job.wav_path)
                job.render_seconds = time.monotonic() - began
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

    def _trim_output(self) -> None:
        """Keep out/ from growing without bound."""
        keep = self.cfg.keep_files
        if keep <= 0:
            return
        files = sorted(self.cfg.out_path.glob("relay_*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass


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
