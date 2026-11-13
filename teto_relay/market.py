"""The Market server: where people share tunings, voicebanks and sounds.

The server is market-server/ in this project (a Cloudflare Worker, which can
also be run on this PC with Node). With the Market server setting
(`market_url`) empty, the Market is the plain list the Store has always
shown (teto_relay.store); with an address, it is that server's listings:

* tunings (.tetostyle), installed with the same checks as a dropped file;
* voicebanks: a file the creator allowed to be shared, installed the usual
  way (teto_relay.library), or a link to the official page;
* sounds: one-shots, loops and FX, kept in the data folder's `sounds`
  folder and laid on a Studio lane like any audio file;

each with an optional short demo that plays before anything is downloaded.

**What comes from the server is not trusted**, as with the Store's list:
every listing is checked for shape, its texts cut to length, and its file
addresses must be on the server itself. The panel asks for things by id,
never by address. Only https is used, except to a server on this PC
(127.0.0.1 / localhost), which is how it is tried before it is put online.

This PC is known to the server only by a random id made once (for likes
and reports), kept in `market.json` with the edit keys of what you shared.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .errors import TetoRelayError

log = logging.getLogger(__name__)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
FORMAT = 2
KINDS = ("tuning", "voicebank", "sound")
MAX_LIST_BYTES = 2_000_000
MAX_ITEMS = 1000
#: As in market-server/src/worker.js.
LIMITS = {"tuning": 64_000, "voicebank": 95 * 1024 * 1024, "sound": 20 * 1024 * 1024, "demo": 3 * 1024 * 1024}
MAX_UPLOAD = LIMITS["voicebank"] + LIMITS["demo"] + 200_000
TIMEOUT = 10.0
#: Big files (a voicebank) get longer.
FILE_TIMEOUT = 120.0
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
SOUND_TYPES = {"wav": ".wav", "mp3": ".mp3", "ogg": ".ogg", "flac": ".flac"}


class MarketError(TetoRelayError):
    """The Market server could not be reached or refused. The message says what."""


# ------------------------------------------------------------ the address
#: A built app's own Market server: one line, its address. The file is kept
#: out of git (.gitignore), so only builds made where it exists have it; the
#: shared code, and the app run from source, have none (there it is the
#: Market server setting).
BUNDLED_SERVER = Path(__file__).resolve().parent / "web" / "market_server.txt"


def bundled_server() -> str:
    """The Market server a built app came with, or ""."""
    from .paths import frozen

    if not frozen():
        return ""
    try:
        lines = BUNDLED_SERVER.read_text(encoding="utf-8-sig").split()
    except OSError:
        return ""
    return lines[0] if lines else ""


def base_url(cfg) -> str:
    """The Market server's address from the settings (or, with the setting
    empty, the one a built app came with), or "" for none (or one that is not
    a plain https address, or http on this PC)."""
    value = (str(getattr(cfg, "market_url", "") or "").strip() or bundled_server()).rstrip("/")
    if not value or len(value) > 200 or any(c.isspace() for c in value) or "\\" in value:
        return ""
    try:
        parts = urlparse(value)
        host = parts.hostname
    except ValueError:
        return ""
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        return ""
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOCAL_HOSTS):
        return value
    return ""


def _on_server(url, base: str) -> str | None:
    """`url` if it is a file on the server at `base`; otherwise None."""
    if not isinstance(url, str) or not base or len(url) > 400 or not url.startswith(base + "/v1/files/"):
        return None
    if any(c.isspace() for c in url) or "\\" in url or ".." in url:
        return None
    return url


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError("the Market server sent the request elsewhere")


def request(url: str, base: str, limit: int, method: str = "GET", data: bytes | None = None,
            headers: dict | None = None, timeout: float = TIMEOUT) -> tuple[int, bytes, str]:
    """Status, body (at most `limit` bytes) and content type of a request to
    the Market server. Raises MarketError when it cannot be reached."""
    if not base or not url.startswith(base + "/"):
        raise MarketError("That address is not on the Market server.")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": "TetoRelay", "Accept": "*/*", **(headers or {})})
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read(limit + 1)
            status, kind = response.status, response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body, status, kind = exc.read(64_000), exc.code, exc.headers.get("Content-Type", "")
    except (OSError, ValueError) as exc:
        raise MarketError(f"Could not reach the Market server ({getattr(exc, 'reason', exc)}).") from exc
    if len(body) > limit:
        raise MarketError("That file is bigger than it should be, so it was not used.")
    return status, body, kind


def _refusal(body: bytes, fallback: str) -> str:
    try:
        return _line(json.loads(body.decode("utf-8")).get("error"), 300) or fallback
    except (ValueError, AttributeError, UnicodeDecodeError):
        return fallback


# ------------------------------------------------------------ the listings
def _line(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub(" ", value).split())[:limit]


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _file(value, base: str, limit: int) -> dict | None:
    if not isinstance(value, dict):
        return None
    url = _on_server(value.get("url"), base)
    size = _count(value.get("size"))
    if url is None or not 0 < size <= limit:
        return None
    return {"url": url, "size": size, "type": _line(value.get("type"), 12), "name": _line(value.get("name"), 80)}


def _https(value) -> str:
    from .store import safe_url

    return safe_url(value) or ""


CURRENCIES = ("THB", "USD", "JPY", "EUR")
HANDLE = re.compile(r"^[a-z0-9_]{3,24}$")


def _price(item: dict) -> tuple[float, str, str]:
    """(price, currency, shop page) of a listing; (0, "", "") for a free one
    or one whose price is not usable (it is then shown as free only if it
    has a file; see parse)."""
    price = item.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not 0 < price <= 1_000_000:
        return 0.0, "", ""
    currency = item.get("currency") if item.get("currency") in CURRENCIES else ""
    shop = _https(item.get("shop_url"))
    if not currency or not shop:
        return 0.0, "", ""
    return round(float(price), 2), currency, shop


def _user(value, base: str) -> dict | None:
    """Who shared a listing: their @handle, name and picture's address."""
    if not isinstance(value, dict) or not HANDLE.match(str(value.get("handle") or "")):
        return None
    avatar = value.get("avatar")
    ok = isinstance(avatar, str) and avatar.startswith(f"{base}/v1/users/{value['handle']}/avatar") and len(avatar) < 300
    return {"handle": value["handle"], "name": _line(value.get("name"), 40) or value["handle"],
            "avatar": avatar if ok else ""}


def parse(raw: bytes, base: str) -> dict:
    """The server's catalog, checked, in the Store's shape plus `sounds`."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise MarketError("The Market server's list is not one Teto Relay can read.") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("items"), list):
        raise MarketError("The Market server is for another version of Teto Relay.")
    out = {"format": 1, "updated": _line(data.get("updated"), 20), "presets": [], "voicebanks": [], "sounds": []}
    seen = set()
    for item in data["items"][:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        entry_id, kind = item.get("id"), item.get("kind")
        if not isinstance(entry_id, str) or not ID.match(entry_id) or entry_id in seen or kind not in KINDS:
            continue
        name = _line(item.get("name"), 60)
        if not name:
            continue
        price, currency, shop = _price(item)
        common = {
            "id": entry_id, "name": name, "description": _line(item.get("description"), 500),
            "tags": [t for t in (_line(t, 20) for t in (item.get("tags") or [])[:8] if isinstance(t, str)) if t],
            "language": _line(item.get("language"), 8), "likes": _count(item.get("likes")),
            "downloads": _count(item.get("downloads")), "demo": _file(item.get("demo"), base, LIMITS["demo"]),
            "price": price, "currency": currency, "shop_url": shop, "user": _user(item.get("user"), base),
        }
        # A paid item is sold from its creator's shop: nothing is downloaded here.
        file = None if price else _file(item.get("file"), base, LIMITS[kind])
        if kind == "tuning":
            if file is None and not price:
                continue
            out["presets"].append({**common, "author": _line(item.get("author"), 60), "url": file["url"] if file else ""})
        elif kind == "voicebank":
            page = _https(item.get("official_url"))
            if file is None and not page and not price:
                continue
            if file and Path(file["name"]).suffix.lower() not in (".zip", ".7z", ".rar"):
                file["name"] = f"{entry_id}.{file['type'] if file['type'] in ('zip', '7z', 'rar') else 'zip'}"
            out["voicebanks"].append({**common, "owner": _line(item.get("author"), 80),
                                      "licence": _line(item.get("licence"), 500), "url": page, "file": file})
        else:
            if (file is None or file["type"] not in SOUND_TYPES) and not price:
                continue
            bpm = item.get("bpm")
            out["sounds"].append({**common, "author": _line(item.get("author"), 60), "file": file,
                                  "bpm": bpm if isinstance(bpm, int) and 40 <= bpm <= 300 else None,
                                  "key": _line(item.get("key"), 8)})
        seen.add(entry_id)
    return out


def catalog(base: str) -> dict:
    status, body, _ = request(base + "/v1/catalog", base, MAX_LIST_BYTES)
    if status != 200:
        raise MarketError(_refusal(body, f"The Market server answered {status}."))
    return {**parse(body, base), "source": "market", "server": base}


def entry(listing: dict, kind: str, entry_id: str) -> dict:
    for found in listing.get(kind) or []:
        if found["id"] == str(entry_id):
            return found
    raise MarketError("That is not in the Market any more. Refresh the list.")


def download(listing: dict, kind: str, entry_id: str, which: str = "file") -> tuple[bytes, dict]:
    """A listing's file or demo, and what it is."""
    base = listing.get("server") or ""
    found = entry(listing, kind, entry_id)
    meta = found.get(which) if which in ("file", "demo") else None
    if which == "file" and found.get("price"):
        raise MarketError("That is sold from its creator's shop: press Buy to open it.")
    if which == "file" and kind == "presets":
        meta = {"url": found["url"], "size": LIMITS["tuning"], "type": "tetostyle", "name": f"{entry_id}.tetostyle"}
    if not meta:
        raise MarketError("That has nothing to download." if which == "file" else "That has no demo.")
    limit = LIMITS["tuning"] if kind == "presets" and which == "file" else int(meta["size"]) + 1024
    status, body, _ = request(meta["url"], base, limit, timeout=FILE_TIMEOUT)
    if status != 200:
        raise MarketError(_refusal(body, f"The Market server answered {status}."))
    return body, meta


# ----------------------------------------------------------- this PC's side
_LOCK = threading.Lock()


def _state_path() -> Path:
    from .paths import data_dir

    return data_dir() / "market.json"


def _state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def device_id() -> str:
    """A random id for this PC, made once: likes and reports count once per PC."""
    with _LOCK:
        data = _state()
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", str(data.get("device") or "")):
            data["device"] = "pc-" + secrets.token_urlsafe(18)
            _save_state(data)
        return data["device"]


def shared() -> list[dict]:
    """What you shared from this PC (id, name, kind, server, edit key)."""
    return [s for s in _state().get("shared", []) if isinstance(s, dict)]


def _remember(item: dict) -> None:
    with _LOCK:
        data = _state()
        data.setdefault("shared", []).append(item)
        _save_state(data)


# ---------------------------------------------------------------- accounts
# Signing in keeps a session token for the server in market.json; the
# password is passed to the server once and never kept or logged.
def _session(base: str) -> dict:
    found = (_state().get("sessions") or {}).get(base)
    return found if isinstance(found, dict) and found.get("token") else {}


def _auth(base: str) -> dict:
    token = _session(base).get("token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _keep_session(base: str, answer: dict | None) -> None:
    with _LOCK:
        data = _state()
        sessions = data.setdefault("sessions", {})
        if answer:
            sessions[base] = {"token": str(answer.get("token") or ""), "handle": _line(answer.get("handle"), 24),
                              "name": _line(answer.get("name"), 40)}
        else:
            sessions.pop(base, None)
        _save_state(data)


def _post_json(base: str, path: str, payload: dict | None, method: str = "POST", fallback: str = "") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    status, raw, _ = request(base + path, base, 200_000, method, data,
                             {"Content-Type": "application/json", **_auth(base)})
    if status == 401 and _session(base) and not path.startswith("/v1/auth/"):
        _keep_session(base, None)          # the session ran out: signed out here too
        raise MarketError("Sign in again: your session ended.")
    if status not in (200, 201):
        raise MarketError(_refusal(raw, fallback or f"The Market server answered {status}."))
    try:
        answer = json.loads(raw)
    except ValueError:
        raise MarketError("The Market server's answer could not be read.") from None
    return answer if isinstance(answer, dict) else {}


def sign_up(base: str, email: str, password: str, handle: str, name: str) -> dict:
    answer = _post_json(base, "/v1/auth/signup", {"email": str(email), "password": str(password),
                                                  "handle": str(handle), "name": str(name)}, fallback="Could not make the account.")
    _keep_session(base, answer)
    return account(base)


def sign_in(base: str, email: str, password: str) -> dict:
    answer = _post_json(base, "/v1/auth/login", {"email": str(email), "password": str(password)}, fallback="Could not sign in.")
    _keep_session(base, answer)
    return account(base)


def sign_out(base: str) -> None:
    if _session(base):
        try:
            _post_json(base, "/v1/auth/logout", None)
        except MarketError:
            pass
    _keep_session(base, None)


def _public_profile(answer: dict, base: str) -> dict:
    handle = str(answer.get("handle") or "")
    avatar = answer.get("avatar")
    return {
        "handle": handle if HANDLE.match(handle) else "",
        "name": _line(answer.get("name"), 40),
        "bio": _line(answer.get("bio"), 300),
        "links": [u for u in (_https(x) for x in (answer.get("links") or [])[:4]) if u],
        "avatar": bool(isinstance(avatar, str) and avatar.startswith(f"{base}/v1/users/{handle}/avatar")),
        "joined": _line(answer.get("joined"), 10),
    }


def account(base: str) -> dict:
    """Who is signed in to the Market here: {"signed_in": False} or their profile."""
    if not _session(base):
        return {"signed_in": False}
    try:
        answer = _post_json(base, "/v1/me", None, "GET")
    except MarketError as exc:
        if not _session(base):
            return {"signed_in": False, "problem": str(exc)}
        kept = _session(base)        # offline: what was known when signing in
        return {"signed_in": True, "offline": True, "handle": kept.get("handle", ""), "name": kept.get("name", "")}
    return {"signed_in": True, **_public_profile(answer, base), "email": _line(answer.get("email"), 200)}


def edit_profile(base: str, name: str, bio: str, links: list) -> dict:
    _post_json(base, "/v1/me", {"name": str(name), "bio": str(bio), "links": [str(x) for x in links][:4]})
    return account(base)


def set_avatar(base: str, image: bytes) -> dict:
    status, raw, _ = request(base + "/v1/me/avatar", base, 200_000, "POST", image,
                             {"Content-Type": "application/octet-stream", **_auth(base)})
    if status != 200:
        raise MarketError(_refusal(raw, "Could not change the picture."))
    return account(base)


def close_account(base: str) -> None:
    _post_json(base, "/v1/me", None, "DELETE", fallback="Could not close the account.")
    _keep_session(base, None)


def mine(base: str) -> list[dict]:
    """Everything you shared from this account, with whether it is listed yet."""
    answer = _post_json(base, "/v1/me/items", None, "GET")
    out = []
    for item in answer.get("items") or []:
        if isinstance(item, dict) and ID.match(str(item.get("id") or "")):
            out.append({"id": item["id"], "kind": _line(item.get("kind"), 12), "name": _line(item.get("name"), 60),
                        "status": _line(item.get("status"), 12), "likes": _count(item.get("likes")),
                        "downloads": _count(item.get("downloads")), "price": _price(item)[0],
                        "currency": _price(item)[1]})
    return out


def person(base: str, handle: str) -> dict:
    """A creator's public page: their profile and what they share."""
    if not HANDLE.match(str(handle)):
        raise MarketError("No such person.")
    status, raw, _ = request(f"{base}/v1/users/{handle}", base, MAX_LIST_BYTES)
    if status != 200:
        raise MarketError(_refusal(raw, "No such person."))
    answer = json.loads(raw)
    items = parse(json.dumps({"format": FORMAT, "items": answer.get("items") or []}).encode(), base)
    return {**_public_profile(answer, base), "items": [i["id"] for kind in ("presets", "voicebanks", "sounds")
                                                       for i in items[kind]]}


def avatar(base: str, handle: str) -> bytes:
    if not HANDLE.match(str(handle)):
        raise MarketError("No such person.")
    status, raw, kind = request(f"{base}/v1/users/{handle}/avatar", base, 400_000)
    if status != 200 or not kind.startswith("image/"):
        raise MarketError("No picture.")
    return raw


def shop_page(listing: dict, kind: str, entry_id: str) -> str:
    """The shop page a paid listing is sold from."""
    found = entry(listing, kind, entry_id)
    if not found.get("price") or not found.get("shop_url"):
        raise MarketError("That is free: press Get.")
    return found["shop_url"]


def like(base: str, entry_id: str, on: bool) -> int:
    if not ID.match(str(entry_id)):
        raise MarketError("No such listing.")
    body = json.dumps({"device": device_id(), "on": bool(on)}).encode()
    status, raw, _ = request(f"{base}/v1/items/{entry_id}/like", base, 10_000, "POST", body,
                             {"Content-Type": "application/json"})
    if status != 200:
        raise MarketError(_refusal(raw, "Could not like it."))
    return _count(json.loads(raw).get("likes"))


def report(base: str, entry_id: str, reason: str) -> None:
    if not ID.match(str(entry_id)):
        raise MarketError("No such listing.")
    body = json.dumps({"device": device_id(), "reason": _line(reason, 300)}).encode()
    status, raw, _ = request(f"{base}/v1/items/{entry_id}/report", base, 10_000, "POST", body,
                             {"Content-Type": "application/json"})
    if status != 200:
        raise MarketError(_refusal(raw, "Could not report it."))


def upload(base: str, body: bytes, content_type: str) -> dict:
    """Pass a share (the page's multipart form: meta, file, demo) to the
    server as it came; keep the edit key it gives back. Returns id and
    status ("pending" until it is reviewed, or "live")."""
    if not content_type.startswith("multipart/form-data"):
        raise MarketError("The upload could not be read.")
    if not 0 < len(body) <= MAX_UPLOAD:
        raise MarketError("That upload is too big: a voicebank can be at most 95 MB.")
    if not _session(base):
        raise MarketError("Sign in to share: your profile shows who made it.")
    status, raw, _ = request(f"{base}/v1/items", base, 20_000, "POST", body,
                             {"Content-Type": content_type, **_auth(base)}, timeout=FILE_TIMEOUT)
    if status == 401:
        _keep_session(base, None)
        raise MarketError("Sign in again: your session ended.")
    if status != 201:
        raise MarketError(_refusal(raw, f"The Market server answered {status}."))
    answer = json.loads(raw)
    entry_id, key = str(answer.get("id") or ""), str(answer.get("edit_key") or "")
    if not ID.match(entry_id):
        raise MarketError("The Market server's answer could not be read.")
    meta = {}
    try:
        meta = json.loads(_meta_field(body, content_type) or "{}")
    except ValueError:
        pass
    _remember({"id": entry_id, "server": base, "kind": _line(meta.get("kind"), 12), "name": _line(meta.get("name"), 60),
               "edit_key": key})
    return {"id": entry_id, "status": "live" if answer.get("status") == "live" else "pending"}


def take_down(base: str, entry_id: str) -> None:
    """Take down something you shared: as the signed-in account, or with the
    edit key kept when it was shared from this PC."""
    if not ID.match(str(entry_id)):
        raise MarketError("No such listing.")
    kept = next((s for s in shared() if s.get("id") == entry_id and s.get("server") == base), None)
    headers = dict(_auth(base))
    if kept is not None:
        headers["X-Edit-Key"] = str(kept.get("edit_key") or "")
    if not headers:
        raise MarketError("Sign in as who shared it.")
    status, raw, _ = request(f"{base}/v1/items/{entry_id}", base, 10_000, "DELETE", headers=headers)
    if status != 200:
        raise MarketError(_refusal(raw, "Could not take it down."))
    with _LOCK:
        data = _state()
        data["shared"] = [s for s in data.get("shared", []) if not (s.get("id") == entry_id and s.get("server") == base)]
        _save_state(data)


def _meta_field(body: bytes, content_type: str) -> str | None:
    """The `meta` field of a multipart body, for remembering what was shared."""
    match = re.search(r"boundary=\"?([^\";]+)", content_type)
    if not match:
        return None
    boundary = b"--" + match.group(1).encode()
    for part in body.split(boundary):
        head, _, value = part.partition(b"\r\n\r\n")
        if b'name="meta"' in head:
            return value.rstrip(b"\r\n-").decode("utf-8", "replace")
    return None


# ------------------------------------------------------------------ sounds
def sounds_dir() -> Path:
    from .paths import data_dir

    return data_dir() / "sounds"


def sound_path(entry_id: str) -> Path | None:
    """A Market sound already on this PC, or None."""
    if not ID.match(str(entry_id)):
        return None
    for suffix in SOUND_TYPES.values():
        path = sounds_dir() / f"{entry_id}{suffix}"
        if path.is_file():
            return path
    return None


def get_sound(listing: dict, entry_id: str) -> Path:
    """The sound's file on this PC, downloaded the first time."""
    have = sound_path(entry_id)
    if have is not None:
        return have
    data, meta = download(listing, "sounds", entry_id)
    # Laid on a lane, it gets the checks any dropped file gets (music.save_audio).
    suffix = SOUND_TYPES.get(meta.get("type", ""), "")
    if not suffix:
        raise MarketError("That sound is not a .wav, .mp3, .ogg or .flac.")
    folder = sounds_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{entry_id}{suffix}"
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    log.info("Market: got the sound %s (%d bytes)", entry_id, len(data))
    return path
