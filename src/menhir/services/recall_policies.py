"""Pure recall ranking, authority, and temporal policy helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from menhir.services.ingest_service import IngestService

from menhir.domain.models import FreshnessState, NodeScope, ProcessingState
from menhir.domain.truth.kinds import DIVERSITY_FAMILY as _FRONTIER_DIVERSITY_FAMILY
from menhir.domain.event_time import event_time_text
from menhir.domain.namespace import namespace_to_group_ids, stamped_namespace
from menhir.domain.recall import (
    CandidateData,
    QueryPreset,
    RecallResult,
    RetrievalScoreKind,
    ScalarAuthorityContributor,
    ScalarAuthorityVerdict,
    ScoredMemory,
    TemporalFact,
)
from menhir.domain.retrieval_tuning import (
    SOURCE_PRIORS,
    CandidateSource,
    RetrievalTuningConfig,
)
from menhir.domain.retrieval_trace_models import (
    AssertionShadowRow,
    AssertionShadowTrace,
    FacetShadowRow,
    FacetShadowTrace,
    RelevanceBreakdown,
    RetrievalTrace,
    ScoringTrace,
    ViewReachability,
)
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.services.hybrid_retrieval import FusionLane, weighted_rrf_multi

if TYPE_CHECKING:
    from menhir.core.bootstrap import UnavailableGraphitiClient
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.telemetry import record_mcp_event
from menhir.domain.utils import days_ago
from datetime import datetime, timezone
from menhir.domain.temporal import parse_iso8601
from menhir.services.scheduler_protocols import LifecycleServiceProtocol
from menhir.services.scoring_service import (
    GRAPHITI_RRF_DUAL_METHOD_MAX,
    MIN_SIMILARITY_THRESHOLD,
    ScoringService,
)
from menhir.domain.git_staleness import BeliefCommitContext, derive_structural_staleness
from menhir.services.change_log_provider import CachedGitChangeLog, ChangeLogProvider
from menhir.infrastructure.paths import repo_root_for_project

logger = logging.getLogger(__name__)


def _frontier_trace_enabled() -> bool:
    """MENHIR_FRONTIER_TRACE=1 logs the per-candidate warden admit/refuse breakdown
    at each frontier gating pass — the diagnostic for 'why did recall return empty'."""
    return os.getenv("MENHIR_FRONTIER_TRACE", "").strip().lower() in ("1", "true", "yes")


# Lenses for which fact-edge POINTER hydration fires. Fact edges are indexes INTO
# memory, not memories themselves: they earn their keep only on episodic/personal-
# recall queries ("what was the first issue I had"), where the answer is a lived past
# event. On a current/code query, unconditional edge-endpoint injection just displaces
# better node-search hits (measured -1/30 on the oracle slice), so gate it here.
_FACT_EDGE_POINTER_LENSES: frozenset[str] = frozenset({"historical"})

_ORACLE_RRF_SCORE_KINDS: frozenset[RetrievalScoreKind] = frozenset({
    RetrievalScoreKind.GRAPHITI_RRF,
    RetrievalScoreKind.WEIGHTED_RRF_NORMALIZED,
    RetrievalScoreKind.FACT_EDGE_RRF,
})


def _oracle_similarity(value: float, score_kind: RetrievalScoreKind | str | None) -> float:
    """Map a retrieval relevance score onto SemanticOracle's [0, 1] contract.

    Graphiti and Menhir weighted-RRF scores share a pinned [0, 2] ceiling. Passing
    them through SemanticOracle's generic clamp collapsed every value above 1.0 into
    a tie. Normalize only at this boundary; SOURCE_PRIOR is already [0, 1].
    """
    try:
        kind = score_kind if isinstance(score_kind, RetrievalScoreKind) else RetrievalScoreKind(score_kind)
    except (TypeError, ValueError):
        kind = RetrievalScoreKind.GRAPHITI_RRF
    normalized = value / GRAPHITI_RRF_DUAL_METHOD_MAX if kind in _ORACLE_RRF_SCORE_KINDS else value
    return max(0.0, min(1.0, normalized))


def _blend_oracle_order(
    scored: list[ScoredMemory],
    oracle_rank: dict[str, int],
    *,
    oracle_weight: float,
) -> list[ScoredMemory]:
    """Blend Oracle rank as a bounded reciprocal-rank residual over upstream rank."""
    if oracle_weight <= 0.0:
        return scored
    base_rank = {candidate.uuid: rank for rank, candidate in enumerate(scored)}
    missing_oracle_rank = len(oracle_rank)

    def fused(candidate: ScoredMemory) -> tuple[float, int]:
        base = base_rank[candidate.uuid]
        oracle = oracle_rank.get(candidate.uuid, missing_oracle_rank)
        score = (1.0 / (1 + base)) + oracle_weight * (1.0 / (1 + oracle))
        return (-score, base)

    return sorted(scored, key=fused)


def _query_wants_history(query_text: str) -> bool:
    """True when the query resolves to a history-wanting lens, using the same
    deterministic classifier the AssertionPipeline uses (query_intent -> lens). This
    gates fact-edge pointer hydration to episodic/personal-recall queries only."""
    from menhir.domain.query_intent import classify_intent
    from menhir.domain.intent_affinity import task_intents_to_lens

    hits, _ = classify_intent(query_text)
    return task_intents_to_lens([h.intent for h in hits]) in _FACT_EDGE_POINTER_LENSES


def _authority_contributors(payload: dict[str, Any]) -> tuple[ScalarAuthorityContributor, ...]:
    """Hydrate the stable public contributor contract from repository dictionaries."""
    return tuple(
        _hydrate_contributor(row)
        for row in payload.get("contributors", [])
        if row.get("assertion_id")
    )


def _hydrate_contributor(row: dict[str, Any]) -> ScalarAuthorityContributor:
    operation = str(row.get("operation") or "")
    valid_at = str(row["valid_at"]) if row.get("valid_at") is not None else None
    stated_span = str(row.get("stated_span") or "")
    extra: dict[str, Any] = {}
    if operation == "occurrence":
        from menhir.services.occurrence_event_time import occurrence_event_time

        # mentioned_at is the row's own session time; never recorded/ingest time.
        mentioned_at = valid_at if row.get("time_basis") == "episode_reference" else None
        timing = occurrence_event_time(stated_span, row.get("turn_text"), mentioned_at)
        extra = {
            "source_sentence": timing.sentence,
            "event_at": timing.event_at,
            "event_basis": timing.event_basis,
            "mentioned_at": mentioned_at,
        }
    return ScalarAuthorityContributor(
        assertion_id=str(row.get("assertion_id") or ""),
        relation=str(row.get("relation") or "CONTRIBUTOR"),
        operation=operation,
        value=row.get("value"),
        stated_span=stated_span,
        valid_at=valid_at,
        evidence_tier=(
            str(row["evidence_tier"]) if row.get("evidence_tier") is not None else None),
        episode_uuid=(
            str(row["episode_uuid"]) if row.get("episode_uuid") is not None else None),
        **extra,
    )

# Source priors now live in domain/retrieval_tuning.py (SOURCE_PRIORS), keyed by
# CandidateSource. These module-level names are kept as readable aliases for the
# two long-standing injected priors and the empty-search pending fallback.
PENDING_ENTITY_SIMILARITY = SOURCE_PRIORS[CandidateSource.PENDING]
FILE_LINKED_BASELINE_SIMILARITY = SOURCE_PRIORS[CandidateSource.FILE_LINKED]


def _select_candidate_content(meta: dict[str, object], *, preset: QueryPreset) -> str | None:
    """Choose the cheapest useful text payload for retrieval results by preset."""
    summary = meta.get("summary")
    content = meta.get("content")
    if preset == QueryPreset.KNOWLEDGE:
        chosen = summary or content
    else:
        chosen = content or summary
    return str(chosen) if chosen else None


def _belief_markers_from_facts(
    rows: list[dict[str, object]],
) -> dict[str, dict[str, bool]]:
    """Per-uuid belief markers from temporal fact-rows: has-any-temporal, and superseded
    when any fact for that uuid has expired_at set (belief invalidation)."""
    markers: dict[str, dict[str, bool]] = {}
    for row in rows:
        uuid = str(row.get("node_uuid") or "")
        if not uuid:
            continue
        m = markers.setdefault(uuid, {"belief_superseded": False, "belief_has_temporal": True})
        if row.get("expired_at") is not None:
            m["belief_superseded"] = True
    return markers


def _repo_path_for(project: str) -> str | None:
    """Best-effort on-disk repo path for a project, or None."""
    try:
        root = repo_root_for_project(project)
        return str(root) if root is not None else None
    except Exception:
        return None


def _staleness_evidence_for(meta, *, provider, repo_resolver):
    """Best-effort git staleness evidence for one candidate's metadata. () on any miss."""
    anchors = [str(p) for p in (meta.get("anchor_paths") or ())]
    project = meta.get("anchor_project")
    if not anchors or not project:
        return ()
    repo = repo_resolver(str(project))
    if not repo:
        return ()
    commit = meta.get("belief_commit")
    ctx = BeliefCommitContext(commit=str(commit)) if commit else None
    changes = provider.changes(repo, since_commit=(str(commit) if commit else None), paths=anchors)
    verdict = derive_structural_staleness(
        anchors=anchors,
        believed_at=(str(meta.get("created_at")) if meta.get("created_at") else None),
        changes=changes, belief_context=ctx,
    )
    return verdict.evidence


def _filter_to_current_beliefs(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Drop fact-edge rows that are no longer current belief (expired_at set).

    Belief invalidation is `expired_at` (menhir learned it was superseded), NOT
    `invalid_at` (world validity). Used when include_invalidated is False.
    """
    return [r for r in rows if r.get("expired_at") is None]


def _build_temporal_facts(
    rows: list[dict[str, object]],
    *,
    event_time: bool = False,
    ended: bool = False,
    now: datetime | None = None,
    said_time: bool = True,
) -> dict[str, tuple[TemporalFact, ...]]:
    """Group fact-edge rows by node_uuid, cap to 5 most recent, and build TemporalFact tuples.

    Sorts by created_at DESC (None last), caps to 5 most recent per node,
    and assigns temporal_role based on whether expired_at is None.
    With ``ended`` (MENHIR_ANCHORED_TIME_RESOLVER) an unexpired fact whose own end (invalid_at)
    is at or before ``now`` is "ended": still believed, no longer true in the world.
    Returns a dict mapping node_uuid -> tuple of TemporalFact.
    """
    pivot = (now or datetime.now(timezone.utc)) if ended else None
    by_uuid: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        node_uuid = str(row.get("node_uuid") or "")
        if node_uuid:
            by_uuid.setdefault(node_uuid, []).append(row)

    result: dict[str, tuple[TemporalFact, ...]] = {}
    for node_uuid, fact_rows in by_uuid.items():
        # Sort by created_at DESC (None sorts last). With reverse=True, the
        # primary key must be `is not None` (True>False) so dated rows lead and
        # null-date rows fall to the end; coalesce the date so str/None never compare.
        sorted_rows = sorted(
            fact_rows,
            key=lambda r: (
                r.get("created_at") is not None,
                r.get("created_at") or "",
            ),
            reverse=True,
        )
        # Cap to 5 most recent
        capped = sorted_rows[:5]
        facts = []
        for row in capped:
            expired_at = row.get("expired_at")
            is_current = expired_at is None
            temporal_role = "current_belief" if is_current else "superseded_belief"
            if pivot is not None and is_current:
                world_end = parse_iso8601(row.get("invalid_at"))
                if world_end is not None and world_end <= pivot:
                    temporal_role = "ended"
            fact = TemporalFact(
                fact=row.get("fact"),
                valid_at=row.get("valid_at"),
                invalid_at=row.get("invalid_at"),
                created_at=row.get("created_at"),
                expired_at=expired_at,
                is_current_belief=is_current,
                temporal_role=temporal_role,
                event_time=event_time_text(row) if event_time else None,
                # Drop a trailing zone id like [UTC] (Neo4j toString), as event_time does.
                said_at=(str(row.get("said_at")).split("[", 1)[0].strip()
                         if said_time and row.get("said_at") else None),
            )
            facts.append(fact)
        result[node_uuid] = tuple(facts)
    return result


_TIER_FACT_TOKEN = re.compile(r"[a-z0-9]+")
# Anchored binding writes "user" where inferred binding writes "the user"; articles never decide.
_TIER_FACT_IGNORED = frozenset({"a", "an", "the"})


def _tier_fact_tokens(text: object) -> frozenset[str]:
    if not isinstance(text, str):
        return frozenset()
    return frozenset(t for t in _TIER_FACT_TOKEN.findall(text.casefold()) if t not in _TIER_FACT_IGNORED)


def _tier_facts_match(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return False
    return a == b or len(a & b) / len(a | b) > 0.8


def label_and_collapse_self_tiers(
    results: list[ScoredMemory], *, anchored_uuid: str, inferred_uuid: str,
) -> list[ScoredMemory]:
    """Label the two self entities by tier and collapse cross-tier duplicate facts.

    An inferred fact is dropped only when a near-duplicate anchored fact is visible on the
    anchored item of the same result, which is then marked ``corroborated``. Recall-time only.
    """
    anchored_idx = next((i for i, sm in enumerate(results) if sm.uuid == anchored_uuid), None)
    inferred_idx = next((i for i, sm in enumerate(results) if sm.uuid == inferred_uuid), None)
    if anchored_idx is None and inferred_idx is None:
        return results
    out = list(results)
    if anchored_idx is not None:
        out[anchored_idx] = replace(out[anchored_idx], self_tier="anchored")
    if inferred_idx is None:
        return out
    inferred = out[inferred_idx]
    if anchored_idx is None:
        out[inferred_idx] = replace(inferred, self_tier="inferred")
        return out
    anchored_facts = list(out[anchored_idx].temporal_facts)
    anchored_tokens = [_tier_fact_tokens(tf.fact) for tf in anchored_facts]
    kept: list[TemporalFact] = []
    for tf in inferred.temporal_facts:
        tokens = _tier_fact_tokens(tf.fact)
        hits = [i for i, a in enumerate(anchored_tokens) if _tier_facts_match(tokens, a)]
        if not hits:
            kept.append(tf)
            continue
        for i in hits:
            anchored_facts[i] = replace(anchored_facts[i], corroborated=True)
    out[anchored_idx] = replace(out[anchored_idx], temporal_facts=tuple(anchored_facts))
    out[inferred_idx] = replace(inferred, self_tier="inferred", temporal_facts=tuple(kept))
    return out


def _scalar_history_records_change(history_view: dict[str, object]) -> bool:
    """True when a scalar_history View records an actual change: a non-absolute entry, or more
    than one distinct absolute value. A slot that only restates one value is not history and
    must not take an advisory recall slot (#243). Truncated payloads count as changing."""
    if int(history_view.get("omitted_entry_count") or 0) > 0:
        return True
    values: set[str] = set()
    for entry in history_view.get("entries") or []:
        if str(entry.get("operation") or "").strip().lower() != "absolute":
            return True
        values.add(str(entry.get("value")).strip().casefold())
    return len(values) > 1


def _scalar_history_has_occurrences(history_view: dict[str, object]) -> bool:
    """True when a stored scalar_history View lists counting-lane occurrence rows: parsed
    `operation_counts` (fetch_scalar_history) or raw `sh_op_counts` JSON (candidate metadata).
    Such a View was built with counting on; with counting off it is stale and hidden."""
    counts = history_view.get("operation_counts")
    if isinstance(counts, dict) and counts.get("occurrence"):
        return True
    return '"occurrence"' in str(history_view.get("sh_op_counts") or "")


def _scalar_history_evidence_days_ago(history_view: dict[str, object]) -> float:
    """Days since the View's newest entry (source time), for the recency term. An advisory row
    has no access record, so it must not get the just-accessed bonus (#243)."""
    raw = history_view.get("last_valid_at")
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.strip())
        except ValueError:
            raw = None
    return days_ago(raw)


def _render_scalar_history_content(history_view: dict[str, object]) -> str:
    """Render a parsed scalar_history View into advisory recall content.

    Format:
      Advisory scalar history — {attribute} ({scope}) — not an absolute current total
      • {valid_at}: {value} ({operation}) — "{stated_span}"
      ...
      Latest: {value} at {date} ({operation}, not absolute)

    Uses source/world time (valid_at) exclusively — never recorded/ingest time.
    Never sums deltas. Never converts the latest delta to an absolute.
    """
    attr = str(history_view.get("attribute") or "value")
    scope = str(history_view.get("scope") or "")
    op_counts = history_view.get("operation_counts") or {}
    entries = history_view.get("entries") or []
    subject = str(history_view.get("subject") or "user")

    delta_only = set(op_counts.keys()) <= {"delta"}
    scope_txt = f" ({scope})" if scope else ""

    lines: list[str] = []
    # Header with advisory disclaimer.
    header = f"Advisory scalar history — {subject}'s {attr}{scope_txt}"
    if delta_only:
        header += " — not an absolute current total"
    lines.append(header)

    # Entries: chronological, operation + value + source-time + original wording.
    for entry in entries:
        valid_at = str(entry.get("valid_at") or "")[:10]
        operation = str(entry.get("operation") or "")
        value = entry.get("value")
        stated_span = str(entry.get("stated_span") or "").strip()

        prefix = "+" if operation == "delta" else ""
        val_str = f"{prefix}{value}" if value is not None else "?"
        entry_line = f"  {valid_at}: {val_str} ({operation})"
        if stated_span:
            entry_line += f' — "{stated_span}"'
        lines.append(entry_line)

    # Latest entry summary.
    if entries:
        latest = entries[-1]
        lval = latest.get("value")
        ldate = str(latest.get("valid_at") or "")[:10]
        lop = str(latest.get("operation") or "")
        qualifier = ", not absolute" if delta_only else ""
        lines.append(f"Latest: {lval} at {ldate} ({lop}{qualifier})")

    return "\n".join(lines)
