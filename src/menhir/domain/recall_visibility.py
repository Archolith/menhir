"""Shared default-recall visibility policy for memory and View nodes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def session_scope_visible(
    memory: Mapping[str, Any], session_id: str | None
) -> bool:
    """Return whether a caller may see one ordinary memory row.

    Non-SESSION rows are unaffected. SESSION rows require a non-empty caller ID and either
    durable source-episode provenance for that session or the legacy scalar owner stamp.
    The provenance set handles Graphiti entity reuse across sessions; the scalar preserves
    compatibility for old rows whose MENTIONS provenance is unavailable.
    """

    if str(memory.get("scope") or "SESSION") != "SESSION":
        return True

    effective_session_id = str(session_id or "").strip()
    if not effective_session_id:
        return False

    provenance_session_ids = memory.get("provenance_session_ids")
    provenance_members = (
        {
            str(value).strip()
            for value in provenance_session_ids
            if str(value or "").strip()
        }
        if isinstance(provenance_session_ids, (list, tuple, set, frozenset))
        else set()
    )
    legacy_owner = str(memory.get("session_id") or "").strip()
    return effective_session_id in provenance_members or (
        not provenance_members and legacy_owner == effective_session_id
    )


def default_recent_context_visible(memory: Mapping[str, Any]) -> bool:
    """Fail closed for raw rows that cannot safely represent current context.

    READY Episodic rows preserve source text, including superseded statements, and therefore
    belong in provenance/history inspection rather than the unqualified recent-context lane.
    An Entity carrying any invalidated fact is likewise unsafe to render as one undifferentiated
    summary; semantic recall can still return its current fact-level projection.
    """

    labels = memory.get("labels")
    if isinstance(labels, (list, tuple, set, frozenset)) and "Episodic" in {
        str(label) for label in labels
    }:
        return False
    if str(memory.get("type") or "").upper() == "EPISODIC":
        return False
    return memory.get("has_invalidated_facts") is not True


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
