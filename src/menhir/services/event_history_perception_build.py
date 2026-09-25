"""Event-history perception — pure proposal -> durable assertion builder.

Turns a grounded ``EventPerceptionProposal`` plus its resolved subject and temporal context into a
durable ``TypedEventAssertion``, resolving ``valid_at`` precision-first (explicit world time, then
episode reference time — never ingest/``learned_at`` time) and abstaining when neither source nor
world time is usable. Pure; does not persist or project."""

from __future__ import annotations

from dataclasses import dataclass

from menhir.domain.event_history import TypedEventAssertion
from menhir.domain.temporal import parse_iso8601
from menhir.services.event_history_perception_parse import EventPerceptionProposal


def _resolve_event_valid_time(
    when: str | None, episode_reference_time: str | None,
) -> "tuple[str | None, str | None]":
    """Choose (valid_at, time_basis) for the assertion, precision-first and NEVER using ingest time:
      * an explicit validated ``when`` (from admission) -> (when, 'explicit');
      * else the episode's own reference time when it parses -> (that time, 'episode_reference');
      * else -> (None, None): abstention, because neither source nor world time is usable.

    ``learned_at`` is deliberately NOT a fallback — event authority orders by world/source time only,
    and an ungrounded ingest stamp must never masquerade as an occurrence time."""
    if when:
        return when, "explicit"
    if episode_reference_time:
        dt = parse_iso8601(episode_reference_time)
        if dt is not None:
            return episode_reference_time, "episode_reference"
    return None, None


@dataclass(frozen=True)
class EventAssertionBuildResult:
    """Pure builder receipt. ``assertion`` is None EXACTLY when the builder abstained (no usable
    world/source time); ``reason`` explains that abstention and is None on success, so a caller can
    inspect and distinguish ``no_valid_time`` from any future abstention reason without string-matching
    prose. ``built`` is True only when an assertion was produced."""

    assertion: TypedEventAssertion | None
    reason: str | None = None

    @property
    def built(self) -> bool:
        return self.assertion is not None


def build_event_assertion(
    proposal: EventPerceptionProposal, *,
    subject_uuid: str, namespace: str | None, learned_at: str,
    episode_reference_time: str | None, turn_evidence_uuid: str | None,
    perceiver_version: str,
) -> EventAssertionBuildResult:
    """Build the durable ``TypedEventAssertion`` from a proposal + its resolved subject and temporal
    context. Returns an ``EventAssertionBuildResult``: ``built`` True with the assertion on success, or
    ``built`` False with an inspectable ``reason`` (``no_valid_time``) on explicit abstention.

    ``valid_at`` uses the proposal's explicit world time when present (``explicit``), else the
    episode reference time (``episode_reference``). ``learned_at`` is NEVER used as ``valid_at``. If
    neither source nor world time is usable the builder abstains and returns no authority-eligible
    data rather than creating it. Evidence tier is forced to ``agent`` (the lowest, advisory tier) —
    probabilistic extraction can never grant itself authority. Pure; does not persist or project. The
    proposal's span/episode flow through unchanged, so the assertion's binding-stable ``source_key`` is
    IDENTICAL to the one the proposal predicted."""
    valid_at, time_basis = _resolve_event_valid_time(proposal.when, episode_reference_time)
    if valid_at is None:
        return EventAssertionBuildResult(None, "no_valid_time")
    assertion = TypedEventAssertion(
        subject_uuid=subject_uuid,
        subject_display=proposal.subject_text,
        predicate=proposal.predicate,
        object_key=proposal.object_key,
        object_display=proposal.object_display,
        valid_at=valid_at,
        learned_at=learned_at,
        stated_span=proposal.stated_span,
        episode_uuid=proposal.episode_uuid,
        span_start=proposal.span_start,
        span_end=proposal.span_end,
        claim_ordinal=proposal.claim_ordinal,
        domain=proposal.domain,
        namespace=namespace,
        turn_evidence_uuid=turn_evidence_uuid,
        time_basis=time_basis,
        evidence_tier="agent",
        perceiver_version=perceiver_version,
    )
    return EventAssertionBuildResult(assertion)
