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

from menhir.cli.setup import SetupError, apply_setup, find_home

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
        # Outside a checkout there is no repository compose file, so write the bundled one
        # into the same directory `.env` lives in. Its credentials are COMPOSE_NEO4J_KEYS,
        # which setup has already written to `.env`.
        from menhir.cli.setup import _GENERATED_COMPOSE

        try:
            compose_file.write_text(_GENERATED_COMPOSE, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise UpError(f"--compose-neo4j needs {compose_file}, which could not be written: {exc}") from exc
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
    probe: Callable[[str, str, str], str],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Poll Neo4j until it answers, refuses the credentials, or ``timeout_s`` elapses.

    Returns the last probe outcome (``ok`` / ``unauthorized`` / ``unreachable``). A refused
    password ends the wait at once: no amount of waiting makes it right. The first probe is
    immediate.
    """

    deadline = clock() + max(0.0, timeout_s)
    while True:
        status = probe(uri, user, password)
        if status != "unreachable":
            return status
        if clock() >= deadline:
            return status
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

    credential = str(getattr(capabilities, "cloud_credential", "n/a"))
    cloud_note = {
        "verified": " (openai: key verified via GET /v1/models)",
        "rejected": " (openai: key REJECTED)",
        "unverified": " (openai: key present; could not reach api.openai.com to verify)",
    }.get(credential, "")
    if llm.kind is ProviderKind.OPENAI and credential == "rejected":
        llm_hint = "OPENAI_API_KEY was rejected with 401/403 -- set a valid key"
    elif llm.kind is ProviderKind.OPENAI:
        llm_hint = "OPENAI_API_KEY, OPENAI_CHAT_MODEL"
    elif llm.kind is ProviderKind.LOCAL:
        llm_hint = "LOCAL_LLM_BASE_URL must answer GET /v1/models and list LOCAL_LLM_CHAT_MODEL"
    else:
        llm_hint = "GRAPHITI_LLM_PROVIDER must be local or openai"
    if embed.kind is ProviderKind.OPENAI and credential == "rejected":
        embed_hint = "OPENAI_API_KEY was rejected with 401/403 -- set a valid key"
    elif embed.kind is ProviderKind.OPENAI:
        embed_hint = "OPENAI_API_KEY, OPENAI_EMBED_MODEL"
    elif embed.kind is ProviderKind.LOCAL:
        embed_hint = "the embedding server must be running and list LOCAL_LLM_EMBED_MODEL at GET /v1/models (LOCAL_LLM_EMBED_BASE_URL if separate)"
    else:
        embed_hint = "GRAPHITI_EMBED_PROVIDER must be local or openai"

    lines = [
        TierLine("python: graphiti_core importable", bool(getattr(capabilities, "graphiti_dependency_ready", False)),
                 "pip install . (or -e .) into the interpreter running menhir"),
        TierLine("python: interpreter guard", bool(getattr(capabilities, "venv_ready", True)),
                 "run from the checkout .venv, or set MENHIR_ALLOW_SYSTEM_PYTHON=1"),
        TierLine(
            "neo4j: reachable",
            bool(getattr(capabilities, "neo4j_ready", False)),
            "NEO4J_USER / NEO4J_PASSWORD were rejected by the server -- fix the credentials"
            if getattr(capabilities, "neo4j_status", "") == "unauthorized"
            else "NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD (or `menhir up --compose-neo4j`)",
        ),
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
        "full": "startup mode: full -- reads, writes, and enrichment.",
        "degraded_reads_only": "startup mode: degraded_reads_only -- recall works; new memories queue until the LLM is reachable.",
        "degraded_queue_only": "startup mode: degraded_queue_only -- writes queue; recall needs the embedder.",
        "unavailable": "startup mode: unavailable -- Neo4j is required to start.",
    }.get(startup_mode, f"startup mode: {startup_mode}")
    return f"{body}\n{tail}"


def report_readiness(checkout: Path) -> tuple[object, bool]:
    """Load the checkout's .env, run preflight, print the tier report.

    Returns ``(capabilities, blocking)`` where ``blocking`` means the server cannot start
    (Neo4j unreachable or the interpreter guard failed). Shared by ``menhir up`` and the end of
    ``menhir setup`` so a newcomer sees the same picture -- and the same next command -- from
    either entry point.
    """

    import os

    from dotenv import load_dotenv

    os.environ.setdefault("ENV_FILE", str(checkout / ".env"))
    load_dotenv(os.environ["ENV_FILE"], override=False)

    from menhir.config import MemorySettings
    from menhir.core import collect_runtime_capabilities

    settings = MemorySettings.from_env()
    capabilities = collect_runtime_capabilities(settings)
    typer.echo(render_report(tier_report(capabilities, settings), capabilities.startup_mode))
    blocking = not capabilities.neo4j_ready or not capabilities.venv_ready
    return capabilities, blocking


def next_step_hint(blocking: bool) -> str:
    if blocking:
        return "Next: fix the MISS lines above, then run 'menhir up' to start the server."
    return "Next: run 'menhir up' to start the server (add --compose-neo4j for the bundled Neo4j)."


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
        checkout = find_home(repo or Path.cwd(), explicit=repo is not None)

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
        from menhir.core.runtime_preflight import probe_neo4j

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
            status = wait_for_neo4j(
                uri=settings.neo4j_uri,
                user=settings.neo4j_user,
                password=settings.neo4j_password,
                timeout_s=wait_timeout if not check else 0.0,
                probe=probe_neo4j,
            )
            if status == "unauthorized":
                typer.echo("[neo4j] reached, but it rejected NEO4J_USER/NEO4J_PASSWORD", err=True)
            elif status != "ok":
                typer.echo("[neo4j] not answering yet", err=True)

        # 4. preflight + tier report
        capabilities = collect_runtime_capabilities(settings)
        typer.echo(render_report(tier_report(capabilities, settings), capabilities.startup_mode))
        blocking = not capabilities.neo4j_ready or not capabilities.venv_ready
        if check:
            typer.echo(next_step_hint(blocking))
            raise typer.Exit(1 if blocking else 0)
        if blocking:
            typer.echo("menhir up: fix the MISS lines above and re-run.", err=True)
            raise typer.Exit(1)

        # 5. serve (foreground)
        from menhir.cli import serve as serve_command

        typer.echo(
            "[serve] starting. To verify end to end: write one SMOKE TEST memory into "
            "namespace 'smoke' with POST /api/memory?wait=true, recall it, then "
            "DELETE /api/namespace/smoke (README: 'Smoke test, then clean up')."
        )
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
    "next_step_hint",
    "render_report",
    "report_readiness",
    "tier_report",
    "up",
    "wait_for_neo4j",
]
