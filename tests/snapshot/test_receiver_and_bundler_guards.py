"""Guards in `receive.py` and `bundler.py` that no test was holding.

Found by mutation testing: 9 survivors in each. Two shapes again -- refusals nothing exercises,
and boundaries only ever tested from far away.

The bundler's git-failure paths are reachable because it INJECTS its runner. That is worth noting
as a design property rather than a testing trick: a module that shells out and offers no seam can
only have its failure paths tested by breaking the real tool, which nobody does, which is how
those paths stay unexercised until a user finds them.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from menhir.snapshot import bundler as bundler_module
from menhir.snapshot.bundler import (
    BundlerError,
    resolve_repo_root,
)
from menhir.snapshot.protocol import PROVISIONAL_LIMITS, SnapshotLimits
from menhir.snapshot.receive import (
    ERR_DISK_BUDGET,
    ERR_SIZE,
    ReceiveError,
    StagingQuotas,
    StagingReceiver,
    UploadState,
)

pytestmark = pytest.mark.unit

_LIMITS = SnapshotLimits(chunk_bytes=8, max_chunk_bytes=16, max_compressed_bytes=1024)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def receiver(tmp_path: Path, clock: FakeClock) -> StagingReceiver:
    return StagingReceiver(tmp_path / "staging", limits=_LIMITS, clock=clock)


# --- receive.py: the bounds at their edges ---------------------------------------------------------


def test_a_zero_byte_upload_is_accepted(receiver: StagingReceiver) -> None:
    """Zero is the lower clause of `declared_bytes < 0 or > max`, and it is legitimate.

    An empty bundle is a real thing -- a repository with every file omitted by policy produces
    one. Tightening the comparison to `<= 0` would refuse it, and no test noticed.
    """
    record = receiver.begin(principal="alice", project_key="p", declared_bytes=0)

    assert record.state is UploadState.RECEIVING
    assert record.total_chunks == 0


def test_a_declared_size_exactly_at_the_compressed_limit_is_accepted(
    receiver: StagingReceiver,
) -> None:
    record = receiver.begin(
        principal="alice", project_key="p", declared_bytes=_LIMITS.max_compressed_bytes
    )

    assert record.declared_bytes == _LIMITS.max_compressed_bytes


def test_a_declared_size_one_byte_over_the_compressed_limit_is_refused(
    receiver: StagingReceiver,
) -> None:
    with pytest.raises(ReceiveError) as excinfo:
        receiver.begin(
            principal="alice",
            project_key="p",
            declared_bytes=_LIMITS.max_compressed_bytes + 1,
        )

    assert excinfo.value.code == ERR_SIZE


def test_a_chunk_size_of_zero_is_read_as_unspecified_not_refused(
    receiver: StagingReceiver,
) -> None:
    """Characterises current behaviour and flags it; not changed here.

    `begin` computes `negotiated = chunk_bytes or self.limits.chunk_bytes`, and 0 is falsy, so an
    explicit request for a zero-byte chunk silently becomes the DEFAULT rather than an error. The
    signature is `chunk_bytes: int | None = None`, so `or` conflates "unspecified" with "zero" --
    two different statements from a caller, given one answer.

    A consequence worth recording: the `negotiated <= 0` clause on the next line is therefore
    unreachable through the public API, which is why its mutation survives. It can only fire if
    `limits.chunk_bytes` were itself <= 0. That makes it defensive rather than dead, but nothing
    reaches it.

    Harmless today -- the client sends a sensible size or none -- and the kind of conflation worth
    settling deliberately rather than discovering when a client starts computing chunk sizes.
    """
    record = receiver.begin(
        principal="alice", project_key="p", declared_bytes=16, chunk_bytes=0
    )

    assert record.chunk_bytes == _LIMITS.chunk_bytes, (
        "0 no longer falls back to the default"
    )


def test_a_chunk_size_exactly_at_the_hard_ceiling_is_accepted(
    receiver: StagingReceiver,
) -> None:
    record = receiver.begin(
        principal="alice",
        project_key="p",
        declared_bytes=16,
        chunk_bytes=_LIMITS.max_chunk_bytes,
    )

    assert record.chunk_bytes == _LIMITS.max_chunk_bytes


def test_a_begin_exactly_at_the_disk_budget_is_admitted(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The budget bounds what is admitted; landing exactly on it is not over it."""
    receiver = StagingReceiver(
        tmp_path / "s",
        limits=_LIMITS,
        quotas=StagingQuotas(disk_budget_bytes=512),
        clock=clock,
    )

    record = receiver.begin(principal="alice", project_key="p", declared_bytes=512)

    assert record.state is UploadState.RECEIVING


def test_a_begin_one_byte_over_the_disk_budget_is_refused(
    tmp_path: Path, clock: FakeClock
) -> None:
    receiver = StagingReceiver(
        tmp_path / "s",
        limits=_LIMITS,
        quotas=StagingQuotas(disk_budget_bytes=512),
        clock=clock,
    )

    with pytest.raises(ReceiveError) as excinfo:
        receiver.begin(principal="alice", project_key="p", declared_bytes=513)

    assert excinfo.value.code == ERR_DISK_BUDGET


def test_a_terminal_record_survives_until_exactly_its_retention_boundary(
    receiver: StagingReceiver, clock: FakeClock
) -> None:
    """`idle >= terminal_retention_s`: at the boundary it goes, one tick before it stays."""
    record = receiver.begin(principal="alice", project_key="p", declared_bytes=16)
    receiver.abort(upload_id=record.upload_id, principal="alice")

    clock.advance(receiver.quotas.terminal_retention_s - 1)
    receiver.sweep()
    assert receiver.status(upload_id=record.upload_id, principal="alice")

    clock.advance(1)
    receiver.sweep()
    with pytest.raises(ReceiveError):
        receiver.status(upload_id=record.upload_id, principal="alice")


# --- bundler.py: the git failure paths -------------------------------------------------------------


def _raising_runner(exc: BaseException):
    def run(_args):
        raise exc

    return run


def test_git_missing_from_path_is_explained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first thing a new user hits if git is not installed.

    Reported as its own code rather than a generic failure, because 'git was not found' and 'git
    said no' need different actions from the person reading it.
    """
    monkeypatch.setattr(
        bundler_module.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")),
    )

    with pytest.raises(BundlerError) as excinfo:
        resolve_repo_root(tmp_path)

    assert "git" in str(excinfo.value).lower()


def test_a_git_timeout_is_reported_as_a_failure_not_a_hang(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        bundler_module.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 30)),
    )

    with pytest.raises(BundlerError) as excinfo:
        resolve_repo_root(tmp_path)

    assert "timed out" in str(excinfo.value).lower()


def test_a_directory_that_is_not_a_repository_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Distinguished from a generic git failure by inspecting stderr.

    The distinction is the useful part: 'this is not a repository' tells the user what to do, and
    'git failed' does not.
    """
    error = subprocess.CalledProcessError(128, "git")
    error.stderr = (
        b"fatal: not a git repository (or any of the parent directories): .git"
    )
    monkeypatch.setattr(
        bundler_module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(error)
    )

    with pytest.raises(BundlerError) as excinfo:
        resolve_repo_root(tmp_path)

    assert "not a git repository" in str(excinfo.value).lower()


def test_any_other_git_failure_is_reported_with_its_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The branch the not-a-repo check falls through to."""
    error = subprocess.CalledProcessError(1, "git")
    error.stderr = b"fatal: detected dubious ownership"
    monkeypatch.setattr(
        bundler_module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(error)
    )

    with pytest.raises(BundlerError) as excinfo:
        resolve_repo_root(tmp_path)

    assert "dubious ownership" in str(excinfo.value)


def test_git_reporting_no_repository_root_is_refused(tmp_path: Path) -> None:
    """Empty output where a path was expected: succeeded, said nothing, must not be believed."""
    with pytest.raises(BundlerError) as excinfo:
        resolve_repo_root(tmp_path, runner=lambda _args: b"   \n")

    assert "repository root" in str(excinfo.value).lower()


def test_unparseable_ls_files_output_is_refused(tmp_path: Path) -> None:
    """A record that does not split into mode/object/stage.

    git's output format is a contract; a version that changed it, or a corrupted pipe, must stop
    the bundle rather than produce a partial one from whatever happened to parse.
    """
    from menhir.snapshot.bundler import build_plan

    def runner(args):
        if args[0] == "rev-parse":
            return str(tmp_path).encode()
        if args[0] == "ls-files":
            return b"this-record-has-no-tabs\0"
        return b""

    with pytest.raises(BundlerError) as excinfo:
        build_plan(tmp_path, runner=runner)

    assert "ls-files" in str(excinfo.value)


# --- bundler.py: size boundaries -------------------------------------------------------------------


def _repo_with(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, object]:
    root = tmp_path / "repo"
    root.mkdir()
    for name, content in files.items():
        (root / name).write_bytes(content)

    def runner(args):
        if args[0] == "rev-parse":
            return str(root).encode()
        if args[0] == "ls-files":
            return b"".join(b"100644 abc 0\t" + n.encode() + b"\0" for n in files)
        return b""

    return root, runner


def test_a_file_exactly_at_the_per_file_limit_is_included(tmp_path: Path) -> None:
    from menhir.snapshot.bundler import build_plan

    root, runner = _repo_with(tmp_path, {"a.bin": b"x" * 64})
    limits = replace(PROVISIONAL_LIMITS, max_file_bytes=64)

    plan = build_plan(root, runner=runner, limits=limits)

    assert [f.path for f in plan.manifest.files] == ["a.bin"]


def test_a_file_one_byte_over_the_per_file_limit_becomes_a_declared_omission(
    tmp_path: Path,
) -> None:
    """Omitted, not refused: an oversized file is a normal fact about a repository.

    It is DECLARED, though, because a file the server never receives and never hears about would
    read as a deletion.
    """
    from menhir.snapshot.bundler import build_plan

    root, runner = _repo_with(tmp_path, {"a.bin": b"x" * 65})
    limits = replace(PROVISIONAL_LIMITS, max_file_bytes=64)

    plan = build_plan(root, runner=runner, limits=limits)

    assert [f.path for f in plan.manifest.files] == []
    assert [o.path for o in plan.manifest.omissions] == ["a.bin"]


def test_a_total_one_byte_over_the_limit_stops_the_bundle(tmp_path: Path) -> None:
    """Unlike a single oversized file, blowing the TOTAL is not something to omit around."""
    from menhir.snapshot.bundler import build_plan

    root, runner = _repo_with(tmp_path, {"a.bin": b"x" * 40, "b.bin": b"y" * 41})
    limits = replace(PROVISIONAL_LIMITS, max_file_bytes=64, max_total_bytes=80)

    with pytest.raises(BundlerError) as excinfo:
        build_plan(root, runner=runner, limits=limits)

    assert "total limit" in str(excinfo.value).lower()


# --- policy.py: the per-file boundary --------------------------------------------------------------


def test_the_policy_includes_a_file_exactly_at_the_per_file_limit() -> None:
    """The last survivor in `policy.py`, and the same shape as all the others.

    Existing policy tests use a file far over the limit, so `>` and `>=` behave identically. A file
    of exactly the limit must be INCLUDED -- tightening the comparison would start omitting files
    at a size that is explicitly allowed, and the omission would be declared rather than loud, so
    nobody would notice until a snapshot was quietly missing something.
    """
    from menhir.snapshot.policy import Decision, SelectionPolicy

    policy = SelectionPolicy(max_file_bytes=64)

    assert policy.classify("src/a.py", 64).decision is Decision.INCLUDE


def test_the_policy_omits_a_file_one_byte_over_the_per_file_limit() -> None:
    from menhir.snapshot.policy import Decision, SelectionPolicy

    policy = SelectionPolicy(max_file_bytes=64)

    verdict = policy.classify("src/a.py", 65)

    assert verdict.decision is Decision.OMIT
