"""CF-75 -- two recall-path N+1 loops become one round trip each, and one of them was also wrong.

CF-75 filed four sequential-await loops and graded itself STRUCTURAL, saying plainly that *"the
per-trip cost and therefore the total latency are unmeasured"*. Measured against Neo4j 5 on the
test instance (21,000 relationships, 200 episodes), the two loops that scale with recall result
size do NOT share a cost profile, which is why they are recorded separately here:

    increment_edge_weight       ONE call    63,008 dbHits   <- an all-relationship scan, per call
      N=50 serial (today)                3,150,400
      N=50 batched                          63,200          ~50x, and flat in N

    fetch_linked_entity_uuids_for_episode
      N=50 serial                              900
      N=50 batched (UNWIND)                  1,150          <- batching costs MORE dbHits

So the edge loop is a database-work defect and the episode loop is purely a round-trip defect. The
entry's first-listed fix -- `asyncio.gather` -- would have left every one of those 50 scans in
place, merely concurrent. Its second, *"one query taking the whole list"*, is the one that works.

**A correctness bug was found inside the loop the entry only accuses of being slow.**
`increment_edge_weight` matched `()-[r]-()`, which yields each relationship TWICE (once per
direction), so the ratchet applied twice: weight 1.0 -> 1.2 per call against a documented "+0.1 per
traversal", reaching the 5.0 cap in half the intended traversals. `test_edge_weight_cap_contract`
could not see it -- it greps this function's SOURCE for the string "0.1" rather than running the
query. Owner ruling 2026-08-22: fix it inside CF-75, accepting that live reinforcement now moves at
the documented rate.

**The query stays UNTYPED, and that was also ruled.** Naming relationship types would let the
existing per-type `uuid` indexes turn the scan into an index seek -- 42,100 -> 351 dbHits at N=50,
another ~120x. It is not taken, because `edge_index` is populated by `fetch_adjacency_pairs`, which
is itself untyped: naming a list here and not there would make the producer say "any relationship
establishes adjacency" while the consumer says "only these reinforce", and every edge outside the
list would silently stop being reinforced with no error to notice it by. Ruled: define that
allowlist on semantics or not at all. A census found **35+ relationship types** in this graph --
including view machinery (`CURRENT`, `ANCHORED_TO`, `SUPERSEDED_ANCHOR`) where traversal
reinforcement is meaningless, and provenance edges (`SUPERSEDES`) where it is arguably harmful --
so the allowlist could not be defined honestly here. Filed as CF-247.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark_unit = pytest.mark.unit


# ---------------------------------------------------------------------------
# Offline: the call sites issue ONE round trip. Runs in the default lane.
# ---------------------------------------------------------------------------


class _CountingAdapter:
    """Records how many times each graph call is made, and with what."""

    def __init__(self, linked: dict[str, list[str]] | None = None) -> None:
        self.linked = linked or {}
        self.singular_episode_calls: list[str] = []
        self.batched_episode_calls: list[list[str]] = []
        self.singular_edge_calls: list[str] = []
        self.batched_edge_calls: list[list[str]] = []
        self.touched: list[str] = []

    def fetch_linked_entity_uuids_for_episode(self, episode_uuid: str) -> list[str]:
        self.singular_episode_calls.append(episode_uuid)
        return list(self.linked.get(episode_uuid, []))

    def fetch_linked_entity_uuids_for_episodes(
        self, episode_uuids: list[str]
    ) -> dict[str, list[str]]:
        self.batched_episode_calls.append(list(episode_uuids))
        return {u: list(self.linked.get(u, [])) for u in episode_uuids}

    def increment_edge_weight(self, edge_uuid: str) -> bool:
        self.singular_edge_calls.append(edge_uuid)
        return True

    def increment_edge_weights(self, edge_uuids: list[str]) -> int:
        self.batched_edge_calls.append(list(edge_uuids))
        return len(edge_uuids)

    def touch_retrieved_nodes(self, node_uuids: list[str]) -> int:
        self.touched = list(node_uuids)
        return len(node_uuids)


def _support(adapter: _CountingAdapter) -> Any:
    from menhir.services.recall_support import RecallSupportMixin

    support = RecallSupportMixin.__new__(RecallSupportMixin)
    support.graph_adapter = adapter
    support.lifecycle_service = None
    return support


class _Result:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid
        self.memory_type = "SEMANTIC"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_reinforcement_is_one_round_trip_for_every_traversed_edge() -> None:
    """THE FINDING. Four edges cost four all-relationship scans; they now cost one query."""
    adapter = _CountingAdapter()
    edge_index = {
        ("a", "b"): ["e1", "e2"],
        ("b", "c"): ["e3"],
        ("c", "d"): ["e4"],
    }
    results = [_Result(u) for u in ("a", "b", "c", "d")]

    await _support(adapter)._post_recall_updates(results, {}, edge_index)

    assert adapter.singular_edge_calls == [], "still calling the per-edge query"
    assert len(adapter.batched_edge_calls) == 1, adapter.batched_edge_calls
    assert sorted(adapter.batched_edge_calls[0]) == ["e1", "e2", "e3", "e4"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_edges_between_two_returned_results_are_reinforced() -> None:
    """POSITIVE CONTROL, and the one that matters most. Batching makes it trivially easy to send
    the whole `edge_index` instead of the traversed subset, which would reinforce edges the caller
    never actually reached -- corrupting the ranking signal rather than merely slowing it."""
    adapter = _CountingAdapter()
    edge_index = {
        ("a", "b"): ["kept"],
        ("a", "zz"): ["dropped-one-endpoint-missing"],
    }

    await _support(adapter)._post_recall_updates(
        [_Result("a"), _Result("b")], {}, edge_index
    )

    assert adapter.batched_edge_calls == [["kept"]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_traversed_edges_issues_no_query_at_all() -> None:
    """A batched call with an empty list is still a round trip. Recall returning unconnected
    results is the common case, not an edge case."""
    adapter = _CountingAdapter()

    await _support(adapter)._post_recall_updates([_Result("a")], {}, {})

    assert adapter.batched_edge_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_duplicated_edge_is_reinforced_once() -> None:
    """The serial loop carried a `seen_edges` set, so one edge appearing under two node pairs was
    ratcheted once. Dropping that during the batch rewrite would double the reinforcement rate for
    exactly the most-connected edges."""
    adapter = _CountingAdapter()
    edge_index = {("a", "b"): ["shared"], ("b", "a"): ["shared"]}

    await _support(adapter)._post_recall_updates(
        [_Result("a"), _Result("b")], {}, edge_index
    )

    assert adapter.batched_edge_calls == [["shared"]]


# ---------------------------------------------------------------------------
# Offline: the episode loop, where ORDER is the thing that can silently break
# ---------------------------------------------------------------------------


class _ShuffledAdapter(_CountingAdapter):
    """Returns the batched map in an order that DISAGREES with the caller's episode list.

    A real Neo4j returns aggregated rows in planner order, which is not the input order. A dict
    built straight from those rows and then iterated would inherit that order -- so the stub has to
    disagree, or the test proves nothing about the re-ordering.
    """

    def fetch_linked_entity_uuids_for_episodes(
        self, episode_uuids: list[str]
    ) -> dict[str, list[str]]:
        super().fetch_linked_entity_uuids_for_episodes(episode_uuids)
        return {u: list(self.linked.get(u, [])) for u in reversed(episode_uuids)}

    # --- the rest of what `_wait_for_pending_episodes` touches ---
    def fetch_relevant_pending_episodes(self, query, limit=3, namespace=None):
        return [{"uuid": u, "processing_state": "READY"} for u in ("ep1", "ep2", "ep3")]

    def fetch_episode_processing(self, uuid):
        return {
            "uuid": uuid,
            "processing_state": "READY",
            "resolved_episode_uuid": uuid,
            "linked_entity_uuids": [],
        }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_batched_episode_lookup_preserves_the_serial_loops_entity_order() -> None:
    """THE SUBTLE ONE, exercised through the real call site rather than re-derived here.

    The entity list feeds `dict.fromkeys`, which dedupes by FIRST occurrence -- so entity order
    decides which duplicate survives, and therefore recall's candidate order. The batched query
    returns rows in planner order, so `_wait_for_pending_episodes` has to re-order by its own
    episode list. The adapter here deliberately returns the map reversed: consuming it as returned
    yields z, shared, y, x and the assertion fails.
    """
    adapter = _ShuffledAdapter(
        linked={"ep1": ["x", "shared"], "ep2": ["shared", "y"], "ep3": ["z"]}
    )
    support = _support(adapter)
    support.ingest_service = object()

    _visible, entity_uuids = await support._wait_for_pending_episodes(
        "q", limit=3, timeout_s=0.0
    )

    assert entity_uuids == ["x", "shared", "y", "z"]


# ---------------------------------------------------------------------------
# Online: the ratchet rate and the batched query, against a real graph.
# The double-application cannot be reproduced by any stub -- it is a property of how Neo4j
# expands an undirected pattern, so only a real database can show it.
# ---------------------------------------------------------------------------


def _seed_edges(repo: Any, count: int = 12) -> None:
    repo.execute("MATCH (n) DETACH DELETE n")
    repo.execute(
        "UNWIND range(1,$n) AS i "
        "CREATE (a:Entity {uuid:'a'+toString(i)})-[:RELATES_TO {uuid:'e'+toString(i), weight:1.0}]->"
        "(b:Entity {uuid:'b'+toString(i)})",
        params={"n": count},
    )


def _weight(repo: Any, edge_uuid: str) -> float:
    rows = repo.execute(
        "MATCH ()-[r]->() WHERE r.uuid = $u RETURN r.weight AS w", params={"u": edge_uuid}
    )
    return float(rows[0]["w"])


@pytest.mark.online
def test_one_traversal_raises_the_weight_by_the_documented_step(test_neo4j_repo) -> None:
    """THE CORRECTNESS BUG. `()-[r]-()` yields each relationship once per direction, so the SET ran
    twice and one traversal moved the weight 1.0 -> 1.2. The contract is +0.1 per traversal.

    This is the assertion `test_edge_weight_cap_contract` cannot make: it greps the source for the
    literal "0.1", which was present and correct the whole time the behaviour was wrong.
    """
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    _seed_edges(test_neo4j_repo)
    repo = ConsolidationRepository(test_neo4j_repo)

    repo.increment_edge_weight("e1")

    assert _weight(test_neo4j_repo, "e1") == pytest.approx(1.1), (
        "the ratchet applied more than once for a single traversal"
    )


@pytest.mark.online
def test_the_batched_ratchet_matches_the_singular_one_step_for_step(test_neo4j_repo) -> None:
    """The batch must not be a different ratchet. Same edge, same number of traversals, same
    weight -- otherwise recall silently reinforces at a different rate than every other caller."""
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    _seed_edges(test_neo4j_repo)
    repo = ConsolidationRepository(test_neo4j_repo)

    repo.increment_edge_weight("e1")
    repo.increment_edge_weight("e1")
    repo.increment_edge_weights(["e2"])
    repo.increment_edge_weights(["e2"])

    assert _weight(test_neo4j_repo, "e1") == pytest.approx(_weight(test_neo4j_repo, "e2"))


@pytest.mark.online
def test_the_batch_reinforces_every_listed_edge_and_no_others(test_neo4j_repo) -> None:
    """POSITIVE CONTROL against the real query. `WHERE r.uuid IN $edge_uuids` is one character away
    from matching everything, and a ratchet that hit every edge would still return a plausible
    count."""
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    _seed_edges(test_neo4j_repo)
    repo = ConsolidationRepository(test_neo4j_repo)

    updated = repo.increment_edge_weights(["e2", "e3", "e5"])

    assert updated == 3
    for touched in ("e2", "e3", "e5"):
        assert _weight(test_neo4j_repo, touched) == pytest.approx(1.1), touched
    for untouched in ("e1", "e4", "e6"):
        assert _weight(test_neo4j_repo, untouched) == pytest.approx(1.0), untouched


@pytest.mark.online
def test_the_batched_ratchet_still_caps_at_five(test_neo4j_repo) -> None:
    """The cap is the reason the CASE exists; a batch that dropped it would let hot edges run away
    and dominate adjacency ranking permanently."""
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    _seed_edges(test_neo4j_repo)
    test_neo4j_repo.execute("MATCH ()-[r]->() WHERE r.uuid='e1' SET r.weight = 4.95")
    repo = ConsolidationRepository(test_neo4j_repo)

    repo.increment_edge_weights(["e1"])
    repo.increment_edge_weights(["e1"])

    assert _weight(test_neo4j_repo, "e1") == pytest.approx(5.0)


@pytest.mark.online
def test_an_empty_batch_touches_nothing(test_neo4j_repo) -> None:
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    _seed_edges(test_neo4j_repo)
    repo = ConsolidationRepository(test_neo4j_repo)

    assert repo.increment_edge_weights([]) == 0
    assert _weight(test_neo4j_repo, "e1") == pytest.approx(1.0)


@pytest.mark.online
def test_the_batched_episode_lookup_agrees_with_the_singular_one(test_neo4j_repo) -> None:
    """Equivalence against the real query, not against a stub that was written to agree."""
    from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository

    test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
    test_neo4j_repo.execute(
        "UNWIND range(1,6) AS i "
        "CREATE (e:Episodic {uuid:'ep'+toString(i)}) "
        "CREATE (n:Entity {uuid:'ent'+toString(i)}) "
        "CREATE (e)-[:MENTIONS {uuid:'m'+toString(i)}]->(n)"
    )
    repo = EpisodeLifecycleRepository()
    repo.neo4j = test_neo4j_repo
    episodes = ["ep1", "ep3", "ep5", "ep-missing"]

    batched = repo.fetch_linked_entity_uuids_for_episodes(episodes)

    for episode in episodes:
        assert sorted(batched.get(episode, [])) == sorted(
            repo.fetch_linked_entity_uuids_for_episode(episode)
        ), episode
