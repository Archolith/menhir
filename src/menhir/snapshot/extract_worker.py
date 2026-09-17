"""Run one extraction in a child process, and survive it however it ends.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P3).
Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md` (decision 1).

`extraction_writer` holds the rules. This holds the blast radius. A malformed archive that hangs or
crashes the ZIP parser becomes a failed job rather than a server outage, which is the whole reason
decision 1 chose a separate process over a thread.

**A killed child runs no cleanup.** The writer removes its root in an `except` clause, and SIGKILL
raises nothing -- so a child killed at its deadline leaves a half-written root behind and the
PARENT has to remove it. That is the counterexample this module exists for, and it is easy to miss
precisely because the in-process cleanup is already correct and already tested.

**Containment is not uniform across platforms, and that is stated rather than implied.** The
wall-clock deadline works everywhere. The memory ceiling uses `RLIMIT_AS`, which POSIX has and
Windows does not: on Windows a bundle can still exhaust memory, bounded only by the machine. Menhir
deploys on Linux, so the gap is a development-environment gap -- but a caller that believes the
ceiling is universal is believing something false.

**Failures cross the boundary as codes, never as tracebacks.** A traceback from a child that was
parsing attacker-chosen bytes is both useless to the caller and a place for those bytes to appear.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from menhir.snapshot.archive_plan import PlannedEntry, plan_archive
from menhir.snapshot.extraction_lease import ExtractionLease, LeaseStore
from menhir.snapshot.extraction_writer import ExtractionError, materialize
from menhir.snapshot.protocol import PROVISIONAL_LIMITS, SnapshotLimits

__all__ = [
    "ERR_WORKER_CRASHED",
    "ERR_WORKER_TIMEOUT",
    "ERR_WORKER_UNREADABLE",
    "WorkerOutcome",
    "run_extraction",
]

ERR_WORKER_TIMEOUT = "snapshot.extract.worker_timeout"
ERR_WORKER_CRASHED = "snapshot.extract.worker_crashed"
ERR_WORKER_UNREADABLE = "snapshot.extract.worker_unreadable_reply"

#: Wall-clock ceiling for one extraction. Generous against a real 64 MiB bundle and far short of
#: "a hung parser nobody notices until the disk fills".
DEFAULT_TIMEOUT_S = 300.0

#: Address-space ceiling for the child, POSIX only. 1 GiB is several times what streaming a
#: bundle to disk needs and well under what a bomb wants.
DEFAULT_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class WorkerOutcome:
    root: Path
    file_count: int
    total_bytes: int
    digests: dict[str, str]


def _child_preexec(memory_limit_bytes: int):  # pragma: no cover -- POSIX only
    """Apply the address-space ceiling inside the child, before it parses anything."""
    try:
        import resource
    except ImportError:
        return None

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))

    return apply


def run_extraction(
    archive_path: Path,
    root: Path,
    *,
    lease: ExtractionLease,
    lease_root: Path,
    lease_ttl_s: float,
    limits: SnapshotLimits = PROVISIONAL_LIMITS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
    child_argv: list[str] | None = None,
) -> WorkerOutcome:
    """Extract `archive_path` into `root` in a child process.

    `child_argv` is injected rather than hooked: a production module with a test-only branch in it
    is a production module that can take that branch in production. Tests substitute a child that
    hangs or dies; nothing in this file knows it is being tested.
    """
    job = json.dumps(
        {
            "archive_path": str(archive_path),
            "root": str(root),
            "lease": {
                "project_key": lease.project_key,
                "upload_id": lease.upload_id,
                "owner": lease.owner,
                "generation": lease.generation,
                "expires_at": lease.expires_at,
            },
            "lease_root": str(lease_root),
            "lease_ttl_s": lease_ttl_s,
            "max_file_count": limits.max_file_count,
            "max_file_bytes": limits.max_file_bytes,
            "max_total_bytes": limits.max_total_bytes,
        }
    )
    argv = child_argv or [sys.executable, "-m", "menhir.snapshot.extract_worker"]

    popen_kwargs: dict[str, object] = {}
    if sys.platform != "win32":
        preexec = _child_preexec(memory_limit_bytes)
        if preexec is not None:
            popen_kwargs["preexec_fn"] = preexec

    try:
        completed = subprocess.run(
            argv,
            input=job,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            **popen_kwargs,  # type: ignore[arg-type]
        )
    except subprocess.TimeoutExpired as exc:
        # The child was killed. It ran no `except`, so its own cleanup never happened and the
        # half-written root is still there -- the parent owns removing it. This is the case the
        # in-process cleanup cannot cover, and the reason it is not enough on its own.
        shutil.rmtree(root, ignore_errors=True)
        raise ExtractionError(
            ERR_WORKER_TIMEOUT, f"extraction exceeded its {timeout_s:.0f}s deadline"
        ) from exc

    if completed.returncode != 0:
        # Includes a segfault and an OOM kill, neither of which produces a usable reply. stderr is
        # deliberately NOT propagated: the child was parsing attacker-chosen bytes.
        shutil.rmtree(root, ignore_errors=True)
        raise ExtractionError(
            ERR_WORKER_CRASHED,
            f"the extraction worker exited with status {completed.returncode}",
        )

    try:
        reply = json.loads(completed.stdout)
    except ValueError as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise ExtractionError(
            ERR_WORKER_UNREADABLE,
            "the extraction worker did not return a readable result",
        ) from exc

    if not reply.get("ok"):
        # A refusal the child reached in an orderly way: it cleaned its own root, so the parent
        # does not. Its code travels; its message does not.
        raise ExtractionError(
            str(reply.get("code") or ERR_WORKER_CRASHED), "the extraction was refused"
        )

    return WorkerOutcome(
        root=Path(reply["root"]),
        file_count=int(reply["file_count"]),
        total_bytes=int(reply["total_bytes"]),
        digests=dict(reply.get("digests") or {}),
    )


def _main() -> int:
    """Child entrypoint: read one job from stdin, write one JSON result to stdout."""
    try:
        job = json.loads(sys.stdin.read())
        archive_path = Path(job["archive_path"])
        limits = SnapshotLimits(
            max_file_count=int(job["max_file_count"]),
            max_file_bytes=int(job["max_file_bytes"]),
            max_total_bytes=int(job["max_total_bytes"]),
        )
        leases = LeaseStore(Path(job["lease_root"]), ttl_s=float(job["lease_ttl_s"]))
        raw_lease = job["lease"]
        lease = ExtractionLease(
            project_key=raw_lease["project_key"],
            upload_id=raw_lease["upload_id"],
            owner=raw_lease["owner"],
            generation=int(raw_lease["generation"]),
            expires_at=float(raw_lease["expires_at"]),
        )

        plan: list[PlannedEntry] = plan_archive(archive_path, limits)
        result = materialize(
            archive_path,
            plan,
            Path(job["root"]),
            lease=lease,
            leases=leases,
            limits=limits,
        )
    except Exception as exc:  # noqa: BLE001 -- every failure becomes a code, never a traceback
        code = getattr(exc, "code", None)
        sys.stdout.write(json.dumps({"ok": False, "code": code or ERR_WORKER_CRASHED}))
        return 0

    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "root": str(result.root),
                "file_count": result.file_count,
                "total_bytes": result.total_bytes,
                "digests": result.digests,
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised as a subprocess
    sys.exit(_main())
