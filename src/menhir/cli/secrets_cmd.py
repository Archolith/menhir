"""`menhir secrets scan` -- count recognised credentials in a namespace. Read-only, no values."""

from __future__ import annotations

import json
from typing import Annotated

import typer

secrets_app = typer.Typer(
    name="secrets",
    help="Credential handling (#577).",
    no_args_is_help=True,
)

EXIT_FINDINGS = 1


@secrets_app.command("scan")
def scan(
    namespace: Annotated[str, typer.Option("--namespace", help="Namespace to scan.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Count stored memories holding a recognised credential, by type and kind.

    Read-only. Prints counts only, never a value. Exits 1 when anything is found.
    """
    from menhir.config.settings_model import MemorySettings
    from menhir.env_file import load_menhir_env
    from menhir.infrastructure.neo4j import Neo4jRepository
    from menhir.services.secret_scan import scan_namespace

    load_menhir_env()
    settings = MemorySettings.from_env()
    neo4j = Neo4jRepository(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        database=settings.neo4j_database,
    )
    report = scan_namespace(neo4j.execute, namespace)
    payload = report.to_dict()
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(
            f"namespace={payload['namespace']} scanned={payload['nodes_scanned']} "
            f"with_hits={payload['nodes_with_hits']}"
        )
        for match in payload["matches"]:
            typer.echo(f"  {match['memory_type']:<14} {match['kind']:<28} {match['nodes']}")
    if report.nodes_with_hits:
        raise typer.Exit(EXIT_FINDINGS)
