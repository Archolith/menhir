"""E2E-21 erasure completeness through the installed stack (#423, #475, #476).

Erasing a memory or a namespace must leave none of its words anywhere Menhir stores data, and
recall must stop returning them. The scripted model derives real Graphiti entities and RELATES_TO
facts that quote each memory's unique phrase, so the graph checks are not vacuous: the phrase is
recorded in derived data BEFORE the erase, and must be gone after it.

Memories (one namespace): A and B both mention the shared entity; A also has a unique entity.
C is erased while its enrichment is still running (the provider is slowed for C's extraction).
A second namespace holds a TODO and a WorkArtifact for the `delete_namespace` check.

Product gap noted by this lane: no MCP tool or CLI flag registers a WorkArtifact in a chosen
namespace, so the WorkArtifact is seeded with the repository's own node shape by a direct write.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig, assert_not_production
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.phase3 import close_pass, lane_backend
from tests.e2e._harness.providers import DeterministicProviderHandler
from tests.e2e._harness.scripted_provider import Call, Rule, Script, scripted_model
from tests.e2e._harness.stack import graph_query

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1800)]

CRITERIA = [
    "erased_memory_words_gone_from_graph",
    "single_source_derivatives_removed",
    "shared_derivatives_flagged_and_hidden",
    "erased_words_gone_from_sidecar",
    "delete_reports_honestly",
    "erase_during_enrichment_leaves_nothing",
    "namespace_erase_covers_todos_and_artifacts",
    "erase_during_enrichment_leaves_no_attached_text",
    "repeat_erase_during_enrichment_leaves_nothing",
    "absent_target_erase_stays_in_namespace",
    "erase_with_busy_sidecar_is_honest",
    "no_unscripted_model_prompt",
]

_EPISODE_ID = re.compile(r"episode_id\W+([0-9a-f]{8}-[0-9a-f-]{27})")
_CURRENT = re.compile(r"<CURRENT MESSAGES>(.*?)</CURRENT MESSAGES>", re.DOTALL)
SHARED = "Shared Quartz Archive"
RACE_DELAY_SECONDS = 20.0


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    return "\n".join(part for part in (getattr(i, "text", "") for i in content) if part)


class Memory:
    """One memory and what the scripted extraction derives from it."""

    def __init__(self, key: str, suffix: str, *, shared: bool) -> None:
        self.key = key
        self.phrase = f"{key} cipher zorblat-{suffix}"
        self.unique_entity = f"Vault {key.upper()} {suffix}"
        self.shared = shared
        self.source_entity = SHARED if shared else f"Annex {key.upper()} {suffix}"
        self.text = f"The {self.source_entity} holds the {self.phrase} record in {self.unique_entity}."
        self.fact = f"The {self.source_entity} holds the {self.phrase} record."

    def entities(self) -> list[dict]:
        names = [self.source_entity, self.unique_entity]
        return [{"name": name, "entity_type_id": 0} for name in names]

    def edges(self) -> list[dict]:
        return [{
            "source_entity_name": self.source_entity,
            "target_entity_name": self.unique_entity,
            "relation_type": "HOLDS",
            "fact": self.fact,
            "episode_indices": [0],
        }]


def _current_text(prompt: str) -> str:
    match = _CURRENT.search(prompt)
    return match.group(1) if match else ""


def _extraction_reply(memories: list[Memory]):
    def reply(call: Call) -> str:
        schema = (call.payload.get("response_format") or {}).get("json_schema") or {}
        name = str(schema.get("name") or "")
        current = _current_text(call.prompt)
        memory = next((m for m in memories if m.phrase in current), None)
        if memory is None:
            return json.dumps(DeterministicProviderHandler._structured_response(
                name, call.prompt, schema.get("schema") or {}))
        if name in {"CombinedExtraction", "PatchedCombinedExtraction"}:
            body = {"extracted_entities": memory.entities(), "edges": memory.edges()}
        elif name == "ExtractedEntities":
            body = {"extracted_entities": [{**e, "episode_indices": [0]} for e in memory.entities()]}
        elif name == "ExtractedEdges":
            body = {"edges": [{**e, "valid_at": None, "invalid_at": None} for e in memory.edges()]}
        else:
            body = DeterministicProviderHandler._structured_response(
                name, call.prompt, schema.get("schema") or {})
        return json.dumps(body)

    return reply


def _carriers(config: E2EConfig, phrase: str) -> dict:
    """Nodes and relationships anywhere in the graph with a string property containing `phrase`."""
    contains = ("any(k IN keys({v}) WHERE ({v}[k] IS :: STRING AND {v}[k] CONTAINS $phrase) "
                "OR ({v}[k] IS :: LIST<STRING> AND any(x IN {v}[k] WHERE x CONTAINS $phrase)))")
    nodes = graph_query(
        config,
        f"MATCH (n) WHERE {contains.format(v='n')} "
        "RETURN labels(n) AS labels, coalesce(n.uuid, n.turn_id, n.assertion_id) AS id, "
        "[k IN keys(n) WHERE (n[k] IS :: STRING AND n[k] CONTAINS $phrase) "
        "OR (n[k] IS :: LIST<STRING> AND any(x IN n[k] WHERE x CONTAINS $phrase))] AS props",
        phrase=phrase,
    )
    rels = graph_query(
        config,
        f"MATCH ()-[r]->() WHERE {contains.format(v='r')} "
        "RETURN type(r) AS type, r.uuid AS id, "
        "[k IN keys(r) WHERE (r[k] IS :: STRING AND r[k] CONTAINS $phrase) "
        "OR (r[k] IS :: LIST<STRING> AND any(x IN r[k] WHERE x CONTAINS $phrase))] AS props",
        phrase=phrase,
    )
    return {"nodes": nodes, "relationships": rels}


def _empty(carriers: dict) -> bool:
    return not carriers["nodes"] and not carriers["relationships"]


def _entity(config: E2EConfig, namespace: str, name: str) -> list[dict]:
    return graph_query(
        config,
        "MATCH (e:Entity {group_id: $ns, name: $name}) "
        "RETURN e.uuid AS uuid, coalesce(e.rederive_pending, false) AS rederive_pending",
        ns=namespace, name=name,
    )


def _sidecar_hits(db: Path, phrase: str) -> dict[str, int]:
    needle = phrase.encode("utf-8")
    hits: dict[str, int] = {}
    for suffix in ("", "-wal"):
        path = Path(str(db) + suffix)
        hits[path.name] = path.read_bytes().count(needle) if path.exists() else -1
    return hits


def _seed_work_artifact(config: E2EConfig, namespace: str, artifact_uuid: str) -> None:
    """Write a :WorkArtifact with an :ArtifactSource in `namespace` (shape of create_artifact)."""
    assert_not_production(config.neo4j_uri, what="graph write target")
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(config.neo4j_uri, auth=(config.neo4j_user, config.neo4j_password))
    try:
        with driver.session(database=config.neo4j_database) as session:
            session.run(
                "CREATE (a:WorkArtifact {artifact_uuid: $u, artifact_type: 'plan', "
                "title: 'e21 namespace artifact', status: 'PROPOSED', namespace: $ns, "
                "created_at: $now, updated_at: $now, status_changed_at: $now, schema_version: 1}) "
                "CREATE (a)-[:EMBODIED_IN]->(s:ArtifactSource {medium: 'markdown', "
                "path: '.agent/plans/e21.md', first_seen_at: $now, last_seen_at: $now})",
                u=artifact_uuid, ns=namespace, now="2026-11-09T00:00:00+00:00",
            ).consume()
    finally:
        driver.close()


def _seed_orphan_edge(config: E2EConfig, namespace: str, dead_uuid: str, fact: str) -> dict:
    """Write two :Entity nodes and a RELATES_TO edge in `namespace` whose episodes=[dead_uuid]."""
    assert_not_production(config.neo4j_uri, what="graph write target")
    from neo4j import GraphDatabase

    ids = {"a": str(uuid4()), "b": str(uuid4()), "edge": str(uuid4())}
    driver = GraphDatabase.driver(config.neo4j_uri, auth=(config.neo4j_user, config.neo4j_password))
    try:
        with driver.session(database=config.neo4j_database) as session:
            session.run(
                "CREATE (a:Entity {uuid: $a, group_id: $ns, name: $an, created_at: datetime()}) "
                "CREATE (b:Entity {uuid: $b, group_id: $ns, name: $bn, created_at: datetime()}) "
                "CREATE (a)-[:RELATES_TO {uuid: $edge, group_id: $ns, name: 'HOLDS', fact: $fact, "
                "episodes: [$dead], created_at: datetime()}]->(b)",
                a=ids["a"], b=ids["b"], edge=ids["edge"], ns=namespace, fact=fact,
                an=f"Orphan Left {ids['a'][:8]}", bn=f"Orphan Right {ids['b'][:8]}", dead=dead_uuid,
            ).consume()
    finally:
        driver.close()
    return ids


def _edge_state(config: E2EConfig, edge_uuid: str) -> list[dict]:
    return graph_query(
        config,
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN r.fact AS fact, r.episodes AS episodes, "
        "r.group_id AS group_id, r.rederive_pending AS rederive_pending",
        u=edge_uuid,
    )


def _entity_pending_by_uuid(config: E2EConfig, uuids: list[str]) -> list[dict]:
    return graph_query(
        config,
        "MATCH (e:Entity) WHERE e.uuid IN $ids RETURN e.uuid AS uuid, e.rederive_pending AS pending",
        ids=uuids,
    )


def _count_namespace(config: E2EConfig, namespace: str) -> int:
    rows = graph_query(
        config,
        "MATCH (n) WHERE n.namespace = $ns OR n.group_id = $ns RETURN count(n) AS c",
        ns=namespace,
    )
    return int(rows[0]["c"])


def _json_or_text(text: str) -> dict:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"raw": text}
    except ValueError:
        return {"raw": text}


async def _add_tracked(client, namespace: str, memory: Memory) -> str:
    receipt = _text(await client.call_tool("call_tool", {
        "name": "add_memory_and_track",
        "arguments": {"text": memory.text, "namespace": namespace, "timeout_s": 180.0},
    }))
    match = _EPISODE_ID.search(receipt)
    assert match, f"no episode_id for memory {memory.key}: {receipt[:400]}"
    assert "status: READY" in receipt, (
        f"memory {memory.key} did not reach READY, so nothing was derived from it:\n{receipt[:600]}"
    )
    return match.group(1)


async def _delete_memory(client, namespace: str, uuid: str) -> str:
    return _text(await client.call_tool("call_tool", {
        "name": "delete_memory", "arguments": {"node_uuid": uuid, "namespace": namespace},
    }))


async def test_e2e_21_erasure(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    suffix = uuid4().hex[:10]
    ns = f"e21-{suffix}"
    ns2 = f"e21b-{suffix}"
    mem_a = Memory("amber", suffix, shared=True)
    mem_b = Memory("birch", suffix, shared=True)
    mem_c = Memory("cedar", suffix, shared=False)
    mem_d = Memory("dune", suffix, shared=False)
    mem_e = Memory("ember", suffix, shared=False)
    memories = [mem_a, mem_b, mem_c, mem_d, mem_e]
    diff_phrase = f"cedar diffmark zorblat-{suffix}"
    ns3 = f"e21c-{suffix}"
    ns4 = f"e21d-{suffix}"
    telemetry_db = e2e_config.state_dir / f"e21-{suffix}" / "mcp_telemetry.db"
    telemetry_db.parent.mkdir(parents=True, exist_ok=True)

    race: dict[str, float | None] = {"started": None, "returned": None, "delete_sent": None}
    race_lock = threading.Lock()

    race_d: dict[str, float | None] = {"started": None, "returned": None, "delete_sent": None}
    slowed = ((mem_c, race), (mem_d, race_d))

    def delay_for(prompt: str) -> float:
        # Slow only the first extraction call for C and for D, so a delete lands mid-extraction.
        current = _current_text(prompt)
        with race_lock:
            for memory, state in slowed:
                if memory.phrase in current and state["started"] is None:
                    state["started"] = time.time()
                    return RACE_DELAY_SECONDS
        return 0.0

    base_reply = _extraction_reply(memories)

    def extraction_reply(call: Call) -> str:
        out = base_reply(call)
        current = _current_text(call.prompt)
        for memory, state in slowed:
            if memory.phrase in current:
                with race_lock:
                    if state["started"] is not None and state["returned"] is None:
                        state["returned"] = time.time()
        return out

    script = Script([
        Rule("extract", "<CURRENT MESSAGES>", extraction_reply, structured=True),
        # Menhir's own entity-identity judge (post-enrichment merge proposals): never the same.
        Rule("identity_judge", "You are an entity identity judge", ["DIFFERENT"]),
    ])
    script.delay_for = delay_for

    with scripted_model(script) as model:
        env = {**feature_env, **model.env, "MENHIR_MCP_TELEMETRY_DB": str(telemetry_db)}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                # ---- A and B: a shared entity, a unique entity each ---------------------
                episode_a = await _add_tracked(client, ns, mem_a)
                await _add_tracked(client, ns, mem_b)

                before_graph = _carriers(e2e_config, mem_a.phrase)
                # Telemetry rows are written asynchronously; give them time to land.
                sidecar_deadline = time.time() + 20
                before_sidecar = _sidecar_hits(telemetry_db, mem_a.phrase)
                while (not any(c > 0 for c in before_sidecar.values())
                       and time.time() < sidecar_deadline):
                    time.sleep(1.0)
                    before_sidecar = _sidecar_hits(telemetry_db, mem_a.phrase)
                derived_before = {
                    "unique_entity": _entity(e2e_config, ns, mem_a.unique_entity),
                    "annex_entity": _entity(e2e_config, ns, mem_a.source_entity),
                    "shared_entity": _entity(e2e_config, ns, SHARED),
                    "fact_edges": graph_query(
                        e2e_config,
                        "MATCH ()-[r:RELATES_TO]->() WHERE r.fact CONTAINS $p "
                        "RETURN r.uuid AS uuid", p=mem_a.phrase),
                    "b_fact_edges": graph_query(
                        e2e_config,
                        "MATCH ()-[r:RELATES_TO]->() WHERE r.fact CONTAINS $p "
                        "RETURN r.uuid AS uuid", p=mem_b.phrase),
                }
                present_before = {
                    "graph_nodes": len(before_graph["nodes"]),
                    "graph_relationships": len(before_graph["relationships"]),
                    "sidecar": before_sidecar,
                }

                # ---- erase A through the MCP tool ----------------------------------------
                reply_a = await _delete_memory(client, ns, episode_a)

                after_graph = _carriers(e2e_config, mem_a.phrase)
                after_sidecar = _sidecar_hits(telemetry_db, mem_a.phrase)
                derived_after = {
                    "unique_entity": _entity(e2e_config, ns, mem_a.unique_entity),
                    "fact_edges": graph_query(
                        e2e_config,
                        "MATCH ()-[r:RELATES_TO]->() WHERE r.fact CONTAINS $p "
                        "RETURN r.uuid AS uuid", p=mem_a.phrase),
                    "shared_entity": _entity(e2e_config, ns, SHARED),
                    "b_fact_edges": graph_query(
                        e2e_config,
                        "MATCH ()-[r:RELATES_TO]->() WHERE r.fact CONTAINS $p "
                        "RETURN r.uuid AS uuid", p=mem_b.phrase),
                }
                recall_text = _text(await client.call_tool("recall_memories", {
                    "query": SHARED, "namespace": ns, "limit": 10}))

                # ---- C: erased while its enrichment is running ---------------------------
                add_c = _text(await client.call_tool("add_memory", {
                    "text": mem_c.text, "namespace": ns, "diff": diff_phrase}))
                match_c = _EPISODE_ID.search(add_c)
                assert match_c, f"no episode_id for memory C: {add_c[:400]}"
                episode_c = match_c.group(1)
                state_at_delete = None
                deadline = time.time() + 90
                while time.time() < deadline:
                    rows = graph_query(
                        e2e_config, "MATCH (e:Episodic {uuid: $u}) RETURN e.processing_state AS s",
                        u=episode_c)
                    state_at_delete = rows[0]["s"] if rows else None
                    if state_at_delete == "ENRICHING" or state_at_delete == "READY":
                        break
                    time.sleep(0.25)
                with race_lock:
                    race["delete_sent"] = time.time()
                reply_c = await _delete_memory(client, ns, episode_c)
                # The erase has returned: its words must be gone now, not only once the worker's
                # compensation later deletes the tombstone (which a crash or no-twin path skips).
                carriers_diff_at_return = _carriers(e2e_config, diff_phrase)
                queue_node_after_delete = graph_query(
                    e2e_config,
                    "MATCH (e:Episodic {uuid: $u}) RETURN e.processing_state AS state, "
                    "e.processing_substage AS substage, e.erased_at IS NOT NULL AS erased",
                    u=episode_c)
                with race_lock:
                    delay_started = race["started"]
                    returned_at_delete = race["returned"]
                # Wait for the slowed extraction to return, then for the worker to settle.
                settle_deadline = time.time() + RACE_DELAY_SECONDS + 120
                while time.time() < settle_deadline:
                    with race_lock:
                        if race["returned"] is not None:
                            break
                    time.sleep(0.5)
                with race_lock:
                    returned_at = race["returned"]
                residue_seen = False
                stable_since = None
                post_deadline = time.time() + 90
                while time.time() < post_deadline:
                    carriers_c = _carriers(e2e_config, mem_c.phrase)
                    elapsed = time.time() - (returned_at or time.time())
                    if not _empty(carriers_c):
                        residue_seen = True
                        stable_since = None
                    elif elapsed >= 10:
                        if stable_since is None:
                            stable_since = time.time()
                        elif time.time() - stable_since >= 5:
                            break
                    time.sleep(1.0)
                carriers_c = _carriers(e2e_config, mem_c.phrase)
                c_entities_left = (_entity(e2e_config, ns, mem_c.unique_entity)
                                   + _entity(e2e_config, ns, mem_c.source_entity))

                carriers_diff = _carriers(e2e_config, diff_phrase)

                # ---- D: erased TWICE while its enrichment is running ---------------------
                add_d = _text(await client.call_tool("add_memory", {
                    "text": mem_d.text, "namespace": ns}))
                match_d = _EPISODE_ID.search(add_d)
                assert match_d, f"no episode_id for memory D: {add_d[:400]}"
                episode_d = match_d.group(1)
                state_d_at_delete = None
                deadline = time.time() + 90
                while time.time() < deadline:
                    rows = graph_query(
                        e2e_config, "MATCH (e:Episodic {uuid: $u}) RETURN e.processing_state AS s",
                        u=episode_d)
                    state_d_at_delete = rows[0]["s"] if rows else None
                    if state_d_at_delete == "ENRICHING" or state_d_at_delete == "READY":
                        break
                    time.sleep(0.25)
                reply_d1 = await _delete_memory(client, ns, episode_d)
                reply_d2 = await _delete_memory(client, ns, episode_d)
                with race_lock:
                    race_d["delete_sent"] = time.time()
                    d_started = race_d["started"]
                    d_returned_at_delete = race_d["returned"]
                settle_deadline = time.time() + RACE_DELAY_SECONDS + 120
                while time.time() < settle_deadline:
                    with race_lock:
                        if race_d["returned"] is not None:
                            break
                    time.sleep(0.5)
                with race_lock:
                    d_returned_at = race_d["returned"]
                d_residue_seen = False
                stable_since = None
                post_deadline = time.time() + 90
                while time.time() < post_deadline:
                    carriers_d = _carriers(e2e_config, mem_d.phrase)
                    elapsed = time.time() - (d_returned_at or time.time())
                    if not _empty(carriers_d):
                        d_residue_seen = True
                        stable_since = None
                    elif elapsed >= 10:
                        if stable_since is None:
                            stable_since = time.time()
                        elif time.time() - stable_since >= 5:
                            break
                    time.sleep(1.0)
                carriers_d = _carriers(e2e_config, mem_d.phrase)
                d_entities_left = (_entity(e2e_config, ns, mem_d.unique_entity)
                                   + _entity(e2e_config, ns, mem_d.source_entity))

                # ---- F4: erase of an absent uuid must not touch another namespace --------
                dead_uuid = str(uuid4())
                orphan_fact = f"orphan fact zorblat-{suffix} restated"
                orphan = _seed_orphan_edge(e2e_config, ns3, dead_uuid, orphan_fact)
                orphan_before = _edge_state(e2e_config, orphan["edge"])
                reply_absent = await _delete_memory(client, ns4, dead_uuid)
                orphan_after = _edge_state(e2e_config, orphan["edge"])
                orphan_entities_after = _entity_pending_by_uuid(
                    e2e_config, [orphan["a"], orphan["b"]])

                # ---- E: erase while another process holds a sqlite read snapshot ---------
                episode_e_id = await _add_tracked(client, ns, mem_e)
                e_deadline = time.time() + 20
                e_before = _sidecar_hits(telemetry_db, mem_e.phrase)
                while not any(c > 0 for c in e_before.values()) and time.time() < e_deadline:
                    time.sleep(1.0)
                    e_before = _sidecar_hits(telemetry_db, mem_e.phrase)
                import sqlite3
                reader = sqlite3.connect(str(telemetry_db), isolation_level=None, timeout=1.0)
                try:
                    reader.execute("BEGIN")
                    reader.execute("SELECT count(*) FROM sqlite_master").fetchall()
                    reply_e = await _delete_memory(client, ns, episode_e_id)
                    e_after_held = _sidecar_hits(telemetry_db, mem_e.phrase)
                    e_graph_after = _carriers(e2e_config, mem_e.phrase)
                finally:
                    reader.close()
                e_after_closed = _sidecar_hits(telemetry_db, mem_e.phrase)

                # ---- second namespace: TODO + WorkArtifact -------------------------------
                todo_receipt = _text(await client.call_tool("add_todo", {
                    "text": f"e21 namespace todo {suffix}", "namespace": ns2}))
                artifact_uuid = str(uuid4())
                _seed_work_artifact(e2e_config, ns2, artifact_uuid)
                nodes_before_ns2 = _count_namespace(e2e_config, ns2)
                dry = _json_or_text(_text(await client.call_tool("call_tool", {
                    "name": "delete_namespace",
                    "arguments": {"namespace": ns2, "dry_run": True}})))
                nodes_after_dry = _count_namespace(e2e_config, ns2)
                real = _json_or_text(_text(await client.call_tool("call_tool", {
                    "name": "delete_namespace", "arguments": {"namespace": ns2}})))
                nodes_after_real = _count_namespace(e2e_config, ns2)
                leftover_labels = graph_query(
                    e2e_config,
                    "MATCH (n) WHERE n.namespace = $ns OR n.group_id = $ns "
                    "RETURN labels(n) AS labels", ns=ns2)

    # ------------------------------------------------------------------ evidence
    lane_evidence.attach("erasure.json", json.dumps({
        "phrases": {m.key: m.phrase for m in memories},
        "before_present": present_before, "before_graph": before_graph,
        "derived_before": derived_before, "reply_a": reply_a,
        "after_graph": after_graph, "after_sidecar": after_sidecar,
        "derived_after": derived_after, "recall_text": recall_text,
        "race": {"state_at_delete": state_at_delete, "delay_started": delay_started,
                 "delete_sent": race["delete_sent"], "returned_at_delete": returned_at_delete,
                 "extraction_returned": returned_at, "residue_seen_after_return": residue_seen,
                 "reply_c": reply_c, "queue_node_after_delete": queue_node_after_delete,
                 "carriers_c": carriers_c, "c_entities_left": c_entities_left},
        "namespace": {"todo_receipt": todo_receipt, "artifact_uuid": artifact_uuid,
                      "nodes_before": nodes_before_ns2, "nodes_after_dry": nodes_after_dry,
                      "dry": dry, "real": real, "nodes_after_real": nodes_after_real,
                      "leftover_labels": leftover_labels},
        "attached_text": {"diff_phrase": diff_phrase, "carriers_at_return": carriers_diff_at_return,
                          "carriers_after": carriers_diff},
        "repeat_erase": {"state_at_delete": state_d_at_delete, "delay_started": d_started,
                         "delete_sent": race_d["delete_sent"],
                         "returned_at_delete": d_returned_at_delete,
                         "extraction_returned": d_returned_at,
                         "residue_seen_after_return": d_residue_seen,
                         "reply_first": reply_d1, "reply_second": reply_d2,
                         "carriers_d": carriers_d, "entities_left": d_entities_left},
        "absent_target": {"namespace_seeded": ns3, "namespace_called": ns4,
                          "dead_uuid": dead_uuid, "orphan": orphan, "fact": orphan_fact,
                          "edge_before": orphan_before, "edge_after": orphan_after,
                          "entities_after": orphan_entities_after, "reply": reply_absent},
        "busy_sidecar": {"sidecar_before": e_before, "sidecar_after_while_reader_held": e_after_held,
                         "sidecar_after_reader_closed": e_after_closed, "reply": reply_e,
                         "graph_after": e_graph_after},
    }, indent=2, default=str))

    where_before = [name for name, hit in (
        ("graph", bool(before_graph["nodes"] or before_graph["relationships"])),
        ("sidecar", any(count > 0 for count in before_sidecar.values()))) if hit]

    lane_evidence.record(
        "erased_memory_words_gone_from_graph",
        passed=bool(before_graph["relationships"]) and bool(before_graph["nodes"])
        and _empty(after_graph),
        detail={"before_nodes": before_graph["nodes"], "before_relationships":
                before_graph["relationships"], "after": after_graph},
    )
    single_before = bool(derived_before["unique_entity"]) and bool(derived_before["fact_edges"])
    lane_evidence.record(
        "single_source_derivatives_removed",
        passed=single_before and not derived_after["unique_entity"]
        and not derived_after["fact_edges"],
        detail={"before": derived_before, "after": derived_after},
    )
    shared_after = derived_after["shared_entity"]
    hidden = (SHARED in recall_text and mem_a.phrase.lower() not in recall_text.lower()
              and mem_a.unique_entity.lower() not in recall_text.lower())
    lane_evidence.record(
        "shared_derivatives_flagged_and_hidden",
        passed=len(derived_before["shared_entity"]) == 1 and len(shared_after) == 1
        and bool(shared_after[0]["rederive_pending"]) and hidden,
        detail={"shared_before": derived_before["shared_entity"], "shared_after": shared_after,
                "recall_contains_name": SHARED in recall_text,
                "recall_contains_phrase": mem_a.phrase.lower() in recall_text.lower(),
                "recall": recall_text[:2000]},
    )
    lane_evidence.record(
        "erased_words_gone_from_sidecar",
        passed=bool(where_before) and all(count <= 0 for count in after_sidecar.values()),
        detail={"telemetry_db": str(telemetry_db), "present_before_in": where_before,
                "sidecar_before": before_sidecar, "sidecar_after": after_sidecar},
    )
    says_erased = (reply_a.startswith(f"Erased memory {episode_a}") and "INCOMPLETE" not in reply_a
                   and "did NOT" not in reply_a)
    names_gap = "INCOMPLETE" in reply_a or "did NOT" in reply_a or "NOTE:" in reply_a
    lane_evidence.record(
        "delete_reports_honestly",
        passed=(says_erased and _empty(after_graph)
                and all(count <= 0 for count in after_sidecar.values())) or names_gap,
        detail={"reply": reply_a, "says_erased": says_erased, "names_gap": names_gap},
    )
    race_hit = (state_at_delete == "ENRICHING" and delay_started is not None
                and returned_at is not None and race["delete_sent"] is not None
                and returned_at_delete is None and race["delete_sent"] < returned_at)
    lane_evidence.record(
        "erase_during_enrichment_leaves_nothing",
        passed=race_hit and _empty(carriers_c) and not c_entities_left,
        detail={"race_window_hit": race_hit, "state_at_delete": state_at_delete,
                "path": ("worker_compensation" if residue_seen else
                         "worker_wrote_nothing_after_erase") if race_hit else "race_not_hit",
                "residue_seen_after_extraction_returned": residue_seen,
                "carriers_after": carriers_c, "entities_left": c_entities_left,
                "reply_c": reply_c},
    )
    lane_evidence.record(
        "erase_during_enrichment_leaves_no_attached_text",
        passed=race_hit and _empty(carriers_diff_at_return) and _empty(carriers_diff),
        detail={"race_window_hit": race_hit, "diff_phrase": diff_phrase,
                "carriers_at_return": carriers_diff_at_return,
                "carriers_after": carriers_diff},
    )
    d_race_hit = (state_d_at_delete == "ENRICHING" and d_started is not None
                  and d_returned_at is not None and race_d["delete_sent"] is not None
                  and d_returned_at_delete is None and race_d["delete_sent"] < d_returned_at)
    lane_evidence.record(
        "repeat_erase_during_enrichment_leaves_nothing",
        passed=d_race_hit and _empty(carriers_d) and not d_entities_left,
        detail={"race_window_hit": d_race_hit, "state_at_delete": state_d_at_delete,
                "residue_seen_after_extraction_returned": d_residue_seen,
                "carriers_after": carriers_d, "entities_left": d_entities_left,
                "reply_first": reply_d1, "reply_second": reply_d2},
    )
    edge_same = (len(orphan_before) == 1 and len(orphan_after) == 1
                 and orphan_after[0]["fact"] == orphan_fact
                 and orphan_after[0]["episodes"] == [dead_uuid]
                 and not orphan_after[0]["rederive_pending"]
                 and orphan_after[0]["group_id"] == ns3)
    lane_evidence.record(
        "absent_target_erase_stays_in_namespace",
        passed=edge_same and len(orphan_entities_after) == 2
        and not any(e["pending"] for e in orphan_entities_after),
        detail={"edge_before": orphan_before, "edge_after": orphan_after,
                "entities_after": orphan_entities_after, "reply": reply_absent},
    )
    e_says_erased = (reply_e.startswith(f"Erased memory {episode_e_id}")
                     and "INCOMPLETE" not in reply_e and "did NOT" not in reply_e)
    e_names_gap = "INCOMPLETE" in reply_e or "did NOT" in reply_e or "NOTE:" in reply_e
    e_phrase_gone = all(count <= 0 for count in e_after_held.values())
    lane_evidence.record(
        "erase_with_busy_sidecar_is_honest",
        passed=bool(any(c > 0 for c in e_before.values())) and (e_names_gap or e_phrase_gone),
        detail={"reply": reply_e, "says_erased": e_says_erased, "names_gap": e_names_gap,
                "sidecar_before": e_before, "sidecar_after_while_reader_held": e_after_held,
                "sidecar_after_reader_closed": e_after_closed},
    )
    by_label = dry.get("by_label") or {}
    lane_evidence.record(
        "namespace_erase_covers_todos_and_artifacts",
        passed=by_label.get("Todo", 0) >= 1 and by_label.get("WorkArtifact", 0) >= 1
        and nodes_after_dry == nodes_before_ns2 and nodes_before_ns2 >= 2
        and nodes_after_real == 0 and not leftover_labels and not real.get("error"),
        detail={"dry_by_label": by_label, "real": real, "nodes_before": nodes_before_ns2,
                "nodes_after_dry": nodes_after_dry, "nodes_after_real": nodes_after_real,
                "leftover_labels": leftover_labels},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, CRITERIA)
