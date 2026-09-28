"""Teto Relay - real-time voice-to-UTAU pitch relay."""

import os as _os

from . import paths as _paths

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
