"""CF-252 - the embedding sweeps exclude structural anchors BY TYPE, not by an endpoint property.

The sweeps used to be label-less and relied on `a.structure_role IS NULL AND b.structure_role IS
NULL` to keep structural anchors out. That is a property test, and 285 production code-file
entities carry no `structure_role`, so 284 `ANCHORED_TO` edges passed it -- 61% of the
operator-facing "semantic facts missing an embedding" count. Since the remedy that count triggers
is a backfill, the miscount was not cosmetic: it pointed the repair script at edges whose `fact` is
`'Memory linked to code file: <path>'`.

The tests below therefore do NOT assert the presence of a string. They assert the property that
matters -- an untagged anchor is excluded -- and they do it with the endpoint test deliberately
DEFEATED, which is the exact condition production is in.
"""

from __future__ import annotations

import re

import pytest

from menhir.infrastructure import embedding_dimensions as ed
from menhir.infrastructure.embedding_dimensions import (
    SEMANTIC_FACT_EDGE_TYPES,
    embedding_dimension_health,
    reset_embedding_dimension_cache,
    semantic_fact_edge_pattern,
)


class _RecordingNeo4j:
    """Captures every query and answers from a tiny in-memory edge table.

    The point of modelling rows rather than returning canned counts: a test that hands back a
    number cannot tell a typed query from an untyped one, which is how a label-less scan survived
    a green suite in the first place.
    """

    uri = "bolt://stub"
    database = "neo4j"

    def __init__(self, edges: list[dict[str, object]]) -> None:
        self.edges = edges
        self.queries: list[str] = []

    # -- query understanding ------------------------------------------------
    @staticmethod
    def _types_matched(query: str) -> set[str] | None:
        """Relationship types the query restricts to, or None if it is label-less."""
        m = re.search(r"-\[\s*r\s*(?::([A-Z_|]+))?\s*\]", query)
        if not m or not m.group(1):
            return None
        return set(m.group(1).split("|"))

    def _selected(self, query: str) -> list[dict[str, object]]:
        allowed = self._types_matched(query)
        rows = self.edges if allowed is None else [e for e in self.edges if e["type"] in allowed]
        if "a.structure_role IS NULL" in query:
            rows = [e for e in rows if e["src_role"] is None and e["dst_role"] is None]
        return rows

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        self.queries.append(query)
        if ":Entity" in query or ":Community" in query:
            return [{"c": 0}] if "count(n)" in query else []
        if "r.fact_embedding IS NULL" in query:
            rows = [e for e in self._selected(query) if e["fact"] is not None and e["embedding"] is None]
            return [{"c": len(rows)}]
        if "r.fact_embedding IS NOT NULL" in query:
            rows = [e for e in self._selected(query) if e["embedding"] is not None]
            dims: dict[int, int] = {}
            for row in rows:
                dims[len(row["embedding"])] = dims.get(len(row["embedding"]), 0) + 1
            return [{"dim": d, "count": c} for d, c in sorted(dims.items(), key=lambda kv: -kv[1])]
        return []


def _anchor(**over: object) -> dict[str, object]:
    """A production-shaped ANCHORED_TO edge whose TARGET is missing `structure_role`.

    This is CF-252's row. Both endpoints look non-structural, so the endpoint predicate admits it.
    """
    row: dict[str, object] = {
        "type": "ANCHORED_TO",
        "fact": "Memory linked to code file: src/menhir/services/scoring_service.py",
        "embedding": None,
        "src_role": None,
        "dst_role": None,  # <- the defect: an untagged code-file entity
    }
    row.update(over)
    return row


def _fact(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "type": "RELATES_TO",
        "fact": "ctharvey prefers conventional commits",
        "embedding": None,
        "src_role": None,
        "dst_role": None,
    }
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _no_cache():
    reset_embedding_dimension_cache()
    yield
    reset_embedding_dimension_cache()


# ---------------------------------------------------------------------------
# The property CF-252 is about
# ---------------------------------------------------------------------------

def test_untagged_anchors_are_not_counted_as_missing_embeddings():
    """The regression itself: 284 anchors, 184 real facts -> the count must be 184, not 468."""
    neo4j = _RecordingNeo4j([_anchor() for _ in range(284)] + [_fact() for _ in range(184)])
    health = embedding_dimension_health(neo4j)
    assert health["null_edge_count"] == 184, (
        "structural anchors are being counted as semantic facts missing an embedding; "
        "a backfill acting on this number would embed file paths"
    )


def test_the_endpoint_predicate_alone_would_have_admitted_them():
    """Proves the fixture reproduces the real defect rather than a strawman.

    If this fails, the anchor rows are being excluded by something other than the type filter and
    the test above proves nothing.
    """
    neo4j = _RecordingNeo4j([_anchor() for _ in range(284)] + [_fact() for _ in range(184)])
    label_less = (
        "MATCH (a)-[r]->(b) WHERE r.fact IS NOT NULL AND r.fact_embedding IS NULL "
        "AND a.structure_role IS NULL AND b.structure_role IS NULL RETURN count(r) AS c"
    )
    assert neo4j.execute(label_less)[0]["c"] == 468


def test_the_endpoint_test_is_kept_behind_the_type_test():
    """Typing must ADD a filter, not replace one.

    The row here is `RELATES_TO` -- so the type filter admits it -- but it lands on a properly
    tagged structural node. Only the endpoint predicate can exclude it, so this is the case that
    detects the endpoint test being dropped now that typing appears to make it redundant. An
    ANCHORED_TO row cannot detect that: the type filter would have excluded it anyway.
    """
    neo4j = _RecordingNeo4j([_fact(dst_role="file"), _fact()])
    assert embedding_dimension_health(neo4j)["null_edge_count"] == 1


def test_anchor_dimensions_do_not_reach_the_mixed_signal():
    """An anchor carrying a stray 768-vector must not read as 'the embedder was changed'.

    `mixed` blocks startup. A structural anchor is not evidence about the semantic embedder.
    """
    neo4j = _RecordingNeo4j([
        _fact(embedding=[0.0] * 1536),
        _anchor(embedding=[0.0] * 768),
    ])
    health = embedding_dimension_health(neo4j)
    assert health["mixed"] is False
    assert health["edge_dims"] == {1536: 1}


# ---------------------------------------------------------------------------
# The ruling is single-sourced and reaches every emitter
# ---------------------------------------------------------------------------

def test_editing_the_ruling_reaches_the_health_sweep(monkeypatch):
    """Swap the ruling; the emitted query must follow it.

    Asserting `pattern == "|".join(TYPES)` would be satisfied by a hardcoded string -- CF-247's
    mutation M7 escaped exactly that test. So change the tuple and observe the effect.
    """
    monkeypatch.setattr(ed, "SEMANTIC_FACT_EDGE_TYPES", ("ANCHORED_TO",))
    neo4j = _RecordingNeo4j([_anchor(), _fact()])
    # With the ruling inverted, the anchor is the "fact" and the real fact is excluded.
    assert embedding_dimension_health(neo4j)["null_edge_count"] == 1
    assert any("ANCHORED_TO" in q for q in neo4j.queries)
    assert not any("[r:RELATES_TO]" in q for q in neo4j.queries)


def test_both_edge_scans_are_typed():
    """Neither sweep may be label-less. Guards against one of the two drifting back."""
    neo4j = _RecordingNeo4j([_fact()])
    embedding_dimension_health(neo4j)
    edge_queries = [q for q in neo4j.queries if "fact_embedding" in q]
    assert len(edge_queries) == 2
    for query in edge_queries:
        assert _RecordingNeo4j._types_matched(query) == set(SEMANTIC_FACT_EDGE_TYPES), (
            f"label-less or wrongly typed edge scan: {query.strip()}"
        )


def test_repair_script_selects_through_the_same_emitter():
    """The backfill's selection is where a miscount becomes vector pollution.

    Read as source rather than executed: the script needs a live graph and an embedder. What must
    hold is that it goes through the shared emitter -- a second hand-written pattern is how the
    health count and the repair it triggers would disagree.
    """
    import pathlib

    src = pathlib.Path(ed.__file__).resolve().parents[3] / "scripts" / "repair_embedding_dimensions.py"
    text = src.read_text(encoding="utf-8")
    select = text[text.index("edge_rows = _query_rows"):]
    select = select[: select.index("manifest = {")]
    assert "semantic_fact_edge_pattern()" in select
    assert "MATCH (a)-[r]->(b)" not in select


def test_the_ruling_names_relates_to_only():
    """Pins the census this was decided on: 7,959/7,959 fact_embedding rows are RELATES_TO.

    Widening it is a decision about what enters the semantic vector space, not a refactor.
    """
    assert SEMANTIC_FACT_EDGE_TYPES == ("RELATES_TO",)
    assert semantic_fact_edge_pattern() == "RELATES_TO"
