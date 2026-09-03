"""Command-line entry point.

    python -m teto_relay                     # console, default bank
    python -m teto_relay --bank renzokubeta  # pick a voicebank
    python -m teto_relay --backend openutau  # real synthesis
    pythonw -m teto_relay --tray             # background, no console window
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import Config


def setup_logging(cfg: Config, verbose: bool, to_console: bool = True) -> None:
    handlers: list[logging.Handler] = []
    # Always log to file: under pythonw there is no console to print to.
    file_handler = logging.handlers.RotatingFileHandler(
        cfg.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handlers.append(file_handler)

    if to_console and sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        handlers.append(console)

    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True)
    # These are chatty and rarely interesting.
    for noisy in ("numba", "matplotlib", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="teto_relay", description="Real-time voice-to-UTAU pitch relay")
    p.add_argument("--bank", help="voicebank key (see --list-banks)")
    p.add_argument("--backend", choices=["null", "openutau"], help="render backend")
    p.add_argument(
        "--model",
        help="whisper model: tiny.en, base.en, small.en, medium.en (bigger = more accurate, slower)",
    )
    p.add_argument("--ptt", dest="capture_mode", action="store_const", const="ptt",
                   help="push-to-talk: hold a key to record (default)")
    p.add_argument("--vad", dest="capture_mode", action="store_const", const="vad",
                   help="automatic: split on silence")
    p.add_argument("--key", dest="ptt_key", help="push-to-talk key, e.g. f8, ctrl_r, space")
    p.add_argument("--input", dest="input_device", help="microphone name substring")
    p.add_argument("--output", dest="output_device", help="output device name substring")
    p.add_argument("--list-banks", action="store_true", help="list voicebanks and exit")
    p.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    p.add_argument("--tray", action="store_true", help="run with a system-tray icon")
    p.add_argument("--web", action="store_true", help="open the local control panel instead of running headless")
    p.add_argument("--port", type=int, default=8765, help="control panel port (default 8765)")
    p.add_argument("--config", type=Path, help="path to a config.json")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)

    if args.bank:
        cfg.voicebank = args.bank
    if args.backend:
        cfg.renderer_backend = args.backend
    if args.model:
        cfg.whisper_model = args.model
    if args.capture_mode:
        cfg.capture_mode = args.capture_mode
    if args.ptt_key:
        cfg.ptt_key = args.ptt_key
    if args.input_device:
        cfg.input_device = args.input_device
    if args.output_device:
        cfg.output_device = args.output_device

    setup_logging(cfg, args.verbose, to_console=not args.tray)

    if args.list_devices:
        from .devices import list_devices

        for d in list_devices():
            print(d)
        return 0

    if args.list_banks:
        from .voicebank import discover

        for b in discover(cfg.voicebank_root):
            print(b)
        return 0

    if args.web:
        from .webui import serve

        return serve(cfg, port=args.port)

    if args.tray:
        from .tray import run_tray

        return run_tray(cfg)

    from .app import run

    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
