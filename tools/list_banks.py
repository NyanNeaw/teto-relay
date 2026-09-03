"""List every voicebank discovered under config.voicebank_root.

    .venv\\Scripts\\python.exe tools\\list_banks.py [search_root]
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teto_relay.config import Config  # noqa: E402
from teto_relay.voicebank import discover  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = Config.load()
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(cfg.voicebank_root)

    banks = discover(root)
    if not banks:
        print(f"No voicebanks found under {root}")
        return 1

    print(f"{len(banks)} voicebank(s) under {root}:\n")
    for b in banks:
        marker = " <- default" if b.key == cfg.voicebank else ""
        print(f"  {b}{marker}")
        print(f"      root:       {b.root}")
        print(f"      phonemizer: {b.phonemizer}")
        for s in b.subbanks:
            print(f"      sub {s.name!r}: {s.entry_count} entries")
            print(f"          e.g. {', '.join(s.sample_aliases[:6])}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
