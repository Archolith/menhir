"""Type-neutral Neo4j persistence for the mutation-kernel research spike.

This module deliberately knows only the kernel envelopes. Domain extensions provide a value codec
for their opaque values; the store owns namespace isolation, immutable assertion storage, and one
replaceable current projection outcome per generic slot.

It is NOT a proposed production schema. The point of the experiment is to test whether persistence
and lifecycle repair can remain extension-neutral without importing scalar or personality semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Protocol

from .kernel import Abstention, Assertion, EvidenceRef, Retirement, View


ProjectionOutcome = View | Abstention | Retirement


class ValueCodec(Protocol):
    """Extension-owned codec for opaque assertion/projection values."""

    def encode(self, value: Any) -> Any: ...

    def decode(self, payload: Any) -> Any: ...


@dataclass(frozen=True)
class JsonValueCodec:
    """Identity codec for values that are already JSON-compatible."""

    def encode(self, value: Any) -> Any:
        return value

    def decode(self, payload: Any) -> Any:
        return payload


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_parts(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _pairs_json(pairs: tuple[tuple[str, str], ...]) -> str:
    return _stable_json([[name, value] for name, value in pairs])


def _evidence_json(evidence: tuple[EvidenceRef, ...]) -> str:
    return _stable_json(
        [
            {
                "source_id": ref.source_id,
                "source_kind": ref.source_kind,
                "span_start": ref.span_start,
                "span_end": ref.span_end,
                "note": ref.note,
            }
            for ref in evidence
        ]
    )


def _projection_slot(outcome: ProjectionOutcome) -> tuple[str, str, str, str, tuple[tuple[str, str], ...]]:
    return (
        outcome.view_type,
        outcome.subject_id,
        outcome.scope,
        outcome.key,
        outcome.dimensions,
    )


class Neo4jEnvelopeStore:
    """Persist generic kernel assertions and projection outcomes in one namespace.

    Assertion rows are immutable-by-fingerprint: replaying the same assertion is a no-op, while
    presenting a different envelope under the same assertion identity fails closed.

    Projection rows are disposable current state. One row exists per generic slot and can move
    between View, Abstention, and Retirement as a deterministic rebuild changes its answer.

    ``namespace`` is storage isolation rather than assertion semantics. The same generic assertion
    identity may therefore exist independently in two namespaces without collision.
    """

    def __init__(
        self,
        neo4j: Any,
        *,
        namespace: str,
        codec: ValueCodec | None = None,
    ) -> None:
        if not namespace.strip():
            raise ValueError("Neo4jEnvelopeStore.namespace must be non-empty")
        self._neo4j = neo4j
        self.namespace = namespace.strip()
        self.codec: ValueCodec = codec or JsonValueCodec()

    def activate(self) -> None:
        """Create only the spike-local uniqueness constraints used by this store."""
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_assertion_storage_key IF NOT EXISTS "
            "FOR (a:MutationAssertion) REQUIRE a.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_projection_storage_key IF NOT EXISTS "
            "FOR (p:MutationProjection) REQUIRE p.storage_key IS UNIQUE"
        )

    def record_assertion(self, assertion: Assertion) -> dict[str, str]:
        encoded_value = self.codec.encode(assertion.value)
        payload = {
            "assertion_id": assertion.assertion_id,
            "source_key": assertion.source_key,
            "assertion_type": assertion.assertion_type,
            "subject_id": assertion.subject_id,
            "scope": assertion.scope,
            "key": assertion.key,
            "value": encoded_value,
            "valid_at": assertion.valid_at,
            "learned_at": assertion.learned_at,
            "authority": assertion.authority,
            "confidence": assertion.confidence,
            "evidence": json.loads(_evidence_json(assertion.evidence)),
            "supersedes": list(assertion.supersedes),
            "interpreter_version": assertion.interpreter_version,
            "metadata": [list(pair) for pair in assertion.metadata],
            "dimensions": [list(pair) for pair in assertion.dimensions],
        }
        payload_hash = _hash_parts(_stable_json(payload))
        storage_key = _hash_parts(self.namespace, assertion.assertion_id)
        rows = self._neo4j.execute(
            """
            MERGE (a:MutationAssertion {storage_key:$storage_key})
              ON CREATE SET
                a.namespace = $namespace,
                a.assertion_id = $assertion_id,
                a.source_key = $source_key,
                a.assertion_type = $assertion_type,
                a.subject_id = $subject_id,
                a.scope = $scope,
                a.key = $key,
                a.value_json = $value_json,
                a.valid_at = $valid_at,
                a.learned_at = $learned_at,
                a.authority = $authority,
                a.confidence = $confidence,
                a.evidence_json = $evidence_json,
                a.supersedes_json = $supersedes_json,
                a.interpreter_version = $interpreter_version,
                a.metadata_json = $metadata_json,
                a.dimensions_json = $dimensions_json,
                a.payload_hash = $payload_hash,
                a.created_at = datetime()
            RETURN a.payload_hash AS payload_hash
            """,
            {
                "storage_key": storage_key,
                "namespace": self.namespace,
                "assertion_id": assertion.assertion_id,
                "source_key": assertion.source_key,
                "assertion_type": assertion.assertion_type,
                "subject_id": assertion.subject_id,
                "scope": assertion.scope,
                "key": assertion.key,
                "value_json": _stable_json(encoded_value),
                "valid_at": assertion.valid_at,
                "learned_at": assertion.learned_at,
                "authority": assertion.authority,
                "confidence": float(assertion.confidence),
                "evidence_json": _evidence_json(assertion.evidence),
                "supersedes_json": _stable_json(list(assertion.supersedes)),
                "interpreter_version": assertion.interpreter_version,
                "metadata_json": _pairs_json(assertion.metadata),
                "dimensions_json": _pairs_json(assertion.dimensions),
                "payload_hash": payload_hash,
            },
        )
        existing_hash = str(rows[0].get("payload_hash") or "") if rows else ""
        if existing_hash != payload_hash:
            raise ValueError(
                "assertion identity collision: persisted envelope differs for "
                f"{assertion.assertion_id}"
            )
        return {"storage_key": storage_key, "payload_hash": payload_hash}

    def load_assertions(
        self,
        *,
        subject_id: str | None = None,
        assertion_type: str | None = None,
        scope: str | None = None,
        key: str | None = None,
        dimensions: tuple[tuple[str, str], ...] | None = None,
    ) -> list[Assertion]:
        predicates = ["a.namespace = $namespace"]
        params: dict[str, Any] = {"namespace": self.namespace}
        for field, value in (
            ("subject_id", subject_id),
            ("assertion_type", assertion_type),
            ("scope", scope),
            ("key", key),
        ):
            if value is not None:
                predicates.append(f"a.{field} = ${field}")
                params[field] = value
        if dimensions is not None:
            predicates.append("a.dimensions_json = $dimensions_json")
            params["dimensions_json"] = _pairs_json(dimensions)
        rows = self._neo4j.execute(
            f"""
            MATCH (a:MutationAssertion)
            WHERE {' AND '.join(predicates)}
            RETURN a.assertion_id AS assertion_id, a.source_key AS source_key,
                   a.assertion_type AS assertion_type, a.subject_id AS subject_id,
                   a.scope AS scope, a.key AS key, a.value_json AS value_json,
                   a.valid_at AS valid_at, a.learned_at AS learned_at,
                   a.authority AS authority, a.confidence AS confidence,
                   a.evidence_json AS evidence_json, a.supersedes_json AS supersedes_json,
                   a.interpreter_version AS interpreter_version,
                   a.metadata_json AS metadata_json, a.dimensions_json AS dimensions_json
            ORDER BY a.valid_at ASC, a.learned_at ASC, a.assertion_id ASC
            """,
            params,
        )
        return [self._hydrate_assertion(dict(row)) for row in rows]

    def count_assertions(self) -> int:
        rows = self._neo4j.execute(
            "MATCH (a:MutationAssertion {namespace:$namespace}) RETURN count(a) AS n",
            {"namespace": self.namespace},
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0

    def record_view(self, view: View) -> dict[str, Any]:
        """Compatibility wrapper for the earlier active-View-only experiment."""
        return self.record_outcome(view)

    def record_outcome(self, outcome: ProjectionOutcome) -> dict[str, Any]:
        """Atomically replace the current projection outcome for one generic slot."""
        payload = self._encode_outcome(outcome)
        view_type, subject_id, scope, key, dimensions = _projection_slot(outcome)
        projection_key = _hash_parts(
            view_type,
            subject_id,
            scope,
            key,
            _pairs_json(dimensions),
        )
        storage_key = _hash_parts(self.namespace, projection_key)
        projection_hash = _hash_parts(_stable_json(payload))
        rows = self._neo4j.execute(
            """
            MERGE (p:MutationProjection {storage_key:$storage_key})
            WITH p, p.projection_hash AS previous_hash
            SET p.namespace = $namespace,
                p.projection_key = $projection_key,
                p.view_type = $view_type,
                p.subject_id = $subject_id,
                p.scope = $scope,
                p.key = $key,
                p.dimensions_json = $dimensions_json,
                p.status = $status,
                p.payload_json = $payload_json,
                p.projection_hash = $projection_hash,
                p.updated_at = datetime()
            RETURN previous_hash AS previous_hash, p.projection_hash AS projection_hash
            """,
            {
                "storage_key": storage_key,
                "namespace": self.namespace,
                "projection_key": projection_key,
                "view_type": view_type,
                "subject_id": subject_id,
                "scope": scope,
                "key": key,
                "dimensions_json": _pairs_json(dimensions),
                "status": payload["status"],
                "payload_json": _stable_json(payload),
                "projection_hash": projection_hash,
            },
        )
        previous_hash = rows[0].get("previous_hash") if rows else None
        return {
            "storage_key": storage_key,
            "projection_key": projection_key,
            "projection_hash": projection_hash,
            "previous_hash": None if previous_hash is None else str(previous_hash),
            "changed": previous_hash != projection_hash,
            "status": payload["status"],
        }

    def load_outcomes(
        self,
        *,
        subject_id: str | None = None,
        view_type: str | None = None,
        scope: str | None = None,
        key: str | None = None,
        dimensions: tuple[tuple[str, str], ...] | None = None,
    ) -> list[ProjectionOutcome]:
        predicates = ["p.namespace = $namespace"]
        params: dict[str, Any] = {"namespace": self.namespace}
        for field, value in (
            ("subject_id", subject_id),
            ("view_type", view_type),
            ("scope", scope),
            ("key", key),
        ):
            if value is not None:
                predicates.append(f"p.{field} = ${field}")
                params[field] = value
        if dimensions is not None:
            predicates.append("p.dimensions_json = $dimensions_json")
            params["dimensions_json"] = _pairs_json(dimensions)
        rows = self._neo4j.execute(
            f"""
            MATCH (p:MutationProjection)
            WHERE {' AND '.join(predicates)}
            RETURN p.payload_json AS payload_json
            ORDER BY p.view_type ASC, p.subject_id ASC, p.scope ASC, p.key ASC,
                     p.dimensions_json ASC
            """,
            params,
        )
        return [
            self._hydrate_outcome(json.loads(str(row["payload_json"])))
            for row in rows
        ]

    def load_views(
        self,
        *,
        subject_id: str | None = None,
        view_type: str | None = None,
        scope: str | None = None,
        key: str | None = None,
        dimensions: tuple[tuple[str, str], ...] | None = None,
    ) -> list[View]:
        """Return only active Views; abstentions and retirements are intentionally absent."""
        return [
            outcome
            for outcome in self.load_outcomes(
                subject_id=subject_id,
                view_type=view_type,
                scope=scope,
                key=key,
                dimensions=dimensions,
            )
            if isinstance(outcome, View)
        ]

    def count_projections(self) -> int:
        rows = self._neo4j.execute(
            "MATCH (p:MutationProjection {namespace:$namespace}) RETURN count(p) AS n",
            {"namespace": self.namespace},
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0

    def count_views(self) -> int:
        rows = self._neo4j.execute(
            """
            MATCH (p:MutationProjection {namespace:$namespace, status:'view'})
            RETURN count(p) AS n
            """,
            {"namespace": self.namespace},
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0

    def _encode_outcome(self, outcome: ProjectionOutcome) -> dict[str, Any]:
        if isinstance(outcome, View):
            return {
                "status": "view",
                "view_type": outcome.view_type,
                "subject_id": outcome.subject_id,
                "scope": outcome.scope,
                "key": outcome.key,
                "dimensions": [list(pair) for pair in outcome.dimensions],
                "value": self.codec.encode(outcome.value),
                "confidence": outcome.confidence,
                "contributor_ids": list(outcome.contributor_ids),
                "counterevidence_ids": list(outcome.counterevidence_ids),
                "effective_authority": outcome.effective_authority,
                "rationale": list(outcome.rationale),
            }
        if isinstance(outcome, Abstention):
            return {
                "status": "abstention",
                "view_type": outcome.view_type,
                "subject_id": outcome.subject_id,
                "scope": outcome.scope,
                "key": outcome.key,
                "dimensions": [list(pair) for pair in outcome.dimensions],
                "reason": outcome.reason,
                "assertion_ids": list(outcome.assertion_ids),
            }
        if isinstance(outcome, Retirement):
            return {
                "status": "retirement",
                "view_type": outcome.view_type,
                "subject_id": outcome.subject_id,
                "scope": outcome.scope,
                "key": outcome.key,
                "dimensions": [list(pair) for pair in outcome.dimensions],
                "reason": outcome.reason,
                "effective_at": outcome.effective_at,
                "contributor_ids": list(outcome.contributor_ids),
                "retired_value": self.codec.encode(outcome.retired_value),
            }
        raise TypeError(f"unsupported projection outcome: {type(outcome)!r}")

    def _hydrate_outcome(self, payload: dict[str, Any]) -> ProjectionOutcome:
        dimensions = tuple(
            (str(name), str(value))
            for name, value in payload.get("dimensions", [])
        )
        common = {
            "view_type": str(payload["view_type"]),
            "subject_id": str(payload["subject_id"]),
            "scope": str(payload.get("scope") or ""),
            "key": str(payload["key"]),
            "dimensions": dimensions,
        }
        status = str(payload.get("status") or "")
        if status == "view":
            return View(
                **common,
                value=self.codec.decode(payload.get("value")),
                confidence=float(payload.get("confidence", 0.0)),
                contributor_ids=tuple(
                    str(value) for value in payload.get("contributor_ids", [])
                ),
                counterevidence_ids=tuple(
                    str(value) for value in payload.get("counterevidence_ids", [])
                ),
                effective_authority=str(payload.get("effective_authority") or "agent"),
                rationale=tuple(str(value) for value in payload.get("rationale", [])),
            )
        if status == "abstention":
            return Abstention(
                **common,
                reason=str(payload.get("reason") or ""),
                assertion_ids=tuple(
                    str(value) for value in payload.get("assertion_ids", [])
                ),
            )
        if status == "retirement":
            return Retirement(
                **common,
                reason=str(payload.get("reason") or ""),
                effective_at=str(payload.get("effective_at") or ""),
                contributor_ids=tuple(
                    str(value) for value in payload.get("contributor_ids", [])
                ),
                retired_value=self.codec.decode(payload.get("retired_value")),
            )
        raise ValueError(f"unknown persisted projection status: {status!r}")

    def _hydrate_assertion(self, row: dict[str, Any]) -> Assertion:
        evidence = tuple(
            EvidenceRef(
                source_id=str(item["source_id"]),
                source_kind=str(item["source_kind"]),
                span_start=item.get("span_start"),
                span_end=item.get("span_end"),
                note=item.get("note"),
            )
            for item in json.loads(str(row.get("evidence_json") or "[]"))
        )
        return Assertion(
            assertion_id=str(row["assertion_id"]),
            source_key=str(row["source_key"]),
            assertion_type=str(row["assertion_type"]),
            subject_id=str(row["subject_id"]),
            scope=str(row.get("scope") or ""),
            key=str(row["key"]),
            value=self.codec.decode(json.loads(str(row["value_json"]))),
            valid_at=str(row["valid_at"]),
            learned_at=str(row["learned_at"]),
            authority=str(row.get("authority") or "agent"),
            confidence=float(row.get("confidence", 1.0)),
            evidence=evidence,
            supersedes=tuple(
                str(value)
                for value in json.loads(str(row.get("supersedes_json") or "[]"))
            ),
            interpreter_version=str(row.get("interpreter_version") or "v1"),
            metadata=tuple(
                (str(name), str(value))
                for name, value in json.loads(str(row.get("metadata_json") or "[]"))
            ),
            dimensions=tuple(
                (str(name), str(value))
                for name, value in json.loads(str(row.get("dimensions_json") or "[]"))
            ),
        )
