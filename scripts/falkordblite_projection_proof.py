"""Bounded #109 transaction experiments, called only by the isolated compatibility probe.

Nothing here is a runtime repository adapter. The autocommit facade deliberately demonstrates
the unsafe mapping; guarded publication candidates cover specific operations only.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

PROJECT_GUARD_JS = """
function requireOk(ok, message) {
    if (ok !== true) { throw new Error(message); }
    return true;
}
falkor.register('requireOk', requireOk);
"""
_STRUCTURE_TOKENS = re.compile(
    r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`|//[^\n]*|/\*.*?\*/"
    r"|\$(?P<parameter>[A-Za-z_][A-Za-z0-9_]*)|(?P<clock>\bdatetime\s*\(\s*\))",
    re.DOTALL,
)
_TERMINAL_COUNT = re.compile(r"\bRETURN\s+count\([^)]*\)\s+AS\s+(cnt|deleted|updated)\s*$", re.IGNORECASE)


def _bind_structure_query(query: str, parameters: dict[str, Any], name: str) -> str:
    """Bind this writer's parameter names without rewriting strings, identifiers or comments.

    Only its zero-argument edge clock is mapped to native localdatetime(). Qualification below
    assumes a UTC runtime; this is NOT an alias for timezone-bearing datetime(parameters).
    """

    def replace(match: re.Match[str]) -> str:
        parameter = match.group("parameter")
        if parameter is not None:
            if parameter not in parameters:
                raise ValueError(f"missing structure parameter {parameter}")
            return f"${name}.{parameter}"
        return "localdatetime()" if match.group("clock") else match.group(0)

    return _STRUCTURE_TOKENS.sub(replace, query)


def project_publication_statement(
    repository: Any,
    claim: Any,
    scan: Any,
    session_id: str,
    user_id: str,
) -> tuple[str, dict[str, Any]]:
    """Operation-specific compound proof using the actual full project writer.

    Capture its read-only mtime inputs, retain its Python scan policy, and compose its existing
    write statements. Only diagnostic counters are placeholders during planning; the actual
    completion count is asserted server-side and actual batch-edge counts are returned.
    Caller must hold the existing structure admission through preparation and publication.
    """
    from menhir.infrastructure.project_identity_binding import root_key_for
    from menhir.infrastructure.structure_queries import StructureGraphWriter

    if (
        scan.project_id != claim.project_id
        or scan.identity_generation != claim.generation
        or root_key_for(scan.root_path) != claim.root_key
    ):
        raise ValueError("scan identity disagrees with publication claim")
    if type(scan.scan_generation) is not int or scan.scan_generation < 1:
        raise ValueError("project proof requires a positive scan generation")
    reads: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    paths: set[str] = set()

    class ReadCapture:
        def execute(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            rows = repository.execute(query, params)
            for row in rows:
                if not row.get("path") or row["path"] in paths:
                    raise ValueError("ambiguous structural mtime identity")
                paths.add(row["path"])
            reads.append((query, dict(params), sorted(rows, key=lambda row: (row["path"], row["mtime"]))))
            return rows

    mtimes = StructureGraphWriter(ReadCapture()).get_file_mtimes(scan.name, scan.project_id)
    steps: list[tuple[str, dict[str, Any], str, bool]] = []

    class WriteCapture:
        edge_batch = False

        def execute(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            # Unexpected reads must not be answered with fabricated application state.
            plain = _STRUCTURE_TOKENS.sub("", query)
            if not re.search(r"\b(MERGE|CREATE|SET|DELETE|REMOVE)\b", plain):
                raise ValueError("unrecognized project read during write planning")
            match = _TERMINAL_COUNT.search(query.strip())
            alias = match.group(1).lower() if match else "proof_rows"
            steps.append((query, dict(params), alias, self.edge_batch))
            return [{"cnt": 0, "deleted": 0, "updated": 1}]

    capture = WriteCapture()

    class Planner(StructureGraphWriter):
        def get_file_mtimes(self, project_name: str, project_id: str | None = None) -> dict[str, float]:
            if project_name != scan.name or project_id != scan.project_id:
                raise ValueError("unrecognized project mtime read")
            return dict(mtimes)

        def _write_edges_batch(self, *args: Any, **kwargs: Any) -> int:
            capture.edge_batch = True
            try:
                return super()._write_edges_batch(*args, **kwargs)
            finally:
                capture.edge_batch = False

    counts = Planner(capture).write_project(scan, session_id, user_id)
    if not steps or steps[-1][2] != "updated" or any(step[2] == "updated" for step in steps[:-1]):
        raise ValueError("unrecognized project completion contract")
    parameters: dict[str, Any] = {
        "proof_id": claim.project_id,
        "proof_host": claim.host,
        "proof_root": claim.root_key,
        "proof_generation": claim.generation,
        "proof_scan": scan.scan_generation,
        "proof_entities": counts["entities"],
        "proof_fixed_edges": counts["edges"],
    }
    parts = [
        """
OPTIONAL MATCH (p:ProjectIdentity {project_id:$proof_id})
WITH collect(p) AS identities
WITH identities, Menhir109Guard.requireOk(
    size(identities)=1 AND coalesce(identities[0].state,'bound')='bound'
    AND coalesce(identities[0].bound_host=$proof_host,false)
    AND coalesce(identities[0].root_key=$proof_root,false)
    AND coalesce(identities[0].claim_generation,0)=$proof_generation
    AND coalesce(identities[0].structure_scan_generation=$proof_scan,false),
    'stale project publication') AS gate
"""
    ]
    for i, (query, params, rows) in enumerate(reads):
        name = f"read{i}"
        parameters[name], parameters[f"snapshot{i}"] = params, rows
        body = _bind_structure_query(query, params, name)
        parts.append(f"""
CALL {{ WITH gate
    CALL {{ WITH gate WITH gate WHERE gate {body} }}
    WITH path, mtime ORDER BY path, mtime
    RETURN collect({{path:path,mtime:mtime}}) AS actual{i}
}}
WITH identities, Menhir109Guard.requireOk(
    coalesce(actual{i}=$snapshot{i},false), 'changed project mtime input') AS gate
""")
    parts.append("""
WITH identities[0] AS identity, gate
SET identity.last_publication_probe=timestamp()
WITH CASE WHEN gate THEN 1 ELSE 0 END AS stage_initial
""")
    previous = "stage_initial"
    edge_counts = ["$proof_fixed_edges"]
    for i, (query, params, alias, edge_batch) in enumerate(steps):
        name, result = f"step{i}", f"stage{i}"
        parameters[name] = params
        body = _bind_structure_query(query, params, name)
        if alias == "proof_rows":
            body += " RETURN count(*) AS proof_rows"
        parts.append(f"""
CALL {{ WITH {previous}
    CALL {{ WITH {previous} WITH {previous} WHERE {previous}>=0 {body} }}
    RETURN sum({alias}) AS {result}
}}
""")
        if edge_batch:
            edge_counts.append(result)
        previous = result
    parts.append(
        f"RETURN Menhir109Guard.requireOk({previous}=1,'project completion marker mismatch') AS committed, "
        f"$proof_entities AS entities, toInteger({'+'.join(edge_counts)}) AS edges"
    )
    return "\n".join(parts), parameters


# Validate BEFORE any mutation; one graph query owns the whole publication decision and write.
# Timestamp values are supplied explicitly because this runtime has no Neo4j datetime() value.
PUBLISH_DEFINITION = """
OPTIONAL MATCH (d:ProjectionDefinitionState {definition_id:$definition_id})
WITH collect(d) AS states
WITH CASE
    WHEN size(states)>1 THEN 'corrupt'
    WHEN size(states)=0 THEN 'advance'
    WHEN coalesce(states[0].current_version,0)>$version THEN 'stale'
    WHEN coalesce(states[0].current_version,0)=$version
         AND coalesce(states[0].semantic_hash,'')<>$hash THEN 'conflict'
    WHEN coalesce(states[0].current_version,0)=$version THEN 'replay'
    ELSE 'advance' END AS action
FOREACH (ignored IN CASE WHEN action IN ['advance','replay'] THEN [1] ELSE [] END |
    MERGE (d:ProjectionDefinitionState {definition_id:$definition_id})
    ON CREATE SET d.created_at=$now
    SET d.current_version=$version, d.semantic_hash=$hash,
        d.coordination_epoch=coalesce(d.coordination_epoch,0)+1,
        d.published_at=CASE WHEN action='advance' THEN $now ELSE d.published_at END
)
WITH action
OPTIONAL MATCH (w:ProjectionWorkState {definition_id:$definition_id})
WITH action, collect(w) AS works
FOREACH (w IN CASE WHEN action='advance' THEN works ELSE [] END |
    SET w.definition_version=$version, w.generation=coalesce(w.generation,0)+1,
        w.reason='definition_published', w.dirty_at=$now,
        w.certified_definition_version=null, w.certified_generation=null,
        w.certified_projection_hash=null, w.certified_derivation_id=null,
        w.certified_receipt_key=null, w.certified_at=null
)
RETURN action, CASE WHEN action='advance' THEN size(works) ELSE 0 END AS invalidated
"""


class AutocommitProbeRepository:
    """Unsafe facade for exercising actual Menhir callbacks against real Falkor storage.

    Only datetime construction is translated. No query batching or pretend rollback is added.
    Do not wire this facade into Menhir runtime construction.
    """

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self.graph.query(
            query.replace("datetime()", "$probe_now"),
            {
                **(params or {}),
                "probe_now": "2026-11-04T00:00:00Z",
            },
        )
        columns = [column[1] for column in result.header]
        return [dict(zip(columns, row, strict=True)) for row in result.result_set]

    def execute_write(self, work: Callable[..., Any]) -> Any:
        return work(self)


def document_publication_statement(claim: Any, document: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Fuse identity validation with the ACTUAL writer's document MERGE, for this proof only.

    The caller must retain the existing admit/release protocol. This is neither an arbitrary
    callback translator nor a new document model; all payload/default/update rules stay owned
    by StructureGraphWriter. No temporal substitution is needed for this writer's string stamps.
    """
    from menhir.infrastructure.structure_queries import StructureGraphWriter

    if document.get("structure_project_id") != claim.project_id:
        raise ValueError("document identity disagrees with the publication claim")
    statements: list[tuple[str, dict[str, Any]]] = []

    class Capture:
        def execute(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            statements.append((query, dict(params)))
            return []

    StructureGraphWriter(Capture()).write_document(**document)
    if len(statements) != 1:
        raise ValueError("document proof requires exactly one writer statement")
    body, parameters = statements[0]
    query = (
        """
OPTIONAL MATCH (p:ProjectIdentity {project_id:$proof_project_id})
WITH p LIMIT 2
WITH collect(p) AS identities
OPTIONAL MATCH (existing:Entity {structure_project:$sp, structure_path:$spath})
WITH identities, existing LIMIT 2
WITH identities, collect(existing) AS documents
WITH identities, CASE
    WHEN size(identities)>1 OR size(documents)>1 THEN 'corrupt'
    WHEN size(identities)=0 THEN 'stale_identity'
    WHEN coalesce(identities[0].state,'bound')<>'bound'
      OR NOT coalesce(identities[0].bound_host=$proof_host,false)
      OR NOT coalesce(identities[0].root_key=$proof_root_key,false)
      OR coalesce(identities[0].claim_generation,0)<>$proof_generation THEN 'stale_identity'
    ELSE 'apply' END AS action
FOREACH (identity IN CASE WHEN action='apply' THEN identities ELSE [] END |
    SET identity.last_publication_probe=timestamp()
"""
        + body
        + """
)
RETURN action
"""
    )
    return query, {
        **parameters,
        "proof_project_id": claim.project_id,
        "proof_host": claim.host,
        "proof_root_key": claim.root_key,
        "proof_generation": int(claim.generation),
    }


def projection_proofs(db: Any) -> list[tuple[str, Callable[[], object]]]:
    """Return independently recorded live checks on separate disposable graph fixtures."""
    from redis.exceptions import ResponseError

    from menhir.domain.projection import ProjectionDefinition, ProjectionTarget
    from menhir.domain.projection_lifecycle import ProjectionLifecycleCorruptionError
    from menhir.infrastructure.projection_lifecycle_repository import (
        ProjectionLifecycleRepository,
    )
    from menhir.infrastructure.projection_lifecycle_repository_encoding import (
        _receipt_key,
    )

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def definition(version: int = 1) -> ProjectionDefinition:
        return ProjectionDefinition(
            definition_id="proof",
            version=version,
            input_assertion_types=frozenset({"proof"}),
            output_view_kind="proof",
            assertion_id_resolver=str,
            assertion_type_resolver=lambda _row: "proof",
            target_resolver=lambda _row: None,
            fold=lambda *_args: None,
        )

    def original_fixture(name: str) -> tuple[Any, Any, Any]:
        graph = db.select_graph(name)
        repository = ProjectionLifecycleRepository(AutocommitProbeRepository(graph))
        repository.publish_definition(definition(), semantic_hash="h1")
        (token,) = repository.dirty_targets(
            definition(),
            [ProjectionTarget(subject_id="subject", namespace="tenant-a")],
            reason="proof",
        )
        graph.query("CREATE (:ProjectionProofView {value:1})")
        return graph, repository, token

    def view_value(graph: Any) -> object:
        return graph.query("MATCH (n:ProjectionProofView) RETURN n.value").result_set

    def original_commit(mode: str) -> object:
        graph, repository, token = original_fixture("menhir109_callback_" + mode)
        if mode == "receipt_conflict":
            graph.query(
                "CREATE (:ProjectionDerivationReceipt {receipt_key:$key, derivation_id:'other'})",
                {"key": _receipt_key(token.work_key, token.generation)},
            )

        def materialize(tx: Any, _token: Any) -> str:
            tx.execute("MATCH (n:ProjectionProofView) SET n.value=2")
            return "" if mode == "blank_hash" else "h2"

        if mode == "accept":
            certificate = repository.commit(token, derivation_id="derive", materialize=materialize)
            require(certificate.projection_hash == "h2", "positive control did not certify")
            require(view_value(graph) == [[2]], "positive control did not install View")
            return "real Menhir commit callback installed and certified the fixture"

        try:
            repository.commit(token, derivation_id="derive", materialize=materialize)
        except ValueError as exc:
            require(mode == "blank_hash" and "materialized projection_hash" in str(exc), str(exc))
        except ProjectionLifecycleCorruptionError as exc:
            require(mode == "receipt_conflict" and "receipt/certificate" in str(exc), str(exc))
        else:
            raise AssertionError("negative control did not reject materialization")
        remaining = view_value(graph)
        require(remaining == [[1]], f"real Menhir {mode} refusal left View {remaining}, expected original [[1]]")
        return remaining

    def publish(graph: Any, version: int, semantic_hash: str) -> tuple[str, int]:
        rows = graph.query(
            PUBLISH_DEFINITION,
            {
                "definition_id": "proof",
                "version": version,
                "hash": semantic_hash,
                "now": "2026-11-04T00:00:00Z",
            },
        ).result_set
        require(len(rows) == 1, f"publication returned {rows}")
        return str(rows[0][0]), int(rows[0][1])

    def snapshot(graph: Any) -> list[str]:
        rows = graph.query("MATCH (n) RETURN labels(n), properties(n)").result_set
        return sorted(json.dumps(row, sort_keys=True) for row in rows)

    def seed(graph: Any) -> None:
        require(publish(graph, 1, "h1") == ("advance", 0), "fresh publication failed")
        graph.query(
            "CREATE (:ProjectionWorkState {definition_id:'proof', generation:1, namespace:'tenant-a', "
            "certified_generation:1, certified_projection_hash:'old'}) "
            "CREATE (:ProjectionWorkState {definition_id:'unrelated', generation:7, namespace:'tenant-b'})"
        )

    def guarded_publication() -> object:
        graph = db.select_graph("menhir109_guarded_publish")
        seed(graph)
        require(publish(graph, 2, "h2") == ("advance", 1), "advance did not invalidate exactly one target")
        require(publish(graph, 2, "h2") == ("replay", 0), "replay was not idempotent")
        before = snapshot(graph)
        require(publish(graph, 1, "h1") == ("stale", 0), "stale publisher not refused")
        require(snapshot(graph) == before, "stale refusal mutated graph")
        require(publish(graph, 2, "different") == ("conflict", 0), "hash reuse not refused")
        require(snapshot(graph) == before, "hash conflict mutated graph")
        rows = graph.query(
            "MATCH (w:ProjectionWorkState) RETURN w.definition_id, w.generation, "
            "w.certified_generation, w.certified_projection_hash ORDER BY w.definition_id"
        ).result_set
        require(rows == [["proof", 2, None, None], ["unrelated", 7, None, None]], f"wrong work state {rows}")
        return "advance, replay, stale/hash refusal, invalidation and unrelated target preservation passed"

    def corrupt_identity() -> object:
        graph = db.select_graph("menhir109_duplicate_definition")
        graph.query(
            "CREATE (:ProjectionDefinitionState {definition_id:'proof', current_version:1}), "
            "(:ProjectionDefinitionState {definition_id:'proof', current_version:2})"
        )
        before = snapshot(graph)
        require(publish(graph, 3, "h3") == ("corrupt", 0), "duplicate identity not refused")
        require(snapshot(graph) == before, "corruption refusal mutated graph")
        return "duplicate definition refused before writes"

    def query_failure_rollback() -> object:
        graph = db.select_graph("menhir109_guarded_query_failure")
        seed(graph)
        before = snapshot(graph)
        # Fail after the definition and work updates, inside the same graph query.
        failing = PUBLISH_DEFINITION.rsplit("RETURN action", 1)[0] + (
            "CREATE (bad:ProofInvalid) SET bad.value={nested:1} RETURN 1"
        )
        try:
            graph.query(
                failing,
                {
                    "definition_id": "proof",
                    "version": 2,
                    "hash": "h2",
                    "now": "failure-injection",
                },
            )
        except ResponseError as exc:
            require("Property values" in str(exc), f"unexpected failure injection: {exc}")
        else:
            raise AssertionError("invalid map property did not fail")
        require(snapshot(graph) == before, "failed atomic query retained definition/work updates")
        return "single-query failure restored the complete pre-publication graph snapshot"

    def concurrent_publication() -> object:
        outcomes = []
        try:
            for iteration in range(8):
                name = f"menhir109_concurrent_{iteration}"
                graph = db.select_graph(name)
                seed(graph)
                barrier = Barrier(2)

                def race(version: int, *, graph_name: str = name, start: Barrier = barrier) -> tuple[str, int]:
                    start.wait(timeout=10)
                    return publish(db.select_graph(graph_name), version, f"h{version}")

                with ThreadPoolExecutor(max_workers=2) as pool:
                    low = pool.submit(race, 2)
                    high = pool.submit(race, 3)
                    results = [low.result(timeout=20), high.result(timeout=20)]
                rows = graph.query(
                    "MATCH (d:ProjectionDefinitionState) RETURN d.current_version, d.semantic_hash"
                ).result_set
                require(rows == [[3, "h3"]], f"publication moved backwards or duplicated identity: {rows}")
                require(results[1] == ("advance", 1), f"higher version failed: {results}")
                expected = 3 if results[0][0] == "advance" else 2
                require(results[0][0] in ("advance", "stale"), f"unexpected lower result {results}")
                rows = graph.query(
                    "MATCH (w:ProjectionWorkState {definition_id:'proof'}) RETURN w.generation"
                ).result_set
                require(rows == [[expected]], f"lost/extra invalidation: {rows}; results={results}")
                outcomes.append(results)
            return {"pairs": len(outcomes), "outcomes": outcomes, "final_version": 3}
        finally:
            # Joined workers can leave multiple pooled sockets. Close them before the probe's
            # orderly-restart check so redislite does not mistake them for other live owners.
            db.client.connection_pool.disconnect()

    return [
        ("menhir_materialization_positive_control", lambda: original_commit("accept")),
        ("menhir_blank_hash_rollback", lambda: original_commit("blank_hash")),
        ("menhir_receipt_conflict_rollback", lambda: original_commit("receipt_conflict")),
        ("guarded_definition_publication", guarded_publication),
        ("guarded_definition_duplicate_refusal", corrupt_identity),
        ("guarded_definition_query_failure_rollback", query_failure_rollback),
        ("guarded_definition_concurrent_publishers", concurrent_publication),
    ]


def structure_document_proofs(db: Any) -> list[tuple[str, Callable[[], object]]]:
    """Real identity admission and document-writer payload, with one atomic publication query."""
    from redis.exceptions import ResponseError

    from menhir.infrastructure.structure_queries import StructureGraphWriter
    from menhir.infrastructure.structure_write_fence import (
        IdentityClaim,
        StructureWritesFrozen,
        admit_structure_writer,
        lock_structure_project,
        raise_fence,
        release_structure_writer,
    )

    claim = IdentityClaim(project_id="pid-1", root_key="/workspace/project", generation=3, host="proof-host")
    document = {
        "file_path": "/workspace/project/README.md",
        "content": "original",
        "project": "project",
        "structure_path": "README.md",
        "structure_project_id": claim.project_id,
        "session_id": "session-one",
        "user_id": "user-one",
        "document_type": "reference_article",
    }

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def fixture(name: str) -> tuple[Any, Any, Any]:
        graph = db.select_graph("menhir109_document_" + name)
        graph.query(
            "CREATE (:ProjectIdentity {project_id:'pid-1', state:'bound', bound_host:'proof-host', "
            "root_key:'/workspace/project', claim_generation:3})"
        )
        repository = AutocommitProbeRepository(graph)
        handle = admit_structure_writer(repository, label="document proof", claim=claim)
        return graph, repository, handle

    def state(graph: Any) -> list[str]:
        rows = graph.query("MATCH (n) RETURN id(n), labels(n), properties(n)").result_set
        rows += graph.query("MATCH (a)-[r]->(b) RETURN id(a), id(r), type(r), properties(r), id(b)").result_set
        return sorted(json.dumps(row, sort_keys=True) for row in rows)

    def entity(graph: Any) -> dict[str, Any]:
        rows = graph.query("MATCH (n:Entity) RETURN properties(n)").result_set
        require(len(rows) == 1, f"document identity is not unique: {rows}")
        return rows[0][0]

    def publish(graph: Any, payload: dict[str, Any]) -> str:
        query, params = document_publication_statement(claim, payload)
        rows = graph.query(query, params).result_set
        require(len(rows) == 1, f"publication returned {rows}")
        return rows[0][0]

    def roundtrip() -> object:
        graph, repository, handle = fixture("roundtrip")
        reference, original, reference_handle = fixture("reference")
        try:
            volatile = {"uuid", "created_at", "last_accessed"}
            for payload in (
                document,
                {**document, "content": "updated", "session_id": "session-two", "document_type": "wiki_article"},
            ):
                lock_structure_project(original, claim)
                StructureGraphWriter(original).write_document(**payload)
                require(publish(graph, payload) == "apply", "valid document refused")
                actual, expected = entity(graph), entity(reference)
                require(
                    {k: v for k, v in actual.items() if k not in volatile}
                    == {k: v for k, v in expected.items() if k not in volatile},
                    "document metadata/update rules drifted",
                )
                if payload is document:
                    created = actual["created_at"], actual["uuid"]
                else:
                    require(
                        (actual["created_at"], actual["uuid"]) == created, "update replaced document identity/history"
                    )
            require(entity(graph)["session_id"] == "session-one", "update changed create-only metadata")
            return "create/update match the actual writer's fields and preserve UUID, creation time and create-only metadata"
        finally:
            release_structure_writer(repository, handle)
            release_structure_writer(original, reference_handle)

    mutations = {
        "host": "MATCH (p:ProjectIdentity) SET p.bound_host='other-host'",
        "missing_host": "MATCH (p:ProjectIdentity) REMOVE p.bound_host",
        "root": "MATCH (p:ProjectIdentity) SET p.root_key='/other/project'",
        "generation": "MATCH (p:ProjectIdentity) SET p.claim_generation=4",
        "state": "MATCH (p:ProjectIdentity) SET p.state='retired'",
        "missing_identity": "MATCH (p:ProjectIdentity) DELETE p",
        "duplicate_identity": "MATCH (p:ProjectIdentity) WITH properties(p) AS props CREATE (q:ProjectIdentity) SET q=props",
        "duplicate_document": "MATCH (n:Entity) WITH properties(n) AS props CREATE (m:Entity) SET m=props SET m.uuid='duplicate'",
    }

    def refuse(name: str, mutation: str) -> object:
        graph, repository, handle = fixture("refuse_" + name)
        try:
            StructureGraphWriter(repository).write_document(**document)
            query, params = document_publication_statement(claim, {**document, "content": "must not publish"})
            graph.query(mutation)
            before = state(graph)
            action = graph.query(query, params).result_set
            expected = "corrupt" if name.startswith("duplicate") else "stale_identity"
            require(action == [[expected]], f"{name} accepted or returned wrong refusal: {action}")
            require(state(graph) == before, f"{name} refusal changed document or identity probe")
            return expected
        finally:
            release_structure_writer(repository, handle)

    def rollback(existing: bool) -> object:
        graph, repository, handle = fixture("rollback_" + str(existing))
        try:
            if existing:
                StructureGraphWriter(repository).write_document(**document)
            query, params = document_publication_statement(claim, {**document, "content": "must roll back"})
            before = state(graph)
            failing = query.rsplit("RETURN action", 1)[0] + "CREATE (bad:DocumentBad) SET bad.value={nested:1} RETURN 1"
            try:
                graph.query(failing, params)
            except ResponseError as exc:
                require("Property values" in str(exc), f"unexpected injected failure: {exc}")
            else:
                raise AssertionError("document injection did not fail")
            require(state(graph) == before, "failure retained document or project-probe writes")
            return "document and project probe restored after post-write failure"
        finally:
            release_structure_writer(repository, handle)

    def migration_drain() -> object:
        graph, repository, handle = fixture("migration")
        try:
            raise_fence(repository, reason="proof migration")
            try:
                admit_structure_writer(repository, label="late writer", claim=claim)
            except StructureWritesFrozen:
                pass
            else:
                raise AssertionError("migration admitted a new writer")
            require(publish(graph, document) == "apply", "migration stranded an already-admitted writer")
        finally:
            release_structure_writer(repository, handle)
        rows = graph.query(
            "MATCH (p:ProjectIdentity), (f:StructureWriteFence) RETURN p.active_writers, f.writers"
        ).result_set
        require(rows == [[[], []]], f"admission/release leaked a writer: {rows}")
        return "freeze refuses new admissions; admitted writer finishes and releases both registrations"

    def competing_writers() -> object:
        graph, repository, handle = fixture("race")
        other = admit_structure_writer(repository, label="second writer", claim=claim)
        plans = [document_publication_statement(claim, {**document, "content": f"writer-{i}"}) for i in range(2)]
        barrier = Barrier(2)

        def run(plan: tuple[str, dict[str, Any]]) -> object:
            barrier.wait(timeout=10)
            return db.select_graph("menhir109_document_race").query(*plan).result_set

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, plan) for plan in plans]
                results = [future.result(timeout=20) for future in futures]
            require(results == [[["apply"]], [["apply"]]], str(results))
            current = entity(graph)
            require(current["content"] in ("writer-0", "writer-1"), "lost document payload")
            require(current["uuid"] in {params["uuid"] for _, params in plans}, "invented document identity")
            return "both valid writers serialize onto one document, preserving the existing last-writer behavior"
        finally:
            release_structure_writer(repository, handle)
            release_structure_writer(repository, other)
            db.client.connection_pool.disconnect()

    checks = [
        ("document_publication_actual_writer_roundtrip", roundtrip),
        ("document_publication_create_rollback", lambda: rollback(False)),
        ("document_publication_update_rollback", lambda: rollback(True)),
        ("document_publication_migration_drain", migration_drain),
        ("document_publication_competing_writers", competing_writers),
    ]
    checks.extend(
        (f"document_publication_refuses_{name}", lambda name=name, query=query: refuse(name, query))
        for name, query in mutations.items()
    )
    return checks


def project_publication_proofs(db: Any) -> list[tuple[str, Callable[[], object]]]:
    """Full-writer compound experiments, native constraints and explicit late rollback guards."""
    from dataclasses import replace
    from datetime import datetime

    from redis.exceptions import ResponseError

    from menhir.infrastructure.project_identity_binding import root_key_for
    from menhir.infrastructure.project_scanner import (
        CallEdge,
        CrossProjectRef,
        DirEntry,
        EndpointEntry,
        FileEntry,
        ImportEdge,
        NestedRepo,
        ProjectScanResult,
        SymbolEntry,
        TestEdge,
    )
    from menhir.infrastructure.structure_queries import (
        StructureGraphWriter,
        _symbol_path,
    )
    from menhir.infrastructure.structure_write_fence import (
        IdentityClaim,
        admit_structure_writer,
        lock_structure_project,
        release_structure_writer,
    )

    db.client.execute_command("GRAPH.UDF", "LOAD", "Menhir109Guard", PROJECT_GUARD_JS)
    claim = IdentityClaim(
        project_id="project-pid", root_key=root_key_for("/workspace/project"), generation=3, host="host"
    )
    alpha = SymbolEntry("src/a.py", "alpha", "function", 1, "alpha()", "alpha", "")
    beta = SymbolEntry("src/b.py", "beta", "function", 1, "beta()", "beta", "")
    base = ProjectScanResult(
        name="project",
        root_path="/workspace/project",
        stack="python",
        description="full fixture",
        project_id=claim.project_id,
        identity_generation=3,
        scan_generation=1,
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
        cross_project_refs=[CrossProjectRef("external", "import", "fixture")],
        nested_repos=[NestedRepo("vendor/child", "child")],
        symbols=[alpha, beta],
        call_edges=[CallEdge(_symbol_path(alpha), _symbol_path(beta))],
        files_discovered=6,
        files_eligible=6,
        files_indexed=6,
        indexed_commit="base-commit",
        indexed_repository="fixture/repository",
        indexed_dirty=False,
    )
    update = replace(
        base,
        scan_fingerprint="updated",
        indexed_commit="new-commit",
        indexed_dirty=True,
        directories=[DirEntry("src")],
        files=[FileEntry("src/a.py", file_mtime=2)],
        symbols=[replace(alpha, name="replacement")],
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

    def require(ok: bool, message: str) -> None:
        if not ok:
            raise AssertionError(message)

    class NativeRepository:
        def __init__(self, graph: Any):
            self.graph = graph

        def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            params = params or {}
            result = self.graph.query(_bind_structure_query(query, params, "native"), {"native": params})
            return [dict(zip([column[1] for column in result.header], row, strict=True)) for row in result.result_set]

    def fixture(name: str) -> tuple[Any, Any, Any]:
        graph = db.select_graph("menhir109_project_" + name)
        for label, properties in (
            ("Entity", ("structure_project_id", "structure_path")),
            ("ProjectIdentity", ("project_id",)),
        ):
            graph.query(f"CREATE INDEX FOR (n:{label}) ON ({','.join('n.' + p for p in properties)})")
            db.client.execute_command(
                "GRAPH.CONSTRAINT",
                "CREATE",
                graph.name,
                "UNIQUE",
                "NODE",
                label,
                "PROPERTIES",
                len(properties),
                *properties,
            )
        repository = NativeRepository(graph)
        deadline = time.monotonic() + 3
        while True:
            constraints = repository.execute("CALL db.constraints()")
            if len(constraints) == 2 and all(row["status"] == "OPERATIONAL" for row in constraints):
                break
            require(time.monotonic() < deadline, f"constraints did not become operational: {constraints}")
            time.sleep(0.02)
        graph.query(
            "CREATE (:ProjectIdentity {project_id:$pid,state:'bound',root_key:$root,bound_host:'host', "
            "claim_generation:3,structure_scan_generation:1}) "
            "CREATE (:Entity {uuid:'foreign',structure_project:'foreign',structure_project_id:'foreign-pid', "
            "structure_path:'src/a.py',structure_role:'file',file_mtime:1}) "
            "CREATE (:Entity {uuid:'manual',structure_project:'project',structure_project_id:$pid, "
            "structure_path:'doc:manual',structure_role:'document',source:'document-ingest'})",
            {"pid": claim.project_id, "root": claim.root_key},
        )
        handle = admit_structure_writer(repository, label="project proof", claim=claim)
        return graph, repository, handle

    def state(graph: Any) -> list[str]:
        rows = graph.query("MATCH (n) RETURN id(n),labels(n),properties(n)").result_set
        rows += graph.query("MATCH (a)-[r]->(b) RETURN id(a),id(r),type(r),properties(r),id(b)").result_set
        return sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)

    def semantic_state(graph: Any) -> list[str]:
        rows = graph.query("MATCH (n:Entity) RETURN properties(n)").result_set
        edges = graph.query(
            "MATCH (a)-[r]->(b) RETURN a.structure_project,a.structure_path,type(r),"
            "b.structure_project,b.structure_path,properties(r)"
        ).result_set
        volatile = {"uuid", "created_at", "last_accessed"}
        values = [[{k: v for k, v in row[0].items() if k not in volatile}] for row in rows]
        values += [row[:-1] + [{k: v for k, v in row[-1].items() if k not in volatile}] for row in edges]
        return sorted(json.dumps(row, sort_keys=True, default=str) for row in values)

    def publish(repository: Any, scan: Any) -> list[Any]:
        query, params = project_publication_statement(repository, claim, scan, "session", "user")
        return repository.graph.query(query, params).result_set[0]

    def expect_rollback(graph: Any, query: str, params: dict[str, Any], message: str | tuple[str, ...]) -> str:
        before = state(graph)
        try:
            graph.query(query, params)
        except ResponseError as exc:
            expected = (message,) if isinstance(message, str) else message
            require(any(value in str(exc) for value in expected), f"wrong publication error: {exc}")
        else:
            raise AssertionError("project publication did not reject")
        require(state(graph) == before, "project rejection retained writes or changed the identity probe")
        return message if isinstance(message, str) else " / ".join(message)

    def roundtrip(partial: bool) -> object:
        graph, repo, handle = fixture("partial" if partial else "complete")
        reference, original, reference_handle = fixture("partial_reference" if partial else "complete_reference")
        try:
            second = replace(update, files_discovered=6, files_eligible=6) if partial else update
            for scan in (base, second):
                lock_structure_project(original, claim, scan_generation=1)
                expected = StructureGraphWriter(original).write_project(scan, "session", "user")
                actual = publish(repo, scan)
                require(
                    actual == [True, expected["entities"], expected["edges"]],
                    f"result count mismatch: {actual}, {expected}",
                )
                require(
                    semantic_state(graph) == semantic_state(reference),
                    "compound publication drifted from actual writer",
                )
            paths = graph.query(
                "MATCH (n:Entity {structure_project_id:$pid}) RETURN n.structure_path", {"pid": claim.project_id}
            ).result_set
            require((["src/b.py"] in paths) == partial, "partial/full scan prune policy changed")
            require(["doc:manual"] in paths, "scan pruned a separately ingested document")
            return "actual writer parity for initial/incremental publication, entities/edges, counts, pruning and completion metadata"
        finally:
            release_structure_writer(repo, handle)
            release_structure_writer(original, reference_handle)

    def refusal(kind: str) -> object:
        graph, repo, handle = fixture(kind)
        try:
            publish(repo, base)
            query, params = project_publication_statement(repo, claim, update, "session", "user")
            if kind == "mtime":
                graph.query(
                    "MATCH (n:Entity {structure_project_id:$pid,structure_path:'src/a.py'}) SET n.file_mtime=99",
                    {"pid": claim.project_id},
                )
                message = "changed project mtime input"
            elif kind == "generation":
                graph.query("MATCH (p:ProjectIdentity) SET p.structure_scan_generation=2")
                message = "stale project publication"
            elif kind == "completion":
                steps = [key for key in params if key.startswith("step")]
                params[max(steps, key=lambda key: int(key[4:]))]["project_id"] = "wrong-project"
                message = "project completion marker mismatch"
            elif kind == "completion_multiple":
                graph.query(
                    "CREATE (:Entity {uuid:'extra-project',structure_project:'project',structure_project_id:$pid,"
                    "structure_path:'invalid-project-marker',structure_role:'project'})",
                    {"pid": claim.project_id},
                )
                message = "project completion marker mismatch"
            else:
                step = next(value for key, value in params.items() if key.startswith("step") and value.get("rows"))
                step["rows"][0]["name"] = {"invalid": "property"}
                # This engine can replace a nested execution error with the later UDF rejection.
                # Either error must reject and roll back the entire graph, never report success.
                message = ("Property values", "project completion marker mismatch")
            return expect_rollback(graph, query, params, message)
        finally:
            release_structure_writer(repo, handle)

    def udf_rollback() -> object:
        graph = db.select_graph("menhir109_project_udf")
        return expect_rollback(
            graph,
            "CREATE (:RollbackSentinel) WITH count(*) AS changed "
            "RETURN Menhir109Guard.requireOk(changed=0,'postcondition rollback')",
            {},
            "postcondition rollback",
        )

    def clock_profile() -> object:
        graph = db.select_graph("menhir109_project_clock")
        now, epoch = graph.query("RETURN localdatetime(),timestamp()").result_set[0]
        require(
            isinstance(now, datetime) and abs(now.timestamp() * 1000 - epoch) < 2000,
            "native structural clock is not compatible with the UTC fixture runtime",
        )
        return "native localdatetime edge clock in UTC runtime only; timezone-bearing datetime(parameters) remains unqualified"

    def late_failure() -> object:
        graph, repo, handle = fixture("late_failure")
        try:
            publish(repo, base)
            query, params = project_publication_statement(repo, claim, update, "session", "user")
            prefix, result = query.rsplit("RETURN Menhir109Guard", 1)
            failing = prefix + "CREATE (bad:ProjectFailure) SET bad.value={nested:1}\nRETURN Menhir109Guard" + result
            return expect_rollback(graph, failing, params, ("Property values", "project completion marker mismatch"))
        finally:
            release_structure_writer(repo, handle)

    def ambiguous_input() -> object:
        graph, repo, handle = fixture("ambiguous")
        try:
            publish(repo, base)
            graph.query(
                "CREATE (:Entity {structure_project:'project',structure_path:'src/a.py',structure_role:'file',file_mtime:1})"
            )
            before = state(graph)
            try:
                project_publication_statement(repo, claim, update, "session", "user")
            except ValueError as exc:
                require("ambiguous structural mtime" in str(exc), str(exc))
            else:
                raise AssertionError("ambiguous legacy/current mtime identity was accepted")
            require(state(graph) == before, "ambiguous preparation mutated graph")
            return "ambiguous legacy/current input refused before planning writes"
        finally:
            release_structure_writer(repo, handle)

    def competing_generations() -> object:
        graph, repo, handle = fixture("race")
        other = admit_structure_writer(repo, label="second scan", claim=claim)
        graph.query("MATCH (p:ProjectIdentity) SET p.structure_scan_generation=2")
        scans = (base, replace(update, scan_generation=2))
        plans = [project_publication_statement(repo, claim, scan, "session", "user") for scan in scans]
        barrier = Barrier(2)

        def run(plan: tuple[str, dict[str, Any]]) -> object:
            barrier.wait(timeout=10)
            try:
                return db.select_graph("menhir109_project_race").query(*plan).result_set
            except ResponseError as exc:
                require("stale project publication" in str(exc), f"unexpected race failure: {exc}")
                return "stale"

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, plan) for plan in plans]
                results = [future.result(timeout=20) for future in futures]
            require(results[0] == "stale" and results[1][0][0] is True, str(results))
            marker = graph.query(
                "MATCH (n:Entity {structure_project_id:$pid,structure_role:'project'}) RETURN n.scan_fingerprint",
                {"pid": claim.project_id},
            ).result_set
            require(marker == [["updated"]], f"old scan replaced new completion marker: {marker}")
            return "stale and current scans race; only the current generation publishes"
        finally:
            release_structure_writer(repo, handle)
            release_structure_writer(repo, other)
            db.client.connection_pool.disconnect()

    def missing_guard() -> object:
        graph, repo, handle = fixture("missing_guard")
        try:
            query, params = project_publication_statement(repo, claim, base, "session", "user")
            return expect_rollback(graph, query.replace("Menhir109Guard.", "MissingGuard."), params, "Unknown function")
        finally:
            release_structure_writer(repo, handle)

    return [
        ("project_publication_udf_postcondition_rollback", udf_rollback),
        ("project_publication_native_clock_profile", clock_profile),
        ("project_publication_full_writer_roundtrip", lambda: roundtrip(False)),
        ("project_publication_partial_scan_policy", lambda: roundtrip(True)),
        ("project_publication_late_failure_rollback", late_failure),
        ("project_publication_ambiguous_input", ambiguous_input),
        ("project_publication_competing_generations", competing_generations),
        ("project_publication_missing_guard", missing_guard),
        *[
            (f"project_publication_refuses_{kind}", lambda kind=kind: refusal(kind))
            for kind in ("mtime", "generation", "completion", "completion_multiple", "property")
        ],
    ]
