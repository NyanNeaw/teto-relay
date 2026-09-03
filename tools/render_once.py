"""Render one .ustx through a chosen backend - the stage 5 smoke test.

    .venv\\Scripts\\python.exe tools\\render_once.py --backend openutau --bank english
    .venv\\Scripts\\python.exe tools\\render_once.py --play
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teto_relay.config import Config  # noqa: E402
from teto_relay.notes import Note  # noqa: E402
from teto_relay.render import make_renderer  # noqa: E402
from teto_relay.ustx import write_ustx  # noqa: E402
from teto_relay.voicebank import discover, select  # noqa: E402

PHRASES = {
    "en-cvvc": ["hello", "there", "teto"],
    "ja-vcv": ["あ", "い", "う"],
    "ja-cv": ["あ", "い", "う"],
    "unknown": ["あ"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Render one utterance")
    parser.add_argument("--bank")
    parser.add_argument("--backend", choices=["null", "openutau"])
    parser.add_argument("--ustx", type=Path, help="render an existing .ustx instead of a sample")
    parser.add_argument("--play", action="store_true", help="play the result to the configured output")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    cfg = Config.load()
    if args.bank:
        cfg.voicebank = args.bank
    if args.backend:
        cfg.renderer_backend = args.backend

    bank = select(discover(cfg.voicebank_root), cfg.voicebank)
    print(f"bank    : {bank}")
    print(f"backend : {cfg.renderer_backend}")

    if args.ustx:
        ustx_path = args.ustx
    else:
        words = PHRASES.get(bank.flavour, PHRASES["unknown"])
        tones = [62, 65, 69][: len(words)]
        notes = [
            Note(lyric=w, start=i * 0.5, end=i * 0.5 + 0.45, tone=t)
            for i, (w, t) in enumerate(zip(words, tones))
        ]
        ustx_path = cfg.out_path / "render_once.ustx"
        write_ustx(notes, ustx_path, bank, cfg)
        print(f"ustx    : {ustx_path}")

    renderer = make_renderer(cfg, bank)
    print(f"renderer: {renderer.name}")
    wav = renderer.render(Path(ustx_path), Path(ustx_path).with_suffix(".wav"))
    print(f"wav     : {wav}")

    if args.play:
        from teto_relay.devices import resolve_output
        from teto_relay.playback import play_once

        device = resolve_output(cfg)
        print(f"playing to {device}")
        play_once(wav, device.index, cfg.playback_gain)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
