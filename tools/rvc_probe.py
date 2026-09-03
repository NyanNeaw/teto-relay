"""Standalone RVC v2 inference without fairseq.

fairseq is imported by exactly one function in the `rvc` package - load_hubert,
eight lines that read a fairseq checkpoint. Everything that actually does the
conversion (the synthesiser, the pipeline) never touches it. So a stub module
satisfies the import and hands back a ContentVec encoder loaded through
transformers instead, which needs no compiler.

Usage: rvc_probe.py <input.wav> <output.wav>
"""
import os, sys, types, time
from pathlib import Path

# Model caches to D: - C: has under 3 GB free.
CACHE = Path(r"D:\Claude\teto-relay\.cache")
CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(CACHE / "hf"))
os.environ.setdefault("TORCH_HOME", str(CACHE / "torch"))
os.environ.setdefault("weight_root", r"D:\Claude\Kasane%20Teto")
os.environ.setdefault("index_root", r"D:\Claude\Kasane%20Teto")

import numpy as np
import torch

MODEL = r"D:\Claude\Kasane%20Teto\Kasane Teto.pth"
INDEX = r"D:\Claude\Kasane%20Teto\added_IVF1367_Flat_nprobe_1_Kasane Teto_v2.index"
CONTENTVEC = "lengyue233/content-vec-best"


def contentvec_dir() -> Path:
    """A local ContentVec that transformers will load, prepared once.

    The upstream repo ships only pytorch_model.bin, and transformers 5 refuses
    to torch.load a .bin unless torch >= 2.6 (CVE-2025-32434) - we are on 2.5.1
    with CUDA 12.1 and are not moving it for this. Converting the weights to
    safetensors once sidesteps the restriction without downgrading anything,
    and leaves a self-contained model directory on D:.
    """
    local = CACHE / "contentvec"
    if (local / "model.safetensors").exists():
        return local
    from huggingface_hub import hf_hub_download
    from safetensors.torch import save_file
    import shutil

    print(f"  preparing {CONTENTVEC} -> {local}", flush=True)
    local.mkdir(parents=True, exist_ok=True)
    weights = hf_hub_download(CONTENTVEC, "pytorch_model.bin")
    config = hf_hub_download(CONTENTVEC, "config.json")
    state = torch.load(weights, map_location="cpu", weights_only=True)
    state = {k: v.contiguous() for k, v in state.items() if isinstance(v, torch.Tensor)}
    save_file(state, str(local / "model.safetensors"))
    shutil.copyfile(config, local / "config.json")
    print(f"  converted {len(state)} tensors to safetensors", flush=True)
    return local


class ContentVec(torch.nn.Module):
    """The interface RVC's pipeline expects, backed by transformers.

    pipeline.py calls `extract_features(source=..., padding_mask=...,
    output_layer=12)` and, for v2, takes element [0] of the result. That is the
    hidden state of layer 12 - exactly what `output_hidden_states` gives.
    """

    def __init__(self):
        super().__init__()
        from transformers import HubertModel

        began = time.monotonic()
        self.model = HubertModel.from_pretrained(str(contentvec_dir()))
        self.model.eval()
        print(f"  content encoder ready in {time.monotonic()-began:.1f}s", flush=True)

    def extract_features(self, source, padding_mask=None, output_layer=12, **kw):
        source = source.to(dtype=next(self.model.parameters()).dtype)
        out = self.model(source, output_hidden_states=True)
        return (out.hidden_states[output_layer],)


def install_fairseq_stub():
    """Satisfy `from fairseq import checkpoint_utils` with a ContentVec loader."""
    fairseq = types.ModuleType("fairseq")
    checkpoint_utils = types.ModuleType("fairseq.checkpoint_utils")

    def load_model_ensemble_and_task(paths, suffix=""):
        return [ContentVec()], None, None

    checkpoint_utils.load_model_ensemble_and_task = load_model_ensemble_and_task
    utils = types.ModuleType("fairseq.utils")
    utils.index_put = None  # only the JIT path wants this
    fairseq.checkpoint_utils = checkpoint_utils
    fairseq.utils = utils
    sys.modules["fairseq"] = fairseq
    sys.modules["fairseq.checkpoint_utils"] = checkpoint_utils
    sys.modules["fairseq.utils"] = utils


def main() -> int:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    install_fairseq_stub()

    from rvc.modules.vc import modules as vc_modules
    from rvc.modules.vc.modules import VC

    # rvc.lib.audio decodes through PyAV, whose open() no longer takes "rb".
    # The relay will hand us a numpy array anyway, so this path is dead weight -
    # replace it with a plain soundfile read.
    def load_audio(path, sr):
        import librosa

        audio, _ = librosa.load(str(path), sr=sr, mono=True)
        return audio.astype(np.float32)

    vc_modules.load_audio = load_audio

    print(f"torch {torch.__version__} cuda={torch.cuda.is_available()}")
    vc = VC()
    print(f"config device={vc.config.device} is_half={vc.config.is_half}")

    began = time.monotonic()
    vc.get_vc(MODEL)
    print(f"voice model loaded in {time.monotonic()-began:.1f}s "
          f"(tgt_sr={vc.tgt_sr} if_f0={vc.if_f0} version={vc.version})")

    began = time.monotonic()
    # crepe rather than rmvpe: torchcrepe is already installed and needs no
    # extra 180 MB download.
    tgt_sr, audio, times, err = vc.vc_single(
        # faiss.read_index takes a str; a Path raises inside the pipeline, which
        # swallows it and silently converts with no index at all.
        sid=0, input_audio_path=src, f0_up_key=12, f0_method="crepe",
        index_file=str(INDEX), index_rate=0.75, filter_radius=3,
        resample_sr=0, rms_mix_rate=0.25, protect=0.33, hubert_path="<stubbed>",
    )
    if err:
        print("FAILED:\n" + err)
        return 1

    elapsed = time.monotonic() - began
    import soundfile as sf

    # The pipeline hands back int16-scaled samples. Writing those as float
    # clips everything to +/-1; convert on the way out.
    audio = np.asarray(audio)
    peak = float(np.abs(audio).max())
    if peak > 1.5:
        audio = (audio / 32768.0).astype(np.float32)
    sf.write(str(dst), audio, tgt_sr, subtype="PCM_16")

    seconds = len(audio) / tgt_sr
    print(f"\nconverted {seconds:.2f}s of audio in {elapsed:.2f}s "
          f"({elapsed/max(seconds,1e-6):.2f}x realtime)")
    print(f"stage times: " + ", ".join(f"{k} {v:.2f}s" for k, v in times.items()))
    print(f"wrote {dst}  {tgt_sr} Hz  raw peak {peak:.0f}  "
          f"final peak {float(np.abs(audio).max()):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
