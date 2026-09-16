"""`menhir sync`: send this machine's project structure to a remote Menhir.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md``.

Only ``--check`` works today, by design. It runs entirely locally -- it reads the repository,
applies the selection policy, and prints what a sync WOULD upload -- and makes no network call and
writes no archive. The upload half is plan phase P2 and is gated on P0's measured transport
limits, so an unqualified ``menhir sync`` refuses with that explanation rather than appearing to
work.

The report deliberately prints full paths for refusals and omissions. That is safe precisely
because it never leaves the user's machine; the same strings are forbidden in anything the server
logs or emits (plan invariant 5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from menhir.snapshot.bundler import (
    BundlePlan,
    BundlerError,
    build_plan,
    estimate_archive_upper_bound,
)
from menhir.snapshot.policy import SelectionPolicy
from menhir.snapshot.protocol import PROVISIONAL_LIMITS, chunk_count

#: Grouped omission headings, in the order a reader most likely cares about.
_OMISSION_LABELS = {
    "submodule": "submodules (not recursed; declared so the server does not read them as deleted)",
    "symlink": "symlinks (regular files only)",
    "oversize": "over the per-file size limit",
    "excluded_dir": "inside directories the structure scanner skips",
    "local_receipt": "Menhir's own local receipts",
    "unreadable": "unreadable or unrepresentable",
}


def _report(plan: BundlePlan, *, limits=PROVISIONAL_LIMITS) -> None:
    manifest = plan.manifest
    echo = typer.echo

    echo(f"repository      {plan.root}")
    echo(f"project name    {manifest.display_name}")
    echo(f"source HEAD     {manifest.source_head or '(no commits)'}")
    echo(f"policy version  {manifest.policy_version} (protocol v{manifest.protocol_version})")
    echo("")
    echo(f"files to upload {manifest.file_count}")
    echo(f"content bytes   {manifest.total_bytes}")
    upper_bound = estimate_archive_upper_bound(manifest)
    echo(
        f"archive size    <= {upper_bound} bytes "
        f"({chunk_count(upper_bound, limits.chunk_bytes)} chunks at "
        f"{limits.chunk_bytes}-byte chunks, provisional)"
    )
    echo(f"tree digest     {manifest.tree_digest}")

    if plan.deleted:
        echo("")
        echo(f"deletions       {len(plan.deleted)} tracked path(s) absent from the working tree")
        for path in plan.deleted[:10]:
            echo(f"  - {path}")
        if len(plan.deleted) > 10:
            echo(f"  ... and {len(plan.deleted) - 10} more")

    if manifest.omissions:
        grouped: dict[str, list[str]] = {}
        for omission in manifest.omissions:
            grouped.setdefault(omission.reason, []).append(omission.path)
        echo("")
        echo(f"omitted         {len(manifest.omissions)} path(s), declared in the manifest")
        for reason, paths in sorted(grouped.items()):
            echo(f"  {_OMISSION_LABELS.get(reason, reason)}: {len(paths)}")
            for path in sorted(paths)[:5]:
                echo(f"    - {path}")
            if len(paths) > 5:
                echo(f"    ... and {len(paths) - 5} more")

    if plan.refusals:
        echo("")
        echo(f"REFUSED         {len(plan.refusals)} path(s) look like secret material:")
        for refusal in plan.refusals:
            echo(f"  - {refusal.path}  ({refusal.label})")
        echo("")
        echo("Nothing is uploaded while a refusal stands. Either remove the file from the")
        echo("repository, or, if it is genuinely safe to send, re-run with:")
        for refusal in plan.refusals[:3]:
            echo(f"  --allow-path {refusal.path}")
        echo("An override applies to the run you type it on; it is not remembered.")


def sync(
    path: Annotated[
        Path | None, typer.Argument(help="Project directory (default: current directory).")
    ] = None,
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Report what would be uploaded. Local only: no network, no archive."
        ),
    ] = False,
    name: Annotated[
        str | None, typer.Option("--name", help="Project display name (default: directory name).")
    ] = None,
    allow_path: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-path",
            help="Upload this secret-risk path anyway. Repeatable; applies to this run only.",
        ),
    ] = None,
) -> None:
    """Report what a remote structure sync would upload from this repository.

    Uploading is not available yet: it arrives with the MCP snapshot tools, whose transport
    limits must be measured first.
    """
    if not check:
        typer.echo(
            "menhir sync can only run with --check right now.\n"
            "The upload path (MCP snapshot tools) is not implemented yet, and its chunk and quota\n"
            "limits have to be measured through a real Streamable HTTP ingress before it ships.\n"
            "Run `menhir sync --check` to see exactly what a sync would send."
        )
        raise typer.Exit(code=2)

    policy = SelectionPolicy(
        max_file_bytes=PROVISIONAL_LIMITS.max_file_bytes,
        allowed_secret_paths=frozenset(allow_path or ()),
    )
    try:
        plan = build_plan(
            path or Path.cwd(),
            display_name=name,
            policy=policy,
            limits=PROVISIONAL_LIMITS,
        )
    except BundlerError as exc:
        typer.echo(f"{exc}")
        raise typer.Exit(code=1) from exc

    _report(plan)
    if plan.blocked:
        raise typer.Exit(code=1)
