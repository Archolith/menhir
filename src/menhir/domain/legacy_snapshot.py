"""Reading the LOSSY historical merge snapshot -- and being honest about what it cannot restore.

Merges performed before the Phase 4 lossless snapshot wrote an audit entry with a small fixed
property allowlist, Entity-only peers, a single relationship property (``weight``), stringified
temporal values, and NO survivor state. Those merges can be PARTIALLY reversed. They can never be
exactly reversed, and the danger is presenting a partial restoration as a recovery.

This reader also serves NEWER graph/sidecar entries, which reach it whenever the journal row that
would have made an EXACT unmerge possible is gone (recovery tier ``GRAPH_SNAPSHOT_ONLY``). So the
allowlist tracks what an entry MAY carry, not only what the oldest ones did -- see
``LEGACY_PROPERTY_ALLOWLIST``. It stays a degraded path either way; a richer entry restores more of
the node, never all of it.

So this module does two things:

1. Parses the legacy entry into the shape the degraded restore needs.
2. Enumerates, per snapshot, the classes of state that are STRUCTURALLY unrecoverable from it.

The second is the point. Every degraded restore reports its omissions, and the operator has to
acknowledge them. We never silently hand back a damaged graph and call it repaired.
"""

from __future__ import annotations

import json

from menhir.domain.erasure import is_erased_marker
from typing import Any

#: The node properties a graph/sidecar audit entry can carry, across BOTH vintages.
#:
#: The first ten are all a legacy entry ever captured (merge_entity's old audit entry).
#: `sources` and `corroboration` were added when merge began deriving provenance from the contributor
#: list rather than accumulating it; entries written before that simply omit them. Both vintages read
#: through this one allowlist because `absorbed_properties` already drops absent values -- an old
#: entry is unaffected by their presence here.
#:
#: They MUST be listed. A degraded restore is driven off this allowlist, so a newer entry whose
#: provenance was captured would otherwise resurrect the node with its contributor list erased and
#: its corroboration reset -- discarding, at the last possible moment, exactly the state the audit
#: entry was extended to preserve.
LEGACY_PROPERTY_ALLOWLIST = (
    "uuid", "name", "summary", "content", "source",
    "source_confidence", "type", "scope", "created_at", "namespace",
    "sources", "corroboration",
)

# Stable codes for the classes of state a legacy snapshot cannot restore. Surfaced to the operator
# verbatim, and asserted in tests, so this contract cannot quietly weaken.
OMITS_NODE_LABELS = "OMITS_NODE_LABELS"
OMITS_PROPERTIES_OUTSIDE_ALLOWLIST = "OMITS_PROPERTIES_OUTSIDE_ALLOWLIST"
OMITS_TYPED_TEMPORAL_VALUES = "OMITS_TYPED_TEMPORAL_VALUES"
OMITS_NON_ENTITY_PEERS = "OMITS_NON_ENTITY_PEERS"
OMITS_RELATIONSHIP_PROPERTIES = "OMITS_RELATIONSHIP_PROPERTIES"
OMITS_SURVIVOR_PRE_MERGE_STATE = "OMITS_SURVIVOR_PRE_MERGE_STATE"
OMITS_SURVIVOR_EPISODE_BASELINE = "OMITS_SURVIVOR_EPISODE_BASELINE"


class LegacySnapshotError(ValueError):
    """The legacy audit entry is malformed and cannot be used."""


class SnapshotErasedError(LegacySnapshotError):
    """The audit entry was erased on request, so this merge is deliberately unrecoverable.

    A subclass so existing handlers keep working, but a distinct type so a caller that cares
    can tell "erased under CF-165" from "corrupt". Reporting an erasure as corruption sends an
    operator hunting a data-integrity bug that does not exist.
    """


def parse(snapshot_json: str) -> dict[str, Any]:
    """Parse a legacy audit entry. Raises rather than guessing at a malformed one."""
    if is_erased_marker(snapshot_json):
        raise SnapshotErasedError(
            "legacy snapshot was erased on request (CF-165); this merge is intentionally "
            "unrecoverable, not corrupt"
        )
    try:
        entry = json.loads(snapshot_json)
    except (TypeError, ValueError) as exc:
        raise LegacySnapshotError(f"legacy snapshot is not valid JSON: {exc}") from exc
    if not isinstance(entry, dict):
        raise LegacySnapshotError("legacy snapshot is not an object")
    absorbed_uuid = entry.get("absorbed_uuid")
    survivor_uuid = entry.get("survivor_uuid")
    if not absorbed_uuid or not survivor_uuid:
        raise LegacySnapshotError("legacy snapshot is missing survivor_uuid/absorbed_uuid")
    props = entry.get("properties")
    if not isinstance(props, dict) or not props.get("uuid"):
        raise LegacySnapshotError("legacy snapshot has no usable absorbed-node properties")
    return entry


def degradations(entry: dict[str, Any]) -> list[str]:
    """The classes of state this snapshot structurally cannot restore.

    The first six are inherent to the legacy schema and always apply -- the schema simply never
    recorded them. The last depends on the entry's vintage: the OLDEST snapshots predate
    ``survivor_episodes_before``, so we cannot know which MENTIONS the merge rebound onto the
    survivor and therefore cannot invert the rebind at all.
    """
    out = [
        OMITS_NODE_LABELS,
        OMITS_PROPERTIES_OUTSIDE_ALLOWLIST,
        OMITS_TYPED_TEMPORAL_VALUES,
        OMITS_NON_ENTITY_PEERS,
        OMITS_RELATIONSHIP_PROPERTIES,
        OMITS_SURVIVOR_PRE_MERGE_STATE,
    ]
    if "survivor_episodes_before" not in entry:
        out.append(OMITS_SURVIVOR_EPISODE_BASELINE)
    return out


def absorbed_properties(entry: dict[str, Any]) -> dict[str, Any]:
    """The absorbed node's restorable properties. Nulls are dropped (Neo4j stores no null props).

    ``created_at`` was stringified by the old snapshot, so it comes back as a STRING, not a Neo4j
    temporal. That is precisely OMITS_TYPED_TEMPORAL_VALUES -- we restore the value we have and
    report the loss rather than fabricating a type.
    """
    props = entry.get("properties") or {}
    return {
        k: v for k, v in props.items()
        if k in LEGACY_PROPERTY_ALLOWLIST and v is not None
    }


def relationships(entry: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(out_rels, in_rels) in the shape the atomic restore takes.

    Only ``weight`` was ever captured, and only Entity peers, so this is a strict subset of the
    node's true incident edges. Relationship types are validated as identifiers here because the
    legacy entry is untrusted input.
    """
    out_rels: list[dict[str, Any]] = []
    in_rels: list[dict[str, Any]] = []
    for rel in entry.get("relationships") or []:
        peer = rel.get("peer_uuid")
        rtype = rel.get("rel_type")
        if not peer or not isinstance(rtype, str) or not rtype.replace("_", "").isalnum():
            continue
        props = {} if rel.get("weight") is None else {"weight": rel["weight"]}
        record = {"type": rtype, "peer_uuid": str(peer), "properties": props}
        (out_rels if rel.get("direction", "out") == "out" else in_rels).append(record)
    return out_rels, in_rels


def rebound_episodes(entry: dict[str, Any]) -> list[str]:
    """The MENTIONS the merge moved onto the survivor: the absorbed node's episodes MINUS the ones
    the survivor already had.

    When ``survivor_episodes_before`` is absent the snapshot predates MENTIONS rebinding entirely
    (the two shipped together), so nothing was rebound and there is nothing to remove.
    """
    absorbed_eps = [str(e) for e in (entry.get("mentioned_by_episodes") or [])]
    if "survivor_episodes_before" not in entry:
        return []
    before = {str(e) for e in (entry.get("survivor_episodes_before") or [])}
    return sorted(e for e in absorbed_eps if e not in before)


def absorbed_episodes(entry: dict[str, Any]) -> list[str]:
    """Episodes that MENTIONED the absorbed node, to be re-pointed back at it."""
    return [str(e) for e in (entry.get("mentioned_by_episodes") or [])]
