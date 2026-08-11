"""menhir CLI — Typer-based entry point."""

from __future__ import annotations

import typer

from menhir.cli.artifacts import artifacts_app
from menhir.cli.hook import hook_app
from menhir.cli.setup import setup as setup_command
from menhir.infrastructure.text_io import read_text_utf8
from menhir.infrastructure.logging_config import (
    build_logging_config,
    configure_logging,
)

app = typer.Typer(
    name="menhir",
    help="Graph-based long-term memory system for AI agents.",
    no_args_is_help=True,
)
app.add_typer(hook_app)
app.add_typer(artifacts_app)
app.command("setup")(setup_command)


# Distinct `serve` exit code meaning "another server already owns the bind port".
# The `serve-watch` watchdog treats it as a redundant start (stop cleanly) rather
# than a crash (retry with backoff forever).
EXIT_PORT_IN_USE = 3

# Distinct `serve` exit code meaning "the configured embedder does not match the
# embeddings stored in the graph" (dimension mismatch). Like EXIT_PORT_IN_USE it is
# terminal: retrying cannot fix it, so `serve-watch` stops instead of crash-looping.
EXIT_EMBEDDING_MISMATCH = 4

# Seconds a freshly-spawned `serve` child must survive before the watchdog claims
# `.server.pid` for it. A redundant server exits near-instantly on port-in-use, so
# the grace window keeps it from clobbering the real server's pid file.
STARTUP_GRACE_S = 3.0


def _pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` is currently running.

    Cross-platform and side-effect-free. On Windows, ``os.kill(pid, 0)`` is unsafe
    (it can terminate the target), so query the process handle via ctypes instead.
    """
    import os

    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid_file(path) -> "int | None":
    """Read an integer pid from ``path``; return None if missing/unparseable."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _release_pid_file(path, owner_pid: int) -> None:
    """Delete ``path`` only if it still records ``owner_pid``.

    Prevents a redundant watchdog from deleting a pid file that a different live
    process now owns (the historical .server.pid / .watchdog.pid clobber bug).
    """
    if _read_pid_file(path) == owner_pid:
        path.unlink(missing_ok=True)


@app.command("ingest-wiki")
def ingest_wiki(
    wiki_dir: str = typer.Argument(..., help="Path to sage-wiki wiki/ directory"),
    project: str | None = typer.Option(
        None, help="Project label (default: parent dir name)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List files without ingesting"
    ),
) -> None:
    """Ingest compiled sage-wiki articles into the memory graph."""
    import os
    from pathlib import Path

    configure_logging()

    wiki_path = Path(wiki_dir)
    if not wiki_path.exists():
        raise typer.BadParameter(f"Directory not found: {wiki_dir}")

    project_name = project or wiki_path.parent.name

    # Walk wiki/concepts/ and wiki/summaries/
    concepts_dir = wiki_path / "concepts"
    summaries_dir = wiki_path / "summaries"

    files_to_ingest: list[tuple[str, str]] = []  # (path, doc_type)

    for source_dir in [concepts_dir, summaries_dir]:
        if not source_dir.exists():
            continue
        for md_file in source_dir.glob("**/*.md"):
            # Read frontmatter to detect document_shape
            try:
                content = read_text_utf8(md_file)
                frontmatter = {}
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        for line in content[3:end].strip().split("\n"):
                            if ":" in line:
                                key, val = line.split(":", 1)
                                frontmatter[key.strip()] = val.strip()
                doc_shape = frontmatter.get("document_shape", "wiki")
                doc_type = (
                    "reference_article" if doc_shape == "reference" else "wiki_article"
                )
                files_to_ingest.append((str(md_file.resolve()), doc_type))
            except Exception:
                pass

    if dry_run:
        typer.echo(
            f"[DRY RUN] Would ingest {len(files_to_ingest)} files as {project_name}:"
        )
        for path, dtype in files_to_ingest[:10]:
            typer.echo(f"  [{dtype}] {path}")
        return

    # Import and ingest
    import asyncio

    from menhir.config import MemorySettings
    from menhir.core.backend_impl import BackendClient

    settings = MemorySettings.from_env()

    async def run_ingest():
        base_url = f"http://{settings.api_host}:{settings.api_port}"
        backend = BackendClient(base_url, settings=settings)
        ingested = 0
        errors = 0
        try:
            for file_path, doc_type in files_to_ingest:
                try:
                    await backend.ingest_document(
                        file_path,
                        project=project_name,
                        session_id="cli",
                        user_id="cli",
                        document_type=doc_type,
                    )
                    ingested += 1
                except Exception:
                    errors += 1
        finally:
            await backend.aclose()
        return ingested, errors

    ingested, errors = asyncio.run(run_ingest())
    typer.echo(
        f"Ingested {ingested} documents ({errors} errors) for project '{project_name}'."
    )


@app.command()
def check() -> None:
    """Run service dependency health checks."""
    configure_logging()
    from menhir.config import MemorySettings
    from menhir.core import collect_runtime_failures

    settings = MemorySettings.from_env()
    failures = collect_runtime_failures(settings, require_venv=True)
    if failures:
        for failure in failures:
            typer.echo(f"[FAIL] {failure}", err=True)
    else:
        typer.echo("[OK] Menhir runtime dependencies are ready.")
    raise SystemExit(1 if failures else 0)


@app.command()
def diagnostics(
    json_mode: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show redacted operator diagnostics snapshot (no secrets, no network)."""
    import json

    from menhir.config import MemorySettings
    from menhir.operator_diagnostics import build_operator_diagnostics

    settings = MemorySettings.from_env()
    snapshot = build_operator_diagnostics(settings)

    if json_mode:
        print(json.dumps(snapshot, indent=2))
        return

    status_symbol = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    print(f"Menhir operator diagnostics: {status_symbol.get(snapshot['status'], snapshot['status'])}")
    bind = snapshot["bind"]
    print(f"  bind: {bind['host']}:{bind['port']}  loopback={str(bind['loopback']).lower()}")
    auth = snapshot["auth"]
    mode_label = "bearer keys" if auth["mode"] == "bearer_keys_configured" else "no auth (open)"
    print(f"  auth: {mode_label}")
    if auth["agent_key_present"]:
        print("    - agent key: configured")
    if auth["readonly_key_present"]:
        print("    - read-only key: configured")
    if auth["admin_key_present"]:
        print("    - admin/operator key: configured")
    if auth["insecure_remote_no_auth_override"]:
        print("    - WARNING: MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH is set (unsafe outside lab)")

    safety = snapshot["safety"]
    warnings = safety.get("warnings", [])
    if warnings:
        print("  warnings:")
        for w in warnings:
            print(f"    - {w}")
    else:
        print("  warnings: none")

    oauth = snapshot.get("oauth_resource_server", {})
    if oauth:
        oauth_status = oauth.get("status", "unknown")
        print(f"  OAuth resource server: {'enabled' if oauth.get('enabled') else 'disabled'} ({oauth_status})")
        if oauth.get("enabled"):
            print(f"    metadata_url: {oauth.get('metadata_url', 'not configured')}")
            print(f"    resource: {oauth.get('resource', 'not configured')}")
            print(f"    authorization_servers: {oauth.get('authorization_servers_count', 0)} configured")
            oauth_checks = oauth.get("checks", [])
            if oauth_checks:
                pass_count = sum(1 for c in oauth_checks if c["status"] == "pass")
                warn_count = sum(1 for c in oauth_checks if c["status"] == "warn")
                fail_count = sum(1 for c in oauth_checks if c["status"] == "fail")
                print(f"    checks: {pass_count} pass, {warn_count} warn, {fail_count} fail")

    mcp = snapshot.get("mcp_backend_client", {})
    if mcp:
        print(f"  MCP backend client: {'enabled' if mcp.get('enabled') else 'disabled'}")
        print(f"    backend_url: {mcp.get('backend_url', 'not configured')}")
        print(f"    auth: {'bearer configured' if mcp.get('auth_header_will_be_sent') else 'no auth'}")
        mcp_warnings = mcp.get("warnings", [])
        if mcp_warnings:
            print("    warnings:")
            for w in mcp_warnings:
                print(f"      - {w}")
        else:
            print("    warnings: none")


@app.command()
def console(
    host: str = typer.Option(None, help="Server host (default: from settings)"),
    port: int = typer.Option(None, help="Server port (default: from settings)"),
    interval: float = typer.Option(1.0, help="Poll interval in seconds"),
    log_file: str = typer.Option(None, help="Path to server.log (default: <log dir>/server.log)"),
    redact: bool = typer.Option(
        None,
        "--redact/--no-redact",
        help="Start with memory-content privacy redaction on/off (default: MENHIR_PRIVACY_REDACT)",
    ),
) -> None:
    """Live operator dashboard: server/neo4j/queue status + log tail (press 'p' privacy, 'q' quit)."""
    import asyncio
    import os
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(os.getenv("ENV_FILE") or None)

    from menhir.cli.console import run_console
    from menhir.config import MemorySettings

    settings = MemorySettings.from_env()
    final_host = host or settings.api_host
    final_port = port or settings.api_port
    if log_file:
        final_log = Path(log_file)
    else:
        log_dir = os.getenv("MENHIR_LOG_DIR") or os.path.join(os.getcwd(), "logs")
        final_log = Path(log_dir) / "server.log"
    final_redact = settings.privacy_redact if redact is None else redact
    # Read-only GETs against /api/*; send the least-privileged key available so the
    # dashboard works when the server runs in STATIC/authed mode. Loopback NONE mode
    # ignores the header.
    api_key = (
        settings.readonly_key
        or settings.operator_key
        or settings.agent_key
        or settings.api_key
        or None
    )

    try:
        asyncio.run(
            run_console(
                host=final_host,
                port=final_port,
                log_file=final_log,
                interval=interval,
                redact=final_redact,
                api_key=api_key,
            )
        )
    except KeyboardInterrupt:
        pass


@app.command()
def serve(
    host: str = typer.Option(
        None, help="Bind address (default: from menhir_API_HOST or 127.0.0.1)"
    ),
    port: int = typer.Option(
        None, help="Bind port (default: from menhir_API_PORT or 8100)"
    ),
) -> None:
    """Start the menhir HTTP + MCP server."""
    import os
    import sys

    import uvicorn
    from dotenv import load_dotenv

    load_dotenv(os.getenv("ENV_FILE") or None)

    from menhir.api.server import create_app
    from menhir.config import MemorySettings

    settings = MemorySettings.from_env()
    configure_logging()
    final_host = host or settings.api_host
    final_port = port or settings.api_port

    # SSOT bind-safety: resolve the auth mode once (OAuth / client-token / static
    # / none) and enforce it. Previously this call passed only the static-key
    # signal, so an OAuth-only or client-token-only remote bind started via the
    # CLI would have been wrongly refused.
    from menhir.config.settings import assert_bind_safe

    assert_bind_safe(settings, host=final_host)

    import logging

    logger = logging.getLogger("menhir.cli")

    # Preflight: if the bind port is already owned by another server, exit with a
    # distinct code so the serve-watch watchdog treats this as a redundant start
    # (stop) rather than a crash (retry forever). Best-effort — a plain exclusive
    # bind probe; if it passes, uvicorn binds for real just below.
    import errno
    import socket

    _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _probe.bind((final_host, final_port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", 0) == 10048:
            logger.warning(
                "menhir server: %s:%d already in use; another server owns it, "
                "exiting (code %d)",
                final_host,
                final_port,
                EXIT_PORT_IN_USE,
            )
            raise SystemExit(EXIT_PORT_IN_USE) from None
        raise
    finally:
        _probe.close()

    # Preflight: refuse to start if the configured embedder does not match the
    # embeddings already stored in the graph. Serving through a dimension mismatch
    # produces vector.similarity errors and can silently drop new memories. Exit
    # with a terminal code so serve-watch stops rather than crash-looping. This is
    # best-effort: if Neo4j is unreachable or the graph is empty, we do not block
    # here (the normal runtime preflight covers Neo4j connectivity).
    try:
        from menhir.infrastructure.embedding_dimensions import (
            evaluate_embedding_compatibility,
        )
        from menhir.infrastructure.neo4j import Neo4jRepository

        _repo = Neo4jRepository(
            uri=settings.neo4j_uri,
            database=settings.neo4j_database,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
        )
        try:
            _compat = evaluate_embedding_compatibility(_repo, settings)
        finally:
            _repo.close()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — best-effort; never false-block on infra errors
        logger.debug("Embedding compatibility preflight skipped: %s", exc)
    else:
        if _compat.blocking:
            banner = _compat.banner()
            logger.error("Embedding dimension mismatch — refusing to start.%s", banner)
            # Also write to stderr directly so the banner is unmissable even if the
            # logging stream is redirected or filtered.
            print(banner, file=sys.stderr, flush=True)
            raise SystemExit(EXIT_EMBEDDING_MISMATCH)

    logger.info("Launching menhir server on %s:%d", final_host, final_port)

    uvicorn.run(
        create_app(settings=settings),
        host=final_host,
        port=final_port,
        workers=1,
        log_level="info",
        log_config=build_logging_config(),
    )


def _ensure_neo4j(container: str, logger: "logging.Logger") -> None:  # type: ignore[name-defined]
    """Try to ensure the named Docker container is running. Never raises."""
    import subprocess
    import time

    def _running() -> bool:
        r = subprocess.run(
            ["docker", "inspect", f"--format={{{{.State.Status}}}}", container],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0 and r.stdout.strip() == "running"

    if _running():
        return

    logger.info("serve-watch: neo4j not running, attempting start container=%s", container)
    subprocess.run(["docker", "start", container], capture_output=True)

    deadline = time.time() + 30.0
    while time.time() < deadline:
        if _running():
            logger.info("serve-watch: neo4j ready container=%s", container)
            return
        time.sleep(2)

    logger.warning(
        "serve-watch: neo4j did not become ready within 30s container=%s", container
    )


@app.command("serve-watch")
def serve_watch(
    host: str = typer.Option(
        None, help="Bind address (default: from settings)"
    ),
    port: int = typer.Option(
        None, help="Bind port (default: from settings)"
    ),
    max_backoff: float = typer.Option(
        60.0, "--max-backoff", help="Maximum restart delay in seconds"
    ),
    neo4j_container: str = typer.Option(
        "yawn-neo4j",
        "--neo4j-container",
        help="Docker container name for Neo4j readiness check",
    ),
) -> None:
    """Start the server with automatic restart on crash (watchdog mode)."""
    import logging
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(os.getenv("ENV_FILE") or None)

    from menhir.config import MemorySettings

    settings = MemorySettings.from_env()
    configure_logging()

    logger = logging.getLogger("menhir.cli.watch")

    final_host = host or settings.api_host
    final_port = port or settings.api_port

    # __file__ = src/menhir/cli/__init__.py — 4 parents = project root
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    pid_file = project_root / ".server.pid"
    watchdog_pid_file = project_root / ".watchdog.pid"

    my_pid = os.getpid()

    # Singleton guard: if another live watchdog is already registered, this start
    # is redundant. Exit cleanly instead of racing for the port. (A same-instant
    # race that slips past this check is still handled below: the losing child
    # exits with EXIT_PORT_IN_USE and this watchdog stops without retrying.)
    existing = _read_pid_file(watchdog_pid_file)
    if existing is not None and existing != my_pid and _pid_alive(existing):
        logger.info(
            "serve-watch: another watchdog already running pid=%d, exiting", existing
        )
        raise SystemExit(0)

    watchdog_pid_file.write_text(str(my_pid))
    logger.info("serve-watch: watchdog started pid=%d", my_pid)

    _child: subprocess.Popen | None = None  # type: ignore[type-arg]
    claimed_server_pid: int | None = None

    def _stop(signum: object = None, frame: object = None) -> None:
        nonlocal _child
        if _child is not None and _child.poll() is None:
            logger.info("serve-watch: forwarding stop to child pid=%d", _child.pid)
            try:
                _child.terminate()
                try:
                    _child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _child.kill()
            except OSError:
                pass
        if claimed_server_pid is not None:
            _release_pid_file(pid_file, claimed_server_pid)
        _release_pid_file(watchdog_pid_file, my_pid)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    backoff = 2.0
    attempt = 0

    # Neo4j is only a local Docker container when the configured URI is loopback. In this
    # deployment it runs on the remote OVM host, so trying to `docker start` a local
    # container is pointless and would burn a 30s readiness wait each restart. Only manage
    # a local container when the graph is actually local.
    from menhir.config.settings import is_loopback_host
    from urllib.parse import urlparse

    _neo4j_host = urlparse(settings.neo4j_uri).hostname or ""
    _neo4j_is_local = is_loopback_host(_neo4j_host)

    try:
        while True:
            attempt += 1
            if _neo4j_is_local:
                _ensure_neo4j(neo4j_container, logger)

            cmd = [sys.executable, "-m", "menhir.cli", "serve"]
            if host:
                cmd += ["--host", host]
            if port:
                cmd += ["--port", str(port)]

            logger.info("serve-watch: starting server attempt=%d", attempt)
            _child = subprocess.Popen(cmd, env=os.environ.copy())

            # Startup grace: only claim .server.pid once the child survives startup.
            # A redundant server exits near-instantly with EXIT_PORT_IN_USE, so it
            # never overwrites the real server's pid file.
            try:
                early_code = _child.wait(timeout=STARTUP_GRACE_S)
            except subprocess.TimeoutExpired:
                early_code = None

            if early_code is None:
                pid_file.write_text(str(_child.pid))
                claimed_server_pid = _child.pid
                _child.wait()
                exit_code = _child.returncode
                _release_pid_file(pid_file, claimed_server_pid)
                claimed_server_pid = None
            else:
                exit_code = early_code
            _child = None

            if exit_code == 0:
                logger.info(
                    "serve-watch: server exited cleanly (code 0), watchdog stopping"
                )
                break

            if exit_code == EXIT_PORT_IN_USE:
                logger.warning(
                    "serve-watch: %s:%d already owned by another server; this "
                    "watchdog is redundant, stopping",
                    final_host,
                    final_port,
                )
                break

            if exit_code == EXIT_EMBEDDING_MISMATCH:
                logger.error(
                    "serve-watch: embedding dimension mismatch — a restart cannot "
                    "fix this. Stopping. Repair the embeddings (see the banner above) "
                    "then start the server again."
                )
                break

            logger.warning(
                "serve-watch: crash detected exit_code=%d, restarting in %.0fs (attempt=%d)",
                exit_code,
                backoff,
                attempt,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
    finally:
        if claimed_server_pid is not None:
            _release_pid_file(pid_file, claimed_server_pid)
        _release_pid_file(watchdog_pid_file, my_pid)
