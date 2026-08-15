"""Type-neutral Neo4j persistence for the mutation-kernel research spike.

This module deliberately knows only the kernel envelopes. Domain extensions provide a value codec
for their opaque ``Assertion.value`` / ``View.value`` payloads; the store owns namespace isolation,
durable envelope fields, replay-safe assertion identity, and one replaceable current View per slot.

It is NOT a proposed production schema. The point of the experiment is to test whether persistence
can be an extension-neutral seam without importing scalar or personality semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Protocol

from .kernel import Assertion, EvidenceRef, View


class ValueCodec(Protocol):
    """Extension-owned codec for opaque assertion/view values."""

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


class Neo4jEnvelopeStore:
    """Persist generic kernel Assertions and current Views in one namespace.

    Assertion rows are immutable-by-fingerprint: replaying the same assertion is a no-op, while
    presenting a different envelope under the same assertion identity fails closed. Views are
    projections, so recording the same slot updates that one disposable current row.

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
        """Create only the two spike-local uniqueness constraints used by this store."""
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_assertion_storage_key IF NOT EXISTS "
            "FOR (a:MutationAssertion) REQUIRE a.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_view_storage_key IF NOT EXISTS "
            "FOR (v:MutationView) REQUIRE v.storage_key IS UNIQUE"
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

    def record_view(self, view: View) -> dict[str, str]:
        encoded_value = self.codec.encode(view.value)
        view_key = _hash_parts(
            view.view_type,
            view.subject_id,
            view.scope,
            view.key,
            _pairs_json(view.dimensions),
        )
        storage_key = _hash_parts(self.namespace, view_key)
        projection_hash = _hash_parts(
            _stable_json(
                {
                    "view_type": view.view_type,
                    "subject_id": view.subject_id,
                    "scope": view.scope,
                    "key": view.key,
                    "value": encoded_value,
                    "confidence": view.confidence,
                    "contributor_ids": list(view.contributor_ids),
                    "counterevidence_ids": list(view.counterevidence_ids),
                    "effective_authority": view.effective_authority,
                    "rationale": list(view.rationale),
                    "dimensions": [list(pair) for pair in view.dimensions],
                }
            )
        )
        self._neo4j.execute(
            """
            MERGE (v:MutationView {storage_key:$storage_key})
            SET v.namespace = $namespace,
                v.view_key = $view_key,
                v.view_type = $view_type,
                v.subject_id = $subject_id,
                v.scope = $scope,
                v.key = $key,
                v.value_json = $value_json,
                v.confidence = $confidence,
                v.contributor_ids_json = $contributor_ids_json,
                v.counterevidence_ids_json = $counterevidence_ids_json,
                v.effective_authority = $effective_authority,
                v.rationale_json = $rationale_json,
                v.dimensions_json = $dimensions_json,
                v.projection_hash = $projection_hash,
                v.updated_at = datetime()
            """,
            {
                "storage_key": storage_key,
                "namespace": self.namespace,
                "view_key": view_key,
                "view_type": view.view_type,
                "subject_id": view.subject_id,
                "scope": view.scope,
                "key": view.key,
                "value_json": _stable_json(encoded_value),
                "confidence": float(view.confidence),
                "contributor_ids_json": _stable_json(list(view.contributor_ids)),
                "counterevidence_ids_json": _stable_json(list(view.counterevidence_ids)),
                "effective_authority": view.effective_authority,
                "rationale_json": _stable_json(list(view.rationale)),
                "dimensions_json": _pairs_json(view.dimensions),
                "projection_hash": projection_hash,
            },
        )
        return {
            "storage_key": storage_key,
            "view_key": view_key,
            "projection_hash": projection_hash,
        }

    def load_views(
        self,
        *,
        subject_id: str | None = None,
        view_type: str | None = None,
        scope: str | None = None,
        key: str | None = None,
    ) -> list[View]:
        predicates = ["v.namespace = $namespace"]
        params: dict[str, Any] = {"namespace": self.namespace}
        for field, value in (
            ("subject_id", subject_id),
            ("view_type", view_type),
            ("scope", scope),
            ("key", key),
        ):
            if value is not None:
                predicates.append(f"v.{field} = ${field}")
                params[field] = value
        rows = self._neo4j.execute(
            f"""
            MATCH (v:MutationView)
            WHERE {' AND '.join(predicates)}
            RETURN v.view_type AS view_type, v.subject_id AS subject_id,
                   v.scope AS scope, v.key AS key, v.value_json AS value_json,
                   v.confidence AS confidence,
                   v.contributor_ids_json AS contributor_ids_json,
                   v.counterevidence_ids_json AS counterevidence_ids_json,
                   v.effective_authority AS effective_authority,
                   v.rationale_json AS rationale_json,
                   v.dimensions_json AS dimensions_json
            ORDER BY v.view_type ASC, v.subject_id ASC, v.scope ASC, v.key ASC
            """,
            params,
        )
        return [self._hydrate_view(dict(row)) for row in rows]

    def count_views(self) -> int:
        rows = self._neo4j.execute(
            "MATCH (v:MutationView {namespace:$namespace}) RETURN count(v) AS n",
            {"namespace": self.namespace},
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0

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

    def _hydrate_view(self, row: dict[str, Any]) -> View:
        return View(
            view_type=str(row["view_type"]),
            subject_id=str(row["subject_id"]),
            scope=str(row.get("scope") or ""),
            key=str(row["key"]),
            value=self.codec.decode(json.loads(str(row["value_json"]))),
            confidence=float(row.get("confidence", 0.0)),
            contributor_ids=tuple(
                str(value)
                for value in json.loads(str(row.get("contributor_ids_json") or "[]"))
            ),
            counterevidence_ids=tuple(
                str(value)
                for value in json.loads(str(row.get("counterevidence_ids_json") or "[]"))
            ),
            effective_authority=str(row.get("effective_authority") or "agent"),
            rationale=tuple(
                str(value)
                for value in json.loads(str(row.get("rationale_json") or "[]"))
            ),
            dimensions=tuple(
                (str(name), str(value))
                for name, value in json.loads(str(row.get("dimensions_json") or "[]"))
            ),
        )
