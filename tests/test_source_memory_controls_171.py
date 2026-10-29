"""#171 source-memory section: behavioural controls against a REAL Neo4j (online).

Every test builds real `:Episodic` queue nodes through the production ingest write paths
(`EpisodeRepository.create_pending_episode` + `set_episode_content_embedding`) and then runs the
real `MemoryQueryRepository.search_episode_embeddings` / `MemoryGraphAdapter` Cypher. The only
direct Cypher is (a) building the *hidden* variants (FAILED, CANDIDATE, GONE, ...) by mutating a
node the production path created, and (b) the legacy `group_id`-only row, which no current write
path can produce (it is stripped of `namespace` after creation). Vectors are 8-d and
deterministic, so cosine order is known.

Run with:  pytest --run-online tests/test_source_memory_controls_171.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.domain.recall import RecallResult
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.context_builder import ContextBuilderService
from menhir.services.recall_pipeline import _build_source_memories

pytestmark = [pytest.mark.online]

DIM = 8
Q = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
MODEL = "embed-model-v1"


def _vec(*head: float) -> list[float]:
    return [*head, *([0.0] * (DIM - len(head)))]


def _make(
    graph: Any,
    uuid: str,
    namespace: str,
    *,
    content: str | None = None,
    vec: list[float] | None = None,
    model: str | None = MODEL,
    ref: datetime | None = None,
) -> str:
    """Create a queue episode the way ingest does, then stamp its embedding the way enrichment does."""
    repo = EpisodeRepository(graph)
    repo.create_pending_episode(
        episode_uuid=uuid,
        name=f"name-{uuid}",
        content=content if content is not None else f"deploy note {uuid}",
        session_id="sess-1",
        user_id="user-1",
        source="claude-code",
        source_confidence=0.9,
        namespace=namespace,
        reference_time=ref,
    )
    if model is not None:
        assert repo.set_episode_content_embedding(uuid, vec or Q, model) is True
    else:
        # No stamp: write only the vector (a pre-stamp legacy embedding).
        graph.execute(
            "MATCH (n:Episodic {uuid: $u}) SET n.content_embedding = $v",
            params={"u": uuid, "v": vec or Q},
        )
    return uuid


def _legacy_group_only(graph: Any, uuid: str) -> None:
    """Rewrite a created row into the legacy shape: no `namespace`, only `group_id = ''`."""
    graph.execute(
        "MATCH (n:Episodic {uuid: $u}) REMOVE n.namespace SET n.group_id = ''",
        params={"u": uuid},
    )


def _search(graph: Any, namespace: str | None, **kw: Any) -> list[dict[str, Any]]:
    return MemoryQueryRepository(graph).search_episode_embeddings(
        Q, limit=kw.pop("limit", 50), namespace=namespace, **kw
    )


def _uuids(rows: list[dict[str, Any]]) -> set[str]:
    return {str(r["uuid"]) for r in rows}


# ---------------------------------------------------------------------------
# A1 / A2: tenancy
# ---------------------------------------------------------------------------


def _seed_tenants(graph: Any) -> None:
    # Near-identical text and the SAME vector everywhere: only tenancy can separate them.
    _make(graph, "a-1", "a", content="the build uses cd 3")
    _make(graph, "b-1", "b", content="the build uses cd 3")
    _make(graph, "d-named", "default", content="the build uses cd 3")
    _make(graph, "d-empty", "", content="the build uses cd 3")
    _make(graph, "d-legacy", "", content="the build uses cd 3")
    _legacy_group_only(graph, "d-legacy")


@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        ("a", {"a-1"}),
        ("b", {"b-1"}),
        ("default", {"d-named", "d-empty", "d-legacy"}),
        ("", {"d-named", "d-empty", "d-legacy"}),
    ],
)
def test_a1_scoped_search_returns_only_that_silo(
    test_neo4j_repo: Any, namespace: str, expected: set[str]
) -> None:
    """Breaks if the tenancy predicate (or its default-silo spelling set) is dropped or widened."""
    _seed_tenants(test_neo4j_repo)
    assert _uuids(_search(test_neo4j_repo, namespace)) == expected


def test_a1_namespace_is_case_sensitive(test_neo4j_repo: Any) -> None:
    """namespace_spellings only special-cases ''/'default'; 'A' and 'a' are different silos."""
    _make(test_neo4j_repo, "lower", "a")
    _make(test_neo4j_repo, "upper", "A")
    assert _uuids(_search(test_neo4j_repo, "a")) == {"lower"}
    assert _uuids(_search(test_neo4j_repo, "A")) == {"upper"}


def test_a2_unscoped_search_sees_every_silo(test_neo4j_repo: Any) -> None:
    """namespace=None is the documented opt-in unscoped read; pin it so a change is visible."""
    _seed_tenants(test_neo4j_repo)
    assert _uuids(_search(test_neo4j_repo, None)) == {
        "a-1", "b-1", "d-named", "d-empty", "d-legacy",
    }


# ---------------------------------------------------------------------------
# A3: hidden rows
# ---------------------------------------------------------------------------

_HIDE: dict[str, str] = {
    "failed": "SET n.processing_state = 'FAILED'",
    "no_processing_state": "REMOVE n.processing_state",
    "candidate": "SET n.scope = 'CANDIDATE'",
    "gone": "SET n.freshness = 'GONE'",
    "structure_role": "SET n.structure_role = 'file'",
    "evidence_projection": "SET n.is_evidence_projection = true",
    "legacy_structure": (
        "SET n.source = 'project-scan', n.content = 'File: src/app.py is the entrypoint'"
    ),
    "non_queue_graphiti_node": "SET n.valid_at = datetime()",
    "no_embedding": "REMOVE n.content_embedding",
    "dimension_mismatch": "SET n.content_embedding = [1.0, 0.0, 0.0]",
    "non_recall_view": "SET n.is_view = true",
}


@pytest.mark.parametrize("variant", sorted(_HIDE))
def test_a3_hidden_row_is_never_returned(test_neo4j_repo: Any, variant: str) -> None:
    """Each variant is created identical to a visible control (same vector, namespace) and then
    made hidden by exactly one property. Breaks if the matching predicate is removed from
    search_episode_embeddings. The control proves the query itself works (no vacuous pass)."""
    graph = test_neo4j_repo
    _make(graph, "control", "ns")
    _make(graph, "hidden", "ns")
    graph.execute(f"MATCH (n:Episodic {{uuid: 'hidden'}}) {_HIDE[variant]}")
    assert _uuids(_search(graph, "ns")) == {"control"}
    assert _uuids(_search(graph, None)) == {"control"}


def test_a3_production_delete_memory_removes_episode(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    _make(graph, "keep", "ns")
    _make(graph, "doomed", "ns")
    assert _uuids(_search(graph, "ns")) == {"keep", "doomed"}
    assert MemoryGraphAdapter(graph).delete_memory("doomed") is True
    assert _uuids(_search(graph, "ns")) == {"keep"}


def test_a3_production_namespace_erasure_removes_only_that_silo(test_neo4j_repo: Any) -> None:
    """Uses the real adapter.delete_namespace saga (queue nodes match on `namespace`)."""
    graph = test_neo4j_repo
    _make(graph, "a-1", "a")
    _make(graph, "b-1", "b")
    _make(graph, "b-2", "b")
    assert _uuids(_search(graph, None)) == {"a-1", "b-1", "b-2"}
    assert MemoryGraphAdapter(graph).delete_namespace("b", namespace="b") >= 2
    assert _uuids(_search(graph, "b")) == set()
    assert _uuids(_search(graph, None)) == {"a-1"}


# ---------------------------------------------------------------------------
# A4: old vs new value, rendered
# ---------------------------------------------------------------------------


class _FakeGraphiti:
    def __init__(self, model: str | None = None) -> None:
        self.embedder_ref = SimpleNamespace(model=model) if model else None

    async def embed_query(self, _q: str) -> list[float]:
        return Q


async def _lane(graph: Any, namespace: str | None, model: str | None = None):
    service = SimpleNamespace(
        graphiti_client=_FakeGraphiti(model), graph_adapter=MemoryGraphAdapter(graph)
    )
    return await _build_source_memories(
        service,
        "where is the db",
        namespace,
        RetrievalTuningConfig(),
        10,
        include_session=True,
    )


async def _render(section: Any) -> str:
    result = RecallResult(
        query="q", preset="knowledge", results=[], candidates_evaluated=0,
        nodes_touched=0, source_memories=section,
    )

    class _Svc:
        async def recall(self, *a: Any, **k: Any) -> RecallResult:
            return result

    return (await ContextBuilderService(recall_service=_Svc()).build_context("q")).context


@pytest.mark.asyncio
async def test_a4_old_and_new_value_render_with_times_oldest_first(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    # Insert the NEW value first so oldest-first cannot be an insertion-order accident.
    _make(graph, "new", "ns", content="db host is pg-new", vec=_vec(1.0, 0.1),
          ref=datetime(2026, 5, 1, tzinfo=UTC))
    _make(graph, "old", "ns", content="db host is pg-old", vec=_vec(1.0, 0.2),
          ref=datetime(2026, 1, 1, tzinfo=UTC))
    section, note, _ms = await _lane(graph, "ns")
    assert note is None and section is not None
    assert [sm.uuid for sm in section] == ["old", "new"]
    block = await _render(section)
    lines = block.splitlines()
    head = lines.index("Source memories (raw episodes, oldest first):")
    # Exact rendered line format: "  - <reference_time> | <content>"
    assert lines[head + 1] == "  - 2026-01-01T00:00:00Z | db host is pg-old"
    assert lines[head + 2] == "  - 2026-05-01T00:00:00Z | db host is pg-new"


# ---------------------------------------------------------------------------
# End to end: namespace reaches the real query via _build_source_memories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_lane_scopes_namespace_and_model_through_real_adapter(
    test_neo4j_repo: Any,
) -> None:
    graph = test_neo4j_repo
    _make(graph, "a-ok", "a", content="alpha secret", ref=datetime(2026, 2, 1, tzinfo=UTC))
    _make(graph, "a-oldmodel", "a", content="alpha stale-model", model="other-model")
    _make(graph, "b-ok", "b", content="beta secret", ref=datetime(2026, 2, 1, tzinfo=UTC))

    section, note, _ms = await _lane(graph, "a", model=MODEL)
    assert note is None and section is not None
    assert [sm.uuid for sm in section] == ["a-ok"]
    block = await _render(section)
    assert "alpha secret" in block
    assert "beta secret" not in block and "alpha stale-model" not in block

    section_b, _n, _ms = await _lane(graph, "b", model=MODEL)
    assert [sm.uuid for sm in section_b or ()] == ["b-ok"]


# ---------------------------------------------------------------------------
# A5: embedding model, and no similarity floor
# ---------------------------------------------------------------------------


def test_a5_model_filter_excludes_other_model_and_unstamped(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    _make(graph, "same", "ns", model=MODEL)
    _make(graph, "other", "ns", model="different-model")  # same dimension, other space
    _make(graph, "unstamped", "ns", model=None)  # vector but no content_embedding_model
    # With a model: coalesce(stamp, '') = model, so the unstamped row is EXCLUDED.
    assert _uuids(_search(graph, "ns", model=MODEL)) == {"same"}
    # Without a model: no filter at all (all three comparable by dimension alone).
    assert _uuids(_search(graph, "ns")) == {"same", "other", "unstamped"}


def test_limit_has_no_similarity_floor_and_orders_by_cosine(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    # All three are (near-)orthogonal or opposite to the query Q = e0.
    _make(graph, "orth", "ns", vec=_vec(0.0, 1.0))
    _make(graph, "opposite", "ns", vec=_vec(-1.0, 0.0))
    _make(graph, "nearly-orth", "ns", vec=_vec(0.05, 1.0))
    rows = _search(graph, "ns")
    assert [r["uuid"] for r in rows] == ["nearly-orth", "orth", "opposite"]
    # Neo4j's vector.similarity.cosine is rescaled to [0, 1] ((1 + cos) / 2): orthogonal ~ 0.5,
    # opposite = 0.0. Nothing here is "similar", yet all three come back (no floor).
    assert all(float(r["cosine"]) < 0.6 for r in rows)
    assert float(rows[-1]["cosine"]) < 0.01
