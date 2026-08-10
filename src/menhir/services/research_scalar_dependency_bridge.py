"""Pure source/proposal validation for research scalar dependency evidence.

This bridge has no parser, composition, persistence, runtime, graph, or LLM dependency.  Parser
evidence remains untrusted transport: this module checks it against the original source and an
already-created :class:`TypedScalarProposal`, but never creates or repairs that proposal.  Source
content hash verification belongs here only at this source-boundary; semantic dependency-rule
validation and composition are separate later research slices.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from menhir.domain.scalar_dependency_evidence import (
    MAX_SOURCE_LENGTH,
    ScalarDependencyEvidence,
    ScalarDependencyEvidenceReceipt,
    SourceBoundSpan,
    SUPPORTED_EVIDENCE_VERSIONS,
    SUPPORTED_SCHEMA_VERSIONS,
    compute_evidence_hash,
    source_slice_sha256,
)
from menhir.domain.typed_assertion import build_source_key
from menhir.services.typed_scalar_rules import TypedScalarProposal


BRIDGE_VERSION = "dependency-bridge-v1"
_ZERO_HASH = "0" * 64
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_TOKEN_RE = re.compile(r"^[a-z0-9._-]{1,64}$")

_CHECK_SOURCE_TYPE = "source_type"
_CHECK_SOURCE_LENGTH = "source_length"
_CHECK_SOURCE_HASH = "source_hash"
_CHECK_EVIDENCE_VERSION = "evidence_version"
_CHECK_EXPECTED_PARSER_ID = "expected_parser_id"
_CHECK_EXPECTED_PARSER_VERSION = "expected_parser_version"
_CHECK_EXPECTED_MODEL_HASH = "expected_model_hash"
_CHECK_EXPECTED_PIPELINE_HASH = "expected_pipeline_hash"
_CHECK_CANDIDATE_HASH = "candidate_hash"
_CHECK_PROPOSAL_TYPE = "proposal_type"
_CHECK_PROPOSAL_LOCATOR = "proposal_locator"
_CHECK_PROPOSAL_SOURCE_KEY = "proposal_source_key"
_CHECK_EXPECTED_EPISODE = "expected_episode_uuid"
_CHECK_EXPECTED_SOURCE_KEY = "expected_source_key"
_CHECK_PROPOSAL_QUOTE = "proposal_quote"
_CHECK_CLAUSE = "clause_contains_proposal"
_CHECK_NUMERIC = "numeric_inside_proposal"
_CHECK_SURFACE_HASHES = "surface_hashes"
_CHECK_EVIDENCE_HASH = "evidence_hash"


@dataclass(frozen=True, slots=True)
class _Reject(Exception):
    reason: str
    check: str


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _valid_identity_token(value: object) -> bool:
    return isinstance(value, str) and _IDENTITY_TOKEN_RE.fullmatch(value) is not None


def _safe_text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def _safe_hash(value: object) -> str:
    return value if _valid_hash(value) else _ZERO_HASH


def _allowlisted_version(value: object, allowed: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def _safe_int(value: object, fallback: int) -> int:
    return value if type(value) is int and value >= 0 else fallback


def _receipt(
    evidence: object,
    *,
    outcome: str,
    reason: str,
    checks: tuple[str, ...],
) -> ScalarDependencyEvidenceReceipt:
    """Construct a bounded receipt even when the object under validation is malformed."""

    if type(evidence) is ScalarDependencyEvidence:
        raw_schema_version = getattr(evidence, "schema_version", None)
        raw_evidence_version = getattr(evidence, "evidence_version", None)
        schema_version = _allowlisted_version(
            raw_schema_version, SUPPORTED_SCHEMA_VERSIONS, "scalar-dependency-v1"
        )
        evidence_version = _allowlisted_version(raw_evidence_version, SUPPORTED_EVIDENCE_VERSIONS, "unknown")
        source_hash = _safe_hash(getattr(evidence, "source_hash", None))
        candidate_hash = _safe_hash(getattr(evidence, "candidate_hash", None))
        evidence_sha256 = _safe_hash(getattr(evidence, "evidence_sha256", None))
        clause = getattr(evidence, "clause_span", None)
        clause_start = _safe_int(getattr(clause, "start", None), 0)
        clause_end = _safe_int(getattr(clause, "end", None), 1)
        tokens = getattr(evidence, "tokens", ())
        edges = getattr(evidence, "edges", ())
        markers = getattr(evidence, "markers", ())
    else:
        schema_version = "scalar-dependency-v1"
        evidence_version = "unknown"
        source_hash = _ZERO_HASH
        candidate_hash = _ZERO_HASH
        evidence_sha256 = _ZERO_HASH
        clause_start = 0
        clause_end = 1
        tokens = edges = markers = ()
    if clause_end <= 0:
        clause_end = 1
    if clause_end > MAX_SOURCE_LENGTH:
        clause_end = MAX_SOURCE_LENGTH
    if clause_start >= clause_end:
        clause_start = 0
    token_count = min(len(tokens), 512) if isinstance(tokens, tuple) else 0
    edge_count = min(len(edges), 1_024) if isinstance(edges, tuple) else 0
    marker_count = min(len(markers), 128) if isinstance(markers, tuple) else 0
    return ScalarDependencyEvidenceReceipt(
        schema_version=schema_version,
        evidence_version=evidence_version,
        bridge_version=BRIDGE_VERSION,
        source_hash=source_hash,
        candidate_hash=candidate_hash,
        evidence_sha256=evidence_sha256,
        clause_start=clause_start,
        clause_end=clause_end,
        token_count=token_count,
        edge_count=edge_count,
        marker_count=marker_count,
        outcome=outcome,
        reason=reason,
        rule_version="not_applied",
        composer_version="not_applied",
        check_names=checks,
    )


def _require_span(span: object, source: str, name: str) -> None:
    if not isinstance(span, SourceBoundSpan):
        raise _Reject("surface_hash_mismatch", _CHECK_SURFACE_HASHES)
    try:
        actual = source_slice_sha256(source, span.start, span.end)
    except (TypeError, ValueError):
        raise _Reject("surface_hash_mismatch", _CHECK_SURFACE_HASHES) from None
    if actual != span.surface_sha256:
        raise _Reject("surface_hash_mismatch", _CHECK_SURFACE_HASHES)


def _validate_surface_hashes(evidence: ScalarDependencyEvidence, source: str) -> None:
    spans: list[object] = [evidence.clause_span]
    for token in evidence.tokens:
        spans.append(token.span)
    cues = evidence.cues
    spans.extend((cues.subject, cues.predicate, cues.numeric_value, cues.unit, cues.target, cues.scope))
    spans.extend(cues.modifiers)
    for marker in evidence.markers:
        spans.append(marker.span)
    seen: set[tuple[int, int, str]] = set()
    for span in spans:
        if span is None:
            continue
        if isinstance(span, SourceBoundSpan):
            key = (span.start, span.end, span.surface_sha256)
            if key in seen:
                continue
            seen.add(key)
        _require_span(span, source, "span")


def _validate(
    evidence: object,
    source_text: object,
    proposal: object,
    expected_candidate_hash: object,
    expected_parser_id: object,
    expected_parser_version: object,
    expected_model_hash: object,
    expected_pipeline_hash: object,
    expected_episode_uuid: object,
    expected_source_key: object,
) -> None:
    if type(evidence) is not ScalarDependencyEvidence:
        raise _Reject("evidence_type_invalid", _CHECK_SOURCE_TYPE)
    if not (
        isinstance(evidence.schema_version, str)
        and evidence.schema_version in SUPPORTED_SCHEMA_VERSIONS
        and isinstance(evidence.evidence_version, str)
        and evidence.evidence_version in SUPPORTED_EVIDENCE_VERSIONS
    ):
        raise _Reject("evidence_version_unsupported", _CHECK_EVIDENCE_VERSION)
    if not _valid_identity_token(expected_parser_id):
        raise _Reject("expected_parser_id_invalid", _CHECK_EXPECTED_PARSER_ID)
    if not _valid_identity_token(expected_parser_version):
        raise _Reject("expected_parser_version_invalid", _CHECK_EXPECTED_PARSER_VERSION)
    if not _valid_hash(expected_model_hash):
        raise _Reject("expected_model_hash_invalid", _CHECK_EXPECTED_MODEL_HASH)
    if not _valid_hash(expected_pipeline_hash):
        raise _Reject("expected_pipeline_hash_invalid", _CHECK_EXPECTED_PIPELINE_HASH)
    if evidence.parser_id != expected_parser_id:
        raise _Reject("parser_id_mismatch", _CHECK_EXPECTED_PARSER_ID)
    if evidence.parser_version != expected_parser_version:
        raise _Reject("parser_version_mismatch", _CHECK_EXPECTED_PARSER_VERSION)
    if evidence.model_hash != expected_model_hash:
        raise _Reject("model_hash_mismatch", _CHECK_EXPECTED_MODEL_HASH)
    if evidence.pipeline_hash != expected_pipeline_hash:
        raise _Reject("pipeline_hash_mismatch", _CHECK_EXPECTED_PIPELINE_HASH)
    if type(source_text) is not str:
        raise _Reject("source_type_invalid", _CHECK_SOURCE_TYPE)
    if len(source_text) != evidence.source_length:
        raise _Reject("source_length_mismatch", _CHECK_SOURCE_LENGTH)
    if len(source_text) > MAX_SOURCE_LENGTH:
        raise _Reject("source_length_mismatch", _CHECK_SOURCE_LENGTH)
    if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != evidence.source_hash:
        raise _Reject("source_hash_mismatch", _CHECK_SOURCE_HASH)
    if not _valid_hash(expected_candidate_hash):
        raise _Reject("expected_candidate_hash_invalid", _CHECK_CANDIDATE_HASH)
    if expected_candidate_hash != evidence.candidate_hash:
        raise _Reject("candidate_hash_mismatch", _CHECK_CANDIDATE_HASH)
    if not isinstance(proposal, TypedScalarProposal):
        raise _Reject("proposal_type_invalid", _CHECK_PROPOSAL_TYPE)
    if (
        type(proposal.span_start) is not int
        or type(proposal.span_end) is not int
        or proposal.span_start < 0
        or proposal.span_start >= proposal.span_end
        or proposal.span_end > len(source_text)
    ):
        raise _Reject("proposal_locator_invalid", _CHECK_PROPOSAL_LOCATOR)
    if type(proposal.episode_uuid) is not str or not proposal.episode_uuid.strip():
        raise _Reject("proposal_locator_invalid", _CHECK_PROPOSAL_LOCATOR)
    if type(expected_episode_uuid) is not str or not expected_episode_uuid.strip():
        raise _Reject("expected_episode_uuid_invalid", _CHECK_EXPECTED_EPISODE)
    if proposal.episode_uuid != expected_episode_uuid:
        raise _Reject("expected_episode_uuid_mismatch", _CHECK_EXPECTED_EPISODE)
    if type(proposal.claim_ordinal) is not int or proposal.claim_ordinal < 0:
        raise _Reject("proposal_locator_invalid", _CHECK_PROPOSAL_LOCATOR)
    try:
        if proposal.source_key != build_source_key(
            proposal.episode_uuid,
            proposal.span_start,
            proposal.span_end,
            proposal.claim_ordinal,
        ):
            raise _Reject("proposal_source_key_mismatch", _CHECK_PROPOSAL_SOURCE_KEY)
    except (AttributeError, TypeError, ValueError):
        raise _Reject("proposal_source_key_mismatch", _CHECK_PROPOSAL_SOURCE_KEY) from None
    if not _valid_hash(expected_source_key):
        raise _Reject("expected_source_key_invalid", _CHECK_EXPECTED_SOURCE_KEY)
    if proposal.source_key != expected_source_key:
        raise _Reject("expected_source_key_mismatch", _CHECK_EXPECTED_SOURCE_KEY)
    if not isinstance(proposal.stated_span, str) or not proposal.stated_span.strip():
        raise _Reject("proposal_quote_invalid", _CHECK_PROPOSAL_QUOTE)
    quote = proposal.stated_span.strip()
    matches = list(re.finditer(re.escape(quote), source_text, re.IGNORECASE))
    if len(matches) != 1 or (matches[0].start(), matches[0].end()) != (
        proposal.span_start,
        proposal.span_end,
    ):
        raise _Reject("proposal_quote_mismatch", _CHECK_PROPOSAL_QUOTE)
    clause = evidence.clause_span
    if clause.start > proposal.span_start or clause.end < proposal.span_end:
        raise _Reject("clause_not_containing_proposal", _CHECK_CLAUSE)
    numeric = evidence.cues.numeric_value
    if numeric.start < proposal.span_start or numeric.end > proposal.span_end:
        raise _Reject("numeric_cue_outside_proposal", _CHECK_NUMERIC)
    _validate_surface_hashes(evidence, source_text)
    if evidence.evidence_sha256 != compute_evidence_hash(evidence):
        raise _Reject("evidence_hash_mismatch", _CHECK_EVIDENCE_HASH)


def validate_source_bound_dependency_evidence(
    evidence: object,
    source_text: object,
    proposal: object,
    *,
    expected_candidate_hash: object,
    expected_parser_id: object,
    expected_parser_version: object,
    expected_model_hash: object,
    expected_pipeline_hash: object,
    expected_episode_uuid: object,
    expected_source_key: object,
) -> ScalarDependencyEvidenceReceipt:
    """Validate source/proposal grounding and return a bounded validated/abstained receipt."""

    try:
        _validate(
            evidence,
            source_text,
            proposal,
            expected_candidate_hash,
            expected_parser_id,
            expected_parser_version,
            expected_model_hash,
            expected_pipeline_hash,
            expected_episode_uuid,
            expected_source_key,
        )
    except _Reject as failure:
        return _receipt(
            evidence,
            outcome="abstained",
            reason=failure.reason,
            checks=(failure.check,),
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return _receipt(
            evidence,
            outcome="abstained",
            reason="evidence_structure_invalid",
            checks=("evidence_structure",),
        )
    return _receipt(
        evidence,
        outcome="validated",
        reason="validated_source_bound_evidence",
        checks=(
            _CHECK_SOURCE_TYPE,
            _CHECK_SOURCE_LENGTH,
            _CHECK_SOURCE_HASH,
            _CHECK_EVIDENCE_VERSION,
            _CHECK_EXPECTED_PARSER_ID,
            _CHECK_EXPECTED_PARSER_VERSION,
            _CHECK_EXPECTED_MODEL_HASH,
            _CHECK_EXPECTED_PIPELINE_HASH,
            _CHECK_CANDIDATE_HASH,
            _CHECK_PROPOSAL_TYPE,
            _CHECK_PROPOSAL_LOCATOR,
            _CHECK_PROPOSAL_SOURCE_KEY,
            _CHECK_EXPECTED_EPISODE,
            _CHECK_EXPECTED_SOURCE_KEY,
            _CHECK_PROPOSAL_QUOTE,
            _CHECK_CLAUSE,
            _CHECK_NUMERIC,
            _CHECK_SURFACE_HASHES,
            _CHECK_EVIDENCE_HASH,
        ),
    )


__all__ = ["BRIDGE_VERSION", "validate_source_bound_dependency_evidence"]
