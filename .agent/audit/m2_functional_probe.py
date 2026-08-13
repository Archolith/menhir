#!/usr/bin/env python3
"""Executable evidence probe for the Menhir M2 API/transport/OAuth audit.

Run from a clean checkout whose dependencies are installed:

    python .agent/audit/m2_functional_probe.py

Every behavioral reproduction promoted in the audit report must be represented
here.  The script prints machine-readable JSON records and exits non-zero if an
expected vulnerable behavior cannot be reproduced or a probe itself fails.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable


SCOPE_FILES = (
    "src/menhir/api/routes.py",
    "src/menhir/api/routes_support.py",
    "src/menhir/api/oauth_authorize.py",
    "src/menhir/api/auth.py",
    "src/menhir/api/routes_handlers.py",
    "src/menhir/api/oauth_preflight.py",
    "src/menhir/api/oauth.py",
    "src/menhir/api/client_token_store.py",
    "src/menhir/api/server_support.py",
    "src/menhir/api/oauth_as_register.py",
    "src/menhir/api/oauth_rate_limit.py",
    "src/menhir/api/mcp_remote.py",
    "src/menhir/api/jose_provider.py",
    "src/menhir/api/oauth_token.py",
    "src/menhir/api/auth_code_store.py",
    "src/menhir/api/server.py",
    "src/menhir/api/oauth_keys.py",
    "src/menhir/api/oauth_metadata.py",
    "src/menhir/api/request_context.py",
    "src/menhir/api/oauth_client_store.py",
    "src/menhir/api/oauth_as_metadata.py",
    "src/menhir/api/errors.py",
    "src/menhir/api/auth_mode.py",
    "src/menhir/api/__init__.py",
)
PROMPT_BASELINE_LINES = 5565


def emit(probe: str, **payload: Any) -> None:
    print(json.dumps({"probe": probe, **payload}, sort_keys=True, default=str))


def probe_scope_line_count() -> None:
    root = Path(__file__).resolve().parents[2]
    counts: dict[str, int] = {}
    for relative in SCOPE_FILES:
        path = root / relative
        counts[relative] = len(path.read_text(encoding="utf-8").splitlines())
    total = sum(counts.values())
    emit(
        "scope_line_count",
        file_count=len(counts),
        total=total,
        prompt_baseline=PROMPT_BASELINE_LINES,
        matches_prompt_baseline=total == PROMPT_BASELINE_LINES,
        counts=counts,
    )


async def probe_phase3_reset_tier() -> None:
    from menhir.api.routes_handlers import phase3_reset_impl

    requested_tiers: list[str] = []
    destructive_events: list[str] = []

    class FakeBackend:
        async def delete_namespace(self, namespace: str) -> dict[str, int]:
            destructive_events.append(f"delete_namespace:{namespace}")
            return {"deleted": 7}

    class FakeAdapter:
        def purge_turn_evidence(self, namespace: str) -> int:
            destructive_events.append(f"purge_turn_evidence:{namespace}")
            return 3

    response = await phase3_reset_impl(
        object(),  # the injected collaborators make a concrete Request unnecessary
        "audit-probe-namespace",
        require_tier=requested_tiers.append,
        try_record_destructive_op_rest=lambda name: destructive_events.append(
            f"telemetry:{name}"
        ),
        require_phase3_adapter=lambda request: (object(), FakeAdapter()),
        get_backend=lambda request: FakeBackend(),
    )

    assert requested_tiers == ["agent"], requested_tiers
    assert destructive_events == [
        "telemetry:phase3_reset",
        "delete_namespace:audit-probe-namespace",
        "purge_turn_evidence:audit-probe-namespace",
    ], destructive_events
    assert response.nodes_deleted == 7
    assert response.turn_evidence_deleted == 3
    emit(
        "phase3_reset_tier",
        required_tier=requested_tiers[0],
        destructive_events=destructive_events,
        nodes_deleted=response.nodes_deleted,
        turn_evidence_deleted=response.turn_evidence_deleted,
    )


async def probe_phase3_views_n_plus_one() -> None:
    from menhir.api.routes_handlers import phase3_views_impl

    class FakeAdapter:
        def __init__(self) -> None:
            self.list_calls = 0
            self.history_calls: list[tuple[str, str, str]] = []

        def list_counters(self, *, namespace: str, limit: int) -> list[dict[str, str]]:
            self.list_calls += 1
            return [
                {"subject": f"subject-{index}", "counter": f"counter-{index}"}
                for index in range(3)
            ]

        def counter_history(
            self, *, subject: str, counter: str, namespace: str
        ) -> list[dict[str, Any]]:
            self.history_calls.append((subject, counter, namespace))
            return [{"current": True, "value": 1}]

    adapter = FakeAdapter()
    response = await phase3_views_impl(
        object(),
        "audit-probe-namespace",
        3,
        require_phase3_adapter=lambda request: (object(), adapter),
    )

    assert adapter.list_calls == 1
    assert len(adapter.history_calls) == 3
    assert response.count == 3
    emit(
        "phase3_views_n_plus_one",
        counters_returned=response.count,
        list_queries=adapter.list_calls,
        per_counter_history_queries=len(adapter.history_calls),
        total_adapter_queries=adapter.list_calls + len(adapter.history_calls),
        history_calls=adapter.history_calls,
    )


async def probe_startup_cleanup_gap() -> None:
    from menhir.api import server_support

    events: list[str] = []
    originals = {
        "start_runtime": server_support.start_runtime,
        "stop_runtime": server_support.stop_runtime,
        "MemoryGraphAdapter": server_support.MemoryGraphAdapter,
    }

    async def fake_start_runtime(settings: object) -> object:
        events.append("start_runtime")
        return SimpleNamespace(
            built=SimpleNamespace(neo4j=object()),
            session=SimpleNamespace(session_id="probe-session"),
        )

    async def fake_stop_runtime() -> None:
        events.append("stop_runtime")

    class RaisingMemoryGraphAdapter:
        def __init__(self, **kwargs: object) -> None:
            events.append("MemoryGraphAdapter")
            raise RuntimeError("probe adapter construction failure")

    server_support.start_runtime = fake_start_runtime
    server_support.stop_runtime = fake_stop_runtime
    server_support.MemoryGraphAdapter = RaisingMemoryGraphAdapter
    try:
        lifespan = server_support.build_runtime_lifespan(
            settings=SimpleNamespace(),
            startup_scope="full",
            mcp_http_instance=SimpleNamespace(session_manager=object()),
        )
        app = SimpleNamespace(state=SimpleNamespace())
        observed_error = ""
        try:
            async with lifespan(app):
                raise AssertionError("lifespan unexpectedly entered")
        except RuntimeError as exc:
            observed_error = str(exc)
    finally:
        server_support.start_runtime = originals["start_runtime"]
        server_support.stop_runtime = originals["stop_runtime"]
        server_support.MemoryGraphAdapter = originals["MemoryGraphAdapter"]

    assert observed_error == "probe adapter construction failure"
    assert events == ["start_runtime", "MemoryGraphAdapter"], events
    emit(
        "startup_cleanup_gap",
        events=events,
        stop_runtime_called="stop_runtime" in events,
        observed_error=observed_error,
    )


def _s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def probe_authorization_code_burn() -> None:
    from archolith_oauth import (
        AuthorizationCodeStore,
        AuthorizationServerConfig,
        OAuthClient,
        OAuthClientStore,
        TokenExchangeError,
        TokenIssuer,
        exchange_authorization_code,
    )
    from menhir.api.jose_provider import generate_signing_key

    client_id = "probe-client"
    redirect_uri = "https://client.example/callback"
    resource = "https://menhir.example/mcp-http"
    verifier = "probe-verifier-with-sufficient-entropy-0123456789"
    challenge = _s256_challenge(verifier)

    with tempfile.TemporaryDirectory(prefix="menhir-m2-code-") as tmp:
        db_path = Path(tmp) / "oauth.db"
        client_store = OAuthClientStore(db_path)
        code_store = AuthorizationCodeStore(db_path, ttl_s=120.0)
        client_store.register(
            OAuthClient(
                client_id=client_id,
                client_name="Probe Client",
                redirect_uris=(redirect_uri,),
                scopes=("menhir:read",),
                client_secret_hash="",
                created_at=time.time(),
                token_endpoint_auth_method="none",
                last_exchanged=None,
            )
        )
        config = AuthorizationServerConfig(
            issuer="https://menhir.example",
            resource=resource,
            scopes_supported=("menhir:read",),
            default_scopes=("menhir:read",),
            access_token_ttl_s=3600,
            authorization_code_ttl_s=120,
        )
        issuer = TokenIssuer(config, generate_signing_key())
        code = code_store.issue(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope="menhir:read",
            code_challenge=challenge,
            code_challenge_method="S256",
            resource=resource,
            subject="probe-subject",
        )

        errors: list[dict[str, str]] = []
        try:
            exchange_authorization_code(
                code_store=code_store,
                client_store=client_store,
                issuer=issuer,
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_verifier="definitely-wrong-verifier",
                resource=resource,
            )
        except TokenExchangeError as exc:
            errors.append({"attempt": "wrong_verifier", "error": exc.error})

        try:
            exchange_authorization_code(
                code_store=code_store,
                client_store=client_store,
                issuer=issuer,
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_verifier=verifier,
                resource=resource,
            )
        except TokenExchangeError as exc:
            errors.append({"attempt": "correct_retry", "error": exc.error})

    assert errors == [
        {"attempt": "wrong_verifier", "error": "invalid_grant"},
        {"attempt": "correct_retry", "error": "invalid_grant"},
    ], errors
    emit(
        "authorization_code_burn",
        attempts=errors,
        correct_retry_succeeded=False,
    )


def probe_oauth_preflight_fail_open() -> None:
    from menhir.api.oauth_preflight import (
        _is_https_or_loopback_http,
        _redact_url_credentials,
    )

    safety = {
        "ftp": _is_https_or_loopback_http("ftp://example.com/jwks"),
        "file": _is_https_or_loopback_http("file:///tmp/jwks.json"),
        "malformed_ipv6": _is_https_or_loopback_http("http://[::1"),
        "remote_http": _is_https_or_loopback_http("http://example.com/jwks"),
        "loopback_http": _is_https_or_loopback_http("http://127.0.0.1/jwks"),
        "https": _is_https_or_loopback_http("https://example.com/jwks"),
    }
    raw = "http://user:pass@example.com:not-a-port/jwks"
    redacted = _redact_url_credentials(raw)

    assert safety == {
        "ftp": True,
        "file": True,
        "malformed_ipv6": True,
        "remote_http": False,
        "loopback_http": True,
        "https": True,
    }, safety
    assert redacted == raw, redacted
    emit(
        "oauth_preflight_fail_open",
        safety=safety,
        malformed_credential_url=raw,
        redacted_result=redacted,
        credentials_remained=redacted == raw,
    )


async def run_async_probes() -> None:
    await probe_phase3_reset_tier()
    await probe_phase3_views_n_plus_one()
    await probe_startup_cleanup_gap()


def main() -> int:
    probes: list[tuple[str, Callable[[], None]]] = [
        ("scope_line_count", probe_scope_line_count),
        ("authorization_code_burn", probe_authorization_code_burn),
        ("oauth_preflight_fail_open", probe_oauth_preflight_fail_open),
    ]
    failures: list[dict[str, str]] = []

    for name, probe in probes:
        try:
            probe()
        except Exception as exc:  # keep running so one failure does not hide later evidence
            failures.append({"probe": name, "type": type(exc).__name__, "error": str(exc)})
            emit(name, status="ERROR", error_type=type(exc).__name__, error=str(exc))

    try:
        asyncio.run(run_async_probes())
    except Exception as exc:
        failures.append(
            {"probe": "async_probes", "type": type(exc).__name__, "error": str(exc)}
        )
        emit(
            "async_probes",
            status="ERROR",
            error_type=type(exc).__name__,
            error=str(exc),
        )

    emit("summary", failures=failures, passed=not failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
