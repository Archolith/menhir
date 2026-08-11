"""`menhir artifacts` — corpus validation, audit, and digest-gated reconcile.

An operator command rather than an MCP tool, because reconciliation needs the
working tree, Git, and the graph at once, and only a process sitting in the
repository has all three. The MCP surface gets the read-only summary and a
single targeted relocation; bulk mutation stays here where it can be reviewed
before it runs.

Dry run is the default everywhere. ``--apply`` additionally requires the digest
of the ledger being applied, so an approved plan cannot be applied to a
different state than the one it was approved against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

artifacts_app = typer.Typer(
    name="artifacts",
    help="Reconcile the work-artifact corpus with the graph.",
    no_args_is_help=True,
)

#: Exit codes the caller can branch on. A digest mismatch is deliberately its
#: own code: it means "your plan is stale", not "reconciliation failed".
EXIT_FINDINGS = 1
EXIT_DIGEST_MISMATCH = 2
EXIT_UNAVAILABLE = 3


def _service() -> Any:
    """Connect to the graph and build the service. Imports are local.

    ``menhir artifacts validate`` must work in a repository with no database
    reachable, so nothing at module import time may require Neo4j.
    """
    from dotenv import load_dotenv

    from menhir.config.settings_model import MemorySettings
    from menhir.infrastructure.neo4j import Neo4jRepository
    from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository
    from menhir.services.artifact_reconciliation_service import (
        ArtifactReconciliationService,
    )

    load_dotenv()
    settings = MemorySettings.from_env()
    neo4j = Neo4jRepository(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        database=settings.neo4j_database,
    )
    return ArtifactReconciliationService(WorkArtifactRepository(neo4j))


def _offline_service() -> Any:
    """A service whose repository reports no sources. Validation only."""
    from menhir.services.artifact_reconciliation_service import (
        ArtifactReconciliationService,
    )

    class _NoSources:
        def list_artifact_source_snapshots(self, *, repository: str | None = None) -> list:
            return []

    return ArtifactReconciliationService(_NoSources())


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


@artifacts_app.command()
def validate(
    path: Annotated[str, typer.Argument(help="Repository root to validate.")] = ".",
    repository: Annotated[str, typer.Option(help="Repository name recorded on sources.")] = "",
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Check the corpus against the authoring contract. No graph access, no writes."""
    service = _offline_service()
    report = service.validate(Path(path), repository=repository or None)

    if as_json:
        _emit(report.as_dict())
    else:
        print(f"validated  : {report.checked} records in {report.repository}")
        if report.ok:
            print("findings   : none")
        else:
            print(f"findings   : {len(report.findings)}")
            for finding in report.findings:
                detail = f"  ({finding.detail})" if finding.detail else ""
                print(f"  {finding.code:<38} {finding.path}{detail}")
    raise typer.Exit(0 if report.ok else EXIT_FINDINGS)


@artifacts_app.command()
def audit(
    repository: Annotated[
        str, typer.Option(help="Repository name recorded on sources (required).")
    ],
    repo: Annotated[str, typer.Option(help="Repository root to audit.")] = ".",
    from_commit: Annotated[str, typer.Option(help="Override the persisted cursor for Git evidence only.")] = "",
    as_json: Annotated[bool, typer.Option("--json", help="Emit the full ledger as JSON.")] = False,
) -> None:
    """Report graph/filesystem parity. Read-only: this command never writes."""
    try:
        service = _service()
    except Exception as exc:  # noqa: BLE001 - operator CLI reports rather than traces
        print(f"graph unavailable: {exc}")
        raise typer.Exit(EXIT_UNAVAILABLE) from exc

    report = service.audit(
        Path(repo),
        repository=repository,
        from_commit=from_commit or None,
    )
    if as_json:
        _emit(report.as_dict())
        return
    _print_report(report)


def _print_report(report: Any) -> None:
    counts = report.counts
    print(f"repository      : {report.repository}")
    print(f"observed commit : {(report.observed_commit or 'unknown')[:12]}")
    print(f"stored cursor   : {(report.cursor_commit or 'none')[:12]}")
    print(f"evidence base   : {(report.evidence_from_commit or 'full audit')[:12]}")
    print(f"evidence valid  : {report.evidence_base_valid}")
    print(f"plan digest     : {report.plan_digest}")
    print(f"corpus entries  : {counts.get('entries', 0)}")
    print(f"graph sources   : {counts.get('sources', 0)}")
    print(f"  by lane       : {counts.get('entries_by_lane', {})}")
    print(f"  by type       : {counts.get('entries_by_type', {})}")
    print(f"actions         : {counts.get('by_kind', {})}")
    print(f"  match basis   : {counts.get('by_basis', {})}")
    print(f"conflicts       : {counts.get('by_conflict', {})}")
    print(f"contradictions  : {counts.get('contradictions', 0)}")

    conflicts = report.conflicts
    if conflicts:
        print("\nconflicts (no mutation is proposed for these):")
        for action in conflicts[:20]:
            print(f"  {action.conflict_kind:<28} {action.path or action.old_path}")
        if len(conflicts) > 20:
            print(f"  ... {len(conflicts) - 20} more (use --json for the full ledger)")

    if report.contradictions:
        print("\nlane/lifecycle contradictions (owner disposition, not automatic):")
        for item in report.contradictions[:20]:
            print(f"  {item.reason:<44} {item.status:<12} {item.path}")
        if len(report.contradictions) > 20:
            print(f"  ... {len(report.contradictions) - 20} more")


@artifacts_app.command()
def reconcile(
    repository: Annotated[
        str, typer.Option(help="Repository name recorded on sources (required).")
    ],
    repo: Annotated[str, typer.Option(help="Repository root to reconcile.")] = ".",
    from_commit: Annotated[str, typer.Option(help="Override the persisted cursor for Git evidence only.")] = "",
    apply: Annotated[bool, typer.Option("--apply", help="Write. Requires --plan-digest.")] = False,
    plan_digest: Annotated[str, typer.Option(help="Digest of the approved audit ledger.")] = "",
    prepare: Annotated[bool, typer.Option("--prepare", help="Backfill source UUIDs and locator keys first.")] = False,
    allow_new_repository: Annotated[
        bool,
        typer.Option(
            "--allow-new-repository",
            help="Permit first registration when the named repository has no sources.",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Apply the safe actions of an approved ledger. Dry run unless --apply."""
    try:
        service = _service()
    except Exception as exc:  # noqa: BLE001
        print(f"graph unavailable: {exc}")
        raise typer.Exit(EXIT_UNAVAILABLE) from exc

    if prepare:
        stamped = service.prepare_sources()
        print(f"prepared: {stamped}")

    if not apply:
        report = service.audit(
            Path(repo), repository=repository, from_commit=from_commit or None
        )
        if as_json:
            _emit(report.as_dict())
        else:
            _print_report(report)
            print("\nDRY RUN -- nothing written.")
            # Echo every option that fed the plan. Dropping one changes the
            # digest, and the operator would be told their approved plan is
            # stale by the command this line told them to run.
            options = [f"--repo {repo}"]
            options.append(f"--repository {repository}")
            if from_commit:
                options.append(f"--from-commit {from_commit}")
            if allow_new_repository:
                options.append("--allow-new-repository")
            options.append(f"--apply --plan-digest {report.plan_digest}")
            print("Re-run with: menhir artifacts reconcile " + " ".join(options))
        return

    if not plan_digest:
        print("--apply requires --plan-digest from an approved audit ledger.")
        raise typer.Exit(EXIT_DIGEST_MISMATCH)

    result = service.apply(
        Path(repo),
        expected_digest=plan_digest,
        repository=repository,
        from_commit=from_commit or None,
        allow_new_repository=allow_new_repository,
    )
    if as_json:
        _emit(result.as_dict())
    else:
        if not result.ok:
            print(f"refused: {result.refused_reason}")
            print(f"current digest: {result.plan_digest}")
        else:
            print(f"run id   : {result.run_id}")
            print(f"applied  : {len(result.applied)}")
            print(f"skipped  : {len(result.skipped)}")
            print(f"conflicts: {len(result.conflicted)}")
            cursor = "advanced" if result.cursor_advanced else result.cursor_reason
            print(f"cursor   : {cursor or 'retained'}")
            for record in result.skipped[:20]:
                outcome = record.get("outcome", {})
                print(f"  skipped {record.get('kind')} {record.get('path')}: "
                      f"{outcome.get('reason')}")
    if not result.ok:
        raise typer.Exit(EXIT_DIGEST_MISMATCH)


@artifacts_app.command("relocate")
def relocate(
    old_path: Annotated[str, typer.Argument(help="Current locator path in the graph.")],
    new_path: Annotated[str, typer.Argument(help="Path the source now lives at.")],
    repository: Annotated[
        str, typer.Option(help="Repository name recorded on the source (required).")
    ],
    medium: Annotated[str, typer.Option(help="Embodiment medium.")] = "markdown",
    repo: Annotated[str, typer.Option(help="Repository root, for hashing the destination.")] = ".",
) -> None:
    """Repair one move by hand, when Git evidence is unavailable.

    Uses the same collision checks as the reconciler. This is an escape hatch
    for an audited ambiguity, not the routine path -- ordinary moves are picked
    up by the hook or the next audit.
    """
    try:
        service = _service()
    except Exception as exc:  # noqa: BLE001
        print(f"graph unavailable: {exc}")
        raise typer.Exit(EXIT_UNAVAILABLE) from exc

    result = service.relocate_manually(
        repository=repository,
        medium=medium,
        old_path=old_path,
        new_path=new_path,
        repo_root=repo,
    )
    _emit(result)
    if not result.get("applied"):
        raise typer.Exit(EXIT_FINDINGS)
