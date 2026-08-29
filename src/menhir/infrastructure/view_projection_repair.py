"""Durable claim/lease boundary for generic View projection repair receipts.

The repository is intentionally not registered in the always-on runtime yet.  A repair pass may
take longer than one scheduler tick and the existing bootstrap has no independently leased hook
whose ownership can be proven here.  Callers may construct this repository explicitly and invoke
``ViewProjectionRepairService.run_pending``; wiring an always-on job is a separate activation step.

Claims are compare-and-set operations at the graph boundary.  The dummy ``claim_lock_version``
increment forces Neo4j to acquire the row write lock before the eligibility predicate is evaluated
again.  Completion/failure transitions then require the current owner, opaque token, and unexpired
lease.  Completion additionally checks the namespace fence generation and the immutable projection
identity captured by the claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.infrastructure.neo4j import Neo4jRepository


PENDING = "pending"
CLAIMED = "claimed"
COMPLETE = "complete"
FAILED = "failed"
TERMINAL_NOT_REBUILDABLE = "terminal_not_rebuildable"


@dataclass(frozen=True)
class ViewProjectionRepairClaim:
    """One leased repair receipt plus the projection identity fenced by the claim."""

    repair_key: str
    owner_id: str
    claim_token: str
    view_uuid: str
    view_key: str
    view_kind: str
    view_subtype: str
    source_family: str
    namespace: str
    namespace_key: str
    fence_generation: int
    subject_uuid: str
    predicate: str
    domain: str
    attempt_count: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ViewProjectionRepairClaim":
        return cls(
            repair_key=str(row.get("repair_key") or ""),
            owner_id=str(row.get("owner_id") or ""),
            claim_token=str(row.get("claim_token") or ""),
            view_uuid=str(row.get("view_uuid") or ""),
            view_key=str(row.get("view_key") or ""),
            view_kind=str(row.get("view_kind") or ""),
            view_subtype=str(row.get("view_subtype") or ""),
            source_family=str(row.get("source_family") or ""),
            namespace=str(row.get("namespace") or "default"),
            namespace_key=str(row.get("namespace_key") or "default"),
            fence_generation=int(row.get("fence_generation", 0) or 0),
            subject_uuid=str(row.get("subject_uuid") or ""),
            predicate=str(row.get("predicate") or ""),
            domain=str(row.get("domain") or ""),
            attempt_count=int(row.get("attempt_count", 0) or 0),
        )


class ViewProjectionRepairRepository:
    """Atomic graph operations for ``:ViewProjectionRepair`` work items."""

    def __init__(self, neo4j: Neo4jRepository) -> None:
        self._neo4j = neo4j

    def claim_pending(
        self,
        *,
        owner_id: str,
        limit: int = 25,
        lease_seconds: int = 300,
    ) -> list[ViewProjectionRepairClaim]:
        """Atomically lease retryable receipts and return their fenced projection identities.

        ``blocked`` is accepted as a legacy producer status.  It is not treated as terminal: an
        empty deterministic rebuild is meaningful because it confirms that the retired View must
        remain absent.  The first claim migrates such a row into the approved state machine.
        """
        clean_owner = str(owner_id or "").strip()
        if not clean_owner:
            raise ValueError("owner_id must be non-blank")
        if limit < 1:
            raise ValueError("limit must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")

        rows = self._neo4j.execute(
            """
            // view_projection_repair:claim
            MATCH (r:ViewProjectionRepair)
            WHERE r.status IN ['pending', 'failed', 'blocked']
               OR (r.status = 'claimed'
                   AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= datetime()))
            WITH r
            ORDER BY coalesce(r.last_attempt_at, r.started_at, datetime({epochMillis: 0})),
                     r.repair_key
            LIMIT $limit
            // A write lock is acquired here.  Eligibility is deliberately re-read only after it.
            SET r.claim_lock_version = coalesce(r.claim_lock_version, 0) + 1
            WITH r
            WHERE r.status IN ['pending', 'failed', 'blocked']
               OR (r.status = 'claimed'
                   AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= datetime()))
            OPTIONAL MATCH (source:Entity {uuid: r.view_uuid})
            WITH r, source,
                 CASE
                     WHEN trim(toString(coalesce(r.namespace, source.namespace,
                                                 source.group_id, ''))) = ''
                     THEN 'default'
                     ELSE trim(toString(coalesce(r.namespace, source.namespace,
                                                 source.group_id)))
                 END AS namespace_key
            MERGE (f:EvidenceNamespaceFence {namespace_key: namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime()
            SET r.subject_uuid = coalesce(r.subject_uuid, source.view_subject_uuid),
                r.predicate = coalesce(r.predicate, source.view_predicate),
                r.domain = coalesce(r.domain, source.view_domain),
                r.view_subtype = coalesce(r.view_subtype, source.view_subtype)
            WITH r, f, namespace_key
            SET r.status = 'claimed',
                r.claim_owner = $owner_id,
                r.claim_token = randomUUID(),
                r.lease_expires_at = datetime() + duration({seconds: $lease_seconds}),
                r.last_attempt_at = datetime(),
                r.attempt_count = coalesce(r.attempt_count, 0) + 1,
                r.namespace_key = namespace_key,
                r.claimed_fence_generation = coalesce(f.generation, 0),
                r.claimed_view_key = coalesce(toString(r.view_key), ''),
                r.claimed_view_kind = coalesce(toString(r.view_kind), ''),
                r.claimed_view_subtype = coalesce(toString(r.view_subtype), ''),
                r.claimed_source_family = coalesce(toString(r.source_family), ''),
                r.claimed_namespace = coalesce(toString(r.namespace), 'default'),
                r.claimed_subject_uuid = coalesce(toString(r.subject_uuid), ''),
                r.claimed_predicate = coalesce(toString(r.predicate), ''),
                r.claimed_domain = coalesce(toString(r.domain), '')
            RETURN r.repair_key AS repair_key,
                   r.claim_owner AS owner_id,
                   r.claim_token AS claim_token,
                   r.view_uuid AS view_uuid,
                   r.view_key AS view_key,
                   r.view_kind AS view_kind,
                   r.view_subtype AS view_subtype,
                   r.source_family AS source_family,
                   coalesce(r.namespace, 'default') AS namespace,
                   namespace_key,
                   r.claimed_fence_generation AS fence_generation,
                   r.subject_uuid AS subject_uuid,
                   r.predicate AS predicate,
                   r.domain AS domain,
                   r.attempt_count AS attempt_count
            ORDER BY repair_key
            """,
            params={
                "owner_id": clean_owner,
                "limit": int(limit),
                "lease_seconds": int(lease_seconds),
            },
            safe_to_reexecute=True,
        )
        return [ViewProjectionRepairClaim.from_row(dict(row)) for row in rows]

    def complete(self, claim: ViewProjectionRepairClaim) -> bool:
        """Complete a claim only while its lease, namespace fence, and identity remain current.

        Absence of a current projection is valid: a successful empty rebuild deliberately leaves
        the erased projection retired.  If a current projection with this key does exist, every
        identity component must still match the claimed receipt.
        """
        rows = self._neo4j.execute(
            """
            // view_projection_repair:complete
            MATCH (r:ViewProjectionRepair {repair_key: $repair_key})
            WHERE r.status = 'claimed'
              AND r.claim_owner = $owner_id
              AND r.claim_token = $claim_token
              AND r.lease_expires_at > datetime()
              AND coalesce(toString(r.view_key), '') = r.claimed_view_key
              AND coalesce(toString(r.view_kind), '') = r.claimed_view_kind
              AND coalesce(toString(r.view_subtype), '') = r.claimed_view_subtype
              AND coalesce(toString(r.source_family), '') = r.claimed_source_family
              AND coalesce(toString(r.namespace), 'default') = r.claimed_namespace
              AND coalesce(toString(r.subject_uuid), '') = r.claimed_subject_uuid
              AND coalesce(toString(r.predicate), '') = r.claimed_predicate
              AND coalesce(toString(r.domain), '') = r.claimed_domain
            MATCH (f:EvidenceNamespaceFence {namespace_key: r.namespace_key})
            WHERE coalesce(f.generation, 0) = r.claimed_fence_generation
            OPTIONAL MATCH (current:Entity)
            WHERE coalesce(current.view_current, current.qs_current, false)
              AND NOT coalesce(current.retired, false)
              AND coalesce(current.view_key, current.qs_key, '') = r.claimed_view_key
            WITH r, [v IN collect(current) WHERE v IS NOT NULL] AS current_versions
            WHERE all(v IN current_versions WHERE
                coalesce(toString(v.view_kind), '') = r.claimed_view_kind
                AND coalesce(toString(v.view_subtype), '') = r.claimed_view_subtype
                AND CASE
                        WHEN trim(toString(coalesce(v.namespace, v.group_id, ''))) = ''
                        THEN 'default'
                        ELSE trim(toString(coalesce(v.namespace, v.group_id)))
                    END = CASE
                        WHEN trim(r.claimed_namespace) = '' THEN 'default'
                        ELSE trim(r.claimed_namespace)
                    END
                AND coalesce(toString(v.view_subject_uuid), '') = r.claimed_subject_uuid
                AND coalesce(toString(v.view_predicate), '') = r.claimed_predicate
                AND coalesce(toString(v.view_domain), '') = r.claimed_domain)
            SET r.status = 'complete',
                r.completed_at = datetime(),
                r.last_error = null,
                r.claim_owner = null,
                r.claim_token = null,
                r.lease_expires_at = null
            RETURN count(r) AS completed
            """,
            params=self._claim_params(claim),
            safe_to_reexecute=True,
        )
        return bool(rows and int(rows[0].get("completed", 0) or 0) == 1)

    def fail(self, claim: ViewProjectionRepairClaim, error: str) -> bool:
        """Record a retryable failure, conditional on the still-current claim lease."""
        return self._finish_with_error(claim, status=FAILED, error=error, terminal=False)

    def terminal_not_rebuildable(self, claim: ViewProjectionRepairClaim, reason: str) -> bool:
        """Record that a claimed legacy/unsupported projection has no lawful rebuild path."""
        return self._finish_with_error(
            claim,
            status=TERMINAL_NOT_REBUILDABLE,
            error=reason,
            terminal=True,
        )

    def _finish_with_error(
        self,
        claim: ViewProjectionRepairClaim,
        *,
        status: str,
        error: str,
        terminal: bool,
    ) -> bool:
        message = str(error or "unspecified repair failure").strip()[:4000]
        rows = self._neo4j.execute(
            """
            // view_projection_repair:error
            MATCH (r:ViewProjectionRepair {repair_key: $repair_key})
            WHERE r.status = 'claimed'
              AND r.claim_owner = $owner_id
              AND r.claim_token = $claim_token
              AND r.lease_expires_at > datetime()
            SET r.status = $status,
                r.last_error = $error,
                r.last_failed_at = datetime(),
                r.failure_count = coalesce(r.failure_count, 0) + 1,
                r.terminal_at = CASE WHEN $terminal THEN datetime() ELSE r.terminal_at END,
                r.claim_owner = null,
                r.claim_token = null,
                r.lease_expires_at = null
            RETURN count(r) AS updated
            """,
            params={
                **self._claim_params(claim),
                "status": status,
                "error": message,
                "terminal": terminal,
            },
            safe_to_reexecute=True,
        )
        return bool(rows and int(rows[0].get("updated", 0) or 0) == 1)

    @staticmethod
    def _claim_params(claim: ViewProjectionRepairClaim) -> dict[str, str]:
        return {
            "repair_key": claim.repair_key,
            "owner_id": claim.owner_id,
            "claim_token": claim.claim_token,
        }
