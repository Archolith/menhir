"""Gate B structure publication against a disposable Neo4j instance."""

from __future__ import annotations

import os
import asyncio
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.project_identity_binding import binding_host, root_key_for
from menhir.infrastructure.project_scanner import FileEntry, ProjectScanResult
from menhir.infrastructure.structure_queries import StructureGraphWriter
from menhir.infrastructure.structure_write_fence import IdentityClaim, StaleStructureScan

pytestmark = [pytest.mark.online]


@pytest.fixture
def graph():
    uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688")
    assert "127.0.0.1" in uri or "localhost" in uri
    repo = Neo4jRepository(
        uri=uri, database="neo4j", user="neo4j",
        password=os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"),
    )
    name = f"gateb-structure-{uuid.uuid4().hex[:12]}"
    project_id = str(uuid.uuid4())
    root = f"C:/disposable/{name}"
    claim = IdentityClaim(
        project_id=project_id, root_key=root_key_for(root), generation=1,
        host=binding_host(),
    )
    repo.execute(
        """
        CREATE (p:ProjectIdentity {
          project_id: $id, state: 'bound', bound_host: $host,
          root_key: $root_key, claim_generation: 1,
          canonical_root_path: $root
        })
        """,
        {"id": project_id, "host": claim.host, "root_key": claim.root_key,
         "root": root},
    )
    adapter = MemoryGraphAdapter(repo)
    try:
        yield repo, adapter, name, root, claim
    finally:
        repo.execute("MATCH (n:Entity {structure_project: $name}) DETACH DELETE n",
                     {"name": name})
        repo.execute("MATCH (p:ProjectIdentity {project_id: $id}) DETACH DELETE p",
                     {"id": project_id})
        repo.close()


def _scan(name: str, root: str, claim: IdentityClaim, fingerprint: str,
          path: str, generation: int | None) -> ProjectScanResult:
    return ProjectScanResult(
        name=name, root_path=root, stack="python", description="fixture",
        files=[FileEntry(rel_path=path, file_mtime=1.0)],
        files_discovered=1, files_eligible=1, files_indexed=1,
        scan_fingerprint=fingerprint, project_id=claim.project_id,
        identity_generation=claim.generation, scan_generation=generation,
    )


def _state(repo: Neo4jRepository, name: str) -> list[dict]:
    return repo.execute(
        """
        MATCH (n:Entity {structure_project: $name})
        RETURN n.structure_path AS path, n.scan_fingerprint AS fingerprint
        ORDER BY path
        """, {"name": name},
    )


def test_late_scan_and_missing_token_cannot_replace_newer_publication(graph):
    repo, adapter, name, root, claim = graph
    old_token = adapter.begin_structure_scan(claim)
    new_token = adapter.begin_structure_scan(claim)
    assert new_token == old_token + 1

    adapter.write_project_structure(
        _scan(name, root, claim, "new", "src/new.py", new_token), "s", "u",
    )
    before = _state(repo, name)
    assert {row["path"] for row in before} == {".", "src/new.py"}

    with pytest.raises(StaleStructureScan):
        adapter.write_project_structure(
            _scan(name, root, claim, "old", "src/old.py", old_token), "s", "u",
        )
    with pytest.raises(StaleStructureScan):
        adapter.write_project_structure(
            _scan(name, root, claim, "missing", "src/old.py", None), "s", "u",
        )
    assert _state(repo, name) == before


def test_mid_publication_exception_rolls_back_entities_and_fingerprint(graph, monkeypatch):
    repo, adapter, name, root, claim = graph
    initial = adapter.begin_structure_scan(claim)
    adapter.write_project_structure(
        _scan(name, root, claim, "initial", "src/original.py", initial), "s", "u",
    )
    before = _state(repo, name)
    later = adapter.begin_structure_scan(claim)

    def fail_after_file_write(*args, **kwargs):
        raise RuntimeError("injected after file mutation")

    monkeypatch.setattr(StructureGraphWriter, "_write_symbols", fail_after_file_write)
    with pytest.raises(RuntimeError, match="injected"):
        adapter.write_project_structure(
            _scan(name, root, claim, "later", "src/changed.py", later), "s", "u",
        )
    assert _state(repo, name) == before


def test_unchanged_fingerprint_refresh_rejects_stale_scan(graph):
    repo, adapter, name, root, claim = graph
    initial = adapter.begin_structure_scan(claim)
    adapter.write_project_structure(
        _scan(name, root, claim, "same", "src/a.py", initial), "s", "u",
    )
    old_token = adapter.begin_structure_scan(claim)
    new_token = adapter.begin_structure_scan(claim)
    assert adapter.refresh_indexed_binding(
        name, "same", "new-commit", "origin", False,
        claim=claim, scan_generation=new_token,
    ) is True
    with pytest.raises(StaleStructureScan):
        adapter.refresh_indexed_binding(
            name, "same", "old-commit", "origin", False,
            claim=claim, scan_generation=old_token,
        )
    rows = repo.execute(
        "MATCH (p:Entity {structure_project: $name, structure_role: 'project'}) "
        "RETURN p.indexed_commit AS commit", {"name": name},
    )
    assert rows == [{"commit": "new-commit"}]


def test_partial_scan_keeps_unseen_legacy_symbols_without_file_mtimes(graph):
    repo, adapter, name, root, claim = graph
    initial = adapter.begin_structure_scan(claim)
    adapter.write_project_structure(
        _scan(name, root, claim, "initial", "src/old.py", initial), "s", "u",
    )
    repo.execute(
        """MATCH (f:Entity {structure_project: $name, structure_path: 'src/old.py'})
        REMOVE f.file_mtime
        CREATE (sym:Entity {structure_project: $name, structure_project_id: $id,
          structure_role: 'symbol', structure_path: 'src/old.py::old', name: 'old'})
        CREATE (f)-[:DEFINES]->(sym)""",
        {"name": name, "id": claim.project_id},
    )
    token = adapter.begin_structure_scan(claim)
    partial = _scan(name, root, claim, "partial", "src/new.py", token)
    partial.files_discovered = 2
    partial.files_eligible = 2
    partial.files_indexed = 1
    assert partial.partial_index is True
    adapter.write_project_structure(partial, "s", "u")

    rows = repo.execute(
        """MATCH (f:Entity {structure_project: $name, structure_path: 'src/old.py'})
        -[:DEFINES]->(sym:Entity {structure_role: 'symbol'})
        RETURN sym.structure_path AS path""",
        {"name": name},
    )
    assert rows == [{"path": "src/old.py::old"}]


def test_binding_refresh_cannot_update_a_same_name_other_identity(graph):
    repo, adapter, name, root, claim = graph
    repo.execute(
        """CREATE (:Entity {structure_project: $name, structure_role: 'project',
          structure_path: '.', structure_project_id: 'other-id',
          scan_fingerprint: 'same', indexed_commit: 'old-commit'})""",
        {"name": name},
    )
    assert adapter.get_scan_fingerprint(name, project_id=claim.project_id) is None
    token = adapter.begin_structure_scan(claim)
    assert adapter.refresh_indexed_binding(
        name, "same", "wrong-commit", "origin", False,
        claim=claim, scan_generation=token,
    ) is False
    assert repo.execute(
        """MATCH (p:Entity {structure_project: $name, structure_role: 'project'})
        RETURN p.indexed_commit AS commit""", {"name": name},
    ) == [{"commit": "old-commit"}]


def test_traversal_failure_after_prior_index_leaves_graph_unchanged(
    graph, tmp_path: Path, monkeypatch,
):
    from menhir.core import backend_runtime_data_ops as runtime_module
    from menhir.core import ingest_guard
    from menhir.domain import project_identity as identity_module
    from menhir.infrastructure import project_scanner as scanner_module
    from menhir.infrastructure import repo_topology
    from menhir.services import project_identity_service

    repo, adapter, name, root, claim = graph
    initial = adapter.begin_structure_scan(claim)
    adapter.write_project_structure(
        _scan(name, root, claim, "initial", "src/old.py", initial), "s", "u",
    )
    before_entities = repo.execute(
        """MATCH (n:Entity {structure_project: $name})
        RETURN n.structure_path AS path, properties(n) AS props ORDER BY path""",
        {"name": name},
    )
    before_edges = repo.execute(
        """MATCH (a:Entity {structure_project: $name})-[r]->(b:Entity)
        RETURN a.structure_path AS source, type(r) AS relation,
               b.structure_path AS target ORDER BY source, relation, target""",
        {"name": name},
    )
    source_root = tmp_path / name
    source_root.mkdir()
    (source_root / "src").mkdir()
    (source_root / "src" / "new.py").write_text("pass\n", encoding="utf-8")
    ops = runtime_module.RuntimeProviderDataOpsMixin()
    async def _off_loop(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    ops._off_loop = _off_loop
    ops.built = SimpleNamespace(graph_adapter=adapter)

    def denied_walk(_root, *, onerror):
        onerror(PermissionError("injected unreadable directory"))
        return []

    with monkeypatch.context() as patch:
        patch.setattr(scanner_module.os, "walk", denied_walk)
        patch.setattr(runtime_module, "get_request_tier", lambda: "agent")
        patch.setattr(ingest_guard, "ensure_ingest_path_allowed", lambda path, **kw: Path(path))
        patch.setattr(repo_topology, "classify_root", lambda path: None)
        patch.setattr(identity_module, "ensure_scan_root_owns_identity", lambda **kw: None)
        patch.setattr(
            project_identity_service, "settle_project_identity",
            lambda *args, **kwargs: (claim, None),
        )
        with pytest.raises(ValueError, match="Project scan refused:.*could not traverse"):
            asyncio.run(ops.scan_and_write_project(
                str(source_root), name=name, force=True, session_id="s", user_id="u",
            ))
    assert repo.execute(
        """MATCH (n:Entity {structure_project: $name})
        RETURN n.structure_path AS path, properties(n) AS props ORDER BY path""",
        {"name": name},
    ) == before_entities
    assert repo.execute(
        """MATCH (a:Entity {structure_project: $name})-[r]->(b:Entity)
        RETURN a.structure_path AS source, type(r) AS relation,
               b.structure_path AS target ORDER BY source, relation, target""",
        {"name": name},
    ) == before_edges


def test_same_project_token_issuance_waits_for_publication_transaction(graph, monkeypatch):
    repo, adapter, name, root, claim = graph
    token = adapter.begin_structure_scan(claim)
    entered = threading.Event()
    release = threading.Event()
    issued = threading.Event()
    errors: list[BaseException] = []
    tokens: list[int] = []
    original = StructureGraphWriter.write_project

    def paused_write(self, scan, session_id, user_id):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release publication")
        return original(self, scan, session_id, user_id)

    monkeypatch.setattr(StructureGraphWriter, "write_project", paused_write)

    def publish():
        try:
            adapter.write_project_structure(
                _scan(name, root, claim, "first", "src/a.py", token), "s", "u",
            )
        except BaseException as error:
            errors.append(error)

    def begin_next():
        try:
            tokens.append(adapter.begin_structure_scan(claim))
        except BaseException as error:
            errors.append(error)
        finally:
            issued.set()

    writer = threading.Thread(target=publish)
    issuer = threading.Thread(target=begin_next)
    writer.start()
    try:
        assert entered.wait(10)
        issuer.start()
        assert not issued.wait(0.25), "a new token passed a project transaction still writing"
    finally:
        release.set()
        writer.join(15)
        if issuer.ident is not None:
            issuer.join(15)
    assert not writer.is_alive() and not issuer.is_alive()
    assert not errors
    assert tokens == [token + 1]
    assert _state(repo, name)[0]["fingerprint"] == "first"
