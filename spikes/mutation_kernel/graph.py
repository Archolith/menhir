"""Generic entity/edge contracts for the mutation-kernel research spike.

Extensions own entity identity and relationship meaning. The generic layer owns stable IDs,
source-grounded entity receipts, endpoint-kind validation, and rebuildable Neo4j relationships.
Nothing here is imported by production Menhir.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Sequence

from .kernel import EvidenceRef, build_source_key


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_parts(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _canonical_properties(properties: Sequence[tuple[str, Any]]) -> tuple[tuple[str, Any], ...]:
    normalized = tuple(
        sorted(((str(name).strip(), value) for name, value in properties), key=lambda row: row[0])
    )
    names = [name for name, _ in normalized]
    if any(not name for name in names):
        raise ValueError("property names must be non-empty")
    if len(names) != len(set(names)):
        raise ValueError("property names must be unique")
    _stable_json({name: value for name, value in normalized})
    return normalized


def _properties_dict(properties: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    return {name: value for name, value in properties}


@dataclass(frozen=True)
class EntityProposal:
    """One source-grounded request for an extension-owned entity identity resolver."""

    entity_kind: str
    display_name: str
    properties: tuple[tuple[str, Any], ...]
    source: EvidenceRef
    valid_at: str
    learned_at: str
    authority: str = "agent"
    confidence: float = 1.0
    ordinal: int = 0

    def __post_init__(self) -> None:
        if not self.entity_kind.strip() or not self.display_name.strip():
            raise ValueError("EntityProposal kind/display name must be non-empty")
        if not self.valid_at.strip() or not self.learned_at.strip():
            raise ValueError("EntityProposal valid/learned time must be non-empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("EntityProposal.confidence must be between 0 and 1")
        if self.ordinal < 0:
            raise ValueError("EntityProposal.ordinal must be >= 0")
        if self.properties != _canonical_properties(self.properties):
            raise ValueError("EntityProposal.properties must be canonical")

    @property
    def source_key(self) -> str:
        return build_source_key(
            self.source.source_id,
            span_start=self.source.span_start,
            span_end=self.source.span_end,
            ordinal=self.ordinal,
        )

    @property
    def property_map(self) -> dict[str, Any]:
        return _properties_dict(self.properties)


IdentityResolver = Callable[[EntityProposal], str]


@dataclass(frozen=True)
class EntityKind:
    kind_id: str
    version: int
    identity_resolver: IdentityResolver

    def __post_init__(self) -> None:
        if not self.kind_id.strip():
            raise ValueError("EntityKind.kind_id must be non-empty")
        if self.version < 1:
            raise ValueError("EntityKind.version must be >= 1")

    def resolve(self, proposal: EntityProposal) -> "ResolvedEntity":
        if proposal.entity_kind != self.kind_id:
            raise ValueError("entity proposal kind mismatch")
        canonical_key = str(self.identity_resolver(proposal)).strip()
        if not canonical_key:
            raise ValueError("entity identity resolver returned an empty canonical key")
        return ResolvedEntity(
            entity_kind=self.kind_id,
            entity_kind_version=self.version,
            entity_id=_hash_parts(self.kind_id, canonical_key),
            canonical_key=canonical_key,
            display_name=proposal.display_name,
        )


@dataclass(frozen=True)
class ResolvedEntity:
    entity_kind: str
    entity_kind_version: int
    entity_id: str
    canonical_key: str
    display_name: str


@dataclass(frozen=True)
class EdgeKind:
    kind_id: str
    version: int
    source_kinds: tuple[str, ...]
    target_kinds: tuple[str, ...]
    temporal: bool = True
    provenance_required: bool = True

    def __post_init__(self) -> None:
        if not self.kind_id.strip() or self.version < 1:
            raise ValueError("EdgeKind requires a non-empty ID and version >= 1")
        source = tuple(sorted(str(value).strip() for value in self.source_kinds))
        target = tuple(sorted(str(value).strip() for value in self.target_kinds))
        if not source or not target or any(not value for value in (*source, *target)):
            raise ValueError("EdgeKind requires non-empty source and target kind sets")
        if len(source) != len(set(source)) or len(target) != len(set(target)):
            raise ValueError("EdgeKind source/target kinds must be unique")
        if self.source_kinds != source or self.target_kinds != target:
            raise ValueError("EdgeKind source/target kinds must be canonical")


@dataclass(frozen=True)
class MaterializedEdge:
    edge_kind: str
    source_entity_id: str
    source_entity_kind: str
    target_entity_id: str
    target_entity_kind: str
    valid_from: str
    contributor_ids: tuple[str, ...]
    effective_authority: str
    confidence: float
    properties: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.edge_kind,
            self.source_entity_id,
            self.source_entity_kind,
            self.target_entity_id,
            self.target_entity_kind,
            self.valid_from,
            self.effective_authority,
        )
        if any(not str(value).strip() for value in required):
            raise ValueError("MaterializedEdge required fields must be non-empty")
        if not self.contributor_ids:
            raise ValueError("MaterializedEdge requires at least one contributor")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("MaterializedEdge.confidence must be between 0 and 1")
        if self.properties != _canonical_properties(self.properties):
            raise ValueError("MaterializedEdge.properties must be canonical")


@dataclass(frozen=True)
class EdgeSet:
    materializer_id: str
    slot_key: str
    effective_at: str
    edges: tuple[MaterializedEdge, ...]
    assertion_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.materializer_id.strip() or not self.slot_key.strip() or not self.effective_at.strip():
            raise ValueError("EdgeSet materializer/slot/effective time must be non-empty")
        identities = [
            (
                e.edge_kind,
                e.source_entity_kind,
                e.source_entity_id,
                e.target_entity_kind,
                e.target_entity_id,
                e.valid_from,
            )
            for e in self.edges
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("EdgeSet contains duplicate edge identities")


@dataclass(frozen=True)
class EdgeAbstention:
    materializer_id: str
    slot_key: str
    effective_at: str
    reason: str
    assertion_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.materializer_id.strip() or not self.slot_key.strip() or not self.effective_at.strip():
            raise ValueError("EdgeAbstention materializer/slot/effective time must be non-empty")
        if not self.reason.strip():
            raise ValueError("EdgeAbstention.reason must be non-empty")


GraphOutcome = EdgeSet | EdgeAbstention


@dataclass(frozen=True)
class PersistedEdge:
    storage_key: str
    edge_kind: str
    source_entity_id: str
    source_entity_kind: str
    target_entity_id: str
    target_entity_kind: str
    materializer_id: str
    slot_key: str
    active: bool
    valid_from: str
    retired_at: str | None
    contributor_ids: tuple[str, ...]
    effective_authority: str
    confidence: float
    properties: tuple[tuple[str, Any], ...]


class GraphSchema:
    def __init__(self, *, entity_kinds: Sequence[EntityKind], edge_kinds: Sequence[EdgeKind]) -> None:
        entities = tuple(entity_kinds)
        edges = tuple(edge_kinds)
        if len({row.kind_id for row in entities}) != len(entities):
            raise ValueError("entity kind IDs must be unique")
        if len({row.kind_id for row in edges}) != len(edges):
            raise ValueError("edge kind IDs must be unique")
        self._entity_kinds = {row.kind_id: row for row in entities}
        self._edge_kinds = {row.kind_id: row for row in edges}

    def resolve_entity(self, proposal: EntityProposal) -> ResolvedEntity:
        kind = self._entity_kinds.get(proposal.entity_kind)
        if kind is None:
            raise KeyError(f"unregistered entity kind: {proposal.entity_kind}")
        return kind.resolve(proposal)

    def entity_kind(self, kind_id: str) -> EntityKind:
        if kind_id not in self._entity_kinds:
            raise KeyError(f"unregistered entity kind: {kind_id}")
        return self._entity_kinds[kind_id]

    def edge_kind(self, kind_id: str) -> EdgeKind:
        if kind_id not in self._edge_kinds:
            raise KeyError(f"unregistered edge kind: {kind_id}")
        return self._edge_kinds[kind_id]

    def validate_edge(self, edge: MaterializedEdge) -> None:
        kind = self.edge_kind(edge.edge_kind)
        self.entity_kind(edge.source_entity_kind)
        self.entity_kind(edge.target_entity_kind)
        if edge.source_entity_kind not in kind.source_kinds:
            raise ValueError(f"edge {kind.kind_id} does not allow source kind {edge.source_entity_kind}")
        if edge.target_entity_kind not in kind.target_kinds:
            raise ValueError(f"edge {kind.kind_id} does not allow target kind {edge.target_entity_kind}")
        if kind.provenance_required and not edge.contributor_ids:
            raise ValueError(f"edge {kind.kind_id} requires provenance contributors")
        if kind.temporal and not edge.valid_from.strip():
            raise ValueError(f"edge {kind.kind_id} requires valid_from")


class Neo4jGraphMaterializer:
    """Persist generic entities plus actual ``MUTATION_EDGE`` relationships."""

    def __init__(self, neo4j: Any, *, namespace: str, schema: GraphSchema) -> None:
        if not namespace.strip():
            raise ValueError("Neo4jGraphMaterializer.namespace must be non-empty")
        self._neo4j = neo4j
        self.namespace = namespace.strip()
        self.schema = schema

    def activate(self) -> None:
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_entity_storage_key IF NOT EXISTS "
            "FOR (e:MutationEntity) REQUIRE e.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_entity_receipt_storage_key IF NOT EXISTS "
            "FOR (r:MutationEntityReceipt) REQUIRE r.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_materialized_edge_storage_key IF NOT EXISTS "
            "FOR ()-[r:MUTATION_EDGE]-() REQUIRE r.storage_key IS UNIQUE"
        )

    def _entity_storage_key(self, entity_kind: str, entity_id: str) -> str:
        return _hash_parts(self.namespace, entity_kind, entity_id)

    def record_entity(self, proposal: EntityProposal) -> ResolvedEntity:
        resolved = self.schema.resolve_entity(proposal)
        entity_key = self._entity_storage_key(resolved.entity_kind, resolved.entity_id)
        receipt_key = _hash_parts(self.namespace, "entity-receipt", entity_key, proposal.source_key)
        evidence = {
            "source_id": proposal.source.source_id,
            "source_kind": proposal.source.source_kind,
            "span_start": proposal.source.span_start,
            "span_end": proposal.source.span_end,
            "note": proposal.source.note,
        }
        rows = self._neo4j.execute(
            """
            MERGE (e:MutationEntity {storage_key:$entity_key})
              ON CREATE SET e.namespace=$namespace,
                            e.entity_kind=$entity_kind,
                            e.entity_kind_version=$entity_kind_version,
                            e.entity_id=$entity_id,
                            e.canonical_key=$canonical_key,
                            e.display_name=$display_name,
                            e.created_at=datetime()
            WITH e
            MERGE (receipt:MutationEntityReceipt {storage_key:$receipt_key})
              ON CREATE SET receipt.namespace=$namespace,
                            receipt.entity_storage_key=$entity_key,
                            receipt.source_key=$source_key,
                            receipt.observed_display_name=$display_name,
                            receipt.properties_json=$properties_json,
                            receipt.evidence_json=$evidence_json,
                            receipt.valid_at=$valid_at,
                            receipt.learned_at=$learned_at,
                            receipt.authority=$authority,
                            receipt.confidence=$confidence,
                            receipt.created_at=datetime()
            MERGE (receipt)-[:EVIDENCE_FOR]->(e)
            RETURN e.entity_kind AS entity_kind,
                   e.entity_kind_version AS entity_kind_version,
                   e.entity_id AS entity_id,
                   e.canonical_key AS canonical_key,
                   e.display_name AS display_name
            """,
            {
                "namespace": self.namespace,
                "entity_key": entity_key,
                "receipt_key": receipt_key,
                "entity_kind": resolved.entity_kind,
                "entity_kind_version": int(resolved.entity_kind_version),
                "entity_id": resolved.entity_id,
                "canonical_key": resolved.canonical_key,
                "display_name": resolved.display_name,
                "source_key": proposal.source_key,
                "properties_json": _stable_json(proposal.property_map),
                "evidence_json": _stable_json(evidence),
                "valid_at": proposal.valid_at,
                "learned_at": proposal.learned_at,
                "authority": proposal.authority,
                "confidence": float(proposal.confidence),
            },
        )
        if not rows:
            raise RuntimeError("entity materialization returned no row")
        row = rows[0]
        persisted = ResolvedEntity(
            entity_kind=str(row["entity_kind"]),
            entity_kind_version=int(row["entity_kind_version"]),
            entity_id=str(row["entity_id"]),
            canonical_key=str(row["canonical_key"]),
            display_name=str(row["display_name"]),
        )
        if (
            persisted.entity_kind != resolved.entity_kind
            or persisted.entity_id != resolved.entity_id
            or persisted.canonical_key != resolved.canonical_key
        ):
            raise ValueError("entity identity collision: persisted entity disagrees with resolver")
        return persisted

    def count_entities(self, *, entity_kind: str | None = None) -> int:
        predicates = ["e.namespace = $namespace"]
        params: dict[str, Any] = {"namespace": self.namespace}
        if entity_kind is not None:
            predicates.append("e.entity_kind = $entity_kind")
            params["entity_kind"] = entity_kind
        rows = self._neo4j.execute(
            f"MATCH (e:MutationEntity) WHERE {' AND '.join(predicates)} RETURN count(e) AS n",
            params,
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0

    def count_entity_receipts(self) -> int:
        rows = self._neo4j.execute(
            "MATCH (r:MutationEntityReceipt {namespace:$namespace}) RETURN count(r) AS n",
            {"namespace": self.namespace},
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0

    def reconcile_edges(self, outcome: GraphOutcome) -> dict[str, object]:
        edges = outcome.edges if isinstance(outcome, EdgeSet) else ()
        for edge in edges:
            self.schema.validate_edge(edge)

        edge_rows: list[dict[str, Any]] = []
        for edge in edges:
            source_key = self._entity_storage_key(edge.source_entity_kind, edge.source_entity_id)
            target_key = self._entity_storage_key(edge.target_entity_kind, edge.target_entity_id)
            storage_key = _hash_parts(
                self.namespace,
                outcome.materializer_id,
                outcome.slot_key,
                edge.edge_kind,
                edge.source_entity_kind,
                edge.source_entity_id,
                edge.target_entity_kind,
                edge.target_entity_id,
                edge.valid_from,
            )
            edge_rows.append(
                {
                    "storage_key": storage_key,
                    "source_storage_key": source_key,
                    "source_entity_kind": edge.source_entity_kind,
                    "target_storage_key": target_key,
                    "target_entity_kind": edge.target_entity_kind,
                    "edge_kind": edge.edge_kind,
                    "valid_from": edge.valid_from,
                    "contributor_ids_json": _stable_json(list(edge.contributor_ids)),
                    "effective_authority": edge.effective_authority,
                    "confidence": float(edge.confidence),
                    "properties_json": _stable_json(_properties_dict(edge.properties)),
                }
            )

        desired_keys = [str(row["storage_key"]) for row in edge_rows]
        projection_hash = _hash_parts(
            _stable_json(
                {
                    "materializer_id": outcome.materializer_id,
                    "slot_key": outcome.slot_key,
                    "effective_at": outcome.effective_at,
                    "status": "edges" if isinstance(outcome, EdgeSet) else "abstention",
                    "reason": None if isinstance(outcome, EdgeSet) else outcome.reason,
                    "assertion_ids": list(outcome.assertion_ids),
                    "edges": edge_rows,
                }
            )
        )

        rows = self._neo4j.execute(
            """
            UNWIND CASE WHEN size($edges)=0 THEN [null] ELSE $edges END AS candidate
            OPTIONAL MATCH (source:MutationEntity)
              WHERE candidate IS NOT NULL
                AND source.storage_key=candidate.source_storage_key
                AND source.namespace=$namespace
                AND source.entity_kind=candidate.source_entity_kind
            OPTIONAL MATCH (target:MutationEntity)
              WHERE candidate IS NOT NULL
                AND target.storage_key=candidate.target_storage_key
                AND target.namespace=$namespace
                AND target.entity_kind=candidate.target_entity_kind
            WITH collect({edge:candidate, source:source, target:target}) AS candidates
            WITH [row IN candidates WHERE row.edge IS NOT NULL] AS edge_rows
            WITH edge_rows,
                 size([row IN edge_rows WHERE row.source IS NULL OR row.target IS NULL]) AS missing
            WHERE missing=0
            OPTIONAL MATCH ()-[current:MUTATION_EDGE]->()
              WHERE current.namespace=$namespace
                AND current.materializer_id=$materializer_id
                AND current.slot_key=$slot_key
                AND coalesce(current.active,false)=true
            WITH edge_rows, collect(current) AS current_edges
            WITH edge_rows,
                 [r IN current_edges WHERE NOT r.storage_key IN $desired_keys] AS retiring,
                 [r IN current_edges | r.storage_key] AS previous_active_keys
            FOREACH (r IN retiring |
                SET r.active=false,
                    r.retired_at=$effective_at,
                    r.updated_at=datetime()
            )
            WITH edge_rows, retiring, previous_active_keys
            CALL {
                WITH edge_rows
                UNWIND edge_rows AS row
                WITH row.edge AS edge, row.source AS source, row.target AS target
                MERGE (source)-[rel:MUTATION_EDGE {storage_key:edge.storage_key}]->(target)
                  ON CREATE SET rel.created_at=datetime()
                SET rel.namespace=$namespace,
                    rel.edge_kind=edge.edge_kind,
                    rel.materializer_id=$materializer_id,
                    rel.slot_key=$slot_key,
                    rel.active=true,
                    rel.valid_from=edge.valid_from,
                    rel.retired_at=null,
                    rel.contributor_ids_json=edge.contributor_ids_json,
                    rel.effective_authority=edge.effective_authority,
                    rel.confidence=edge.confidence,
                    rel.properties_json=edge.properties_json,
                    rel.projection_hash=$projection_hash,
                    rel.updated_at=datetime()
                RETURN count(rel) AS upserted
            }
            RETURN previous_active_keys AS previous_active_keys,
                   size(retiring) AS retired,
                   upserted AS upserted
            """,
            {
                "namespace": self.namespace,
                "materializer_id": outcome.materializer_id,
                "slot_key": outcome.slot_key,
                "effective_at": outcome.effective_at,
                "desired_keys": desired_keys,
                "projection_hash": projection_hash,
                "edges": edge_rows,
            },
        )
        if not rows:
            raise ValueError("graph edge materialization could not resolve all endpoint entities")
        row = rows[0]
        previous_active = tuple(str(value) for value in (row.get("previous_active_keys") or []))
        return {
            "changed": set(previous_active) != set(desired_keys),
            "retired": int(row.get("retired", 0) or 0),
            "upserted": int(row.get("upserted", 0) or 0),
            "projection_hash": projection_hash,
            "active_edge_count": len(desired_keys),
            "status": "edges" if isinstance(outcome, EdgeSet) else "abstention",
        }

    def load_edges(
        self,
        *,
        materializer_id: str | None = None,
        slot_key: str | None = None,
        active: bool | None = None,
    ) -> list[PersistedEdge]:
        predicates = ["r.namespace = $namespace"]
        params: dict[str, Any] = {"namespace": self.namespace}
        if materializer_id is not None:
            predicates.append("r.materializer_id = $materializer_id")
            params["materializer_id"] = materializer_id
        if slot_key is not None:
            predicates.append("r.slot_key = $slot_key")
            params["slot_key"] = slot_key
        if active is not None:
            predicates.append("coalesce(r.active,false) = $active")
            params["active"] = bool(active)
        rows = self._neo4j.execute(
            f"""
            MATCH (source:MutationEntity)-[r:MUTATION_EDGE]->(target:MutationEntity)
            WHERE {' AND '.join(predicates)}
            RETURN r.storage_key AS storage_key,
                   r.edge_kind AS edge_kind,
                   source.entity_id AS source_entity_id,
                   source.entity_kind AS source_entity_kind,
                   target.entity_id AS target_entity_id,
                   target.entity_kind AS target_entity_kind,
                   r.materializer_id AS materializer_id,
                   r.slot_key AS slot_key,
                   coalesce(r.active,false) AS active,
                   r.valid_from AS valid_from,
                   r.retired_at AS retired_at,
                   r.contributor_ids_json AS contributor_ids_json,
                   r.effective_authority AS effective_authority,
                   r.confidence AS confidence,
                   r.properties_json AS properties_json
            ORDER BY r.valid_from ASC, r.storage_key ASC
            """,
            params,
        )
        hydrated: list[PersistedEdge] = []
        for row in rows:
            props = json.loads(str(row.get("properties_json") or "{}"))
            hydrated.append(
                PersistedEdge(
                    storage_key=str(row["storage_key"]),
                    edge_kind=str(row["edge_kind"]),
                    source_entity_id=str(row["source_entity_id"]),
                    source_entity_kind=str(row["source_entity_kind"]),
                    target_entity_id=str(row["target_entity_id"]),
                    target_entity_kind=str(row["target_entity_kind"]),
                    materializer_id=str(row["materializer_id"]),
                    slot_key=str(row["slot_key"]),
                    active=bool(row.get("active")),
                    valid_from=str(row["valid_from"]),
                    retired_at=None if row.get("retired_at") is None else str(row["retired_at"]),
                    contributor_ids=tuple(
                        str(value)
                        for value in json.loads(str(row.get("contributor_ids_json") or "[]"))
                    ),
                    effective_authority=str(row.get("effective_authority") or "agent"),
                    confidence=float(row.get("confidence", 0.0) or 0.0),
                    properties=tuple(sorted(((str(k), v) for k, v in props.items()), key=lambda x: x[0])),
                )
            )
        return hydrated

    def count_edges(self, *, active: bool | None = None) -> int:
        predicates = ["r.namespace = $namespace"]
        params: dict[str, Any] = {"namespace": self.namespace}
        if active is not None:
            predicates.append("coalesce(r.active,false) = $active")
            params["active"] = bool(active)
        rows = self._neo4j.execute(
            f"MATCH ()-[r:MUTATION_EDGE]->() WHERE {' AND '.join(predicates)} RETURN count(r) AS n",
            params,
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0
