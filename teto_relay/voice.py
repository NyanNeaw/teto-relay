"""Voice conversion - your delivery, Teto's timbre.

The other mode reconstructs what you said as sung notes. This one does not
reconstruct anything: your recorded phrase goes through an RVC model and comes
back with her voice on it, keeping your rhythm, your stresses and your
intonation exactly. It cannot sing in tune for you; it can only be you, in her
voice.

**On fairseq.** The `rvc` package declares fairseq, which is sdist-only and
needs a compiler this machine does not have. It is imported by exactly two
files, neither on the inference path: a JIT helper, and `load_hubert()` - eight
lines that read a fairseq checkpoint. So a stub module satisfies the import and
the content encoder is loaded through `transformers` instead. See
`_install_fairseq_stub`.
"""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# The content encoder RVC v2 was trained against. Its repo ships only a .bin,
# which transformers 5 refuses to torch.load under torch 2.6 (CVE-2025-32434),
# so the weights are converted to safetensors once and cached.
CONTENTVEC_REPO = "lengyue233/content-vec-best"

# RVC works on 16 kHz input regardless of what the model outputs.
INPUT_RATE = 16000


def _contentvec_dir(cache: Path) -> Path:
    """A local ContentVec that transformers will load, prepared once."""
    local = cache / "contentvec"
    if (local / "model.safetensors").exists():
        return local

    import shutil

    import torch
    from huggingface_hub import hf_hub_download
    from safetensors.torch import save_file

    log.info("Preparing the content encoder (one-off, ~360 MB)...")
    local.mkdir(parents=True, exist_ok=True)
    weights = hf_hub_download(CONTENTVEC_REPO, "pytorch_model.bin")
    config = hf_hub_download(CONTENTVEC_REPO, "config.json")
    state = torch.load(weights, map_location="cpu", weights_only=True)
    state = {k: v.contiguous() for k, v in state.items() if hasattr(v, "contiguous")}
    save_file(state, str(local / "model.safetensors"))
    shutil.copyfile(config, local / "config.json")
    log.info("Content encoder ready at %s", local)
    return local


def _content_encoder(cache: Path, device: str):
    """The interface RVC's pipeline expects, backed by transformers.

    pipeline.py calls `extract_features(source=..., padding_mask=...,
    output_layer=12)` and takes element [0]; for v2 it never touches
    `final_proj`, which is why the missing-key warning on load is harmless.
    """
    import torch
    from transformers import HubertModel

    class ContentVec(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.model = HubertModel.from_pretrained(str(_contentvec_dir(cache)))
            self.model.eval()

        def extract_features(self, source, padding_mask=None, output_layer=12, **_):
            source = source.to(dtype=next(self.model.parameters()).dtype)
            out = self.model(source, output_hidden_states=True)
            return (out.hidden_states[output_layer],)

    return ContentVec().to(device).eval()


def _install_fairseq_stub() -> None:
    """Satisfy `from fairseq import checkpoint_utils` so `rvc` can be imported.

    Nothing calls through it - the encoder is set directly on the VC object -
    but the import happens at module scope in rvc.modules.vc.utils.
    """
    if "fairseq" in sys.modules:
        return
    fairseq = types.ModuleType("fairseq")
    checkpoint_utils = types.ModuleType("fairseq.checkpoint_utils")

    def _unavailable(*_args, **_kwargs):
        raise RuntimeError(
            "fairseq is not installed; teto_relay.voice loads the content "
            "encoder through transformers instead"
        )

    checkpoint_utils.load_model_ensemble_and_task = _unavailable
    utils = types.ModuleType("fairseq.utils")
    utils.index_put = None
    fairseq.checkpoint_utils = checkpoint_utils
    fairseq.utils = utils
    sys.modules.update(
        {"fairseq": fairseq, "fairseq.checkpoint_utils": checkpoint_utils,
         "fairseq.utils": utils}
    )


def to_float32(audio: np.ndarray) -> np.ndarray:
    """Normalise the pipeline's output, which comes back int16-scaled.

    Writing those samples as float clips every one of them, and the failure is
    quiet - the file plays, it just sounds destroyed.
    """
    audio = np.asarray(audio)
    if np.issubdtype(audio.dtype, np.integer):
        return (audio / 32768.0).astype(np.float32)
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak > 1.5:
        return (audio / 32768.0).astype(np.float32)
    return audio.astype(np.float32)


class VoiceConverter:
    """Loads the RVC model once and converts utterances with it."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._vc = None
        self.target_rate: int = 0
        self.version: str = ""

    @property
    def loaded(self) -> bool:
        return self._vc is not None

    def load(self) -> None:
        """Load the voice model and the content encoder. Idempotent."""
        if self._vc is not None:
            return

        model = Path(self.cfg.rvc_model)
        if not model.exists():
            raise RuntimeError(
                f"No RVC model at {model}. Set rvc_model to a .pth voice model, "
                "or switch Engine back to 'utau'."
            )

        _install_fairseq_stub()
        # `get_vc` resolves relative model names against these and walks
        # index_root looking for a matching .index. Unset, that walk is
        # os.walk(None), which raises "expected str, bytes or os.PathLike
        # object, not NoneType" from somewhere that looks nothing like a
        # missing environment variable.
        os.environ.setdefault("weight_root", str(model.parent))
        os.environ.setdefault("index_root", str(model.parent))
        os.environ.setdefault("rmvpe_root", str(model.parent))

        from rvc.modules.vc.modules import VC

        cache = Path(__file__).resolve().parent.parent / ".cache"
        vc = VC()
        vc.get_vc(str(model))
        vc.hubert_model = _content_encoder(cache, str(vc.config.device))

        self._vc = vc
        self.target_rate = int(vc.tgt_sr)
        self.version = str(vc.version)
        log.info(
            "Voice model ready: %s (%s, %d Hz, f0=%s) on %s",
            model.name, self.version, self.target_rate, vc.if_f0, vc.config.device,
        )

    def convert(self, audio: np.ndarray, sample_rate: int) -> tuple[np.ndarray, int]:
        """One utterance in, the same utterance in Teto's voice out."""
        if self._vc is None:
            self.load()
        vc = self._vc

        if sample_rate != INPUT_RATE:
            raise ValueError(f"voice conversion expects {INPUT_RATE} Hz, got {sample_rate}")

        audio = np.asarray(audio, dtype=np.float32)
        # The pipeline assumes a normalised input; a hot mic otherwise drives
        # the encoder into clipping.
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if peak > 0.95:
            audio = audio * (0.95 / peak)

        index = str(self.cfg.rvc_index) if self.cfg.rvc_index else ""
        if index and not Path(index).exists():
            log.warning("RVC index %s is missing; converting without it", index)
            index = ""

        method = (self.cfg.rvc_f0_method or "crepe").lower()
        if method == "rmvpe" and not (Path(self.cfg.rvc_model).parent / "rmvpe.pt").exists():
            log.warning(
                "rvc_f0_method is 'rmvpe' but rmvpe.pt is not beside the model; "
                "using crepe, which is already loaded"
            )
            method = "crepe"

        times = {"npy": 0.0, "f0": 0.0, "infer": 0.0}
        converted = vc.pipeline.pipeline(
            vc.hubert_model,
            vc.net_g,
            0,                       # speaker id; these models carry one voice
            audio,
            "<memory>",              # only used as a harvest cache key
            times,
            int(self.cfg.rvc_pitch),
            method,
            # faiss.read_index wants a str. Given a Path it raises inside the
            # pipeline, which swallows it and converts with no index at all -
            # quietly worse, never reported.
            index,
            float(self.cfg.rvc_index_rate),
            vc.if_f0,
            int(self.cfg.rvc_filter_radius),
            vc.tgt_sr,
            0,                       # no resampling here; playback handles it
            float(self.cfg.rvc_rms_mix_rate),
            vc.version,
            float(self.cfg.rvc_protect),
            None,
        )
        log.debug(
            "voice stages: features %.2fs, f0 %.2fs, infer %.2fs",
            times["npy"], times["f0"], times["infer"],
        )
        return to_float32(converted), self.target_rate

    def close(self) -> None:
        self._vc = None
