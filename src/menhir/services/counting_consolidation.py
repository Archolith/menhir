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
- Each namespace is claimed with a fenced lease (claim, renew per page, release); a lost fence stops
  that namespace. Prior-version occurrences are retired and their Views rebuilt before perception.
- sweep_counting_flag_state rebuilds Views after the counting flag flips, with no LLM calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from uuid import uuid4

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
        perceiver_version: str, at: str, run_id: str, incarnation: str, epoch: int,
    ) -> None: ...

    def claim_counting_namespace(
        self, namespace: str, *, run_id: str, lease_seconds: int, now: str,
    ) -> dict[str, Any] | None: ...

    def renew_counting_claim(
        self, namespace: str, *, run_id: str, incarnation: str, epoch: int,
        lease_seconds: int, now: str,
    ) -> bool: ...

    def release_counting_claim(
        self, namespace: str, *, run_id: str, incarnation: str,
    ) -> None: ...

    def retire_prior_version_occurrences(
        self, namespace: str, turn_ids: list[str], *, perceiver_version: str,
        run_id: str, incarnation: str, epoch: int,
    ) -> list[str]: ...

    def mark_counting_flag_state(
        self, namespace: str, *, enabled: bool, at: str, create_only: bool = False,
    ) -> None: ...

    def counting_flag_sweep_targets(
        self, *, enabled: bool, namespaces: list[str] | None, limit: int,
    ) -> list[str]: ...

    def counting_sweep_subjects(
        self, namespace: str, *, limit: int, after: str = "",
    ) -> list[str]: ...

    def scalar_claims_for_turns(
        self, namespace: str, turn_ids: list[str], *, limit: int,
    ) -> list[dict[str, Any]]: ...

    def scalar_state_service(
        self, *, scalar_history_enabled: bool = False, counting_enabled: bool | None = None,
    ) -> Any: ...


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
    #: per-namespace claim lease; renewed after every page.
    lease_seconds: int = 900


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

    from menhir.infrastructure.turn_evidence_repository import CountingFenceLost
    from menhir.infrastructure.typed_assertion_repository import ScalarStateActivationError
    from menhir.services.typed_scalar_perception import (
        ScalarStateNotActivatedError,
        TypedScalarPerceptionService,
    )

    start_calls = counting_llm.calls
    occurrences_stored = 0
    advisory = 0
    processed = 0
    failed = 0
    busy = 0
    fenced = 0
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
        run_id = uuid4().hex
        for namespace in targets:
            if call_budget is not None and counting_llm.calls >= call_budget:
                break
            claim = graph_adapter.claim_counting_namespace(
                namespace, run_id=run_id, lease_seconds=config.lease_seconds,
                now=datetime.now(timezone.utc).isoformat())
            if claim is None:
                busy += 1
                continue
            incarnation = str(claim["incarnation"])
            epoch = int(claim["epoch"])
            namespace_advanced = False
            try:
                # Create-only: a marker left at False by a failed re-enable sweep must stay so the
                # sweep retries; only a completed sweep flips it.
                graph_adapter.mark_counting_flag_state(
                    namespace, enabled=True, at=datetime.now(timezone.utc).isoformat(),
                    create_only=True)
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
                    turn_ids = [str(row["uuid"]) for row in rows]
                    # Retire this turn set's prior-version occurrences and rebuild the affected
                    # Views before perceiving, so no View keeps counting a retired head.
                    retired_subjects = graph_adapter.retire_prior_version_occurrences(
                        namespace, turn_ids,
                        perceiver_version=config.perceiver_version,
                        run_id=run_id, incarnation=incarnation, epoch=epoch,
                    )
                    for subject in retired_subjects:
                        _rebuild_subject(state_service, subject, namespace,
                                         history=config.scalar_history_enabled)

                    def _fence(ns: str = namespace, inc: str = incarnation, ep: int = epoch) -> None:
                        # Renew-as-check: refuses unless this run still owns the same watermark.
                        if not graph_adapter.renew_counting_claim(
                            ns, run_id=run_id, incarnation=inc, epoch=ep,
                            lease_seconds=config.lease_seconds,
                            now=datetime.now(timezone.utc).isoformat(),
                        ):
                            raise CountingFenceLost(f"counting claim for {ns} was lost")

                    # Fence check + fresh lease before the slow LLM step, then again before every
                    # occurrence write (write_guard) so a lease lost mid-LLM stores nothing.
                    _fence()
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
                            write_guard=_fence,
                        )
                        occurrences_stored += int(perception.get("bound", 0))
                        advisory += int(perception.get("advisory", 0))
                    batches += 1

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
                        run_id=run_id, incarnation=incarnation, epoch=epoch,
                    )
                    namespace_advanced = True
                    if not graph_adapter.renew_counting_claim(
                        namespace, run_id=run_id, incarnation=incarnation, epoch=epoch,
                        lease_seconds=config.lease_seconds,
                        now=datetime.now(timezone.utc).isoformat(),
                    ):
                        raise CountingFenceLost(
                            f"counting claim for {namespace} was lost before renewal")
                    if len(rows) < config.batch_size:
                        break
            except CountingFenceLost as exc:
                fenced += 1
                logger.warning("counting: namespace %s fenced; stopping it: %s", namespace, exc)
            except Exception:
                failed += 1
                logger.exception("counting: namespace %s failed; cursor left as last advanced",
                                 namespace)
            finally:
                try:
                    graph_adapter.release_counting_claim(
                        namespace, run_id=run_id, incarnation=incarnation)
                except Exception:
                    logger.exception("counting: releasing claim for %s failed", namespace)
            if namespace_advanced:
                processed += 1

    return {
        "counting_namespaces_dirty": len(targets),
        "counting_namespaces_processed": processed,
        "counting_namespaces_failed": failed,
        "counting_namespaces_busy": busy,
        "counting_namespaces_fenced": fenced,
        "counting_batches": batches,
        "counting_occurrences_stored": occurrences_stored,
        "counting_advisory": advisory,
        "counting_llm_calls": counting_llm.calls - start_calls,
    }


def _rebuild_subject(state_service: Any, subject: str, namespace: str, *, history: bool) -> None:
    """Rebuild one subject's projections; history Views too when that lane is on, since they also
    list occurrences."""
    if history:
        state_service.rebuild_scalar_projections(subject, namespace=namespace, history_enabled=True)
    else:
        state_service.rebuild_scalar_state(subject, namespace=namespace)


def sweep_counting_flag_state(
    graph_adapter: CountingConsolidationGraph,
    *,
    enabled: bool,
    namespaces: list[str] | None,
    scalar_history_enabled: bool,
    limit: int = 50,
    subject_limit: int = 500,
) -> dict[str, int]:
    """Rebuild Views for namespaces whose counting flag marker disagrees with the current flag.
    Subjects are paged (`subject_limit` per page) until exhausted; the marker is set only after
    every page rebuilt, so a crash leaves it for the next pass. Makes no LLM calls."""
    swept = 0
    rebuilt = 0
    failed = 0
    targets = graph_adapter.counting_flag_sweep_targets(
        enabled=enabled, namespaces=namespaces, limit=limit)
    for namespace in targets:
        try:
            svc = graph_adapter.scalar_state_service(
                scalar_history_enabled=scalar_history_enabled, counting_enabled=enabled)
            after = ""
            while True:
                subjects = graph_adapter.counting_sweep_subjects(
                    namespace, limit=subject_limit, after=after)
                for subject in subjects:
                    _rebuild_subject(svc, subject, namespace, history=scalar_history_enabled)
                    rebuilt += 1
                if len(subjects) < subject_limit:
                    break
                after = subjects[-1]
            graph_adapter.mark_counting_flag_state(
                namespace, enabled=enabled, at=datetime.now(timezone.utc).isoformat())
            swept += 1
        except Exception:
            failed += 1
            logger.exception("counting: flag sweep failed for %s", namespace)
    return {
        "counting_sweep_namespaces": swept,
        "counting_sweep_subjects_rebuilt": rebuilt,
        "counting_sweep_failed": failed,
    }


__all__ = [
    "COUNTING_CLAIM_ORDINAL",
    "COUNTING_SYSTEM_PROMPT",
    "CountingConsolidationConfig",
    "CountingConsolidationGraph",
    "build_counting_context",
    "run_counting_consolidation",
    "sweep_counting_flag_state",
]
