"""Isolated #109 prepared-scalar experiment; not a runtime storage implementation.

Uses Menhir's actual numeric fold/hash functions. The original experiment uses synthetic labels;
the source-bound variant reads production assertion/evidence labels but still writes synthetic Views.
The separate real-View candidate captures actual create/supersede and provenance query bodies and
publishes them with real work/receipt labels in one guarded command, with diagnostic string temporals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from menhir.domain.projection import (
    ProjectionAbstention,
    ProjectionMaterialization,
    ProjectionRetirement,
)
from menhir.domain.projection_lifecycle import ProjectionWorkToken
from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import normalize_scalar
from menhir.infrastructure.projection_lifecycle_repository_encoding import (
    _receipt_key,
    _target_json,
    _work_key,
)
from menhir.services.scalar_projection_definition import (
    SCALAR_STATE_PROJECTION,
    scalar_projection_target,
)
from menhir.services.scalar_projection_hash import (
    scalar_projection_absent_hash,
    scalar_projection_present_hash,
)

MAX_INPUTS = 32
MAX_AGE_MS = 30_000
SNAPSHOT = """
OPTIONAL MATCH (d:PreparedDefinition {definition_id:$definition_id})
WITH d LIMIT 2
WITH collect(properties(d)) AS definitions
OPTIONAL MATCH (w:PreparedWork {work_key:$work_key})
WITH definitions, w LIMIT 2
WITH definitions, collect(w) AS work_nodes
WITH definitions, work_nodes, [w IN work_nodes | properties(w)] AS works
OPTIONAL MATCH (a:PreparedAssertion {namespace:$namespace, subject_uuid:$subject})
WITH definitions, work_nodes, works, a ORDER BY a.assertion_id LIMIT $snapshot_input_limit
WITH definitions, work_nodes, works, collect(properties(a)) AS inputs
OPTIONAL MATCH (v:PreparedView {work_key:$work_key})
WITH definitions, work_nodes, works, inputs, v LIMIT 2
WITH definitions, work_nodes, works, inputs, collect(properties(v)) AS views
OPTIONAL MATCH (r:PreparedReceipt {receipt_key:$receipt_key})
WITH definitions, work_nodes, works, inputs, views, r LIMIT 2
WITH definitions, work_nodes, works, inputs, views, collect(properties(r)) AS receipts,
     timestamp() AS clock
"""
READ = SNAPSHOT + "RETURN definitions, works, inputs, views, receipts, clock"
COMMIT = (
    SNAPSHOT
    + """
WITH work_nodes, CASE
    WHEN size(definitions)<>1 OR size(works)<>1 OR size(views)>1 OR size(receipts)>1 THEN 'corrupt'
    WHEN clock<$as_of_ms OR clock>=$valid_until_ms THEN 'expired'
    WHEN definitions<>$definitions OR inputs<>$inputs THEN 'stale_inputs'
    WHEN works=[$after_work] AND views=[$after_view] AND receipts=[$after_receipt] THEN 'replay'
    WHEN works<>$works THEN 'stale_work'
    WHEN views<>$views THEN 'stale_view'
    WHEN receipts<>$receipts THEN 'receipt_conflict'
    ELSE 'apply' END AS action
FOREACH (w IN CASE WHEN action='apply' THEN work_nodes ELSE [] END |
    SET w=$after_work
    MERGE (v:PreparedView {work_key:$work_key}) SET v=$after_view
    CREATE (r:PreparedReceipt) SET r=$after_receipt
)

RETURN action
"""
)

# Keep both variants' state/receipt/time guards identical. Only their source snapshot differs.
# Node/edge identities distinguish duplicate nodes and parallel edges, even with equal properties.
_SOURCE_INPUTS = """
OPTIONAL MATCH (f:EvidenceNamespaceFence {namespace_key:$namespace})
WITH definitions, work_nodes, works, f LIMIT 2
WITH definitions, work_nodes, works, collect(properties(f)) AS fences
OPTIONAL MATCH (a:TypedAssertion {subject_uuid:$subject})
WHERE coalesce(a.namespace, a.group_id, 'default')=$namespace
WITH definitions, work_nodes, works, fences, a ORDER BY a.assertion_id, id(a) LIMIT $snapshot_input_limit
OPTIONAL MATCH (t:TurnEvidence {turn_id:a.episode_uuid})
OPTIONAL MATCH (e:Episodic)-[r:ADMITTED_ON]->(t)
OPTIONAL MATCH (e)-[g:ADMITTED_ON]->(grounding:TurnEvidence)
OPTIONAL MATCH (direct:TurnEvidence {turn_id:e.uuid})
OPTIONAL MATCH (alias:Episodic {uuid:e.uuid})
OPTIONAL MATCH (collision:Episodic {uuid:t.turn_id})
WITH definitions, work_nodes, works, fences, a, t, e, r, g, grounding, direct, alias, collision
ORDER BY a.assertion_id, id(a), id(t), id(e), id(r), id(g), id(direct), id(alias), id(collision)
LIMIT $snapshot_input_limit
WITH definitions, work_nodes, works, fences,
     collect({assertion:properties(a), assertion_node:id(a), turn:properties(t), turn_node:id(t),
              episode:properties(e), episode_node:id(e), admission_node:id(r),
              grounding:properties(grounding), grounding_node:id(grounding), grounding_edge:id(g),
              direct_node:id(direct), alias_node:id(alias), collision_node:id(collision)}) AS sources
WITH definitions, work_nodes, works,
     {sources:[s IN sources WHERE s.assertion IS NOT NULL], fences:fences} AS inputs
"""
PRODUCTION_SNAPSHOT = (
    SNAPSHOT[: SNAPSHOT.index("OPTIONAL MATCH (a:PreparedAssertion")]
    + _SOURCE_INPUTS
    + SNAPSHOT[SNAPSHOT.index("OPTIONAL MATCH (v:PreparedView") :]
)
PRODUCTION_READ = PRODUCTION_SNAPSHOT + "RETURN definitions, works, inputs, views, receipts, clock"
# Nested map equality can yield null in this engine when a record contains null identities,
# even when a later component differs. Compare every component with explicit null handling.
_SOURCE_EQUAL = (
    "size(inputs.sources)=size($inputs.sources) AND coalesce(inputs.fences=$inputs.fences,false) AND "
    "all(i IN range(0,size(inputs.sources)-1) WHERE "
    + " AND ".join(
        f"coalesce(coalesce(inputs.sources[i].{name},{{}})=coalesce($inputs.sources[i].{name},{{}}),false)"
        for name in ("assertion", "turn", "episode", "grounding")
    )
    + " AND "
    + " AND ".join(
        f"coalesce(inputs.sources[i].{name},-1)=coalesce($inputs.sources[i].{name},-1)"
        for name in (
            "assertion_node",
            "turn_node",
            "episode_node",
            "admission_node",
            "grounding_node",
            "grounding_edge",
            "direct_node",
            "alias_node",
            "collision_node",
        )
    )
    + ")"
)
PRODUCTION_COMMIT = PRODUCTION_SNAPSHOT + COMMIT[len(SNAPSHOT) :].replace("inputs<>$inputs", f"NOT ({_SOURCE_EQUAL})")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _keys(token: ProjectionWorkToken) -> dict[str, object]:
    _require(token.definition_id == SCALAR_STATE_PROJECTION.definition_id, "unsupported definition")
    _require(token.definition_version == SCALAR_STATE_PROJECTION.version, "unsupported definition version")
    _require(bool(token.target.namespace), "proof requires an explicit namespace")
    _require(
        len(token.target.key) == 4 and token.target.key[2] == "count", "proof supports numeric scalar targets only"
    )
    _require(token.work_key == _work_key(token.definition_id, token.target), "work key/target mismatch")
    return {
        "definition_id": token.definition_id,
        "work_key": token.work_key,
        "namespace": token.target.namespace,
        "subject": token.target.subject_id,
        "receipt_key": _receipt_key(token.work_key, token.generation),
        "snapshot_input_limit": MAX_INPUTS + 1,
    }


@dataclass(frozen=True)
class PreparedScalar:
    """An immutable source snapshot; all payload/certificate fields are derived again at dispatch."""

    token: ProjectionWorkToken
    derivation_id: str
    snapshot_json: str

    def parameters(self) -> dict[str, object]:
        keys = _keys(self.token)
        _require(bool(self.derivation_id.strip()), "blank derivation id")
        data = json.loads(self.snapshot_json)
        definitions, works, inputs, views, receipts, clock = data
        _require(len(definitions) == len(works) == 1, "missing/duplicate definition or work")
        _require(len(views) <= 1 and not receipts, "duplicate View or existing receipt")
        _require(
            all(
                v.get("namespace") == self.token.target.namespace
                and v.get("subject_uuid") == self.token.target.subject_id
                for v in views
            ),
            "foreign View",
        )
        _require(len(inputs) <= MAX_INPUTS, "input bound exceeded")
        _require(len({r["assertion_id"] for r in inputs}) == len(inputs), "duplicate assertion identity")
        definition, work = definitions[0], works[0]
        _require(definition.get("current_version") == self.token.definition_version, "stale definition")
        _require(bool(definition.get("semantic_hash")), "missing definition hash")
        expected = {
            "definition_id": self.token.definition_id,
            "definition_version": self.token.definition_version,
            "target_json": _target_json(self.token.target),
            "generation": self.token.generation,
            "target_present": self.token.target_present,
        }
        _require(all(work.get(k) == v for k, v in expected.items()), "stale/foreign work token")
        _require(work.get("applied_generation", 0) < self.token.generation, "generation already applied")
        _require(isinstance(clock, int) and not isinstance(clock, bool), "invalid database clock")
        _require(
            all(
                r.get("namespace") == self.token.target.namespace
                and r.get("subject_uuid") == self.token.target.subject_id
                for r in inputs
            ),
            "foreign input",
        )
        as_of = datetime.fromtimestamp(clock / 1000, tz=UTC)
        deadlines = [clock + MAX_AGE_MS]
        for row in inputs:
            when = parse_iso8601(row.get("valid_at"))
            if when is not None and when > as_of:
                deadlines.append(int(when.timestamp() * 1000))
        _require(min(deadlines) > clock, "no valid preparation interval")
        rows = tuple(row for row in inputs if scalar_projection_target(row) == self.token.target)
        outcome = SCALAR_STATE_PROJECTION.fold(self.token.target, rows, as_of) if self.token.target_present else None
        present = isinstance(outcome, ProjectionMaterialization)
        if present:
            state = outcome.payload
            payload = {
                "value": str(state.value),
                "valid_at": state.valid_at,
                "contributor_ids": sorted(state.contributor_ids),
                "effective_tier": state.effective_tier,
                "episode_uuids": sorted(state.episode_uuids),
            }
            digest = scalar_projection_present_hash(SCALAR_STATE_PROJECTION, self.token.target, **payload)
        elif not self.token.target_present or isinstance(outcome, (ProjectionAbstention, ProjectionRetirement)):
            payload = {"present": False}
            digest = scalar_projection_absent_hash(SCALAR_STATE_PROJECTION, self.token.target)
        else:
            raise TypeError("unsupported scalar fold outcome")
        _require(
            isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
            "invalid prepared projection hash",
        )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        after_view = {
            "work_key": self.token.work_key,
            "namespace": self.token.target.namespace,
            "subject_uuid": self.token.target.subject_id,
            "payload_json": encoded,
            "present": present,
            "projection_hash": digest,
        }
        after_work = {
            **work,
            "applied_generation": self.token.generation,
            "applied_definition_version": self.token.definition_version,
            "applied_projection_hash": digest,
            "certified_definition_version": self.token.definition_version,
            "certified_projection_hash": digest,
            "certified_derivation_id": self.derivation_id,
            "certified_generation": self.token.generation,
            "certified_receipt_key": keys["receipt_key"],
            "completed_at": as_of.isoformat(),
            "certified_at": as_of.isoformat(),
        }
        after_receipt = {
            **expected,
            "work_key": self.token.work_key,
            "receipt_key": keys["receipt_key"],
            "derivation_id": self.derivation_id,
            "projection_hash": digest,
            "created_at": as_of.isoformat(),
        }
        return {
            **keys,
            "definitions": definitions,
            "works": works,
            "inputs": inputs,
            "views": views,
            "receipts": receipts,
            "after_work": after_work,
            "after_view": after_view,
            "after_receipt": after_receipt,
            "as_of_ms": clock,
            "valid_until_ms": min(deadlines),
        }


def prepare(graph: Any, token: ProjectionWorkToken, derivation_id: str) -> PreparedScalar:
    result = graph.query(READ, _keys(token)).result_set
    _require(len(result) == 1, "snapshot did not return one row")
    plan = PreparedScalar(token, derivation_id, json.dumps(result[0], sort_keys=True))
    plan.parameters()  # Reject malformed/incomplete inputs before any mutation.
    return plan


def commit(graph: Any, plan: PreparedScalar) -> str:
    result = graph.query(COMMIT, plan.parameters()).result_set
    _require(len(result) == 1 and len(result[0]) == 1, "invalid commit result")
    return str(result[0][0])


def _production_inputs(bundle: dict[str, Any], token: ProjectionWorkToken) -> list[dict[str, Any]]:
    """Hydrate the restricted production read from the SAME snapshot that commit revalidates.

    This profile deliberately refuses legacy/default-silo and ambiguous evidence rather than
    silently picking one source. It does not replace the production repository's broader API.
    """
    from menhir.infrastructure.typed_assertion_repository import (
        TypedAssertionRepository,
    )

    _require(token.target.namespace not in (None, "", "default"), "source proof requires a nondefault namespace")
    sources, fences = bundle["sources"], bundle["fences"]
    _require(len(sources) <= MAX_INPUTS, "source closure bound exceeded")
    _require(len(fences) == 1, "missing/duplicate evidence fence")
    generation = fences[0].get("generation")
    _require(type(generation) is int and generation >= 0, "invalid evidence fence generation")
    grouped: dict[int, list[dict[str, Any]]] = {}
    for source in sources:
        grouped.setdefault(source["assertion_node"], []).append(source)
    assertion_ids: set[str] = set()
    inputs = []
    for records in grouped.values():
        assertion = records[0]["assertion"]
        assertion_id = assertion.get("assertion_id")
        _require(isinstance(assertion_id, str) and bool(assertion_id), "missing source assertion identity")
        _require(assertion_id not in assertion_ids, "duplicate source assertion identity")
        assertion_ids.add(assertion_id)
        _require(
            all(
                assertion.get(name) is None or type(assertion[name]) is bool
                for name in ("superseded", "binding_pending")
            ),
            "invalid source eligibility flag",
        )
        if assertion.get("superseded", False) or assertion.get("binding_pending", False):
            continue
        _require(len(records) == 1, "ambiguous source provenance")
        source = records[0]
        turn, episode = source["turn"], source["episode"]
        _require(turn is not None and episode is not None, "source proof requires turn-backed admission")
        _require(
            source["grounding_node"] == source["turn_node"]
            and source["grounding_edge"] == source["admission_node"]
            and source["alias_node"] == source["episode_node"]
            and source["direct_node"] is None
            and source["collision_node"] is None,
            "ambiguous source evidence identity",
        )
        for node in (assertion, turn, episode):
            namespace = node.get("namespace")
            if namespace is None:
                namespace = node.get("group_id", "default")
            _require(namespace == token.target.namespace, "foreign source evidence")
        _require(assertion.get("subject_uuid") == token.target.subject_id, "foreign source subject")
        _require(bool(turn.get("turn_id")) and bool(episode.get("uuid")), "missing source evidence identity")
        _require(
            all(
                node.get(name) is None or type(node[name]) is bool
                for node in (turn, episode)
                for name in ("evidence_finalized", "evidence_quarantined")
            ),
            "invalid source evidence flag",
        )
        _require(
            turn.get("evidence_finalized") is True and not turn.get("evidence_quarantined", False),
            "source evidence is not live",
        )
        evidence_generation = turn.get("evidence_generation")
        if evidence_generation is None:
            evidence_generation = turn.get("publication_generation")
        _require(type(evidence_generation) is int and evidence_generation == generation, "stale source evidence fence")
        _require(
            not episode.get("evidence_finalized", False) and not episode.get("evidence_quarantined", False),
            "source proof requires an unfinalized episode bridge",
        )
        row = TypedAssertionRepository._hydrate(dict(assertion))
        row.update(
            episode_uuid=episode["uuid"],
            source_episode_uuid=episode["uuid"],
            grounding_id=assertion.get("episode_uuid"),
            turn_id=turn["turn_id"],
            absolute_semantics=assertion.get("absolute_semantics")
            if assertion.get("absolute_semantics") is not None
            else "ordinary",
        )
        for name in ("valid_at", "learned_at"):
            _require(row.get(name) is None or isinstance(row[name], str), "source proof requires string timestamps")
        for name in (
            "importance",
            "count_object",
            "count_item",
            "count_action",
            "count_status",
            "count_target_id",
            "count_family",
        ):
            if row.get(name) is None:
                row[name] = ""
        inputs.append(row)
    return inputs


@dataclass(frozen=True)
class PreparedProductionScalar(PreparedScalar):
    """Source-bound variant; output/history storage is still experimental and synthetic."""

    def parameters(self) -> dict[str, object]:
        data = json.loads(self.snapshot_json)
        bundle = data[2]
        data[2] = _production_inputs(bundle, self.token)
        parameters = PreparedScalar(self.token, self.derivation_id, json.dumps(data)).parameters()
        return {**parameters, "inputs": bundle}


def prepare_production(graph: Any, token: ProjectionWorkToken, derivation_id: str) -> PreparedProductionScalar:
    result = graph.query(PRODUCTION_READ, _keys(token)).result_set
    _require(len(result) == 1, "source snapshot did not return one row")
    plan = PreparedProductionScalar(token, derivation_id, json.dumps(result[0], sort_keys=True))
    plan.parameters()
    return plan


def commit_production(graph: Any, plan: PreparedProductionScalar) -> str:
    result = graph.query(PRODUCTION_COMMIT, plan.parameters()).result_set
    _require(len(result) == 1 and len(result[0]) == 1, "invalid source commit result")
    return str(result[0][0])


# A separate, deliberately restricted real-View candidate. No runtime adapter uses it.
REAL_VIEW_SNAPSHOT = (
    PRODUCTION_SNAPSHOT[: PRODUCTION_SNAPSHOT.index("OPTIONAL MATCH (v:PreparedView")]
    .replace("PreparedDefinition", "ProjectionDefinitionState")
    .replace("PreparedWork", "ProjectionWorkState")
    .replace(
        "collect(properties(f)) AS fences",
        "collect(CASE WHEN f IS NULL THEN null ELSE "
        "{namespace_key:f.namespace_key,generation:f.generation,node:id(f)} END) AS fences",
    )
    + """
OPTIONAL MATCH (v:Entity {view_key:$real_view_key})
WITH definitions, work_nodes, works, inputs, v ORDER BY id(v) LIMIT $snapshot_input_limit
OPTIONAL MATCH (v)-[link]-(other)
WITH definitions, work_nodes, works, inputs, v, link, other ORDER BY id(v), id(link)
WITH definitions, work_nodes, works, inputs, v,
     collect(CASE WHEN link IS NULL THEN null ELSE
       {node:id(link),kind:type(link),source:id(startNode(link)),target:id(endNode(link)),
        props:properties(link)} END) AS links
WITH definitions, work_nodes, works, inputs,
     collect(CASE WHEN v IS NULL THEN null ELSE {node:id(v),props:properties(v),links:links} END) AS views
OPTIONAL MATCH (r:ProjectionDerivationReceipt {receipt_key:$receipt_key})
WITH definitions, work_nodes, works, inputs, views, r LIMIT 2
WITH definitions, work_nodes, works, inputs, views, collect(properties(r)) AS receipts,
     timestamp() AS clock
"""
)
REAL_VIEW_READ = REAL_VIEW_SNAPSHOT + "RETURN definitions, works, inputs, views, receipts, clock"


@dataclass(frozen=True)
class PreparedRealViewScalar(PreparedScalar):
    """Count create/supersede proof using the actual writer; string temporals only.

    Refresh, retirement and general callback translation are intentionally refused. Captured write
    bodies are trusted experiment code, not an input format exposed to callers or persisted jobs.
    """

    view_key: str
    steps_json: str
    anchors: tuple[str, ...]

    def parameters(self) -> dict[str, object]:
        data = json.loads(self.snapshot_json)
        history = data[3]
        _require(len(history) <= MAX_INPUTS, "View history bound exceeded")
        _require(self.token.target_present, "real View proof does not support retirement")
        _require(
            all(
                v["props"].get("view_subject_uuid") == self.token.target.subject_id
                and v["props"].get("namespace") == self.token.target.namespace
                and v["props"].get("view_kind") == "scalar_state"
                for v in history
            ),
            "foreign View history",
        )
        data[3] = []  # The base candidate's synthetic View checks do not describe real history.
        base = PreparedProductionScalar(self.token, self.derivation_id, json.dumps(data)).parameters()
        rows = _production_inputs(data[2], self.token)
        outcome = SCALAR_STATE_PROJECTION.fold(
            self.token.target,
            tuple(r for r in rows if scalar_projection_target(r) == self.token.target),
            datetime.fromtimestamp(base["as_of_ms"] / 1000, tz=UTC),
        )
        _require(isinstance(outcome, ProjectionMaterialization), "real View proof requires a present fold")
        state = outcome.payload
        bridge = {
            s["episode"]["uuid"]: s["turn"]["turn_id"]
            for s in data[2]["sources"]
            if s["episode"] is not None and s["turn"] is not None
        }
        anchors = tuple(sorted({bridge[e] for e in state.episode_uuids}))
        _require(anchors == self.anchors, "resolved anchors disagree with source snapshot")
        digest = scalar_projection_present_hash(
            SCALAR_STATE_PROJECTION,
            self.token.target,
            value=state.value,
            valid_at=state.valid_at,
            contributor_ids=state.contributor_ids,
            effective_tier=state.effective_tier,
            episode_uuids=anchors,
        )
        after_work = dict(base["after_work"])
        after_work.update(applied_projection_hash=digest, certified_projection_hash=digest)
        after_receipt = {**base["after_receipt"], "projection_hash": digest}
        steps = json.loads(self.steps_json)
        _require(len(steps) == 2, "unsupported real View write trace")
        _require("CREATE (n:Entity" in steps[0][0], "real View proof supports create/supersede only")
        _require("RETURN ca, cd, cs" in steps[1][0], "missing real scalar provenance rewrite")
        return {
            **base,
            "real_view_key": self.view_key,
            "views": history,
            "after_work": after_work,
            "after_receipt": after_receipt,
            "expected_value": normalize_scalar(state.value),
            "expected_valid_at": state.valid_at,
            "expected_contributors": sorted(state.contributor_ids),
            "expected_tier": state.effective_tier,
            "expected_anchors": list(anchors),
            "expected_history": len(history) + 1,
            "expected_uuid": steps[0][1]["uuid"],
            "expected_ca": 1 if state.anchor_id else 0,
            "expected_cd": len(state.contributed_delta_ids),
            "expected_cs": len(state.superseded_anchor_ids),
            "expected_anchor_ids": [state.anchor_id] if state.anchor_id else [],
            "expected_delta_ids": list(state.contributed_delta_ids),
            "expected_superseded_ids": list(state.superseded_anchor_ids),
            "expected_old": steps[0][1].get("old"),
            **{f"view_step{i}": step[1] for i, step in enumerate(steps)},
        }


def prepare_real_view(graph: Any, token: ProjectionWorkToken, derivation_id: str) -> PreparedRealViewScalar:
    import re

    from falkordblite_projection_proof import AutocommitProbeRepository

    from menhir.infrastructure.scalar_projection_materializer import (
        ScalarStateProjectionMaterializer,
    )
    from menhir.infrastructure.view_repository import ViewRepository

    kind = ViewRepository.KINDS["scalar_state"]
    attribute, scope, value_kind, unit = token.target.key
    key = ViewRepository._key(
        token.target.namespace,
        "",
        kind.key_discriminator({"attribute": attribute, "scope": scope, "value_kind": value_kind, "unit": unit}),
        subject_uuid=token.target.subject_id,
    )
    keys = {**_keys(token), "real_view_key": key}
    snapshot = graph.query(REAL_VIEW_READ, keys).result_set
    _require(len(snapshot) == 1, "invalid real View snapshot")
    data = snapshot[0]
    rows = _production_inputs(data[2], token)
    outcome = SCALAR_STATE_PROJECTION.fold(
        token.target,
        tuple(r for r in rows if scalar_projection_target(r) == token.target),
        datetime.fromtimestamp(data[5] / 1000, tz=UTC),
    )
    _require(token.target_present and isinstance(outcome, ProjectionMaterialization), "present fold required")
    steps: list[tuple[str, dict[str, Any]]] = []

    class Capture(AutocommitProbeRepository):
        def execute(self, query: str, params: dict[str, Any] | None = None, **_kwargs: Any):
            parameters = dict(params or {})
            # Only the exact two known mutation shapes are captured. Other mutations fail closed.
            if "CREATE (n:Entity" in query:
                steps.append((query, parameters))
                return [{"uuid": parameters["uuid"]}]
            if "RETURN ca, cd, cs" in query:
                steps.append((query, parameters))
                return [{"ca": 0, "cd": 0, "cs": 0}]
            if re.search(r"\b(?:CREATE|MERGE|SET|DELETE|REMOVE)\b", query):
                raise ValueError("unsupported real View mutation (refresh/retirement not qualified)")
            return super().execute(re.sub(r"datetime\((\$\w+)\)", r"\1", query), parameters)

    anchors = ScalarStateProjectionMaterializer()._materialize_state(
        ViewRepository(Capture(graph)), outcome.payload, token
    )
    # A changed preflight read cannot authorize writes from a mixed source/View snapshot.
    after = graph.query(REAL_VIEW_READ, keys).result_set
    _require(len(after) == 1 and after[0][:5] == data[:5], "source/View changed during preparation")
    plan = PreparedRealViewScalar(
        token,
        derivation_id,
        json.dumps(data, sort_keys=True),
        key,
        json.dumps(steps, sort_keys=True),
        tuple(sorted(anchors)),
    )
    plan.parameters()
    return plan


def real_view_statement(plan: PreparedRealViewScalar) -> tuple[str, dict[str, object]]:
    import re

    from falkordblite_projection_proof import _bind_structure_query

    parameters = plan.parameters()
    parts = [
        REAL_VIEW_SNAPSHOT
        + f"""
WITH work_nodes, CASE
 WHEN size(definitions)<>1 OR size(works)<>1 OR size(receipts)>1 THEN 'corrupt'
 WHEN clock<$as_of_ms OR clock>=$valid_until_ms THEN 'expired'
 WHEN definitions<>$definitions OR NOT ({_SOURCE_EQUAL}) THEN 'stale_inputs'
 WHEN works=[$after_work] AND receipts=[$after_receipt] THEN 'replay'
 WHEN works<>$works THEN 'stale_work'
 WHEN NOT coalesce(views=$views,false) THEN 'stale_view'
 WHEN receipts<>$receipts THEN 'receipt_conflict'
 ELSE 'apply' END AS action
WITH work_nodes, action, true AS stage_initial
"""
    ]
    previous = "stage_initial"
    for i, (query, params) in enumerate(json.loads(plan.steps_json)):
        # Diagnostic strings only; the existing binder preserves literals/comments/parameter names.
        query = re.sub(r"datetime\((\$\w+)\)", r"\1", query)
        body = _bind_structure_query(query, params, f"view_step{i}")
        returned = "uuid" if i == 0 else "{ca:ca,cd:cd,cs:cs}"
        expected = "rows=[$expected_uuid]" if i == 0 else ("rows=[{ca:$expected_ca,cd:$expected_cd,cs:$expected_cs}]")
        parts.append(f"""
CALL {{ WITH action, {previous}
 CALL {{ WITH action, {previous} WITH action, {previous} WHERE action='apply' AND {previous} {body} }}
 RETURN collect({returned}) AS rows
}}
WITH work_nodes, action, Menhir109Guard.requireOk(
 {previous} AND (action<>'apply' OR coalesce({expected},false)), 'real View writer refused') AS stage{i}
""")
        previous = f"stage{i}"
    parts.append("""
OPTIONAL MATCH (v:Entity {view_key:$real_view_key})
WITH work_nodes, action, stage1, collect(v) AS history
WITH work_nodes, action, stage1, history, [v IN history WHERE coalesce(v.view_current,true)] AS current
CALL {
 WITH current WITH head(current) AS head
 OPTIONAL MATCH (e)-[:MENTIONS]->(head)
 RETURN collect(coalesce(e.uuid,e.turn_id)) AS mentions
}
CALL {
 WITH current WITH head(current) AS head
 OPTIONAL MATCH (head)-[:CURRENT_ANCHOR]->(a)
 RETURN collect(a.assertion_id) AS anchors
}
CALL {
 WITH current WITH head(current) AS head
 OPTIONAL MATCH (head)-[:CONTRIBUTED_TO]->(a)
 RETURN collect(a.assertion_id) AS deltas
}
CALL {
 WITH current WITH head(current) AS head
 OPTIONAL MATCH (head)-[:SUPERSEDED_ANCHOR]->(a)
 RETURN collect(a.assertion_id) AS excluded
}
CALL {
 WITH current WITH head(current) AS head
 OPTIONAL MATCH (head)-[:SUPERSEDES]->(old)
 RETURN collect(CASE WHEN old IS NULL THEN null ELSE
  {uuid:old.uuid,current:old.view_current,marker:coalesce(old.ss_view_key_current,'')} END) AS prior
}
WITH work_nodes, action, Menhir109Guard.requireOk(
 stage1 AND (NOT action IN ['apply','replay'] OR
 (size(history)=$expected_history AND size(current)=1 AND
  current[0].uuid=$expected_uuid AND current[0].ss_value=$expected_value AND
  current[0].namespace=$namespace AND current[0].view_subject_uuid=$subject AND
  current[0].view_kind='scalar_state' AND current[0].ss_view_key_current=$real_view_key AND
  NOT coalesce(current[0].retired,false) AND
  current[0].valid_at=$expected_valid_at AND current[0].scalar_contributors=$expected_contributors AND
  current[0].scalar_effective_tier=$expected_tier AND current[0].episode_uuids=$expected_anchors AND
  size(mentions)=size($expected_anchors) AND all(e IN $expected_anchors WHERE e IN mentions) AND
  size(anchors)=size($expected_anchor_ids) AND all(a IN $expected_anchor_ids WHERE a IN anchors) AND
  size(deltas)=size($expected_delta_ids) AND all(a IN $expected_delta_ids WHERE a IN deltas) AND
  size(excluded)=size($expected_superseded_ids) AND all(a IN $expected_superseded_ids WHERE a IN excluded) AND
  (($expected_old IS NULL AND size(prior)=0) OR
   prior=[{uuid:$expected_old,current:false,marker:''}]))),
 'real View postcondition failed') AS certified
FOREACH (w IN CASE WHEN action='apply' AND certified THEN work_nodes ELSE [] END |
 SET w=$after_work
 CREATE (r:ProjectionDerivationReceipt) SET r=$after_receipt
)
RETURN action
""")
    return "\n".join(parts), parameters


def commit_real_view(graph: Any, plan: PreparedRealViewScalar) -> str:
    query, parameters = real_view_statement(plan)
    result = graph.query(query, parameters).result_set
    _require(len(result) == 1 and len(result[0]) == 1, "invalid real View commit result")
    return str(result[0][0])
