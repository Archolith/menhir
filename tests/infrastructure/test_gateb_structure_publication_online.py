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
def graph(request, tmp_path):
    uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688")
    assert "127.0.0.1" in uri or "localhost" in uri
    if getattr(request, "param", "neo4j") == "falkor":
        pytest.importorskip("redislite.falkordb_client")
        from menhir.infrastructure.falkor import FalkorLiteRepository

        repo = FalkorLiteRepository(tmp_path / "project.rdb")
    else:
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


@pytest.mark.parametrize("graph", ["neo4j", "falkor"], indirect=True)
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


@pytest.mark.parametrize("graph", ["neo4j", "falkor"], indirect=True)
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


@pytest.mark.parametrize("graph", ["neo4j", "falkor"], indirect=True)
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


@pytest.mark.parametrize("graph", ["neo4j", "falkor"], indirect=True)
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


def test_document_publication_create_update_and_identity_fence(graph):
    from menhir.infrastructure.structure_write_fence import StaleIdentityClaim

    repo, adapter, name, root, claim = graph

    def publish(content, generation=claim.generation):
        adapter.write_document(
            root + "/guide.md",
            content,
            project=name,
            structure_path="docs/guide.md",
            project_id=claim.project_id,
            identity_generation=generation,
            identity_root=root,
            session_id="session",
            user_id="user",
            document_type="reference_article",
        )

    publish("first")
    before = repo.execute(
        "MATCH (n:Entity {structure_project:$name}) RETURN n.uuid AS uuid, n.created_at AS created_at",
        {"name": name},
    )[0]
    publish("updated")
    assert repo.execute(
        "MATCH (n:Entity {structure_project:$name}) RETURN n.uuid AS uuid, n.created_at AS created_at, "
        "n.content AS content, n.document_type AS document_type, n.structure_project_id AS project_id",
        {"name": name},
    ) == [
        {
            **before,
            "content": "updated",
            "document_type": "reference_article",
            "project_id": claim.project_id,
        }
    ]
    with pytest.raises(StaleIdentityClaim):
        publish("unauthorized", generation=claim.generation + 1)
    assert repo.execute(
        "MATCH (n:Entity {structure_project:$name}) RETURN n.content AS content",
        {"name": name},
    ) == [{"content": "updated"}]


@pytest.mark.parametrize("existing", [False, True])
def test_document_publication_exception_rolls_back_merge(graph, monkeypatch, existing):
    repo, adapter, name, root, claim = graph

    def publish(content):
        adapter.write_document(
            root + "/guide.md",
            content,
            project=name,
            structure_path="docs/guide.md",
            project_id=claim.project_id,
            identity_generation=claim.generation,
            identity_root=root,
            session_id="session",
            user_id="user",
        )

    if existing:
        publish("original")
    before = repo.execute(
        "MATCH (n:Entity {structure_project:$name}) RETURN properties(n) AS props",
        {"name": name},
    )
    original = StructureGraphWriter._merge_entity

    def fail_after_merge(self, **kwargs):
        original(self, **kwargs)
        raise RuntimeError("injected after document merge")

    monkeypatch.setattr(StructureGraphWriter, "_merge_entity", fail_after_merge)
    with pytest.raises(RuntimeError, match="injected after document merge"):
        publish("must-rollback")
    assert (
        repo.execute(
            "MATCH (n:Entity {structure_project:$name}) RETURN properties(n) AS props",
            {"name": name},
        )
        == before
    )


@pytest.mark.parametrize("graph", ["neo4j", "falkor"], indirect=True)
def test_full_project_publication_and_incremental_pruning(graph):
    from dataclasses import replace

    from menhir.infrastructure.project_scanner import (
        CallEdge,
        CrossProjectRef,
        DirEntry,
        EndpointEntry,
        ImportEdge,
        NestedRepo,
        SymbolEntry,
        TestEdge,
    )
    from menhir.infrastructure.structure_queries import _symbol_path

    repo, adapter, name, root, claim = graph
    alpha = SymbolEntry("src/a.py", "alpha", "function", 1, "alpha()", "alpha", "")
    beta = SymbolEntry("src/b.py", "beta", "function", 1, "beta()", "beta", "")
    external, child = name + "-external", name + "-child"
    scan = ProjectScanResult(
        name=name,
        root_path=root,
        stack="python",
        description="full fixture",
        project_id=claim.project_id,
        identity_generation=claim.generation,
        scan_generation=adapter.begin_structure_scan(claim),
        scan_fingerprint="base",
        directories=[DirEntry("src"), DirEntry("tests")],
        files=[
            FileEntry("src/a.py", file_mtime=1),
            FileEntry("src/b.py", file_mtime=1),
            FileEntry("tests/test_a.py", role="test", file_mtime=1),
            FileEntry("README.md", role="document", file_mtime=1),
            FileEntry("pyproject.toml", role="config", file_mtime=1),
            FileEntry("main.py", role="entrypoint", file_mtime=1),
        ],
        dependencies=["pytest"],
        imports=[ImportEdge("src/a.py", "src/b.py")],
        test_edges=[TestEdge("tests/test_a.py", "src/a.py")],
        endpoints=[EndpointEntry("GET /fixture", "src/a.py", "http")],
        cross_project_refs=[CrossProjectRef(external, "import", "fixture")],
        nested_repos=[NestedRepo("vendor/child", child)],
        symbols=[alpha, beta],
        call_edges=[CallEdge(_symbol_path(alpha), _symbol_path(beta))],
        files_discovered=6,
        files_eligible=6,
        files_indexed=6,
        indexed_commit="base-commit",
        indexed_repository="fixture/repository",
    )
    adapter.write_document(
        root + "/keep.md",
        "retained",
        project=name,
        structure_path="doc:keep",
        project_id=claim.project_id,
        identity_generation=claim.generation,
        identity_root=root,
        session_id="s",
        user_id="u",
    )
    try:
        assert adapter.write_project_structure(scan, "s", "u") == {
            "entities": 13,
            "edges": 17,
        }
        assert repo.execute(
            "MATCH (n:Entity {structure_project:$name, structure_path:'.'}) "
            "RETURN n.indexed_commit AS commit, n.scan_fingerprint AS fingerprint",
            {"name": name},
        ) == [{"commit": "base-commit", "fingerprint": "base"}]
        update = replace(
            scan,
            scan_generation=adapter.begin_structure_scan(claim),
            scan_fingerprint="updated",
            indexed_commit="new-commit",
            indexed_dirty=True,
            directories=[DirEntry("src")],
            files=[FileEntry("src/a.py", file_mtime=2)],
            symbols=[replace(alpha, name="new_alpha")],
            dependencies=[],
            imports=[],
            test_edges=[],
            endpoints=[],
            cross_project_refs=[],
            nested_repos=[],
            call_edges=[],
            files_discovered=1,
            files_eligible=1,
            files_indexed=1,
        )
        assert adapter.write_project_structure(update, "s", "u") == {
            "entities": 4,
            "edges": 3,
        }
        paths = repo.execute(
            "MATCH (n:Entity {structure_project:$name}) RETURN n.structure_path AS path ORDER BY path",
            {"name": name},
        )
        assert {r["path"] for r in paths} == {
            ".",
            "src",
            "src/a.py",
            "src/a.py::new_alpha",
            "doc:keep",
        }
    finally:
        repo.execute(
            "MATCH (n:Entity) WHERE n.structure_project IN $names DETACH DELETE n",
            {"names": [external, child]},
        )


@pytest.mark.parametrize("graph", ["falkor"], indirect=True)
@pytest.mark.parametrize("change", ["mtime", "identity"])
def test_native_project_revalidates_planning_inputs(graph, monkeypatch, change):
    from menhir.infrastructure.structure_write_fence import StaleStructureScan

    repo, adapter, name, root, claim = graph
    adapter.write_project_structure(
        _scan(
            name, root, claim, "initial", "a.py", adapter.begin_structure_scan(claim)
        ),
        "s",
        "u",
    )
    token = adapter.begin_structure_scan(claim)
    original = repo.execute

    def changed(query, params=None, **kwargs):
        if "WITH 0 AS stage_initial" in query:
            if change == "mtime":
                original(
                    "MATCH (n:Entity {structure_project:$name, structure_path:'a.py'}) SET n.file_mtime=99",
                    {"name": name},
                )
            else:
                original(
                    "MATCH (p:ProjectIdentity {project_id:$id}) SET p.claim_generation=2",
                    {"id": claim.project_id},
                )
        return original(query, params, **kwargs)

    monkeypatch.setattr(repo, "execute", changed)
    with pytest.raises(StaleStructureScan):
        adapter.write_project_structure(
            _scan(name, root, claim, "unauthorized", "b.py", token), "s", "u"
        )
    assert {r["path"] for r in _state(repo, name)} == {".", "a.py"}
    assert {r["fingerprint"] for r in _state(repo, name) if r["fingerprint"]} == {
        "initial"
    }


@pytest.mark.parametrize("graph", ["neo4j", "falkor"], indirect=True)
@pytest.mark.parametrize("count", [0, 2])
def test_project_completion_cardinality_failure_rolls_back(graph, monkeypatch, count):
    repo, adapter, name, root, claim = graph
    adapter.write_project_structure(
        _scan(
            name, root, claim, "initial", "a.py", adapter.begin_structure_scan(claim)
        ),
        "s",
        "u",
    )
    before = _state(repo, name)
    token = adapter.begin_structure_scan(claim)
    original = StructureGraphWriter._merge_entity

    def wrong_completion(self, **kwargs):
        original(self, **kwargs)
        if kwargs["structure_role"] == "project":
            if count == 0:
                self.neo4j.execute(
                    "MATCH (n:Entity {structure_project:$name, structure_role:'project'}) SET n.structure_project_id='wrong'",
                    {"name": name},
                )
            else:
                self.neo4j.execute(
                    "CREATE (:Entity {structure_project:$name, structure_role:'project', structure_project_id:$id, structure_path:'duplicate'})",
                    {"name": name, "id": claim.project_id},
                )

    monkeypatch.setattr(StructureGraphWriter, "_merge_entity", wrong_completion)
    from menhir.infrastructure.falkor import FalkorLiteRepository

    if isinstance(repo, FalkorLiteRepository):
        from redis.exceptions import ResponseError

        error, message = ResponseError, "(?i)step|increment"
    else:
        error, message = RuntimeError, "exactly one project node"
    with pytest.raises(error, match=message):
        adapter.write_project_structure(
            _scan(name, root, claim, "must-rollback", "b.py", token), "s", "u"
        )
    assert _state(repo, name) == before
