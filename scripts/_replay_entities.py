"""Copy an episode's linked entities from the read-only source graph into a replay namespace.

Without this, a replay harness that passes `linked_entities_for_episode=lambda _e: []` silently
restricts itself to SELF-subject facts. `bind_and_persist_typed_scalars` materializes a
ScalarStateView only under `is_bound and not binding_pending`; an unbound subject still persists a
:TypedAssertion (under an advisory uuid) but never calls `rebuild_scalar_state`. So every fact about
a non-self subject -- 'National Geographic', 'my car', a named friend -- commits at the gate and then
vanishes before the View, and the harness scores it as a fold failure that production would not have.

Measured cost of the omission on the LME panel: 34-42 of ~325 commits (10-13%) were structurally
unable to produce a View, and 7 of the 20 gate->fold gap cells were this artifact rather than a
menhir defect.

Entity uuids are REMAPPED per replay namespace for the same reason episode uuids are: every arm and
config materializes into its own throwaway namespace, and a shared entity uuid would let one arm's
binding decide another's.
"""

from __future__ import annotations

import uuid as _uuid
from typing import Any

#: Mirrors `EpisodeLifecycle.fetch_linked_entities_for_episode`: any relationship to a REAL entity,
#: with derived View nodes excluded (recallable Views are themselves :Entity and would otherwise be
#: offered as binding targets). Kept in sync with that query deliberately -- a looser rule here would
#: make the replay bind to candidates production never sees.
LINKED_ENTITIES_Q = (
    "MATCH (e:Episodic {uuid:$episode_uuid})-[]-(n:Entity) "
    "WHERE NOT coalesce(n.is_view, false) AND NOT coalesce(n.is_quantstate, false) "
    "AND n.view_kind IS NULL AND n.uuid IS NOT NULL AND n.name IS NOT NULL "
    "RETURN DISTINCT n.uuid AS uuid, n.name AS name"
)


def fetch_linked_entities(session, episode_uuid: str) -> list[dict[str, str]]:
    """Binding candidates for one SOURCE episode, read from the read-only graph."""
    return [
        {"uuid": str(r["uuid"]), "name": str(r["name"])}
        for r in session.run(LINKED_ENTITIES_Q, {"episode_uuid": episode_uuid}).data()
    ]


def materialize_linked_entities(
    repo: Any, *, namespace: str, fresh_episode_uuid: str,
    entities: list[dict[str, str]], salt: str,
) -> int:
    """Recreate `entities` inside `namespace` and link them to the remapped episode.

    Returns the number of entities written. The relationship type is MENTIONS to match how graphiti
    links episodes to extracted entities; the production lookup matches ANY relationship, so the
    exact type only has to be something the traversal will find.
    """
    written = 0
    for entity in entities:
        fresh = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{salt}|entity|{entity['uuid']}"))
        repo.execute(
            "MERGE (n:Entity {uuid:$uuid}) SET n.name=$name, n.group_id=$ns "
            "WITH n MATCH (e:Episodic {uuid:$episode}) MERGE (e)-[:MENTIONS]->(n)",
            {"uuid": fresh, "name": entity["name"], "ns": namespace,
             "episode": fresh_episode_uuid},
        )
        written += 1
    return written
