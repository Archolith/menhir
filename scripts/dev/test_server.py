"""Launch a throwaway Menhir server in a selectable auth *shape* for fast testing.

Why this exists
---------------
Testing the auth/OAuth surface against a live server kept requiring hand-rolled
env + `menhir.cli serve` invocations. Two hazards made that error-prone:

1. A plain `serve` inherits the repo ``.env`` (``load_dotenv(ENV_FILE or None)``
   searches upward), so it boots ``startup_mode=full`` against the **real Neo4j
   backend** and with the **dev bearer keys** — violating the "never touch the
   real backend / never :8090" test rule.
2. Each auth mode needs a different, fiddly env combination.

This launcher removes both. It runs the server with a **fully isolated env**
(no repo ``.env``, no inherited shell vars, a dead/throwaway Neo4j so startup
degrades instead of touching real data) on a **safe port** (default 8099, and
it refuses 8090), in one of a few named *shapes*. Startup is backgrounded in the
server, so ``/api/health`` (auth-exempt) answers immediately even while the
backend init fails in the background — which is exactly what auth-path testing
needs.

No shape requires a secret: bearer keys are fixed test values and OAuth signing
keys are generated locally (joserfc). The client-token / AS SQLite stores go in
a temp dir that is deleted at teardown.

Shapes
------
- ``no-auth``      : no keys configured (loopback dev mode).
- ``static``       : static bearer keys (operator/agent/readonly = known values).
- ``client-token`` : per-client token tier enabled (bootstrap + admin gate).
- ``oauth``        : OAuth resource-server mode. ``--jwks-uri`` selectable;
                     defaults to a dead URI so the IdP-outage path (503) is
                     exercisable.
- ``oauth-as``     : embedded authorization server enabled (DCR/authorize/token).

Usage
-----
CLI (foreground; Ctrl-C to stop, auto-teardown)::

    python scripts/dev/test_server.py --shape client-token --port 8099

For a disposable ChatGPT OAuth acceptance server behind a secure tunnel::

    python scripts/dev/test_server.py --shape oauth-as --backend neo4j \
        --public-base-url https://example-tunnel.example \
        --oauth-refresh --oauth-access-ttl 120 --interactive-control

The OAuth consent key for this throwaway shape is the fixed ``test-operator-key``.

Programmatic (used by scripts/smoke/auth_shapes_smoke.py)::

    from scripts.dev.test_server import launch, TEST_KEYS
    with launch("static", port=8099) as srv:
        ...  # srv.base_url, srv.keys

Safety
------
- Refuses ``--port 8090`` (the real server).
- Never reads the repo ``.env``; the subprocess env is built from scratch.
- Neo4j points at a dead port by default (``--backend none``) so no real graph
  is touched; pass ``--backend neo4j`` to use a disposable Docker Neo4j.
- A public test profile refuses inherited ``MENHIR_TEST_NEO4J_URI`` reuse.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TextIO
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_PORTS = {8090}  # the real server — never bind or probe it here


def free_port() -> int:
    """Return an OS-assigned free localhost TCP port.

    Throwaway servers must never bind a fixed port: a fixed port can collide with
    an already-running instance (a dev/container server on 8099, say), and the
    health-wait would then talk to *that* process. An ephemeral port + the
    instance-id handshake together guarantee isolation.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])

SHAPES = ("no-auth", "static", "client-token", "oauth", "oauth-as")

# Fixed, non-secret test credentials. These are deliberately well-known — the
# whole point is a throwaway server with no real data behind it.
TEST_KEYS = {
    "operator": "test-operator-key",
    "agent": "test-agent-key",
    "readonly": "test-readonly-key",
}

# A JWKS URI that resolves to nothing, so the OAuth path exercises the
# IdP-outage branch (server_error -> 503) deterministically.
DEAD_JWKS_URI = "http://127.0.0.1:9/.well-known/jwks.json"


@dataclass
class RunningServer:
    shape: str
    port: int
    base_url: str
    proc: subprocess.Popen
    workdir: Path
    keys: dict[str, str] = field(default_factory=dict)
    log_path: Path | None = None
    instance_id: str = ""
    # Populated for backend="neo4j" launches: the throwaway Neo4j the server runs
    # against, so a smoke can write graph fixtures directly to the same database.
    neo4j_uri: str = ""
    neo4j_user: str = ""
    neo4j_password: str = ""
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict, repr=False)
    log_file: TextIO | None = field(default=None, repr=False)
    creationflags: int = 0
    preexec_fn: Callable[[], None] | None = field(default=None, repr=False)
    health_timeout_s: float = 30.0

    def tail_log(self, n: int = 40) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])

    def restart(self) -> dict:
        """Restart only Menhir, preserving its stores and throwaway Neo4j."""
        if not self.command or self.log_file is None:
            raise RuntimeError("server restart state is unavailable")
        _terminate(self.proc)
        self.log_file.flush()
        self.proc = subprocess.Popen(  # noqa: S603
            list(self.command),
            cwd=str(self.workdir),
            env=self.env,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            creationflags=self.creationflags,
            preexec_fn=self.preexec_fn,
        )
        return _wait_for_health(
            self.base_url,
            self.health_timeout_s,
            self.proc,
            expect_instance_id=self.instance_id,
        )


def _remove_workdir(path: Path) -> None:
    """Remove sensitive throwaway state and surface any cleanup failure."""
    shutil.rmtree(path)


def _validated_public_origin(value: str) -> str:
    """Return a canonical HTTPS origin suitable for a public OAuth test."""
    raw = value.strip()
    try:
        parts = urlsplit(raw)
        _ = parts.port
    except ValueError as exc:
        raise ValueError("--public-base-url must be a valid HTTPS origin") from exc
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "--public-base-url must be an HTTPS origin without credentials, path, query, or fragment"
        )
    return f"https://{parts.netloc}"


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


@dataclass
class _Neo4jSidecar:
    uri: str
    user: str
    password: str
    container: str | None  # None when reusing an external Neo4j (nothing to tear down)


@contextlib.contextmanager
def _throwaway_neo4j(*, wait_s: float = 90.0, allow_external: bool = True):
    """Provide a throwaway Neo4j for backend-backed launches.

    Fast path: if ``MENHIR_TEST_NEO4J_URI`` is set, reuse that Neo4j (with
    ``MENHIR_TEST_NEO4J_USER`` / ``MENHIR_TEST_NEO4J_PASSWORD``) and start nothing.
    Otherwise start a disposable ``neo4j:5-community`` container on an ephemeral
    bolt port, wait until it accepts connections, and remove it on exit.
    """
    ext = os.getenv("MENHIR_TEST_NEO4J_URI")
    if ext:
        if not allow_external:
            raise RuntimeError(
                "public OAuth test profiles refuse MENHIR_TEST_NEO4J_URI reuse; "
                "unset it so the launcher creates a disposable Docker Neo4j"
            )
        yield _Neo4jSidecar(
            uri=ext,
            user=os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
            password=os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "neo4j"),
            container=None,
        )
        return

    if shutil.which("docker") is None:
        raise RuntimeError(
            "backend='neo4j' needs Docker (or set MENHIR_TEST_NEO4J_URI to an "
            "existing throwaway Neo4j). Docker was not found on PATH."
        )
    bolt = free_port()
    name = f"menhir-smoke-neo4j-{secrets.token_hex(4)}"
    password = "smokethrowaway"
    run = subprocess.run(  # noqa: S603
        ["docker", "run", "-d", "--rm", "--name", name,
         "-p", f"127.0.0.1:{bolt}:7687",
         "-e", f"NEO4J_AUTH=neo4j/{password}",
         "-e", "NEO4J_server_memory_heap_max__size=512m",
         "neo4j:5-community"],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        raise RuntimeError(f"failed to start throwaway Neo4j: {run.stderr.strip()}")
    uri = f"bolt://127.0.0.1:{bolt}"
    try:
        _wait_for_neo4j(uri, "neo4j", password, wait_s)
        yield _Neo4jSidecar(uri=uri, user="neo4j", password=password, container=name)
    finally:
        with contextlib.suppress(Exception):
            subprocess.run(["docker", "rm", "-f", name],  # noqa: S603
                           capture_output=True, text=True, timeout=30)


def _wait_for_neo4j(uri: str, user: str, password: str, timeout_s: float) -> None:
    """Block until the Neo4j at *uri* answers a trivial query, or time out."""
    from neo4j import GraphDatabase  # local import: only needed for backend launches

    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        try:
            driver = GraphDatabase.driver(uri, auth=(user, password))
            try:
                driver.verify_connectivity()
                with driver.session() as s:
                    s.run("RETURN 1").consume()
                return
            finally:
                driver.close()
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            time.sleep(1.0)
    raise TimeoutError(f"throwaway Neo4j not ready after {timeout_s}s: {last_err}")


def _wait_for_health(base_url: str, timeout_s: float, proc: subprocess.Popen,
                     *, expect_instance_id: str) -> dict:
    """Poll /api/health until *our* server answers or *timeout_s* elapses.

    Verifies the health response carries our ``instance_id``. If a *different*
    process holds this port (e.g. a dev server or a stale instance), its health
    response won't match and we fail loudly instead of silently running against
    the wrong server.
    """
    deadline = time.monotonic() + timeout_s
    url = f"{base_url}/api/health"
    last_err = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early (code {proc.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 (loopback)
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = str(exc)
            time.sleep(0.4)
            continue
        got = payload.get("instance_id")
        if got == expect_instance_id:
            return payload
        # A server answered but it is not ours — a foreign process on this port.
        raise RuntimeError(
            f"port {base_url} is held by a different server "
            f"(instance_id={got!r}, expected {expect_instance_id!r}); refusing to "
            f"run against it"
        )
    raise TimeoutError(f"health check timed out after {timeout_s}s: {last_err}")


def _container_secret_mounts(workdir: Path, env: dict[str, str]) -> list[tuple[Path, str]]:
    """Materialize throwaway files required by the production image entrypoint."""
    menhir_dir = workdir / "container-secrets" / "menhir"
    oauth_dir = workdir / "container-secrets" / "oauth"
    policy_dir = workdir / "container-policy"
    menhir_dir.mkdir(parents=True)
    oauth_dir.mkdir(parents=True)
    policy_dir.mkdir(parents=True)

    secret_values = {
        "neo4j-password": env["NEO4J_PASSWORD"],
        "operator-key": env.get("MENHIR_OPERATOR_KEY", TEST_KEYS["operator"]),
        "agent-key": env.get("MENHIR_AGENT_KEY", TEST_KEYS["agent"]),
        "readonly-key": env.get("MENHIR_READONLY_KEY", TEST_KEYS["readonly"]),
        "openai-api-key": env.get("OPENAI_API_KEY", ""),
        "local-llm-api-key": env.get("LOCAL_LLM_API_KEY", ""),
    }
    for name, value in secret_values.items():
        if value:
            path = menhir_dir / name
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)

    consent = oauth_dir / "oauth-consent-secret"
    consent.write_text("throwaway-container-consent", encoding="utf-8")
    consent.chmod(0o600)
    signing_key = oauth_dir / "oauth_signing_key.json"
    signing_key.write_text("{}", encoding="utf-8")
    signing_key.chmod(0o600)
    (policy_dir / "client-policy.json").write_bytes(
        (REPO_ROOT / "deploy" / "client-policy.production.json").read_bytes()
    )
    return [
        (menhir_dir, "/run/secrets/menhir"),
        (oauth_dir, "/run/secrets/oauth"),
        (policy_dir, "/srv/menhir/production/policy"),
    ]


def _container_self_confirmation_mounts(
    env: dict[str, str],
) -> tuple[list[tuple[Path, str]], dict[str, str]]:
    """Map only explicit public verification fixtures into the disposable app image.

    Bind the live confirmation directory, not a copied snapshot, so signatures added/replaced or
    revoked by the host-side test remain visible. Never mount its parent (which may hold private
    signing material). All mounts are made read-only by `_container_command`.
    """
    mounts: list[tuple[Path, str]] = []
    overrides: dict[str, str] = {}
    for setting, target, directory in (
        (
            "MENHIR_CANONICAL_SELF_CONFIRMATION_PUBLIC_KEY_PATH",
            "/run/menhir-self-authority/owner-public.pem",
            False,
        ),
        (
            "MENHIR_CANONICAL_SELF_CONFIRMATION_DIRECTORY",
            "/run/menhir-self-authority/confirmations",
            True,
        ),
    ):
        value = env.get(setting, "").strip()
        if not value:
            continue
        try:
            source = Path(value).resolve(strict=True)
            valid = source.is_dir() if directory else source.is_file()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{setting} must name an existing test fixture") from exc
        if not valid:
            kind = "directory" if directory else "regular file"
            raise ValueError(f"{setting} must name a {kind}")
        mounts.append((source, target))
        overrides[setting] = target
    return mounts, overrides


def _container_command(
    *, image: str, name: str, port: int, workdir: Path, env: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    """Build an isolated production-image invocation without secrets in argv."""
    if not image.strip():
        raise ValueError("container image must be non-empty")
    neo4j = urlsplit(env["NEO4J_URI"])
    if neo4j.hostname not in {"127.0.0.1", "localhost"} or neo4j.port is None:
        raise ValueError("container-image tests require a loopback disposable Neo4j")

    confirmation_mounts, confirmation_env = _container_self_confirmation_mounts(env)
    mounts = [*_container_secret_mounts(workdir, env), *confirmation_mounts]
    prefixes = (
        "ENV_FILE", "GRAPHITI_", "LLM_", "LOCAL_LLM_", "MENHIR_",
        "NEO4J_", "OPENAI_", "SCHEDULER_", "GEMINI_",
    )
    secret_env_keys = {
        "GEMINI_API_KEY", "LOCAL_LLM_API_KEY", "MENHIR_AGENT_KEY",
        "MENHIR_API_KEY", "MENHIR_OPERATOR_KEY", "MENHIR_READONLY_KEY",
        "NEO4J_PASSWORD", "OPENAI_API_KEY",
    }
    container_env = {
        key: value for key, value in env.items()
        if key.startswith(prefixes) and key not in secret_env_keys
    }
    container_env.update({
        "ENV_FILE": "/tmp/empty.env",
        "MENHIR_API_HOST": "0.0.0.0",
        "MENHIR_API_PORT": "8099",
        "MENHIR_OAUTH_AS_DIR": "/tmp/menhir-oauth",
        "MENHIR_MCP_TELEMETRY_DB": "/tmp/menhir-telemetry.db",
        "NEO4J_URI": f"bolt://host.docker.internal:{neo4j.port}",
        "WORKSPACE_ROOT": "/tmp/menhir-workspace",
    })
    container_env.update(confirmation_env)
    docker_env = os.environ.copy()
    for key in secret_env_keys:
        docker_env.pop(key, None)
    docker_env.update(container_env)
    command = [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=512m",
        "--no-healthcheck",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--add-host", "host.docker.internal:host-gateway",
        "-p", f"127.0.0.1:{port}:8099",
    ]
    for source, target in mounts:
        command.extend(["-v", f"{source}:{target}:ro"])
    for key in sorted(container_env):
        command.extend(["-e", key])
    command.append(image)
    return command, docker_env


def resolve_container_image(image: str, *, expected_revision: str) -> tuple[str, str]:
    """Resolve a tag once to an immutable image ID and verify its revision label."""
    inspected = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True,
    )
    if inspected.returncode != 0:
        raise RuntimeError(f"cannot inspect release image {image!r}: {inspected.stderr.strip()}")
    try:
        records = json.loads(inspected.stdout)
        record = records[0]
        image_id = str(record["Id"])
        revision = str(record["Config"]["Labels"]["org.opencontainers.image.revision"])
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"release image {image!r} has invalid identity metadata") from exc
    if not image_id.startswith("sha256:"):
        raise RuntimeError(f"release image {image!r} did not resolve to an immutable ID")
    if revision != expected_revision:
        raise RuntimeError(
            f"release image revision mismatch: expected {expected_revision}, got {revision}"
        )
    return image_id, revision


def _remove_container(name: str, proc: subprocess.Popen) -> None:
    """Remove one exact throwaway container and reap its docker client."""
    removed = None
    for _attempt in range(2):
        removed = subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30,
        )
        if removed.returncode == 0:
            break
        exists = subprocess.run(
            ["docker", "container", "inspect", name],
            capture_output=True, text=True, timeout=30,
        )
        if exists.returncode != 0:
            break
    _terminate(proc)
    if removed is not None and removed.returncode != 0:
        exists = subprocess.run(
            ["docker", "container", "inspect", name],
            capture_output=True, text=True, timeout=30,
        )
        if exists.returncode == 0:
            raise RuntimeError(
                f"throwaway container {name!r} survived removal: {removed.stderr.strip()}"
            )


@contextlib.contextmanager
def launch(shape: str, *, port: int | None = None, host: str = "127.0.0.1",
           backend: str = "none", jwks_uri: str = DEAD_JWKS_URI,
           oauth: dict[str, str] | None = None,
           public_base_url: str | None = None,
           oauth_refresh: bool = False,
           oauth_access_ttl_s: int | None = None,
           python_executable: str | None = None,
           container_image: str | None = None,
           health_timeout_s: float = 30.0, quiet: bool = True):
    """Context manager that starts a shaped throwaway server and tears it down.

    Yields a :class:`RunningServer`. The server is killed and its temp workdir
    removed on exit (even on exception). ``port`` defaults to a free ephemeral
    port so concurrent/adjacent servers never collide; pass one only when you
    need a fixed port.
    """
    if port is None:
        port = free_port()
    if port in FORBIDDEN_PORTS:
        raise ValueError(f"refusing to use port {port} (reserved for the real server)")
    if public_base_url is not None:
        public_base_url = _validated_public_origin(public_base_url)

    with contextlib.ExitStack() as stack:
        # Backend-backed launches bring up a throwaway Neo4j first (and a full
        # startup takes longer, so give health a bigger budget by default).
        neo4j: tuple[str, str, str] | None = None
        if backend == "neo4j":
            sidecar = stack.enter_context(
                _throwaway_neo4j(
                    allow_external=public_base_url is None and container_image is None
                )
            )
            neo4j = (sidecar.uri, sidecar.user, sidecar.password)
            if health_timeout_s < 90.0:
                health_timeout_s = 90.0

        workdir = Path(tempfile.mkdtemp(prefix=f"menhir-test-{shape}-{port}-"))
        # Cleanup failures are material here: this directory contains OAuth
        # signing keys and token stores, so never silently leave it behind.
        stack.callback(_remove_workdir, workdir)
        log_path = workdir / "server.log"
        instance_id = f"smoke-{shape}-{secrets.token_hex(8)}"
        env = _shape_env(shape, port=port, host=host, workdir=workdir,
                         jwks_uri=jwks_uri, backend=backend, instance_id=instance_id,
                         neo4j=neo4j, oauth=oauth,
                         public_base_url=public_base_url,
                         oauth_refresh=oauth_refresh,
                         oauth_access_ttl_s=oauth_access_ttl_s)
        base_url = f"http://{host}:{port}"

        container_name = ""
        process_env = env
        if container_image:
            if backend != "neo4j":
                raise ValueError("container-image tests require backend='neo4j'")
            if python_executable:
                raise ValueError("container_image and python_executable are mutually exclusive")
            container_name = f"menhir-smoke-app-{secrets.token_hex(4)}"
            cmd, process_env = _container_command(
                image=container_image, name=container_name, port=port,
                workdir=workdir, env=env,
            )
        elif python_executable:
            py = Path(python_executable)
            if not py.is_file():
                raise FileNotFoundError(f"requested Python interpreter does not exist: {py}")
            cmd = [str(py), "-m", "menhir.cli", "serve", "--host", host, "--port", str(port)]
        else:
            py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
            if not py.exists():
                py = Path(sys.executable)
            cmd = [str(py), "-m", "menhir.cli", "serve", "--host", host, "--port", str(port)]

        log_f = stack.enter_context(log_path.open("w", encoding="utf-8"))
        # New process group so we can signal the whole tree on teardown.
        creationflags = 0
        preexec = None
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            preexec = os.setsid  # type: ignore[assignment]

        # Run from the isolated workdir (NOT the repo) so cwd-relative dotenv
        # auto-loading cannot pick up the repo's real keys. menhir is venv-installed,
        # so `python -m menhir.cli` resolves regardless of cwd.
        proc = subprocess.Popen(  # noqa: S603
            cmd, cwd=str(workdir), env=process_env, stdout=log_f, stderr=subprocess.STDOUT,
            creationflags=creationflags, preexec_fn=preexec,
        )
        srv = RunningServer(shape=shape, port=port, base_url=base_url, proc=proc,
                            workdir=workdir, keys=dict(TEST_KEYS), log_path=log_path,
                            instance_id=instance_id,
                            neo4j_uri=neo4j[0] if neo4j else "",
                            neo4j_user=neo4j[1] if neo4j else "",
                            neo4j_password=neo4j[2] if neo4j else "",
                            command=tuple(cmd) if not container_image else (),
                            env=process_env, log_file=log_f,
                            creationflags=creationflags, preexec_fn=preexec,
                            health_timeout_s=health_timeout_s)
        if container_name:
            stack.callback(_remove_container, container_name, srv.proc)
        else:
            stack.callback(lambda: _terminate(srv.proc))
        health = _wait_for_health(base_url, health_timeout_s, proc,
                                  expect_instance_id=instance_id)
        if not quiet:
            print(f"[test_server] shape={shape} backend={backend} up at {base_url} "
                  f"(startup_mode={health.get('startup_mode')})", file=sys.stderr)
        yield srv


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(Exception):
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"test server process {proc.pid} did not exit after termination and kill"
            ) from exc
    if proc.poll() is None:
        raise RuntimeError(f"test server process {proc.pid} is still running after termination")


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Launch a throwaway Menhir server in an auth shape.")
    ap.add_argument("--shape", choices=SHAPES, required=True)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--backend", choices=("none", "neo4j"), default="none",
                    help="'none' = auth-only, no Neo4j (fast). 'neo4j' = full scope "
                    "against a throwaway Neo4j (public profiles refuse external reuse).")
    ap.add_argument("--jwks-uri", default=DEAD_JWKS_URI,
                    help="OAuth shape only. Default is a dead URI (exercises the 503 outage path).")
    ap.add_argument(
        "--public-base-url",
        default=None,
        help="Externally visible origin for OAuth metadata (for example, an HTTPS tunnel URL).",
    )
    ap.add_argument(
        "--oauth-refresh",
        action="store_true",
        help="OAuth-AS shape only. Enable persistent refresh-token issuance and rotation.",
    )
    ap.add_argument(
        "--oauth-access-ttl",
        type=int,
        default=None,
        help="OAuth-AS shape only. Override access-token lifetime in seconds.",
    )
    ap.add_argument(
        "--use-current-python",
        action="store_true",
        help="Launch Menhir with this interpreter (useful with uv run --isolated --frozen).",
    )
    ap.add_argument(
        "--interactive-control",
        action="store_true",
        help="Accept 'restart' and 'stop' commands on stdin while preserving test state.",
    )
    ap.add_argument("--health-timeout", type=float, default=30.0)
    args = ap.parse_args(argv)

    if (args.oauth_refresh or args.oauth_access_ttl is not None) and args.shape != "oauth-as":
        ap.error("--oauth-refresh and --oauth-access-ttl require --shape oauth-as")
    if args.oauth_access_ttl is not None and args.oauth_access_ttl <= 0:
        ap.error("--oauth-access-ttl must be positive")
    if args.public_base_url and args.shape not in {"oauth", "oauth-as"}:
        ap.error("--public-base-url requires an OAuth shape")

    with launch(args.shape, port=args.port, host=args.host, backend=args.backend,
                jwks_uri=args.jwks_uri, public_base_url=args.public_base_url,
                oauth_refresh=args.oauth_refresh,
                oauth_access_ttl_s=args.oauth_access_ttl,
                python_executable=sys.executable if args.use_current_python else None,
                health_timeout_s=args.health_timeout, quiet=False) as srv:
        print(f"Menhir test server [{srv.shape}] running at {srv.base_url}")
        if srv.shape == "static":
            print(f"  keys: operator={TEST_KEYS['operator']} "
                  f"agent={TEST_KEYS['agent']} readonly={TEST_KEYS['readonly']}")
        elif srv.shape == "oauth-as":
            print(f"  throwaway OAuth consent key: {TEST_KEYS['operator']}")
        print("  Ctrl-C to stop (auto-teardown).")
        try:
            if args.interactive_control:
                print("  Commands: restart (preserves OAuth/Neo4j state), stop")
                while srv.proc.poll() is None:
                    try:
                        command = input().strip().lower()
                    except EOFError:
                        break
                    if command == "restart":
                        health = srv.restart()
                        print(
                            "[test_server] restarted; OAuth and Neo4j state preserved "
                            f"(startup_mode={health.get('startup_mode')})",
                            file=sys.stderr,
                        )
                    elif command in {"stop", "quit", "exit"}:
                        break
                    elif command:
                        print("[test_server] commands: restart, stop", file=sys.stderr)
            else:
                while srv.proc.poll() is None:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[test_server] stopping...", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
