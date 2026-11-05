"""Native disposable-database cases for the #109 prepared materialization experiment."""

from __future__ import annotations

import json
import re
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
    commit_production,
    commit_real_view,
    prepare,
    prepare_production,
    prepare_real_view,
    real_view_statement,
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
            "value_kind": "count",
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


def production_schema_proofs(db: Any) -> list[tuple[str, Callable[[], object]]]:
    """Qualify the real read/write contracts separately from the synthetic commit candidate.

    A FAIL is a backend acceptance gate, including a successful synthetic commit whose result
    disagrees with the real source read. No production services or data are used.
    """
    from falkordblite_projection_proof import AutocommitProbeRepository

    from menhir.infrastructure.typed_assertion_repository import (
        TypedAssertionRepository,
    )
    from menhir.infrastructure.view_repository import ViewRepository
    from menhir.services.scalar_projection_hash import scalar_projection_present_hash

    class Repository(AutocommitProbeRepository):
        def execute(self, query: str, params: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
            # The native probe has no transaction timeout API. Do not translate query semantics.
            return super().execute(query, params)

    class StringTemporalRepository(Repository):
        def execute(self, query: str, params: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
            # Diagnostic only: string fixtures let us inspect the next incompatibility. This is
            # deliberately NOT evidence for Neo4j temporal semantics or a runtime query translator.
            return super().execute(re.sub(r"datetime\((\$\w+)\)", r"\1", query), params, **kwargs)

    def fixture(name: str) -> tuple[Any, TypedAssertionRepository]:
        graph = db.select_graph("menhir109_schema_" + name)
        graph.query(
            "CREATE (a:TypedAssertion {assertion_id:'a1', subject_uuid:'subject', subject_display:'Alice', "
            "namespace:'tenant-a', attribute:'owned', scope:'', value_kind:'count', unit:'count', "
            "operation:'absolute', value:10, value_json:'10', evidence_tier:'user', "
            "valid_at:'2026-01-01T00:00:00Z', learned_at:'2026-01-01T00:00:00Z', episode_uuid:'turn-a'}) "
            "CREATE (t:TurnEvidence {turn_id:'turn-a', namespace:'tenant-a', evidence_finalized:true, "
            "evidence_generation:0}) "
            "CREATE (e:Episodic {uuid:'episode-a', group_id:'tenant-a'})-[:ADMITTED_ON]->(t) "
            "CREATE (:EvidenceNamespaceFence {namespace_key:'tenant-a', generation:0})"
        )
        return graph, TypedAssertionRepository(Repository(graph))

    def read(repository: TypedAssertionRepository) -> list[dict[str, Any]]:
        return repository.materializable_assertions_for_entity("subject", namespace="tenant-a")

    def graph_state(graph: Any) -> list[str]:
        rows = graph.query("MATCH (n) RETURN id(n), labels(n), properties(n)").result_set
        rows += graph.query("MATCH (a)-[r]->(b) RETURN id(a), id(r), type(r), properties(r), id(b)").result_set
        return sorted(json.dumps(row, sort_keys=True) for row in rows)

    def digest(rows: list[dict[str, Any]]) -> str:
        target = scalar_projection_target(rows[0])
        outcome = SCALAR_STATE_PROJECTION.fold(target, tuple(rows), datetime.now(UTC))
        state = outcome.payload
        return scalar_projection_present_hash(
            SCALAR_STATE_PROJECTION,
            target,
            value=state.value,
            valid_at=state.valid_at,
            contributor_ids=state.contributor_ids,
            effective_tier=state.effective_tier,
            episode_uuids=state.episode_uuids,
        )

    def real_read() -> object:
        graph, repository = fixture("read")
        graph.query(
            "MATCH (a:TypedAssertion) WITH properties(a) AS p "
            "CREATE (b:TypedAssertion) SET b=p SET b.assertion_id='pending', b.binding_pending=true "
            "CREATE (c:TypedAssertion) SET c=p SET c.assertion_id='old', c.superseded=true "
            "CREATE (d:TypedAssertion) SET d=p SET d.assertion_id='foreign', d.namespace='tenant-b'"
        )
        rows = read(repository)
        _require(len(rows) == 1 and rows[0]["assertion_id"] == "a1", f"eligibility drift: {rows}")
        _require(rows[0]["grounding_id"] == "turn-a" and rows[0]["episode_uuid"] == "episode-a", str(rows))
        before = digest(rows)
        graph.query("MATCH (a:TypedAssertion {assertion_id:'a1'}) SET a.evidence_tier='agent'")
        after = digest(read(repository))
        _require(before != after, "authority change did not change canonical hash")
        return "actual repository excludes pending/superseded/foreign rows, joins source episode and hashes authority"

    def work_fixture(name: str) -> tuple[Any, TypedAssertionRepository, Any]:
        graph, repository = fixture(name)
        rows = read(repository)
        target = scalar_projection_target(rows[0])
        from menhir.domain.projection_lifecycle import ProjectionWorkToken

        token = ProjectionWorkToken(
            work_key=_work_key(SCALAR_STATE_PROJECTION.definition_id, target),
            definition_id=SCALAR_STATE_PROJECTION.definition_id,
            definition_version=1,
            target=target,
            generation=1,
            target_present=True,
            reason="schema-proof",
        )
        # Prepare before changing relationship/liveness state; assertion properties stay identical.
        graph.query(
            "CREATE (:PreparedDefinition {definition_id:$id, current_version:1, semantic_hash:'proof-v1'}) "
            "CREATE (:PreparedWork {work_key:$key, definition_id:$id, definition_version:1, "
            "target_json:$target, generation:1, applied_generation:0, target_present:true})",
            {"id": token.definition_id, "key": token.work_key, "target": _target_json(target)},
        )
        return graph, repository, token

    def source_drift(mutation: str) -> object:
        graph, repository, token = work_fixture(mutation)
        original_hash = digest(read(repository))
        raw = graph.query("MATCH (a:TypedAssertion) RETURN properties(a)").result_set
        plan = prepare_production(graph, token, "derive")
        _require(plan.parameters()["after_view"]["projection_hash"] == original_hash, "fixture hash mismatch")
        if mutation == "admission_link":
            graph.query(
                "MATCH (e:Episodic)-[r:ADMITTED_ON]->(t:TurnEvidence) DELETE r "
                "CREATE (:Episodic {uuid:'episode-b', group_id:'tenant-a'})-[:ADMITTED_ON]->(t)"
            )
            _require(digest(read(repository)) != original_hash, "source relink did not alter actual fold hash")
        else:
            graph.query("MATCH (t:TurnEvidence) SET t.evidence_quarantined=true")
            # Read/fold alone retains this row. The production View gate must reject its evidence.
            _require(digest(read(repository)) == original_hash, "quarantine unexpectedly changed fold inputs")
        _require(graph.query("MATCH (a:TypedAssertion) RETURN properties(a)").result_set == raw, "assertion mutated")
        before = graph_state(graph)
        action = commit_production(graph, plan)
        _require(
            action == "stale_inputs",
            f"source-bound commit returned {action} after real {mutation} changed; assertion properties unchanged",
        )
        _require(graph_state(graph) == before, "source refusal mutated graph")
        return action

    def source_roundtrip() -> object:
        graph, repository, token = work_fixture("source_roundtrip")
        plan = prepare_production(graph, token, "derive")
        expected = digest(read(repository))
        _require(plan.parameters()["after_view"]["projection_hash"] == expected, "source snapshot/read fold mismatch")
        _require(commit_production(graph, plan) == "apply", "source publication refused")
        state = graph_state(graph)
        _require(commit_production(graph, plan) == "replay", "source replay was not recognized")
        _require(graph_state(graph) == state, "source replay mutated graph")
        graph.query("MATCH (t:TurnEvidence) SET t.evidence_quarantined=true")
        state = graph_state(graph)
        _require(commit_production(graph, plan) == "stale_inputs", "replay acknowledged quarantined evidence")
        _require(graph_state(graph) == state, "source replay refusal mutated graph")
        return "real source fold hash matches repository; atomic apply/replay and no-write refusal after quarantine"

    def source_multi_input() -> object:
        graph, repository, token = work_fixture("source_multi_input")
        graph.query(
            "MATCH (a:TypedAssertion) WITH properties(a) AS p "
            "CREATE (b:TypedAssertion) SET b=p SET b.assertion_id='delta', b.operation='delta', "
            "b.value=2, b.value_json='2', b.valid_at='2026-02-01T00:00:00Z' "
            "CREATE (c:TypedAssertion) SET c=p SET c.assertion_id='pending', c.binding_pending=true, c.episode_uuid='missing' "
            "CREATE (d:TypedAssertion) SET d=p SET d.assertion_id='superseded', d.superseded=true, d.episode_uuid='missing'"
        )
        plan = prepare_production(graph, token, "derive")
        _require(
            plan.parameters()["after_view"]["projection_hash"] == digest(read(repository)), "multi-input read mismatch"
        )
        payload = json.loads(plan.parameters()["after_view"]["payload_json"])
        _require(payload["value"] == "12" and payload["contributor_ids"] == ["a1", "delta"], str(payload))
        _require(commit_production(graph, plan) == "apply", "multi-input source apply refused")
        _require(commit_production(graph, plan) == "replay", "multi-input source replay refused")
        return "anchor/delta fold matches real read; pending/superseded rows remain guarded but do not contribute"

    def source_empty() -> object:
        graph, _, token = work_fixture("source_empty")
        graph.query("MATCH (a:TypedAssertion) DELETE a")
        plan = prepare_production(graph, token, "derive")
        _require(plan.parameters()["after_view"]["present"] is False, "empty source was not absent")
        _require(commit_production(graph, plan) == "apply", "empty source publication refused")
        state = graph_state(graph)
        _require(commit_production(graph, plan) == "replay", "empty source replay refused")
        _require(graph_state(graph) == state, "empty source replay mutated graph")
        return "empty closure produces absent hash and exact no-write replay"

    source_mutations = {
        "fence_generation": "MATCH (f:EvidenceNamespaceFence) SET f.generation=1",
        "evidence_generation": "MATCH (t:TurnEvidence) SET t.evidence_generation=1",
        "unfinalized": "MATCH (t:TurnEvidence) SET t.evidence_finalized=false",
        "foreign_turn": "MATCH (t:TurnEvidence) SET t.namespace='tenant-b'",
        "authority": "MATCH (a:TypedAssertion) SET a.evidence_tier='agent'",
        "pending": "MATCH (a:TypedAssertion) SET a.binding_pending=true",
        "superseded": "MATCH (a:TypedAssertion) SET a.superseded=true",
        "input_delete": "MATCH (a:TypedAssertion) DELETE a",
        "input_insert": "MATCH (a:TypedAssertion) WITH properties(a) AS p CREATE (b:TypedAssertion) "
        "SET b=p SET b.assertion_id='a2', b.value=11, b.value_json='11'",
        "parallel_admission": "MATCH (e:Episodic), (t:TurnEvidence) CREATE (e)-[:ADMITTED_ON]->(t)",
        "second_grounding": "MATCH (e:Episodic) CREATE (e)-[:ADMITTED_ON]->(:TurnEvidence {turn_id:'turn-b', namespace:'tenant-a'})",
        "episode_alias": "CREATE (:Episodic {uuid:'episode-a', group_id:'tenant-a'})",
        "turn_alias": "CREATE (:TurnEvidence {turn_id:'turn-a', namespace:'tenant-a'})",
        "direct_alias": "CREATE (:TurnEvidence {turn_id:'episode-a', namespace:'tenant-a'})",
        "evidence_collision": "CREATE (:Episodic {uuid:'turn-a', group_id:'tenant-a'})",
        "duplicate_fence": "CREATE (:EvidenceNamespaceFence {namespace_key:'tenant-a', generation:0})",
    }

    def refuse_source(name: str, query: str) -> object:
        graph, _, token = work_fixture("refuse_" + name)
        plan = prepare_production(graph, token, "derive")
        graph.query(query)
        state = graph_state(graph)
        action = commit_production(graph, plan)
        _require(action == "stale_inputs", f"{name} accepted: {action}")
        _require(graph_state(graph) == state, f"{name} refusal mutated graph")
        if name not in ("authority", "pending", "superseded", "input_delete", "input_insert"):
            try:
                prepare_production(graph, token, "fresh")
            except ValueError:
                pass
            else:
                raise AssertionError(f"fresh preparation accepted invalid {name} evidence")
            _require(graph_state(graph) == state, "invalid fresh preparation mutated graph")
        return action

    def source_bound() -> object:
        graph, _, token = work_fixture("source_bound")
        graph.query(
            "MATCH (a:TypedAssertion) WITH properties(a) AS p UNWIND range(1,$count) AS i "
            "CREATE (b:TypedAssertion) SET b=p SET b.assertion_id='extra-'+toString(i)",
            {"count": MAX_INPUTS},
        )
        state = graph_state(graph)
        try:
            prepare_production(graph, token, "derive")
        except ValueError as exc:
            _require("source closure bound" in str(exc), str(exc))
        else:
            raise AssertionError("source closure silently truncated")
        _require(graph_state(graph) == state, "source overflow refusal mutated graph")
        return "fetch-one-extra rejects source-closure overflow before any write"

    def source_rollback() -> object:
        from falkordblite_prepared_materialization import PRODUCTION_COMMIT
        from redis.exceptions import ResponseError

        graph, _, token = work_fixture("source_rollback")
        plan = prepare_production(graph, token, "derive")
        state = graph_state(graph)
        failing = (
            PRODUCTION_COMMIT.rsplit("RETURN action", 1)[0] + "CREATE (n:PreparedBad) SET n.bad={nested:1} RETURN 1"
        )
        try:
            graph.query(failing, plan.parameters())
        except ResponseError as exc:
            _require("Property values" in str(exc), f"unexpected failure: {exc}")
        else:
            raise AssertionError("source publication injection did not fail")
        _require(graph_state(graph) == state, "source query failure retained writes")
        return "failure after payload/certificate writes restores nodes and relationships"

    def source_race() -> object:
        graph, _, token = work_fixture("source_race")
        plans = [prepare_production(graph, token, f"derive-{i}") for i in range(2)]
        barrier = Barrier(2)

        def publish(plan: Any) -> str:
            barrier.wait(timeout=10)
            return commit_production(db.select_graph("menhir109_schema_source_race"), plan)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(publish, plan) for plan in plans]
                actions = [future.result(timeout=20) for future in futures]
            _require(actions.count("apply") == 1 and actions.count("stale_work") == 1, str(actions))
            _require(
                graph.query("MATCH (r:PreparedReceipt) RETURN count(r)").result_set == [[1]], "duplicate source receipt"
            )
            return actions
        finally:
            db.client.connection_pool.disconnect()

    def real_view_fixture(name: str):
        graph, _, token = work_fixture("atomic_view_" + name)
        graph.query(
            "MATCH (d:PreparedDefinition), (w:PreparedWork) "
            "SET d:ProjectionDefinitionState, w:ProjectionWorkState "
            "REMOVE d:PreparedDefinition, w:PreparedWork"
        )
        return graph, token

    def atomic_real_view_roundtrip() -> object:
        from dataclasses import replace

        from menhir.infrastructure.realization_coverage_repository import (
            ScalarStateProjectionHashSource,
        )

        graph, token = real_view_fixture("roundtrip")
        graph.query("MATCH (a:TypedAssertion) SET a.value=10.0,a.value_json='10.0'")
        plan = prepare_real_view(graph, token, "first")
        _require(commit_real_view(graph, plan) == "apply", "real View initial commit refused")
        reader = ScalarStateProjectionHashSource(StringTemporalRepository(graph))
        expected = reader.current_projection_hash(
            definition=SCALAR_STATE_PROJECTION, target=token.target, target_present=True
        )
        _require(plan.parameters()["after_receipt"]["projection_hash"] == expected, "real View hash differs")
        before = graph_state(graph)
        _require(commit_real_view(graph, plan) == "replay", "real View replay refused")
        _require(graph_state(graph) == before, "real View replay mutated graph")
        graph.query(
            "MATCH (a:TypedAssertion) CREATE (b:TypedAssertion) SET b=properties(a) "
            "SET b.assertion_id='a2', b.value=11, b.value_json='11', b.valid_at='2026-02-01T00:00:00Z' "
            "WITH b MATCH (w:ProjectionWorkState) SET w.generation=2"
        )
        second = prepare_real_view(graph, replace(token, generation=2), "second")
        _require(commit_real_view(graph, second) == "apply", "real View supersession refused")
        rows = graph.query(
            "MATCH (n:Entity)-[:SUPERSEDES]->(o:Entity) RETURN n.ss_value,n.view_current,o.ss_value,o.view_current"
        ).result_set
        _require(rows == [["11", True, "10", False]], f"real View history drift: {rows}")
        expected = reader.current_projection_hash(
            definition=SCALAR_STATE_PROJECTION, target=token.target, target_present=True
        )
        _require(second.parameters()["after_receipt"]["projection_hash"] == expected, "superseded hash differs")
        _require(
            graph.query("MATCH (:ProjectionDerivationReceipt) RETURN count(*)").result_set == [[2]], "receipt drift"
        )
        return "actual View create/supersede and provenance writes share one query with real work/receipt; hash reader agrees"

    def atomic_real_view_refuse(name: str, mutation: str) -> object:
        graph, token = real_view_fixture(name)
        plan = prepare_real_view(graph, token, "derive")
        graph.query(mutation, {"key": plan.view_key, "receipt": plan.parameters()["receipt_key"]})
        before = graph_state(graph)
        action = commit_real_view(graph, plan)
        _require(action != "apply", f"real View accepted {name}")
        _require(graph_state(graph) == before, f"real View refusal wrote after {name}")
        return action

    def atomic_real_view_rollback(*, supersede: bool = False) -> object:
        from dataclasses import replace

        graph, token = real_view_fixture("rollback_" + str(supersede))
        if supersede:
            _require(commit_real_view(graph, prepare_real_view(graph, token, "initial")) == "apply", "seed refused")
            graph.query(
                "MATCH (a:TypedAssertion) SET a.value=11,a.value_json='11' "
                "WITH a MATCH (w:ProjectionWorkState) SET w.generation=2"
            )
            token = replace(token, generation=2)
        plan = prepare_real_view(graph, token, "derive")
        query, params = real_view_statement(plan)
        # Fail AFTER the real View, provenance, work and receipt mutations have executed.
        query = query.removesuffix("RETURN action\n") + (
            "RETURN Menhir109Guard.requireOk(false,'injected late certification failure') AS action"
        )
        before = graph_state(graph)
        try:
            graph.query(query, params)
        except Exception as exc:  # noqa: BLE001 - injected native failure is the acceptance condition
            _require("injected late certification failure" in str(exc), f"unexpected query failure: {exc}")
        else:
            raise AssertionError("late certification failure did not raise")
        _require(graph_state(graph) == before, "late failure leaked View/history/provenance/work/receipt writes")
        _require(commit_real_view(graph, plan) == "apply", "rollback made work unrecoverable")
        return "late native failure restores complete graph; same prepared work succeeds afterward"

    def atomic_real_view_tampered_write(part: str) -> object:
        graph, token = real_view_fixture("tamper_" + part)
        plan = prepare_real_view(graph, token, "derive")
        query, params = real_view_statement(plan)
        if part == "writer":
            params["view_step0"] = {**params["view_step0"], "eps": ["missing"]}
        elif part == "provenance":
            params["view_step1"] = {**params["view_step1"], "anchor_ids": ["missing"]}
        else:
            params["expected_value"] = "invalid"
        before = graph_state(graph)
        try:
            graph.query(query, params)
        except Exception as exc:  # noqa: BLE001 - require the specific native postcondition failure
            _require("real View" in str(exc), f"unexpected query failure: {exc}")
        else:
            raise AssertionError("invalid View write/postcondition was certified")
        _require(graph_state(graph) == before, "refused View/provenance/postcondition leaked writes")
        return "native postcondition throws; complete graph unchanged"

    def atomic_real_view_race() -> object:
        graph, token = real_view_fixture("race")
        plans = [prepare_real_view(graph, token, "worker-a"), prepare_real_view(graph, token, "worker-b")]
        barrier = Barrier(2)

        def run(plan):
            barrier.wait(timeout=10)
            return commit_real_view(graph, plan)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run, plans))
            _require(sorted(results) == ["apply", "stale_work"], f"competing publications: {results}")
            _require(graph.query("MATCH (:Entity) RETURN count(*)").result_set == [[1]], "race forked history")
            _require(
                graph.query("MATCH (:ProjectionDerivationReceipt) RETURN count(*)").result_set == [[1]],
                "race forked receipt",
            )
            return results
        finally:
            db.client.connection_pool.disconnect()

    def atomic_real_view_replay_source_change() -> object:
        graph, token = real_view_fixture("replay_source")
        plan = prepare_real_view(graph, token, "derive")
        _require(commit_real_view(graph, plan) == "apply", "seed refused")
        graph.query("MATCH (t:TurnEvidence) SET t.evidence_quarantined=true")
        before = graph_state(graph)
        _require(commit_real_view(graph, plan) == "stale_inputs", "replay acknowledged quarantined evidence")
        _require(graph_state(graph) == before, "source-invalid replay mutated graph")
        return "replay revalidates source evidence without writes"

    def atomic_real_view_profile_refusal() -> object:
        from dataclasses import replace

        graph, token = real_view_fixture("profile")
        _require(commit_real_view(graph, prepare_real_view(graph, token, "initial")) == "apply", "seed refused")
        graph.query("MATCH (w:ProjectionWorkState) SET w.generation=2")
        before = graph_state(graph)
        for next_token in (replace(token, generation=2), replace(token, generation=2, target_present=False)):
            try:
                prepare_real_view(graph, next_token, "unsupported")
            except ValueError:
                _require(graph_state(graph) == before, "unsupported preparation mutated graph")
            else:
                raise AssertionError("unqualified refresh/retirement accepted")
        return "same-value refresh and retirement explicitly refused without writes"

    def atomic_real_view_replay_provenance_damage(role: str) -> object:
        graph, token = real_view_fixture("replay_damage_" + role)
        plan = prepare_real_view(graph, token, "derive")
        _require(commit_real_view(graph, plan) == "apply", "seed refused")
        graph.query("MATCH ()-[r:" + role + "]->() DELETE r")
        before = graph_state(graph)
        try:
            commit_real_view(graph, plan)
        except Exception as exc:  # noqa: BLE001 - corrupted replay must fail the native postcondition
            _require("real View postcondition failed" in str(exc), f"unexpected replay failure: {exc}")
        else:
            raise AssertionError("replay acknowledged broken live provenance")
        _require(graph_state(graph) == before, "corrupt replay mutated graph")
        return "replay refuses damaged live provenance without writes"

    def real_materializer_string_temporals() -> object:
        from dataclasses import replace

        from menhir.infrastructure.realization_coverage_repository import (
            ScalarStateProjectionHashSource,
        )
        from menhir.infrastructure.scalar_projection_materializer import (
            ScalarStateProjectionMaterializer,
        )

        graph, _, token = work_fixture("real_materializer_string_temporals")
        repository = StringTemporalRepository(graph)
        materialize = ScalarStateProjectionMaterializer()
        first_hash = materialize(repository, token)
        expected = ScalarStateProjectionHashSource(repository).current_projection_hash(
            definition=SCALAR_STATE_PROJECTION, target=token.target, target_present=True
        )
        _require(first_hash == expected, "real materializer disagrees with installed hash")
        rows = graph.query("MATCH (n:Entity {view_current:true}) RETURN n.episode_uuids").result_set
        _require(rows == [[["turn-a"]]], f"episode bridge was not normalized: {rows}")
        graph.query(
            "MATCH (a:TypedAssertion) CREATE (b:TypedAssertion) SET b=properties(a) "
            "SET b.assertion_id='next', b.value=11, b.value_json='11', b.valid_at='2026-02-01T00:00:00Z'"
        )
        second_hash = materialize(repository, replace(token, generation=2))
        _require(second_hash != first_hash, "new value retained old certification hash")
        history = graph.query(
            "MATCH (n:Entity)-[:SUPERSEDES]->(o:Entity) RETURN n.view_current, o.view_current"
        ).result_set
        _require(history == [[True, False]], f"materializer history drift: {history}")
        absent_hash = materialize(repository, replace(token, generation=3, target_present=False))
        _require(absent_hash != second_hash, "retirement retained present hash")
        rows = graph.query("MATCH (n:Entity) RETURN n.view_current ORDER BY n.uuid").result_set
        _require(rows == [[False], [False]], f"retirement lost history or retained current: {rows}")
        return "real materializer/hash reader agree after episode normalization; history/retirement pass with string temporals only; no atomic certification claim"

    def view_history(repository_type: type[Repository]) -> object:
        graph, _ = fixture("view_history_" + repository_type.__name__)
        views = ViewRepository(repository_type(graph))
        arguments = {
            "subject": "Alice",
            "subject_uuid": "subject",
            "attribute": "owned",
            "scope": "",
            "value_kind": "count",
            "unit": "count",
            "namespace": "tenant-a",
            "valid_at": "2026-01-01T00:00:00Z",
            "episode_uuids": ["episode-a"],
        }
        first = views.record_scalar_state(value=10, **arguments)
        second = views.record_scalar_state(value=11, **arguments)
        _require(first["uuid"] != second["uuid"] and second["superseded"], "history was overwritten")
        rows = graph.query(
            "MATCH (n:Entity)-[:SUPERSEDES]->(o:Entity) RETURN n.uuid, o.uuid, n.view_current, o.view_current"
        ).result_set
        _require(rows == [[second["uuid"], first["uuid"], True, False]], f"invalid history: {rows}")
        refreshed = views.record_scalar_state(value=11, audit={"effective_tier": "agent"}, **arguments)
        _require(refreshed["uuid"] == second["uuid"] and not refreshed["created"], "same-value refresh forked history")
        current = graph.query(
            "MATCH (t:TurnEvidence)-[:MENTIONS]->(n:Entity {uuid:$uuid}) RETURN t.turn_id, n.effective_tier",
            {"uuid": second["uuid"]},
        ).result_set
        _require(current == [["turn-a", "agent"]], f"same-value authority/provenance refresh drift: {current}")
        _require(views.retire_scalar_state(view_key=second["view_key"]), "retirement refused")
        remaining = graph.query(
            "MATCH (n:Entity {view_key:$key}) RETURN n.view_current ORDER BY n.uuid",
            {"key": second["view_key"]},
        ).result_set
        _require(remaining == [[False], [False]], f"retirement lost history or retained current: {remaining}")
        return "real writer created, superseded, refreshed authority/provenance and retired, preserving both versions"

    proofs = [
        ("production_scalar_assertion_read", real_read),
        ("prepared_production_source_roundtrip_replay", source_roundtrip),
        ("prepared_production_multi_input", source_multi_input),
        ("prepared_production_empty_source", source_empty),
        ("prepared_production_source_bound", source_bound),
        ("prepared_production_admission_link_fence", lambda: source_drift("admission_link")),
        ("prepared_production_evidence_liveness_fence", lambda: source_drift("quarantine")),
        ("prepared_production_query_failure_rollback", source_rollback),
        ("prepared_production_competing_publishers", source_race),
        ("production_scalar_view_history", lambda: view_history(Repository)),
        ("production_scalar_view_history_string_temporals", lambda: view_history(StringTemporalRepository)),
        ("production_scalar_materializer_string_temporals", real_materializer_string_temporals),
        ("atomic_real_view_roundtrip", atomic_real_view_roundtrip),
        ("atomic_real_view_late_failure_rollback", atomic_real_view_rollback),
        ("atomic_real_view_supersession_rollback", lambda: atomic_real_view_rollback(supersede=True)),
        (
            "atomic_real_view_quarantine_refusal",
            lambda: atomic_real_view_refuse("quarantine", "MATCH (t:TurnEvidence) SET t.evidence_quarantined=true"),
        ),
        (
            "atomic_real_view_work_refusal",
            lambda: atomic_real_view_refuse("work", "MATCH (w:ProjectionWorkState) SET w.generation=2"),
        ),
        (
            "atomic_real_view_definition_refusal",
            lambda: atomic_real_view_refuse(
                "definition", "MATCH (d:ProjectionDefinitionState) SET d.semantic_hash='changed'"
            ),
        ),
        (
            "atomic_real_view_prior_history_refusal",
            lambda: atomic_real_view_refuse("history", "CREATE (:Entity {view_key:$key})"),
        ),
        (
            "atomic_real_view_receipt_conflict",
            lambda: atomic_real_view_refuse(
                "receipt", "CREATE (:ProjectionDerivationReceipt {receipt_key:$receipt,derivation_id:'conflicting'})"
            ),
        ),
        ("atomic_real_view_writer_refusal_rollback", lambda: atomic_real_view_tampered_write("writer")),
        ("atomic_real_view_provenance_refusal_rollback", lambda: atomic_real_view_tampered_write("provenance")),
        ("atomic_real_view_postcondition_rollback", lambda: atomic_real_view_tampered_write("postcondition")),
        ("atomic_real_view_competing_publishers", atomic_real_view_race),
        ("atomic_real_view_replay_source_fence", atomic_real_view_replay_source_change),
        ("atomic_real_view_profile_refusal", atomic_real_view_profile_refusal),
        ("atomic_real_view_replay_mentions_damage", lambda: atomic_real_view_replay_provenance_damage("MENTIONS")),
        ("atomic_real_view_replay_anchor_damage", lambda: atomic_real_view_replay_provenance_damage("CURRENT_ANCHOR")),
    ]
    proofs.extend(
        (f"prepared_production_refuses_{name}", lambda name=name, query=query: refuse_source(name, query))
        for name, query in source_mutations.items()
    )
    return proofs
