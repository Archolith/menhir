"""Live smoke: Menhir OAuth resource-server against a real Auth0 tenant.

The SaaS-IdP counterpart to the local mock-IdP coverage. It mints a genuine
Auth0 client-credentials access token, launches a throwaway Menhir in the
``oauth`` shape pointed at Auth0's *real* issuer + JWKS + audience, and asserts
Menhir validates the real RS256 token end to end:

- a valid Auth0 token -> passes auth (not 401/403; backendless => 503 backend, which
  proves the token was accepted and the request reached a protected handler).
- no token           -> 401 + Bearer challenge advertising resource metadata.
- a token for the WRONG audience (Auth0 Management API) -> 401 (aud mismatch).
- a structurally-valid but bogus token -> 401 (signature fails against real JWKS),
  NOT 503 (the JWKS is reachable, so this is a token error, not an outage).

Credentials come from the environment so nothing sensitive is committed:

    AUTH0_DOMAIN=dev-xxxx.us.auth0.com
    AUTH0_CLIENT_ID=...            # the M2M app granted the menhir:* scopes
    AUTH0_CLIENT_SECRET=...
    AUTH0_AUDIENCE=https://menhir-test/api

If AUTH0_DOMAIN/secret are unset the smoke SKIPS (exit 0) rather than failing, so
it is safe to include in run_all on machines without the tenant configured.

Run:  python scripts/smoke/auth0_live_smoke.py
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
from dev.test_server import launch  # noqa: E402

PASS, FAIL = "PASS", "FAIL"


def _mint_token(domain: str, client_id: str, client_secret: str, audience: str) -> str:
    conn = http.client.HTTPSConnection(domain, timeout=15)
    payload = json.dumps({
        "client_id": client_id, "client_secret": client_secret,
        "audience": audience, "grant_type": "client_credentials",
    })
    conn.request("POST", "/oauth/token", payload, {"content-type": "application/json"})
    res = conn.getresponse()
    body = json.loads(res.read().decode("utf-8"))
    if "access_token" not in body:
        raise RuntimeError(f"Auth0 token mint failed (HTTP {res.status}): {body}")
    return body["access_token"]


def _req(url: str, *, method: str = "GET", headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 (loopback)
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return None, {}, f"ERR:{type(e).__name__}:{e}"


class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def expect(self, label: str, ok: bool, detail: str = "") -> None:
        self.results.append((PASS if ok else FAIL, label, detail))

    def eq(self, label: str, got, want) -> None:
        self.expect(label, got == want, f"got {got!r}, want {want!r}")

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.results if s == FAIL)


def main() -> int:
    domain = os.getenv("AUTH0_DOMAIN", "").strip()
    client_id = os.getenv("AUTH0_CLIENT_ID", "").strip()
    client_secret = os.getenv("AUTH0_CLIENT_SECRET", "").strip()
    audience = os.getenv("AUTH0_AUDIENCE", "").strip()
    if not (domain and client_id and client_secret and audience):
        print("[SKIP] Auth0 env not configured "
              "(need AUTH0_DOMAIN/CLIENT_ID/CLIENT_SECRET/AUDIENCE). Skipping live smoke.")
        return 0

    issuer = f"https://{domain}/"
    jwks_uri = f"https://{domain}/.well-known/jwks.json"

    c = Checks()

    # Mint a real menhir-audience token + a wrong-audience (Management API) token.
    try:
        good_token = _mint_token(domain, client_id, client_secret, audience)
    except Exception as e:  # noqa: BLE001
        c.expect("auth0: mint menhir-audience token", False, str(e))
        good_token = ""
    try:
        wrong_aud_token = _mint_token(domain, client_id, client_secret,
                                      f"https://{domain}/api/v2/")
    except Exception:  # noqa: BLE001
        wrong_aud_token = ""  # Management grant may be absent; that check is skipped below.

    oauth_cfg = {
        "issuer": issuer,
        "jwks_uri": jwks_uri,
        "audience": audience,
        "authorization_servers": issuer,
    }
    try:
        with launch("oauth", oauth=oauth_cfg) as s:
            base = s.base_url

            # 1. No token -> 401 + Bearer challenge.
            code_no, hdrs_no, _ = _req(f"{base}/mcp/test")
            c.eq("auth0: no token -> 401", code_no, 401)
            c.expect("auth0: Bearer challenge advertises resource_metadata",
                     "resource_metadata=" in hdrs_no.get("www-authenticate", ""),
                     hdrs_no.get("www-authenticate", "<none>"))

            # 2. Valid Auth0 token -> passes auth. Backendless => not 401/403.
            #    (A 503 here means "authenticated, but backend absent" = auth OK.)
            if good_token:
                code_ok, _, body_ok = _req(f"{base}/mcp/test",
                                           headers={"Authorization": f"Bearer {good_token}"})
                c.expect("auth0: valid token passes auth (not 401/403)",
                         code_ok not in (401, 403), f"code={code_ok} body={body_ok[:120]}")

            # 3. Wrong-audience token (Auth0 Management API) -> 401 (aud mismatch).
            if wrong_aud_token:
                code_wa, _, _ = _req(f"{base}/mcp/test",
                                     headers={"Authorization": f"Bearer {wrong_aud_token}"})
                c.eq("auth0: wrong-audience token -> 401", code_wa, 401)

            # 4. Bogus token against a *reachable* JWKS -> 401 (token error), not 503.
            code_bad, _, _ = _req(f"{base}/mcp/test",
                                  headers={"Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.fake.sig"})
            c.eq("auth0: bogus token vs live JWKS -> 401 (not outage)", code_bad, 401)
    except Exception as e:  # noqa: BLE001
        c.expect("auth0: launch/run", False, f"{type(e).__name__}: {e}")

    for status, label, detail in c.results:
        line = f"[{status}] {label}"
        if status == FAIL and detail:
            line += f"  ({detail})"
        print(line)
    total, failed = len(c.results), c.failed
    print(f"\n{total - failed}/{total} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
