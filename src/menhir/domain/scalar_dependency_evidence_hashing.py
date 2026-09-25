"""Deterministic canonical JSON and hashing for scalar dependency evidence envelopes."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from menhir.domain.scalar_dependency_evidence_spans import (
    MarkerEvidence,
    ScalarCueEvidence,
    SourceBoundSpan,
)

if TYPE_CHECKING:
    from menhir.domain.scalar_dependency_evidence import ScalarDependencyEvidence


def _span_payload(span: SourceBoundSpan | None) -> object:
    if span is None:
        return None
    return {
        "end": span.end,
        "start": span.start,
        "surface_sha256": span.surface_sha256,
        "token_end": span.token_end,
        "token_start": span.token_start,
    }


def _cue_payload(cues: ScalarCueEvidence) -> dict[str, object]:
    return {
        "clause_root_token": cues.clause_root_token,
        "modifiers": [_span_payload(span) for span in cues.modifiers],
        "numeric_value": _span_payload(cues.numeric_value),
        "predicate": _span_payload(cues.predicate),
        "scope": _span_payload(cues.scope),
        "subject": _span_payload(cues.subject),
        "target": _span_payload(cues.target),
        "unit": _span_payload(cues.unit),
    }


def _marker_payload(marker: MarkerEvidence) -> dict[str, object]:
    return {
        "category": marker.category,
        "span": _span_payload(marker.span),
        "token_indices": list(marker.token_indices),
    }


def _evidence_payload(evidence: "ScalarDependencyEvidence") -> dict[str, object]:
    return {
        "candidate_hash": evidence.candidate_hash,
        "clause_span": _span_payload(evidence.clause_span),
        "cues": _cue_payload(evidence.cues),
        "edges": [
            {"dependent_index": edge.dependent_index, "head_index": edge.head_index, "label": edge.label}
            for edge in evidence.edges
        ],
        "evidence_version": evidence.evidence_version,
        "markers": [_marker_payload(marker) for marker in evidence.markers],
        "model_hash": evidence.model_hash,
        "parser_id": evidence.parser_id,
        "parser_version": evidence.parser_version,
        "pipeline_hash": evidence.pipeline_hash,
        "schema_version": evidence.schema_version,
        "source_hash": evidence.source_hash,
        "source_length": evidence.source_length,
        "tokens": [
            {
                "dependency_label": token.dependency_label,
                "head_index": token.head_index,
                "lemma_sha256": token.lemma_sha256,
                "pos_tag": token.pos_tag,
                "span": _span_payload(token.span),
                "token_index": token.token_index,
            }
            for token in evidence.tokens
        ],
    }


def canonical_evidence_json(evidence: "ScalarDependencyEvidence") -> str:
    """Return deterministic JSON for evidence, excluding ``evidence_sha256``."""

    return json.dumps(_evidence_payload(evidence), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_evidence_hash(evidence: "ScalarDependencyEvidence") -> str:
    return hashlib.sha256(canonical_evidence_json(evidence).encode("utf-8")).hexdigest()


def verify_evidence_hash(evidence: "ScalarDependencyEvidence") -> bool:
    return evidence.evidence_sha256 == compute_evidence_hash(evidence)
