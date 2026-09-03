"""The renderer seam.

Everything upstream of this file is engine-agnostic: the pipeline hands over a
.ustx path and gets back a .wav path. That is what let stages 1-4 and 6 be
built and tested before the OpenUtau route was proven.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Renderer(Protocol):
    name: str

    def render(self, ustx_path: Path, out_wav: Path) -> Path:
        """Synthesise `ustx_path` into `out_wav` and return the wav path."""
        ...

    def close(self) -> None:
        """Release any engine resources."""
        ...


class RenderError(RuntimeError):
    """Raised when a backend cannot produce audio for a project."""
