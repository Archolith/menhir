"""Shared default-recall visibility policy for memory and View nodes."""

from __future__ import annotations


def _canonical_tenant_cypher(variable: str) -> str:
    """Canonical tenant expression for legacy empty/default namespace spellings."""
    tenant = f"coalesce({variable}.namespace, {variable}.group_id, '')"
    return f"CASE WHEN {tenant} = '' THEN 'default' ELSE {tenant} END"


def view_live_provenance_cypher(variable: str = "n") -> str:
    """Return exact incoming-``MENTIONS``/receipt set equality for one View.

    UUID existence elsewhere in the graph is insufficient: lifecycle authority is the relationship
    from each contributing evidence node to this exact View version. The receipt must be non-empty
    and duplicate-free, every receipt UUID must have its correctly typed incoming relationship, and
    the View may have no additional incoming ``MENTIONS`` relationships. Each evidence node must
    also belong to the View's canonical tenant; legacy ``''`` and ``'default'`` spellings are
    equivalent, but a cross-tenant relationship never establishes live provenance.
    """

    contributors = f"coalesce({variable}.episode_uuids, [])"
    view_tenant = _canonical_tenant_cypher(variable)
    evidence_tenant = _canonical_tenant_cypher("e")
    every_receipt_is_unique = (
        f"all(eid IN {contributors} WHERE "
        f"single(other IN {contributors} WHERE other = eid))"
    )
    every_receipt_is_linked = (
        f"all(eid IN {contributors} WHERE EXISTS {{ "
        f"MATCH (e)-[:MENTIONS]->({variable}) "
        f"WHERE ((e:Episodic AND e.uuid = eid) "
        f"OR (e:TurnEvidence AND e.turn_id = eid)) "
        f"AND {evidence_tenant} = {view_tenant} }})"
    )
    incoming_mentions = f"COUNT {{ MATCH ()-[:MENTIONS]->({variable}) }}"
    return (
        f"size({contributors}) > 0 "
        f"AND {every_receipt_is_unique} "
        f"AND {every_receipt_is_linked} "
        f"AND {incoming_mentions} = size({contributors})"
    )


def memory_retired_cypher(variable: str = "n", *, protection_checked_by_caller: bool = False) -> str:
    """Single definition of memory-level RETIRED (distinct from the View ``retired`` flag).

    Retired = the stored ``memory_retired`` marker OR, for an ordinary ``:Entity``, the derived
    rule: at least one ``:Episodic`` mentions it and every mentioning ``:Episodic`` is retired.
    Derived at read time, so restoring or adding a source needs no write. Derived Views, flagged
    entities, retention-protected entities and entities with no mentioning episode are never
    derived-retired. Every visibility/lifecycle read of retired state must use this helper.

    ``protection_checked_by_caller`` drops the flagged/retention terms for a caller whose own
    predicate already requires them (``mutable_eligibility_cypher``), so they are not stated twice.
    """

    # Local imports: infrastructure.cypher imports this module at load time.
    from menhir.domain.retention import destructive_retention_allowed_cypher
    from menhir.infrastructure.cypher import non_derived_view_cypher

    v = variable
    ordinary = non_derived_view_cypher(v)
    protection = "" if protection_checked_by_caller else (
        f"AND NOT coalesce({v}.user_flagged, false) "
        f"AND {destructive_retention_allowed_cypher(v)} "
    )
    return (
        f"(coalesce({v}.memory_retired, false) OR ({v}:Entity "
        f"AND {ordinary} "
        f"{protection}"
        f"AND EXISTS {{ MATCH (:Episodic)-[:MENTIONS]->({v}) }} "
        f"AND NOT EXISTS {{ MATCH (e:Episodic)-[:MENTIONS]->({v}) "
        f"WHERE NOT coalesce(e.memory_retired, false) }}))"
    )


def default_recall_visibility_cypher(variable: str = "n", *, include_retired: bool = False) -> str:
    """Return the fail-closed predicate used by generic recall/listing surfaces.

    Ordinary memories remain visible unless they are candidates or gone. A materialized View has a
    stronger fail-closed contract: it must explicitly be a current, nonretired FACT for the RECALL
    audience and its durable contributor receipt must exactly equal its incoming ``MENTIONS``
    relationship set. OPERATOR Views remain available to explicit inspection paths.

    Memories carrying the ``memory_retired`` marker are hidden unless ``include_retired`` is set
    (historical surfaces: history, timeline). The bare ``retired`` View flag is never used for it.

    Explicit UUID inspection deliberately does not use this predicate; historical and invalid rows
    must remain inspectable by operator/provenance tooling even though they cannot enter context.
    """

    retired_clause = (
        "" if include_retired else f"AND NOT {memory_retired_cypher(variable)} "
    )
    return (
        f"coalesce({variable}.scope, 'PERSISTENT') <> 'CANDIDATE' "
        f"AND coalesce({variable}.freshness, 'ACTIVE') <> 'GONE' "
        f"{retired_clause}"
        f"AND (NOT coalesce({variable}.is_view, false) OR ("
        f"{variable}.view_class = 'FACT' "
        f"AND {variable}.view_audience = 'RECALL' "
        f"AND coalesce({variable}.view_current, {variable}.qs_current, false) "
        f"AND NOT coalesce({variable}.retired, false) "
        f"AND {view_live_provenance_cypher(variable)}"
        f"))"
    )


def memory_lifecycle_note(
    *, status: object = None, artifact_status: object = None, superseded_by: object = None,
) -> str | None:
    """Label stored history without changing recall eligibility or its original content."""
    notes = []
    if str(status or "").strip().lower() == "completed":
        notes.append("Completed memory; historical context, not an outstanding obligation.")
    if str(artifact_status or "").strip().lower() == "historical" or str(superseded_by or "").strip():
        notes.append("Historical artifact; superseded content, not current guidance.")
    return " ".join(notes) or None
