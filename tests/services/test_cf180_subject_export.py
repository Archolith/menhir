"""CF-180 - the subject-data export, tested on the properties that could do harm.

The export is a reader over both stores, so the interesting tests are not "does it write a file".
They are: does it refuse when it cannot prove safety, does it withhold what an erasure is
withholding, and does its manifest tell the truth about what it left out.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from menhir.services.subject_export import ExportRefused, export_subject_data

WRITE_KEYWORDS = ("CREATE", "MERGE", "SET ", "DELETE", "REMOVE", "DROP", "DETACH")


class _FakeNeo4j:
    """Records every statement so the read-only claim is checked, not asserted."""

    def __init__(self, nodes=None, relationships=None):
        self._nodes = nodes or []
        self._rels = relationships or []
        self.queries: list[str] = []

    def execute(self, query, params=None):
        self.queries.append(query)
        rows = self._nodes if "labels(n) AS labels" in query else self._rels
        namespace = (params or {}).get("namespace")
        if namespace is not None:
            rows = [r for r in rows if r.get("namespace", "default") == namespace]
        return [dict(r) for r in rows]


def _node(uuid, *, namespace="default", structural=False, **props):
    return {
        "uuid": uuid,
        "labels": ["Entity"],
        "namespace": namespace,
        "structural": structural,
        "properties": {"uuid": uuid, "content": f"content of {uuid}", **props},
    }


def _rel(start, end, rel_type="RELATES_TO", **props):
    return {
        "type": rel_type,
        "start_uuid": start,
        "end_uuid": end,
        "namespace": "default",
        "properties": {"fact": f"{start} relates to {end}", **props},
    }


@pytest.fixture
def sidecar(tmp_path) -> Path:
    """A telemetry DB with the erasure-subject table and one content table populated."""
    from menhir.infrastructure.erasure_subjects import ErasureSubjectStore

    db_path = tmp_path / "telemetry.db"
    ErasureSubjectStore(db_path)._ensure_ready()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS lifecycle_actions "
            "(id INTEGER PRIMARY KEY, node_uuid TEXT, notes TEXT)"
        )
        conn.executemany(
            "INSERT INTO lifecycle_actions (node_uuid, notes) VALUES (?, ?)",
            [("node-1", "a note about node-1"), (None, "a stranded note with no key")],
        )
    return db_path


def _record_live_erasure(db_path: Path, subject_type: str, subject_value: str) -> None:
    from menhir.infrastructure.erasure_subjects import ErasureSubjectStore

    ErasureSubjectStore(db_path).record_subjects(
        op_id="op-1", subjects=[(subject_type, subject_value)]
    )


def _manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# Refusal: the failure mode worse than no export
# ---------------------------------------------------------------------------


def test_it_refuses_when_the_suppression_state_cannot_be_read(tmp_path):
    """FAIL CLOSED. An export claims completeness; it must not claim it over an unknown exclusion.

    `suppressed_node_uuids` degrades by suppressing everything, which is right for a paged reader
    that can still return a smaller page. An export cannot degrade that way without lying, so it
    refuses instead.
    """
    unreadable = tmp_path / "not-a-database.db"
    unreadable.write_text("this is not sqlite", encoding="utf-8")

    with pytest.raises(ExportRefused, match="suppression state"):
        export_subject_data(
            _FakeNeo4j([_node("node-1")]),
            output_dir=tmp_path / "bundle",
            telemetry_db_path=unreadable,
        )


def test_a_refusal_leaves_no_partial_bundle(tmp_path):
    """A half-written bundle is worse than none: it looks like a record and is not one."""
    unreadable = tmp_path / "not-a-database.db"
    unreadable.write_text("this is not sqlite", encoding="utf-8")
    out = tmp_path / "bundle"

    with pytest.raises(ExportRefused):
        export_subject_data(
            _FakeNeo4j([_node("node-1")]), output_dir=out, telemetry_db_path=unreadable
        )

    assert not (out / "graph_nodes.jsonl").exists()
    assert not (out / "manifest.json").exists()


def test_it_refuses_a_namespace_under_a_live_erasure(tmp_path, sidecar):
    """Exporting a namespace mid-erasure serves exactly what the erasure is withholding."""
    _record_live_erasure(sidecar, "NAMESPACE", "doomed")

    with pytest.raises(ExportRefused, match="live, unpurged erasure"):
        export_subject_data(
            _FakeNeo4j([_node("node-1", namespace="doomed")]),
            output_dir=tmp_path / "bundle",
            telemetry_db_path=sidecar,
            namespace="doomed",
        )


# ---------------------------------------------------------------------------
# Withholding
# ---------------------------------------------------------------------------


def test_a_node_under_a_live_erasure_is_withheld_with_its_edges(tmp_path, sidecar):
    """The node goes, and so does every edge that would reveal it by uuid."""
    _record_live_erasure(sidecar, "NODE_UUID", "node-doomed")
    out = tmp_path / "bundle"

    manifest = export_subject_data(
        _FakeNeo4j(
            [_node("node-1"), _node("node-doomed")],
            [_rel("node-1", "node-doomed"), _rel("node-1", "node-1")],
        ),
        output_dir=out,
        telemetry_db_path=sidecar,
    )

    exported = {row["uuid"] for row in _lines(out / "graph_nodes.jsonl")}
    assert exported == {"node-1"}
    assert manifest["withheld_node_uuids"] == 1

    edges = _lines(out / "graph_relationships.jsonl")
    assert all("node-doomed" not in (e["start_uuid"], e["end_uuid"]) for e in edges)
    assert len(edges) == 1


def test_the_positive_control_exports_both_nodes_without_an_erasure(tmp_path, sidecar):
    """Without this, the withholding test could pass because nothing was ever exported."""
    out = tmp_path / "bundle"
    export_subject_data(
        _FakeNeo4j([_node("node-1"), _node("node-doomed")], [_rel("node-1", "node-doomed")]),
        output_dir=out,
        telemetry_db_path=sidecar,
    )
    assert {r["uuid"] for r in _lines(out / "graph_nodes.jsonl")} == {"node-1", "node-doomed"}
    assert len(_lines(out / "graph_relationships.jsonl")) == 1


# ---------------------------------------------------------------------------
# Honesty of the manifest
# ---------------------------------------------------------------------------


def test_the_manifest_reports_unreachable_sidecar_content(tmp_path, sidecar):
    """The stranded row (NULL key) must be COUNTED, not silently inherited.

    Owner ruling: ship with a stated gap. A gap nobody states is the thing CF-180 warns about.
    """
    out = tmp_path / "bundle"
    manifest = export_subject_data(
        _FakeNeo4j([_node("node-1")]), output_dir=out, telemetry_db_path=sidecar
    )

    assert manifest["coverage"]["unaddressable_total"] >= 1
    assert manifest["coverage"]["unaddressable_rows"]["lifecycle_actions.notes"] == 1


def test_a_namespace_filtered_bundle_says_it_is_not_a_complete_record(tmp_path, sidecar):
    """A project extract must never be mistaken for a subject record."""
    out = tmp_path / "bundle"
    manifest = export_subject_data(
        _FakeNeo4j([_node("node-1", namespace="yawn"), _node("node-2", namespace="archolith")]),
        output_dir=out,
        telemetry_db_path=sidecar,
        namespace="yawn",
    )

    assert manifest["coverage"]["namespace_filter"] == "yawn"
    assert any("not a complete record" in note for note in manifest["coverage"]["notes"])
    assert {r["uuid"] for r in _lines(out / "graph_nodes.jsonl")} == {"node-1"}


def test_structural_rows_are_included_and_counted_not_dropped(tmp_path, sidecar):
    """Dropping them would be an unstated omission; the manifest counts them so a reader can filter."""
    out = tmp_path / "bundle"
    manifest = export_subject_data(
        _FakeNeo4j([_node("mem-1"), _node("file-1", structural=True)]),
        output_dir=out,
        telemetry_db_path=sidecar,
    )

    assert manifest["graph_nodes"] == 2
    assert manifest["graph_nodes_structural"] == 1


def test_embeddings_are_excluded_and_the_manifest_says_so(tmp_path, sidecar):
    """A 1536-float vector per node would dominate the bundle and carries nothing readable."""
    out = tmp_path / "bundle"
    manifest = export_subject_data(
        _FakeNeo4j(
            [_node("node-1", name_embedding=[0.1] * 1536)],
            [_rel("node-1", "node-1", fact_embedding=[0.2] * 1536)],
        ),
        output_dir=out,
        telemetry_db_path=sidecar,
    )

    node = _lines(out / "graph_nodes.jsonl")[0]
    assert "name_embedding" not in node["properties"]
    assert node["properties"]["content"] == "content of node-1", "content must survive the filter"
    assert "fact_embedding" not in _lines(out / "graph_relationships.jsonl")[0]["properties"]
    assert "name_embedding" in manifest["coverage"]["excluded_properties"]


# ---------------------------------------------------------------------------
# Structural properties of the export itself
# ---------------------------------------------------------------------------


def test_the_export_issues_no_write_statement(tmp_path, sidecar):
    """Checked against the statements actually issued, not asserted in a docstring."""
    fake = _FakeNeo4j([_node("node-1")], [_rel("node-1", "node-1")])
    export_subject_data(fake, output_dir=tmp_path / "bundle", telemetry_db_path=sidecar)

    assert fake.queries, "no statements were issued at all; this proves nothing"
    for query in fake.queries:
        upper = query.upper()
        for keyword in WRITE_KEYWORDS:
            assert keyword not in upper, f"export issued a write: {query.strip()[:120]}"


def test_the_sidecar_export_follows_the_erasure_registry(tmp_path, sidecar):
    """The registry decides which tables hold content, so a newly classified column joins the
    export without anyone editing it.

    A second hand-maintained list is how the export and the erasure map would drift, and the
    registry already has a guarding test that fails on an unclassified TEXT column -- this
    inherits that ratchet instead of duplicating it.
    """
    from menhir.infrastructure.telemetry.erasure_inventory import CONTENT_COLUMNS

    out = tmp_path / "bundle"
    manifest = export_subject_data(
        _FakeNeo4j([_node("node-1")]), output_dir=out, telemetry_db_path=sidecar
    )

    assert "lifecycle_actions" in manifest["sidecar_rows"]
    assert manifest["sidecar_rows"]["lifecycle_actions"] == 2, "rows must be exported whole"
    registry_tables = {entry.table for entry in CONTENT_COLUMNS}
    assert set(manifest["sidecar_rows"]).issubset(registry_tables)


@pytest.mark.online
def test_the_export_runs_against_a_real_graph(test_neo4j_repo, tmp_path, sidecar):
    """EXECUTION, not simulation. The fake repository above never PARSES the Cypher.

    A stub driver accepts any string, so a malformed projection, a bad `labels(n)` call or an
    unbound parameter in the namespace clause is invisible to every test above -- the same hazard
    that hid a missing `$default_ns` binding in CF-48 until a parameterized reference test caught
    it. This runs the real statements against a real database.
    """
    from uuid import uuid4

    marker = str(uuid4())
    test_neo4j_repo.execute(
        """
        CREATE (a:Entity {uuid: $a, name: 'exported memory', content: $marker,
                          namespace: 'export-test', name_embedding: [0.1, 0.2, 0.3]})
        CREATE (b:Entity {uuid: $b, name: 'src', namespace: 'export-test',
                          structure_role: 'directory'})
        CREATE (a)-[:RELATES_TO {fact: 'a relates to b', fact_embedding: [0.4, 0.5]}]->(b)
        """,
        {"a": f"a-{marker}", "b": f"b-{marker}", "marker": marker},
    )

    out = tmp_path / "bundle"
    manifest = export_subject_data(
        test_neo4j_repo,
        output_dir=out,
        telemetry_db_path=sidecar,
        namespace="export-test",
    )

    nodes = _lines(out / "graph_nodes.jsonl")
    assert {n["uuid"] for n in nodes} == {f"a-{marker}", f"b-{marker}"}
    assert manifest["graph_nodes_structural"] == 1

    exported = next(n for n in nodes if n["uuid"] == f"a-{marker}")
    assert exported["properties"]["content"] == marker
    assert "name_embedding" not in exported["properties"]

    edges = _lines(out / "graph_relationships.jsonl")
    assert len(edges) == 1
    assert edges[0]["properties"]["fact"] == "a relates to b"
    assert "fact_embedding" not in edges[0]["properties"]
