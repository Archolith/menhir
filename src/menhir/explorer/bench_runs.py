"""Bounded filesystem catalog and read-only task projection for LongMemEval benchmark runs.

Contract: bench-inspection/v1

This module reads LME run artifacts (manifest, checkpoints, provenance) from a configured
allowlisted results root via MENHIR_BENCH_RESULTS_ROOT (required; fail closed if absent).
It never writes to Neo4j or imports archolith_bench runtime code.
The configured active run, plus provenance-verified recall runs that reuse its exact graph,
may combine artifact data with live graph projections.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from menhir.explorer.recall_packet_prototype import build_typed_recall_packet

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "bench-inspection/v1"


def _safe_int(value: object, default: int = 0) -> int:
    """Coerce *value* to int, returning *default* for None, empty, or malformed."""
    import math
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            if not math.isfinite(value):
                return default
            return int(value)
        except (OverflowError, ValueError, TypeError):
            return default
    try:
        return int(str(value))
    except (ValueError, TypeError, OverflowError):
        return default

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_safe_component(component: str) -> bool:
    return bool(_SAFE_COMPONENT.match(component))


def _read_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if path.is_symlink():
        logger.warning("Rejecting symlinked artifact: %s", path)
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Live graph queries (ported from archolith_bench/scalar_viewer.py)
# ---------------------------------------------------------------------------

_LIVE_EVIDENCE_QUERY = """
MATCH (t:TurnEvidence {namespace: $namespace})
OPTIONAL MATCH (t)-[:FOUNDS]->(a:TypedAssertion {namespace: $namespace})
RETURN t.turn_id AS id,
       coalesce(t.role, t.declarant, 'unknown') AS role,
       t.text AS text,
       t.session_id AS session_id,
       toString(t.occurred_at) AS occurred_at,
       toString(t.recorded_at) AS recorded_at,
       collect(DISTINCT a.assertion_id) AS founds
ORDER BY recorded_at, id
"""

_LIVE_ASSERTIONS_QUERY = """
MATCH (a:TypedAssertion {namespace: $namespace})
RETURN a.assertion_id AS id,
       a.source_key AS source_key,
       a.episode_uuid AS evidence_id,
       a.subject_uuid AS subject_uuid,
       a.subject_display AS subject,
       a.attribute AS attribute,
       a.scope AS scope,
       a.value_kind AS value_kind,
       a.unit AS unit,
       a.operation AS operation,
       a.value AS value,
       a.stated_span AS stated_span,
       toString(a.valid_at) AS valid_at,
       toString(a.learned_at) AS learned_at,
       a.time_basis AS time_basis,
       a.evidence_tier AS evidence_tier,
       a.perceiver_version AS perceiver_version,
       coalesce(a.binding_pending, false) AS binding_pending,
       coalesce(a.superseded, false) AS superseded
ORDER BY valid_at, learned_at, id
"""

_LIVE_STATE_VIEWS_QUERY = """
MATCH (v:Entity {group_id: $namespace, view_kind: 'scalar_state'})
RETURN v.uuid AS id,
       v.view_key AS view_key,
       v.view_subject_uuid AS subject_uuid,
       v.view_subject AS subject,
       v.ss_attribute AS attribute,
       v.ss_scope AS scope,
       v.ss_kind AS value_kind,
       v.ss_unit AS unit,
       v.ss_value AS value,
       v.ss_display AS display,
       v.summary AS summary,
       toString(v.valid_at) AS valid_at,
       toString(v.created_at) AS created_at,
       coalesce(v.view_current, true) AS current,
       v.scalar_effective_tier AS effective_tier,
       coalesce(v.scalar_contributors, []) AS contributor_ids,
       v.supersedes AS supersedes,
       v.superseded_by AS superseded_by
ORDER BY valid_at, created_at, id
"""

_LIVE_HISTORY_VIEWS_QUERY = """
MATCH (v:Entity {group_id: $namespace, view_kind: 'scalar_history'})
OPTIONAL MATCH (v)-[:HISTORY_ENTRY]->(history_assertion:TypedAssertion)
WITH v, collect(DISTINCT history_assertion.assertion_id) AS contributor_ids
RETURN v.uuid AS id,
       v.view_key AS view_key,
       v.view_subject_uuid AS subject_uuid,
       v.view_subject AS subject,
       v.ss_attribute AS attribute,
       v.ss_scope AS scope,
       v.ss_kind AS value_kind,
       v.ss_unit AS unit,
       v.sh_entry_count AS entry_count,
       v.sh_signature AS signature,
       v.sh_op_counts AS op_counts,
       v.sh_first_valid_at AS first_valid_at,
       v.sh_last_valid_at AS last_valid_at,
       v.view_payload AS payload,
       contributor_ids,
       toString(v.valid_at) AS valid_at,
       toString(v.created_at) AS created_at,
       coalesce(v.view_current, true) AS current
ORDER BY valid_at, created_at, id
"""

_LIVE_EVENT_ASSERTIONS_QUERY = """
MATCH (a:TypedEventAssertion {namespace: $namespace})
RETURN a.assertion_id AS id,
       a.assertion_key AS assertion_key,
       a.source_key AS source_key,
       a.episode_uuid AS evidence_id,
       a.turn_evidence_uuid AS turn_evidence_id,
       a.subject_uuid AS subject_uuid,
       a.subject_display AS subject,
       a.predicate AS predicate,
       a.domain AS domain,
       a.object_key AS object_key,
       a.object_display AS object_display,
       a.stated_span AS stated_span,
       toString(a.valid_at) AS valid_at,
       toString(a.learned_at) AS learned_at,
       a.time_basis AS time_basis,
       a.evidence_tier AS evidence_tier,
       coalesce(a.binding_pending, false) AS binding_pending,
       coalesce(a.superseded, false) AS superseded
ORDER BY valid_at, learned_at, assertion_key
"""

_LIVE_EVENT_VIEWS_QUERY = """
MATCH (v:Entity {group_id: $namespace, view_kind: 'timeline'})
WHERE v.view_predicate IS NOT NULL AND trim(coalesce(v.view_predicate, '')) <> ''
OPTIONAL MATCH (v)-[:EVENT_HISTORY_ENTRY]->(event_assertion:TypedEventAssertion)
WITH v, collect(DISTINCT event_assertion.assertion_key) AS contributor_ids
RETURN v.uuid AS id,
       v.view_key AS view_key,
       v.view_subject_uuid AS subject_uuid,
       v.view_subject AS subject,
       v.view_predicate AS predicate,
       v.view_domain AS domain,
       v.view_value AS entry_count,
       v.view_payload AS payload,
       contributor_ids,
       toString(v.valid_at) AS valid_at,
       toString(v.created_at) AS created_at,
       coalesce(v.view_current, true) AS current
ORDER BY predicate, domain, valid_at, created_at, id
"""

_LIVE_FACTS_QUERY = """
MATCH (s:Entity {group_id: $namespace})-[r]->(o:Entity {group_id: $namespace})
WHERE r.fact IS NOT NULL
RETURN s.name AS subject,
       type(r) AS relation,
       o.name AS object,
       r.fact AS fact,
       coalesce(r.episodes, []) AS episode_ids,
       toString(r.valid_at) AS valid_at,
       toString(r.created_at) AS learned_at
ORDER BY coalesce(toString(r.valid_at), toString(r.created_at)), fact
LIMIT 40
"""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class BenchScore:
    arm: str = ""
    correct: bool = False
    score: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    response_text: str = ""
    recalled: str = ""
    gold: str = ""
    question: str = ""


@dataclass
class BenchTask:
    namespace: str = ""
    question_id: str = ""
    question: str = ""
    answer: str = ""
    question_type: str = ""
    turns: int = 0
    typed_assertions: int = 0
    scalar_views: int = 0
    turn_evidence: int = 0
    scalar_llm_calls: int = 0
    scalar_consolidated: bool = False
    ready: bool = False
    failed_remaining: int = 0
    graph_available: bool | None = None
    scores: list[BenchScore] = field(default_factory=list)
    source_warning: str | None = None
    primary_outcome: str | None = None  # "PASSED", "FAILED", or "NOT SCORED"


@dataclass
class BenchRun:
    run_id: str = ""
    label: str = ""
    source: str = ""
    variant: str = ""
    model: str = ""
    total_items: int = 0
    completed_items: int = 0
    tasks: list[BenchTask] = field(default_factory=list)
    arms: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_at: str = ""
    menhir_commit: str = ""
    bench_commit: str = ""
    arm: str = ""
    is_active: bool = False
    source_warning: str | None = None
    attempts: int = 0
    noncanonical: bool = False
    resumed: bool = False
    phases: list[dict[str, Any]] = field(default_factory=list)
    harness_exit: int | None = None
    graph_source_run_id: str | None = None
    live_graph_source_run_id: str | None = None
    source_graph_reused: bool = False
    reingested: bool = False
    graph_container: str = ""
    graph_volume: str = ""
    fixture_sha256: str = ""
    fixture_count: int = 0
    namespace_prefix: str = ""


# ---------------------------------------------------------------------------
# Primary outcome helper
# ---------------------------------------------------------------------------


def _primary_outcome(scores: list[BenchScore]) -> str | None:
    """Determine the primary outcome pill for a task.

    Selection rule: prefer the score whose arm is ``menhir_recall``;
    otherwise the first scored arm that is not ``no_memory``; if none
    exist return ``NOT SCORED``.
    """
    if not scores:
        return "NOT SCORED"
    candidate: BenchScore | None = None
    for s in scores:
        if s.arm == "menhir_recall":
            candidate = s
            break
    if candidate is None:
        for s in scores:
            if s.arm != "no_memory":
                candidate = s
                break
    if candidate is None:
        return "NOT SCORED"
    return "PASSED" if candidate.correct else "FAILED"


# ---------------------------------------------------------------------------
# Checkpoint parsing
# ---------------------------------------------------------------------------


def _parse_checkpoint_record(rec: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(rec, dict):
        return None
    result = rec.get("result")
    if isinstance(result, dict):
        task_id = str(result.get("task_id") or rec.get("task_id") or "")
        correct = bool(result.get("correct"))
        raw_score = result.get("score")
        score = float(raw_score) if raw_score is not None else (1.0 if correct else 0.0)
        return {
            "arm": str(rec.get("arm", "")),
            "task_id": task_id,
            "correct": correct,
            "score": score,
            "input_tokens": _safe_int(result.get("input_tokens")),
            "output_tokens": _safe_int(result.get("output_tokens")),
            "response_text": str(result.get("response_text", "")),
            "recalled": str(result.get("recalled", "")),
            "gold": str(result.get("gold", "")),
            "question": str(result.get("question", "")),
        }
    correct = bool(rec.get("correct"))
    raw_score = rec.get("score")
    score = float(raw_score) if raw_score is not None else (1.0 if correct else 0.0)
    return {
        "arm": str(rec.get("arm", "")),
        "task_id": str(rec.get("task_id", "")),
        "correct": correct,
        "score": score,
        "input_tokens": _safe_int(rec.get("input_tokens")),
        "output_tokens": _safe_int(rec.get("output_tokens")),
        "response_text": str(rec.get("response_text", "")),
        "recalled": str(rec.get("recalled", "")),
        "gold": str(rec.get("gold", "")),
        "question": str(rec.get("question", "")),
    }


def _read_checkpoint_scores(path: Path) -> dict[str, list[dict[str, Any]]]:
    scores: dict[str, list[dict[str, Any]]] = {}
    if path.is_symlink():
        return scores
    if not path.exists():
        return scores
    try:
        raw = path.read_text(encoding="utf-8")
        raw = raw.strip()
        if not raw:
            return scores
        if raw.startswith("["):
            try:
                records = json.loads(raw)
                if isinstance(records, list):
                    for rec in records:
                        parsed = _parse_checkpoint_record(rec)
                        if parsed and parsed["task_id"]:
                            ns = parsed["task_id"]
                            if not ns.startswith("lme-"):
                                ns = f"lme-{ns}"
                            scores.setdefault(ns, []).append(parsed)
                    return scores
            except json.JSONDecodeError:
                pass
        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            parsed = _parse_checkpoint_record(rec)
            if parsed and parsed["task_id"]:
                ns = parsed["task_id"]
                if not ns.startswith("lme-"):
                    ns = f"lme-{ns}"
                scores.setdefault(ns, []).append(parsed)
    except OSError as exc:
        logger.debug("Could not read checkpoint %s: %s", path, exc)
    return scores


# ---------------------------------------------------------------------------
# Path safety — reject ALL symlinks, not only outside-root symlinks
# ---------------------------------------------------------------------------


def _is_bare_real_directory(path: Path) -> bool:
    """Return True if path is a real directory (not a symlink) and exists."""
    return path.exists() and path.is_dir() and not path.is_symlink() and not path.resolve().is_symlink()


def _safe_resolve(root: Path, *parts: str) -> Path | None:
    """Resolve *parts* under *root*; return the resolved path.

    Rejects if any intermediate component is a symlink, if the result
    escapes *root*, or if the result does not exist.
    Returns None on rejection.
    """
    root_resolved = root.resolve()
    # Build the path one component at a time, rejecting symlinks at every step.
    current = root_resolved
    for part in parts:
        candidate = current / part
        if candidate.is_symlink():
            logger.warning("Symlink rejected at %s/%s", current, part)
            return None
        current = candidate
    final = current.resolve()
    if final.is_symlink():
        logger.warning("Final resolved path is a symlink: %s", final)
        return None
    try:
        final.relative_to(root_resolved)
    except ValueError:
        logger.warning("Path escapes root: %s", final)
        return None
    if not final.exists():
        return None
    return final


def _safe_resolve_file(root: Path, *parts: str) -> Path | None:
    """Like _safe_resolve but for files (does not require is_dir)."""
    root_resolved = root.resolve()
    current = root_resolved
    for part in parts:
        candidate = current / part
        if candidate.is_symlink():
            return None
        current = candidate
    final = current.resolve()
    if final.is_symlink():
        return None
    try:
        final.relative_to(root_resolved)
    except ValueError:
        return None
    if not final.exists():
        return None
    return final


# ---------------------------------------------------------------------------
# Run discovery
# ---------------------------------------------------------------------------


def _discover_run_dirs(results_root: Path) -> dict[str, Path]:
    """Discover run directories mapping run_id -> relative path from root.

    Rejects symlinked entries. Duplicate run IDs are logged as ambiguous
    and both are excluded.
    """
    if not results_root.is_dir() or results_root.is_symlink():
        return {}
    root_resolved = results_root.resolve()
    found: dict[str, list[Path]] = {}

    for entry in sorted(root_resolved.iterdir()):
        if entry.is_symlink():
            continue
        if not entry.is_dir():
            continue
        if not _is_safe_component(entry.name):
            continue
        # Pattern 1: direct subdirectory
        manifest = _read_json(entry / "manifest.json")
        if isinstance(manifest, list):
            rid = entry.name
            rel = Path(entry.name)
            found.setdefault(rid, []).append(rel)
            continue
        # Pattern 2: one level deeper
        for sub in sorted(entry.iterdir()):
            if sub.is_symlink():
                continue
            if not sub.is_dir():
                continue
            if not _is_safe_component(sub.name):
                continue
            sub_manifest = _read_json(sub / "manifest.json")
            if isinstance(sub_manifest, list):
                rid = sub.name
                rel = Path(entry.name) / sub.name
                found.setdefault(rid, []).append(rel)

    # Exclude ambiguous duplicates
    result: dict[str, Path] = {}
    for rid, paths in found.items():
        if len(paths) > 1:
            logger.warning("Ambiguous run_id %r found at %s; excluding both", rid, paths)
            continue
        result[rid] = paths[0]

    return result


def _read_provenance(provenance_path: Path) -> dict[str, Any]:
    data = _read_json(provenance_path)
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
        return data[0]
    return {}


_IDENTITY_FIELDS = frozenset({
    "variant", "arm", "extract_model", "menhir_commit", "bench_commit",
    "started_at", "LONGMEMEVAL_VARIANT", "dataset", "namespace_prefix",
    "segmentation", "fixture_sha256", "fixture_count", "container", "volume",
})


def _get_identity(provenance: dict[str, Any]) -> dict[str, Any]:
    """Resolve identity, merging latest_attempt fields over top-level identity.

    In the current provenance schema, latest_attempt carries identity fields
    directly (menhir_commit, bench_commit, variant, etc.) rather than under
    a nested .identity key. Fields from latest_attempt win over top-level.
    """
    base: dict[str, Any] = {}
    identity = provenance.get("identity")
    if isinstance(identity, dict):
        for k in _IDENTITY_FIELDS:
            if k in identity:
                base[k] = identity[k]
    latest = provenance.get("latest_attempt")
    if isinstance(latest, dict):
        for k in _IDENTITY_FIELDS:
            if k in latest:
                base[k] = latest[k]
    return base


def _get_provenance_value(provenance: dict[str, Any], key: str) -> Any:
    """Resolve a provenance field with latest-attempt precedence.

    Run writers have used three placements over time: top-level fields, the nested
    ``identity`` object, and direct fields on ``latest_attempt``. Read all three,
    while retaining the same latest-attempt precedence as ``_get_identity``.
    """
    value = provenance.get(key)
    identity = provenance.get("identity")
    if isinstance(identity, dict) and key in identity:
        value = identity[key]
    latest = provenance.get("latest_attempt")
    if isinstance(latest, dict) and key in latest:
        value = latest[key]
    return value


def _find_checkpoint_files(run_dir: Path) -> list[Path]:
    """Find checkpoint score files, rejecting symlinks."""
    results: list[Path] = []
    for pat in (".checkpoint_*.jsonl",):
        for cp in run_dir.glob(pat):
            if not cp.is_symlink():
                results.append(cp)
    for child in run_dir.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        for pat in (".checkpoint_*.jsonl",):
            for cp in child.glob(pat):
                if not cp.is_symlink():
                    results.append(cp)
    rm = run_dir / "run_manifest.json"
    if rm.exists() and not rm.is_symlink():
        results.append(rm)
    return results


# ---------------------------------------------------------------------------
# Telemetry audit reader (ported from scalar_viewer.py, no archolith_bench import)
# ---------------------------------------------------------------------------


def _normalized_slot(values: list[Any] | tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(str(value or "").strip().lower() for value in values)


def _assertion_slot(assertion: dict[str, Any]) -> tuple[str, ...]:
    return _normalized_slot([
        assertion.get("subject_uuid"),
        assertion.get("attribute"),
        assertion.get("scope"),
        assertion.get("value_kind"),
        assertion.get("unit"),
    ])


def _audit_slot(event: dict[str, Any]) -> tuple[str, ...] | None:
    details = event.get("details") or {}
    raw_slot = details.get("slot")
    if not isinstance(raw_slot, (list, tuple)):
        return None
    if len(raw_slot) == 5:
        return _normalized_slot(raw_slot)
    if len(raw_slot) == 4 and details.get("subject_uuid"):
        return _normalized_slot([details["subject_uuid"], *raw_slot])
    return None


def _read_audit(
    telemetry_db: Path,
    namespace: str,
    assertions: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]], str | None]:
    if not telemetry_db.exists():
        return None, [], None
    uri = telemetry_db.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
            rows = conn.execute(
                """
                SELECT id, recorded_at, event, status, episode_uuid, details_json
                FROM lifecycle_events
                WHERE phase = 'consolidation_audit'
                  AND json_extract(details_json, '$.namespace') = ?
                ORDER BY id
                """,
                (namespace,),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Could not read telemetry audit: %s", exc)
        return None, [], "Telemetry audit could not be read."

    assertion_ids = {str(a.get("id") or "") for a in assertions}
    source_keys = {str(a.get("source_key") or "") for a in assertions}
    parsed: list[dict[str, Any]] = []
    relevant_passes: set[str] = set()
    pass_last_id: dict[str, int] = {}
    for row_id, at, event, state, pass_id, details_json in rows:
        try:
            details = json.loads(details_json or "{}")
        except (TypeError, ValueError):
            details = {"_raw": details_json}
        pid = str(pass_id or "")
        parsed.append({
            "id": int(row_id),
            "recorded_at": at,
            "event": event,
            "state": state,
            "pass_id": pid,
            "details": details,
        })
        pass_last_id[pid] = int(row_id)
        if (
            str(details.get("assertion_id") or "") in assertion_ids
            or str(details.get("source_key") or "") in source_keys
        ):
            relevant_passes.add(pid)

    if not relevant_passes:
        return None, [], (
            "Audit events exist for this namespace, but none match the assertions in this graph. "
            "The graph and telemetry may come from different benchmark attempts."
        )
    pass_id = max(relevant_passes, key=lambda pid: pass_last_id.get(pid, -1))
    events = [
        {
            "recorded_at": row["recorded_at"],
            "event": row["event"],
            "state": row["state"],
            "details": row["details"],
        }
        for row in parsed
        if row["pass_id"] == pass_id
    ]
    return pass_id, events, None


def annotate_assertion_fold_outcomes(
    assertions: list[dict[str, Any]],
    views: list[dict[str, Any]],
    history_views: list[dict[str, Any]],
    audit: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Explain each assertion's state and history projection outcomes."""
    current_state_ids: set[str] = set()
    historical_state_ids: set[str] = set()
    for view in views:
        target = current_state_ids if view.get("current") else historical_state_ids
        target.update(str(value) for value in view.get("contributor_ids") or [] if value)

    current_history_ids: set[str] = set()
    historical_history_ids: set[str] = set()
    for view in history_views:
        target = current_history_ids if view.get("current") else historical_history_ids
        target.update(str(value) for value in view.get("contributor_ids") or [] if value)
        target.update(
            str(entry.get("assertion_id"))
            for entry in view.get("entries") or []
            if entry.get("assertion_id")
        )

    state_events: dict[tuple[str, ...], dict[str, str]] = {}
    history_events: dict[tuple[str, ...], dict[str, str]] = {}
    for event in audit:
        slot = _audit_slot(event)
        event_name = str(event.get("event") or "")
        event_state = str(event.get("state") or "")
        details = event.get("details") or {}
        if slot and event_name == "fold" and event_state in {"abstain", "expiry"}:
            state_events[slot] = {
                "status": "abstained" if event_state == "abstain" else "expired",
                "reason": str(details.get("reason") or event_state),
            }
        elif slot and event_name == "view_write" and event_state == "stale_skipped":
            state_events[slot] = {
                "status": "write_failed",
                "reason": "stale scalar_state write was skipped",
            }
        elif slot and event_name == "history_fold" and event_state == "abstain":
            history_events[slot] = {
                "status": "abstained",
                "reason": str(details.get("reason") or event_state),
            }
        elif event_name == "history_rebuild" and event_state == "incomplete":
            for failure in details.get("failed_slots") or []:
                failed_slot = failure.get("slot_key")
                if isinstance(failed_slot, (list, tuple)) and len(failed_slot) == 5:
                    history_events[_normalized_slot(failed_slot)] = {
                        "status": "write_failed",
                        "reason": str(failure.get("error") or "scalar_history write failed"),
                    }

    annotated: list[dict[str, Any]] = []
    for assertion in assertions:
        assertion_id = str(assertion.get("id") or "")
        slot = _assertion_slot(assertion)
        pending = bool(assertion.get("binding_pending"))

        if pending:
            state = {"status": "not_folded", "reason": "subject binding is pending"}
            history = {"status": "not_folded", "reason": "subject binding is pending"}
        else:
            state = state_events.get(slot)
            if assertion_id in current_state_ids:
                state = {"status": "current", "reason": "contributes to the current scalar_state view"}
            elif state is None and assertion_id in historical_state_ids:
                state = {"status": "historical", "reason": "contributed to a superseded scalar_state view"}
            elif state is None and assertion.get("superseded"):
                state = {"status": "superseded", "reason": "assertion is superseded"}
            elif state is None:
                state = {
                    "status": "not_materialized",
                    "reason": (
                        "recorded in scalar_history but not used by the current scalar_state fold"
                        if assertion_id in current_history_ids
                        else "no scalar_state fold outcome was recorded"
                    ),
                }

            history = history_events.get(slot)
            if assertion_id in current_history_ids:
                history = {"status": "recorded", "reason": "recorded in the current scalar_history view"}
            elif history is None and assertion_id in historical_history_ids:
                history = {"status": "historical", "reason": "recorded in a superseded scalar_history view"}
            elif history is None:
                history = {
                    "status": "not_materialized",
                    "reason": "no scalar_history fold outcome was recorded",
                }

        annotated.append({
            **assertion,
            "fold_outcome": {
                "state": state,
                "history": history,
            },
        })
    return annotated


# ---------------------------------------------------------------------------
# Memory inventory builder
# ---------------------------------------------------------------------------


def _build_memory_inventory(
    assertions: list[dict[str, Any]],
    views: list[dict[str, Any]],
    history_views: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    event_views: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    operations_by_assertion = {
        str(assertion.get("id") or ""): str(assertion.get("operation") or "")
        for assertion in assertions
    }
    inventory: list[dict[str, Any]] = []
    for view_kind, rows in (
        ("scalar_state", views),
        ("scalar_history", history_views),
    ):
        for view in rows:
            op_counts = view.get("op_counts") or {}
            if view_kind == "scalar_history" and isinstance(op_counts, dict):
                operations = set(op_counts.keys())
            else:
                operations = {
                    operations_by_assertion.get(str(assertion_id), "")
                    for assertion_id in view.get("contributor_ids") or []
                }
            operations.discard("")
            if operations == {"absolute"}:
                derivation = "absolute"
            elif operations == {"delta"}:
                derivation = "delta"
            elif operations:
                derivation = "mixed"
            else:
                derivation = "unknown"
            inventory.append({
                "id": str(view.get("id") or ""),
                "memory_type": "view",
                "view_kind": view_kind,
                "derivation": derivation,
                "operations": sorted(operations),
                "current": bool(view.get("current")),
                "subject": str(view.get("subject") or ""),
                "attribute": str(view.get("attribute") or ""),
                "scope": str(view.get("scope") or ""),
                "value": str(view.get("display") or view.get("value") or ""),
                "content": str(view.get("summary") or ""),
                "valid_at": view.get("valid_at"),
            })
    for view in event_views or []:
        inventory.append({
            "id": str(view.get("id") or ""),
            "memory_type": "view",
            "view_kind": "event_history",
            "derivation": "event",
            "operations": ["occurrence"],
            "current": bool(view.get("current")),
            "subject": str(view.get("subject") or ""),
            "attribute": str(view.get("predicate") or ""),
            "scope": str(view.get("domain") or ""),
            "value": f"{_safe_int(view.get('entry_count'))} occurrence(s)",
            "content": "",
            "valid_at": view.get("valid_at"),
        })
    for index, fact in enumerate(facts):
        inventory.append({
            "id": f"content:{index}",
            "memory_type": "content",
            "view_kind": None,
            "derivation": None,
            "operations": [],
            "current": None,
            "subject": str(fact.get("subject") or ""),
            "relation": str(fact.get("relation") or ""),
            "object": str(fact.get("object") or ""),
            "content": str(fact.get("fact") or ""),
            "episode_ids": list(fact.get("episode_ids") or []),
            "valid_at": fact.get("valid_at"),
            "learned_at": fact.get("learned_at"),
        })
    return inventory


# ---------------------------------------------------------------------------
# Catalog — fail closed when MENHIR_BENCH_RESULTS_ROOT is absent
# ---------------------------------------------------------------------------


class BenchRunCatalog:
    """Filesystem catalog of LME benchmark runs.

    Requires a configured results_root (caller must handle the case where
    MENHIR_BENCH_RESULTS_ROOT env var is not set by returning empty results
    with a clear configuration warning).
    """

    def __init__(self, results_root: str | Path | None, active_run_id: str | None = None):
        if results_root is None:
            self._root = None
        else:
            raw = Path(results_root)
            if raw.is_symlink():
                logger.warning("Benchmark results root is a symlink: %s; treating as unconfigured", raw)
                self._root = None
            else:
                self._root = raw.resolve()
        self._active_run_id = active_run_id
        self._run_dirs: dict[str, Path] = {}

    @property
    def results_root(self) -> Path | None:
        return self._root

    @property
    def active_run_id(self) -> str | None:
        return self._active_run_id

    @property
    def is_configured(self) -> bool:
        return self._root is not None and self._root.is_dir()

    def _ensure_discovered(self) -> None:
        if not self._run_dirs and self._root is not None:
            self._run_dirs = _discover_run_dirs(self._root)

    def _run_dir(self, run_id: str) -> Path | None:
        if self._root is None:
            return None
        if not _is_safe_component(run_id):
            return None
        self._ensure_discovered()
        rel = self._run_dirs.get(run_id)
        if rel is None:
            return None
        return _safe_resolve(self._root, *rel.parts)

    def list_runs(self) -> list[BenchRun]:
        if self._root is None:
            return []
        self._ensure_discovered()
        entries: list[tuple[float, str, Path]] = []
        for rid, rel in self._run_dirs.items():
            run_dir = _safe_resolve(self._root, *rel.parts)
            if run_dir is None or not run_dir.is_dir():
                continue
            mtime = run_dir.stat().st_mtime
            entries.append((mtime, rid, run_dir))
        entries.sort(key=lambda x: x[0], reverse=True)
        runs: list[BenchRun] = []
        for mtime, rid, run_dir in entries:
            manifest = _read_json(run_dir / "manifest.json")
            if not isinstance(manifest, list):
                continue
            run = self._build_run(rid, run_dir, manifest)
            if run is not None:
                self._apply_live_graph_lineage(run)
                runs.append(run)
        return runs

    def get_run(self, run_id: str) -> BenchRun | None:
        if self._root is None:
            return None
        run_dir = self._run_dir(run_id)
        if run_dir is None or not run_dir.is_dir():
            return None
        manifest = _read_json(run_dir / "manifest.json")
        if not isinstance(manifest, list):
            return BenchRun(
                run_id=run_id,
                source=run_dir.parent.name,
                total_items=0, completed_items=0,
                source_warning="Run directory exists but manifest has no task array.",
            )
        run = self._build_run(run_id, run_dir, manifest)
        if run is not None:
            self._apply_live_graph_lineage(run)
        return run

    def get_run_dir(self, run_id: str) -> Path | None:
        """Return the resolved run directory for *run_id*, or None."""
        return self._run_dir(run_id)

    def is_active(self, run_id: str) -> bool:
        return self._active_run_id is not None and run_id == self._active_run_id

    def live_graph_source(self, run: BenchRun) -> str | None:
        """Return the run whose live graph may safely back ``run``.

        Historical recall results may point at the configured active graph only
        when provenance explicitly says the graph was reused without reingest and
        the immutable graph identity matches. Missing identity fields fail closed.
        """
        if run.is_active:
            return run.run_id
        if (
            not run.source_graph_reused
            or run.reingested
            or not run.graph_source_run_id
            or run.graph_source_run_id != self._active_run_id
        ):
            return None

        source = self.get_run(run.graph_source_run_id)
        if source is None or not source.is_active:
            return None

        compared_fields = (
            "graph_container",
            "graph_volume",
            "fixture_sha256",
            "fixture_count",
            "namespace_prefix",
        )
        for field_name in compared_fields:
            historical_value = getattr(run, field_name)
            source_value = getattr(source, field_name)
            if not historical_value or not source_value or historical_value != source_value:
                logger.warning(
                    "Rejecting live graph lineage for run %s: %s mismatch",
                    run.run_id,
                    field_name,
                )
                return None
        return source.run_id

    def _apply_live_graph_lineage(self, run: BenchRun) -> None:
        """Annotate a run after fail-closed graph-lineage resolution."""
        source_run_id = self.live_graph_source(run)
        run.live_graph_source_run_id = source_run_id
        if not source_run_id or source_run_id == run.run_id:
            return
        run.source_warning = (
            f"REUSED ACTIVE GRAPH: historical scores + live graph from {source_run_id}"
        )
        for task in run.tasks:
            task.graph_available = False
            task.source_warning = (
                "REUSED ACTIVE GRAPH CONFIGURED: Historical score artifact; "
                f"live graph projection is sourced from {source_run_id} after identity validation."
            )

    def _build_run(self, run_id: str, run_dir: Path, manifest: list[Any]) -> BenchRun | None:
        is_active = self.is_active(run_id)
        run_source = run_dir.parent.name

        provenance = _read_provenance(run_dir / "run_provenance.json")
        identity = _get_identity(provenance)
        attempt_count = 0
        noncanonical = False
        resumed = False
        phases: list[dict[str, Any]] = []
        harness_exit: int | None = None
        if isinstance(provenance, dict):
            attempt_count = _safe_int(provenance.get("attempt_count"))
            noncanonical = bool(provenance.get("noncanonical", False))
            resumed = bool(provenance.get("resumed", False))
            if isinstance(provenance.get("phases"), list):
                for p in provenance["phases"]:
                    if isinstance(p, dict):
                        phases.append({
                            "phase": str(p.get("phase", "")),
                            "status": str(p.get("status", "")),
                            "completed_at": str(p.get("completed_at", "")),
                        })
            harness_exit = provenance.get("harness_exit")
            if isinstance(harness_exit, int):
                pass
            else:
                harness_exit = None
            latest = provenance.get("latest_attempt")
            if isinstance(latest, dict):
                resumed = resumed or bool(latest.get("resumed", False))

        variant = str(identity.get("variant", identity.get("LONGMEMEVAL_VARIANT", "?")))
        arm = str(identity.get("arm", ""))
        extract_model = str(identity.get("extract_model", ""))
        menhir_commit = str(identity.get("menhir_commit", ""))
        bench_commit = str(identity.get("bench_commit", ""))
        started_at = str(identity.get("started_at", ""))
        graph_source_run_id = str(_get_provenance_value(provenance, "source_run_id") or "") or None
        source_graph_reused = bool(_get_provenance_value(provenance, "source_graph_reused"))
        reingested = bool(_get_provenance_value(provenance, "reingested"))
        graph_container = str(_get_provenance_value(provenance, "container") or "")
        graph_volume = str(_get_provenance_value(provenance, "volume") or "")
        fixture_sha256 = str(_get_provenance_value(provenance, "fixture_sha256") or "")
        fixture_count = _safe_int(_get_provenance_value(provenance, "fixture_count"))
        namespace_prefix = str(_get_provenance_value(provenance, "namespace_prefix") or "")

        manifest_rows = [r for r in manifest if isinstance(r, dict)]
        total_items = len(manifest_rows)
        completed_items = sum(1 for r in manifest_rows if r.get("ready"))

        checkpoint_scores: dict[str, list[dict[str, Any]]] = {}
        for cp in _find_checkpoint_files(run_dir):
            records = _read_checkpoint_scores(cp)
            for ns, recs in records.items():
                checkpoint_scores.setdefault(ns, []).extend(recs)

        tasks: list[BenchTask] = []
        for row in manifest_rows:
            namespace = str(row.get("namespace", ""))
            question_id = str(row.get("question_id", namespace))
            question = str(row.get("question", ""))
            answer = str(row.get("answer", ""))
            question_type = str(row.get("question_type", ""))
            turns = _safe_int(row.get("turns"))
            typed_assertions = _safe_int(row.get("typed_assertions"))
            scalar_views = _safe_int(row.get("scalar_views"))
            turn_evidence = _safe_int(row.get("turn_evidence"))
            scalar_llm_calls = _safe_int(row.get("scalar_llm_calls"))
            scalar_consolidated = bool(row.get("scalar_consolidated", False))
            ready = bool(row.get("ready", False))
            failed_remaining = _safe_int(row.get("failed_remaining"))

            # graph_available starts None for non-active, False for active
            # (only set True after a successful live query in task reader).
            if is_active:
                graph_available = False
                source_warning = (
                    "ACTIVE CONFIGURED: This run is configured as the active benchmark run. "
                    "Live graph projection becomes available when you open a task."
                )
            else:
                graph_available = None
                source_warning = (
                    "ARTIFACT-ONLY: graph data is not available for non-active runs."
                )

            task_scores_raw = checkpoint_scores.get(namespace, [])
            task_scores = [BenchScore(**{k: s.get(k, "") for k in BenchScore.__dataclass_fields__}) for s in task_scores_raw]
            primary_outcome = _primary_outcome(task_scores)
            tasks.append(BenchTask(
                namespace=namespace,
                question_id=question_id,
                question=question,
                answer=answer,
                question_type=question_type,
                turns=turns,
                typed_assertions=typed_assertions,
                scalar_views=scalar_views,
                turn_evidence=turn_evidence,
                scalar_llm_calls=scalar_llm_calls,
                scalar_consolidated=scalar_consolidated,
                ready=ready,
                failed_remaining=failed_remaining,
                graph_available=graph_available,
                scores=task_scores,
                source_warning=source_warning,
                primary_outcome=primary_outcome,
            ))

        arms: dict[str, dict[str, Any]] = {}
        for recs in checkpoint_scores.values():
            for rec in recs:
                arm_name = rec.get("arm", "")
                if arm_name not in arms:
                    arms[arm_name] = {"n": 0, "correct": 0}
                arms[arm_name]["n"] += 1
                if rec.get("correct"):
                    arms[arm_name]["correct"] += 1

        label = run_id
        if arm:
            label = f"{run_id} ({arm})"

        src_warning: str | None = None
        if is_active:
            src_warning = "ACTIVE CONFIGURED"
        else:
            src_warning = "ARTIFACT-ONLY"

        return BenchRun(
            run_id=run_id,
            label=label,
            source=run_source,
            variant=variant,
            model=extract_model,
            total_items=total_items,
            completed_items=completed_items,
            tasks=tasks,
            arms=arms,
            started_at=started_at,
            menhir_commit=menhir_commit,
            bench_commit=bench_commit,
            arm=arm,
            is_active=is_active,
            source_warning=src_warning,
            attempts=attempt_count,
            noncanonical=noncanonical,
            resumed=resumed,
            phases=phases,
            harness_exit=harness_exit,
            graph_source_run_id=graph_source_run_id,
            source_graph_reused=source_graph_reused,
            reingested=reingested,
            graph_container=graph_container,
            graph_volume=graph_volume,
            fixture_sha256=fixture_sha256,
            fixture_count=fixture_count,
            namespace_prefix=namespace_prefix,
        )


# ---------------------------------------------------------------------------
# Recursive privacy redaction
# ---------------------------------------------------------------------------

_TEXT_KEYS = frozenset({
    "question", "answer",
    "response_text", "recalled", "gold",
    "text", "stated_span", "source_quote",
    "subject", "attribute", "value", "display", "summary", "content",
    "name", "fact", "object", "object_display", "quote", "what", "relation",
    "reason", "value_json",
})


def _redact_nested(obj: Any) -> Any:
    from menhir.privacy import redact_text
    if isinstance(obj, dict):
        return {
            k: redact_text(v, reveal=False) if k in _TEXT_KEYS and isinstance(v, str)
            else _redact_nested(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_nested(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Task projection reader
# ---------------------------------------------------------------------------


class BenchRunTaskReader:
    """Read-only task projection for a single run's task."""

    def __init__(self, catalog: BenchRunCatalog, repo_provider: Callable[[], Any] | None = None):
        self._catalog = catalog
        self._repo_provider = repo_provider

    def get_task_detail(
        self,
        run_id: str,
        namespace: str,
        *,
        reveal: bool = False,
    ) -> dict[str, Any] | None:
        if not _is_safe_component(namespace):
            return None

        run = self._catalog.get_run(run_id)
        if run is None:
            return None

        task: BenchTask | None = None
        for t in run.tasks:
            if t.namespace == namespace:
                task = t
                break
        if task is None:
            return None

        result: dict[str, Any] = {
            "contract": CONTRACT_VERSION,
            "run_id": run_id,
            "namespace": task.namespace,
            "question_id": task.question_id,
            "question": task.question,
            "answer": task.answer,
            "question_type": task.question_type,
            "turns": task.turns,
            "typed_assertions": task.typed_assertions,
            "scalar_views": task.scalar_views,
            "turn_evidence": task.turn_evidence,
            "scalar_llm_calls": task.scalar_llm_calls,
            "scalar_consolidated": task.scalar_consolidated,
            "ready": task.ready,
            "failed_remaining": task.failed_remaining,
            "graph_available": task.graph_available,
            "source_warning": task.source_warning,
            "is_active": run.is_active,
            "graph_source_run_id": run.live_graph_source_run_id,
            "historical_score_with_live_graph": bool(
                run.live_graph_source_run_id
                and run.live_graph_source_run_id != run.run_id
            ),
            "run_label": run.label,
            "run_variant": run.variant,
            "run_arm": run.arm,
            "run_menhir_commit": run.menhir_commit,
            "run_bench_commit": run.bench_commit,
            "run_started_at": run.started_at,
            "attempts": run.attempts,
            "noncanonical": run.noncanonical,
            "resumed": run.resumed,
            "phases": run.phases,
            "harness_exit": run.harness_exit,
            "primary_outcome": task.primary_outcome,
            "scores": [
                {
                    "arm": s.arm,
                    "correct": s.correct,
                    "score": s.score,
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "response_text": s.response_text,
                    "recalled": s.recalled,
                    "gold": s.gold,
                    "question": s.question,
                }
                for s in task.scores
            ],
        }

        # The active run, or a provenance-verified recall run that reused its
        # exact graph, may read the shared live graph. Historical scores remain
        # artifact data and are labeled separately in the response.
        graph_source_run_id = run.live_graph_source_run_id
        if graph_source_run_id and self._repo_provider is not None:
            run_dir = self._catalog.get_run_dir(graph_source_run_id)
            live_result = self._try_live_query(
                namespace,
                run_dir,
                graph_source_run_id=graph_source_run_id,
                historical_run_id=run_id if graph_source_run_id != run_id else None,
            )
            if live_result is not None:
                result.update(live_result)

        if not reveal:
            result = _redact_nested(result)

        return result

    def _try_live_query(
        self,
        namespace: str,
        run_dir: Path | None = None,
        *,
        graph_source_run_id: str | None = None,
        historical_run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Attempt a live graph query. Returns a partial update dict or None."""
        configured_label = (
            f"REUSED ACTIVE GRAPH ({graph_source_run_id})"
            if historical_run_id
            else "ACTIVE CONFIGURED"
        )
        try:
            repo = self._repo_provider()
            if repo is None:
                return {
                    "graph_available": False,
                    "source_warning": (
                        f"{configured_label} — Neo4j repository is not available. "
                        "Check that the server started with a working Neo4j connection."
                    ),
                }
            live = self._query_live(repo, namespace, run_dir=run_dir)
            evidence_count = len(live.get("evidence", []))
            assertion_count = len(live.get("assertions", []))
            view_count = len(live.get("views", []))
            history_count = len(live.get("history_views", []))
            event_assertion_count = len(live.get("event_assertions", []))
            event_view_count = len(live.get("event_views", []))
            fact_count = len(live.get("facts", []))
            has_content = (
                evidence_count > 0 or assertion_count > 0 or view_count > 0
                or history_count > 0 or event_assertion_count > 0
                or event_view_count > 0 or fact_count > 0
            )
            if has_content:
                source_warning = "LIVE BENCHMARK GRAPH"
                if historical_run_id:
                    source_warning = (
                        "HISTORICAL SCORE + LIVE SOURCE GRAPH: "
                        f"{historical_run_id} scores over graph {graph_source_run_id}."
                    )
                return {
                    "graph_available": True,
                    "source_warning": source_warning,
                    "graph_source_run_id": graph_source_run_id,
                    "historical_score_with_live_graph": bool(historical_run_id),
                    "live_graph": live,
                }
            else:
                return {
                    "graph_available": False,
                    "source_warning": (
                        f"{configured_label} — live graph query returned no content "
                        "for this namespace. The graph may be empty or the namespace "
                        "may not exist in the current graph."
                    ),
                }
        except Exception as exc:
            logger.warning("Live graph query failed for namespace %s: %s", namespace, exc)
            return {
                "graph_available": False,
                "source_warning": (
                    f"{configured_label} — live graph query failed: "
                    f"{type(exc).__name__}. Check Neo4j connectivity."
                ),
            }

    def _query_live(self, repo: Any, namespace: str, run_dir: Path | None = None) -> dict[str, Any]:
        evidence = repo.execute(_LIVE_EVIDENCE_QUERY, params={"namespace": namespace})
        assertions = repo.execute(_LIVE_ASSERTIONS_QUERY, params={"namespace": namespace})
        views = repo.execute(_LIVE_STATE_VIEWS_QUERY, params={"namespace": namespace})
        history_views = repo.execute(_LIVE_HISTORY_VIEWS_QUERY, params={"namespace": namespace})
        event_assertions = repo.execute(
            _LIVE_EVENT_ASSERTIONS_QUERY, params={"namespace": namespace}
        )
        event_views = repo.execute(_LIVE_EVENT_VIEWS_QUERY, params={"namespace": namespace})
        facts = repo.execute(_LIVE_FACTS_QUERY, params={"namespace": namespace})

        views_list = [dict(v) for v in views] if views else []
        history_list = [dict(v) for v in history_views] if history_views else []
        event_assertion_list = [dict(a) for a in event_assertions] if event_assertions else []
        event_view_list = [dict(v) for v in event_views] if event_views else []
        assertions_list = [dict(a) for a in assertions] if assertions else []
        facts_list = [dict(f) for f in facts] if facts else []
        evidence_list = [dict(e) for e in evidence] if evidence else []

        for hv in history_list:
            raw_payload = hv.get("payload")
            if isinstance(raw_payload, str):
                try:
                    hv["entries"] = json.loads(raw_payload)
                except (json.JSONDecodeError, TypeError):
                    hv["entries"] = []
            elif isinstance(raw_payload, list):
                hv["entries"] = raw_payload
            else:
                hv["entries"] = []
            if "op_counts" in hv and isinstance(hv["op_counts"], str):
                try:
                    hv["op_counts"] = json.loads(hv["op_counts"])
                except (json.JSONDecodeError, TypeError):
                    hv["op_counts"] = {}
            hv.pop("payload", None)

        for event_view in event_view_list:
            raw_payload = event_view.get("payload")
            if isinstance(raw_payload, str):
                try:
                    event_view["entries"] = json.loads(raw_payload)
                except (json.JSONDecodeError, TypeError):
                    event_view["entries"] = []
            elif isinstance(raw_payload, list):
                event_view["entries"] = raw_payload
            else:
                event_view["entries"] = []
            event_view.pop("payload", None)

        # Build evidence-by-id map for source quote resolution
        evidence_by_id = {str(e.get("id", "")): e for e in evidence_list}

        # Audit + fold outcomes
        telemetry_db = self._resolve_telemetry_db(run_dir) if run_dir else None
        audit_pass_id, audit, audit_warning = _read_audit(
            telemetry_db, namespace, assertions_list
        ) if telemetry_db else (None, [], None)

        assertions_annotated = annotate_assertion_fold_outcomes(
            assertions_list, views_list, history_list, audit
        )

        # Annotate assertions with source quote from evidence
        for assertion in assertions_annotated:
            evidence_id = str(assertion.get("evidence_id", ""))
            source = evidence_by_id.get(evidence_id)
            if source is not None:
                assertion["source_quote"] = source.get("text", "")
            else:
                # Fall back: search evidence whose founds include this assertion
                for e in evidence_list:
                    if assertion.get("id") in (e.get("founds") or []):
                        assertion["source_quote"] = e.get("text", "")
                        break

        inventory = _build_memory_inventory(
            assertions_annotated, views_list, history_list, facts_list, event_view_list
        )
        derivation_by_view_id = {
            item["id"]: item["derivation"]
            for item in inventory
            if item.get("memory_type") == "view"
        }
        for view in views_list + history_list + event_view_list:
            view["derivation"] = derivation_by_view_id.get(
                str(view.get("id") or ""), "unknown"
            )

        recall_packet = build_typed_recall_packet({
            "assertions": assertions_annotated,
            "views": views_list,
            "history_views": history_list,
            "event_assertions": event_assertion_list,
            "event_views": event_view_list,
            "memory_inventory": inventory,
        })

        scalar_roles = [
            {
                "id": "scalar-state",
                "name": "Current Scalar",
                "system_name": "Scalar State",
                "role": "Authoritative current value",
                "answers": "What is true now?",
                "count": len(views_list),
                "status": "available" if views_list else "empty",
            },
            {
                "id": "scalar-history",
                "name": "Change Scalar",
                "system_name": "Scalar History",
                "role": "Advisory numeric change record",
                "answers": "How did the value change over time?",
                "count": len(history_list),
                "status": "available" if history_list else "empty",
            },
            {
                "id": "event-history",
                "name": "Event Scalar",
                "system_name": "Event History",
                "role": "Ordered occurrence history",
                "answers": "What happened latest or immediately before it?",
                "count": len(event_view_list),
                "assertion_count": len(event_assertion_list),
                "status": "available" if event_view_list else "empty",
            },
        ]

        return {
            "evidence": evidence_list,
            "assertions": assertions_annotated,
            "views": views_list,
            "history_views": history_list,
            "event_assertions": event_assertion_list,
            "event_views": event_view_list,
            "scalar_roles": scalar_roles,
            "facts": facts_list,
            "memory_inventory": inventory,
            "recall_packet": recall_packet,
            "audit_pass_id": audit_pass_id,
            "audit_warning": audit_warning,
            "assertion_count": len(assertions_annotated),
            "evidence_count": len(evidence_list),
        }

    def _resolve_telemetry_db(self, run_dir: Path) -> Path | None:
        """Resolve telemetry DB path from the run's result directory.

        The telemetry DB must be inside the resolved run directory, must not be
        a symlink, and must be under the configured results root.
        """
        if self._catalog.results_root is None:
            return None
        candidate = run_dir / "mcp_telemetry.db"
        if not candidate.exists() or candidate.is_symlink():
            return None
        try:
            candidate.resolve().relative_to(self._catalog.results_root.resolve())
        except ValueError:
            return None
        return candidate


# ---------------------------------------------------------------------------
# Default provider factory — fail closed when env absent
# ---------------------------------------------------------------------------

_CACHED_CATALOG: BenchRunCatalog | None = None


def _filtered_task_ids(tasks: list[BenchTask], outcome: str | None) -> list[str]:
    """Return namespace list for tasks matching *outcome* category.

    ``outcome`` is one of ``"all"``, ``"passed"``, ``"failed"``, ``"unscored"``,
    or ``None`` (treated as ``"all"``).
    """
    if not outcome or outcome == "all":
        return [t.namespace for t in tasks]
    target = outcome.upper()
    target = "NOT SCORED" if target == "UNSCORED" else target
    return [t.namespace for t in tasks if (t.primary_outcome or "NOT SCORED") == target]


def _nav_neighbors(
    ordered: list[str],
    current: str,
) -> tuple[str | None, str | None]:
    """Return (prev_ns, next_ns) within *ordered* list around *current*."""
    try:
        idx = ordered.index(current)
    except ValueError:
        return None, None
    prev_ns = ordered[idx - 1] if idx > 0 else None
    next_ns = ordered[idx + 1] if idx < len(ordered) - 1 else None
    return prev_ns, next_ns


def _outcome_counts(tasks: list[BenchTask]) -> dict[str, int]:
    passed = sum(1 for t in tasks if t.primary_outcome == "PASSED")
    failed = sum(1 for t in tasks if t.primary_outcome == "FAILED")
    unscored = sum(1 for t in tasks if t.primary_outcome is None or t.primary_outcome == "NOT SCORED")
    return {"total": len(tasks), "passed": passed, "failed": failed, "unscored": unscored}


VALID_OUTCOMES = frozenset({"all", "failed", "passed", "unscored"})


_OUTCOME_PARAM_MAP: dict[str, str] = {
    "PASSED": "passed",
    "FAILED": "failed",
    "NOT SCORED": "unscored",
}


def _outcome_from_primary(primary: str | None) -> str:
    """Map a task's ``primary_outcome`` value to a filter param.

    ``"PASSED"`` → ``"passed"``, ``"FAILED"`` → ``"failed"``,
    ``"NOT SCORED"`` or ``None`` → ``"unscored"``.
    """
    if primary:
        mapped = _OUTCOME_PARAM_MAP.get(primary)
        if mapped:
            return mapped
    return "unscored"


def _normalize_outcome(raw: str | None) -> str:
    """Normalize *raw* to one of ``all``, ``failed``, ``passed``, ``unscored``.

    Invalid or missing values default to ``"all"``.
    """
    if raw and raw.strip() in VALID_OUTCOMES:
        return raw.strip()
    return "all"


def default_bench_run_provider() -> BenchRunCatalog:
    """Create a BenchRunCatalog from environment variables.

    Reads:
    - ``MENHIR_BENCH_RESULTS_ROOT`` — required. Path to results directory.
    - ``MENHIR_BENCH_ACTIVE_RUN_ID`` — optional active run ID.

    Returns a catalog that reports empty results when the env var is not set,
    so callers can display a clear configuration message.
    """
    global _CACHED_CATALOG
    if _CACHED_CATALOG is not None:
        return _CACHED_CATALOG

    results_root = os.environ.get("MENHIR_BENCH_RESULTS_ROOT")
    if not results_root or not results_root.strip():
        logger.warning(
            "MENHIR_BENCH_RESULTS_ROOT is not set. Benchmark catalog will be empty. "
            "Set this env var to the archolith-bench results directory."
        )
        _CACHED_CATALOG = BenchRunCatalog(results_root=None, active_run_id=None)
        return _CACHED_CATALOG

    active_run_id = os.environ.get("MENHIR_BENCH_ACTIVE_RUN_ID")
    if active_run_id and not active_run_id.strip():
        active_run_id = None

    _CACHED_CATALOG = BenchRunCatalog(results_root=results_root, active_run_id=active_run_id)
    return _CACHED_CATALOG
