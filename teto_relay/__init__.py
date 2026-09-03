"""Teto Relay - real-time voice-to-UTAU pitch relay."""

import os as _os
from pathlib import Path as _Path

__version__ = "0.1.0"

# Model caches default to the user profile on C:, which on this machine has
# under 2 GB free - whisper, torch and the 1.2 GB aligner would fill it. These
# must be set before torch or huggingface_hub are imported, so they live here
# rather than in a shell script the app might be started without.
_CACHE = _Path(__file__).resolve().parent.parent / ".cache"
for _var, _sub in (
    ("TORCH_HOME", "torch"),
    ("HF_HOME", "hf"),
    ("HUGGINGFACE_HUB_CACHE", "hf"),
):
    if not _os.environ.get(_var):
        _path = _CACHE / _sub
        _path.mkdir(parents=True, exist_ok=True)
        _os.environ[_var] = str(_path)
