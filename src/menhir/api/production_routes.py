"""Minimal health and maintenance routes for the production HTTP surface."""

from __future__ import annotations

import base64
import asyncio
import hmac
import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from menhir import __version__
from menhir.api.oauth import JWKS_REFRESH_STATS, OAuthAuthenticationError
from menhir.mcp.served_metadata import release_commit, served_mcp_metadata_digest


_LOG = logging.getLogger(__name__)
router = APIRouter()


def _runtime_mode(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return str(getattr(settings, "runtime_mode", "production"))


def _instance_id(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return str(getattr(settings, "instance_id", ""))


def _capabilities(request: Request) -> Any:
    runtime_ctx = getattr(request.app.state, "runtime_ctx", None)
    return getattr(runtime_ctx, "capabilities", None)


@router.get("/livez", include_in_schema=False)
async def livez(request: Request) -> dict[str, str]:
    """Report process liveness without disclosing dependency details."""

    return {"status": "alive", "mode": _runtime_mode(request)}


async def _caller_may_see_build(request: Request) -> bool:
    """True only for a single bearer token the MCP endpoint would also accept.

    /readyz sits outside BearerAuthMiddleware because health checks carry no credential, so this
    repeats that middleware's OAuth checks: the token, then the client policy. Any doubt means no
    build block rather than an error, because a bad token must not change the readiness answer.
    """

    headers = getattr(request, "headers", None)
    if headers is None:
        return False
    values = headers.getlist("authorization")
    if len(values) != 1 or not values[0].startswith("Bearer "):
        return False
    verifier = getattr(request.app.state, "oauth_verifier", None)
    policy = getattr(request.app.state, "client_policy", None)
    if verifier is None or policy is None:
        return False
    try:
        principal = await verifier.verify_access_token(values[0][len("Bearer "):])
        policy.require_client(
            client_id=principal.client_id,
            scopes=principal.scopes,
            tier=principal.tier,
        )
    except (OAuthAuthenticationError, PermissionError) as exc:
        _LOG.debug("readyz build block refused: %s", type(exc).__name__)
        return False
    except Exception as exc:  # a broken check must not break readiness
        _LOG.warning("readyz build check failed: %s", type(exc).__name__)
        return False
    return True


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """Report mode-aware readiness; the build block goes only to a caller with a valid token."""

    mode = _runtime_mode(request)
    runtime_ctx = getattr(request.app.state, "runtime_ctx", None)
    capabilities = _capabilities(request)
    failures: list[str] = []

    if runtime_ctx is None or capabilities is None:
        failures.append("runtime_not_initialized")
    elif not bool(getattr(capabilities, "neo4j_ready", False)):
        failures.append("neo4j_unavailable")

    if getattr(request.app.state, "oauth_signing_key", None) is None:
        failures.append("oauth_key_unavailable")

    if mode == "candidate-readonly":
        if not bool(getattr(request.app.state, "mutation_fence_active", False)):
            failures.append("mutation_fence_inactive")
    else:
        if capabilities is not None and not bool(
            getattr(capabilities, "enrichment_ready", False)
        ):
            failures.append("enrichment_unavailable")
        if runtime_ctx is not None and getattr(runtime_ctx, "scheduler", None) is None:
            failures.append("sole_writer_scheduler_unavailable")

    ready = not failures
    content: dict[str, Any] = {
        "status": "ready" if ready else "not_ready",
        "mode": mode,
        "mutation_fence": mode == "candidate-readonly" and ready,
        "scheduler": "disabled_by_mode"
        if mode == "candidate-readonly"
        else ("ready" if ready else "unavailable"),
        "failures": failures,
        # Diagnostic only; never affects readiness. Counts are per process.
        "oauth_jwks": JWKS_REFRESH_STATS.snapshot(),
    }
    headers: dict[str, str] | None = None
    if await _caller_may_see_build(request):
        try:
            mcp_digest = await served_mcp_metadata_digest(
                getattr(request.app.state, "mcp_http_instance", None)
            )
        except Exception as exc:  # diagnostic only; never affects readiness
            _LOG.warning("readyz mcp metadata digest unavailable: %s", type(exc).__name__)
            mcp_digest = "unavailable"
        # Diagnostic only; never affects readiness or the status code.
        content["build"] = {
            "version": __version__,
            "commit": release_commit(),
            "mcp_metadata_sha256": mcp_digest,
        }
        # Required because this body differs by caller; a shared cache must not replay it.
        headers = {"Cache-Control": "no-store"}
    return JSONResponse(status_code=200 if ready else 503, content=content, headers=headers)


@router.post("/internal/source-fence", include_in_schema=False)
async def source_fence_probe(request: Request) -> JSONResponse:
    """Return a challenge-bound proof from the fenced source authority.

    This route is intentionally absent from the public Caddy allowlist. It also
    requires a dedicated bearer secret and only responds while the instance is
    in candidate-readonly mode with the mutation fence active.
    """

    settings = getattr(request.app.state, "settings", None)
    token = str(getattr(settings, "source_fence_token", ""))
    private_key_path = str(getattr(settings, "source_fence_private_key_path", ""))
    key_id = str(getattr(settings, "source_fence_key_id", ""))
    release_id = str(getattr(settings, "release_id", ""))
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token or not hmac.compare_digest(supplied, token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    challenge = request.headers.get("x-menhir-fence-challenge", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", challenge):
        return JSONResponse(status_code=400, content={"error": "invalid_challenge"})
    if _runtime_mode(request) != "candidate-readonly" or not bool(
        getattr(request.app.state, "mutation_fence_active", False)
    ):
        return JSONResponse(status_code=503, content={"error": "source_not_fenced"})
    if not _instance_id(request) or not key_id or not release_id or not private_key_path:
        return JSONResponse(status_code=503, content={"error": "fence_identity_unavailable"})
    claims = {
        "challenge": challenge,
        "instance_id": _instance_id(request),
        "key_id": key_id,
        "mutation_fence": True,
        "release_id": release_id,
        "runtime_mode": "candidate-readonly",
    }
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":"))
    try:
        key_bytes = await asyncio.to_thread(Path(private_key_path).read_bytes)
        private_key = serialization.load_pem_private_key(key_bytes, password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError("source-fence key is not Ed25519")
        proof = base64.urlsafe_b64encode(
            private_key.sign(payload.encode("utf-8"))
        ).rstrip(b"=").decode("ascii")
    except (OSError, ValueError, TypeError):
        return JSONResponse(status_code=503, content={"error": "fence_signer_unavailable"})
    return JSONResponse(
        status_code=200,
        content={**claims, "signature": proof},
        headers={"Cache-Control": "no-store"},
    )


async def candidate_mutation_unavailable() -> JSONResponse:
    """Return the bounded refusal used by disabled OAuth-authority routes."""

    return JSONResponse(
        status_code=503,
        content={
            "error": "temporarily_unavailable",
            "error_description": "candidate-readonly mode does not admit authority mutations",
        },
        headers={"Retry-After": "60", "Cache-Control": "no-store"},
    )


__all__ = ["candidate_mutation_unavailable", "router", "source_fence_probe"]
