"""Admission-facing adapter for durable :TurnEvidence provenance.

This reader deliberately returns provenance metadata only. Raw turn text remains evidence
content and is not needed to decide what authority the source is allowed to request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.domain.admission import SourceProvenance

__all__ = [
    "TurnEvidenceAdmissionSource",
    "load_turn_evidence_admission_source",
]


@dataclass(frozen=True)
class TurnEvidenceAdmissionSource:
    """Stored provenance needed by an admission-grant issuer or domain policy."""

    provenance: SourceProvenance
    role: str | None
    declarant: str | None
    producer_source_id: str | None
    source_client: str | None
    hook_version: str | None


def load_turn_evidence_admission_source(
    neo4j: Any,
    turn_id: str,
) -> TurnEvidenceAdmissionSource | None:
    """Load source-bound provenance for one durable ``:TurnEvidence`` record.

    ``SourceProvenance.source_id`` is the canonical Menhir ``turn_id`` -- not the
    producer-supplied ``t.source_id``. Missing/blank ``source_kind`` fails closed by
    returning ``None`` because an authority grant cannot be bound to an unidentified
    source mechanism.
    """

    turn_id = str(turn_id or "").strip()
    if not turn_id:
        raise ValueError("turn_id must be a non-blank string")

    rows = neo4j.execute(
        """
        MATCH (t:TurnEvidence {turn_id: $turn_id})
        RETURN t.turn_id AS source_id,
               t.source_kind AS source_kind,
               t.role AS role,
               t.declarant AS declarant,
               t.source_id AS producer_source_id,
               t.source_client AS source_client,
               t.hook_version AS hook_version
        LIMIT 1
        """,
        params={"turn_id": turn_id},
    )
    if not rows:
        return None

    row = rows[0]
    source_id = str(row.get("source_id") or "").strip()
    source_kind = str(row.get("source_kind") or "").strip()
    if not source_id or not source_kind:
        return None

    return TurnEvidenceAdmissionSource(
        provenance=SourceProvenance(
            source_id=source_id,
            source_kind=source_kind,
        ),
        role=str(row["role"]) if row.get("role") is not None else None,
        declarant=(
            str(row["declarant"]) if row.get("declarant") is not None else None
        ),
        producer_source_id=(
            str(row["producer_source_id"])
            if row.get("producer_source_id") is not None
            else None
        ),
        source_client=(
            str(row["source_client"])
            if row.get("source_client") is not None
            else None
        ),
        hook_version=(
            str(row["hook_version"])
            if row.get("hook_version") is not None
            else None
        ),
    )
