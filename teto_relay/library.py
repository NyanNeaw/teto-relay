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
BANK_MARKERS = ("oto.ini", "character.txt", "character.yaml")
CHARACTER_FILES = ("character.txt", "character.yaml")
# Voices OpenUtau can use but Teto Relay cannot: they have no oto.ini.
OTHER_ENGINES = {
    "dsconfig.yaml": "a DiffSinger voicebank",
    "enuconfig.yaml": "an ENUNU voicebank",
    "vogen.json": "a Vogen voicebank",
}
ARCHIVES_WE_CANNOT_OPEN = (".rar", ".7z", ".lzh", ".tar", ".gz")
#: What the upload accepts. .uar is UTAU's installer archive, a renamed zip.
VOICEBANK_ARCHIVES = (".zip", ".uar")
MAX_UPLOAD = 1_500_000_000  # 1.5 GB - an RVC index alone can be 170 MB
MAX_UNPACKED = 4_000_000_000  # what a voicebank zip may expand to


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


def _decode_names(archive: zipfile.ZipFile) -> None:
    """Give every member its real name.

    A zip made by a Japanese tool stores names in Shift-JIS without saying so,
    and Python reads them as cp437: あ.wav became "âJüK.wav", the oto.ini no
    longer matched a single sample, and the bank sang nothing. One encoding is
    chosen for the whole archive, the first that reads every name.
    """
    legacy = [m for m in archive.infolist() if not m.flag_bits & 0x800]
    if not legacy:
        return
    raw = [m.filename.encode("cp437") for m in legacy]
    for encoding in ("utf-8", "cp932", "gbk", "cp949"):
        try:
            names = [r.decode(encoding) for r in raw]
        except UnicodeDecodeError:
            continue
        for member, name in zip(legacy, names):
            member.filename = name
        return


def _depth(path: Path, top: Path) -> int:
    return len(path.relative_to(top).parts)


def _find_bank_root(folder: Path) -> Path | None:
    """The folder that actually holds the voicebank.

    Archives are packed inconsistently: sometimes the oto.ini is at the top,
    sometimes it is two folders down inside a name with the author's handle.
    A singer folder (character.txt) wins over the oto.ini inside it: a bank
    recorded at several pitches keeps one oto.ini per pitch folder, and taking
    the first of those installed one pitch and left the rest behind.
    """
    if any((folder / marker).exists() for marker in BANK_MARKERS):
        return folder
    for name in CHARACTER_FILES:
        found = sorted(folder.rglob(name), key=lambda p: (_depth(p, folder), str(p)))
        if found:
            return found[0].parent
    otos = sorted(folder.rglob("oto.ini"), key=lambda p: (_depth(p, folder), str(p)))
    if not otos:
        return None
    # No singer file: the folder that holds every oto.ini.
    import os

    common = Path(os.path.commonpath([str(p.parent) for p in otos]))
    return common if common != folder or len(otos) == 1 else otos[0].parent


def _not_a_bank_reason(folder: Path) -> str:
    for marker, what in OTHER_ENGINES.items():
        if any(folder.rglob(marker)):
            return (f"That is {what}. Teto Relay sings UTAU voicebanks (a folder with "
                    "oto.ini and .wav samples); open this one in OpenUtau instead.")
    return ("No oto.ini or character.txt anywhere in that zip, so it is not "
            "a voicebank. Zip the folder that contains oto.ini.")


def _check_samples(bank: Path) -> tuple[int, int]:
    """(samples named in the oto.ini files, of those, how many are missing)."""
    from .voicebank import parse_oto

    named = missing = 0
    for oto in bank.rglob("oto.ini"):
        for entry in parse_oto(oto):
            named += 1
            if not (oto.parent / entry.wav).exists():
                missing += 1
    return named, missing


def install_voicebank(data: bytes, filename: str, root: Path) -> dict:
    """Unpack an uploaded voicebank zip into the library.

    Returns a summary; raises ValueError with something the user can act on.
    """
    lower = filename.lower()
    if lower.endswith(ARCHIVES_WE_CANNOT_OPEN):
        raise ValueError(
            f"{Path(filename).suffix} files can't be opened here. Unpack it, then either "
            "zip the voicebank folder and add that, or copy the folder into your "
            "voicebank folder (Setup shows where)."
        )
    if not lower.endswith(VOICEBANK_ARCHIVES):
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

        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError("That file is not a valid .zip.") from exc
        with archive:
            _decode_names(archive)
            members = _safe_members(archive)
            if not members:
                raise ValueError("That zip is empty, or every entry was unsafe to extract.")
            # The upload size is capped, but a small zip can claim to unpack
            # to terabytes. Check what it says before writing any of it.
            unpacked = sum(m.file_size for m in members)
            if unpacked > MAX_UNPACKED:
                raise ValueError(
                    f"That zip would unpack to {unpacked/1e9:.1f} GB; a voicebank is "
                    f"at most {MAX_UNPACKED/1e9:.0f} GB, so it was not installed."
                )
            free = shutil.disk_usage(root).free
            if unpacked > free:
                raise ValueError(
                    f"Not enough disk space: the voicebank needs {unpacked/1e9:.1f} GB "
                    f"and {free/1e9:.1f} GB is free."
                )
            archive.extractall(staging, members=members)

        found = _find_bank_root(staging)
        if found is None:
            raise ValueError(_not_a_bank_reason(staging))

        wavs = sum(1 for _ in found.rglob("*.wav"))
        if wavs == 0:
            raise ValueError("That bank has no .wav samples in it, so it cannot sing.")
        named, missing = _check_samples(found)
        if named and missing > named * 0.5:
            raise ValueError(
                f"{missing} of the {named} samples its oto.ini lists are not in the zip "
                "(or their names were garbled when it was packed), so it would sing "
                "mostly silence. Try a different download of this voicebank."
            )
        if not any((found / name).exists() for name in CHARACTER_FILES):
            # OpenUtau needs one to load the bank; UTAU would write the same.
            (found / "character.txt").write_bytes(f"name={stem}\r\n".encode("cp932", errors="replace"))

        target = root / stem
        if target.exists():
            raise ValueError(f"{stem!r} is already installed. Rename the zip to add another.")
        # Move the bank itself up, so the library holds banks rather than the
        # accidental nesting a zip happened to have.
        shutil.move(str(found), str(target))
        if missing:
            log.warning("%s: %d of %d samples named in oto.ini are missing", stem, missing, named)
        return {"name": stem, "path": str(target), "samples": wavs, "missing": missing}
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
    # weights_only: a .pth is a pickle, and unpickling runs whatever code the
    # file names. An RVC checkpoint is only tensors, numbers and strings, so
    # the restricted loader reads every genuine one - and refuses anything
    # that would need to execute code to load.
    try:
        import torch
    except ImportError as exc:
        target.unlink(missing_ok=True)
        raise ValueError(
            "Checking a voice model needs PyTorch, which is not installed. "
            "Install the voice-conversion extras first (see README)."
        ) from exc
    try:
        checkpoint = torch.load(str(target), map_location="cpu", weights_only=True)
    except Exception as exc:  # noqa: BLE001 - the message is for the user
        target.unlink(missing_ok=True)
        raise ValueError(
            "That .pth could not be opened as a plain model file, so it was not "
            f"kept ({type(exc).__name__}). RVC voice models load this way; a file "
            "that needs more than that is either not an RVC model or not safe to open."
        ) from exc
    if not isinstance(checkpoint, dict):
        target.unlink(missing_ok=True)
        raise ValueError("That .pth is not an RVC voice model.")

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
