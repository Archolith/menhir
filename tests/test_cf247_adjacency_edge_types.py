"""CF-247 -- which relationship types establish recall adjacency, decided rather than inherited.

Both the adjacency producer (`fetch_adjacency_pairs`) and the reinforcement consumer
(`increment_edge_weights`) matched relationships UNTYPED: `MATCH (a)-[r]-(b)` and
`MATCH ()-[r]->()`. The effective contract was "every relationship type in the graph establishes
adjacency and earns traversal reinforcement" -- not a decision anyone recorded, just what an
untyped pattern gives you. 30+ types are created across the codebase.

**The entry's own severity estimate was never measured, and the measurement narrows it hard.**
Both endpoints of an adjacency row must be recall candidates -- non-structural `:Entity` /
`:Episodic` -- and `context_node_ids` is accepted by `run_recall` but populated by NO caller, so
the candidate set is exactly the recall results. Checking every type's endpoint labels at source:

* `ANCHORED_TO` targets a STRUCTURAL entity, which the recall filter excludes.
* `CURRENT_ANCHOR`, `CONTRIBUTED_TO`, `SUPERSEDED_ANCHOR`, `HISTORY_ENTRY` target `:TypedAssertion`.
* `ADMITTED_ON` targets `:TurnEvidence`. `HAS_MEMBER` / `HAS_EPISODE` target communities.
* The artifact/todo/work-artifact edges target `:WorkArtifact`, `:Todo`, `:ArtifactSource`, ...

None of those is recallable, so none was ever in play. Exactly FOUR types could reach adjacency:
`RELATES_TO`, `MENTIONS`, `NEXT_EPISODE`, `SUPERSEDES`.

**So two of the entry's own examples do not hold.** It names `ANCHORED_TO` as View plumbing that
earns reinforcement -- it cannot appear at all. And it says reinforcing `SUPERSEDES` "teaches the
ranker that a superseded value is strongly associated with its replacement" -- but that edge is
written with no `uuid` property, and reinforcement only reaches uuid-bearing edges, so it is never
reinforced. The ADJACENCY half of that concern is real and is the live defect: recalling a current
artifact gave its superseded predecessor a ranking boost.

Owner ruling 2026-08-23: allowlist `RELATES_TO` and `MENTIONS`. `NEXT_EPISODE` is dropped too --
temporal succession is not semantic relatedness, and two consecutive episodes about unrelated
subjects should not rank each other up.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from menhir.domain.recall import ADJACENCY_EDGE_TYPES, adjacency_edge_pattern
from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.memory_queries import MemoryQueryRepository


@dataclass
class _StubNeo4j:
    responses: list[list[dict]] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def execute(self, query: str, params: dict | None = None, **kwargs) -> list[dict]:
        self.calls.append({"query": query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


# ---------------------------------------------------------------------------
# The ruling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_allowlist_is_exactly_the_owner_ruling() -> None:
    """Pinned as a decision, not a default. Widening this is a semantic change to what recall
    considers "related", so it must be a deliberate edit that fails this test first."""
    assert ADJACENCY_EDGE_TYPES == ("RELATES_TO", "MENTIONS")


@pytest.mark.unit
@pytest.mark.parametrize("excluded", ["SUPERSEDES", "NEXT_EPISODE", "ANCHORED_TO", "ADMITTED_ON"])
def test_the_types_the_ruling_dropped_stay_dropped(excluded: str) -> None:
    """SUPERSEDES is the one that was doing damage: it connects two `:Entity` artifacts, so a
    recalled artifact boosted the predecessor it replaced."""
    assert excluded not in ADJACENCY_EDGE_TYPES


# ---------------------------------------------------------------------------
# Producer and consumer must agree
# ---------------------------------------------------------------------------


def _rendered(repo_call) -> str:
    neo4j = _StubNeo4j()
    repo_call(neo4j)
    return neo4j.calls[0]["query"]


@pytest.mark.unit
def test_the_producer_emits_the_typed_pattern() -> None:
    query = _rendered(
        lambda n: MemoryQueryRepository(n).fetch_adjacency_pairs(["a", "b"])
    )

    assert f"MATCH (a)-[r:{adjacency_edge_pattern()}]-(b)" in query


@pytest.mark.unit
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda n: ConsolidationRepository(n).increment_edge_weight("e-1"), id="single"),
        pytest.param(
            lambda n: ConsolidationRepository(n).increment_edge_weights(["e-1"]), id="batch"
        ),
    ],
)
def test_the_consumer_emits_the_typed_pattern(call) -> None:
    assert f"MATCH ()-[r:{adjacency_edge_pattern()}]->()" in _rendered(call)


@pytest.mark.unit
def test_producer_and_consumer_use_the_same_type_list() -> None:
    """THE FINDING. Narrowing the consumer alone produces a silent disagreement: recall ranks on an
    edge it then declines to reinforce, and the two drift apart with nothing to catch it. One
    emitter, both sites -- so a change to the ruling reaches both or neither.
    """
    producer = _rendered(lambda n: MemoryQueryRepository(n).fetch_adjacency_pairs(["a", "b"]))
    consumer = _rendered(lambda n: ConsolidationRepository(n).increment_edge_weights(["e-1"]))

    pattern = adjacency_edge_pattern()
    assert f"[r:{pattern}]" in producer
    assert f"[r:{pattern}]" in consumer


@pytest.mark.unit
@pytest.mark.parametrize(
    "func",
    [
        MemoryQueryRepository.fetch_adjacency_pairs,
        ConsolidationRepository.increment_edge_weight,
        ConsolidationRepository.increment_edge_weights,
    ],
)
def test_no_untyped_relationship_match_survives_in_these_functions(func) -> None:
    """RATCHET. An untyped pattern here silently re-opens the finding, and it reads as innocuous.

    Anchored to a MATCH on a non-comment line, and needles assembled so this file does not match
    itself. The first version of this test scanned raw source and fired on the explanatory COMMENT
    that quotes the old pattern -- the same way CF-250's ratchet fired on CF-75's own comment. A
    guard that flags its own documentation gets deleted rather than obeyed, so it has to read
    syntax rather than prose.
    """
    untyped = "-" + "[r]" + "-"
    offenders = [
        line.strip()
        for line in inspect.getsource(func).splitlines()
        if "MATCH" in line
        and not line.lstrip().startswith("#")
        and untyped in line
    ]

    assert not offenders, f"untyped relationship match: {offenders}"


@pytest.mark.unit
def test_editing_the_ruling_reaches_the_emitted_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the emitter stopped reading `ADJACENCY_EDGE_TYPES`, editing the ruling would silently
    change nothing -- the failure mode that makes a single-source-of-truth fix cosmetic.

    The first version of this test asserted `pattern == "|".join(TYPES)`, which a HARDCODED
    "RELATES_TO|MENTIONS" satisfies exactly, because the two agree today. Mutation caught it. The
    property is not "these are equal now", it is "changing the tuple changes the pattern".
    """
    monkeypatch.setattr(
        "menhir.domain.recall.ADJACENCY_EDGE_TYPES", ("RELATES_TO", "MENTIONS", "SPECULATIVE")
    )

    assert adjacency_edge_pattern() == "RELATES_TO|MENTIONS|SPECULATIVE"


# ---------------------------------------------------------------------------
# The uuid gate, documented rather than deleted (owner ruling, second half)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_edge_with_no_uuid_ranks_but_is_never_reinforced() -> None:
    """THE SECOND, INDEPENDENT CONDITION. Reinforcement reaches an edge only if BOTH its type is
    allowlisted AND it carries `r.uuid`. The uuid half already held before CF-247 -- `edge_index`
    is only populated `if edge_uuid` -- but nothing said so, so it read as an accident.

    Recorded as a real control: a `MENTIONS` edge written without a uuid contributes to ranking
    and must not be ratcheted, because there is no stable identity to ratchet.
    """
    from menhir.services.recall_support import RecallSupportMixin

    class _Adapter:
        def fetch_adjacency_pairs(self, *_args):
            return [
                {"source": "a", "target": "b", "weight": 2.0, "edge_uuid": None},
                {"source": "a", "target": "c", "weight": 1.0, "edge_uuid": "edge-1"},
            ]

    service = RecallSupportMixin()
    service.graph_adapter = _Adapter()

    adjacency_map, edge_index = asyncio.run(
        service._compute_adjacency(["a", "b", "c"], None, None)
    )

    assert adjacency_map, "the uuid-less edge must still contribute to ranking"
    assert list(edge_index.values()) == [["edge-1"]], (
        "an edge with no uuid was queued for reinforcement"
    )


# ---------------------------------------------------------------------------
# Execution-level proof (online)
# ---------------------------------------------------------------------------


@pytest.mark.online
def test_a_supersedes_edge_no_longer_creates_adjacency(test_neo4j_repo) -> None:
    """THE LIVE DEFECT, asserted against a real graph.

    `artifact_repository.supersede_artifact` writes `(new:Entity)-[:SUPERSEDES]->(old:Entity)`.
    Both are recall candidates, so before this change recalling the current artifact boosted the
    superseded one -- the ranker learning that stale content is strongly associated with the thing
    that replaced it.
    """
    new_uuid, old_uuid, related_uuid = str(uuid4()), str(uuid4()), str(uuid4())
    test_neo4j_repo.execute(
        """
        CREATE (new:Entity {uuid: $new, name: 'current'})
        CREATE (old:Entity {uuid: $old, name: 'superseded'})
        CREATE (rel:Entity {uuid: $rel, name: 'genuinely related'})
        CREATE (new)-[:SUPERSEDES]->(old)
        CREATE (new)-[:RELATES_TO {uuid: 'r-1'}]->(rel)
        """,
        {"new": new_uuid, "old": old_uuid, "rel": related_uuid},
    )

    rows = MemoryQueryRepository(test_neo4j_repo).fetch_adjacency_pairs(
        [new_uuid, old_uuid, related_uuid]
    )

    pairs = {frozenset((str(r["source"]), str(r["target"]))) for r in rows}
    assert frozenset((new_uuid, related_uuid)) in pairs, "RELATES_TO must still establish adjacency"
    assert frozenset((new_uuid, old_uuid)) not in pairs, (
        "a superseded artifact is still boosting its replacement"
    )


@pytest.mark.online
def test_reinforcement_cannot_ratchet_a_disallowed_type_even_if_asked(test_neo4j_repo) -> None:
    """Defence in depth: the consumer refuses by TYPE, not merely because the producer stopped
    handing it that uuid. A caller passing a SUPERSEDES uuid directly must still be refused."""
    a_uuid, b_uuid = str(uuid4()), str(uuid4())
    test_neo4j_repo.execute(
        """
        CREATE (a:Entity {uuid: $a})
        CREATE (b:Entity {uuid: $b})
        CREATE (a)-[:SUPERSEDES {uuid: 'sup-1', weight: 1.0}]->(b)
        CREATE (a)-[:RELATES_TO {uuid: 'rel-1', weight: 1.0}]->(b)
        """,
        {"a": a_uuid, "b": b_uuid},
    )

    updated = ConsolidationRepository(test_neo4j_repo).increment_edge_weights(["sup-1", "rel-1"])

    assert updated == 1, "exactly the RELATES_TO edge should have been ratcheted"
    rows = test_neo4j_repo.execute(
        "MATCH ()-[r]->() WHERE r.uuid IN ['sup-1', 'rel-1'] RETURN r.uuid AS uuid, r.weight AS w"
    )
    weights = {str(r["uuid"]): float(r["w"]) for r in rows}
    assert weights["sup-1"] == 1.0, "a SUPERSEDES edge was reinforced"
    assert weights["rel-1"] > 1.0
