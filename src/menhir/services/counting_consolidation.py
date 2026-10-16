"""Counting-lane runner for personal-memory consolidation (scalar counting plan, unit 3).

The counting lane reads each user turn once, after the scalar lane has finished it, and stores
single happenings as `occurrence` assertions (claim ordinal 1, so a counting claim never shares a
head with a scalar claim on the same span). The scalar fold counts them into a basis="counted" View
only for slots with no stated total.

Order and authority:
- Runs after the scalar phase in the same scheduler pass and shares its call counter and budget, so
  when the budget runs out counting goes without, not scalars.
- Its own :CountingConsolidationWatermark cursor, but the loader only returns turns at or before the
  scalar cursor for the scalar perceiver_version in effect. No scalar cursor, or one from another
  version, returns nothing (pause).
- A page advances the counting cursor only after it ran, exactly like the scalar runner.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from menhir.domain.typed_assertion import COUNTING_OPERATIONS
from menhir.services.scalar_consolidation import CountingLlm

logger = logging.getLogger(__name__)

#: claim ordinal of every counting-lane claim (the scalar lane always uses 0).
COUNTING_CLAIM_ORDINAL = 1

#: bound on the "already captured" context per page, so a dense page cannot blow the prompt.
_CONTEXT_CLAIM_LIMIT = 200

COUNTING_SYSTEM_PROMPT = (
    "You read a user's numbered memory episodes and record SINGLE HAPPENINGS that the user may later "
    "want counted ('how many times did I go to the gym?', 'how much did I spend on concerts?'). "
    "Emit one observation per happening that the episode says ACTUALLY TOOK PLACE: 'I went to the "
    "gym today', 'I visited the museum', 'I bought concert tickets for $80', 'I finished another "
    "book'. Do NOT emit plans, hopes, hypotheticals, questions, or negated happenings ('I'm going to "
    "the gym tomorrow', 'if I visit', 'did I go?', 'I didn't go'). Do NOT emit standing properties, "
    "stated totals, or changes to a total: those are already captured by another reader. The user "
    "message may begin with an ALREADY CAPTURED list of facts another reader stored for these "
    "episodes; never emit a happening for a clause listed there, and when you emit a happening for "
    "the same activity, reuse that list's subject and attribute. "
    "Each observation is an object: "
    "{\"subject\": <who or what the happening is about: 'user' when the user did it ('I went', 'my "
    "run'); a separate object or place when the activity is clearly about it ('I visited the "
    "museum' -> subject 'the museum'); a named third party's name exactly as written when they did "
    "it>, "
    "\"attribute\": <stable snake_case activity family ending in '_count' for a count ('gym_visit_count', "
    "'book_finished_count', 'museum_visit_count') or in '_spend' for money ('concert_spend')>, "
    "\"scope\": <optional differentiator; \"\" if none>, "
    "\"value_kind\": \"count\" for a happening that is counted, or \"money\" for an amount spent, "
    "\"unit\": \"\" for count; the currency code for money ('usd'), "
    "\"operation\": \"occurrence\" (always), "
    "\"value\": for count, 1 for one happening, or the number the clause states for a happening that "
    "occurred several times at once ('I ran twice today' -> 2); for money, the single positive amount "
    "the clause states ('$80' -> 80), "
    "\"importance\": one of \"tracked\" (the user counts or tracks it, or it is a goal, habit, hobby, "
    "health, or money), \"notable\" (a deliberate activity the user may ask about later: trips, "
    "visits, purchases, events), or \"routine\" (everyday trivia: ate lunch, slept, commuted), "
    "\"stated_span\": <the SHORTEST exact source substring that states this happening; keep the quote "
    "identical each time you read the same happening>}. "
    "Rules: one observation per happening, never one per mention of an earlier happening; a later "
    "retelling of the same happening ('like I said, I went to the gym yesterday') is NOT a new one. "
    "Approximate or vague amounts ('a few times', 'about $50') emit NOTHING. Do not do arithmetic. "
    "CONSISTENCY (critical -- each episode is read several times and only agreeing readings are "
    "kept): for a given happening ALWAYS produce the SAME subject, attribute, value_kind, unit, value "
    "and stated_span. "
    "Emit ONLY grounded observations: every object MUST contain subject, attribute, value_kind, "
    "operation, value, importance and stated_span, with stated_span copied verbatim from the episode. "
    "Output ONLY a JSON array with EXACTLY ONE envelope for EVERY numbered episode, in the same "
    "order: {\"episode\": <number>, \"observations\": [<zero or more observation objects>]}. Use an "
    "empty observations array when an episode has no happening. Do not repeat the episode number "
    "inside each observation."
)


class CountingConsolidationGraph(Protocol):
    """Graph capabilities used directly by counting consolidation."""

    def list_counting_dirty_namespaces(
        self, *, perceiver_version: str, scalar_perceiver_version: str, limit: int,
    ) -> list[str]: ...

    def load_next_counting_batch(
        self, namespace: str, *, perceiver_version: str, scalar_perceiver_version: str,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def advance_counting_cursor(
        self, namespace: str, *, cursor_at: str, cursor_uuid: str,
        perceiver_version: str, at: str,
    ) -> None: ...

    def scalar_claims_for_turns(
        self, namespace: str, turn_ids: list[str], *, limit: int,
    ) -> list[dict[str, Any]]: ...

    def scalar_state_service(self, *, scalar_history_enabled: bool = False) -> Any: ...


@dataclass(frozen=True)
class CountingConsolidationConfig:
    """Counting-lane settings passed through the scheduler facade."""

    #: the counting lane's own version; stamps its cursor and its assertions.
    perceiver_version: str = "v1"
    #: the scalar version in effect; counting only reads turns the scalar cursor for it has passed.
    scalar_perceiver_version: str = "v1"
    batch_size: int = 500
    max_namespaces: int = 200
    embed_version: str | None = None
    reconcile_attribute: bool = True
    reconcile_scope: bool = True
    reconcile_subject: bool = True
    canonical_self: bool = False
    scalar_history_enabled: bool = False


def build_counting_context(
    episodes: list[Any], claims: list[dict[str, Any]],
) -> str:
    """The "already captured" block for one page: the scalar claims stored for these turns, keyed by
    the episode number the model sees. Empty when there are none (the user message is then exactly
    the episode log, as for scalars)."""
    index_by_uuid = {str(getattr(e, "uuid", "")): i for i, e in enumerate(episodes)}
    lines: list[str] = []
    for claim in claims:
        idx = index_by_uuid.get(str(claim.get("episode_uuid") or ""))
        if idx is None:
            continue
        value = claim.get("value_json")
        try:
            value = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            pass
        lines.append(
            f"[{idx}] subject={claim.get('subject') or ''!s} "
            f"attribute={claim.get('attribute') or ''!s} "
            f"operation={claim.get('operation') or ''!s} value={value!s} "
            f"quote={json.dumps(str(claim.get('stated_span') or ''))}"
        )
    if not lines:
        return ""
    return "ALREADY CAPTURED (do not count these; reuse these names):\n" + "\n".join(lines)


def run_counting_consolidation(
    graph_adapter: CountingConsolidationGraph,
    *,
    namespaces: list[str] | None,
    counting_llm: CountingLlm,
    build_episodes: Callable[[list[dict[str, Any]]], list[Any]],
    k: int,
    threshold: float,
    call_budget: int | None,
    embed: Callable[[str], list[float] | None] | None,
    config: CountingConsolidationConfig,
) -> dict[str, object]:
    """Run the counting lane over turns the scalar lane has finished, sharing the run's budget."""

    from menhir.infrastructure.typed_assertion_repository import ScalarStateActivationError
    from menhir.services.typed_scalar_perception import (
        ScalarStateNotActivatedError,
        TypedScalarPerceptionService,
    )

    start_calls = counting_llm.calls
    occurrences_stored = 0
    advisory = 0
    processed = 0
    batches = 0
    targets: list[str] = []
    service = None

    try:
        state_service = graph_adapter.scalar_state_service(
            scalar_history_enabled=config.scalar_history_enabled)
        # Router and shadow stay off: both are scalar-lane instruments with no occurrence support.
        service = TypedScalarPerceptionService(
            graph_adapter,
            state_service,
            perceiver_version=config.perceiver_version,
            embed=embed,
            embed_version=config.embed_version,
            scalar_history_enabled=config.scalar_history_enabled,
        )
        service.ensure_activated()
    except (ScalarStateActivationError, ScalarStateNotActivatedError) as exc:
        logger.warning("scalar-state activation refused; counting lane skipped this run: %s", exc)
        service = None

    if service is not None:
        # Listed here, after the scalar phase, so turns scalars finished this pass are counted now.
        if namespaces is not None:
            targets = list(dict.fromkeys(namespaces))
        else:
            targets = graph_adapter.list_counting_dirty_namespaces(
                perceiver_version=config.perceiver_version,
                scalar_perceiver_version=config.scalar_perceiver_version,
                limit=config.max_namespaces,
            )
        for namespace in targets:
            if call_budget is not None and counting_llm.calls >= call_budget:
                break
            namespace_touched = False
            while True:
                if call_budget is not None and counting_llm.calls >= call_budget:
                    break
                rows = graph_adapter.load_next_counting_batch(
                    namespace,
                    perceiver_version=config.perceiver_version,
                    scalar_perceiver_version=config.scalar_perceiver_version,
                    limit=config.batch_size,
                )
                if not rows:
                    break
                episodes = build_episodes(rows)
                if episodes:
                    claims = graph_adapter.scalar_claims_for_turns(
                        namespace, [str(e.uuid) for e in episodes], limit=_CONTEXT_CLAIM_LIMIT)
                    reference_by_uuid = {
                        str(row["uuid"]): (str(row.get("valid_at") or "") or None)
                        for row in rows
                    }
                    perception = service.perceive_and_persist(
                        episodes,
                        counting_llm,
                        k=k,
                        threshold=threshold,
                        namespace=namespace,
                        episode_reference_time=reference_by_uuid.get,
                        reconcile_attribute=config.reconcile_attribute,
                        reconcile_scope=config.reconcile_scope,
                        reconcile_subject=config.reconcile_subject,
                        canonical_self=config.canonical_self,
                        system_prompt=COUNTING_SYSTEM_PROMPT,
                        allowed_operations=COUNTING_OPERATIONS,
                        claim_ordinal=COUNTING_CLAIM_ORDINAL,
                        context=build_counting_context(episodes, claims),
                    )
                    occurrences_stored += int(perception.get("bound", 0))
                    advisory += int(perception.get("advisory", 0))
                batches += 1
                namespace_touched = True

                # Same fail-closed cursor rule as the scalar runner: advance on the monotonic
                # ingestion key only; a wholly timeless page leaves the namespace dirty.
                advanceable = [row for row in rows if str(row.get("cursor_at") or "")]
                if not advanceable:
                    logger.warning(
                        "counting: page for %s has no orderable timestamp; not advancing cursor",
                        namespace,
                    )
                    break
                cursor = advanceable[-1]
                graph_adapter.advance_counting_cursor(
                    namespace,
                    cursor_at=str(cursor["cursor_at"]),
                    cursor_uuid=str(cursor.get("uuid") or ""),
                    perceiver_version=config.perceiver_version,
                    at=datetime.now(timezone.utc).isoformat(),
                )
                if len(rows) < config.batch_size:
                    break
            if namespace_touched:
                processed += 1

    return {
        "counting_namespaces_dirty": len(targets),
        "counting_namespaces_processed": processed,
        "counting_batches": batches,
        "counting_occurrences_stored": occurrences_stored,
        "counting_advisory": advisory,
        "counting_llm_calls": counting_llm.calls - start_calls,
    }


__all__ = [
    "COUNTING_CLAIM_ORDINAL",
    "COUNTING_SYSTEM_PROMPT",
    "CountingConsolidationConfig",
    "CountingConsolidationGraph",
    "build_counting_context",
    "run_counting_consolidation",
]
