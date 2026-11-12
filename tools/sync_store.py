"""Keep the Store's files in step.

The Store's list and presets live in docs/store (published by GitHub Pages),
and a copy ships inside the app (teto_relay/web/store) for when there is no
internet. Run this after editing docs/store/catalog.json or adding a preset:

    python tools/sync_store.py

It writes the four built-in presets into docs/store/presets from the app's own
definitions (teto_relay.presets.BUILTIN), checks every preset and the catalog
the way the app will, and copies docs/store over the app's copy. The tests
fail if the two have drifted apart.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from teto_relay import presets, store  # noqa: E402

DOCS = ROOT / "docs" / "store"
BUNDLED = ROOT / "teto_relay" / "web" / "store"


def main() -> int:
    (DOCS / "presets").mkdir(parents=True, exist_ok=True)
    for key, preset in presets.BUILTIN.items():
        (DOCS / "presets" / f"{key}{presets.EXTENSION}").write_text(
            presets.dumps(presets.validate(preset)), encoding="utf-8", newline="\n")
    for path in sorted((DOCS / "presets").glob(f"*{presets.EXTENSION}")):
        presets.parse(path.read_bytes())  # raises, naming what is wrong
    listing = store.parse((DOCS / "catalog.json").read_bytes())
    files = {p.stem for p in (DOCS / "presets").glob(f"*{presets.EXTENSION}")}
    listed = {p["id"] for p in listing["presets"]}
    if files != listed:
        print(f"catalog.json and docs/store/presets disagree: {sorted(files ^ listed)}")
        return 1
    if BUNDLED.exists():
        shutil.rmtree(BUNDLED)
    shutil.copytree(DOCS, BUNDLED)
    print(f"Store in step: {len(listing['presets'])} presets, {len(listing['voicebanks'])} voicebanks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
