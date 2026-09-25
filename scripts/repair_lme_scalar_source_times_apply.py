"""Backup-proof verification, audit writing, and apply for the LME source-time repair.

Extracted verbatim from ``repair_lme_scalar_source_times.py``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.neo4j import Neo4jRepository

from repair_lme_scalar_source_times_model import (
    REPAIR_SOURCE,
    RepairPlan,
    RepairRefusal,
    SourceTimeIndex,
    _fixture_hash,
)
from repair_lme_scalar_source_times_planning import build_repair_plan


def verify_backup_proof(path: Path) -> dict[str, Any]:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepairRefusal(f"cannot read backup proof {path}: {exc}") from exc
    if metadata.get("snapshot_type") != "neo4j-volume-tarball":
        raise RepairRefusal("backup proof is not a Neo4j volume snapshot")
    snapshot_name = str(metadata.get("file") or "")
    expected_sha = str(metadata.get("sha256") or "").lower()
    if not snapshot_name or len(expected_sha) != 64:
        raise RepairRefusal("backup proof is missing snapshot filename or SHA-256")
    snapshot_path = path.parent / snapshot_name
    if not snapshot_path.is_file():
        raise RepairRefusal(f"backup tarball does not exist: {snapshot_path}")
    actual_sha = _fixture_hash(snapshot_path)
    if actual_sha != expected_sha:
        raise RepairRefusal(
            f"backup tarball SHA-256 mismatch: expected {expected_sha}, got {actual_sha}"
        )
    return {
        "metadata_path": str(path.resolve()),
        "snapshot_path": str(snapshot_path.resolve()),
        "sha256": actual_sha,
    }


def _write_audit(path: Path, payload: dict[str, Any], *, create: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if create else "w"
    with path.open(mode, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def apply_repair(
    repository: Neo4jRepository,
    plan: RepairPlan,
    *,
    audit_path: Path,
    fixture_path: Path,
    source_times: SourceTimeIndex,
    namespace_prefix: str,
    backup: dict[str, Any],
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    audit: dict[str, Any] = {
        "schema_version": 1,
        "status": "prepared",
        "started_at": started_at.isoformat(),
        "fixture": {
            "path": str(fixture_path.resolve()),
            "sha256": source_times.fixture_sha256,
        },
        "namespace_prefix": namespace_prefix,
        "backup": backup,
        "plan": plan.summary(),
        "evidence_updates": list(plan.evidence_updates),
        "assertion_updates": list(plan.assertion_updates),
        "rebuilds": [],
    }
    try:
        _write_audit(audit_path, audit, create=True)
    except FileExistsError as exc:
        raise RepairRefusal(f"audit output already exists: {audit_path}") from exc

    repair_id = hashlib.sha256(
        f"{source_times.fixture_sha256}|{namespace_prefix}|{started_at.isoformat()}".encode()
    ).hexdigest()
    rows = repository.execute(
        """
        CALL () {
          UNWIND $evidence AS row
          MATCH (t:TurnEvidence {turn_id: row.turn_id})
          SET t.occurred_at = datetime(row.target),
              t.source_time_repair_id = $repair_id
          RETURN count(t) AS evidence_updated
        }
        CALL () {
          UNWIND $assertions AS row
          MATCH (a:TypedAssertion {assertion_id: row.assertion_id})
          SET a.source_time_repair_original_valid_at =
                coalesce(a.source_time_repair_original_valid_at, a.valid_at),
              a.valid_at = datetime(row.target),
              a.source_time_repair_id = $repair_id
          RETURN count(a) AS assertions_updated
        }
        RETURN evidence_updated, assertions_updated
        """,
        {
            "evidence": list(plan.evidence_updates),
            "assertions": list(plan.assertion_updates),
            "repair_id": repair_id,
        },
    )
    applied = rows[0] if rows else {}
    if int(applied.get("evidence_updated") or 0) != len(plan.evidence_updates):
        raise RepairRefusal("evidence update count changed between planning and apply")
    if int(applied.get("assertions_updated") or 0) != len(plan.assertion_updates):
        raise RepairRefusal("assertion update count changed between planning and apply")

    audit["status"] = "timestamps_applied"
    audit["repair_id"] = repair_id
    audit["applied"] = {
        "evidence_updated": int(applied.get("evidence_updated") or 0),
        "assertions_updated": int(applied.get("assertions_updated") or 0),
    }
    _write_audit(audit_path, audit)

    adapter = MemoryGraphAdapter(repository)
    scalar_service = adapter.scalar_state_service()
    evaluation_time = datetime.now(timezone.utc)
    for subject_uuid, namespace in plan.rebuild_targets:
        result = scalar_service.rebuild_scalar_state(
            subject_uuid,
            namespace=namespace,
            source=REPAIR_SOURCE,
            as_of=evaluation_time,
        )
        rebuild = {
            "subject_uuid": subject_uuid,
            "namespace": namespace,
            "written": int(result.get("written") or 0),
            "retired": int(result.get("retired") or 0),
            "stale_skipped": result.get("stale_skipped") or [],
        }
        audit["rebuilds"].append(rebuild)
        audit["status"] = "rebuilding_views"
        _write_audit(audit_path, audit)
        if rebuild["stale_skipped"]:
            raise RepairRefusal(
                f"projection rebuild stale-skipped {subject_uuid} in {namespace}"
            )

    verification = build_repair_plan(repository, source_times, namespace_prefix)
    if verification.evidence_updates or verification.assertion_updates:
        raise RepairRefusal(
            "post-repair verification is not idempotent: "
            f"{len(verification.evidence_updates)} evidence and "
            f"{len(verification.assertion_updates)} assertions still need repair"
        )

    audit["status"] = "complete"
    audit["completed_at"] = datetime.now(timezone.utc).isoformat()
    audit["verification"] = verification.summary()
    _write_audit(audit_path, audit)
    return audit
