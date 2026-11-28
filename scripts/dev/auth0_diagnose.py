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


class _DiagnosticError(RuntimeError):
    """Safe, concise error suitable for diagnostic output."""


def _request_json(conn, method: str, path: str, label: str, *, body=None, headers=None):
    try:
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        payload = response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise _DiagnosticError(f"{label} request failed (network error)") from exc
    if response.status != 200:
        raise _DiagnosticError(f"{label} request failed (HTTP {response.status})")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _DiagnosticError(f"{label} returned an unexpected payload") from exc


def _post_token(conn: http.client.HTTPSConnection, domain: str, cid: str, secret: str) -> str:
    payload = json.dumps({
        "client_id": cid, "client_secret": secret,
        "audience": f"https://{domain}/api/v2/", "grant_type": "client_credentials",
    })
    body = _request_json(conn, "POST", "/oauth/token", "management token",
                         body=payload, headers={"content-type": "application/json"})
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise _DiagnosticError("management token returned an unexpected payload")
    return token


def _get_list(conn: http.client.HTTPSConnection, path: str, label: str, token: str) -> list:
    payload = _request_json(conn, "GET", path, label, headers={"authorization": f"Bearer {token}"})
    if not isinstance(payload, list):
        raise _DiagnosticError(f"{label} returned an unexpected payload")
    if any(not isinstance(entry, dict) for entry in payload):
        raise _DiagnosticError(f"{label} returned malformed entries")
    if label == "resource servers":
        for server in payload:
            scopes = server.get("scopes", [])
            if not isinstance(scopes, list) or any(
                not isinstance(scope, dict)
                or not isinstance(scope.get("value"), str)
                or not scope["value"].strip()
                for scope in scopes
            ):
                raise _DiagnosticError("resource servers returned malformed scopes")
    return payload


def main() -> int:
    domain = os.getenv("AUTH0_DOMAIN", "")
    cid = os.getenv("AUTH0_MGMT_CLIENT_ID", "")
    secret = os.getenv("AUTH0_MGMT_CLIENT_SECRET", "")
    if not (domain and cid and secret):
        print("need AUTH0_DOMAIN, AUTH0_MGMT_CLIENT_ID, AUTH0_MGMT_CLIENT_SECRET", file=sys.stderr)
        return 2

    conn = http.client.HTTPSConnection(domain)
    try:
        token = _post_token(conn, domain, cid, secret)

        print("=== APIs (resource servers) ===")
        servers = _get_list(conn, "/api/v2/resource-servers", "resource servers", token)
        for s in servers:
            scopes = [sc["value"] for sc in s.get("scopes", [])]
            print(f"  name={s.get('name')!r}  identifier={s.get('identifier')!r}")
            print(f"      id={s.get('id')}  rbac scopes={scopes}")

        print("\n=== Client grants (app -> audience : scopes) ===")
        grants = _get_list(conn, "/api/v2/client-grants", "client grants", token)
        for g in grants:
            print(f"  client_id={g.get('client_id')}  audience={g.get('audience')!r}  scope={g.get('scope')}")

        print("\n=== Applications (clients) ===")
        clients = _get_list(
            conn,
            "/api/v2/clients?fields=client_id,name,app_type&include_fields=true",
            "applications",
            token,
        )
        for c in clients:
            print(f"  client_id={c.get('client_id')}  name={c.get('name')!r}  app_type={c.get('app_type')}")
    except _DiagnosticError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
