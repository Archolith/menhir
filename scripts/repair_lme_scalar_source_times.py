#!/usr/bin/env python3
"""Repair scalar world time from a frozen LongMemEval fixture without re-extraction.

Historical LME imports written before TurnEvidence accepted ``occurred_at`` stamped both
``:TurnEvidence`` and ``:TypedAssertion.valid_at`` with server receive time. The frozen fixture
still contains the authoritative session date, and the importer namespace-qualifies each
``TurnEvidence.session_id`` deterministically, so the original world time is exactly recoverable.

This migration is dry-run by default. Apply requires:

* an explicit fixture SHA-256;
* an explicit namespace-prefix confirmation;
* a verified portable graph snapshot made before the write; and
* a new audit-output path.

It changes only ``TurnEvidence.occurred_at`` and ``TypedAssertion.valid_at`` before rebuilding
disposable ScalarStateView projections from the repaired assertion log. It never calls an LLM,
re-ingests an episode, or changes assertion identity.

Example:

    PYTHONPATH=src python scripts/repair_lme_scalar_source_times.py \
      --uri bolt://127.0.0.1:7794 \
      --fixture ../archolith-bench/fixtures/longmemeval/knowledge_update_subset.json \
      --prefix lme-

    PYTHONPATH=src python scripts/repair_lme_scalar_source_times.py \
      --uri bolt://127.0.0.1:7794 \
      --fixture ../archolith-bench/fixtures/longmemeval/knowledge_update_subset.json \
      --prefix lme- --confirm-prefix lme- \
      --expected-fixture-sha256 <sha256> \
      --backup-proof /path/to/graph-snapshot.json \
      --audit-out /path/to/scalar-source-time-repair.json \
      --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.neo4j import Neo4jRepository

DATE_FORMAT = "%Y/%m/%d (%a) %H:%M"
REPAIR_SOURCE = "scalar-source-time-repair"


class RepairRefusal(RuntimeError):
    """The graph/fixture combination is not safe enough to mutate."""


class QueryExecutor(Protocol):
    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class SourceTimeIndex:
    fixture_sha256: str
    session_times: dict[tuple[str, str], datetime]
    namespaces: frozenset[str]


@dataclass(frozen=True)
class RepairPlan:
    evidence_updates: tuple[dict[str, str | None], ...]
    assertion_updates: tuple[dict[str, str | None], ...]
    rebuild_targets: tuple[tuple[str, str], ...]
    evidence_total: int
    assertion_total: int
    evidence_already_correct: int
    assertions_already_correct: int

    def summary(self) -> dict[str, int]:
        return {
            "evidence_total": self.evidence_total,
            "evidence_updates": len(self.evidence_updates),
            "evidence_already_correct": self.evidence_already_correct,
            "assertion_total": self.assertion_total,
            "assertion_updates": len(self.assertion_updates),
            "assertions_already_correct": self.assertions_already_correct,
            "rebuild_targets": len(self.rebuild_targets),
        }


def _fixture_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_fixture_date(raw: object) -> datetime:
    try:
        parsed = datetime.strptime(str(raw), DATE_FORMAT)
    except (TypeError, ValueError) as exc:
        raise RepairRefusal(f"invalid LongMemEval haystack date: {raw!r}") from exc
    return parsed.replace(tzinfo=timezone.utc)


def load_source_time_index(fixture_path: Path, namespace_prefix: str) -> SourceTimeIndex:
    if not namespace_prefix.strip():
        raise RepairRefusal("namespace prefix must not be blank")
    try:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepairRefusal(f"cannot read fixture {fixture_path}: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise RepairRefusal("LongMemEval fixture must be a non-empty JSON array")

    session_times: dict[tuple[str, str], datetime] = {}
    namespaces: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise RepairRefusal("LongMemEval fixture rows must be JSON objects")
        question_id = str(item.get("question_id") or "").strip()
        if not question_id:
            raise RepairRefusal("LongMemEval fixture row is missing question_id")
        namespace = f"{namespace_prefix}{question_id}"
        namespaces.add(namespace)

        sessions = item.get("haystack_sessions") or item.get("sessions") or []
        dates = item.get("haystack_dates") or []
        raw_session_ids = item.get("haystack_session_ids") or []
        if not isinstance(sessions, list) or not isinstance(dates, list):
            raise RepairRefusal(f"{question_id}: sessions and haystack_dates must be arrays")
        if len(sessions) != len(dates):
            raise RepairRefusal(
                f"{question_id}: {len(sessions)} sessions but {len(dates)} haystack dates"
            )
        if raw_session_ids and len(raw_session_ids) != len(sessions):
            raise RepairRefusal(
                f"{question_id}: {len(sessions)} sessions but {len(raw_session_ids)} session ids"
            )

        for index, raw_date in enumerate(dates):
            raw_session_id = raw_session_ids[index] if raw_session_ids else None
            session_id = (
                f"{namespace}-{raw_session_id}"
                if raw_session_id
                else f"{namespace}-s{index}"
            )
            key = (namespace, session_id)
            target = _parse_fixture_date(raw_date)
            previous = session_times.get(key)
            if previous is not None and previous != target:
                raise RepairRefusal(f"conflicting fixture dates for session {session_id}")
            session_times[key] = target

    if not session_times:
        raise RepairRefusal("fixture contains no session timestamps")
    return SourceTimeIndex(
        fixture_sha256=_fixture_hash(fixture_path),
        session_times=session_times,
        namespaces=frozenset(namespaces),
    )


def _as_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise RepairRefusal(f"stored timestamp is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _same_instant(left: object, right: datetime) -> bool:
    parsed = _as_datetime(left)
    return parsed is not None and parsed == right.astimezone(timezone.utc)


def _iso(value: object) -> str | None:
    parsed = _as_datetime(value)
    return parsed.isoformat() if parsed is not None else None


def build_repair_plan(
    executor: QueryExecutor,
    source_times: SourceTimeIndex,
    namespace_prefix: str,
) -> RepairPlan:
    evidence_rows = executor.execute(
        """
        MATCH (t:TurnEvidence)
        WHERE t.namespace STARTS WITH $prefix
        RETURN t.turn_id AS turn_id,
               t.namespace AS namespace,
               t.session_id AS session_id,
               t.occurred_at AS occurred_at
        ORDER BY t.turn_id
        """,
        {"prefix": namespace_prefix},
    )
    if not evidence_rows:
        raise RepairRefusal(f"no TurnEvidence rows found under prefix {namespace_prefix!r}")

    target_by_turn: dict[str, datetime] = {}
    evidence_updates: list[dict[str, str | None]] = []
    evidence_already_correct = 0
    unmapped_evidence: list[str] = []
    conflicting_existing: list[str] = []
    for row in evidence_rows:
        turn_id = str(row.get("turn_id") or "")
        namespace = str(row.get("namespace") or "")
        session_id = str(row.get("session_id") or "")
        target = source_times.session_times.get((namespace, session_id))
        if not turn_id or target is None:
            unmapped_evidence.append(
                f"turn_id={turn_id or '<blank>'} namespace={namespace!r} session_id={session_id!r}"
            )
            continue
        target_by_turn[turn_id] = target
        stored = row.get("occurred_at")
        if _same_instant(stored, target):
            evidence_already_correct += 1
            continue
        if stored is not None:
            conflicting_existing.append(
                f"turn_id={turn_id} stored={_iso(stored)} fixture={target.isoformat()}"
            )
            continue
        evidence_updates.append(
            {
                "turn_id": turn_id,
                "old_occurred_at": None,
                "target": target.isoformat(),
            }
        )
    if unmapped_evidence:
        preview = "; ".join(unmapped_evidence[:5])
        raise RepairRefusal(
            f"{len(unmapped_evidence)} TurnEvidence rows do not map exactly to the fixture: {preview}"
        )
    if conflicting_existing:
        preview = "; ".join(conflicting_existing[:5])
        raise RepairRefusal(
            f"{len(conflicting_existing)} TurnEvidence rows already carry a conflicting source time: "
            f"{preview}"
        )

    assertion_rows = executor.execute(
        """
        MATCH (a:TypedAssertion)
        WHERE a.namespace STARTS WITH $prefix
        OPTIONAL MATCH (t:TurnEvidence)-[:FOUNDS]->(a)
        RETURN a.assertion_id AS assertion_id,
               a.namespace AS namespace,
               a.episode_uuid AS episode_uuid,
               a.subject_uuid AS subject_uuid,
               a.binding_pending AS binding_pending,
               a.valid_at AS valid_at,
               collect(DISTINCT t.turn_id) AS founded_turn_ids
        ORDER BY a.assertion_id
        """,
        {"prefix": namespace_prefix},
    )

    assertion_updates: list[dict[str, str | None]] = []
    assertions_already_correct = 0
    rebuild_targets: set[tuple[str, str]] = set()
    unmapped_assertions: list[str] = []
    ambiguous_assertions: list[str] = []
    for row in assertion_rows:
        assertion_id = str(row.get("assertion_id") or "")
        episode_uuid = str(row.get("episode_uuid") or "")
        founded_turn_ids = {
            str(turn_id)
            for turn_id in (row.get("founded_turn_ids") or [])
            if str(turn_id or "")
        }
        candidate_turn_ids = founded_turn_ids or ({episode_uuid} if episode_uuid else set())
        targets = {
            target_by_turn[turn_id]
            for turn_id in candidate_turn_ids
            if turn_id in target_by_turn
        }
        missing_turn_ids = sorted(candidate_turn_ids - target_by_turn.keys())
        if not assertion_id or not targets or missing_turn_ids:
            unmapped_assertions.append(
                f"assertion_id={assertion_id or '<blank>'} "
                f"turn_ids={sorted(candidate_turn_ids)!r} missing={missing_turn_ids!r}"
            )
            continue
        if len(targets) != 1:
            ambiguous_assertions.append(
                f"assertion_id={assertion_id} targets={sorted(t.isoformat() for t in targets)!r}"
            )
            continue
        target = next(iter(targets))
        if _same_instant(row.get("valid_at"), target):
            assertions_already_correct += 1
        else:
            assertion_updates.append(
                {
                    "assertion_id": assertion_id,
                    "old_valid_at": _iso(row.get("valid_at")),
                    "target": target.isoformat(),
                }
            )
        subject_uuid = str(row.get("subject_uuid") or "")
        namespace = str(row.get("namespace") or "")
        binding_pending = bool(row.get("binding_pending"))
        if subject_uuid and namespace and not binding_pending and not subject_uuid.startswith("unbound:"):
            rebuild_targets.add((subject_uuid, namespace))

    if unmapped_assertions:
        preview = "; ".join(unmapped_assertions[:5])
        raise RepairRefusal(
            f"{len(unmapped_assertions)} TypedAssertion rows do not map exactly to TurnEvidence: "
            f"{preview}"
        )
    if ambiguous_assertions:
        preview = "; ".join(ambiguous_assertions[:5])
        raise RepairRefusal(
            f"{len(ambiguous_assertions)} TypedAssertion rows have conflicting foundations: {preview}"
        )

    return RepairPlan(
        evidence_updates=tuple(evidence_updates),
        assertion_updates=tuple(assertion_updates),
        rebuild_targets=tuple(sorted(rebuild_targets)),
        evidence_total=len(evidence_rows),
        assertion_total=len(assertion_rows),
        evidence_already_correct=evidence_already_correct,
        assertions_already_correct=assertions_already_correct,
    )


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repair LME scalar world time from a frozen fixture; dry-run by default."
    )
    parser.add_argument("--uri", default="bolt://127.0.0.1:7694")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="lmedata123")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-prefix")
    parser.add_argument("--expected-fixture-sha256")
    parser.add_argument("--backup-proof", type=Path)
    parser.add_argument("--audit-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source_times = load_source_time_index(args.fixture, args.prefix)
        repository = Neo4jRepository(
            uri=args.uri,
            database=args.database,
            user=args.user,
            password=args.password,
        )
        try:
            if not repository.ping():
                raise RepairRefusal(f"Neo4j is not reachable at {args.uri}")
            plan = build_repair_plan(repository, source_times, args.prefix)
            print(json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "fixture_sha256": source_times.fixture_sha256,
                    "fixture_namespaces": len(source_times.namespaces),
                    **plan.summary(),
                },
                indent=2,
                sort_keys=True,
            ))
            if not args.apply:
                print("DRY RUN -- no graph writes.")
                return 0

            if args.confirm_prefix != args.prefix:
                raise RepairRefusal("--confirm-prefix must exactly match --prefix")
            expected_sha = str(args.expected_fixture_sha256 or "").lower()
            if expected_sha != source_times.fixture_sha256:
                raise RepairRefusal(
                    "--expected-fixture-sha256 does not match the fixture bytes"
                )
            if args.backup_proof is None:
                raise RepairRefusal("--backup-proof is required with --apply")
            if args.audit_out is None:
                raise RepairRefusal("--audit-out is required with --apply")
            backup = verify_backup_proof(args.backup_proof)
            audit = apply_repair(
                repository,
                plan,
                audit_path=args.audit_out,
                fixture_path=args.fixture,
                source_times=source_times,
                namespace_prefix=args.prefix,
                backup=backup,
            )
            print(json.dumps(
                {
                    "status": audit["status"],
                    "repair_id": audit["repair_id"],
                    "evidence_updated": audit["applied"]["evidence_updated"],
                    "assertions_updated": audit["applied"]["assertions_updated"],
                    "views_rebuilt": len(audit["rebuilds"]),
                },
                indent=2,
                sort_keys=True,
            ))
            return 0
        finally:
            repository.close()
    except RepairRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
