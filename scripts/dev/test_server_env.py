"""Shape environment construction for the throwaway test-server launcher.

Builds the *complete* subprocess environment for each auth shape from scratch
so nothing leaks in from the repo ``.env`` or the caller's shell.
"""

from __future__ import annotations

import os
from pathlib import Path

from .test_server_util import SHAPES, TEST_KEYS, _validated_public_origin


def _shape_env(shape: str, *, port: int, host: str, workdir: Path, jwks_uri: str,
               backend: str, instance_id: str,
               neo4j: tuple[str, str, str] | None = None,
               oauth: dict[str, str] | None = None,
               public_base_url: str | None = None,
               oauth_refresh: bool = False,
               oauth_access_ttl_s: int | None = None) -> dict[str, str]:
    """Build the *complete* environment for a shape from scratch (no repo leakage)."""
    if shape not in SHAPES:
        raise ValueError(f"unknown shape {shape!r}; choose from {SHAPES}")
    if public_base_url is not None:
        public_base_url = _validated_public_origin(public_base_url)

    # Minimal base: just enough for Python + uvicorn to run. No MENHIR_*/NEO4J_*/
    # OPENAI_* inherited from the caller's shell.
    passthrough = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP",
                   "PATHEXT", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
                   "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "LANG", "LC_ALL")
    env: dict[str, str] = {k: os.environ[k] for k in passthrough if k in os.environ}

    # Identity handshake: the server echoes this in /api/health, and the launcher
    # verifies it before handing the URL to the caller. Guarantees we never talk
    # to a *different* process that happens to hold this port.
    env["MENHIR_INSTANCE_ID"] = instance_id

    # An empty ENV_FILE that exists -> the explicit load_dotenv(ENV_FILE) calls
    # load nothing from the repo.
    env_file = workdir / "empty.env"
    env_file.write_text("", encoding="utf-8")
    env["ENV_FILE"] = str(env_file)
    # Belt-and-suspenders: the server's import chain also does a *cwd-relative*
    # dotenv auto-load that ignores ENV_FILE. We run the subprocess from an
    # isolated cwd (the workdir, set at launch) and drop an empty ``.env`` there
    # so that cwd-relative search finds nothing instead of the repo's real keys.
    (workdir / ".env").write_text("", encoding="utf-8")

    env["MENHIR_API_HOST"] = host
    env["MENHIR_API_PORT"] = str(port)

    # Backend isolation.
    #  - "none" (default): reduced startup scope — the memory backend is NOT
    #    started, so no Neo4j is contacted at all and the auth/OAuth surface
    #    comes up instantly. Backend-dependent routes return 503. This is the
    #    right mode for auth testing and needs no Docker.
    #  - "neo4j": full startup scope against the provided throwaway Neo4j
    #    (*neo4j* = (uri, user, password)). Populates the graph adapter so
    #    backend routes (e.g. /api/tool-events) work. No LLM/OpenAI required —
    #    the server degrades LLM features but the graph adapter is built from
    #    Neo4j alone.
    if backend == "neo4j":
        if neo4j is None:
            raise ValueError("backend='neo4j' requires a neo4j=(uri,user,password) tuple")
        uri, user, password = neo4j
        env["MENHIR_STARTUP_SCOPE"] = "full"
        env["MENHIR_ALLOW_SYSTEM_PYTHON"] = "1"
        env["NEO4J_URI"] = uri
        env["NEO4J_USER"] = user
        env["NEO4J_PASSWORD"] = password
        env["WORKSPACE_ROOT"] = str(workdir)
        env["MENHIR_MCP_TELEMETRY_DB"] = str(workdir / "mcp_telemetry.db")
    else:  # "none"
        env["MENHIR_STARTUP_SCOPE"] = "auth-only"
        # A dead Neo4j URI as a belt-and-suspenders guarantee we never reach the
        # real graph even if the scope gate were bypassed.
        env["NEO4J_URI"] = "bolt://127.0.0.1:7699"
        env["NEO4J_USER"] = "neo4j"
        env["NEO4J_PASSWORD"] = "throwaway"

    # Isolated stores for the client-token / AS SQLite dbs + signing key.
    env["MENHIR_OAUTH_AS_DIR"] = str(workdir / "oauth-store")
    (workdir / "oauth-store").mkdir(parents=True, exist_ok=True)

    if shape == "static":
        env["MENHIR_OPERATOR_KEY"] = TEST_KEYS["operator"]
        env["MENHIR_AGENT_KEY"] = TEST_KEYS["agent"]
        env["MENHIR_READONLY_KEY"] = TEST_KEYS["readonly"]
    elif shape == "client-token":
        env["MENHIR_CLIENT_TOKENS_ENABLED"] = "1"
    elif shape == "oauth":
        env["MENHIR_OAUTH_ENABLED"] = "true"
        env["MENHIR_PUBLIC_BASE_URL"] = public_base_url or f"http://{host}:{port}"
        # Defaults exercise the local dead-JWKS outage path. A real external IdP
        # (e.g. Auth0) is driven by passing `oauth={issuer,jwks_uri,audience,
        # authorization_servers}` to launch(); explicit values win over defaults.
        env["MENHIR_OAUTH_ISSUER"] = "https://idp.test.local/"
        env["MENHIR_OAUTH_JWKS_URI"] = jwks_uri
        env["MENHIR_OAUTH_AUDIENCE"] = f"http://{host}:{port}/mcp-http"
        # Required for the protected-resource metadata endpoint to render (200).
        env["MENHIR_AUTHORIZATION_SERVERS"] = "https://idp.test.local/"
        if oauth:
            if oauth.get("issuer"):
                env["MENHIR_OAUTH_ISSUER"] = oauth["issuer"]
            if oauth.get("jwks_uri"):
                env["MENHIR_OAUTH_JWKS_URI"] = oauth["jwks_uri"]
            if oauth.get("audience"):
                env["MENHIR_OAUTH_AUDIENCE"] = oauth["audience"]
            if oauth.get("authorization_servers"):
                env["MENHIR_AUTHORIZATION_SERVERS"] = oauth["authorization_servers"]
    elif shape == "oauth-as":
        env["MENHIR_OAUTH_AS_ENABLED"] = "1"
        env["MENHIR_OAUTH_ENABLED"] = "true"
        env["MENHIR_PUBLIC_BASE_URL"] = public_base_url or f"http://{host}:{port}"
        if oauth_refresh:
            env["MENHIR_OAUTH_AS_REFRESH_TOKENS_ENABLED"] = "1"
            env["MENHIR_OAUTH_AS_REFRESH_WITHOUT_OFFLINE_ACCESS_ENABLED"] = "1"
        if oauth_access_ttl_s is not None:
            env["MENHIR_OAUTH_AS_ACCESS_TTL_S"] = str(oauth_access_ttl_s)
        # This is a deliberately public, fixed test credential for an isolated
        # throwaway graph. It must never be used for a real Menhir deployment.
        env["MENHIR_OPERATOR_KEY"] = TEST_KEYS["operator"]
    # no-auth: nothing extra (loopback + no keys)

    return env
