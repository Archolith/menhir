from __future__ import annotations

import json
from pathlib import Path

from deploy.lib.backup_cleanup_txn import begin, complete


def _pending(path: Path, generation: str) -> None:
    path.write_text(json.dumps({"generation": generation, "plaintext_removed": False}) + "\n")


def test_cleanup_transaction_removes_plaintext_and_finalizes_receipt(tmp_path: Path):
    generations = tmp_path / "generations"
    cleanup = tmp_path / "status" / "plaintext-cleanup"
    generations.mkdir()
    generation = generations / "generation.Abc123"
    generation.mkdir()
    (generation / "secret").write_text("sensitive")
    receipt = tmp_path / "status" / "receipt.json"
    receipt.parent.mkdir()
    receipt_root = tmp_path / "status" / "receipts"
    _pending(receipt, generation.name)
    journal = tmp_path / "status" / "journal.json"

    begin(journal, receipt, generation, generations, cleanup, receipt_root)
    complete(journal, receipt, generations, cleanup, receipt_root)

    assert not generation.exists()
    assert not (cleanup / generation.name).exists()
    assert not journal.exists()
    assert json.loads(receipt.read_text())["plaintext_removed"] is True
    assert (receipt_root / f"{generation.name}.json").read_bytes() == receipt.read_bytes()


def test_cleanup_transaction_resumes_after_atomic_park(tmp_path: Path):
    generations = tmp_path / "generations"
    cleanup = tmp_path / "status" / "plaintext-cleanup"
    generations.mkdir()
    generation = generations / "generation.Resume1"
    generation.mkdir()
    (generation / "authority").write_text("data")
    receipt = tmp_path / "status" / "receipt.json"
    receipt.parent.mkdir()
    receipt_root = tmp_path / "status" / "receipts"
    _pending(receipt, generation.name)
    journal = tmp_path / "status" / "journal.json"
    begin(journal, receipt, generation, generations, cleanup, receipt_root)

    parked = cleanup / generation.name
    generation.replace(parked)  # simulate SIGKILL after the durable rename
    complete(journal, receipt, generations, cleanup, receipt_root)

    assert not parked.exists()
    assert json.loads(receipt.read_text())["plaintext_removed"] is True


def test_cleanup_transaction_refuses_changed_pending_receipt(tmp_path: Path):
    generations = tmp_path / "generations"
    cleanup = tmp_path / "status" / "plaintext-cleanup"
    generations.mkdir()
    generation = generations / "generation.Refuse1"
    generation.mkdir()
    receipt = tmp_path / "status" / "receipt.json"
    receipt.parent.mkdir()
    receipt_root = tmp_path / "status" / "receipts"
    _pending(receipt, generation.name)
    journal = tmp_path / "status" / "journal.json"
    begin(journal, receipt, generation, generations, cleanup, receipt_root)
    receipt.write_text(json.dumps({"generation": generation.name, "plaintext_removed": False, "tampered": True}))

    try:
        complete(journal, receipt, generations, cleanup, receipt_root)
    except ValueError as exc:
        assert "changed" in str(exc)
    else:
        raise AssertionError("changed pending receipt was accepted")
    assert generation.exists()
    assert journal.exists()
