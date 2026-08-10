"""Opt-in routing for deterministic scalar extraction.

The router is deliberately small and pure: it validates the extractor contract, partitions the
original episode list in order, and converts admitted deterministic proposals into normal scalar
decisions.  It never calls an LLM, binds, or persists.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Sequence

from menhir.services.deterministic_scalar_extractor import (
    EXTRACTOR_VERSION,
    TEMPLATE_VERSION,
    DeterministicExtraction,
    EpisodeReceipt,
    DeterministicScalarExtractor,
    TEMPLATE_REGISTRY,
)
from menhir.services.typed_scalar_rules import (
    PERCEPTION_EVIDENCE_TIER,
    TypedScalarDecision,
    TypedScalarProposal,
    parse_scalar_row,
)

ROUTE_DETERMINISTIC = "deterministic_scalar"
ROUTE_LLM_REVIEW = "llm_review"
ROUTER_VERSION = "deterministic-router-v1"
ROUTER_REASON_FAILURE = "deterministic_router_contract_failure"
ROUTER_REASON_COMMITTED = "deterministic_scalar_committed"
_CLASS_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
KNOWN_CLASS_IDS = frozenset(template.class_id for template in TEMPLATE_REGISTRY) | {
    "c_correction", "c_prev_current",
}


@dataclass(frozen=True, slots=True)
class EpisodeRoute:
    episode_uuid: str
    episode_index: int
    route: str


@dataclass(frozen=True, slots=True)
class RoutingResult:
    routes: tuple[EpisodeRoute, ...]
    deterministic_proposals: tuple[TypedScalarProposal, ...]
    failure: str | None = None
    class_counts: tuple[tuple[str, int], ...] = ()
    eligible_unpromoted: int = 0
    mixed_class_blocked: int = 0
    promoted_class_ids: tuple[str, ...] = ()
    route_class_counts: tuple[tuple[str, str, int], ...] = ()
    deterministic_class_by_source: tuple[tuple[str, str], ...] = ()

    @property
    def reviewed_episodes(self) -> tuple[str, ...]:
        return tuple(item.episode_uuid for item in self.routes if item.route == ROUTE_LLM_REVIEW)

    @property
    def route_counts(self) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for item in self.routes:
            counts[item.route] = counts.get(item.route, 0) + 1
        return tuple(sorted(counts.items()))

    @property
    def deterministic_episode_count(self) -> int:
        return sum(item.route == ROUTE_DETERMINISTIC for item in self.routes)


class DeterministicScalarRouter:
    """Validate one extractor result and partition episodes fail-closed."""

    version = ROUTER_VERSION

    def __init__(self, promoted_class_ids: Sequence[str] = ()) -> None:
        normalized: list[str] = []
        for value in promoted_class_ids:
            if not isinstance(value, str):
                raise ValueError("promoted_class_id_invalid")
            class_id = value.strip().lower()
            if not _CLASS_ID_RE.fullmatch(class_id) or class_id not in KNOWN_CLASS_IDS:
                raise ValueError("promoted_class_id_unknown")
            if class_id not in normalized:
                normalized.append(class_id)
        self.promoted_class_ids = tuple(sorted(normalized))

    def route(self, episodes: Sequence[Any], extraction: DeterministicExtraction) -> RoutingResult:
        """Validate an injected extraction, replaying the extractor to detect stale/forged data."""
        try:
            if DeterministicScalarExtractor().extract(episodes) != extraction:
                raise ValueError("extraction_replay_mismatch")
        # The injected extraction is an untrusted boundary.  Any ordinary
        # exception raised while replaying it must fail closed; deliberately
        # leave BaseException (KeyboardInterrupt/SystemExit, etc.) alone.
        except Exception as exc:
            return self._failure(episodes, exc)
        return self._route_validated(episodes, extraction)

    def extract_and_route(self, episodes: Sequence[Any]) -> tuple[DeterministicExtraction, RoutingResult]:
        """Extract once and route through the private validated path."""
        extraction = DeterministicScalarExtractor().extract(episodes)
        return extraction, self._route_validated(episodes, extraction)

    def _route_validated(self, episodes: Sequence[Any], extraction: DeterministicExtraction) -> RoutingResult:
        try:
            if (extraction.extractor_version != EXTRACTOR_VERSION
                    or extraction.template_version != TEMPLATE_VERSION):
                raise ValueError("extractor_version_mismatch")
            for class_id in self.promoted_class_ids:
                if class_id not in KNOWN_CLASS_IDS:
                    raise ValueError("promoted_class_id_unknown")
            input_ids = [self._episode_uuid(episode) for episode in episodes]
            if any(not value for value in input_ids) or len(set(input_ids)) != len(input_ids):
                raise ValueError("duplicate_or_missing_input_uuid")
            receipts = tuple(extraction.episode_receipts)
            if len(receipts) != len(episodes):
                raise ValueError("receipt_count_mismatch")
            receipt_by_uuid: dict[str, EpisodeReceipt] = {}
            for index, receipt in enumerate(receipts):
                if not receipt.episode_uuid or receipt.episode_uuid in receipt_by_uuid:
                    raise ValueError("duplicate_or_missing_receipt_uuid")
                if receipt.episode_index != index or receipt.episode_index < 0:
                    raise ValueError("receipt_index_mismatch")
                if receipt.episode_index >= len(input_ids) or input_ids[receipt.episode_index] != receipt.episode_uuid:
                    raise ValueError("receipt_episode_index_uuid_mismatch")
                if receipt.fully_covered != (not bool(receipt.reasons)):
                    raise ValueError("receipt_coverage_mismatch")
                receipt_by_uuid[receipt.episode_uuid] = receipt
            if set(receipt_by_uuid) != set(input_ids):
                raise ValueError("receipt_uuid_mismatch")
            expected_eligible = tuple(
                episode_uuid for episode_uuid in input_ids
                if receipt_by_uuid[episode_uuid].fully_covered
            )
            if tuple(extraction.fully_eligible_episode_uuids) != expected_eligible:
                raise ValueError("eligibility_tuple_mismatch")
            proposals_by_uuid: dict[str, list[TypedScalarProposal]] = {value: [] for value in input_ids}
            seen_sources: set[str] = set()
            for proposal in extraction.proposals:
                if not isinstance(proposal, TypedScalarProposal):
                    raise ValueError("proposal_type_mismatch")
                if proposal.episode_uuid not in proposals_by_uuid or proposal.source_key in seen_sources:
                    raise ValueError("proposal_provenance_mismatch")
                episode_index = input_ids.index(proposal.episode_uuid)
                source = str(getattr(episodes[episode_index], "content", "") or "")
                if (proposal.span_start < 0 or proposal.span_start >= proposal.span_end
                        or proposal.span_end > len(source)
                        or proposal.stated_span != source[proposal.span_start:proposal.span_end]):
                    raise ValueError("proposal_span_mismatch")
                drop_reasons: list[str] = []
                reparsed = parse_scalar_row({
                    "episode": episode_index,
                    "subject": proposal.subject_text,
                    "attribute": proposal.attribute,
                    "scope": proposal.scope,
                    "value_kind": proposal.value_kind,
                    "unit": proposal.unit,
                    "operation": proposal.operation,
                    "value": proposal.value,
                    "when": proposal.when,
                    "stated_span": proposal.stated_span,
                    "display": "",
                }, episodes, drop_reasons.append)
                if reparsed != proposal:
                    raise ValueError("proposal_revalidation_mismatch")
                seen_sources.add(proposal.source_key)
                proposals_by_uuid[proposal.episode_uuid].append(proposal)
            admitted: dict[str, dict[str, TypedScalarProposal]] = {value: {} for value in input_ids}
            for receipt in receipts:
                has_drop = False
                for candidate in receipt.candidate_receipts:
                    if candidate.episode_index != receipt.episode_index:
                        raise ValueError("candidate_episode_index_mismatch")
                    if candidate.class_id not in KNOWN_CLASS_IDS:
                        raise ValueError("extracted_class_id_unknown")
                    if candidate.outcome not in {"admitted", "dropped"}:
                        raise ValueError("candidate_outcome_invalid")
                    if candidate.outcome == "dropped":
                        has_drop = True
                        if (candidate.proposal is not None or not str(candidate.drop_reason or "").strip()
                                or candidate.drop_reason not in receipt.reasons):
                            raise ValueError("dropped_candidate_invalid")
                        continue
                    if not isinstance(candidate.proposal, TypedScalarProposal):
                        raise ValueError("admitted_candidate_missing_proposal")
                    proposal = candidate.proposal
                    if proposal.episode_uuid != receipt.episode_uuid:
                        raise ValueError("candidate_episode_mismatch")
                    if (candidate.source_start != proposal.span_start
                            or candidate.source_end != proposal.span_end
                            or candidate.stated_span != proposal.stated_span):
                        raise ValueError("candidate_span_mismatch")
                    if proposal.source_key in admitted[receipt.episode_uuid]:
                        raise ValueError("duplicate_admitted_source_key")
                    admitted[receipt.episode_uuid][proposal.source_key] = proposal
                if has_drop and receipt.fully_covered:
                    raise ValueError("dropped_candidate_coverage_invalid")
            for episode_uuid, proposals in proposals_by_uuid.items():
                if set(admitted[episode_uuid]) != {proposal.source_key for proposal in proposals}:
                    raise ValueError("proposal_not_admitted_by_receipt")
                if any(admitted[episode_uuid][proposal.source_key] != proposal for proposal in proposals):
                    raise ValueError("proposal_identity_mismatch")
            routes: list[EpisodeRoute] = []
            deterministic: list[TypedScalarProposal] = []
            class_counts: dict[str, int] = {}
            eligible_unpromoted = 0
            mixed_class_blocked = 0
            route_class_counts: dict[tuple[str, str], int] = {}
            deterministic_class_by_source: dict[str, str] = {}
            for index, episode_uuid in enumerate(input_ids):
                receipt = receipt_by_uuid[episode_uuid]
                proposals = proposals_by_uuid[episode_uuid]
                admitted_classes = {
                    candidate.class_id
                    for candidate in receipt.candidate_receipts
                    if candidate.outcome == "admitted" and candidate.proposal is not None
                }
                for class_id in admitted_classes:
                    class_counts[class_id] = class_counts.get(class_id, 0) + 1
                all_promoted = bool(admitted_classes) and admitted_classes.issubset(self.promoted_class_ids)
                if receipt.fully_covered and proposals and all_promoted:
                    route = ROUTE_DETERMINISTIC
                    deterministic.extend(proposals)
                    for proposal in proposals:
                        matching = next(
                            candidate.class_id for candidate in receipt.candidate_receipts
                            if candidate.outcome == "admitted" and candidate.proposal == proposal)
                        deterministic_class_by_source[proposal.source_key] = matching
                else:
                    route = ROUTE_LLM_REVIEW
                    if receipt.fully_covered and proposals and not all_promoted:
                        eligible_unpromoted += 1
                        if len(admitted_classes) > 1:
                            mixed_class_blocked += 1
                for class_id in admitted_classes:
                    key = (route, class_id)
                    route_class_counts[key] = route_class_counts.get(key, 0) + 1
                routes.append(EpisodeRoute(episode_uuid, index, route))
            return RoutingResult(
                tuple(routes), tuple(deterministic), None,
                tuple(sorted(class_counts.items())), eligible_unpromoted,
                mixed_class_blocked, self.promoted_class_ids,
                tuple((route, class_id, count) for (route, class_id), count in sorted(route_class_counts.items())),
                tuple(sorted(deterministic_class_by_source.items())),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            return self._failure(episodes, exc)

    def _failure(self, episodes: Sequence[Any], exc: Exception) -> RoutingResult:
        return RoutingResult(
            tuple(EpisodeRoute(self._episode_uuid(episode), index, ROUTE_LLM_REVIEW)
                  for index, episode in enumerate(episodes)),
            (),
            f"{ROUTER_REASON_FAILURE}:{type(exc).__name__}",
            promoted_class_ids=self.promoted_class_ids,
        )

    @staticmethod
    def _episode_uuid(episode: Any) -> str:
        return str(getattr(episode, "uuid", "") or "").strip()

    @staticmethod
    def _to_decisions(result: RoutingResult) -> list[TypedScalarDecision]:
        if not isinstance(result, RoutingResult) or result.failure is not None:
            return []
        decisions: list[TypedScalarDecision] = []
        try:
            class_by_source = dict(result.deterministic_class_by_source)
            promoted_class_ids = set(result.promoted_class_ids)
        except (TypeError, ValueError):
            return []
        for proposal in result.deterministic_proposals:
            if not isinstance(proposal, TypedScalarProposal):
                return []
            class_id = class_by_source.get(proposal.source_key)
            if (not isinstance(class_id, str)
                    or class_id not in KNOWN_CLASS_IDS
                    or class_id not in promoted_class_ids):
                return []
            decisions.append(TypedScalarDecision(
                source_key=proposal.source_key,
                committed=True,
                reason=f"{ROUTER_REASON_COMMITTED}:{class_id}",
                veto="deterministic_admission",
                agreement=1.0,
                k=1,
                distribution={"deterministic_scalar": 1},
                proposal=proposal,
                evidence_tier=PERCEPTION_EVIDENCE_TIER,
            ))
        return decisions


__all__ = [
    "DeterministicScalarRouter",
    "EpisodeRoute",
    "RoutingResult",
    "ROUTE_DETERMINISTIC",
    "ROUTE_LLM_REVIEW",
    "ROUTER_VERSION",
]
