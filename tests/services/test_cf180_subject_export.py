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
        # Keyed ONLY by session_id, which a namespace filter cannot resolve. This is the table
        # that exercises the omission branch; lifecycle_actions resolves via node_uuid and so
        # exercises narrowing instead.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS recall_receipts "
            "(id INTEGER PRIMARY KEY, session_id TEXT, reason TEXT)"
        )
        conn.executemany(
            "INSERT INTO recall_receipts (session_id, reason) VALUES (?, ?)",
            [("sess-1", "why this was recalled"), ("sess-2", "another reason")],
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


# ---------------------------------------------------------------------------
# Defects found reviewing the first implementation (bf1b79ee)
# ---------------------------------------------------------------------------


def test_the_export_never_creates_the_erasure_subjects_table(tmp_path):
    """READ-ONLY, enforced. The obvious helper would have made an export MUTATE the sidecar.

    `ErasureSubjectStore.has_live_erasure` calls `_ensure_ready`, which issues CREATE TABLE,
    CREATE INDEX and mkdirs the parent directory. The first implementation used it, so pointing an
    "export" at a telemetry database created schema in it. The suppression state is now read
    through a `mode=ro` URI instead.
    """
    db_path = tmp_path / "no_erasures.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE lifecycle_actions (id INTEGER PRIMARY KEY, node_uuid TEXT, notes TEXT)"
        )
        conn.execute("INSERT INTO lifecycle_actions (node_uuid, notes) VALUES ('node-1', 'hi')")

    export_subject_data(
        _FakeNeo4j([_node("node-1")]), output_dir=tmp_path / "bundle", telemetry_db_path=db_path
    )

    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "erasure_subjects" not in tables, "the export created schema in the operator's sidecar"


def test_the_sidecar_connection_refuses_writes(tmp_path, sidecar):
    """The read-only claim is enforced by SQLite, not by reviewing the statements."""
    from menhir.services.subject_export import _connect_readonly

    conn = _connect_readonly(sidecar)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE should_not_exist (x INTEGER)")
    finally:
        conn.close()


def test_a_namespace_level_erasure_withholds_its_nodes_from_a_whole_store_export(tmp_path, sidecar):
    """The first implementation checked only NODE_UUID subjects.

    `suppressed_node_uuids` matches NODE_UUID rows, so a namespace-level erasure suppressed
    nothing in a whole-store export: every node in the doomed namespace was exported while the
    manifest simultaneously listed that namespace as suppressed. The suppression set is now read
    whole and covers every subject type.
    """
    _record_live_erasure(sidecar, "NAMESPACE", "doomed")
    out = tmp_path / "bundle"

    manifest = export_subject_data(
        _FakeNeo4j(
            [_node("keep-1", namespace="default"), _node("doomed-1", namespace="doomed")],
            [],
        ),
        output_dir=out,
        telemetry_db_path=sidecar,
    )

    assert {r["uuid"] for r in _lines(out / "graph_nodes.jsonl")} == {"keep-1"}
    assert manifest["withheld_node_uuids"] == 1


def test_no_exported_edge_references_a_node_the_bundle_lacks(tmp_path, sidecar):
    """A bundle that names nodes it does not contain is not a standalone record.

    The first implementation filtered relationships on the START node only, so an edge leaving the
    exported set kept a dangling `end_uuid` -- and under a namespace filter carried its `fact` text
    about a node outside the scope.
    """
    out = tmp_path / "bundle"
    export_subject_data(
        _FakeNeo4j([_node("node-1")], [_rel("node-1", "node-elsewhere")]),
        output_dir=out,
        telemetry_db_path=sidecar,
    )

    exported = {r["uuid"] for r in _lines(out / "graph_nodes.jsonl")}
    for edge in _lines(out / "graph_relationships.jsonl"):
        assert edge["start_uuid"] in exported and edge["end_uuid"] in exported


def test_a_namespace_filter_narrows_a_table_it_can_resolve(tmp_path, sidecar):
    """`lifecycle_actions` is keyed by node_uuid, so the filter narrows it to exported nodes."""
    out = tmp_path / "bundle"
    manifest = export_subject_data(
        _FakeNeo4j([_node("node-9", namespace="yawn")]),
        output_dir=out,
        telemetry_db_path=sidecar,
        namespace="yawn",
    )

    rows = _lines(out / "sidecar_lifecycle_actions.jsonl")
    assert all(r["node_uuid"] == "node-9" for r in rows), (
        "a namespace-filtered bundle contains rows for nodes outside the namespace"
    )
    assert "lifecycle_actions" not in manifest["coverage"]["omitted_sidecar_tables"]


def test_a_namespace_filter_omits_a_table_it_cannot_narrow_and_names_it(tmp_path, sidecar):
    """Over-export into a scoped extract is the direction that LEAKS.

    `recall_receipts` is keyed only by session_id, which a namespace filter cannot resolve. The
    first implementation exported every row of such a table regardless of the filter, so a 'yawn'
    bundle carried other projects' telemetry. Omission is the safe side of that trade, and the
    manifest has to name what it dropped or the omission is just a different silent gap.

    NOTE this must be a table the filter genuinely cannot resolve. An earlier version of this test
    used lifecycle_actions, which resolves via node_uuid and returns zero rows anyway -- so it
    passed whether or not the omission branch existed, and a mutation removing that branch escaped.
    """
    out = tmp_path / "bundle"
    manifest = export_subject_data(
        _FakeNeo4j([_node("node-9", namespace="yawn")]),
        output_dir=out,
        telemetry_db_path=sidecar,
        namespace="yawn",
    )

    assert "recall_receipts" in manifest["coverage"]["omitted_sidecar_tables"]
    assert "recall_receipts" not in manifest["sidecar_rows"]
    assert not (out / "sidecar_recall_receipts.jsonl").exists()
    assert any("OMITTED" in note or "omitted" in note for note in manifest["coverage"]["notes"])


def test_a_whole_store_export_still_includes_that_table(tmp_path, sidecar):
    """The omission is a property of the FILTER, not of the table. Without this, the test above is
    satisfied by an export that simply never emits recall_receipts."""
    out = tmp_path / "bundle"
    manifest = export_subject_data(
        _FakeNeo4j([_node("node-1")]), output_dir=out, telemetry_db_path=sidecar
    )

    assert manifest["sidecar_rows"]["recall_receipts"] == 2
    assert not manifest["coverage"]["omitted_sidecar_tables"]


def test_a_failure_partway_through_leaves_no_bundle(tmp_path, sidecar, monkeypatch):
    """A directory of JSONL with no manifest still looks like a record."""
    import menhir.services.subject_export as se

    def _boom(*args, **kwargs):
        raise RuntimeError("sidecar exploded")

    monkeypatch.setattr(se, "_export_sidecar", _boom)
    out = tmp_path / "bundle"

    with pytest.raises(RuntimeError, match="sidecar exploded"):
        export_subject_data(
            _FakeNeo4j([_node("node-1")]), output_dir=out, telemetry_db_path=sidecar
        )

    assert not out.exists(), "a partial bundle survived a mid-export failure"
