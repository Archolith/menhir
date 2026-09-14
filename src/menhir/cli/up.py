"""`menhir up`: one command from a fresh checkout to a running server.

Composes steps that already exist -- `setup` (env), the root compose file (Neo4j), preflight
(`check`), and `serve` -- and adds only what sat between them: a bounded wait for Bolt after
`docker compose up`, and a tier report that names the `.env` key behind each missing
capability instead of a stack trace.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import typer

from menhir.cli.setup import SetupError, apply_setup, find_checkout

DEFAULT_WAIT_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 2.0
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class UpError(RuntimeError):
    """Raised when `menhir up` cannot continue safely."""


def neo4j_is_loopback(uri: str) -> bool:
    host = (urlparse(uri).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def compose_neo4j_up(repo: Path, *, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> str:
    """Start the root compose file's Neo4j service. Returns a one-line description."""

    compose_file = repo / "docker-compose.yml"
    if not compose_file.is_file():
        raise UpError(f"--compose-neo4j needs {compose_file}, which is missing.")
    try:
        result = run(
            ["docker", "compose", "-f", str(compose_file), "up", "-d", "neo4j"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UpError("docker is not on PATH; install Docker or point NEO4J_URI at an existing Neo4j.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise UpError("docker compose up failed: " + (detail[-1] if detail else f"exit {result.returncode}"))
    return f"docker compose up -d neo4j ({compose_file})"


def wait_for_neo4j(
    *,
    uri: str,
    user: str,
    password: str,
    timeout_s: float,
    check: Callable[[str, str, str], bool],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll Neo4j until it answers or ``timeout_s`` elapses. The first probe is immediate."""

    deadline = clock() + max(0.0, timeout_s)
    while True:
        if check(uri, user, password):
            return True
        if clock() >= deadline:
            return False
        sleep(_POLL_INTERVAL_S)


@dataclass(frozen=True)
class TierLine:
    capability: str
    ready: bool
    hint: str  # the .env keys (or action) that turn this on; empty when ready

    def render(self) -> str:
        mark = "ok  " if self.ready else "MISS"
        return f"[{mark}] {self.capability}" + (f"  <- {self.hint}" if self.hint and not self.ready else "")


def tier_report(capabilities: object, settings: object) -> list[TierLine]:
    """Map a RuntimeCapabilities snapshot to lines that say what to set."""

    from menhir.infrastructure.providers import ProviderConfig, ProviderKind

    llm = ProviderConfig.for_graphiti_llm(settings)  # type: ignore[arg-type]
    embed = ProviderConfig.for_graphiti_embedder(settings)  # type: ignore[arg-type]

    cloud_note = " (openai: key present; verified on first call, not at startup)"
    if llm.kind is ProviderKind.OPENAI:
        llm_hint = "OPENAI_API_KEY, OPENAI_CHAT_MODEL"
    elif llm.kind is ProviderKind.LOCAL:
        llm_hint = "LOCAL_LLM_BASE_URL must answer GET /v1/models and list LOCAL_LLM_CHAT_MODEL"
    else:
        llm_hint = "GRAPHITI_LLM_PROVIDER must be local or openai"
    if embed.kind is ProviderKind.OPENAI:
        embed_hint = "OPENAI_API_KEY, OPENAI_EMBED_MODEL"
    elif embed.kind is ProviderKind.LOCAL:
        embed_hint = "LOCAL_LLM_EMBED_MODEL (and LOCAL_LLM_EMBED_BASE_URL if on another server)"
    else:
        embed_hint = "GRAPHITI_EMBED_PROVIDER must be local or openai"

    lines = [
        TierLine("python: graphiti_core importable", bool(getattr(capabilities, "graphiti_dependency_ready", False)),
                 "pip install . (or -e .) into the interpreter running menhir"),
        TierLine("python: interpreter guard", bool(getattr(capabilities, "venv_ready", True)),
                 "run from the checkout .venv, or set MENHIR_ALLOW_SYSTEM_PYTHON=1"),
        TierLine("neo4j: reachable", bool(getattr(capabilities, "neo4j_ready", False)),
                 "NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD (or `menhir up --compose-neo4j`)"),
        TierLine(
            "llm: graphiti extraction" + (cloud_note if llm.kind is ProviderKind.OPENAI else ""),
            bool(getattr(capabilities, "graphiti_llm_ready", False)),
            llm_hint,
        ),
        TierLine(
            "llm: embeddings" + (cloud_note if embed.kind is ProviderKind.OPENAI else ""),
            bool(getattr(capabilities, "embedder_ready", False)),
            embed_hint,
        ),
    ]
    return lines


def render_report(lines: list[TierLine], startup_mode: str) -> str:
    body = "\n".join(line.render() for line in lines)
    tail = {
        "full": "startup mode: full -- reads, writes, and enrichment. A cloud key is only proven by the first "
        "enrichment: watch /api/health services.llm_auth or the WARNING on your next add_memory.",
        "degraded_reads_only": "startup mode: degraded_reads_only -- recall works; new memories queue until the LLM is reachable.",
        "degraded_queue_only": "startup mode: degraded_queue_only -- writes queue; recall needs the embedder.",
        "unavailable": "startup mode: unavailable -- Neo4j is required to start.",
    }.get(startup_mode, f"startup mode: {startup_mode}")
    return f"{body}\n{tail}"


def up(
    repo: Annotated[Path | None, typer.Option(help="Menhir source checkout (default: search from cwd).")] = None,
    check: Annotated[bool, typer.Option("--check", help="Report what would start and stop; launch nothing.")] = False,
    provider: Annotated[str | None, typer.Option(help="Write a consistent LLM provider block into .env: local or openai.")] = None,
    compose_neo4j: Annotated[bool, typer.Option("--compose-neo4j", help="Start the root docker-compose.yml Neo4j and point .env at it.")] = False,
    wait_timeout: Annotated[float, typer.Option("--wait-timeout", help="Seconds to wait for Neo4j to answer.")] = DEFAULT_WAIT_TIMEOUT_S,
    host: Annotated[str | None, typer.Option(help="Bind address passed to serve.")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port passed to serve.")] = None,
) -> None:
    """Bring Menhir up from a checkout: env, Neo4j, preflight report, serve."""

    import os

    from dotenv import load_dotenv

    try:
        checkout = find_checkout(repo or Path.cwd())

        # 1. env (idempotent; never overwrites a filled-in secret)
        changes = apply_setup(
            checkout,
            create_env=True,
            configure_git_hooks=False,
            provider=provider,
            compose_neo4j=compose_neo4j,
        )
        for change in changes:
            typer.echo(f"[env] {change}")
        # Load the checkout's .env BEFORE importing menhir.config / menhir.core: those reach
        # graphiti_core, whose import calls load_dotenv() on the current directory. Loading
        # first (override=False) keeps the conventional precedence -- shell environment, then
        # this checkout's .env -- and a bystander .env in cwd cannot win.
        os.environ.setdefault("ENV_FILE", str(checkout / ".env"))
        load_dotenv(os.environ["ENV_FILE"], override=False)

        from menhir.config import MemorySettings
        from menhir.core import collect_runtime_capabilities
        from menhir.core.runtime_preflight import check_neo4j_connectivity

        settings = MemorySettings.from_env()

        # 2. Neo4j via the root compose file, only when asked and only for a loopback URI
        if compose_neo4j:
            if not neo4j_is_loopback(settings.neo4j_uri):
                raise UpError(
                    f"--compose-neo4j only applies to a loopback NEO4J_URI; .env has {settings.neo4j_uri}."
                )
            if check:
                typer.echo("[neo4j] would run: docker compose up -d neo4j (skipped by --check)")
            else:
                typer.echo(f"[neo4j] {compose_neo4j_up(checkout)}")

        # 3. wait for Bolt (skipped under --check when we did not start anything)
        if not check or not compose_neo4j:
            if check:
                typer.echo(f"[neo4j] probing {settings.neo4j_uri} ...")
            else:
                typer.echo(f"[neo4j] waiting up to {wait_timeout:.0f}s for {settings.neo4j_uri} ...")
            if not wait_for_neo4j(
                uri=settings.neo4j_uri,
                user=settings.neo4j_user,
                password=settings.neo4j_password,
                timeout_s=wait_timeout if not check else 0.0,
                check=check_neo4j_connectivity,
            ):
                typer.echo("[neo4j] not answering yet", err=True)

        # 4. preflight + tier report
        capabilities = collect_runtime_capabilities(settings)
        typer.echo(render_report(tier_report(capabilities, settings), capabilities.startup_mode))
        blocking = not capabilities.neo4j_ready or not capabilities.venv_ready
        if check:
            raise typer.Exit(1 if blocking else 0)
        if blocking:
            typer.echo("menhir up: fix the MISS lines above and re-run.", err=True)
            raise typer.Exit(1)

        # 5. serve (foreground)
        from menhir.cli import serve as serve_command

        typer.echo("[serve] starting")
        serve_command(host=host, port=port)
    except typer.Exit:
        raise
    except (SetupError, UpError, OSError, ValueError) as exc:
        typer.echo(f"menhir up failed: {exc}", err=True)
        raise typer.Exit(1) from exc


__all__ = [
    "DEFAULT_WAIT_TIMEOUT_S",
    "TierLine",
    "UpError",
    "compose_neo4j_up",
    "neo4j_is_loopback",
    "render_report",
    "tier_report",
    "up",
    "wait_for_neo4j",
]
