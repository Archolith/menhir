"""Investigation reference extension for the generic graph mutation spike.

This module owns all investigative meaning used by the test: how a person and parcel are identified,
what an ownership assertion contains, and how competing ownership assertions fold into the current
``OWNS`` graph relationship. The generic graph layer never imports this module.
"""

from __future__ import annotations

from typing import Iterable

from .graph import (
    EdgeAbstention,
    EdgeKind,
    EdgeSet,
    EntityKind,
    EntityProposal,
    GraphOutcome,
    GraphSchema,
    MaterializedEdge,
    ResolvedEntity,
)
from .kernel import (
    Assertion,
    EvidenceRef,
    current_assertions,
    effective_authority,
    make_assertion,
    parse_when,
)


PERSON_KIND = "investigation.person"
PARCEL_KIND = "investigation.parcel"
OWNS_EDGE = "investigation.owns"
OWNERSHIP_ASSERTION = "investigation.ownership"
OWNERSHIP_MATERIALIZER = "investigation.current_ownership"


def _person_identity(proposal: EntityProposal) -> str:
    external_id = str(proposal.property_map.get("external_id") or "").strip().lower()
    if not external_id:
        raise ValueError("investigation.person requires an extension-owned external_id identity key")
    return f"external:{external_id}"


def _parcel_identity(proposal: EntityProposal) -> str:
    county = str(proposal.property_map.get("county") or "").strip().lower()
    account_id = str(proposal.property_map.get("account_id") or "").strip().lower()
    if not county or not account_id:
        raise ValueError("investigation.parcel requires county + account_id identity")
    return f"{county}:{account_id}"


def investigation_graph_schema() -> GraphSchema:
    return GraphSchema(
        entity_kinds=(
            EntityKind(kind_id=PARCEL_KIND, version=1, identity_resolver=_parcel_identity),
            EntityKind(kind_id=PERSON_KIND, version=1, identity_resolver=_person_identity),
        ),
        edge_kinds=(
            EdgeKind(
                kind_id=OWNS_EDGE,
                version=1,
                source_kinds=(PERSON_KIND,),
                target_kinds=(PARCEL_KIND,),
                temporal=True,
                provenance_required=True,
            ),
        ),
    )


def person_proposal(
    source: EvidenceRef,
    *,
    display_name: str,
    external_id: str,
    valid_at: str,
    learned_at: str | None = None,
    authority: str = "trusted_tool",
) -> EntityProposal:
    return EntityProposal(
        entity_kind=PERSON_KIND,
        display_name=display_name,
        properties=(("external_id", external_id),),
        source=source,
        valid_at=valid_at,
        learned_at=learned_at or valid_at,
        authority=authority,
    )


def parcel_proposal(
    source: EvidenceRef,
    *,
    display_name: str,
    county: str,
    account_id: str,
    valid_at: str,
    learned_at: str | None = None,
    authority: str = "trusted_tool",
) -> EntityProposal:
    return EntityProposal(
        entity_kind=PARCEL_KIND,
        display_name=display_name,
        properties=tuple(sorted((("account_id", account_id), ("county", county)))),
        source=source,
        valid_at=valid_at,
        learned_at=learned_at or valid_at,
        authority=authority,
    )


def ownership_assertion(
    source: EvidenceRef,
    *,
    investigation_id: str,
    owner: ResolvedEntity,
    parcel: ResolvedEntity,
    valid_at: str,
    learned_at: str | None = None,
    authority: str = "trusted_tool",
    confidence: float = 1.0,
    supersedes: tuple[str, ...] = (),
    interpreter_version: str = "investigation-v1",
) -> Assertion:
    if owner.entity_kind != PERSON_KIND:
        raise ValueError("ownership owner must be an investigation.person")
    if parcel.entity_kind != PARCEL_KIND:
        raise ValueError("ownership target must be an investigation.parcel")
    return make_assertion(
        source=source,
        assertion_type=OWNERSHIP_ASSERTION,
        subject_id=parcel.entity_id,
        scope=investigation_id,
        key="owner",
        value={
            "owner_entity_id": owner.entity_id,
            "owner_entity_kind": owner.entity_kind,
            "parcel_entity_kind": parcel.entity_kind,
        },
        valid_at=valid_at,
        learned_at=learned_at or valid_at,
        authority=authority,
        confidence=confidence,
        supersedes=supersedes,
        interpreter_version=interpreter_version,
    )


def ownership_slot_key(investigation_id: str, parcel_entity_id: str) -> str:
    return f"{investigation_id}|parcel:{parcel_entity_id}|owner"


def fold_ownership(assertions: Iterable[Assertion]) -> GraphOutcome:
    rows = current_assertions(assertions)
    if not rows:
        raise ValueError("fold_ownership requires at least one current assertion")

    expected_subject = rows[0].subject_id
    expected_scope = rows[0].scope
    for row in rows:
        if row.assertion_type != OWNERSHIP_ASSERTION:
            raise ValueError("fold_ownership received a non-ownership assertion")
        if row.subject_id != expected_subject or row.scope != expected_scope or row.key != "owner":
            raise ValueError("fold_ownership received assertions from more than one ownership slot")

    latest_time = max(parse_when(row.valid_at) for row in rows)
    latest = [row for row in rows if parse_when(row.valid_at) == latest_time]
    owners = {
        (
            str(row.value.get("owner_entity_kind") or ""),
            str(row.value.get("owner_entity_id") or ""),
        )
        for row in latest
    }
    effective_at = latest_time.isoformat().replace("+00:00", "Z")
    slot_key = ownership_slot_key(expected_scope, expected_subject)

    if len(owners) != 1:
        return EdgeAbstention(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=slot_key,
            effective_at=effective_at,
            reason="ambiguous_current_owner",
            assertion_ids=tuple(sorted(row.assertion_id for row in latest)),
        )

    owner_kind, owner_id = next(iter(owners))
    if owner_kind != PERSON_KIND or not owner_id:
        return EdgeAbstention(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=slot_key,
            effective_at=effective_at,
            reason="invalid_owner_identity",
            assertion_ids=tuple(sorted(row.assertion_id for row in latest)),
        )

    contributors = tuple(sorted(row.assertion_id for row in latest))
    confidence = min(row.confidence for row in latest)
    return EdgeSet(
        materializer_id=OWNERSHIP_MATERIALIZER,
        slot_key=slot_key,
        effective_at=effective_at,
        edges=(
            MaterializedEdge(
                edge_kind=OWNS_EDGE,
                source_entity_id=owner_id,
                source_entity_kind=owner_kind,
                target_entity_id=expected_subject,
                target_entity_kind=PARCEL_KIND,
                valid_from=effective_at,
                contributor_ids=contributors,
                effective_authority=effective_authority(latest),
                confidence=confidence,
                properties=(("investigation_id", expected_scope),),
            ),
        ),
        assertion_ids=contributors,
    )
