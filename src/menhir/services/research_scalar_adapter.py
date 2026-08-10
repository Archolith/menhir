"""Pure adapter for offline scalar-extraction research candidates.

Research protocols should speak the same raw-row contract as the typed-scalar boundary and
then reuse Menhir's existing validation, grounding, and structural composition code.  This module
does not know about routing, persistence, graph state, schedulers, or audit emission.  It returns
an immutable, quote-free receipt so an experiment can inspect why a candidate was rejected without
creating a production scalar decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from menhir.services.structural_scalar_composer import (
    STRUCTURAL_COMPOSER_VERSION,
    StructuralComposition,
    compose_structural_scalar_identity,
)
from menhir.services.typed_scalar_rules import (
    TypedScalarProposal,
    parse_scalar_row,
)

__all__ = [
    "RESEARCH_ADAPTER_VERSION",
    "ResearchScalarReceipt",
    "ResearchScalarResult",
    "adapt_research_candidate",
]


RESEARCH_ADAPTER_VERSION = "research-adapter-v1"
_COMPOSITION_ERROR = "struct.composer_error"
_ADAPTER_ERROR = "research.adapter_error"
_CLAIMED_LOCATOR_FIELDS = frozenset({"episode_uuid", "span_start", "span_end"})


@dataclass(frozen=True)
class ResearchScalarReceipt:
    """Bounded, quote-free provenance for one research candidate attempt."""

    adapter_version: str
    candidate_id_hash: str
    candidate_hash: str
    parse_status: str
    parse_reason: str | None
    episode_uuid: str | None
    source_key: str | None
    span_start: int | None
    span_end: int | None
    composition_status: str | None
    composition_reason: str | None
    composition_rule_id: str | None
    composer_version: str | None
    checks: tuple[str, ...]


@dataclass(frozen=True)
class ResearchScalarResult:
    """The non-persisted result of adapting and composing one research candidate."""

    proposal: TypedScalarProposal | None
    composition: StructuralComposition | None
    receipt: ResearchScalarReceipt


def _stable_hash(value: object) -> str:
    """Hash a bounded JSON-ish value without exposing candidate text in receipts."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: repr(item),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        encoded = repr(value).encode("utf-8", "replace")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_id_hash(candidate: Mapping[str, Any], candidate_id: str | None) -> str:
    raw_id = candidate_id
    if raw_id is None:
        candidate_value = candidate.get("candidate_id")
        raw_id = candidate_value if isinstance(candidate_value, str) else None
    return _stable_hash(raw_id if raw_id is not None else candidate)


def _base_receipt(
    *,
    candidate_hash: str,
    candidate_id_hash: str,
    parse_status: str,
    parse_reason: str | None,
    episode_uuid: str | None = None,
    source_key: str | None = None,
    span_start: int | None = None,
    span_end: int | None = None,
    composition_status: str | None = None,
    composition_reason: str | None = None,
    composition_rule_id: str | None = None,
    composer_version: str | None = None,
    checks: tuple[str, ...] = (),
) -> ResearchScalarReceipt:
    return ResearchScalarReceipt(
        adapter_version=RESEARCH_ADAPTER_VERSION,
        candidate_id_hash=candidate_id_hash,
        candidate_hash=candidate_hash,
        parse_status=parse_status,
        parse_reason=parse_reason,
        episode_uuid=episode_uuid,
        source_key=source_key,
        span_start=span_start,
        span_end=span_end,
        composition_status=composition_status,
        composition_reason=composition_reason,
        composition_rule_id=composition_rule_id,
        composer_version=composer_version,
        checks=checks,
    )


def _reject(
    *,
    candidate_hash: str,
    candidate_id_hash: str,
    reason: str,
    episode_uuid: str | None = None,
    source_key: str | None = None,
    span_start: int | None = None,
    span_end: int | None = None,
) -> ResearchScalarResult:
    return ResearchScalarResult(
        proposal=None,
        composition=None,
        receipt=_base_receipt(
            candidate_hash=candidate_hash,
            candidate_id_hash=candidate_id_hash,
            parse_status="rejected",
            parse_reason=reason,
            episode_uuid=episode_uuid,
            source_key=source_key,
            span_start=span_start,
            span_end=span_end,
        ),
    )


def _episode_indices(episodes: Sequence[Any]) -> dict[str, int] | None:
    indices: dict[str, int] = {}
    for index, episode in enumerate(episodes):
        uuid = str(getattr(episode, "uuid", "") or "").strip()
        if not uuid or uuid in indices:
            return None
        indices[uuid] = index
    return indices


def _claimed_locator(
    candidate: Mapping[str, Any],
) -> tuple[str | None, tuple[int, int] | None, str | None]:
    """Validate optional candidate claims without trusting them as proposal provenance."""
    present = _CLAIMED_LOCATOR_FIELDS.intersection(candidate)
    if not present:
        return None, None, None
    raw_uuid = candidate.get("episode_uuid")
    if raw_uuid is not None and (not isinstance(raw_uuid, str) or not raw_uuid.strip()):
        return None, None, "claimed_episode_uuid_invalid"
    if {"span_start", "span_end"}.intersection(present) and not {
        "span_start", "span_end",
    }.issubset(present):
        return None, None, "claimed_span_incomplete"
    if "span_start" not in present:
        return raw_uuid.strip() if isinstance(raw_uuid, str) else None, None, None
    start, end = candidate.get("span_start"), candidate.get("span_end")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or end <= start
    ):
        return None, None, "claimed_span_invalid"
    return raw_uuid.strip() if isinstance(raw_uuid, str) else None, (start, end), None


def adapt_research_candidate(
    candidate: Mapping[str, Any] | Any,
    episodes: Sequence[Any],
    *,
    candidate_id: str | None = None,
    canonical_self: bool = True,
) -> ResearchScalarResult:
    """Validate, ground, and structurally compose one offline research candidate.

    ``candidate`` uses the same raw row fields accepted by :func:`parse_scalar_row`.  It may carry
    optional ``episode_uuid``/``span_start``/``span_end`` claims for cross-checking; those claims
    are never used to construct the returned proposal.  A candidate that cannot be validated or
    whose claims disagree with the parser-derived locator is dropped without calling the composer.
    """
    if isinstance(candidate, Mapping):
        raw_candidate = dict(candidate)
    else:
        raw_candidate = {"_candidate": repr(candidate)}
    candidate_hash = _stable_hash(raw_candidate)
    id_hash = _candidate_id_hash(raw_candidate, candidate_id)
    episode_list = list(episodes)
    indices = _episode_indices(episode_list)
    if indices is None:
        return _reject(
            candidate_hash=candidate_hash,
            candidate_id_hash=id_hash,
            reason="episode_uuid_invalid_or_duplicate",
        )

    claimed_uuid, claimed_span, claim_error = _claimed_locator(raw_candidate)
    if claim_error is not None:
        return _reject(candidate_hash=candidate_hash, candidate_id_hash=id_hash, reason=claim_error)
    if "episode" not in raw_candidate and claimed_uuid is not None:
        index = indices.get(claimed_uuid)
        if index is None:
            return _reject(
                candidate_hash=candidate_hash,
                candidate_id_hash=id_hash,
                reason="claimed_episode_uuid_unknown",
            )
        raw_candidate["episode"] = index

    drops: list[str] = []
    try:
        proposal = parse_scalar_row(raw_candidate, episode_list, drops.append)
    except Exception:
        return _reject(candidate_hash=candidate_hash, candidate_id_hash=id_hash, reason=_ADAPTER_ERROR)
    if proposal is None:
        return _reject(
            candidate_hash=candidate_hash,
            candidate_id_hash=id_hash,
            reason=drops[0] if drops else "candidate_rejected",
        )

    if claimed_uuid is not None and claimed_uuid != proposal.episode_uuid:
        return _reject(
            candidate_hash=candidate_hash,
            candidate_id_hash=id_hash,
            reason="claimed_episode_uuid_mismatch",
            episode_uuid=proposal.episode_uuid,
            source_key=proposal.source_key,
            span_start=proposal.span_start,
            span_end=proposal.span_end,
        )
    if claimed_span is not None and claimed_span != (proposal.span_start, proposal.span_end):
        return _reject(
            candidate_hash=candidate_hash,
            candidate_id_hash=id_hash,
            reason="claimed_span_mismatch",
            episode_uuid=proposal.episode_uuid,
            source_key=proposal.source_key,
            span_start=proposal.span_start,
            span_end=proposal.span_end,
        )

    source_by_uuid = {
        proposal.episode_uuid: str(getattr(episode_list[index], "content", "") or "")
        for index, episode in enumerate(episode_list)
        if str(getattr(episode, "uuid", "") or "").strip() == proposal.episode_uuid
    }
    source_text = source_by_uuid.get(proposal.episode_uuid, "")
    try:
        composition = compose_structural_scalar_identity(
            proposal, source_text, canonical_self=canonical_self
        )
    except Exception:
        return ResearchScalarResult(
            proposal=proposal,
            composition=None,
            receipt=_base_receipt(
                candidate_hash=candidate_hash,
                candidate_id_hash=id_hash,
                parse_status="admitted",
                parse_reason=None,
                episode_uuid=proposal.episode_uuid,
                source_key=proposal.source_key,
                span_start=proposal.span_start,
                span_end=proposal.span_end,
                composition_status="error",
                composition_reason=_COMPOSITION_ERROR,
                composer_version=STRUCTURAL_COMPOSER_VERSION,
            ),
        )
    receipt = composition.receipt
    return ResearchScalarResult(
        proposal=proposal,
        composition=composition,
        receipt=_base_receipt(
            candidate_hash=candidate_hash,
            candidate_id_hash=id_hash,
            parse_status="admitted",
            parse_reason=None,
            episode_uuid=proposal.episode_uuid,
            source_key=proposal.source_key,
            span_start=proposal.span_start,
            span_end=proposal.span_end,
            composition_status=receipt.outcome,
            composition_reason=receipt.reason_code,
            composition_rule_id=receipt.rule_id,
            composer_version=receipt.composer_version,
            checks=receipt.checks,
        ),
    )
