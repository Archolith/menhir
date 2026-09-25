"""menhir CLI — Typer-based entry point."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from menhir.cli.artifacts import artifacts_app
from menhir.cli.beacon import beacon_app
from menhir.cli.hook import hook_app
from menhir.cli.serve_ops import (
    EXIT_EMBEDDING_MISMATCH,
    EXIT_PORT_IN_USE,
    STARTUP_GRACE_S,
    serve,
    serve_watch,
)
from menhir.cli.setup import setup as setup_command
from menhir.cli.sync import sync as sync_command
from menhir.cli.up import up as up_command
from menhir.cli.wiki_ingest import ingest_wiki
from menhir.infrastructure.logging_config import configure_logging

if TYPE_CHECKING:
    import logging

app = typer.Typer(
    name="menhir",
    help="Provenance, governed context, and code-impact analysis for coding agents.",
    invoke_without_command=True,
)


@app.callback()
def _root(ctx: typer.Context) -> None:
    """With no subcommand, show what is configured and what to run next (`menhir up --check`)."""

    if ctx.invoked_subcommand is None:
        # The first thing a newcomer types after `pip install` is the bare command. Help text
        # lists 20 subcommands; the tier report says which one matters right now. Outside a
        # source checkout there is nothing to report on, so fall back to the help text.
        up_command(check=True)


app.add_typer(hook_app)
app.add_typer(artifacts_app)
app.add_typer(beacon_app)
app.command("setup")(setup_command)
app.command("sync")(sync_command)
app.command("up")(up_command)


app.command("ingest-wiki")(ingest_wiki)


@app.command("erasure-backfill")
def erasure_backfill(
    apply: bool = typer.Option(False, "--apply", help="Write changes (default is a dry run)"),
    redact_unaddressable: bool = typer.Option(
        False,
        "--redact-unaddressable",
        help="ALSO erase legacy content no subject key can ever reach. Irreversible.",
    ),
    json_mode: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Backfill CF-165 lineage on sidecar rows written before the migration.

    Dry run by default. Fills merge_audit namespaces only where a surviving node PROVES them --
    merge requires both participants to share a namespace, so a live survivor settles both sides;
    a missing survivor proves nothing and the row is left NULL.

    Rows in mcp_events / extraction_lab_runs that predate lineage carry memory text with no
    subject, so nothing can derive one. They are reported, and --redact-unaddressable erases
    them. That destroys historical telemetry and cannot be undone, which is why it is separate
    and opt-in: the alternative is retaining content that no erasure request can ever reach.
    """
    import json
    import sqlite3

    configure_logging()
    from menhir.config import MemorySettings
    from menhir.core import build_memory_services
    from menhir.infrastructure.graph_operations import GraphOperationsJournal
    from menhir.services.erasure_backfill import run_backfill

    settings = MemorySettings.from_env()
    built = build_memory_services(settings)
    db_path = GraphOperationsJournal().db_path
    conn = sqlite3.connect(db_path)
    try:
        report = run_backfill(
            conn,
            built.graph_adapter,
            dry_run=not apply,
            redact_unaddressable=redact_unaddressable,
        )
    finally:
        conn.close()

    if json_mode:
        typer.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        typer.echo(report.render())


@app.command("saga-preflight")
def saga_preflight(
    json_mode: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Judge whether live saga recovery may be switched on for THIS deployment (CF-20c).

    Read-only. Reports what is sitting in the PREPARED backlog and whether this host can be trusted
    to prove a writer dead, then gives a CLEAN / NOT CLEAN verdict. Run it before setting
    MENHIR_SAGA_RECONCILE_STARTUP_MODE=live, and run it again on every deployment separately --
    the answer is a property of the deployment, not of the build.

    Exit code 0 means clean, 1 means blocked. Warnings do not affect the exit code: a narrowed
    capability is a legitimate configuration, not a reason to refuse.
    """
    import json

    configure_logging()
    from menhir.config import MemorySettings
    from menhir.core import build_memory_services
    from menhir.services.saga_preflight import build_default_dispatcher, run_preflight

    settings = MemorySettings.from_env()
    built = build_memory_services(settings)
    report = run_preflight(build_default_dispatcher(built.graph_adapter))

    if json_mode:
        typer.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        typer.echo(report.render())
    raise SystemExit(0 if report.clean else 1)


@app.command()
def check() -> None:
    """Run service dependency health checks."""
    configure_logging()
    from menhir.config import MemorySettings
    from menhir.core import collect_runtime_failures
    from menhir.env_file import load_menhir_env

    load_menhir_env()
    settings = MemorySettings.from_env()
    failures = collect_runtime_failures(settings, require_venv=None)
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
    from menhir.env_file import load_menhir_env
    from menhir.operator_diagnostics import build_operator_diagnostics

    load_menhir_env()
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

    from menhir.env_file import load_menhir_env

    load_menhir_env()

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
        or settings.agent_key
        or settings.operator_key
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


app.command("serve")(serve)
app.command("serve-watch")(serve_watch)
