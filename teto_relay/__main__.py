"""Command-line entry point.

    python -m teto_relay                     # console, default bank
    python -m teto_relay --bank renzokubeta  # pick a voicebank
    python -m teto_relay --backend openutau  # real synthesis
    pythonw -m teto_relay --tray             # background, no console window
    python -m teto_relay --web               # browser control panel
    python -m teto_relay --doctor            # check the installation
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import Config, ConfigError
from .errors import TetoRelayError, describe

log = logging.getLogger("teto_relay")


def setup_logging(cfg: Config, verbose: bool, to_console: bool = True) -> None:
    handlers: list[logging.Handler] = []
    # Always log to file: under pythonw there is no console to print to.
    log_path = Path(cfg.log_file)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        handlers.append(file_handler)
    except OSError as exc:
        # A read-only folder must not stop the app; say so on the console.
        print(f"Could not write the log file {log_path} ({exc}); logging to the console only.",
              file=sys.stderr or sys.stdout)

    if to_console and sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        handlers.append(console)

    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True)
    # These are chatty and rarely interesting.
    for noisy in ("numba", "matplotlib", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def report(message: str, title: str = "Teto Relay") -> None:
    """Show a message to the person, wherever they can see it.

    Under pythonw (tray mode, or the packaged app) there is no console, so a
    startup error printed to stderr went nowhere and the app just vanished.
    """
    stream = sys.stderr if sys.stderr is not None else sys.stdout
    if stream is not None:
        print(f"\n{message}\n", file=stream)
        return
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
        except Exception:  # noqa: BLE001 - nothing else left to try
            pass


def log_startup(cfg: Config, config_path: Path | None) -> None:
    """One block at the top of every log that says what is running where."""
    import platform

    from . import __version__, paths

    log.info("Teto Relay %s on Python %s, %s", __version__, platform.python_version(),
             platform.platform())
    log.info("Data folder: %s%s", paths.data_dir(),
             " (portable)" if paths.portable() else "")
    log.info("Config: %s", config_path or paths.config_path())


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
    p.add_argument("--doctor", action="store_true",
                   help="check the installation and settings, then exit")
    p.add_argument("--no-browser", action="store_true",
                   help="with --web, do not open the panel in a browser")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        from . import __version__

        print(f"Teto Relay {__version__}")
        return 0

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        report(str(exc))
        return 2

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
    try:
        cfg.validate("the command line")
    except ConfigError as exc:
        report(str(exc))
        return 2

    setup_logging(cfg, args.verbose, to_console=not args.tray)
    log_startup(cfg, args.config)

    try:
        return _run(args, cfg)
    except KeyboardInterrupt:
        return 130
    except TetoRelayError as exc:
        log.error("%s", exc)
        report(str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 - last line of defence
        log.exception("unexpected error")
        report(describe(exc, cfg.log_file))
        return 1


def _run(args, cfg: Config) -> int:
    if args.doctor:
        from .doctor import run_doctor

        return run_doctor(cfg)

    if args.list_devices:
        from .devices import list_devices

        for d in list_devices():
            print(d)
        return 0

    if args.list_banks:
        from .voicebank import discover

        root = cfg.voicebank_path()
        banks = discover(root)
        print(f"Voicebanks in {root}:")
        for b in banks:
            print(f"  {b}")
        if not banks:
            print("  (none - a voicebank is a folder with an oto.ini and .wav samples)")
        return 0

    if args.web:
        from .webui import serve

        return serve(cfg, port=args.port, open_browser=not args.no_browser)

    if args.tray:
        from .tray import run_tray

        return run_tray(cfg)

    from .app import run

    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
