"""Round-trip a generated .ustx through OpenUtau's own reader.

This checks the one real gamble in stage 4: that omitting the big `expressions`
block is safe because OpenUtau repopulates defaults on load.

    .venv\\Scripts\\python.exe tools\\validate_ustx.py [existing.ustx]
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teto_relay import dotnet  # noqa: E402
from teto_relay.config import Config  # noqa: E402
from teto_relay.notes import Note  # noqa: E402
from teto_relay.ustx import write_ustx  # noqa: E402
from teto_relay.voicebank import discover, select  # noqa: E402


def sample_notes() -> list[Note]:
    """A short rising phrase with a bent first note."""
    return [
        Note(lyric="hello", start=0.00, end=0.42, tone=62, contour=[(-40.0, 0.0), (100.0, 35.0), (300.0, -20.0)]),
        Note(lyric="there", start=0.42, end=0.90, tone=65),
        Note(lyric="teto", start=0.95, end=1.60, tone=69),
    ]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = Config.load()

    if len(sys.argv) > 1:
        path = Path(sys.argv[1]).resolve()
        print(f"Validating existing file: {path}")
    else:
        banks = discover(cfg.voicebank_root)
        bank = select(banks, cfg.voicebank)
        path = (cfg.out_path / "validate_sample.ustx").resolve()
        write_ustx(sample_notes(), path, bank, cfg)
        print(f"Generated {path} using bank {bank.key!r} ({bank.flavour})")

    print("\n--- loading through OpenUtau.Core ---")
    dotnet.start(cfg.openutau_dir)

    from OpenUtau.Core.Format import Formats, Ustx  # noqa: E402

    project = None
    for label, fn in (("Ustx.Load", lambda: Ustx.Load(str(path))),
                      ("Formats.ReadProject", lambda: Formats.ReadProject(str(path)))):
        try:
            project = fn()
            print(f"  [ OK ] {label} returned {type(project).__name__}")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {label}: {type(exc).__name__}: {exc}")

    if project is None:
        print("\nVERDICT: OpenUtau could NOT read the generated file.")
        return 1

    # Report what OpenUtau made of it, especially the parts we did not write.
    try:
        parts = list(project.voiceParts)
        notes = list(parts[0].notes) if parts else []
        print(f"  bpm={project.bpm}  resolution={project.resolution}")
        print(f"  tracks={len(list(project.tracks))}  voice_parts={len(parts)}  notes={len(notes)}")
        for n in notes[:8]:
            print(f"    note: lyric={n.lyric!r} tone={n.tone} position={n.position} duration={n.duration}")
        exps = list(project.expressions.Keys) if project.expressions is not None else []
        print(f"  expressions auto-populated: {len(exps)} -> {sorted(exps)[:10]}")
        track = list(project.tracks)[0]
        print(f"  track singer field: {track.singer!r}")
        print(f"  track phonemizer:   {track.phonemizer!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] could not introspect the project: {type(exc).__name__}: {exc}")

    print("\nVERDICT: OpenUtau read the generated .ustx successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
