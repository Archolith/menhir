"""Live smoke of the auth/OAuth surface across launch shapes.

Spins up a throwaway Menhir per shape (via scripts/dev/test_server.py, backendless
auth-only scope — no Neo4j) and asserts the behavior of the auth middleware and the
6207fa3 hardening guards against a *running server*, complementing the unit tests:

- no-auth      : protected route passes auth (no 401).
- static       : missing/wrong key -> 401; correct key -> passes auth (503 backend, not 401).
- client-token : loopback bootstrap mints once (200); a forwarding header blocks
                 bootstrap (401, CT-001); once a token exists bootstrap is closed (401).
- oauth        : no token -> 401 + Bearer challenge; ?api_key= rejected; a token against a
                 dead JWKS -> 503 (IdP-outage, N-003), not 401; CORS preflight passes (N-002).
- oauth-as     : discovery metadata is unauthenticated and well-formed.

Exit code 0 = all pass. Non-zero = a check failed (prints the failures).

Run:  python scripts/smoke/auth_shapes_smoke.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
from dev.test_server import DEAD_JWKS_URI, TEST_KEYS, launch  # noqa: E402

PASS, FAIL = "PASS", "FAIL"


def _req(url: str, *, method: str = "GET", headers: dict | None = None):
    """Return (status_code, headers_dict, body_text). Never raises on HTTP error."""
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=6) as r:  # noqa: S310 (loopback)
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


def check_no_auth(c: Checks) -> None:
    with launch("no-auth") as s:
        code, _, _ = _req(f"{s.base_url}/mcp/test")
        # No keys configured -> auth passes through; backendless -> not a 401.
        c.expect("no-auth: /mcp passes auth (not 401)", code != 401, f"code={code}")


def check_static(c: Checks) -> None:
    with launch("static") as s:
        code_none, _, _ = _req(f"{s.base_url}/api/stats")
        c.eq("static: no key -> 401", code_none, 401)
        code_bad, _, _ = _req(f"{s.base_url}/api/stats", headers={"Authorization": "Bearer nope"})
        c.eq("static: wrong key -> 401", code_bad, 401)
        code_ok, _, _ = _req(f"{s.base_url}/api/stats",
                             headers={"Authorization": f"Bearer {TEST_KEYS['operator']}"})
        # Auth passes; backend absent -> 503 (the point is: NOT 401).
        c.expect("static: operator key passes auth (not 401)", code_ok != 401, f"code={code_ok}")


def check_client_token(c: Checks) -> None:
    with launch("client-token") as s:
        mint_url = f"{s.base_url}/api/admin/clients"
        body = json.dumps({"client_name": "smoke", "tier": "operator"}).encode()

        # CT-001: a forwarding header blocks the loopback bootstrap.
        code_xff, _, _ = _req(mint_url, method="POST",
                              headers={"Content-Type": "application/json",
                                       "X-Forwarded-For": "203.0.113.9"})
        c.eq("client-token: bootstrap w/ X-Forwarded-For -> 401 (CT-001)", code_xff, 401)

        # Clean loopback bootstrap mints the first token.
        req = urllib.request.Request(mint_url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:  # noqa: S310
                code_mint, mint_body = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code_mint, mint_body = e.code, {}
        c.eq("client-token: clean loopback bootstrap -> 200", code_mint, 200)
        c.expect("client-token: mint returns a token", bool(mint_body.get("token")),
                 f"body keys={list(mint_body)}")

        # Bootstrap is now closed (store has an active token).
        code_again, _, _ = _req(mint_url, method="POST",
                                headers={"Content-Type": "application/json"})
        c.eq("client-token: bootstrap closed after first mint -> 401", code_again, 401)


def check_oauth(c: Checks) -> None:
    with launch("oauth", jwks_uri=DEAD_JWKS_URI) as s:
        # No token -> 401 + Bearer challenge advertising resource metadata.
        code_no, hdrs_no, _ = _req(f"{s.base_url}/mcp/test")
        c.eq("oauth: no token -> 401", code_no, 401)
        c.expect("oauth: Bearer challenge advertises resource_metadata",
                 "resource_metadata=" in hdrs_no.get("www-authenticate", ""),
                 hdrs_no.get("www-authenticate", "<none>"))

        # ?api_key= is rejected under OAuth.
        code_key, _, _ = _req(f"{s.base_url}/mcp/test?api_key=whatever")
        c.eq("oauth: ?api_key= rejected -> 401", code_key, 401)

        # N-003: a real-looking token against a DEAD JWKS -> IdP outage -> 503, not 401.
        code_out, hdrs_out, _ = _req(f"{s.base_url}/mcp/test",
                                     headers={"Authorization": "Bearer eyJ.fake.token"})
        c.eq("oauth: token vs dead JWKS -> 503 (N-003)", code_out, 503)
        c.expect("oauth: outage has no Bearer challenge (N-003)",
                 "www-authenticate" not in {k.lower() for k in hdrs_out},
                 f"headers={list(hdrs_out)}")

        # N-002: CORS preflight (OPTIONS + Origin, no auth) is not 401'd by the middleware.
        code_pre, _, _ = _req(f"{s.base_url}/api/stats", method="OPTIONS",
                              headers={"Origin": "https://app.example.com",
                                       "Access-Control-Request-Method": "GET"})
        c.expect("oauth: CORS preflight not blocked by auth (N-002)", code_pre != 401,
                 f"code={code_pre}")


def check_oauth_as(c: Checks) -> None:
    with launch("oauth-as") as s:
        code, _, body = _req(f"{s.base_url}/.well-known/oauth-protected-resource")
        c.eq("oauth-as: protected-resource metadata unauthenticated -> 200", code, 200)
        try:
            doc = json.loads(body)
            c.expect("oauth-as: metadata has 'resource'", "resource" in doc, f"keys={list(doc)}")
        except Exception:  # noqa: BLE001
            c.expect("oauth-as: metadata is JSON", False, body[:120])


def main() -> int:
    c = Checks()
    for fn in (check_no_auth, check_static, check_client_token, check_oauth, check_oauth_as):
        try:
            fn(c)
        except Exception as e:  # noqa: BLE001
            c.expect(f"{fn.__name__}: launch/run", False, f"{type(e).__name__}: {e}")

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
