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


def default_recall_visibility_cypher(variable: str = "n") -> str:
    """Return the fail-closed predicate used by generic recall/listing surfaces.

    Ordinary memories remain visible unless they are candidates or gone. A materialized View has a
    stronger fail-closed contract: it must explicitly be a current, nonretired FACT for the RECALL
    audience and its durable contributor receipt must exactly equal its incoming ``MENTIONS``
    relationship set. OPERATOR Views remain available to explicit inspection paths.

    Explicit UUID inspection deliberately does not use this predicate; historical and invalid rows
    must remain inspectable by operator/provenance tooling even though they cannot enter context.
    """

    return (
        f"coalesce({variable}.scope, 'PERSISTENT') <> 'CANDIDATE' "
        f"AND coalesce({variable}.freshness, 'ACTIVE') <> 'GONE' "
        f"AND (NOT coalesce({variable}.is_view, false) OR ("
        f"{variable}.view_class = 'FACT' "
        f"AND {variable}.view_audience = 'RECALL' "
        f"AND coalesce({variable}.view_current, {variable}.qs_current, false) "
        f"AND NOT coalesce({variable}.retired, false) "
        f"AND {view_live_provenance_cypher(variable)}"
        f"))"
    )
