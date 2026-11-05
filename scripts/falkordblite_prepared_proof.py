"""Native disposable-database cases for the #109 prepared materialization experiment."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import Any

from falkordblite_prepared_materialization import (
    COMMIT,
    MAX_INPUTS,
    PreparedScalar,
    _keys,
    _require,
    commit,
    prepare,
)

from menhir.infrastructure.projection_lifecycle_repository_encoding import (
    _target_json,
    _work_key,
)
from menhir.services.scalar_projection_definition import (
    SCALAR_STATE_PROJECTION,
    scalar_projection_target,
)


def prepared_proofs(db: Any) -> list[tuple[str, Callable[[], object]]]:
    """Live assertions on bounded, synthetic storage using real scalar fold/hash semantics."""
    from redis.exceptions import ResponseError

    from menhir.domain.projection_lifecycle import ProjectionWorkToken

    def fixture(name: str, *, present: bool = True) -> tuple[Any, ProjectionWorkToken]:
        graph = db.select_graph("menhir109_prepared_" + name)
        row = {
            "assertion_id": "a1",
            "subject_uuid": "subject",
            "subject_display": "Alice",
            "namespace": "tenant-a",
            "attribute": "owned",
            "scope": "",
            "value_kind": "number",
            "unit": "count",
            "operation": "absolute",
            "value": 10,
            "valid_at": "2026-01-01T00:00:00+00:00",
            "learned_at": "2026-01-01T00:00:00+00:00",
            "evidence_tier": "user",
            "episode_uuid": "episode-a1",
            "absolute_semantics": "ordinary",
        }
        target = scalar_projection_target(row)
        token = ProjectionWorkToken(
            work_key=_work_key(SCALAR_STATE_PROJECTION.definition_id, target),
            definition_id=SCALAR_STATE_PROJECTION.definition_id,
            definition_version=1,
            target=target,
            generation=1,
            target_present=present,
            reason="proof",
        )
        work = {
            "work_key": token.work_key,
            "definition_id": token.definition_id,
            "definition_version": 1,
            "target_json": _target_json(target),
            "generation": 1,
            "applied_generation": 0,
            "target_present": present,
        }
        graph.query(
            "CREATE (d:PreparedDefinition) SET d=$definition "
            "CREATE (w:PreparedWork) SET w=$work CREATE (a:PreparedAssertion) SET a=$input "
            "CREATE (v:PreparedView {work_key:$work_key, namespace:'tenant-a', subject_uuid:'subject', payload_json:'old'})",
            {
                "definition": {
                    "definition_id": token.definition_id,
                    "current_version": 1,
                    "semantic_hash": "proof-definition-v1",
                },
                "work": work,
                "input": row,
                "work_key": token.work_key,
            },
        )
        return graph, token

    def snapshot(graph: Any) -> list[str]:
        return sorted(
            json.dumps(row, sort_keys=True)
            for row in graph.query("MATCH (n) RETURN labels(n), properties(n)").result_set
        )

    def roundtrip(present: bool) -> object:
        graph, token = fixture("present" if present else "retire", present=present)
        plan = prepare(graph, token, "derive")
        _require(commit(graph, plan) == "apply", "prepared payload did not apply")
        state = snapshot(graph)
        _require(commit(graph, plan) == "replay", "ack-loss replay was not recognized")
        _require(snapshot(graph) == state, "replay mutated graph")
        params = plan.parameters()
        actual = graph.query(
            "MATCH (v:PreparedView), (r:PreparedReceipt), (w:PreparedWork) "
            "RETURN properties(v), properties(r), properties(w)"
        ).result_set
        _require(
            actual == [[params["after_view"], params["after_receipt"], params["after_work"]]],
            "stored payload/receipt/work differs from the prepared contract",
        )
        return {"present": present, "hash": params["after_view"]["projection_hash"]}

    mutations = {
        "input_change": "MATCH (a:PreparedAssertion) SET a.value=11",
        "input_delete": "MATCH (a:PreparedAssertion) DELETE a",
        "input_insert": "MATCH (a:PreparedAssertion) WITH properties(a) AS row CREATE (b:PreparedAssertion) "
        "SET b=row SET b.assertion_id='a2', b.value=12",
        "generation": "MATCH (w:PreparedWork) SET w.generation=2",
        "definition": "MATCH (d:PreparedDefinition) SET d.semantic_hash='changed'",
        "view": "MATCH (v:PreparedView) SET v.payload_json='changed'",
        "receipt": "CREATE (:PreparedReceipt {receipt_key:$receipt_key, projection_hash:'other'})",
        "duplicate_view": "CREATE (:PreparedView {work_key:$work_key})",
    }

    def refuse(name: str, mutation: str) -> object:
        graph, token = fixture(name)
        plan = prepare(graph, token, "derive")
        graph.query(mutation, _keys(token))
        before = snapshot(graph)
        action = commit(graph, plan)
        _require(action not in ("apply", "replay"), f"{name} accepted a stale/corrupt preparation")
        _require(snapshot(graph) == before, f"{name} refusal mutated graph")
        return action

    def rollback() -> object:
        graph, token = fixture("failure")
        plan = prepare(graph, token, "derive")
        before = snapshot(graph)
        failing = COMMIT.rsplit("RETURN action", 1)[0] + "CREATE (n:PreparedBad) SET n.bad={nested:1} RETURN 1"
        try:
            graph.query(failing, plan.parameters())
        except ResponseError as exc:
            _require("Property values" in str(exc), f"unexpected injected failure: {exc}")
        else:
            raise AssertionError("failure injection did not fail")
        _require(snapshot(graph) == before, "query failure retained payload, certificate or receipt")
        return "whole snapshot restored after failure following payload/certificate writes"

    def race() -> object:
        graph, token = fixture("race")
        plans = [prepare(graph, token, f"derive-{i}") for i in range(2)]
        barrier = Barrier(2)

        def run(plan: PreparedScalar) -> str:
            barrier.wait(timeout=10)
            return commit(db.select_graph("menhir109_prepared_race"), plan)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, plan) for plan in plans]
                actions = [future.result(timeout=20) for future in futures]
            _require(actions.count("apply") == 1 and actions.count("stale_work") == 1, str(actions))
            count = graph.query("MATCH (r:PreparedReceipt) RETURN count(r)").result_set
            _require(count == [[1]], f"conflicting publishers produced {count} receipts")
            return actions
        finally:
            db.client.connection_pool.disconnect()

    def activation_deadline() -> object:
        graph, token = fixture("activation")
        now = graph.query("RETURN timestamp()").result_set[0][0]
        activation = now + 400
        valid_at = datetime.fromtimestamp(activation / 1000, tz=UTC).isoformat()
        graph.query(
            "MATCH (a:PreparedAssertion) WITH properties(a) AS row CREATE (b:PreparedAssertion) "
            "SET b=row SET b.assertion_id='future', b.value=20, b.valid_at=$valid_at",
            {"valid_at": valid_at},
        )
        plan = prepare(graph, token, "derive")
        payload = json.loads(plan.parameters()["after_view"]["payload_json"])
        _require(payload["value"] == "10", "future assertion activated during preparation")
        stop = time.monotonic() + 2
        while graph.query("RETURN timestamp()").result_set[0][0] <= activation:
            _require(time.monotonic() < stop, "database clock did not reach activation")
            time.sleep(0.01)
        before = snapshot(graph)
        _require(commit(graph, plan) == "expired", "prepared payload crossed an activation boundary")
        _require(snapshot(graph) == before, "expired preparation mutated graph")
        fresh = prepare(graph, token, "fresh")
        _require(
            json.loads(fresh.parameters()["after_view"]["payload_json"])["value"] == "20",
            "fresh preparation did not incorporate newly active assertion",
        )
        _require(commit(graph, fresh) == "apply", "fresh preparation failed")
        return "database-clock activation fence refused old payload; fresh fold installed value 20"

    def namespace_isolation() -> object:
        graph, token = fixture("namespace")
        plan = prepare(graph, token, "derive")
        graph.query(
            "CREATE (:PreparedAssertion {namespace:'tenant-b', subject_uuid:'subject', "
            "assertion_id:'foreign', value:999})"
        )
        query = "MATCH (a:PreparedAssertion {namespace:'tenant-b'}) RETURN properties(a)"
        before = graph.query(query).result_set
        _require(commit(graph, plan) == "apply", "unrelated namespace invalidated preparation")
        _require(graph.query(query).result_set == before, "commit mutated foreign input")
        return "other namespace neither contributed to the fold nor changed during commit"

    def writer_exit() -> object:
        graph, token = fixture("writer_exit")
        plan = prepare(graph, token, "derive")
        request = {
            "socket": db.client.socket_file,
            "graph": "menhir109_prepared_writer_exit",
            "query": COMMIT,
            "params": plan.parameters(),
        }
        # Exit after the database reply, before acknowledging anything to this parent. This
        # models loss of the caller process, not a database crash or a proven TCP fault.
        code = (
            "import json,os,sys; from falkordb import FalkorDB; r=json.load(sys.stdin); "
            "db=FalkorDB(unix_socket_path=r['socket']); "
            "db.select_graph(r['graph']).query(r['query'],r['params']); os._exit(71)"
        )
        child = subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        _require(child.returncode == 71 and not child.stdout, f"writer did not exit as injected: {child.stderr}")
        before = snapshot(graph)
        _require(commit(graph, plan) == "replay", "lost-writer recovery was not an exact replay")
        _require(snapshot(graph) == before, "recovery replay duplicated or changed materialization")
        return "writer exited after commit without parent acknowledgement; replay left snapshot unchanged"

    def input_bound() -> object:
        graph, token = fixture("bound")
        graph.query(
            "MATCH (a:PreparedAssertion) WITH properties(a) AS row UNWIND range(1,$n) AS i "
            "CREATE (b:PreparedAssertion) SET b=row SET b.assertion_id='extra-'+toString(i)",
            {"n": MAX_INPUTS + 2},
        )
        before = snapshot(graph)
        try:
            prepare(graph, token, "derive")
        except ValueError as exc:
            _require("input bound" in str(exc), str(exc))
        else:
            raise AssertionError("oversized input set was silently truncated and prepared")
        _require(snapshot(graph) == before, "oversize refusal mutated graph")
        return "bounded read detects overflow rather than certifying a truncated source set"

    def replay_refusals() -> object:
        outcomes = {}
        for name in ("input_change", "generation", "view", "receipt"):
            graph, token = fixture("replay_" + name)
            plan = prepare(graph, token, "derive")
            _require(commit(graph, plan) == "apply", "replay fixture did not apply")
            if name == "receipt":
                graph.query("MATCH (r:PreparedReceipt) SET r.projection_hash='changed'")
            else:
                graph.query(mutations[name], _keys(token))
            before = snapshot(graph)
            action = commit(graph, plan)
            _require(action not in ("apply", "replay"), f"{name} was blindly acknowledged as replay")
            _require(snapshot(graph) == before, f"{name} recovery replay mutated graph")
            outcomes[name] = action
        return outcomes

    proofs = [
        ("prepared_scalar_roundtrip_replay", lambda: roundtrip(True)),
        ("prepared_scalar_retirement_replay", lambda: roundtrip(False)),
        ("prepared_scalar_query_failure_rollback", rollback),
    ]
    proofs.extend(
        (f"prepared_scalar_refuses_{name}", lambda name=name, query=query: refuse(name, query))
        for name, query in mutations.items()
    )
    proofs.extend(
        [
            ("prepared_scalar_activation_fence", activation_deadline),
            ("prepared_scalar_namespace_isolation", namespace_isolation),
            ("prepared_scalar_writer_exit_replay", writer_exit),
            ("prepared_scalar_input_bound", input_bound),
            ("prepared_scalar_replay_revalidates_current_state", replay_refusals),
            ("prepared_scalar_competing_publishers", race),
        ]
    )
    return proofs
