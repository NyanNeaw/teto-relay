"""Installing voices: UTAU voicebanks and RVC models.

Both arrive as a file the user picked, so both are validated before they are
kept. A voicebank that turns out to be a folder of holiday photos, or a .pth
that is some other kind of checkpoint, is rejected with a reason rather than
being written into the library and failing later at render time.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

# A voicebank is one oto.ini away from being a voicebank; character.txt is
# conventional but not required, and plenty of banks omit it.
BANK_MARKERS = ("oto.ini", "character.txt")
MAX_UPLOAD = 1_500_000_000  # 1.5 GB - an RVC index alone can be 170 MB


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Every member that is safe to extract.

    A zip may name `../../Windows/System32/...` or an absolute path; extracting
    that writes outside the destination. Python does sanitise `extractall`, but
    only since 3.6.2 and only for some shapes, so the check is explicit.
    """
    out: list[zipfile.ZipInfo] = []
    for member in archive.infolist():
        name = member.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            log.warning("refusing zip entry %r: it points outside the folder", name)
            continue
        out.append(member)
    return out


def _find_bank_root(folder: Path) -> Path | None:
    """The folder that actually holds the voicebank.

    Archives are packed inconsistently: sometimes the oto.ini is at the top,
    sometimes it is two folders down inside a name with the author's handle.
    """
    if any((folder / marker).exists() for marker in BANK_MARKERS):
        return folder
    for path in sorted(folder.rglob("oto.ini"))[:1]:
        return path.parent
    for path in sorted(folder.rglob("character.txt"))[:1]:
        return path.parent
    return None


def install_voicebank(data: bytes, filename: str, root: Path) -> dict:
    """Unpack an uploaded voicebank zip into the library.

    Returns a summary; raises ValueError with something the user can act on.
    """
    if not filename.lower().endswith(".zip"):
        raise ValueError("A voicebank must be a .zip of the bank folder.")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"That file is {len(data)/1e9:.1f} GB; the limit is 1.5 GB.")

    root.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem.strip() or "voicebank"
    staging = root / f".installing-{stem}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    try:
        import io

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = _safe_members(archive)
            if not members:
                raise ValueError("That zip is empty, or every entry was unsafe to extract.")
            archive.extractall(staging, members=members)

        found = _find_bank_root(staging)
        if found is None:
            raise ValueError(
                "No oto.ini or character.txt anywhere in that zip, so it is not "
                "a voicebank. Zip the folder that contains oto.ini."
            )

        wavs = len(list(found.glob("*.wav"))) + sum(
            len(list(p.glob("*.wav"))) for p in found.iterdir() if p.is_dir()
        )
        if wavs == 0:
            raise ValueError("That bank has no .wav samples in it, so it cannot sing.")

        target = root / stem
        if target.exists():
            raise ValueError(f"{stem!r} is already installed. Rename the zip to add another.")
        # Move the bank itself up, so the library holds banks rather than the
        # accidental nesting a zip happened to have.
        shutil.move(str(found), str(target))
        return {"name": stem, "path": str(target), "samples": wavs}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_rvc_model(data: bytes, filename: str, folder: Path) -> dict:
    """Save an uploaded RVC voice model, after checking it is one."""
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pth", ".index"):
        raise ValueError("An RVC voice is a .pth model, with an optional .index.")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"That file is {len(data)/1e9:.1f} GB; the limit is 1.5 GB.")

    folder.mkdir(parents=True, exist_ok=True)
    target = folder / Path(filename).name
    target.write_bytes(data)

    if suffix == ".index":
        return {"kind": "index", "path": str(target)}

    # An RVC checkpoint carries its own description. Reading it both proves the
    # file is what it claims and tells the panel what it is.
    try:
        import torch

        checkpoint = torch.load(str(target), map_location="cpu", weights_only=False)
    except Exception as exc:  # noqa: BLE001 - the message is for the user
        target.unlink(missing_ok=True)
        raise ValueError(f"That .pth could not be opened: {exc}") from exc

    missing = [k for k in ("weight", "config", "sr") if k not in checkpoint]
    if missing:
        target.unlink(missing_ok=True)
        raise ValueError(
            "That .pth is not an RVC voice model - it has no "
            + ", ".join(missing)
            + ". Models trained by other tools will not load."
        )

    return {
        "kind": "model",
        "path": str(target),
        "name": target.stem,
        "sample_rate": str(checkpoint.get("sr", "?")),
        "version": str(checkpoint.get("version", "v1")),
        "pitch": bool(checkpoint.get("f0", 0)),
        "info": str(checkpoint.get("info", ""))[:60],
    }


def accent_colour(image_bytes: bytes) -> str | None:
    """The colour a voicebank's own artwork is built around, as #rrggbb.

    Used to tint the panel per voice. Greys and near-blacks are skipped: an
    icon is mostly outline and background, and tinting the interface the colour
    of someone's line art would give every bank the same dark grey.
    """
    try:
        import colorsys
        from io import BytesIO

        from PIL import Image

        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        image.thumbnail((64, 64))
        best, best_score = None, 0.0
        for count, (r, g, b) in image.getcolors(64 * 64) or []:
            h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
            if s < 0.25 or l < 0.18 or l > 0.88:
                continue
            # Frequent and colourful beats merely frequent.
            score = count * (s ** 1.5)
            if score > best_score:
                best, best_score = (r, g, b), score
        if best is None:
            return None
        # Push it to a strength that works as an accent on both themes.
        h, l, s = colorsys.rgb_to_hls(*[c / 255 for c in best])
        r, g, b = colorsys.hls_to_rgb(h, min(max(l, 0.45), 0.62), max(s, 0.55))
        return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))
    except Exception:
        log.debug("could not read an accent colour", exc_info=True)
        return None
