"""#574 live: index-backed similarity search returns what the scan returns, and really uses the index.

Needs a throwaway Neo4j 5.18+ (``--run-online``, ``MENHIR_TEST_NEO4J_URI``); see tests/conftest.py.
"""

from __future__ import annotations

import os
import random

import pytest

pytestmark = [pytest.mark.online, pytest.mark.asyncio]

DIM = 8
BIG_GROUP = ""
SMALL_GROUP = "small"


def _vec(rnd: random.Random) -> list[float]:
    return [rnd.uniform(-1, 1) for _ in range(DIM)]


@pytest.fixture
def seeded(test_neo4j_repo):
    repo = test_neo4j_repo
    _drop_vector_indexes(repo)
    repo.execute("MATCH (n) DETACH DELETE n")
    rnd = random.Random(574)
    nodes = [
        {"uuid": f"n{i}", "group_id": SMALL_GROUP if i % 16 == 0 else BIG_GROUP, "e": _vec(rnd)}
        for i in range(640)
    ]
    repo.execute(
        "UNWIND $rows AS r CREATE (n:Entity {uuid: r.uuid, name: r.uuid, group_id: r.group_id, "
        "summary: '', created_at: datetime(), name_embedding: r.e})",
        params={"rows": nodes},
    )
    edges = [
        {"uuid": f"e{i}", "s": f"n{rnd.randrange(640)}", "t": f"n{rnd.randrange(640)}",
         "group_id": SMALL_GROUP if i % 16 == 0 else BIG_GROUP, "e": _vec(rnd)}
        for i in range(480)
    ]
    repo.execute(
        "UNWIND $rows AS r MATCH (a:Entity {uuid: r.s}), (b:Entity {uuid: r.t}) "
        "CREATE (a)-[:RELATES_TO {uuid: r.uuid, name: 'REL', fact: r.uuid, group_id: r.group_id, "
        "episodes: [], created_at: datetime(), fact_embedding: r.e}]->(b)",
        params={"rows": edges},
    )
    yield repo, rnd, edges
    _drop_vector_indexes(repo)


def _drop_vector_indexes(repo) -> None:
    """Every vector index, whatever its name: one left covering the same property would be used."""
    for row in repo.execute("SHOW VECTOR INDEXES YIELD name RETURN name"):
        repo.execute(f"DROP INDEX `{row['name']}` IF EXISTS")


def _create_indexes(repo) -> dict[str, str]:
    from menhir.infrastructure.vector_indexes import ensure_vector_indexes

    report = ensure_vector_indexes(repo.execute, DIM)
    repo.execute("CALL db.awaitIndexes(120)")
    return report


@pytest.fixture
async def graph_driver():
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    driver = Neo4jDriver(
        uri=os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        user=os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
        password=os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"),
        database=os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j"),
    )
    yield driver
    await driver.close()


@pytest.fixture
def scan_spy(monkeypatch):
    """Counts the hook's fallbacks to the stock scan functions."""
    from graphiti_core.search import search_utils

    calls = {"node": 0, "edge": 0}
    real_node, real_edge = search_utils.node_similarity_search, search_utils.edge_similarity_search

    async def node(*args, **kwargs):
        calls["node"] += 1
        return await real_node(*args, **kwargs)

    async def edge(*args, **kwargs):
        calls["edge"] += 1
        return await real_edge(*args, **kwargs)

    monkeypatch.setattr(search_utils, "node_similarity_search", node)
    monkeypatch.setattr(search_utils, "edge_similarity_search", edge)
    return calls, real_node, real_edge


async def test_default_install_creates_all_vector_indexes_once(seeded) -> None:
    from menhir.infrastructure.vector_indexes import VECTOR_INDEX_SPECS, ensure_vector_indexes

    repo, _, _ = seeded
    assert set(_create_indexes(repo).values()) == {"created"}
    rows = repo.execute("SHOW VECTOR INDEXES YIELD name, state, options RETURN name, state, options")
    by_name = {row["name"]: row for row in rows}
    assert set(by_name) >= {spec.name for spec in VECTOR_INDEX_SPECS}
    for spec in VECTOR_INDEX_SPECS:
        config = by_name[spec.name]["options"]["indexConfig"]
        assert by_name[spec.name]["state"] == "ONLINE"
        assert config["vector.dimensions"] == DIM
        assert config["vector.similarity_function"] == "COSINE"
        # Unquantized, so index scores are exact and the completeness margin can be tight.
        assert config.get("vector.quantization.enabled", False) is False
    # An upgrade restart: nothing new, every index reported as it stands.
    assert set(ensure_vector_indexes(repo.execute, DIM).values()) == {"online"}


@pytest.mark.parametrize(
    ("group_ids", "limit", "min_score"),
    [(None, 10, 0.6), ([BIG_GROUP], 15, 0.6), ([SMALL_GROUP], 10, 0.5), (None, 50, 0.0)],
)
async def test_node_search_matches_scan_and_uses_index(
    seeded, graph_driver, scan_spy, group_ids, limit, min_score
) -> None:
    from graphiti_core.search.search_filters import SearchFilters

    from menhir.infrastructure.graphiti_vector_search import MenhirSearchInterface, _StockDriverView

    repo, rnd, _ = seeded
    _create_indexes(repo)
    calls, real_node, _ = scan_spy
    hook = MenhirSearchInterface()
    for _ in range(15):
        probe = _vec(rnd)
        got = await hook.node_similarity_search(graph_driver, probe, SearchFilters(), group_ids, limit, min_score)
        want = await real_node(_StockDriverView(graph_driver), probe, SearchFilters(), group_ids, limit, min_score)
        assert [n.uuid for n in got] == [n.uuid for n in want]
    assert calls["node"] == 0, "the hook scanned instead of using the index"


@pytest.mark.parametrize(
    ("group_ids", "limit", "min_score"),
    [(None, 10, 0.6), ([BIG_GROUP], 20, 0.6), ([SMALL_GROUP], 10, 0.5)],
)
async def test_edge_search_matches_scan_and_uses_index(
    seeded, graph_driver, scan_spy, group_ids, limit, min_score
) -> None:
    from graphiti_core.search.search_filters import SearchFilters

    from menhir.infrastructure.graphiti_vector_search import MenhirSearchInterface, _StockDriverView

    repo, rnd, _ = seeded
    _create_indexes(repo)
    calls, _, real_edge = scan_spy
    hook = MenhirSearchInterface()
    for _ in range(15):
        probe = _vec(rnd)
        got = await hook.edge_similarity_search(
            graph_driver, probe, None, None, SearchFilters(), group_ids, limit, min_score
        )
        want = await real_edge(
            _StockDriverView(graph_driver), probe, None, None, SearchFilters(), group_ids, limit, min_score
        )
        assert [e.uuid for e in got] == [e.uuid for e in want]
    assert calls["edge"] == 0, "the hook scanned instead of using the index"


async def test_endpoint_pair_filter_scans(seeded, graph_driver, scan_spy) -> None:
    from graphiti_core.search.search_filters import SearchFilters

    from menhir.infrastructure.graphiti_vector_search import MenhirSearchInterface

    repo, rnd, edges = seeded
    _create_indexes(repo)
    calls, _, _ = scan_spy
    pair = [edges[0]["s"], edges[0]["t"]]
    flt = SearchFilters(edge_source_node_uuids=pair, edge_target_node_uuids=pair)
    got = await MenhirSearchInterface().edge_similarity_search(
        graph_driver, edges[0]["e"], None, None, flt, [edges[0]["group_id"]], 10, 0.6
    )
    assert edges[0]["uuid"] in {e.uuid for e in got}
    assert calls["edge"] == 1


async def test_absent_index_scans_and_a_dropped_index_falls_back(seeded, graph_driver, scan_spy) -> None:
    from graphiti_core.search.search_filters import SearchFilters

    from menhir.infrastructure.graphiti_vector_search import MenhirSearchInterface
    from menhir.infrastructure.vector_indexes import ENTITY_NAME_EMBEDDING

    repo, rnd, _ = seeded
    calls, _, _ = scan_spy
    hook = MenhirSearchInterface()
    probe = _vec(rnd)
    before = await hook.node_similarity_search(graph_driver, probe, SearchFilters(), None, 10, 0.6)
    assert calls["node"] == 1  # no index yet: scan

    _create_indexes(repo)
    hook.catalog.invalidate()
    indexed = await hook.node_similarity_search(graph_driver, probe, SearchFilters(), None, 10, 0.6)
    assert calls["node"] == 1 and [n.uuid for n in indexed] == [n.uuid for n in before]

    # Dropped after the catalog read: the index call fails, the hook scans and forgets the state.
    repo.execute(f"DROP INDEX {ENTITY_NAME_EMBEDDING.name}")
    after = await hook.node_similarity_search(graph_driver, probe, SearchFilters(), None, 10, 0.6)
    assert calls["node"] == 2 and [n.uuid for n in after] == [n.uuid for n in before]
    assert hook.catalog.needs_refresh()


async def test_index_queries_read_only_the_index_candidates(seeded, graph_driver) -> None:
    """Every candidate must come from the index call. Any scan or seek in a plan means the planner
    found another way in -- e.g. the group_id index, which in the default namespace is ~every node."""
    from graphiti_core.search.search_filters import SearchFilters

    from menhir.infrastructure.graphiti_vector_search import MenhirSearchInterface

    repo, rnd, _ = seeded
    _create_indexes(repo)
    sent: list[tuple[str, dict]] = []
    real_execute = graph_driver.execute_query

    async def recording(query, **kwargs):
        sent.append((query, {k: v for k, v in kwargs.items() if not k.endswith("_")}))
        return await real_execute(query, **kwargs)

    graph_driver.execute_query = recording
    hook = MenhirSearchInterface()
    await hook.node_similarity_search(graph_driver, _vec(rnd), SearchFilters(), [BIG_GROUP], 10, 0.6)
    await hook.edge_similarity_search(graph_driver, _vec(rnd), None, None, SearchFilters(), [BIG_GROUP], 10, 0.6)
    index_queries = [(q, p) for q, p in sent if "db.index.vector.query" in q]
    assert len(index_queries) >= 4  # stats + search, node and edge
    _assert_index_only(index_queries)


def _assert_index_only(queries: list[tuple[str, dict]]) -> None:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        auth=(os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"), os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")),
    )
    try:
        for query, params in queries:
            operators = _operators(driver.execute_query("EXPLAIN " + query, params).summary.plan)
            assert any(op.startswith("ProcedureCall") for op in operators), operators
            assert not any("Scan" in op or "Seek" in op for op in operators), (query, operators)
    finally:
        driver.close()


def _operators(plan) -> list[str]:
    ops = [plan["operatorType"]] if isinstance(plan, dict) else [plan.operator_type]
    children = plan.get("children", []) if isinstance(plan, dict) else plan.children
    for child in children:
        ops.extend(_operators(child))
    return ops


async def test_adapter_infers_dimensions_from_stored_embeddings(seeded) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    repo, _, _ = seeded
    report = MemoryGraphAdapter(neo4j=repo).ensure_vector_indexes()
    assert set(report.values()) == {"created"}
    dims = {r["d"] for r in repo.execute(
        "SHOW VECTOR INDEXES YIELD options RETURN options.indexConfig['vector.dimensions'] AS d"
    )}
    assert dims == {DIM}


async def test_empty_graph_without_configured_dimension_skips(test_neo4j_repo) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    _drop_vector_indexes(test_neo4j_repo)
    test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
    report = MemoryGraphAdapter(neo4j=test_neo4j_repo).ensure_vector_indexes()
    assert set(report.values()) == {"skipped:unknown_dimensions"}


class _CountingRepo:
    def __init__(self, repo) -> None:
        self.repo = repo
        self.queries: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None, **kwargs):
        self.queries.append(query)
        self.calls.append((query, dict(params or {})))
        return self.repo.execute(query, params=params, **kwargs)

    def index_calls(self) -> list[tuple[str, dict]]:
        return [(q, p) for q, p in self.calls if "db.index.vector.query" in q]


@pytest.fixture
def seeded_lanes(seeded):
    repo, rnd, _ = seeded
    episodes = [
        {"uuid": f"ep{i}", "ns": SMALL_GROUP if i % 16 == 0 else "",
         "state": "FAILED" if i % 7 == 0 else "COMPLETED", "e": _vec(rnd)}
        for i in range(400)
    ]
    repo.execute(
        "UNWIND $rows AS r CREATE (n:Episodic {uuid: r.uuid, content: r.uuid, source: 'test', "
        "namespace: r.ns, group_id: r.ns, processing_state: r.state, created_at: datetime(), "
        "content_embedding: r.e})",
        params={"rows": episodes},
    )
    assertions = [
        {"id": f"a{i}", "ns": SMALL_GROUP if i % 16 == 0 else "default",
         "superseded": i % 5 == 0, "e": _vec(rnd)}
        for i in range(400)
    ]
    repo.execute(
        "UNWIND $rows AS r CREATE (a:TypedAssertion {assertion_id: r.id, namespace: r.ns, "
        "superseded: r.superseded, binding_pending: false, stated_span: r.id, name_embedding: r.e})",
        params={"rows": assertions},
    )
    _create_indexes(repo)
    return repo, rnd


@pytest.mark.parametrize("namespace", [None, "default", SMALL_GROUP])
async def test_episode_lane_matches_scan_and_uses_index(seeded_lanes, namespace) -> None:
    from menhir.infrastructure.memory_queries import MemoryQueryRepository
    from menhir.infrastructure.vector_indexes import VectorIndexCatalog

    repo, rnd = seeded_lanes
    counting = _CountingRepo(repo)
    indexed = MemoryQueryRepository(counting, vector_catalog=VectorIndexCatalog())  # type: ignore[arg-type]
    scan = MemoryQueryRepository(repo)
    for _ in range(10):
        probe = _vec(rnd)
        got = indexed.search_episode_embeddings(probe, limit=10, namespace=namespace)
        want = scan.search_episode_embeddings(probe, limit=10, namespace=namespace)
        assert [r["uuid"] for r in got] == [r["uuid"] for r in want]
        assert got and all(r["uuid"] != "" for r in got)
    assert any("db.index.vector.queryNodes" in q for q in counting.queries)
    assert not any(q.lstrip().startswith("MATCH (n:Episodic)") for q in counting.queries)
    _assert_index_only(counting.index_calls()[:4])


@pytest.mark.parametrize("namespaces", [None, ["default"], [SMALL_GROUP]])
async def test_assertion_lane_matches_scan_and_uses_index(seeded_lanes, namespaces) -> None:
    from menhir.infrastructure.memory_queries import MemoryQueryRepository
    from menhir.infrastructure.vector_indexes import VectorIndexCatalog

    repo, rnd = seeded_lanes
    counting = _CountingRepo(repo)
    indexed = MemoryQueryRepository(counting, vector_catalog=VectorIndexCatalog())  # type: ignore[arg-type]
    scan = MemoryQueryRepository(repo)
    for _ in range(10):
        probe = _vec(rnd)
        got = indexed.search_assertion_embeddings(probe, limit=20, namespaces=namespaces)
        want = scan.search_assertion_embeddings(probe, limit=20, namespaces=namespaces)
        assert [r["assertion_id"] for r in got] == [r["assertion_id"] for r in want]
        assert got
    assert any("db.index.vector.queryNodes" in q for q in counting.queries)
    assert not any(q.lstrip().startswith("MATCH (a:TypedAssertion)") for q in counting.queries)
    _assert_index_only(counting.index_calls()[:4])
