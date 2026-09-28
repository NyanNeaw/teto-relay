"""Write the portable zip straight from dist/TetoRelay.

build.ps1 used to copy the 3.4 GB app folder to a staging folder and zip that
with Compress-Archive, which needs the app's size again in free space on top
of the zip - on the test PC the disk filled up half-way through. Here every
file is streamed into the zip from where it already is.

    python packaging/portable_zip.py <app folder> <zip path> <top folder name> <extra files...>
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

PORTABLE_NOTE = (
    "Settings, logs and downloaded models are kept in the data folder next to "
    "this file. Delete this file to use %LOCALAPPDATA%\\TetoRelay instead.\r\n"
)


def build(app: Path, zip_path: Path, top: str, extras: list[Path]) -> int:
    zip_path.unlink(missing_ok=True)
    partial = zip_path.with_suffix(".zip.partial")
    count = 0
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(app.rglob("*")):
                # Not the data folder: a portable copy run from the build
                # folder keeps the builder's settings, recordings and models
                # there, and they went into the zip (1.4 GB instead of 0.86).
                if path.relative_to(app).parts[0] in ("data", "portable.txt"):
                    continue
                if path.is_file():
                    archive.write(path, f"{top}/{path.relative_to(app).as_posix()}")
                    count += 1
            archive.writestr(f"{top}/portable.txt", PORTABLE_NOTE)
            for extra in extras:
                archive.write(extra, f"{top}/{extra.name}")
                count += 1
        partial.replace(zip_path)
    finally:
        partial.unlink(missing_ok=True)
    return count


if __name__ == "__main__":
    app, zip_path, top, *extras = sys.argv[1:]
    n = build(Path(app), Path(zip_path), top, [Path(e) for e in extras])
    print(f"{zip_path}: {n} files, {Path(zip_path).stat().st_size / 1e9:.2f} GB")
