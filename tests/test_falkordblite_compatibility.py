"""Offline safety/reporting checks; live compatibility evidence comes from the isolated probe."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("falkor_probe", ROOT / "scripts" / "falkordblite_compatibility.py")
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
DOCUMENT_SPEC = importlib.util.spec_from_file_location(
    "falkor_document_proof", ROOT / "scripts" / "falkordblite_projection_proof.py"
)
assert DOCUMENT_SPEC is not None and DOCUMENT_SPEC.loader is not None
document_proof = importlib.util.module_from_spec(DOCUMENT_SPEC)
DOCUMENT_SPEC.loader.exec_module(document_proof)


def test_check_preserves_failure_and_continues() -> None:
    def fail() -> None:
        raise RuntimeError("write survived rollback")

    failed = probe.check("rollback", fail)
    passed = probe.check("next", lambda: {"rows": 1})
    assert failed == {"name": "rollback", "status": "FAIL", "detail": "RuntimeError: write survived rollback"}
    assert passed["status"] == "PASS"
    assert probe.exit_code([failed, passed]) == 1
    assert probe.exit_code([]) == 1
    assert probe.exit_code([passed]) == 0


def test_native_windows_refuses_before_importing_or_starting_runtime(monkeypatch, capsys) -> None:
    monkeypatch.setattr(probe.platform, "system", lambda: "Windows")

    def forbidden() -> dict[str, str]:
        pytest.fail("unsupported platform must not inspect dependencies or start a database")

    monkeypatch.setattr(probe, "required_versions", forbidden)
    assert probe.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["decision"] == "BLOCKED"
    assert report["checks"][0]["name"] == "native_platform"


def test_wrong_dependency_version_refuses_before_database_start(monkeypatch, capsys) -> None:
    monkeypatch.setattr(probe.platform, "system", lambda: "Linux")
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda name: "wrong-version")
    assert probe.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["decision"] == "BLOCKED"
    assert report["checks"][0]["name"] == "probe_execution"
    assert "install investigation pins" in report["checks"][0]["detail"]


@pytest.mark.parametrize(
    "dependencies",
    [
        [],
        ["archolith-graphiti-core>=1"],
        [
            "archolith-graphiti-core==one",
            "archolith-graphiti-core==two",
        ],
    ],
)
def test_missing_or_ambiguous_fork_pin_fails_closed(monkeypatch, dependencies) -> None:
    monkeypatch.setattr(probe.tomllib, "loads", lambda text: {"project": {"dependencies": dependencies}})
    with pytest.raises(AssertionError, match="one exact Graphiti fork pin"):
        probe.required_versions()


def test_document_compiler_keeps_actual_writer_metadata_and_binds_the_claim() -> None:
    from menhir.infrastructure.structure_write_fence import IdentityClaim

    claim = IdentityClaim(project_id="pid", root_key="/workspace/project", generation=3, host="host")
    document = {
        "file_path": "/workspace/project/README.md",
        "content": "quoted ' content \\ here",
        "project": "project",
        "structure_path": "README.md",
        "structure_project_id": "pid",
        "session_id": "session",
        "user_id": "user",
        "document_type": "reference_article",
    }
    query, params = document_proof.document_publication_statement(claim, document)
    assert isinstance(query, str)
    assert params["proof_project_id"] == params["spid"] == "pid"
    assert params["proof_generation"] == 3
    assert params["proof_host"] == "host"
    assert params["proof_root_key"] == "/workspace/project"
    assert params["props"]["content"] == document["content"]
    assert params["props"]["source"] == "document-ingest"
    assert params["props"]["document_type"] == "reference_article"
    assert params["props"]["group_id"] == ""
    assert params["props"]["structure_role"] == "document"
    assert params["props"]["name"] == "README.md"
    with pytest.raises(ValueError, match="identity disagrees"):
        document_proof.document_publication_statement(claim, {**document, "structure_project_id": "foreign"})


def test_document_compiler_refuses_a_multi_statement_writer(monkeypatch) -> None:
    from menhir.infrastructure.structure_queries import StructureGraphWriter
    from menhir.infrastructure.structure_write_fence import IdentityClaim

    def multiple_statements(writer, **kwargs):
        writer.neo4j.execute("CREATE (:First)", {})
        writer.neo4j.execute("CREATE (:Second)", {})

    monkeypatch.setattr(StructureGraphWriter, "write_document", multiple_statements)
    claim = IdentityClaim(project_id="pid", root_key="/workspace/project", generation=3, host="host")
    with pytest.raises(ValueError, match="exactly one writer statement"):
        document_proof.document_publication_statement(claim, {"structure_project_id": "pid"})
