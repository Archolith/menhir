"""Investigation-specific identity dependencies for generic graph scheduling.

The scheduler knows only that receipt identity changed. This extension adapter owns the meaning:
which historical person IDs appear inside investigative ownership assertions, which ownership slots
those assertions invalidate, and how a worker derives the current graph outcome plus exact receipt
identity guards.
"""

from __future__ import annotations

from .identity import Neo4jIdentityEvolution
from .identity_scheduler import (
    GraphMaterializerTarget,
    GuardedGraphDerivation,
    IdentityDependencyDefinition,
    IdentityMappingChange,
    ReceiptIdentityGuard,
)
from .investigation import (
    OWNERSHIP_ASSERTION,
    OWNERSHIP_MATERIALIZER,
    PERSON_KIND,
    ownership_slot_key,
)
from .investigation_identity import fold_ownership_with_identity
from .kernel import Assertion, current_assertions, parse_when
from .neo4j_store import Neo4jEnvelopeStore


def ownership_identity_dependency_definition(
    assertions: Neo4jEnvelopeStore,
) -> IdentityDependencyDefinition:
    """Map changed historical person identities to only affected ownership slots."""

    def mapper(changes: tuple[IdentityMappingChange, ...]):
        affected_old_ids = {
            change.original_entity_id
            for change in changes
            if change.entity_kind == PERSON_KIND
        }
        if not affected_old_ids:
            return ()

        targets: set[GraphMaterializerTarget] = set()
        for row in assertions.load_assertions(assertion_type=OWNERSHIP_ASSERTION):
            owner_id = str(row.value.get("owner_entity_id") or "")
            if owner_id not in affected_old_ids:
                continue
            targets.add(
                GraphMaterializerTarget(
                    materializer_id=OWNERSHIP_MATERIALIZER,
                    slot_key=ownership_slot_key(row.scope, row.subject_id),
                )
            )
        return tuple(sorted(targets))

    return IdentityDependencyDefinition(
        definition_id="investigation.ownership.identity",
        version=1,
        entity_kind=PERSON_KIND,
        mapper=mapper,
    )


def derive_ownership_with_identity_guards(
    assertions: list[Assertion],
    identity: Neo4jIdentityEvolution,
) -> GuardedGraphDerivation:
    """Derive ownership and capture the exact receipt resolutions used by the latest evidence."""
    rows = current_assertions(assertions)
    if not rows:
        raise ValueError("derive_ownership_with_identity_guards requires current assertions")

    identity.bootstrap_current_identity()
    latest_time = max(parse_when(row.valid_at) for row in rows)
    latest = [row for row in rows if parse_when(row.valid_at) == latest_time]

    guards: list[ReceiptIdentityGuard] = []
    seen_receipts: set[str] = set()
    for row in latest:
        if row.assertion_type != OWNERSHIP_ASSERTION:
            raise ValueError("identity-guard derivation received a non-ownership assertion")
        owner_kind = str(row.value.get("owner_entity_kind") or "")
        owner_id = str(row.value.get("owner_entity_id") or "")
        resolution = identity.resolve(
            entity_kind=owner_kind,
            entity_id=owner_id,
            source_key=row.source_key,
        )
        if resolution.entity is None:
            continue
        receipt_key = identity._receipt_storage_key(owner_kind, owner_id, row.source_key)
        if receipt_key in seen_receipts:
            continue
        seen_receipts.add(receipt_key)
        guards.append(
            ReceiptIdentityGuard(
                receipt_storage_key=receipt_key,
                source_key=row.source_key,
                expected_current_entity_id=resolution.entity.entity_id,
            )
        )

    def resolver(kind: str, entity_id: str, source_key: str):
        return identity.resolve(
            entity_kind=kind,
            entity_id=entity_id,
            source_key=source_key,
        ).entity

    outcome = fold_ownership_with_identity(rows, resolver)
    return GuardedGraphDerivation(
        outcome=outcome,
        guards=tuple(sorted(guards, key=lambda row: row.receipt_storage_key)),
    )
