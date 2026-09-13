"""Failure-path lifecycle of the maintenance markers.

These paths had never been exercised, by test or by a real run, before
2026-09-13. The first real install rollback found that a maintenance could be
neither completed nor abandoned afterwards, for two independent reasons, and
that every completed cycle left its markers behind for the next one.

See pipeline/MARKERS.md for the inventory these tests are drawn from.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scaffold" / "menhir_scaffold.py"
SPEC = importlib.util.spec_from_file_location("menhir_scaffold_lifecycle", SCRIPT)
scaffold = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scaffold)

HEX = "0" * 64
ATTEMPT = "a" * 32


def _journal(started: dt.datetime, *, release_id: str, stage: str = "start") -> dict:
    when = scaffold.iso(started)
    return {
        "schema": 1,
        "kind": "menhir-release-run",
        "release_id": release_id,
        "release_manifest_sha256": HEX,
        "stage": stage,
        "generation": "",
        "started_utc": when,
        "updated_utc": when,
        "completed_utc": scaffold.iso(started) if stage == "complete" else None,
        "runner_sha256": HEX,
        "approval_sha256": HEX,
        "promotion_attempt_id": ATTEMPT,
        "approved_utc": when,
        "promotion_started_utc": when,
    }


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    status = tmp_path / "status"
    status.mkdir()
    release = tmp_path / "release.json"
    monkeypatch.setattr(scaffold, "STATUS_ROOT", status)
    monkeypatch.setattr(scaffold, "RELEASE_RUN", status / "release-run.json")
    monkeypatch.setattr(scaffold, "FIRST_MUTATION", status / "first-mutation")
    monkeypatch.setattr(scaffold, "MAINTENANCE_HISTORY", status / "maintenance-history")
    monkeypatch.setattr(scaffold, "RELEASE_PATH", release)
    monkeypatch.setattr(scaffold, "ADMISSION_READY", tmp_path / "admission.ready")
    # Root-only host checks are not the subject here.
    monkeypatch.setattr(scaffold, "require_root", lambda: None)
    monkeypatch.setattr(scaffold, "require_safe_root_file", lambda path, label: None)
    monkeypatch.setattr(scaffold.os, "chown", lambda *a, **k: None, raising=False)

    # atomic_json fsyncs its parent directory via os.open(dir, O_RDONLY), which
    # Windows refuses. Durability is not under test here; keep the write.
    def _plain_json(path: Path, value, mode: int = 0o400) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="ascii")
    monkeypatch.setattr(scaffold, "atomic_json", _plain_json)
    monkeypatch.setattr(scaffold.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(scaffold, "verify_static", lambda c, r: {
        "contract": {"runtime": {"public_ready_url": "https://example.invalid/readyz"}}
    })
    monkeypatch.setattr(scaffold, "inspect_runtime", lambda contract: (True, []))
    monkeypatch.setattr(scaffold, "public_ready", lambda url: True)
    return {"status": status, "release": release, "tmp": tmp_path}


def _write(path: Path, value, *, mtime: dt.datetime | None = None) -> None:
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    if mtime is not None:
        stamp = mtime.timestamp()
        os.utime(path, (stamp, stamp))


def test_stale_marker_from_a_previous_cycle_does_not_veto_abandon(host: dict) -> None:
    """The live-host case on 2026-09-13.

    first-mutation was written by release 13's promote on Sep 7. Release 14's
    maintenance began Sep 13, its install rolled back, and abandon refused with
    'cannot abandon maintenance after first mutation' -- on a marker six days
    older than the journal. A marker older than the journal belongs to an
    earlier cycle and must be archived, not obeyed.
    """
    started = scaffold.utc_now() - dt.timedelta(minutes=5)
    _write(host["release"], {"release_id": "menhir-prod-0.2.0-13"})
    _write(scaffold.RELEASE_RUN, _journal(started, release_id="menhir-prod-0.2.0-14"))
    _write(scaffold.FIRST_MUTATION, "generation.old\n", mtime=started - dt.timedelta(days=6))

    result = scaffold.abandon_maintenance(Path("contract"), Path("receipt"), "rolled back")

    assert not scaffold.RELEASE_RUN.exists()
    assert not scaffold.FIRST_MUTATION.exists()
    archive = Path(result["archive"])
    assert (archive / "release-run.json").exists()
    assert (archive / "first-mutation").exists()
    assert "first-mutation" in result["moved_sha256"]


def test_marker_written_by_this_maintenance_still_vetoes_abandon(host: dict) -> None:
    """The protection is kept: a mutation made under this journal is a point of
    no return and abandon must refuse."""
    started = scaffold.utc_now() - dt.timedelta(minutes=5)
    _write(host["release"], {"release_id": "menhir-prod-0.2.0-14"})
    _write(scaffold.RELEASE_RUN, _journal(started, release_id="menhir-prod-0.2.0-14"))
    _write(scaffold.FIRST_MUTATION, "generation.new\n", mtime=started + dt.timedelta(minutes=1))

    with pytest.raises(scaffold.ScaffoldError, match="after first mutation"):
        scaffold.abandon_maintenance(Path("contract"), Path("receipt"), "attempt")
    assert scaffold.RELEASE_RUN.exists()
    assert scaffold.FIRST_MUTATION.exists()


def test_abandon_at_start_does_not_require_live_release_to_match(host: dict) -> None:
    """After a rolled-back install the live release is the PRIOR one while the
    journal names the release that was attempted. At stage start nothing has
    been installed under this journal, so that mismatch is expected, not a
    reason to refuse. Before this, abandon-after-rollback was impossible."""
    started = scaffold.utc_now() - dt.timedelta(minutes=5)
    _write(host["release"], {"release_id": "menhir-prod-0.2.0-13"})
    _write(scaffold.RELEASE_RUN, _journal(started, release_id="menhir-prod-0.2.0-14"))

    scaffold.abandon_maintenance(Path("contract"), Path("receipt"), "rolled back")
    assert not scaffold.RELEASE_RUN.exists()


def test_abandon_past_start_still_requires_live_release_to_match(host: dict) -> None:
    started = scaffold.utc_now() - dt.timedelta(minutes=5)
    _write(host["release"], {"release_id": "menhir-prod-0.2.0-13"})
    _write(scaffold.RELEASE_RUN, _journal(started, release_id="menhir-prod-0.2.0-14", stage="staged"))

    with pytest.raises(scaffold.ScaffoldError, match="not bound to the live release"):
        scaffold.abandon_maintenance(Path("contract"), Path("receipt"), "attempt")


def test_completed_maintenance_archives_its_markers_with_the_journal(host: dict) -> None:
    """Completion previously archived only the journal. Every marker the cycle
    wrote stayed behind: candidate state, selection, fences and first-mutation.
    They go with the journal now, so the next cycle starts from nothing."""
    started = scaffold.utc_now() - dt.timedelta(hours=1)
    _write(scaffold.RELEASE_RUN, _journal(started, release_id="menhir-prod-0.2.0-14", stage="complete"))
    for name in scaffold.MAINTENANCE_MARKERS:
        _write(host["status"] / name, "x\n")

    scaffold._archive_completed_maintenance()

    assert not scaffold.RELEASE_RUN.exists()
    for name in scaffold.MAINTENANCE_MARKERS:
        assert not (host["status"] / name).exists(), name
    history = list(scaffold.MAINTENANCE_HISTORY.iterdir())
    journals = [p for p in history if p.suffix == ".json"]
    marker_dirs = [p for p in history if p.name.endswith(".markers")]
    assert len(journals) == 1 and len(marker_dirs) == 1
    assert {p.name for p in marker_dirs[0].iterdir()} == set(scaffold.MAINTENANCE_MARKERS)


def _bound(started: dt.datetime, release_sha: str) -> dict:
    return {
        "release_id": "menhir-prod-0.2.0-14", "release_manifest_sha256": release_sha,
        "runner_sha256": HEX, "approval_sha256": HEX, "promotion_attempt_id": ATTEMPT,
        "approved_utc": scaffold.iso(started), "promotion_started_utc": scaffold.iso(started),
    }


def test_artifact_only_completion_is_proven_not_declared(host: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """A release that ships the prior image unchanged installs its artifacts
    and stops. It closes by proof: the live authority is this maintenance's,
    the verifier passes, the runtime carries that authority's images, no
    candidate exists, the endpoint is ready."""
    started = scaffold.utc_now() - dt.timedelta(minutes=5)
    _write(host["release"], {"release_id": "menhir-prod-0.2.0-14"})
    release_sha = scaffold.sha256_file(host["release"])
    journal = _journal(started, release_id="menhir-prod-0.2.0-14")
    journal["release_manifest_sha256"] = release_sha
    _write(scaffold.RELEASE_RUN, journal)
    calls: list[list[str]] = []
    monkeypatch.setattr(scaffold.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or type("R", (), {"returncode": 0})())

    state = scaffold.complete_maintenance(_bound(started, release_sha), artifact_only=True)

    assert state["stage"] == "complete" and state["completed_utc"] is not None
    assert json.loads(scaffold.RELEASE_RUN.read_text())["stage"] == "complete"
    assert [str(scaffold.VERIFY_ARTIFACTS)] in calls


def test_artifact_only_refuses_when_live_authority_is_not_this_release(host: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    started = scaffold.utc_now() - dt.timedelta(minutes=5)
    _write(host["release"], {"release_id": "menhir-prod-0.2.0-13"})
    _write(scaffold.RELEASE_RUN, _journal(started, release_id="menhir-prod-0.2.0-14"))
    with pytest.raises(scaffold.ScaffoldError, match="not this maintenance's release"):
        scaffold.complete_maintenance(_bound(started, HEX), artifact_only=True)
    assert json.loads(scaffold.RELEASE_RUN.read_text())["stage"] == "start"


def test_artifact_only_refuses_when_verifier_fails(host: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    started = scaffold.utc_now() - dt.timedelta(minutes=5)
    _write(host["release"], {"release_id": "menhir-prod-0.2.0-14"})
    release_sha = scaffold.sha256_file(host["release"])
    journal = _journal(started, release_id="menhir-prod-0.2.0-14")
    journal["release_manifest_sha256"] = release_sha
    _write(scaffold.RELEASE_RUN, journal)
    monkeypatch.setattr(scaffold.subprocess, "run",
                        lambda cmd, **k: type("R", (), {"returncode": 1})())
    with pytest.raises(scaffold.ScaffoldError, match="do not verify"):
        scaffold.complete_maintenance(_bound(started, release_sha), artifact_only=True)


def test_artifact_only_refuses_when_a_candidate_exists(host: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    started = scaffold.utc_now() - dt.timedelta(minutes=5)
    _write(host["release"], {"release_id": "menhir-prod-0.2.0-14"})
    release_sha = scaffold.sha256_file(host["release"])
    journal = _journal(started, release_id="menhir-prod-0.2.0-14")
    journal["release_manifest_sha256"] = release_sha
    _write(scaffold.RELEASE_RUN, journal)
    monkeypatch.setattr(scaffold.subprocess, "run", lambda cmd, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(scaffold, "inspect_runtime", lambda contract: (True, ["menhir-candidate-app"]))
    with pytest.raises(scaffold.ScaffoldError, match="candidate containers exist"):
        scaffold.complete_maintenance(_bound(started, release_sha), artifact_only=True)


def test_plain_complete_still_requires_acceptance(host: dict) -> None:
    started = scaffold.utc_now() - dt.timedelta(minutes=5)
    _write(scaffold.RELEASE_RUN, _journal(started, release_id="menhir-prod-0.2.0-14"))
    with pytest.raises(scaffold.ScaffoldError, match="before acceptance"):
        scaffold.complete_maintenance(_bound(started, HEX))
