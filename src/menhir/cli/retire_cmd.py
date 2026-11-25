"""`menhir retire judge` -- report-only category judge for retire candidates. Writes nothing to the graph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

retire_app = typer.Typer(
    name="retire",
    help="Retire lifecycle tools (report-only).",
    no_args_is_help=True,
)


@retire_app.command("judge")
def judge(
    namespace: Annotated[str, typer.Option("--namespace", help="Namespace to review.")],
    limit: Annotated[int, typer.Option("--limit", help="Most candidates to judge.")] = 20,
    call_budget: Annotated[int, typer.Option("--call-budget", help="Most LLM calls.")] = 30,
    k: Annotated[int, typer.Option("--k", help="Samples per memory.")] = 3,
    idle_days: Annotated[int, typer.Option("--idle-days", help="Idle threshold in days.")] = 90,
    max_edge_count: Annotated[
        int, typer.Option("--max-edge-count", help="Idle memories must have fewer edges.")
    ] = 3,
    model: Annotated[str | None, typer.Option("--model", help="Chat model override.")] = None,
    nominate_only: Annotated[
        bool, typer.Option("--nominate-only", help="List candidates; make no LLM call.")
    ] = False,
    out: Annotated[Path | None, typer.Option("--out", help="Write the JSON report here.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Judge why memories could be retired. Read-only: no graph write, no retire, no recall touch."""
    from menhir.config.settings_model import MemorySettings
    from menhir.env_file import load_menhir_env
    from menhir.infrastructure.memory_queries import MemoryQueryRepository
    from menhir.infrastructure.neo4j import Neo4jRepository
    from menhir.infrastructure.sync_llm import make_sync_chat
    from menhir.services.retire_judge import build_report, gather_evidence, judge as run_judge, nominate

    load_menhir_env()
    settings = MemorySettings.from_env()
    neo4j = Neo4jRepository(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        database=settings.neo4j_database,
    )
    candidates = nominate(
        neo4j.execute, namespace, idle_days=idle_days, max_edge_count=max_edge_count, limit=limit,
    )
    judged = None
    if not nominate_only:
        gather_evidence(candidates, MemoryQueryRepository(neo4j))
        chat = make_sync_chat(settings, temperature=0.7, model=model)
        judged = run_judge(candidates, chat, k=k, call_budget=call_budget)
    report = build_report(namespace, candidates, judged)
    payload = json.dumps(report, indent=2)
    if out is not None:
        out.write_text(payload, encoding="utf-8")
    if as_json or out is None:
        typer.echo(payload)
    else:
        typer.echo(
            f"namespace={report['namespace']} candidates={len(report['items'])} "
            f"llm_calls={report['llm_calls']} totals={report['totals']} report={out}"
        )
