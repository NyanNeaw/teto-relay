"""The Store: a list of presets to install and voicebanks to go and get.

The list is one JSON file, ``catalog.json``, published with the project's
pages (docs/store/, served by GitHub Pages)::

    {"format": 1, "updated": "2026-11-12",
     "presets":    [{"id", "name", "author", "description", "url"}],
     "voicebanks": [{"id", "name", "owner", "licence", "url"}]}

A preset's ``url`` is its .tetostyle file; installing it is the same import
as dropping that file on the window (teto_relay.presets), with the same
checks. A voicebank's ``url`` is its owner's own download page and nothing
else: Teto Relay never hosts, bundles or downloads a voicebank. The page is
opened in the browser, and the .zip from there is added the usual way.

Everything is free; there are no accounts and nothing is paid for.

**The catalog is not trusted.** It comes from the internet, so it is treated
as data from a stranger: it must be an object of the expected shape, every
text is cut to length and stripped of control characters (and shown as text,
never as HTML), every link must be https with no login in it, and an entry
that fails is left out. Nothing in it is ever run. The panel asks for things
by ``id``, never by address, so only an address that passed these checks is
ever fetched or opened.

A copy of the catalog and its presets ships with the app
(teto_relay/web/store) and is what the Store shows with no internet.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .errors import TetoRelayError

log = logging.getLogger(__name__)

CATALOG_URL = "https://nyanneaw.github.io/teto-relay/store/catalog.json"
FORMAT = 1
MAX_CATALOG_BYTES = 200_000
MAX_PRESETS, MAX_VOICEBANKS = 200, 100
MAX_URL = 300
#: Seconds to wait for the internet before showing the copy that came with the app.
TIMEOUT = 6.0
#: How long a fetched catalog is shown before it is fetched again.
KEEP_SECONDS = 600.0
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


class StoreError(TetoRelayError):
    """Something in the Store could not be fetched or used. The message says what."""


# ------------------------------------------------------------- checking it
def safe_url(url, suffix: str = "") -> str | None:
    """`url` if it is a plain https address (no login, no odd characters,
    not over-long) ending in `suffix`; otherwise None."""
    if not isinstance(url, str) or not 0 < len(url) <= MAX_URL or url != url.strip():
        return None
    if _CONTROL.search(url) or any(c.isspace() for c in url) or "\\" in url:
        return None
    try:
        parts = urlparse(url)
        host, login = parts.hostname, parts.username or parts.password
    except ValueError:
        return None
    if parts.scheme != "https" or not host or login or "@" in parts.netloc:
        return None
    if suffix and not parts.path.lower().endswith(suffix):
        return None
    return url


def _line(value, limit: int) -> str:
    """One line of text from the catalog: text only, no control characters,
    no longer than `limit`."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub(" ", value).split())[:limit]


def _entries(items, limit: int, texts: dict[str, int], suffix: str) -> list[dict]:
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        if len(out) >= limit:
            break
        if not isinstance(item, dict):
            continue
        entry_id, url = item.get("id"), safe_url(item.get("url"), suffix)
        if not isinstance(entry_id, str) or not ID.match(entry_id) or entry_id in seen or url is None:
            continue
        entry = {"id": entry_id, "url": url, **{key: _line(item.get(key), size) for key, size in texts.items()}}
        if not entry["name"]:
            continue
        seen.add(entry_id)
        out.append(entry)
    return out


def validate(data) -> dict:
    """The catalog as Teto Relay will use it, from what a file held: only
    the entries, fields and links that pass (see the module's notes).
    Raises StoreError if it is not a catalog at all."""
    from .presets import EXTENSION, MAX_AUTHOR, MAX_DESCRIPTION, MAX_NAME

    if not isinstance(data, dict):
        raise StoreError("The Store's list is not a list Teto Relay can read.")
    version = data.get("format")
    if isinstance(version, bool) or not isinstance(version, int) or version != FORMAT:
        raise StoreError("The Store's list is for another version of Teto Relay.")
    return {
        "format": FORMAT,
        "updated": _line(data.get("updated"), 20),
        "presets": _entries(data.get("presets"), MAX_PRESETS,
                            {"name": MAX_NAME, "author": MAX_AUTHOR, "description": MAX_DESCRIPTION}, EXTENSION),
        "voicebanks": _entries(data.get("voicebanks"), MAX_VOICEBANKS,
                               {"name": 80, "owner": 80, "licence": 300}, ""),
    }


def parse(raw: bytes) -> dict:
    if len(raw) > MAX_CATALOG_BYTES:
        raise StoreError("The Store's list is too big to be right.")
    try:
        return validate(json.loads(raw.decode("utf-8-sig")))
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, StoreError):
            raise
        raise StoreError("The Store's list is not a list Teto Relay can read.") from None


# ------------------------------------------------------------- fetching it
class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only to another https address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if safe_url(newurl) is None:
            raise urllib.error.URLError("redirected to an address that is not plain https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, limit: int) -> bytes:
    """The bytes at an https address, at most `limit` of them. Raises
    StoreError: no connection, not https, or more than `limit`."""
    if safe_url(url) is None:
        raise StoreError("That address is not a plain https address.")
    request = urllib.request.Request(url, headers={"User-Agent": "TetoRelay", "Accept": "*/*"})
    try:
        with urllib.request.build_opener(_HttpsOnly).open(request, timeout=TIMEOUT) as response:
            data = response.read(limit + 1)
    except (OSError, ValueError) as exc:  # URLError and timeouts are OSErrors
        raise StoreError(f"Could not reach the Store ({getattr(exc, 'reason', exc)}).") from exc
    if len(data) > limit:
        raise StoreError("That file is bigger than it should be, so it was not used.")
    return data


def bundled_folder() -> Path:
    """The copy of the Store that ships with the app."""
    return Path(__file__).resolve().parent / "web" / "store"


def bundled() -> dict:
    return {**parse((bundled_folder() / "catalog.json").read_bytes()), "source": "bundled"}


_KEPT: dict = {}
_KEPT_LOCK = threading.Lock()


def catalog(refresh: bool = False) -> dict:
    """The Store's list: from the internet ("source": "online"), or, when
    that cannot be had or is not a catalog, the copy that came with the app
    ("bundled", with "problem" saying why). A fetched list is kept for a few
    minutes, and `refresh` fetches it again now."""
    with _KEPT_LOCK:
        kept = _KEPT.get("catalog")
        if kept is not None and not refresh and time.monotonic() - _KEPT["at"] < KEEP_SECONDS:
            return kept
        try:
            found = {**parse(download(CATALOG_URL, MAX_CATALOG_BYTES)), "source": "online"}
        except StoreError as exc:
            log.info("Store: showing the bundled list (%s)", exc)
            return {**bundled(), "problem": str(exc)}
        _KEPT.update(catalog=found, at=time.monotonic())
        return found


def forget() -> None:
    """Drop the kept catalog (tests, and a refresh that failed)."""
    with _KEPT_LOCK:
        _KEPT.clear()


# ------------------------------------------------------------ using entries
def _entry(listing: dict, kind: str, entry_id: str) -> dict:
    for entry in listing.get(kind) or []:
        if entry["id"] == entry_id:
            return entry
    raise StoreError("That is not in the Store any more. Refresh the list.")


def preset_file(listing: dict, entry_id: str) -> bytes:
    """The .tetostyle file of a preset in the Store, by its id: downloaded,
    or, without internet, the copy that came with the app if there is one.
    What it holds is checked when it is imported, like any other preset."""
    from .presets import EXTENSION, MAX_BYTES

    entry = _entry(listing, "presets", str(entry_id))
    try:
        return download(entry["url"], MAX_BYTES)
    except StoreError as exc:
        local = bundled_folder() / "presets" / f"{entry['id']}{EXTENSION}"
        if local.is_file():
            log.info("Store: using the bundled copy of %s (%s)", entry["id"], exc)
            return local.read_bytes()
        raise StoreError(f"Could not download {entry['name']}. Check your internet connection.") from exc


def official_page(listing: dict, entry_id: str) -> str:
    """The official download page of a voicebank in the Store, by its id."""
    return _entry(listing, "voicebanks", str(entry_id))["url"]
