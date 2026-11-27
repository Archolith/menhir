"""Synthetic host example for Menhir's experimental extension entry point.

Register EXTENSION with menhir.api.server.create_app(extensions=(EXTENSION,)).
In the host's writable maintenance runtime, construct DecisionTimeline with
the runtime graph and a ViewRepository from the registered extension set.
Call initialize once before putting synthetic Decision records.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from menhir.domain.namespace import namespace_group_id_error, tenant_scope_cypher, tenant_scope_params
from menhir.domain.projection import (
    ProjectionDefinition,
    ProjectionMaterialization,
    ProjectionTarget,
    evaluate_projection,
)
from menhir.domain.projection_lifecycle import ProjectionWorkToken
from menhir.extensions import ProjectionExtension
from menhir.infrastructure.neo4j import Neo4jRepository, Neo4jTransaction
from menhir.infrastructure.projection_lifecycle_repository import ProjectionLifecycleRepository
from menhir.infrastructure.view_repository import ViewKind, ViewRepository

_KIND = "project_decision_timeline"


@dataclass(frozen=True)
class Decision:
    decision_id: str
    when: date
    decision: str
    source: str


@dataclass(frozen=True)
class _ScopedDecision:
    target: ProjectionTarget
    value: Decision


def _entries_json(entries: object) -> str:
    return json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DecisionTimelineKind(ViewKind):
    name = _KIND
    keyword_indexed = False
    read_fields = "n.view_subject AS project, n.view_payload AS entries"

    def key_discriminator(self, payload: dict[str, Any]) -> str:
        return self.name

    def signature(self, payload: dict[str, Any]) -> str:
        return hashlib.sha256(_entries_json(payload["entries"]).encode()).hexdigest()

    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        return f"{subject} decisions", f"{len(payload['entries'])} recorded decisions"

    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        entries = payload["entries"]
        return {"view_payload": _entries_json(entries), "view_value": len(entries)}

    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"project": row["project"], "entries": json.loads(row["entries"])}


def _target(namespace: str, project: str) -> ProjectionTarget:
    if not isinstance(namespace, str) or not namespace.strip() or namespace_group_id_error(namespace):
        raise ValueError("a valid explicit namespace is required")
    if not re.fullmatch(r"[a-z0-9_-]+", project):
        raise ValueError("project must be a lowercase slug")
    return ProjectionTarget(subject_id=project, namespace=namespace)


def _fold(target: ProjectionTarget, rows: tuple[object, ...], _as_of: datetime | None) -> ProjectionMaterialization:
    scoped = sorted(
        (_as_decision(row) for row in rows), key=lambda row: (row.value.when, row.value.decision_id),
    )
    entries = [
        {"id": value.decision_id, "when": value.when.isoformat(), "decision": value.decision, "source": value.source}
        for value in (row.value for row in scoped)
    ]
    return ProjectionMaterialization(target, _KIND, entries, tuple(_decision_id(row) for row in scoped))


def _as_decision(row: object) -> _ScopedDecision:
    if not isinstance(row, _ScopedDecision):
        raise TypeError("the timeline accepts scoped decision records")
    return row


def _decision_id(row: object) -> str:
    scoped = _as_decision(row)
    return _entries_json([scoped.target.namespace, scoped.target.subject_id, scoped.value.decision_id])


DEFINITION = ProjectionDefinition(
    definition_id="example.project_decisions",
    version=1,
    input_assertion_types=frozenset({"project_decision"}),
    output_view_kind=_KIND,
    assertion_id_resolver=_decision_id,
    assertion_type_resolver=lambda row: "project_decision" if isinstance(row, _ScopedDecision) else "unknown",
    target_resolver=lambda row: _as_decision(row).target,
    fold=_fold,
)
VIEW_KIND = DecisionTimelineKind()


def _remove_views(tx: Neo4jTransaction, target: ProjectionTarget) -> None:
    tx.execute(
        f"""
        MATCH (n:Entity {{is_view:true, view_kind:$kind, view_subject:$project}})
        WHERE {tenant_scope_cypher("n")}
        DETACH DELETE n
        """,
        {"kind": _KIND, "project": target.subject_id, **tenant_scope_params(target.namespace)},
    )


def _materialize(views: ViewRepository, token: ProjectionWorkToken) -> str:
    target = _target(token.target.namespace, token.target.subject_id)
    if target != token.target:
        raise ValueError("decision timeline only accepts one slot per project")
    rows = views.neo4j.execute(
        """
        MATCH (n:ExampleProjectDecision {namespace:$namespace, project:$project})
        RETURN n.decision_id AS decision_id, n.when AS when, n.decision AS decision, n.source AS source
        """,
        {"namespace": target.namespace, "project": target.subject_id},
    ) if token.target_present else []
    decisions = tuple(
        _ScopedDecision(
            target, Decision(row["decision_id"], date.fromisoformat(row["when"]), row["decision"], row["source"]),
        )
        for row in rows
    )
    if not decisions:
        _remove_views(views.neo4j, target)
        return hashlib.sha256(b"absent").hexdigest()
    (outcome,) = evaluate_projection(DEFINITION, decisions)
    if not isinstance(outcome, ProjectionMaterialization):
        raise ValueError("unexpected decision timeline outcome")
    views.record(_KIND, subject=target.subject_id, namespace=target.namespace, entries=outcome.payload)
    current = views.fetch_current(_KIND, subject=target.subject_id, namespace=target.namespace)
    if current is None or current["entries"] != outcome.payload:
        raise RuntimeError("decision timeline materialization did not persist the fold")
    return VIEW_KIND.signature({"entries": current["entries"]})


EXTENSION = ProjectionExtension(DEFINITION, VIEW_KIND, _materialize)


class DecisionTimeline:
    """Synthetic record producer and current View reader for an explicitly registered host."""

    def __init__(self, graph: Neo4jRepository, views: ViewRepository) -> None:
        if views.KINDS.get(_KIND) is not VIEW_KIND:
            raise ValueError("the host must register the decision timeline extension")
        self._graph = graph
        self._views = views
        self._queue = ProjectionLifecycleRepository(graph)

    def initialize(self) -> None:
        self._queue.activate()
        self._queue.publish_definition(DEFINITION, semantic_hash="example-project-decisions-v1")

    def put(self, *, namespace: str, project: str, decision: Decision) -> None:
        target = _target(namespace, project)
        if not all(value.strip() for value in (decision.decision_id, decision.decision, decision.source)):
            raise ValueError("decision id, text, and source are required")
        if not isinstance(decision.when, date) or isinstance(decision.when, datetime):
            raise ValueError("decision date must be a calendar date")

        def work(tx: Neo4jTransaction) -> None:
            self._queue.dirty_target_in_transaction(tx, DEFINITION, target, reason="decision_changed")
            tx.execute(
                """
                MERGE (n:ExampleProjectDecision {namespace:$namespace, project:$project, decision_id:$id})
                SET n.when=$when, n.decision=$decision, n.source=$source
                """,
                {"namespace": namespace, "project": project, "id": decision.decision_id,
                 "when": decision.when.isoformat(), "decision": decision.decision, "source": decision.source},
            )

        self._graph.execute_write(work)

    def erase(self, *, namespace: str, project: str, decision_id: str) -> None:
        target = _target(namespace, project)

        def work(tx: Neo4jTransaction) -> None:
            self._queue.dirty_target_in_transaction(tx, DEFINITION, target, reason="decision_erased")
            tx.execute(
                "MATCH (n:ExampleProjectDecision {namespace:$namespace, project:$project, decision_id:$id}) DELETE n",
                {"namespace": namespace, "project": project, "id": decision_id},
            )
            # Every historical version may still carry the erased record. Purge
            # the slot now, then let the queued fold restore surviving entries.
            _remove_views(tx, target)

        self._graph.execute_write(work)

    def current(self, *, namespace: str, project: str) -> dict[str, Any] | None:
        _target(namespace, project)
        return self._views.fetch_current(_KIND, subject=project, namespace=namespace)
