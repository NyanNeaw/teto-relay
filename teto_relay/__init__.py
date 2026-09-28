"""Teto Relay - real-time voice-to-UTAU pitch relay."""

import os as _os
import sys as _sys

from . import paths as _paths

# The packaged TetoRelay.exe (and pythonw) has no console: sys.stdout and
# sys.stderr are None. Anything that writes to them then fails - Hugging
# Face's download progress bar raised "'NoneType' object has no attribute
# 'write'" loading the Thai model, and on the next try hung the relay's
# restart for good; a first-time whisper download would have done the same.
for _name in ("stdout", "stderr"):
    if getattr(_sys, _name) is None:
        setattr(_sys, _name, open(_os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
# Progress bars go to a console nobody can see, and they are what broke.
_os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

__version__ = "0.3.0"

# Model caches default to the user profile on C:, which on this machine has
# under 2 GB free - whisper, torch and the 1.2 GB aligner would fill it. These
# must be set before torch or huggingface_hub are imported, so they live here
# rather than in a shell script the app might be started without. They go in
# the data folder (see teto_relay.paths), beside the config.
_CACHE = _paths.cache_dir()
for _var, _sub in (
    ("TORCH_HOME", "torch"),
    ("HF_HOME", "hf"),
    ("HUGGINGFACE_HUB_CACHE", "hf"),
    # pythainlp's Thai G2P model and corpora (teto_relay.thai).
    ("PYTHAINLP_DATA_DIR", "pythainlp"),
):
    if not _os.environ.get(_var):
        _path = _CACHE / _sub
        try:
            _path.mkdir(parents=True, exist_ok=True)
        except OSError:
            # An unwritable data folder must not stop the import: --doctor
            # and the error message box are what explain it to the user.
            continue
        _os.environ[_var] = str(_path)
