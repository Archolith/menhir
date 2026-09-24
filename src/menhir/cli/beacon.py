"""``menhir beacon generate`` — local MVP Beacon generation command (issue #120).

A local operator command rather than an MCP tool: it writes a repository file
and shells out to a separately installed Beacon interpreter, so it stays out of
the agent-facing MCP surface while the MVP contract is being frozen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

beacon_app = typer.Typer(
    name="beacon",
    help="Generate a Beacon manifest from an indexed local project.",
    no_args_is_help=True,
)

#: Exit code for an expected refusal: a stale or incomplete index ("re-ingest first"), a
#: freshness or CAS refusal, an unusable --beacon-python, a Beacon build/validate failure, or
#: a filesystem error while staging. These are operator outcomes, not crashes, so they print
#: one line and exit with a stable code. Anything else still propagates as a traceback.
EXIT_REFUSED = 2

#: Beacon's stderr is embedded in BeaconCompatError messages verbatim; cap what one refusal
#: line can carry so a chatty child cannot flood the terminal.
_MAX_MESSAGE_CHARS = 4096


def _reader() -> Any:
    """Connect to the configured graph and build the structure read surface."""
    from menhir.config.settings_model import MemorySettings
    from menhir.env_file import load_menhir_env
    from menhir.infrastructure.neo4j import Neo4jRepository
    from menhir.infrastructure.structure_queries import StructureGraphWriter

    load_menhir_env()
    settings = MemorySettings.from_env()
    neo4j = Neo4jRepository(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        database=settings.neo4j_database,
    )
    return StructureGraphWriter(neo4j=neo4j), neo4j


@beacon_app.command()
def generate(
    project: Annotated[
        str, typer.Argument(help="Indexed project name in Menhir's graph.")
    ],
    repo: Annotated[
        str, typer.Option(help="Absolute repository root for the project.")
    ],
    beacon_python: Annotated[
        str,
        typer.Option(
            help="Python interpreter with a Beacon installed whose CLI supports build+validate (kept out of Menhir's env)."
        ),
    ],
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Refresh an existing generated manifest.")
    ] = False,
    expected_sha256: Annotated[
        str,
        typer.Option(
            "--expected-sha256",
            help="Digest of the existing generated manifest (refresh only).",
        ),
    ] = "",
) -> None:
    """Generate or refresh beacon.generated.yaml from Menhir-held project knowledge."""
    from menhir.services.beacon_compat import BeaconCompatError
    from menhir.services.beacon_generation import BeaconGenerationError, generate_beacon

    repo_root = Path(repo).resolve()
    reader, neo4j = _reader()
    try:
        outcome = generate_beacon(
            reader,
            project,
            repo_root,
            beacon_python=beacon_python,
            refresh=refresh,
            expected_sha256=expected_sha256 or None,
        )
    except (BeaconGenerationError, BeaconCompatError, OSError) as exc:
        message = " ".join(str(exc).split()) or exc.__class__.__name__
        if len(message) > _MAX_MESSAGE_CHARS:
            message = message[:_MAX_MESSAGE_CHARS] + " [truncated]"
        typer.echo(f"beacon generate refused: {message}", err=True)
        raise typer.Exit(EXIT_REFUSED) from None
    finally:
        neo4j.close()

    verb = "created" if outcome.created else "refreshed"
    typer.echo(f"{verb} {outcome.output_path}")
    typer.echo(f"sha256: {outcome.sha256}")
    typer.echo(f"scan_fingerprint: {outcome.scan_fingerprint}")
    typer.echo(f"git_head: {outcome.git_head or 'none'}")
