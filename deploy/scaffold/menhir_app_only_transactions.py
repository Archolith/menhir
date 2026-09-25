"""Deployment transaction stage journal and live authority restore."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from menhir_app_only_constants import ACTIVE, LAST, LIVE_ENV, LIVE_RELEASE
from menhir_app_only_core import atomic_bytes, atomic_json, now_iso, require_root_file


def write_stage(transaction: dict[str, Any], stage: str, **extra: Any) -> None:
    transaction.update(extra)
    transaction["stage"] = stage
    transaction["updated_utc"] = now_iso()
    atomic_json(ACTIVE, transaction)


def finalize_transaction(transaction: dict[str, Any]) -> None:
    atomic_json(LAST, transaction)
    try:
        ACTIVE.unlink()
    except FileNotFoundError:
        pass


def restore_authority(transaction: dict[str, Any], candidate: bool) -> None:
    tx = Path(transaction["transaction_root"])
    release_name = "candidate-release.json" if candidate else "prior-release.json"
    env_name = "candidate-production.env" if candidate else "prior-production.env"
    require_root_file(tx / release_name, release_name)
    require_root_file(tx / env_name, env_name)
    atomic_bytes(LIVE_ENV, (tx / env_name).read_bytes(), 0o600)
    atomic_bytes(LIVE_RELEASE, (tx / release_name).read_bytes(), 0o400)
