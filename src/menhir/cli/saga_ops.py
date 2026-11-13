"""`menhir saga` -- operator commands for stranded saga journal rows (#427 fix B). Thin Typer layer."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

saga_app = typer.Typer(
    name="saga",
    help="Inspect and resolve stranded saga journal rows.",
    no_args_is_help=True,
)

_BY = Annotated[str, typer.Option("--by", help="Operator name, recorded on the row.")]


def _wiring() -> tuple[Any, dict[str, Any], Any]:
    from menhir.infrastructure.logging_config import configure_logging
    from menhir.config import MemorySettings
    from menhir.core import build_memory_services
    from menhir.services.saga_preflight import build_default_dispatcher

    configure_logging()
    built = build_memory_services(MemorySettings.from_env())
    dispatcher = build_default_dispatcher(built.graph_adapter)
    from menhir.services.saga_reconcile_gate import ReconciliationGate

    return dispatcher.journal, dict(dispatcher.handlers), ReconciliationGate()


def _journal() -> Any:
    from menhir.infrastructure.graph_operations import GraphOperationsJournal

    return GraphOperationsJournal()


def _emit(result: dict[str, Any]) -> None:
    typer.echo(json.dumps(result, indent=2, sort_keys=True, default=str))
    raise typer.Exit(0 if result.get("ok") else 1)


@saga_app.command("list")
def list_cmd(
    state: Annotated[str | None, typer.Option("--state", help="PREPARED or NEEDS_REVIEW.")] = None,
) -> None:
    """List PREPARED and NEEDS_REVIEW rows with ownership classification. Read-only."""
    from menhir.services import saga_operator

    _emit(saga_operator.list_operations(_journal(), state=state))


@saga_app.command("show")
def show_cmd(op_id: str) -> None:
    """Show a row, its claim history, ownership and the handler's verdict. Read-only."""
    from menhir.services import saga_operator

    journal, handlers, _gate = _wiring()
    _emit(saga_operator.show_operation(journal, handlers, op_id))


@saga_app.command("attest")
def attest_cmd(
    op_id: str,
    owner_token: Annotated[str, typer.Option("--owner-token", help="The owner token you inspected.")],
    by: _BY,
) -> None:
    """Record that the row's writer process has stopped.

    Attestation states the writer process has stopped, so it is true only when that process is
    gone (container restarted since the row's created_at, or host rebooted). For a row written by
    the RUNNING server, restart the server first, then attest the old token, then resolve.
    """
    from menhir.services import saga_operator

    _emit(saga_operator.attest(_journal(), op_id, owner_token=owner_token, by=by))


@saga_app.command("resolve")
def resolve_cmd(
    op_id: str,
    by: _BY,
    apply: Annotated[bool, typer.Option("--apply", help="Write. Default is a dry run.")] = False,
) -> None:
    """Close an abandoned PREPARED row from the graph's observed state (dry run by default)."""
    from menhir.services import saga_operator

    journal, handlers, gate = _wiring()
    _emit(saga_operator.resolve(journal, handlers, gate, op_id, by=by, apply=apply))


@saga_app.command("clear-review")
def clear_review_cmd(
    op_id: str,
    to: Annotated[str, typer.Option("--to", help="FAILED, COMMITTED or REVERSED.")],
    by: _BY,
) -> None:
    """Move a NEEDS_REVIEW row to a terminal state, stamped with the operator's name."""
    from menhir.services import saga_operator

    _emit(saga_operator.clear_review(_journal(), op_id, to_state=to, by=by))
