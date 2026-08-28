#!/usr/bin/env python3
"""Crash-recoverable directory-swap transaction for Menhir restores.

Incoming authority is prepared in sibling directories before this helper is
called.  The journal records cryptographic tree digests before any rename.
Each rename is same-filesystem and directory-fsynced; ``apply`` and ``rollback``
infer progress from the three bound paths, so either operation is safe to retry
after SIGKILL.  A successful transaction is committed as an immutable rollback
anchor while the displaced production directories remain untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

_ID = re.compile(r"restore-[A-Za-z0-9.-]+")
_LABEL = re.compile(r"[a-z0-9-]+")


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict, mode: int = 0o400) -> None:
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


def _tree_digest(root: Path) -> str:
    if root.is_symlink():
        raise ValueError(f"authority path is a symlink: {root}")
    if not root.exists():
        return "absent"
    if not root.is_dir():
        raise ValueError(f"authority path is not a directory: {root}")
    digest = hashlib.sha256()
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs.sort()
        files.sort()
        for name in [".", *dirs, *files]:
            path = current_path if name == "." else current_path / name
            if path.is_symlink():
                raise ValueError(f"authority tree contains a symlink: {path}")
            info = path.stat(follow_symlinks=False)
            relative = "." if path == root else path.relative_to(root).as_posix()
            kind = "d" if stat.S_ISDIR(info.st_mode) else "f" if stat.S_ISREG(info.st_mode) else "x"
            if kind == "x":
                raise ValueError(f"authority tree contains a special entry: {path}")
            digest.update(
                f"{kind}\0{relative}\0{stat.S_IMODE(info.st_mode):04o}\0{info.st_uid}\0{info.st_gid}\0".encode()
            )
            if kind == "f":
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def _load(journal: Path) -> dict:
    if journal.is_symlink() or not journal.is_file():
        raise ValueError("restore journal must be a regular non-symlink file")
    if os.name == "posix" and stat.S_IMODE(journal.stat().st_mode) & 0o022:
        raise ValueError("restore journal must not be group/other writable")
    with journal.open("r", encoding="ascii") as handle:
        value = json.load(handle)
    if set(value) != {
        "schema", "phase", "transaction_id", "prior_generation", "target_generation", "entries"
    }:
        raise ValueError("restore journal schema is invalid")
    if value["schema"] != 1 or value["phase"] not in {"prepared", "applied", "rolled-back"}:
        raise ValueError("restore journal phase is invalid")
    if not _ID.fullmatch(value["transaction_id"]):
        raise ValueError("restore transaction id is invalid")
    if not re.fullmatch(r"generation\.[A-Za-z0-9]+", value["target_generation"]):
        raise ValueError("restore target generation is invalid")
    if value["prior_generation"] and not re.fullmatch(
            r"generation\.[A-Za-z0-9]+", value["prior_generation"]):
        raise ValueError("restore prior generation is invalid")
    if not isinstance(value["entries"], list) or not value["entries"]:
        raise ValueError("restore journal entries are invalid")
    expected_keys = {
        "label", "target", "stage", "previous", "failed", "had_previous",
        "previous_digest", "incoming_digest",
    }
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != expected_keys:
            raise ValueError("restore journal entry schema is invalid")
        label = entry["label"]
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise ValueError("restore journal entry label is invalid")
        target = Path(entry["target"])
        stage = Path(entry["stage"])
        previous = Path(entry["previous"])
        failed = Path(entry["failed"])
        if not all(path.is_absolute() for path in (target, stage, previous, failed)):
            raise ValueError("restore journal paths must be absolute")
        if len({target.parent, stage.parent, previous.parent, failed.parent}) != 1:
            raise ValueError("restore swap paths must be siblings")
        txn = value["transaction_id"]
        if stage.name != f".menhir-restore-stage-{txn}-{label}" \
                or previous.name != f".menhir-pre-restore-{txn}-{label}" \
                or failed.name != f".menhir-failed-restore-{txn}-{label}":
            raise ValueError("restore journal swap path naming is invalid")
        if not isinstance(entry["had_previous"], bool):
            raise ValueError("restore journal had_previous is invalid")
        for key in ("previous_digest", "incoming_digest"):
            if entry[key] != "absent" and not re.fullmatch(r"[0-9a-f]{64}", entry[key]):
                raise ValueError(f"restore journal {key} is invalid")
    return value


def begin(journal: Path, transaction_id: str, prior_generation: str, target_generation: str,
          raw_entries: list[str]) -> None:
    if journal.exists() or journal.is_symlink():
        raise ValueError("an unfinished restore journal already exists")
    if not journal.is_absolute() or not _ID.fullmatch(transaction_id):
        raise ValueError("restore journal path or transaction id is invalid")
    if not re.fullmatch(r"generation\.[A-Za-z0-9]+", target_generation):
        raise ValueError("restore target generation is invalid")
    if prior_generation and not re.fullmatch(r"generation\.[A-Za-z0-9]+", prior_generation):
        raise ValueError("restore prior generation is invalid")
    entries = []
    labels: set[str] = set()
    targets: set[Path] = set()
    for raw in raw_entries:
        try:
            label, target_raw, stage_raw = raw.split("=", 2)
        except ValueError as exc:
            raise ValueError("restore entry must be label=target=stage") from exc
        if not _LABEL.fullmatch(label) or label in labels:
            raise ValueError("restore entry label is invalid or duplicated")
        target, stage = Path(target_raw), Path(stage_raw)
        if not target.is_absolute() or not stage.is_absolute() or target.parent != stage.parent:
            raise ValueError("restore target and stage must be absolute siblings")
        if target in targets or stage.name != f".menhir-restore-stage-{transaction_id}-{label}":
            raise ValueError("restore target is duplicated or stage naming is invalid")
        previous = target.parent / f".menhir-pre-restore-{transaction_id}-{label}"
        failed = target.parent / f".menhir-failed-restore-{transaction_id}-{label}"
        if previous.exists() or previous.is_symlink() or failed.exists() or failed.is_symlink():
            raise ValueError("restore previous/failed path is already occupied")
        incoming_digest = _tree_digest(stage)
        if incoming_digest == "absent":
            raise ValueError("restore stage is missing")
        previous_digest = _tree_digest(target)
        entries.append({
            "label": label,
            "target": str(target),
            "stage": str(stage),
            "previous": str(previous),
            "failed": str(failed),
            "had_previous": previous_digest != "absent",
            "previous_digest": previous_digest,
            "incoming_digest": incoming_digest,
        })
        labels.add(label)
        targets.add(target)
    if not entries:
        raise ValueError("at least one restore entry is required")
    _atomic_json(journal, {
        "schema": 1,
        "phase": "prepared",
        "transaction_id": transaction_id,
        "prior_generation": prior_generation,
        "target_generation": target_generation,
        "entries": entries,
    })


def apply(journal: Path) -> None:
    value = _load(journal)
    if value["phase"] == "applied":
        return
    if value["phase"] != "prepared":
        raise ValueError("only a prepared restore can be applied")
    for entry in value["entries"]:
        target, stage, previous = (Path(entry[key]) for key in ("target", "stage", "previous"))
        if previous.exists():
            if _tree_digest(previous) != entry["previous_digest"]:
                raise ValueError("displaced authority digest mismatch")
        elif entry["had_previous"]:
            if _tree_digest(target) != entry["previous_digest"]:
                raise ValueError("current authority changed after journal commit")
            os.replace(target, previous)
            _fsync_dir(target.parent)
        if stage.exists():
            if _tree_digest(stage) != entry["incoming_digest"]:
                raise ValueError("incoming restore stage digest mismatch")
            if target.exists() or target.is_symlink():
                raise ValueError("restore target occupied during transaction")
            os.replace(stage, target)
            _fsync_dir(target.parent)
        if _tree_digest(target) != entry["incoming_digest"]:
            raise ValueError("restored authority digest mismatch")
    value["phase"] = "applied"
    _atomic_json(journal, value)


def rollback(journal: Path, result_path: Path | None = None) -> None:
    value = _load(journal)
    if value["phase"] == "rolled-back":
        return
    for entry in reversed(value["entries"]):
        target, stage, previous, failed = (
            Path(entry[key]) for key in ("target", "stage", "previous", "failed")
        )
        if previous.exists():
            if _tree_digest(previous) != entry["previous_digest"]:
                raise ValueError("pre-restore authority digest mismatch; refusing rollback")
            if target.exists():
                if failed.exists() or failed.is_symlink():
                    raise ValueError("failed-restore preservation path is occupied")
                os.replace(target, failed)
                _fsync_dir(target.parent)
            os.replace(previous, target)
            _fsync_dir(target.parent)
        elif not entry["had_previous"] and target.exists() and not stage.exists():
            if failed.exists() or failed.is_symlink():
                raise ValueError("failed-restore preservation path is occupied")
            os.replace(target, failed)
            _fsync_dir(target.parent)
        if entry["had_previous"] and _tree_digest(target) != entry["previous_digest"]:
            raise ValueError("rollback did not restore prior authority")
        if not entry["had_previous"] and target.exists():
            raise ValueError("rollback did not restore the initially absent target")
    value["phase"] = "rolled-back"
    if result_path is None:
        _atomic_json(journal, value)
    else:
        if not result_path.is_absolute() or result_path == journal or result_path.is_symlink():
            raise ValueError("rollback result path is invalid")
        if result_path.exists():
            existing = _load(result_path)
            if existing != value:
                raise ValueError("rollback result already exists with different content")
        else:
            _atomic_json(result_path, value)


def _validate_retained_prior_authority(value: dict) -> None:
    """Prove every rollback target still matches its journaled pre-restore state."""
    for entry in value["entries"]:
        previous = Path(entry["previous"])
        actual = _tree_digest(previous)
        expected = entry["previous_digest"] if entry["had_previous"] else "absent"
        if actual != expected:
            raise ValueError(
                f'retained prior authority mismatch for {entry["label"]}'
            )


def validate_anchor(anchor: Path, target_generation: str) -> None:
    if not re.fullmatch(r"generation\.[A-Za-z0-9]+", target_generation):
        raise ValueError("restore anchor target generation is invalid")
    value = _load(anchor)
    if value["phase"] != "applied" or value["target_generation"] != target_generation:
        raise ValueError("restore anchor does not bind the expected applied generation")
    _validate_retained_prior_authority(value)


def commit(journal: Path, anchor_root: Path) -> Path:
    value = _load(journal)
    if value["phase"] != "applied":
        raise ValueError("only an applied restore can be committed")
    if not anchor_root.is_absolute() or anchor_root.is_symlink():
        raise ValueError("restore anchor root must be an absolute non-symlink path")
    anchor_root.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(anchor_root, 0o700)
    anchor = anchor_root / f'{value["transaction_id"]}.json'
    _validate_retained_prior_authority(value)
    if anchor.exists() or anchor.is_symlink():
        if anchor.is_symlink() or not anchor.is_file():
            raise ValueError("restore rollback anchor is not a regular file")
        if _load(anchor) != value:
            raise ValueError("restore rollback anchor already exists with different content")
    else:
        _atomic_json(anchor, value)
    journal.unlink()
    _fsync_dir(journal.parent)
    return anchor


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise ValueError("usage: restore_authority_txn.py <begin|apply|rollback|commit|validate-anchor> ...")
    command = argv[1]
    if command == "begin" and len(argv) >= 7:
        begin(Path(argv[2]), argv[3], argv[4], argv[5], argv[6:])
    elif command == "apply" and len(argv) == 3:
        apply(Path(argv[2]))
    elif command == "rollback" and len(argv) in {3, 4}:
        rollback(Path(argv[2]), Path(argv[3]) if len(argv) == 4 else None)
    elif command == "commit" and len(argv) == 4:
        print(commit(Path(argv[2]), Path(argv[3])))
    elif command == "validate-anchor" and len(argv) == 4:
        validate_anchor(Path(argv[2]), argv[3])
    else:
        raise ValueError("usage: restore_authority_txn.py <begin|apply|rollback|commit|validate-anchor> ...")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"restore authority transaction refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
