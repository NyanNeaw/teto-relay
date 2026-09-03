"""Render backends, selected by `Config.renderer_backend`."""

from __future__ import annotations

import logging

from .base import RenderError, Renderer

log = logging.getLogger(__name__)


def make_renderer(cfg, bank=None) -> Renderer:
    """Build the configured backend, falling back to the null renderer.

    The fallback is deliberate: a failure to start the synthesis engine should
    degrade the relay to audible tones rather than silently killing the
    pipeline, and the log line says exactly what went wrong.
    """
    backend = (cfg.renderer_backend or "null").lower()

    if backend == "null":
        from .null import NullRenderer

        return NullRenderer(cfg)

    if backend == "openutau":
        try:
            from .openutau import OpenUtauRenderer

            return OpenUtauRenderer(cfg, bank)
        except Exception:
            log.exception("OpenUtau backend failed to start; falling back to tones")
            from .null import NullRenderer

            return NullRenderer(cfg)

    raise ValueError(f"unknown renderer_backend {cfg.renderer_backend!r} (expected 'null' or 'openutau')")


__all__ = ["Renderer", "RenderError", "make_renderer"]
