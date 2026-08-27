#!/usr/bin/env python3
"""Durable plaintext-cleanup transaction for an uploaded backup generation.

The off-host object is already verified before this helper is used.  A root-owned
journal binds the pending receipt and the two fixed local paths.  Re-running
``complete`` after SIGKILL is idempotent: plaintext is either still at the
generation path, atomically parked at the cleanup path, or already absent.  The
receipt is finalized only after both plaintext locations are absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    # Directory fsync is the Linux production durability primitive. Windows
    # does not permit opening directories this way; unit tests still exercise
    # the state machine and atomic replacements there.
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temporary_path, mode)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(temporary_path, 0, 0)
        os.replace(temporary_path, path)
        _fsync_dir(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _direct_child(path: Path, parent: Path, label: str) -> None:
    if not path.is_absolute() or not parent.is_absolute():
        raise ValueError(f"{label} paths must be absolute")
    if path.parent.resolve(strict=True) != parent.resolve(strict=True):
        raise ValueError(f"{label} must be a direct child of its fixed root")
    if not path.name.startswith("generation.") or not path.name[11:].isalnum():
        raise ValueError(f"{label} has an invalid generation name")


def begin(journal: Path, receipt: Path, generation: Path,
          generations_root: Path, cleanup_root: Path, receipt_root: Path) -> None:
    _direct_child(generation, generations_root, "generation")
    cleanup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(cleanup_root, 0o700)
    if not receipt_root.is_absolute() or receipt_root.is_symlink():
        raise ValueError("receipt root must be an absolute non-symlink path")
    receipt_root.mkdir(parents=True, exist_ok=True)
    os.chmod(receipt_root, 0o700)
    cleanup = cleanup_root / generation.name
    if cleanup.exists() or cleanup.is_symlink():
        raise ValueError("fixed cleanup path is already occupied")
    with receipt.open("r", encoding="ascii") as handle:
        value = json.load(handle)
    if value.get("plaintext_removed") is not False:
        raise ValueError("cleanup transaction requires a pending receipt")
    state = {
        "schema": 1,
        "phase": "cleanup-required",
        "generation": generation.name,
        "generation_path": str(generation),
        "generations_root": str(generations_root),
        "cleanup_path": str(cleanup),
        "cleanup_root": str(cleanup_root),
        "receipt_path": str(receipt),
        "receipt_root": str(receipt_root),
        "pending_receipt_sha256": _sha256(receipt),
    }
    _atomic_json(journal, state, 0o400)


def complete(journal: Path, expected_receipt: Path,
             expected_generations_root: Path, expected_cleanup_root: Path,
             expected_receipt_root: Path) -> None:
    if journal.is_symlink() or not journal.is_file():
        raise ValueError("cleanup journal must be a regular non-symlink file")
    mode = stat.S_IMODE(journal.stat().st_mode)
    if os.name == "posix" and mode & 0o022:
        raise ValueError("cleanup journal must not be group/other writable")
    with journal.open("r", encoding="ascii") as handle:
        state = json.load(handle)
    required = {
        "schema", "phase", "generation", "generation_path", "generations_root",
        "cleanup_path", "cleanup_root", "receipt_path", "pending_receipt_sha256",
        "receipt_root",
    }
    if set(state) != required or state["schema"] != 1 or state["phase"] != "cleanup-required":
        raise ValueError("cleanup journal schema/phase is invalid")

    generation = Path(state["generation_path"])
    generations_root = Path(state["generations_root"])
    cleanup = Path(state["cleanup_path"])
    cleanup_root = Path(state["cleanup_root"])
    receipt = Path(state["receipt_path"])
    receipt_root = Path(state["receipt_root"])
    if receipt != expected_receipt or generations_root != expected_generations_root \
            or cleanup_root != expected_cleanup_root or receipt_root != expected_receipt_root:
        raise ValueError("cleanup journal does not bind the configured fixed roots")
    _direct_child(generation, generations_root, "generation")
    _direct_child(cleanup, cleanup_root, "cleanup")
    if generation.name != state["generation"] or cleanup.name != generation.name:
        raise ValueError("cleanup journal generation binding is invalid")
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError("pending receipt is missing or unsafe")
    if not receipt_root.is_absolute() or receipt_root.is_symlink() or not receipt_root.is_dir():
        raise ValueError("receipt root is missing or unsafe")

    with receipt.open("r", encoding="ascii") as handle:
        receipt_value = json.load(handle)
    if receipt_value.get("generation") != state["generation"]:
        raise ValueError("pending receipt generation does not match cleanup journal")
    already_final = receipt_value.get("plaintext_removed") is True
    if not already_final and _sha256(receipt) != state["pending_receipt_sha256"]:
        raise ValueError("pending receipt changed after cleanup journal was committed")

    if generation.exists() and cleanup.exists():
        raise ValueError("both generation and cleanup paths exist; manual reconciliation required")
    if generation.is_symlink() or cleanup.is_symlink():
        raise ValueError("cleanup transaction refuses symlink paths")
    if generation.exists():
        os.replace(generation, cleanup)
        _fsync_dir(generations_root)
        _fsync_dir(cleanup_root)
    if cleanup.exists():
        shutil.rmtree(cleanup)
        _fsync_dir(cleanup_root)

    if not already_final:
        receipt_value["plaintext_removed"] = True
        _atomic_json(receipt, receipt_value, 0o400)

    # The immutable, per-generation receipt is part of this transaction. A
    # crash after its atomic link but before journal removal is idempotent:
    # recovery accepts only an exact byte-for-byte copy of the finalized
    # singleton receipt and never overwrites an existing generation record.
    generation_receipt = receipt_root / f'{state["generation"]}.json'
    if generation_receipt.is_symlink():
        raise ValueError("per-generation receipt must not be a symlink")
    if generation_receipt.exists():
        if not generation_receipt.is_file() or _sha256(generation_receipt) != _sha256(receipt):
            raise ValueError("per-generation receipt already exists with different content")
    else:
        fd, temporary = tempfile.mkstemp(prefix=f".{generation_receipt.name}.", dir=receipt_root)
        temporary_path = Path(temporary)
        try:
            with receipt.open("rb") as source, os.fdopen(fd, "wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            if os.name == "posix":
                os.chmod(temporary_path, 0o400)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(temporary_path, 0, 0)
            os.link(temporary_path, generation_receipt)
            _fsync_dir(receipt_root)
        finally:
            temporary_path.unlink(missing_ok=True)
    journal.unlink()
    _fsync_dir(journal.parent)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise ValueError("usage: backup_cleanup_txn.py <begin|complete> ...")
    if argv[1] == "begin" and len(argv) == 8:
        begin(*(Path(value) for value in argv[2:8]))
    elif argv[1] == "complete" and len(argv) == 7:
        complete(*(Path(value) for value in argv[2:7]))
    else:
        raise ValueError("usage: backup_cleanup_txn.py <begin|complete> ...")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"backup cleanup transaction refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
