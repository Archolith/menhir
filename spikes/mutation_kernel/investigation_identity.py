"""Identity-aware investigative folds for the mutation-kernel research spike.

The durable ownership Assertion keeps the entity ID understood when the source was first interpreted.
This adapter asks the generic identity overlay what that source receipt resolves to *now*, then calls
the existing investigation fold with only the endpoint IDs rewritten in-memory. Assertion IDs,
source keys, authority, confidence, and provenance remain unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable

from .graph import EdgeAbstention, GraphOutcome, ResolvedEntity
from .investigation import OWNERSHIP_ASSERTION, fold_ownership, ownership_slot_key
from .kernel import Assertion, current_assertions, parse_when


IdentityResolver = Callable[[str, str, str], ResolvedEntity | None]


def fold_ownership_with_identity(
    assertions: Iterable[Assertion],
    resolver: IdentityResolver,
) -> GraphOutcome:
    rows = current_assertions(assertions)
    if not rows:
        raise ValueError("fold_ownership_with_identity requires at least one current assertion")

    latest_time = max(parse_when(row.valid_at) for row in rows)
    latest_ids = {
        row.assertion_id
        for row in rows
        if parse_when(row.valid_at) == latest_time
    }

    rewritten: list[Assertion] = []
    unresolved: list[str] = []
    for row in rows:
        if row.assertion_type != OWNERSHIP_ASSERTION:
            raise ValueError("identity-aware ownership fold received a non-ownership assertion")
        if row.assertion_id not in latest_ids:
            rewritten.append(row)
            continue

        owner_kind = str(row.value.get("owner_entity_kind") or "")
        owner_id = str(row.value.get("owner_entity_id") or "")
        resolved = resolver(owner_kind, owner_id, row.source_key)
        if resolved is None or resolved.entity_kind != owner_kind:
            unresolved.append(row.assertion_id)
            rewritten.append(row)
            continue

        value = dict(row.value)
        value["owner_entity_id"] = resolved.entity_id
        value["owner_entity_kind"] = resolved.entity_kind
        rewritten.append(replace(row, value=value))

    if unresolved:
        sample = rows[0]
        effective_at = latest_time.isoformat().replace("+00:00", "Z")
        return EdgeAbstention(
            materializer_id="investigation.current_ownership",
            slot_key=ownership_slot_key(sample.scope, sample.subject_id),
            effective_at=effective_at,
            reason="unresolved_current_owner_identity",
            assertion_ids=tuple(sorted(unresolved)),
        )

    return fold_ownership(rewritten)
