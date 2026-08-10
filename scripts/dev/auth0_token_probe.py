#!/usr/bin/env python3
"""Fetch an Auth0 client-credentials token and decode its claims.

Credentials come from the environment so no secret is stored on disk:

    AUTH0_DOMAIN=dev-xxxx.us.auth0.com
    AUTH0_CLIENT_ID=...
    AUTH0_CLIENT_SECRET=...
    AUTH0_AUDIENCE=https://menhir-test/api   # or pass --audience

Use this to inspect the exact `aud`/`scope`/`permissions` a menhir resource
server will receive before wiring the full OAuth shape.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import sys


def _decode_segment(seg: str) -> dict:
    pad = "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg + pad))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Auth0 client-credentials token probe.")
    ap.add_argument("--audience", default=os.getenv("AUTH0_AUDIENCE", ""),
                    help="API Identifier to request (defaults to $AUTH0_AUDIENCE)")
    args = ap.parse_args(argv)

    domain = os.getenv("AUTH0_DOMAIN", "")
    client_id = os.getenv("AUTH0_CLIENT_ID", "")
    client_secret = os.getenv("AUTH0_CLIENT_SECRET", "")
    missing = [k for k, v in {
        "AUTH0_DOMAIN": domain, "AUTH0_CLIENT_ID": client_id,
        "AUTH0_CLIENT_SECRET": client_secret, "audience": args.audience,
    }.items() if not v]
    if missing:
        print(f"missing: {', '.join(missing)}", file=sys.stderr)
        return 2

    conn = http.client.HTTPSConnection(domain)
    payload = json.dumps({
        "client_id": client_id,
        "client_secret": client_secret,
        "audience": args.audience,
        "grant_type": "client_credentials",
    })
    conn.request("POST", "/oauth/token", payload, {"content-type": "application/json"})
    res = conn.getresponse()
    body = res.read().decode("utf-8")
    print(f"HTTP {res.status} {res.reason}")
    try:
        obj = json.loads(body)
    except Exception:
        print(body)
        return 1
    if "access_token" not in obj:
        print(json.dumps(obj, indent=2))
        return 1
    tok = obj["access_token"]
    print(f"token_type={obj.get('token_type')} expires_in={obj.get('expires_in')} scope={obj.get('scope')!r}")
    parts = tok.split(".")
    if len(parts) != 3:
        print(f"NON-JWT (opaque) token, len={len(tok)} -- menhir cannot validate this "
              "(did you omit the `audience` param?)")
        return 0
    print("--- JWT header ---")
    print(json.dumps(_decode_segment(parts[0]), indent=2))
    print("--- JWT claims (what menhir sees) ---")
    print(json.dumps(_decode_segment(parts[1]), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
