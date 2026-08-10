"""Event-history consolidation runner — backfill grounded categorical occurrences from :TurnEvidence.

This is the Phase-3 event-history backfill boundary. It turns canonical user :TurnEvidence rows into
durable ``TypedEventAssertion``s via the existing event-history perception seam, then rebuilds the
affected event-lane timeline Views through the deterministic ``EventHistoryService``.

It wires up NOTHING: no settings/flag, no runtime/scheduler/endpoint, and no repository/View writes
beyond the persistence + lane-rebuild delegates it drives. It is deliberately generic and
offline-only. The scheduler facade owns wiring; this module only executes the consolidated pipeline.

## The fail-closed spine

A page only advances the :EventConsolidationWatermark cursor when the WHOLE page succeeded:

* structural model output (malformed JSON, wrong shape, bad/duplicate/missing/mismatched episode
  envelope) fails the page and NEVER advances;
* a rejected persist (missing ``assertion_id``, ``binding_pending``, ``binding_mismatch``) or any
  exception leaves the cursor dirty;
* an incomplete lane rebuild leaves the cursor dirty;
* a page with no orderable ``cursor_at`` fails and does not advance.

A structurally valid page with zero admitted events is successful and advances. Exact replay is okay.
Only self-subject events are admitted (exact normalized tokens); the LLM's free-text domain is
deliberately ignored this lane to avoid lane fragmentation.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from menhir.domain.event_history import EventLane
from menhir.services import perception
from menhir.services.event_history_perception import (
    build_event_assertion,
    extract_events_once,
)

logger = logging.getLogger(__name__)


class CountingLlm(Protocol):
    """Callable LLM seam whose invocation count is shared across consolidation phases."""

    calls: int

    def __call__(self, system: str, user: str) -> str: ...


class EventConsolidationGraph(Protocol):
    """Graph capabilities used directly by event consolidation."""

    def list_event_dirty_evidence_namespaces(
        self,
        *,
        perceiver_version: str,
        limit: int,
    ) -> list[str]: ...

    def load_next_event_evidence_batch(
        self,
        namespace: str,
        *,
        perceiver_version: str,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def advance_event_cursor(
        self,
        namespace: str,
        *,
        cursor_at: str,
        cursor_uuid: str,
        perceiver_version: str,
        at: str,
    ) -> None: ...

    def activate_event_history(self) -> dict[str, Any]: ...

    def ensure_self_entity(self, namespace: str) -> str: ...

    def record_typed_event_assertion(self, assertion: Any) -> dict[str, Any]: ...

    def event_history_service(self) -> Any: ...


@dataclass(frozen=True)
class EventConsolidationConfig:
    """Event-only settings passed through the scheduler facade."""

    perceiver_version: str = "v1"
    batch_size: int = 500


#: Exact, normalized self-subject tokens admitted as the speaker (the single canonical subject).
_SELF_TOKENS = frozenset({"user", "the user", "i", "me", "myself", "speaker"})

#: Structural model-output defects that invalidate the ENTIRE page (never advance the cursor).
_STRUCTURAL_DROPS = frozenset(
    {
        "malformed_json",
        "not_a_json_array",
        "bad_episode_envelope",
        "duplicate_episode_envelope",
        "missing_episode_envelope",
        "episode_envelope_mismatch",
    }
)


async def select_event_targets(
    graph_adapter: EventConsolidationGraph,
    namespaces: list[str] | None,
    perceiver_version: str,
    max_namespaces: int,
) -> list[str]:
    """Snapshot event-dirty namespaces before the blocking consolidation worker starts.

    Explicit ``namespaces`` form a direct allowlist; otherwise the discovery query returns the
    event-dirty namespaces bounded by ``max_namespaces``.
    """
    if namespaces is not None:
        return list(namespaces)
    return await asyncio.to_thread(
        graph_adapter.list_event_dirty_evidence_namespaces,
        perceiver_version=perceiver_version,
        limit=max_namespaces,
    )


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_event_consolidation(
    graph_adapter: EventConsolidationGraph,
    *,
    event_targets: list[str],
    counting_llm: CountingLlm,
    call_budget: int | None,
    config: EventConsolidationConfig,
) -> dict[str, object]:
    """Run the event-history consolidation pipeline over the given target namespaces with the shared
    LLM budget. Returns bounded, generic metrics (no raw user content).

    ``event_namespaces_processed`` counts a namespace as processed whenever ANY page was attempted or
    touched for it (including a page that then failed closed), NOT only namespaces whose pages fully
    advanced. A namespace is only ever left untouched when budget gating prevented its first load.
    ``event_namespaces_failed`` counts namespaces that failed closed (page load, extraction, build,
    persist, rebuild, cursor, or activation). On activation/service-creation failure no namespace is
    processed, so ``event_namespaces_failed`` reflects ALL supplied ``event_targets``."""
    start_calls = counting_llm.calls

    event_namespaces_processed = 0
    event_namespaces_failed = 0
    event_batches = 0
    event_proposals = 0
    event_assertions_recorded = 0
    event_assertions_created = 0
    event_views_rebuilt = 0
    event_drop_reasons: Counter[str] = Counter()
    event_errors: Counter[str] = Counter()

    event_history_service = None
    try:
        graph_adapter.activate_event_history()
        event_history_service = graph_adapter.event_history_service()
    except Exception as exc:  # noqa: BLE001 - activation failure fails closed, no cursor advance
        logger.warning(
            "event-history activation refused; event consolidation disabled "
            "(namespaces stay event-dirty for retry): %s",
            exc,
        )
        event_errors["activation_failed"] += 1
        event_namespaces_failed = len(event_targets)

    if event_history_service is not None:
        for namespace in event_targets:
            if call_budget is not None and counting_llm.calls >= call_budget:
                break
            namespace_touched = False
            namespace_failed = False
            self_uuid: str | None = None
            self_resolved = False
            while True:
                if call_budget is not None and counting_llm.calls >= call_budget:
                    break
                try:
                    rows = graph_adapter.load_next_event_evidence_batch(
                        namespace,
                        perceiver_version=config.perceiver_version,
                        limit=config.batch_size,
                    )
                except Exception as exc:  # noqa: BLE001 - load failure fails the namespace closed
                    namespace_touched = True
                    namespace_failed = True
                    event_errors["load_failed"] += 1
                    logger.warning(
                        "event evidence batch load failed for %s: %s", namespace, exc
                    )
                    break
                if not rows:
                    break
                event_batches += 1
                namespace_touched = True

                # Build episodes from raw trimmed user content ONLY (no date prefix); preserve the
                # exact source text for quote grounding. Collapse exact duplicates by (body,
                # valid_at) — first wins — but still advance over the ORIGINAL row page after success.
                episodes, valid_at_by_uuid = _build_episodes(rows)
                page_ok = True
                page_drops: Counter[str] = Counter()

                def _record_drop(reason: str) -> None:
                    page_drops[reason] += 1

                proposals: list[Any] = []
                if episodes:
                    try:
                        proposals = extract_events_once(
                            episodes, counting_llm, on_drop=_record_drop
                        )
                        event_proposals += len(proposals)
                    except Exception as exc:  # noqa: BLE001 - extraction failure fails the page
                        page_ok = False
                        event_errors["extraction_failed"] += 1
                        logger.warning(
                            "event extraction failed for %s: %s", namespace, exc
                        )

                if any(r in _STRUCTURAL_DROPS for r in page_drops):
                    page_ok = False
                    event_errors["structural_output"] += 1

                if page_ok:
                    # Admit only exact normalized self-subject tokens; canonicalize to 'user' and
                    # drop the LLM's free-text domain this lane (avoid lane fragmentation).
                    admitted: list[Any] = []
                    for prop in proposals:
                        subject = (
                            str(getattr(prop, "subject_text", "") or "").strip().lower()
                        )
                        if subject in _SELF_TOKENS:
                            admitted.append(
                                replace(prop, subject_text="user", domain=None)
                            )
                        else:
                            _record_drop("non_self_subject")

                    if admitted and not self_resolved:
                        try:
                            resolved = graph_adapter.ensure_self_entity(namespace)
                            self_uuid = str(resolved or "")
                            self_resolved = True
                            if not self_uuid:
                                page_ok = False
                                event_errors["self_resolve_failed"] += 1
                        except Exception as exc:  # noqa: BLE001 - binding failure fails closed
                            page_ok = False
                            event_errors["self_resolve_failed"] += 1
                            logger.warning(
                                "ensure_self_entity failed for %s: %s", namespace, exc
                            )

                if page_ok:
                    assertions: list[Any] = []
                    for prop in admitted:
                        try:
                            result = build_event_assertion(
                                prop,
                                subject_uuid=self_uuid or "",
                                namespace=namespace,
                                learned_at=_now_utc(),
                                episode_reference_time=valid_at_by_uuid.get(
                                    prop.episode_uuid
                                ),
                                turn_evidence_uuid=prop.episode_uuid,
                                perceiver_version=config.perceiver_version,
                            )
                        except Exception as exc:  # noqa: BLE001 - build failure fails the page
                            page_ok = False
                            event_errors["build_failed"] += 1
                            logger.warning(
                                "event assertion build failed for %s: %s",
                                namespace,
                                exc,
                            )
                            break
                        if not result.built:
                            _record_drop("no_valid_time")
                            continue
                        assertions.append(result.assertion)

                recorded: list[Any] = []
                if page_ok:
                    for assertion in assertions:
                        try:
                            res = graph_adapter.record_typed_event_assertion(assertion)
                        except Exception as exc:  # noqa: BLE001 - persist failure fails the page
                            page_ok = False
                            event_errors["record_failed"] += 1
                            logger.warning(
                                "record_typed_event_assertion failed: %s", exc
                            )
                            break
                        assertion_id = str(res.get("assertion_id") or "")
                        if (
                            not assertion_id
                            or res.get("binding_pending")
                            or res.get("binding_mismatch")
                        ):
                            page_ok = False
                            event_errors["persist_rejected"] += 1
                            break
                        recorded.append(assertion)
                        event_assertions_recorded += 1
                        if res.get("created"):
                            event_assertions_created += 1

                if page_ok and recorded:
                    lanes = _dedup_lanes(recorded, namespace)
                    try:
                        rebuild = event_history_service.rebuild_lanes(lanes)
                    except Exception as exc:  # noqa: BLE001 - rebuild failure fails the page
                        page_ok = False
                        event_errors["rebuild_failed"] += 1
                        logger.warning(
                            "event lane rebuild failed for %s: %s", namespace, exc
                        )
                    else:
                        if not rebuild.get("complete"):
                            page_ok = False
                            event_errors["rebuild_incomplete"] += 1
                        else:
                            event_views_rebuilt += int(rebuild.get("lane_count", 0))

                event_drop_reasons.update(page_drops)

                if not page_ok:
                    namespace_failed = True
                    break

                final_row = rows[-1]
                cursor_at = str(final_row.get("cursor_at") or "")
                if not cursor_at:
                    page_ok = False
                    event_errors["no_cursor_at"] += 1
                    namespace_failed = True
                    break

                try:
                    graph_adapter.advance_event_cursor(
                        namespace,
                        cursor_at=cursor_at,
                        cursor_uuid=str(final_row.get("uuid") or ""),
                        perceiver_version=config.perceiver_version,
                        at=_now_utc(),
                    )
                except Exception as exc:  # noqa: BLE001 - cursor failure marks namespace failed
                    namespace_failed = True
                    event_errors["cursor_advance_failed"] += 1
                    logger.warning(
                        "event cursor advance failed for %s: %s", namespace, exc
                    )
                    break
                if len(rows) < config.batch_size:
                    break

            if namespace_touched:
                event_namespaces_processed += 1
            if namespace_failed:
                event_namespaces_failed += 1

    return {
        "event_namespaces_dirty": len(event_targets),
        # 'processed' == any page attempted/touched for the namespace, not fully advanced
        "event_namespaces_processed": event_namespaces_processed,
        "event_namespaces_failed": event_namespaces_failed,
        "event_batches": event_batches,
        "event_proposals": event_proposals,
        "event_assertions_recorded": event_assertions_recorded,
        "event_assertions_created": event_assertions_created,
        "event_views_rebuilt": event_views_rebuilt,
        "event_drop_reasons": dict(event_drop_reasons),
        "event_errors": dict(event_errors),
        "event_llm_calls": counting_llm.calls - start_calls,
    }


def _build_episodes(
    rows: list[dict[str, Any]],
) -> tuple[list[perception.Episode], dict[str, str | None]]:
    """Build deduplicated episodes from raw rows and a uuid -> valid_at reference-time map.

    Episodes carry raw trimmed user content ONLY (no date prefix), capped to the first 2000 chars like
    the scalar consolidation, preserving the exact prefix used for quote grounding. Exact duplicates by
    (body, valid_at) are collapsed — first wins. The
    reference-time map keeps EVERY row (including duplicates) so assertions can still ground to the
    originating row's world time.
    """
    episodes: list[perception.Episode] = []
    seen: set[tuple[str, str]] = set()
    valid_at_by_uuid: dict[str, str | None] = {}
    for row in rows:
        uuid = str(row.get("uuid") or "")
        body = str(row.get("content") or "").strip()[:2000]
        valid_at = str(row.get("valid_at") or "") or None
        valid_at_by_uuid[uuid] = valid_at
        key = (body, valid_at or "")
        if key in seen:
            continue
        seen.add(key)
        episodes.append(perception.Episode(uuid=uuid, content=body))
    return episodes, valid_at_by_uuid


def _dedup_lanes(assertions: list[Any], namespace: str) -> list[EventLane]:
    """Collect deduplicated :EventLane values from the persisted assertions (one per lane key)."""
    lanes: list[EventLane] = []
    seen: set[tuple[str, str, str, str]] = set()
    for assertion in assertions:
        lane = EventLane(
            subject_uuid=str(getattr(assertion, "subject_uuid", "") or ""),
            predicate=str(getattr(assertion, "predicate", "") or ""),
            namespace=namespace,
            domain=getattr(assertion, "domain", None),
        )
        if lane.key in seen:
            continue
        seen.add(lane.key)
        lanes.append(lane)
    return lanes


__all__ = [
    "CountingLlm",
    "EventConsolidationConfig",
    "EventConsolidationGraph",
    "run_event_consolidation",
    "select_event_targets",
]
