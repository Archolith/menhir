#!/usr/bin/env python3
"""Issue #109 compatibility spike; creates only a fresh temporary embedded database.

Install the investigation pins from .agent/reference/menhir-falkordblite-compatibility.md.
Run: python scripts/falkordblite_compatibility.py

No environment files, service URLs, credentials, LLMs or embedders are used. JSON on stdout
reports independent checks; exit 1 means at least one compatibility gate failed. This is a
primitive compatibility probe, not a working Menhir backend or full issue #109 acceptance.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import platform
import tempfile
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def required_versions() -> dict[str, str]:
    """Exercise the fork pinned by this checkout, rather than silently using another release."""
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    prefix = "archolith-graphiti-core=="
    pins = [dep.removeprefix(prefix) for dep in project["project"]["dependencies"] if dep.startswith(prefix)]
    require(len(pins) == 1, "expected one exact Graphiti fork pin in pyproject.toml")
    return {"archolith-graphiti-core": pins[0], "falkordblite": "0.11.0"}


def check(name: str, work: Callable[[], object]) -> dict[str, object]:
    """Retain the failure evidence without turning an exception into a passing check."""
    try:
        detail = work()
    except Exception as exc:  # noqa: BLE001 - retain independent capability failure evidence
        return {"name": name, "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}
    return {"name": name, "status": "PASS", "detail": detail}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def exit_code(checks: list[dict[str, object]]) -> int:
    return int(not checks or any(item["status"] != "PASS" for item in checks))


def query_probes(db: Any) -> list[dict[str, object]]:
    """Exercise raw storage primitives separately from Graphiti's adapters."""
    from redis.exceptions import ResponseError

    graph = db.select_graph("menhir_probe")

    def bootstrap() -> object:
        for _ in range(2):
            graph.query(
                "MERGE (a:Probe {uuid:$a, namespace:$ns}) "
                "MERGE (b:Probe {uuid:$b, namespace:$ns}) MERGE (a)-[:LINK]->(b)",
                {"a": "a", "b": "b", "ns": "one"},
            )
        rows = graph.query("MATCH (a:Probe)-[r:LINK]->(b:Probe) RETURN count(r)").result_set
        require(rows == [[1]], f"MERGE replay duplicated an edge: {rows}")
        return rows

    def isolation() -> object:
        graph.query("CREATE (:Probe {uuid:'other', namespace:'two'})")
        rows = graph.query("MATCH (n:Probe {namespace:$ns}) RETURN count(n)", {"ns": "one"}).result_set
        require(rows == [[2]], f"namespace predicate returned {rows}")
        other = db.select_graph("menhir_empty")
        empty = other.query("MATCH (n) RETURN count(n)").result_set
        require(empty == [[0]], f"fresh graph saw another graph's nodes: {empty}")
        return {"filtered": rows, "other_graph": empty}

    def single_query_rollback() -> object:
        try:
            graph.query("CREATE (n:FailedQuery {uuid:'single'}) SET n.bad={nested:1} RETURN n")
        except ResponseError as exc:
            error = str(exc)
        else:
            raise AssertionError("invalid map property did not raise; failure injection is inconclusive")
        rows = graph.query("MATCH (n:FailedQuery) RETURN count(n)").result_set
        require(rows == [[0]], f"failed single query left writes: {rows}; error={error}")
        return {"remaining": rows, "error": error}

    def multi_rollback() -> object:
        with db.client.pipeline(transaction=True) as batch:
            batch.execute_command("GRAPH.QUERY", "menhir_multi", "CREATE (:FailedBatch {uuid:'first'})")
            batch.execute_command("GRAPH.QUERY", "menhir_multi", "RETURN nonexistent_function()")
            results = batch.execute(raise_on_error=False)
        require(isinstance(results[1], ResponseError), f"failure injection did not fail: {results}")
        rows = db.select_graph("menhir_multi").query("MATCH (n:FailedBatch) RETURN count(n)").result_set
        require(rows == [[0]], f"MULTI/EXEC left the first mutation: {rows}; error={results[1]}")
        return rows

    checks = [check("bootstrap_and_merge_replay", bootstrap), check("isolation_primitives", isolation)]
    checks += [check("single_query_rollback", single_query_rollback), check("multi_exec_rollback", multi_rollback)]
    for name, query in (
        ("neo4j_vector_expression", "RETURN vector.similarity.cosine([1.0,0.0],[1.0,0.0])"),
        ("neo4j_element_id", "MATCH (n:Probe) RETURN elementId(n) LIMIT 1"),
        ("neo4j_schema_introspection", "SHOW INDEXES YIELD name RETURN count(*) AS cnt"),
        (
            "neo4j_unique_constraint",
            "CREATE CONSTRAINT probe_uuid IF NOT EXISTS FOR (n:Probe) REQUIRE n.uuid IS UNIQUE",
        ),
        ("neo4j_temporal_value", "RETURN datetime()"),
    ):
        checks.append(check(name, lambda query=query: graph.query(query).result_set))
    return checks


async def graphiti_probes(db: Any) -> list[dict[str, object]]:
    """Use the published fork's actual driver, model save and search operations."""
    from falkordb.asyncio import FalkorDB
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode
    from graphiti_core.search.search_filters import SearchFilters

    client = FalkorDB(unix_socket_path=db.client.socket_file)
    driver = FalkorDriver(falkor_db=client, database="menhir_graphiti")
    checks: list[dict[str, object]] = []
    try:
        try:
            # The driver schedules its public schema builder from __init__ on this loop.
            await driver._init_task
            checks.append({"name": "graphiti_schema", "status": "PASS", "detail": "fork schema builder completed"})
        except Exception as exc:  # noqa: BLE001 - report schema failure and continue independent checks
            checks.append({"name": "graphiti_schema", "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"})

        try:
            node = EntityNode(
                uuid="probe_entity",
                name="Compatibility sentinel",
                group_id="probe_group",
                summary="Synthetic graph storage probe",
                created_at=datetime.now(UTC),
                name_embedding=[1.0, 0.0, 0.0],
            )
            await node.save(driver)
            found = await driver.search_ops.node_similarity_search(
                driver,
                [1.0, 0.0, 0.0],
                SearchFilters(),
                group_ids=["probe_group"],
                min_score=0.9,
            )
            require([n.uuid for n in found] == [node.uuid], f"vector search returned {[n.uuid for n in found]}")
            checks.append({"name": "graphiti_entity_vector_roundtrip", "status": "PASS", "detail": node.uuid})
        except Exception as exc:  # noqa: BLE001 - report model/search incompatibilities
            checks.append(
                {"name": "graphiti_entity_vector_roundtrip", "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}
            )

        try:
            target = EntityNode(
                uuid="probe_target",
                name="Target",
                group_id="probe_group",
                created_at=datetime.now(UTC),
            )
            await target.save(driver)
            edge = EntityEdge(
                uuid="probe_edge",
                source_node_uuid="probe_entity",
                target_node_uuid=target.uuid,
                group_id="probe_group",
                created_at=datetime.now(UTC),
                name="LINK",
                fact="Synthetic compatibility link",
                fact_embedding=[1.0, 0.0, 0.0],
            )
            await edge.save(driver)
            await edge.save(driver)
            restored = await EntityEdge.get_by_uuid(driver, edge.uuid)
            require(restored.fact == edge.fact, "edge roundtrip changed its fact")
            rows, _, _ = await driver.execute_query(
                "MATCH ()-[e:RELATES_TO {uuid:$uuid}]->() RETURN count(e) AS count",
                uuid=edge.uuid,
            )
            require(rows == [{"count": 1}], f"edge replay created duplicates: {rows}")
            checks.append({"name": "graphiti_edge_roundtrip_replay", "status": "PASS", "detail": rows})
        except Exception as exc:  # noqa: BLE001 - report model/edge incompatibilities
            checks.append(
                {"name": "graphiti_edge_roundtrip_replay", "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}
            )

        try:
            rows, _, _ = await driver.execute_query(
                "CALL db.idx.fulltext.queryNodes('Entity', 'Compatibility') YIELD node RETURN node.uuid AS uuid"
            )
            require([row["uuid"] for row in rows] == ["probe_entity"], f"fulltext returned {rows}")
            checks.append({"name": "graphiti_fulltext_index", "status": "PASS", "detail": rows})
        except Exception as exc:  # noqa: BLE001 - report fulltext incompatibilities
            checks.append(
                {"name": "graphiti_fulltext_index", "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}
            )

        class InjectedFailure(Exception):
            pass

        async def failed_publication(tx: Any) -> None:
            await tx.run("CREATE (:FailedPublication {uuid:'uncommitted'})")
            raise InjectedFailure("application rejected publication after its first write")

        try:
            async with driver.session() as session:
                await session.execute_write(failed_publication)
        except InjectedFailure:
            rows, _, _ = await driver.execute_query("MATCH (n:FailedPublication) RETURN count(n) AS remaining")
            checks.append(
                check(
                    "graphiti_callback_rollback",
                    lambda: require(rows == [{"remaining": 0}], f"callback failure left writes: {rows}"),
                )
            )
        else:
            checks.append({"name": "graphiti_callback_rollback", "status": "FAIL", "detail": "failure not propagated"})
    finally:
        await driver.close()
    return checks


def persistence_probe(db: Any, path: Path) -> dict[str, object]:
    """Close the embedded server, confirm it exited, then reopen the same database file."""
    import psutil
    from redislite.falkordb_client import FalkorDB

    db.select_graph("menhir_restart").query("CREATE (:Sentinel {uuid:'durable'})")
    pid = db.client.pid
    db.client.save()
    db.close()
    old_status = psutil.Process(pid).status() if psutil.pid_exists(pid) else "exited"
    # Container PID 1 may not reap a dead child; a zombie cannot serve requests.
    require(old_status in ("exited", psutil.STATUS_ZOMBIE), f"old server PID {pid} is {old_status}; restart not proved")
    require(path.is_file() and path.stat().st_size > 0, "no persisted database file")
    reopened = FalkorDB(str(path))
    try:
        rows = reopened.select_graph("menhir_restart").query("MATCH (n:Sentinel) RETURN n.uuid").result_set
        require(rows == [["durable"]], f"restart lost sentinel: {rows}")
        require(reopened.client.pid != pid, "reopen reused the original server")
        return {"old_pid": pid, "old_status": old_status, "new_pid": reopened.client.pid, "rows": rows}
    finally:
        reopened.close()


def main() -> int:
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
    report: dict[str, object] = {"platform": platform.platform(), "python": platform.python_version()}
    checks: list[dict[str, object]] = []
    report["checks"] = checks
    if platform.system() == "Windows":
        checks.append(
            {
                "name": "native_platform",
                "status": "FAIL",
                "detail": "0.11.0 has no native Windows wheel; Linux results do not qualify Windows",
            }
        )
    else:
        try:
            pins = required_versions()
            versions = {name: importlib.metadata.version(name) for name in pins}
            report["versions"] = versions
            require(versions == pins, f"install investigation pins {pins}; found {versions}")
            report["client_versions"] = {name: importlib.metadata.version(name) for name in ("falkordb", "redis")}
            from redislite.falkordb_client import FalkorDB

            with tempfile.TemporaryDirectory(prefix="menhir-109-") as directory:
                path = Path(directory) / "probe.rdb"
                db = FalkorDB(str(path))
                try:
                    report["redis_version"] = db.client.info("server")["redis_version"]
                    report["modules"] = db.client.module_list()
                    checks.extend(query_probes(db))
                    checks.extend(asyncio.run(graphiti_probes(db)))
                    checks.append(check("restart_persistence", lambda: persistence_probe(db, path)))
                finally:
                    db.close()
        except Exception as exc:  # noqa: BLE001 - dependency/startup failures must produce a failed report
            checks.append({"name": "probe_execution", "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"})
    report["decision"] = "BLOCKED" if exit_code(checks) else "PRIMITIVES_ONLY_NOT_BACKEND_QUALIFICATION"
    print(json.dumps(report, indent=2, default=str))
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
