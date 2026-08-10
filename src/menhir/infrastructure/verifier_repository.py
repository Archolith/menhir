"""Neo4j graph operations for verifiers — the source-of-truth binding layer.

A verifier is a plain `(:Entity {is_verifier:true})` node holding a binding (kind + params + the
register coordinates it maintains). It is data, not code: the executor that reads the live source
lives in `menhir.services.verifier_sync.DEFAULT_EXECUTORS`.

Edges:
  (register:Entity {view_kind:'counter'})-[:VERIFIED_BY]->(verifier)   -- the value it maintains
  (belief:Entity)-[:REFERENCES]->(register)                            -- prose that relies on it

Beliefs are flagged (`needs_review`, `review_reason`) when their register's value changes, so recall
can down-rank prose that now restates a stale value instead of asserting it as current truth.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from menhir.domain.namespace import stamped_namespace


class VerifierRepository:
    """Neo4j operations for graph-native verifiers and their dependents."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    @staticmethod
    def _verifier_key(kind: str, params: dict[str, Any], subject: str, counter: str) -> str:
        canon = json.dumps(params, sort_keys=True, separators=(",", ":"))
        return f"{kind}::{subject.strip().lower()}::{counter.strip().lower()}::{canon}"

    # ------------------------------------------------------------------ seeding / linking

    def upsert_verifier(
        self,
        *,
        kind: str,
        params: dict[str, Any],
        register_subject: str,
        register_counter: str,
        namespace: str = "agent-status",
    ) -> str:
        """Create (or return) the verifier node for a (kind, register) binding. Idempotent on a
        deterministic key so re-seeding never duplicates. Returns the verifier uuid."""
        key = self._verifier_key(kind, params, register_subject, register_counter)
        rows = self._neo4j.execute(
            """
            MERGE (v:Entity {is_verifier: true, verifier_key: $key})
            ON CREATE SET v.uuid = $uuid,
                          v.verifier_kind = $kind,
                          v.verifier_params = $params,
                          v.register_subject = $subject,
                          v.register_counter = $counter,
                          v.group_id = $ns,
                          v.namespace = $ns_stamped,
                          v.name = $name,
                          v.created_at = datetime()
            RETURN v.uuid AS uuid
            """,
            params={
                "key": key,
                "uuid": str(uuid4()),
                "kind": kind,
                "params": json.dumps(params, sort_keys=True),
                "subject": register_subject,
                "counter": register_counter,
                "ns": namespace,
                "ns_stamped": stamped_namespace(namespace),
                "name": f"verifier: {register_subject}.{register_counter} <- {kind}",
            },
        )
        return str(rows[0]["uuid"]) if rows else ""

    def link_reference(self, *, belief_uuid: str, register_uuid: str) -> bool:
        """Record that a (free-text) belief relies on a register's value: belief -[:REFERENCES]-> reg."""
        rows = self._neo4j.execute(
            """
            MATCH (b:Entity {uuid: $belief}), (r:Entity {uuid: $register})
            MERGE (b)-[rel:REFERENCES]->(r)
            ON CREATE SET rel.created_at = datetime()
            RETURN r.uuid AS uuid
            """,
            params={"belief": belief_uuid, "register": register_uuid},
        )
        return bool(rows)

    # ------------------------------------------------------------------ sync-time reads/writes

    def list_verifiers(self) -> list[dict[str, Any]]:
        rows = self._neo4j.execute(
            """
            MATCH (v:Entity {is_verifier: true})
            RETURN v.uuid AS uuid, v.verifier_kind AS verifier_kind,
                   v.verifier_params AS verifier_params,
                   v.register_subject AS register_subject,
                   v.register_counter AS register_counter
            """,
            params={},
        )
        return [dict(r) for r in rows]

    def ensure_verified_edge(self, *, register_uuid: str, verifier_uuid: str) -> None:
        """register -[:VERIFIED_BY]-> verifier. MERGE keeps it idempotent as the register supersedes
        (a fresh current-version register uuid links itself on the next sync)."""
        self._neo4j.execute(
            """
            MATCH (r:Entity {uuid: $register}), (v:Entity {uuid: $verifier})
            MERGE (r)-[rel:VERIFIED_BY]->(v)
            ON CREATE SET rel.created_at = datetime()
            """,
            params={"register": register_uuid, "verifier": verifier_uuid},
        )

    def stamp_verifier(self, *, verifier_uuid: str, value: float, display: str, at: str) -> None:
        """Record the last successful re-derivation on the verifier node (freshness provenance)."""
        self._neo4j.execute(
            """
            MATCH (v:Entity {uuid: $uuid})
            SET v.last_verified_at = datetime($at),
                v.last_value = $value,
                v.last_display = $display
            """,
            params={"uuid": verifier_uuid, "at": at, "value": float(value), "display": display},
        )

    def flag_referencing_beliefs(
        self, *, verifier_uuid: str, new_value: float, display: str, at: str
    ) -> int:
        """When a verifier's value changes, flag every belief that REFERENCES its register(s) for
        review, so recall can down-rank prose that may now restate a stale value. Returns the count.
        Registers themselves are excluded (they self-supersede via record_counter)."""
        rows = self._neo4j.execute(
            """
            MATCH (v:Entity {uuid: $verifier})<-[:VERIFIED_BY]-(reg:Entity)<-[:REFERENCES]-(b:Entity)
            WHERE coalesce(b.is_view, false) = false
            SET b.needs_review = true,
                b.review_reason = 'verifier value changed to ' + $display,
                b.review_flagged_at = datetime($at)
            RETURN count(DISTINCT b) AS flagged
            """,
            params={"verifier": verifier_uuid, "display": display, "at": at},
        )
        return int(rows[0]["flagged"]) if rows else 0
