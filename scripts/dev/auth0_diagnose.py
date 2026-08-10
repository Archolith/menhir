#!/usr/bin/env python3
"""Introspect an Auth0 tenant via the Management API to debug audience/grant issues.

Uses a Management-API M2M app (client_credentials against /api/v2/) to list the
tenant's APIs (resource servers) and client grants, so we can see the exact API
identifier and whether a given app is authorized for it.

    AUTH0_DOMAIN=dev-xxxx.us.auth0.com
    AUTH0_MGMT_CLIENT_ID=...        # the app authorized for the Management API
    AUTH0_MGMT_CLIENT_SECRET=...
"""
from __future__ import annotations

import http.client
import json
import os
import sys


def _post_token(conn: http.client.HTTPSConnection, domain: str, cid: str, secret: str) -> str:
    payload = json.dumps({
        "client_id": cid, "client_secret": secret,
        "audience": f"https://{domain}/api/v2/", "grant_type": "client_credentials",
    })
    conn.request("POST", "/oauth/token", payload, {"content-type": "application/json"})
    res = conn.getresponse()
    body = json.loads(res.read().decode("utf-8"))
    if "access_token" not in body:
        raise SystemExit(f"mgmt token failed: {json.dumps(body)}")
    return body["access_token"]


def _get(conn: http.client.HTTPSConnection, path: str, token: str):
    conn.request("GET", path, headers={"authorization": f"Bearer {token}"})
    res = conn.getresponse()
    return res.status, json.loads(res.read().decode("utf-8"))


def main() -> int:
    domain = os.getenv("AUTH0_DOMAIN", "")
    cid = os.getenv("AUTH0_MGMT_CLIENT_ID", "")
    secret = os.getenv("AUTH0_MGMT_CLIENT_SECRET", "")
    if not (domain and cid and secret):
        print("need AUTH0_DOMAIN, AUTH0_MGMT_CLIENT_ID, AUTH0_MGMT_CLIENT_SECRET", file=sys.stderr)
        return 2

    conn = http.client.HTTPSConnection(domain)
    token = _post_token(conn, domain, cid, secret)

    print("=== APIs (resource servers) ===")
    st, servers = _get(conn, "/api/v2/resource_servers", token)
    for s in servers if isinstance(servers, list) else []:
        scopes = [sc.get("value") for sc in s.get("scopes", [])]
        print(f"  name={s.get('name')!r}  identifier={s.get('identifier')!r}")
        print(f"      id={s.get('id')}  rbac scopes={scopes}")

    print("\n=== Client grants (app -> audience : scopes) ===")
    st, grants = _get(conn, "/api/v2/client_grants", token)
    for g in grants if isinstance(grants, list) else []:
        print(f"  client_id={g.get('client_id')}  audience={g.get('audience')!r}  scope={g.get('scope')}")

    print("\n=== Applications (clients) ===")
    st, clients = _get(conn, "/api/v2/clients?fields=client_id,name,app_type&include_fields=true", token)
    for c in clients if isinstance(clients, list) else []:
        print(f"  client_id={c.get('client_id')}  name={c.get('name')!r}  app_type={c.get('app_type')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
