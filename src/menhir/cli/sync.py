"""`menhir sync`: send this machine's project structure to a remote Menhir.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md``.

``--check`` runs entirely locally: it reads the repository, applies the selection policy, and
prints what a sync WOULD upload, making no network call and writing no archive. Without it the
same plan is built and then uploaded, which became possible once P2A measured the transport and
P2B built the client.

**A refusal stops a send, structurally.** The blocked check runs before the upload branch rather
than inside it, so while a secret-risk path stands there is no code path that reaches the network.

The report deliberately prints full paths for refusals and omissions. That is safe precisely
because it never leaves the user's machine; the same strings are forbidden in anything the server
logs or emits (plan invariant 5).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

import typer

from menhir.config import MemorySettings
from menhir.snapshot.bundler import (
    BundlePlan,
    BundlerError,
    build_plan,
    estimate_archive_upper_bound,
    write_bundle,
)
from menhir.snapshot.policy import SelectionPolicy
from menhir.snapshot.protocol import PROVISIONAL_LIMITS, chunk_count
from menhir.snapshot.upload_client import SnapshotUploader, SnapshotUploadError

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
    prov = manifest.provenance
    echo(f"base commit     {prov.base_commit or '(no commits)'}")
    echo(f"branch          {prov.branch or '(detached)'}")
    # Said plainly, because it is the question the stamp exists to answer: a commit id on its own
    # reads as "these were the bytes at that commit" and frequently is not.
    if prov.dirty:
        echo("working tree    DIRTY -- bundled bytes are not the bytes at that commit")
    else:
        echo("working tree    clean (tracked files match the base commit)")
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


def _remote_target(settings: MemorySettings) -> tuple[str, str]:
    """Resolve the remote URL and the key that can actually drive the snapshot tools.

    Deliberately NOT `resolve_backend_auth_key`: that returns the agent key, and the receive tools
    are operator-tier. Sending an agent key produces a permission refusal that says nothing about
    tiers, so the caller would go looking for a broken server instead of a wrong key.
    """
    url = (settings.backend_url or "").strip()
    if not url:
        typer.echo(
            "menhir sync needs a remote Menhir to send to, and MENHIR_BACKEND_URL is not set.\n"
            "Run `menhir sync --check` to see what a sync would send, without a network call."
        )
        raise typer.Exit(code=2)

    key = (settings.operator_key or "").strip()
    if not key:
        typer.echo(
            "menhir sync needs MENHIR_OPERATOR_KEY.\n"
            "The snapshot receive tools are operator-tier, so an agent or readonly key is refused\n"
            "by the server with a message about permissions rather than about the key you set."
        )
        raise typer.Exit(code=2)
    return url, key


def _upload(plan: BundlePlan, *, settings: MemorySettings) -> None:
    """Write the bundle to a temporary file and send it.

    A file, not memory: the pilot quota admits 64 MiB compressed and the uploader streams from
    disk a chunk at a time, so holding the whole archive in the client would be the only place
    the bundle size mattered.
    """
    url, key = _remote_target(settings)
    echo = typer.echo

    with tempfile.TemporaryDirectory(prefix="menhir-sync-") as workspace:
        archive = Path(workspace) / "bundle.zip"
        with archive.open("wb") as handle:
            write_bundle(plan, handle)

        size = archive.stat().st_size
        echo("")
        echo(f"archive         {size} bytes")
        echo(f"sending to      {url}")

        def progress(sent: int, total: int) -> None:
            # Rewritten in place rather than one line per chunk: a 64 MiB bundle is ~64 chunks and
            # a scrolling log buries the result.
            typer.echo(f"\r  chunk {sent}/{total}", nl=False)

        try:
            outcome = SnapshotUploader(url, auth_key=key).upload(
                archive,
                project_key=plan.manifest.display_name,
                progress=progress,
            )
        except SnapshotUploadError as exc:
            echo("")
            echo(f"upload failed   {exc}")
            echo(f"                ({exc.code})")
            raise typer.Exit(code=1) from exc

    echo("")
    echo(f"upload          {outcome.state}")
    echo(
        f"                {outcome.sent_chunks} chunk(s) of {outcome.chunk_bytes} bytes, "
        f"{outcome.bytes_sent} bytes sent"
    )
    if outcome.state != "SEALED":
        # Every chunk was accepted and the upload still is not whole. Not a transport failure, so
        # it must not read like success -- a partial upload nobody notices is the worst outcome
        # here, because the server holds it until the inactivity TTL.
        echo("                the server does not consider this upload complete")
        raise typer.Exit(code=1)
    echo("")
    echo("The snapshot is staged on the server. Nothing has been extracted or written to the")
    echo("graph: that arrives with later phases of the snapshot plan.")


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
    """Send this repository's structure to a remote Menhir, or report what would be sent.

    `--check` is local only: it reads the repository, applies the selection policy and prints the
    plan, making no network call and writing no archive. Without it, the same plan is built and
    then actually uploaded.
    """
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
    # Checked before the upload branch and not inside it: a refusal must stop a send, and the
    # cheapest way to guarantee that is for the send to be unreachable while one stands.
    if plan.blocked:
        raise typer.Exit(code=1)
    if check:
        return

    _upload(plan, settings=MemorySettings.from_env())
