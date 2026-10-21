"""Fixture-index loading and repair-plan construction for the LME source-time repair.

Extracted verbatim from ``repair_lme_scalar_source_times.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from repair_lme_scalar_source_times_model import (
    DATE_FORMAT,
    QueryExecutor,
    RepairPlan,
    RepairRefusal,
    SourceTimeIndex,
    _fixture_hash,
)


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
