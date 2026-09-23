"""`menhir sync`: send this machine's project structure to a remote Menhir.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md``.

``--check`` runs entirely locally: it reads the repository, applies the selection policy, and
prints what a sync WOULD upload, making no network call and writing no archive. Without it the
same plan is built, uploaded, explicitly committed, and reported at the server's terminal stage.

**A refusal stops a send, structurally.** The blocked check runs before the upload branch rather
than inside it, so while a secret-risk path stands there is no code path that reaches the network.

The report deliberately prints full paths for refusals and omissions. That is safe precisely
because it never leaves the user's machine; the same strings are forbidden in anything the server
logs or emits (plan invariant 5).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
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


def _remote_receipt_path(root: Path, url: str) -> Path:
    key = hashlib.sha256(url.rstrip("/").encode("utf-8")).hexdigest()[:24]
    return root / ".menhir" / "sources" / f"{key}.json"


def _valid_remote_project_id(project_id: str) -> bool:
    return (
        project_id.startswith("project-")
        and len(project_id) == 40
        and all(c in "0123456789abcdef" for c in project_id[8:])
    )


def _read_remote_project_id(root: Path, url: str) -> str | None:
    path = _remote_receipt_path(root, url)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        project_id = str(raw["project_id"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SnapshotUploadError(
            "sync.identity.invalid_receipt",
            f"{path} is not a usable remote project receipt; repair it deliberately",
        ) from exc
    if raw.get("server_url") != url.rstrip("/") or not _valid_remote_project_id(project_id):
        raise SnapshotUploadError(
            "sync.identity.invalid_receipt",
            f"{path} does not match this remote Menhir",
        )
    return project_id


def _write_remote_project_id(root: Path, url: str, project_id: str) -> None:
    if not _valid_remote_project_id(project_id):
        raise SnapshotUploadError(
            "sync.identity.invalid_server_receipt",
            "the server returned an unusable project identity",
        )
    path = _remote_receipt_path(root, url)
    payload = json.dumps(
        {"schema": 1, "server_url": url.rstrip("/"), "project_id": project_id},
        sort_keys=True,
    ).encode("utf-8")
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_temp = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(raw_temp)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Linking a fully-written inode creates the final name atomically without replacing a
            # receipt another process may have won concurrently. A crash can leave only a hidden
            # temp file, never a partial authoritative receipt.
            os.link(temp_path, path)
        except FileExistsError:
            existing = _read_remote_project_id(root, url)
            if existing != project_id:
                raise SnapshotUploadError(
                    "sync.identity.receipt_conflict",
                    "the server returned a different project identity than this checkout records",
                )
    except SnapshotUploadError:
        raise
    except OSError as exc:
        raise SnapshotUploadError(
            "sync.identity.receipt_write_failed",
            "the remote project identity could not be stored locally",
        ) from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


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
                project_id=plan.manifest.project_id,
                progress=progress,
            )
        except SnapshotUploadError as exc:
            echo("")
            echo(f"upload failed   {exc}")
            echo(f"                ({exc.code})")
            raise typer.Exit(code=1) from exc

    try:
        _write_remote_project_id(plan.root, url, outcome.project_id)
    except SnapshotUploadError as exc:
        echo("")
        echo(f"sync completed, but the local identity receipt failed: {exc}")
        echo(f"                ({exc.code})")
        raise typer.Exit(code=1) from exc

    echo("")
    echo(f"upload          {outcome.state}")
    echo(
        f"                {outcome.sent_chunks} chunk(s) of {outcome.chunk_bytes} bytes, "
        f"{outcome.bytes_sent} bytes sent"
    )
    if outcome.state not in {"SEALED", "READY"}:
        echo("                the server did not reach a successful terminal stage")
        raise typer.Exit(code=1)
    echo("")
    stage = str(outcome.result.get("stage") or "received")
    echo(f"server stage    {stage}")
    echo(f"project id      {outcome.project_id}")
    echo(f"snapshot id     {outcome.snapshot_id}")
    if stage == "received":
        echo("The server retained the sealed snapshot without opening it (receive mode).")
    elif stage == "scanned":
        echo("The server extracted and scanned the snapshot without writing the graph (shadow mode).")
    elif stage == "published":
        echo("The server published the snapshot as the project's canonical structural view.")


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
    settings = MemorySettings.from_env()
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
    if not check and (settings.backend_url or "").strip():
        try:
            project_id = _read_remote_project_id(plan.root, settings.backend_url.strip())
        except SnapshotUploadError as exc:
            typer.echo(f"{exc} ({exc.code})")
            raise typer.Exit(code=1) from exc
        if project_id:
            plan = replace(plan, manifest=replace(plan.manifest, project_id=project_id))

    _report(plan)
    # Checked before the upload branch and not inside it: a refusal must stop a send, and the
    # cheapest way to guarantee that is for the send to be unreachable while one stands.
    if plan.blocked:
        raise typer.Exit(code=1)
    if check:
        return

    _upload(plan, settings=settings)
