from __future__ import annotations

import json

from typer.testing import CliRunner

from menhir.services import secret_scan
from menhir.services.secret_scan import scan_namespace

AWS = "AKIAIOSFODNN7EXAMPLE"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _rows() -> list[dict]:
    return [
        {"uuid": "a1", "labels": ["Entity"], "type": "SEMANTIC", "content": f"key {AWS}",
         "summary": None, "name": "n", "text": None, "diff": None},
        {"uuid": "a2", "labels": ["Episodic"], "type": "EPISODIC", "content": f"{AWS} and {GH}",
         "summary": None, "name": "e", "text": None, "diff": None},
        {"uuid": "a3", "labels": ["Entity"], "type": "SEMANTIC", "content": "plain prose",
         "summary": None, "name": "p", "text": None, "diff": None},
    ]


def _fake_execute(rows):
    calls = []

    def execute(query, params):
        calls.append((query, dict(params)))
        return [r for r in rows if r["uuid"] > params["after"]][: params["batch"]]

    execute.calls = calls  # type: ignore[attr-defined]
    return execute


def test_counts_by_type_and_kind_without_values() -> None:
    report = scan_namespace(_fake_execute(_rows()), "work")
    data = report.to_dict()
    assert data["nodes_scanned"] == 3 and data["nodes_with_hits"] == 2
    assert {"memory_type": "SEMANTIC", "kind": "AWS Access Key", "nodes": 1} in data["matches"]
    assert AWS not in json.dumps(data) and GH not in json.dumps(data)


def test_query_is_read_only() -> None:
    execute = _fake_execute(_rows())
    scan_namespace(execute, "work")
    for query, _ in execute.calls:
        upper = query.upper()
        assert not any(w in upper for w in ("CREATE", "MERGE", "SET ", "DELETE", "REMOVE"))


def test_pages_through_all_batches(monkeypatch) -> None:
    monkeypatch.setattr(secret_scan, "BATCH", 2)
    execute = _fake_execute(_rows())
    report = scan_namespace(execute, "work")
    assert report.nodes_scanned == 3 and len(execute.calls) == 2


def test_cli_prints_counts_only_and_exits_1_on_findings(monkeypatch) -> None:
    from menhir.cli import app

    class FakeNeo4j:
        def __init__(self, **_kw):
            self.execute = _fake_execute(_rows())

    monkeypatch.setattr("menhir.infrastructure.neo4j.Neo4jRepository", FakeNeo4j)
    monkeypatch.setattr("menhir.env_file.load_menhir_env", lambda *a, **k: None)
    result = CliRunner().invoke(app, ["secrets", "scan", "--namespace", "work"])
    assert result.exit_code == 1
    assert "with_hits=2" in result.output
    assert AWS not in result.output and GH not in result.output
    as_json = CliRunner().invoke(app, ["secrets", "scan", "--namespace", "work", "--json"])
    assert json.loads(as_json.output)["nodes_with_hits"] == 2
