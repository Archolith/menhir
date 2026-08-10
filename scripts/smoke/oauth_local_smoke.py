#!/usr/bin/env python3
"""OAuth local smoke — validates OAuth resource-server behavior of a Menhir HTTP service.

Self-serves a throwaway OAuth-mode server by default (no external service, no
prod risk); use --url to target an already-running instance instead.
No tokens, no secrets, no IdP, no JWKS fetch, no Neo4j, no Docker, no model calls.

Usage:
    python scripts/smoke/oauth_local_smoke.py                     # self-serve
    python scripts/smoke/oauth_local_smoke.py --json
    python scripts/smoke/oauth_local_smoke.py --url http://127.0.0.1:8099
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Own dir on path so `from _server import ...` resolves regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_BASE_URL = "http://127.0.0.1:8100"

CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"


def _resolve_base_url() -> str:
    return (os.environ.get("MENHIR_SMOKE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _normalize_headers(raw: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in raw.items()}


def _http_get(url: str, timeout: float = 10.0) -> tuple[int, dict, dict[str, str]]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            headers = _normalize_headers(dict(resp.headers.items()))
            return resp.status, body, headers
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            body = {"_raw": body_text}
        headers = _normalize_headers(dict(exc.headers.items()))
        return exc.code, body, headers
    except Exception as exc:
        return -1, {"_error": str(exc)}, {}


def _validate_bearer_challenge(headers: dict[str, str], label: str) -> tuple[str, str] | None:
    www_auth = headers.get("www-authenticate", "")
    if not www_auth:
        return CHECK_FAIL, f"{label} missing WWW-Authenticate header"
    if "Bearer" not in www_auth:
        return CHECK_FAIL, f"{label} WWW-Authenticate is not Bearer: {www_auth}"
    if "resource_metadata=" not in www_auth:
        return CHECK_FAIL, f"{label} Bearer challenge missing resource_metadata=: {www_auth}"
    return None


def check_metadata(base: str) -> tuple[str, str]:
    url = f"{base}/.well-known/oauth-protected-resource"
    status, body, _ = _http_get(url)
    if status != 200:
        return CHECK_FAIL, f"metadata returned {status}, expected 200"
    if not isinstance(body, dict):
        return CHECK_FAIL, "metadata body is not JSON"
    if "resource" not in body:
        return CHECK_FAIL, "metadata missing 'resource' field"
    if "authorization_servers" not in body:
        return CHECK_FAIL, "metadata missing 'authorization_servers' field"
    if "scopes_supported" not in body:
        return CHECK_FAIL, "metadata missing 'scopes_supported' field"
    return CHECK_PASS, "protected-resource metadata OK"


def check_mcp_bearer_challenge(base: str) -> tuple[str, str]:
    url = f"{base}/mcp"
    status, body, headers = _http_get(url)
    if status != 401:
        return CHECK_FAIL, f"/mcp returned {status}, expected 401"
    result = _validate_bearer_challenge(headers, "/mcp")
    if result is not None:
        return result
    return CHECK_PASS, "/mcp Bearer challenge OK"


def check_mcp_http_bearer_challenge(base: str) -> tuple[str, str]:
    url = f"{base}/mcp-http"
    status, body, headers = _http_get(url)
    if status != 401:
        return CHECK_FAIL, f"/mcp-http returned {status}, expected 401"
    result = _validate_bearer_challenge(headers, "/mcp-http")
    if result is not None:
        return result
    return CHECK_PASS, "/mcp-http Bearer challenge OK"


def _check_api_key_rejection(url: str, label: str) -> tuple[str, str]:
    status, _, headers = _http_get(url)
    if status == 200:
        return CHECK_FAIL, f"{label} returned 200, expected rejection"
    if status != 401:
        return CHECK_FAIL, f"{label} returned {status}, expected 401"
    result = _validate_bearer_challenge(headers, label)
    if result is not None:
        return result
    return CHECK_PASS, f"{label} rejected with Bearer challenge"


def check_mcp_api_key_rejection(base: str) -> tuple[str, str]:
    return _check_api_key_rejection(f"{base}/mcp?api_key=not-a-real-key", "/mcp?api_key=...")


def check_mcp_http_api_key_rejection(base: str) -> tuple[str, str]:
    return _check_api_key_rejection(f"{base}/mcp-http?api_key=not-a-real-key", "/mcp-http?api_key=...")


_CHECKS = [
    ("protected-resource metadata", check_metadata),
    ("/mcp bearer challenge", check_mcp_bearer_challenge),
    ("/mcp-http bearer challenge", check_mcp_http_bearer_challenge),
    ("/mcp ?api_key= rejection", check_mcp_api_key_rejection),
    ("/mcp-http ?api_key= rejection", check_mcp_http_api_key_rejection),
]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OAuth local smoke — validates OAuth resource-server behavior."
    )
    parser.add_argument(
        "--base-url",
        "--url",
        dest="base_url",
        default=None,
        help="Base URL of the Menhir service to check "
        "(default: MENHIR_SMOKE_URL env or %s)" % DEFAULT_BASE_URL,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output",
    )
    return parser.parse_known_args(argv)[0]


def main(argv: list[str] | None = None) -> int:
    """Run the OAuth checks against a resolved base URL and report.

    Pure check-execution layer: it does NOT start a server — it targets whatever
    base URL resolves from --base-url / MENHIR_SMOKE_URL / DEFAULT_BASE_URL. The
    self-serve orchestration lives in ``_cli`` so this stays trivially unit-testable
    against a mocked HTTP layer.
    """
    args = _parse_args(argv or sys.argv[1:])
    base = args.base_url if args.base_url else _resolve_base_url()
    use_json = args.json

    results: list[dict] = []
    all_pass = True

    for name, func in _CHECKS:
        try:
            status, detail = func(base)
        except Exception as exc:  # noqa: BLE001
            status, detail = CHECK_FAIL, str(exc)
        results.append({"check": name, "status": status, "detail": detail})
        if status != CHECK_PASS:
            all_pass = False

    if use_json:
        print(json.dumps({"base_url": base, "checks": results, "all_pass": all_pass}))
    else:
        print(f"OAuth local smoke - {base}")
        print()
        for r in results:
            symbol = "OK" if r["status"] == CHECK_PASS else "FAIL"
            print(f"  [{symbol}] {r['check']}")
            if r["status"] != CHECK_PASS:
                print(f"         {r['detail']}")
        print()
        print(f"Result: {'ALL PASS' if all_pass else 'SOME FAILURES'}")

    return 0 if all_pass else 1


def _cli(argv: list[str] | None = None) -> int:
    """CLI entry: self-serve a throwaway OAuth-mode server unless a target is given.

    If neither ``--base-url``/``--url`` nor ``MENHIR_SMOKE_URL`` is set, launch a
    throwaway OAuth-mode Menhir (dead JWKS so the outage/challenge paths are
    exercisable) on a free port with the instance-id handshake, and point the
    checks at it — no external service, no prod risk. Explicit targets skip the
    launch and hit the running server directly.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parse_args(argv)
    external = args.base_url or os.environ.get("MENHIR_SMOKE_URL")
    if external:
        return main(argv)

    from _server import smoke_server

    with smoke_server("oauth", url=None) as base:
        # Re-invoke the pure check layer against the throwaway server. Preserve
        # any other flags (e.g. --json) the caller passed.
        passthrough = [a for a in argv if a not in ("--base-url", "--url")]
        return main(["--base-url", base, *passthrough])


if __name__ == "__main__":
    raise SystemExit(_cli())
