"""Isolated #109 prepared-scalar experiment; not a runtime storage implementation.

Uses Menhir's actual numeric fold/hash functions but synthetic graph labels. The snapshot contains
complete input properties, not the production assertion/provenance relationship closure.
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
