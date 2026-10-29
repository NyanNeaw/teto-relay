"""Analysis that starts while the phrase is still being recorded.

Everything that needs the words waits for whisper, which needs the whole
phrase. Pitch does not: crepe judges each 10 ms frame on its own, from the
64 ms of audio around it, and only joins the frames up (Viterbi decoding)
at the end. So while the talk key is held, the frames already heard are
worked out on a thread of their own; at the release only the last few
frames and the decoding are left. The result is the same track
`pitch.track_f0` would give (tests/test_production.py checks it), and its
CPU time no longer competes with whisper and the aligner after the release.

Only crepe is done this way; anything else - pyin, a phrase shorter than
one crepe window, a frame that failed - falls back to `pitch.track_f0`.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from . import pitch as pitch_mod

log = logging.getLogger(__name__)

#: crepe's analysis window, in samples at 16 kHz: frame i covers
#: [i*hop - 512, i*hop + 512) of the phrase (torchcrepe pads 512 each side).
WINDOW = 1024
HALF = WINDOW // 2
#: torchcrepe decodes in batches of this many frames; decoding in the same
#: blocks keeps the result identical to `pitch.track_f0`.
DECODE_BLOCK = 512
#: Work on the frames heard so far once this much new audio has arrived.
STEP_SECONDS = 0.25


class LivePitch:
    """Pitch for one phrase, worked out as its audio arrives."""

    def __init__(self, cfg, sample_rate: int):
        self.cfg = cfg
        self.sample_rate = sample_rate
        self.hop = max(1, sample_rate // 100)
        self.device, self.model = pitch_mod.crepe_device_model(cfg)
        self._pieces: list[np.ndarray] = []
        self._samples = 0
        self._probabilities: list = []  # (frames, 360) tensors, in order
        self._frames_done = 0
        self._since_step = 0
        self._failed = False
        self._closing = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="live-pitch", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ audio thread
    def feed(self, audio: np.ndarray) -> None:
        """Add audio to the phrase (called from the audio thread: cheap)."""
        if audio is None or not len(audio):
            return
        with self._lock:
            self._pieces.append(np.asarray(audio, dtype=np.float32))
            self._samples += len(audio)
            self._since_step += len(audio)
            if self._since_step >= STEP_SECONDS * self.sample_rate:
                self._since_step = 0
                self._wake.set()

    def cancel(self) -> None:
        """The phrase was dropped (too short): stop working on it."""
        self._closing = True
        self._wake.set()

    # ----------------------------------------------------------- its own thread
    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._closing:
                return
            try:
                self._advance(self._audio(), final=False)
            except Exception:  # noqa: BLE001 - finish() falls back to track_f0
                log.debug("live pitch failed; the phrase is tracked after the release", exc_info=True)
                self._failed = True
                return

    def _audio(self) -> np.ndarray:
        with self._lock:
            if len(self._pieces) > 1:
                self._pieces = [np.concatenate(self._pieces)]
            return self._pieces[0] if self._pieces else np.empty(0, dtype=np.float32)

    def _advance(self, audio: np.ndarray, final: bool) -> None:
        """Work out every frame whose window is complete (all of them if final)."""
        import torch
        import torchcrepe

        if final:
            target = 1 + len(audio) // self.hop
        else:
            # Frame i is complete once sample i*hop + HALF has arrived.
            target = 0 if len(audio) < HALF else (len(audio) - HALF) // self.hop + 1
        if target <= self._frames_done:
            return
        first = self._frames_done
        # Padded like torchcrepe: HALF zeros before; after, only on the final pass.
        padded = np.concatenate([np.zeros(HALF, np.float32), audio,
                                 np.zeros(HALF if final else 0, np.float32)])
        start, end = first * self.hop, (target - 1) * self.hop + WINDOW
        with torch.no_grad():
            tensor = torch.from_numpy(np.ascontiguousarray(padded[start:end]))
            frames = torch.nn.functional.unfold(tensor[None, None, None, :], kernel_size=(1, WINDOW),
                                                stride=(1, self.hop))
            frames = frames.transpose(1, 2).reshape(-1, WINDOW).to(self.device)
            frames -= frames.mean(dim=1, keepdim=True)
            frames /= torch.max(torch.tensor(1e-10, device=frames.device), frames.std(dim=1, keepdim=True))
            probabilities = torchcrepe.infer(frames, self.model, self.device, embed=False)
        self._probabilities.append(probabilities)
        self._frames_done = target

    # ------------------------------------------------------------- the release
    def finish(self, audio: np.ndarray):
        """The phrase's F0Track, or None to have `pitch.track_f0` do it."""
        self._closing = True
        self._wake.set()
        self._thread.join(timeout=5.0)
        if self._failed or self._thread.is_alive() or len(audio) < WINDOW:
            return None
        heard = self._audio()
        if len(heard) != len(audio) or not np.array_equal(heard, audio):
            log.debug("live pitch heard different audio from the phrase; tracking it afresh")
            return None
        try:
            import torch
            import torchcrepe

            self._advance(audio, final=True)
            probabilities = torch.cat(self._probabilities, 0)  # (frames, 360)
            f0s, periodicities = [], []
            with torch.no_grad():
                for i in range(0, probabilities.shape[0], DECODE_BLOCK):
                    block = probabilities[i:i + DECODE_BLOCK].reshape(1, -1, torchcrepe.PITCH_BINS).transpose(1, 2)
                    f0, periodicity = torchcrepe.postprocess(block, self.cfg.f0_min, self.cfg.f0_max,
                                                             torchcrepe.decode.viterbi, False, True)
                    f0s.append(f0)
                    periodicities.append(periodicity)
            f0 = torch.cat(f0s, 1)[0].detach().cpu().numpy().astype(float)
            periodicity = torch.cat(periodicities, 1)[0].detach().cpu().numpy()
        except Exception:  # noqa: BLE001
            log.debug("live pitch could not finish; tracking the phrase afresh", exc_info=True)
            return None
        return pitch_mod.crepe_track(f0, periodicity, self.sample_rate, self.cfg,
                                     f"crepe/{self.model}@{self.device} (live)")
