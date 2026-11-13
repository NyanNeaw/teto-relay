"""Review what people shared in the Market (market-server/).

    python tools/market_admin.py queue
    python tools/market_admin.py live    <id>     # list it
    python tools/market_admin.py reject  <id>     # keep it out
    python tools/market_admin.py remove  <id>     # delete it and its files
    python tools/market_admin.py demo    <id>     # save its demo to listen to
    python tools/market_admin.py ban     <@name>  # hide everything an account shared
    python tools/market_admin.py unban   <@name>

The server's address and the admin token come from MARKET_URL and
MARKET_ADMIN_TOKEN (or --server / --token). The token is the ADMIN_TOKEN
secret set on the Worker; for the copy run on this PC (dev/serve.mjs) it is
"local-admin-token-0000".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def call(server: str, token: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.rstrip("/") + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "TetoRelay admin"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["queue", "live", "reject", "remove", "demo", "ban", "unban"])
    ap.add_argument("id", nargs="?")
    ap.add_argument("--server", default=os.environ.get("MARKET_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--token", default=os.environ.get("MARKET_ADMIN_TOKEN", "local-admin-token-0000"))
    args = ap.parse_args(argv)
    if args.action == "queue":
        status, raw = call(args.server, args.token, "GET", "/v1/admin/queue")
        if status != 200:
            print(f"{status}: {raw.decode(errors='replace')}")
            return 1
        data = json.loads(raw)
        if not data["items"]:
            print("Nothing waiting.")
        for it in data["items"]:
            size = (it.get("file") or {}).get("size", 0)
            who = f"@{it['user']['handle']}" if it.get("user") else (it["author"] or "?")
            price = f"  {it['price']} {it['currency']} at {it['shop_url']}" if it.get("price") else ""
            print(f"[{it['status']}] {it['id']}  {it['kind']}  {it['name']!r} by {who}{price}"
                  f"  {size / 1048576:.1f} MB  reports: {it.get('reports', 0)}")
            if it.get("licence"):
                print(f"      licence: {it['licence']}")
            if it.get("description"):
                print(f"      {it['description'][:200]}")
        for rep in data.get("reports", []):
            print(f"  report on {rep['item_id']}: {rep['reason']}")
        return 0
    if not args.id:
        ap.error("give the listing's id")
    if args.action in ("ban", "unban"):
        status, raw = call(args.server, args.token, "POST", f"/v1/admin/users/{args.id.lstrip('@')}",
                           {"status": "banned" if args.action == "ban" else "active"})
        print("Done." if status == 200 else f"{status}: {raw.decode(errors='replace')}")
        return 0 if status == 200 else 1
    if args.action == "demo":
        req = urllib.request.Request(f"{args.server.rstrip('/')}/v1/files/{args.id}/demo")
        with urllib.request.urlopen(req, timeout=30) as r:
            out = f"{args.id}-demo"
            open(out, "wb").write(r.read())
        print(f"Saved {out}")
        return 0
    status, raw = call(args.server, args.token, "POST", f"/v1/admin/items/{args.id}",
                       {"status": {"live": "live", "reject": "rejected", "remove": "removed"}[args.action]})
    print("Done." if status == 200 else f"{status}: {raw.decode(errors='replace')}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
