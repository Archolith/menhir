#!/usr/bin/env python3
"""Provision a clean menhir API + client grant in Auth0 via the Management API.

Creates a resource server with identifier $MENHIR_API_ID (no trailing space),
the three menhir:* scopes, RBAC + permissions-in-token enabled, then grants the
test M2M app all three scopes. Idempotent-ish: reports if the identifier exists.

Env:
    AUTH0_DOMAIN
    AUTH0_MGMT_CLIENT_ID / AUTH0_MGMT_CLIENT_SECRET   (Management API app)
    MENHIR_API_ID                                     (target identifier)
    GRANT_CLIENT_ID                                   (M2M app to authorize)
"""
from __future__ import annotations

import http.client
import json
import os
import sys


def _req(conn, method, path, token, body=None):
    headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    res = conn.getresponse()
    raw = res.read().decode("utf-8")
    try:
        return res.status, json.loads(raw) if raw else {}
    except Exception:
        return res.status, raw


def _mgmt_token(conn, domain, cid, secret):
    conn.request("POST", "/oauth/token", json.dumps({
        "client_id": cid, "client_secret": secret,
        "audience": f"https://{domain}/api/v2/", "grant_type": "client_credentials",
    }), {"content-type": "application/json"})
    body = json.loads(conn.getresponse().read())
    if "access_token" not in body:
        raise SystemExit(f"mgmt token failed: {body}")
    return body["access_token"]


def main() -> int:
    domain = os.environ["AUTH0_DOMAIN"]
    api_id = os.environ["MENHIR_API_ID"]
    grant_client = os.environ["GRANT_CLIENT_ID"]
    conn = http.client.HTTPSConnection(domain)
    tok = _mgmt_token(conn, domain, os.environ["AUTH0_MGMT_CLIENT_ID"], os.environ["AUTH0_MGMT_CLIENT_SECRET"])

    if api_id != api_id.strip():
        raise SystemExit(f"refusing: MENHIR_API_ID has surrounding whitespace: {api_id!r}")

    # 1. Does an API with this exact identifier already exist?
    st, servers = _req(conn, "GET", "/api/v2/resource-servers", tok)
    existing = next((s for s in servers if s.get("identifier") == api_id), None)
    if existing:
        print(f"[skip] resource server already exists: id={existing['id']}")
        rs_id = existing["id"]
    else:
        st, rs = _req(conn, "POST", "/api/v2/resource-servers", tok, {
            "name": "menhir",
            "identifier": api_id,
            "signing_alg": "RS256",
            "scopes": [
                {"value": "menhir:read", "description": "Read Menhir memory"},
                {"value": "menhir:write", "description": "Write Menhir memory"},
                {"value": "menhir:admin", "description": "Menhir operator/admin actions"},
            ],
            "enforce_policies": True,          # RBAC
            "token_dialect": "access_token_authz",  # put permissions in the access token
        })
        if st not in (200, 201):
            raise SystemExit(f"create resource server failed HTTP {st}: {rs}")
        rs_id = rs["id"]
        print(f"[ok] created resource server id={rs_id} identifier={api_id!r}")

    # 2. Grant the M2M app all three scopes for this audience.
    st, grants = _req(conn, "GET", "/api/v2/client-grants", tok)
    g = next((x for x in grants if x.get("client_id") == grant_client and x.get("audience") == api_id), None)
    scopes = ["menhir:read", "menhir:write", "menhir:admin"]
    if g:
        print(f"[skip] client grant already exists: id={g['id']} scope={g.get('scope')}")
    else:
        st, cg = _req(conn, "POST", "/api/v2/client-grants", tok, {
            "client_id": grant_client, "audience": api_id, "scope": scopes,
        })
        if st not in (200, 201):
            raise SystemExit(f"create client grant failed HTTP {st}: {cg}")
        print(f"[ok] granted {grant_client} -> {api_id!r} scope={scopes}")

    print("\n[done] clean menhir API provisioned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
