from __future__ import annotations

import json
from pathlib import Path

import pytest

from deploy.lib.restore_authority_txn import apply, begin, commit, rollback, validate_anchor


def _dir(path: Path, content: str) -> Path:
    path.mkdir()
    (path / "authority.db").write_text(content)
    return path


def _paths(tmp_path: Path):
    txn = "restore-generation.Abc-20260827T010203Z"
    target = _dir(tmp_path / "oauth", "old")
    stage = _dir(tmp_path / f".menhir-restore-stage-{txn}-oauth", "new")
    journal = tmp_path / "journal.json"
    entry = f"oauth={target}={stage}"
    return txn, target, stage, journal, entry


def test_apply_commit_and_late_rollback_preserve_both_authorities(tmp_path: Path):
    txn, target, stage, journal, entry = _paths(tmp_path)
    anchors = tmp_path / "anchors"
    begin(journal, txn, "generation.Old", "generation.Abc", [entry])
    apply(journal)
    assert (target / "authority.db").read_text() == "new"
    anchor = commit(journal, anchors)
    assert not journal.exists()
    assert anchor.is_file()
    validate_anchor(anchor, "generation.Abc")

    # Runtime mutation of the restored authority does not weaken the immutable
    # prior anchor; rollback preserves that mutated failed restore separately.
    (target / "runtime-write").write_text("after-start")
    rollback_result = anchors / f"{txn}.rollback.json"
    rollback(anchor, rollback_result)
    assert (target / "authority.db").read_text() == "old"
    assert json.loads(anchor.read_text())["phase"] == "applied"
    assert json.loads(rollback_result.read_text())["phase"] == "rolled-back"
    failed = tmp_path / f".menhir-failed-restore-{txn}-oauth"
    assert (failed / "runtime-write").read_text() == "after-start"


def test_validate_anchor_rehashes_retained_prior_authority(tmp_path: Path):
    txn, _target, _stage, journal, entry = _paths(tmp_path)
    begin(journal, txn, "generation.Old", "generation.Abc", [entry])
    apply(journal)
    anchor = commit(journal, tmp_path / "anchors")
    previous = Path(json.loads(anchor.read_text())["entries"][0]["previous"])
    (previous / "authority.db").write_text("tampered after commit")

    with pytest.raises(ValueError, match="retained prior authority"):
        validate_anchor(anchor, "generation.Abc")


def test_apply_recovers_after_first_rename(tmp_path: Path):
    txn, target, stage, journal, entry = _paths(tmp_path)
    begin(journal, txn, "generation.Old", "generation.Abc", [entry])
    state = json.loads(journal.read_text())
    previous = Path(state["entries"][0]["previous"])
    target.replace(previous)  # SIGKILL after parking old authority
    apply(journal)
    assert (target / "authority.db").read_text() == "new"
    assert (previous / "authority.db").read_text() == "old"


def test_partial_apply_can_roll_back(tmp_path: Path):
    txn, target, stage, journal, entry = _paths(tmp_path)
    begin(journal, txn, "generation.Old", "generation.Abc", [entry])
    state = json.loads(journal.read_text())
    previous = Path(state["entries"][0]["previous"])
    target.replace(previous)
    rollback(journal)
    assert (target / "authority.db").read_text() == "old"
    assert stage.exists()


def test_begin_refuses_non_sibling_stage(tmp_path: Path):
    txn = "restore-generation.Abc-20260827T010203Z"
    target = _dir(tmp_path / "target", "old")
    other = tmp_path / "other"
    other.mkdir()
    stage = _dir(other / f".menhir-restore-stage-{txn}-oauth", "new")
    with pytest.raises(ValueError, match="siblings"):
        begin(
            tmp_path / "journal.json", txn, "generation.Old", "generation.Abc",
            [f"oauth={target}={stage}"],
        )


def test_commit_replay_accepts_identical_anchor_and_clears_journal(tmp_path: Path):
    txn, _target, _stage, journal, entry = _paths(tmp_path)
    anchors = tmp_path / "anchors"
    begin(journal, txn, "generation.Old", "generation.Abc", [entry])
    apply(journal)
    anchor = commit(journal, anchors)

    # Model SIGKILL after the durable anchor replacement but before journal unlink.
    journal.write_bytes(anchor.read_bytes())
    assert commit(journal, anchors) == anchor
    assert not journal.exists()


def test_commit_replay_rejects_divergent_existing_anchor(tmp_path: Path):
    txn, _target, _stage, journal, entry = _paths(tmp_path)
    anchors = tmp_path / "anchors"
    begin(journal, txn, "generation.Old", "generation.Abc", [entry])
    apply(journal)
    anchors.mkdir()
    anchor = anchors / f"{txn}.json"
    value = json.loads(journal.read_text())
    value["target_generation"] = "generation.Other"
    anchor.write_text(json.dumps(value))

    with pytest.raises(ValueError, match="different content"):
        commit(journal, anchors)
    assert journal.exists()


@pytest.mark.parametrize("mutation", ["missing", "changed"])
def test_commit_validates_retained_prior_authority(tmp_path: Path, mutation: str):
    txn, _target, _stage, journal, entry = _paths(tmp_path)
    begin(journal, txn, "generation.Old", "generation.Abc", [entry])
    apply(journal)
    previous = Path(json.loads(journal.read_text())["entries"][0]["previous"])
    if mutation == "missing":
        (previous / "authority.db").unlink()
        previous.rmdir()
    else:
        (previous / "authority.db").write_text("tampered")

    with pytest.raises(ValueError, match="retained prior authority"):
        commit(journal, tmp_path / "anchors")
    assert journal.exists()
