"""Neo4j audit receipts for admission decisions in the mutation-kernel spike.

Admission decisions are durable governance evidence distinct from the semantic Assertion. A rejected
attempt can therefore be audited even though it never enters assertion history. An admitted decision
is recorded first; only the engine-produced sealed assertion may then be passed to the generic
``Neo4jEnvelopeStore``.

This wrapper is an interface experiment, not a claim that callers cannot bypass the underlying store
in the current spike. A production core would make admitted persistence the supported write path.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .admission import AdmissionDecision
from .neo4j_store import Neo4jEnvelopeStore, _hash_parts, _stable_json


class Neo4jAdmissionStore:
    def __init__(
        self,
        neo4j: Any,
        *,
        namespace: str,
        assertions: Neo4jEnvelopeStore,
    ) -> None:
        if not namespace.strip():
            raise ValueError("Neo4jAdmissionStore.namespace must be non-empty")
        self._neo4j = neo4j
        self.namespace = namespace.strip()
        self.assertions = assertions

    def activate(self) -> None:
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_admission_receipt_storage_key IF NOT EXISTS "
            "FOR (r:MutationAdmissionReceipt) REQUIRE r.storage_key IS UNIQUE"
        )

    def _payload(self, decision: AdmissionDecision) -> dict[str, object]:
        return {
            "decision_id": decision.decision_id,
            "status": decision.status,
            "reason": decision.reason,
            "proposal_assertion_id": decision.proposal_assertion_id,
            "admitted_assertion_id": (
                None
                if decision.admitted_assertion is None
                else decision.admitted_assertion.assertion_id
            ),
            "source_key": decision.source_key,
            "policy_id": decision.policy_id,
            "policy_version": decision.policy_version,
            "requested_authority": decision.requested_authority,
            "ingress_ceiling": decision.ingress_ceiling,
            "policy_ceiling": decision.policy_ceiling,
            "effective_authority": decision.effective_authority,
            "attempted_promotion": decision.attempted_promotion,
            "grant_ids": list(decision.grant_ids),
        }

    def record_decision(self, decision: AdmissionDecision) -> dict[str, object]:
        payload = self._payload(decision)
        payload_hash = _hash_parts(_stable_json(payload))
        storage_key = _hash_parts(self.namespace, "admission", decision.decision_id)
        write_token = uuid.uuid4().hex
        rows = self._neo4j.execute(
            """
            MERGE (receipt:MutationAdmissionReceipt {storage_key:$storage_key})
              ON CREATE SET receipt.namespace=$namespace,
                            receipt.decision_id=$decision_id,
                            receipt.status=$status,
                            receipt.reason=$reason,
                            receipt.proposal_assertion_id=$proposal_assertion_id,
                            receipt.admitted_assertion_id=$admitted_assertion_id,
                            receipt.source_key=$source_key,
                            receipt.policy_id=$policy_id,
                            receipt.policy_version=$policy_version,
                            receipt.requested_authority=$requested_authority,
                            receipt.ingress_ceiling=$ingress_ceiling,
                            receipt.policy_ceiling=$policy_ceiling,
                            receipt.effective_authority=$effective_authority,
                            receipt.attempted_promotion=$attempted_promotion,
                            receipt.grant_ids_json=$grant_ids_json,
                            receipt.payload_hash=$payload_hash,
                            receipt.assertion_persisted=false,
                            receipt.created_token=$write_token,
                            receipt.created_at=datetime()
            RETURN receipt.payload_hash AS payload_hash,
                   receipt.created_token=$write_token AS created,
                   coalesce(receipt.assertion_persisted,false) AS assertion_persisted
            """,
            {
                "storage_key": storage_key,
                "namespace": self.namespace,
                "decision_id": decision.decision_id,
                "status": decision.status,
                "reason": decision.reason,
                "proposal_assertion_id": decision.proposal_assertion_id,
                "admitted_assertion_id": payload["admitted_assertion_id"],
                "source_key": decision.source_key,
                "policy_id": decision.policy_id,
                "policy_version": int(decision.policy_version),
                "requested_authority": decision.requested_authority,
                "ingress_ceiling": decision.ingress_ceiling,
                "policy_ceiling": decision.policy_ceiling,
                "effective_authority": decision.effective_authority,
                "attempted_promotion": bool(decision.attempted_promotion),
                "grant_ids_json": _stable_json(list(decision.grant_ids)),
                "payload_hash": payload_hash,
                "write_token": write_token,
            },
        )
        if not rows:
            raise RuntimeError("admission receipt write returned no result")
        existing_hash = str(rows[0].get("payload_hash") or "")
        if existing_hash != payload_hash:
            raise ValueError("admission decision ID collision: persisted receipt differs")
        return {
            "storage_key": storage_key,
            "created": bool(rows[0].get("created")),
            "assertion_persisted": bool(rows[0].get("assertion_persisted")),
        }

    def admit_and_record(self, decision: AdmissionDecision) -> dict[str, object]:
        receipt = self.record_decision(decision)
        if decision.status == "rejected":
            return {**receipt, "assertion_recorded": False}
        assertion = decision.admitted_assertion
        if assertion is None:
            raise ValueError("admitted decision is missing its sealed assertion")
        assertion_write = self.assertions.record_assertion(assertion)
        self._neo4j.execute(
            """
            MATCH (receipt:MutationAdmissionReceipt {storage_key:$storage_key})
            SET receipt.assertion_persisted=true,
                receipt.assertion_persisted_at=datetime()
            RETURN receipt.storage_key AS storage_key
            """,
            {"storage_key": str(receipt["storage_key"])},
        )
        return {
            **receipt,
            "assertion_recorded": True,
            "assertion_created": bool(assertion_write.get("created")),
            "assertion_storage_key": assertion_write.get("storage_key"),
        }

    def load_receipts(self) -> list[dict[str, object]]:
        rows = self._neo4j.execute(
            """
            MATCH (receipt:MutationAdmissionReceipt {namespace:$namespace})
            RETURN receipt.decision_id AS decision_id,
                   receipt.status AS status,
                   receipt.reason AS reason,
                   receipt.proposal_assertion_id AS proposal_assertion_id,
                   receipt.admitted_assertion_id AS admitted_assertion_id,
                   receipt.source_key AS source_key,
                   receipt.policy_id AS policy_id,
                   receipt.policy_version AS policy_version,
                   receipt.requested_authority AS requested_authority,
                   receipt.ingress_ceiling AS ingress_ceiling,
                   receipt.policy_ceiling AS policy_ceiling,
                   receipt.effective_authority AS effective_authority,
                   coalesce(receipt.attempted_promotion,false) AS attempted_promotion,
                   receipt.grant_ids_json AS grant_ids_json,
                   coalesce(receipt.assertion_persisted,false) AS assertion_persisted
            ORDER BY receipt.decision_id ASC
            """,
            {"namespace": self.namespace},
        )
        return [
            {
                "decision_id": str(row["decision_id"]),
                "status": str(row["status"]),
                "reason": str(row["reason"]),
                "proposal_assertion_id": str(row["proposal_assertion_id"]),
                "admitted_assertion_id": (
                    None
                    if row.get("admitted_assertion_id") is None
                    else str(row["admitted_assertion_id"])
                ),
                "source_key": str(row["source_key"]),
                "policy_id": str(row["policy_id"]),
                "policy_version": int(row["policy_version"]),
                "requested_authority": str(row["requested_authority"]),
                "ingress_ceiling": (
                    None if row.get("ingress_ceiling") is None else str(row["ingress_ceiling"])
                ),
                "policy_ceiling": (
                    None if row.get("policy_ceiling") is None else str(row["policy_ceiling"])
                ),
                "effective_authority": (
                    None
                    if row.get("effective_authority") is None
                    else str(row["effective_authority"])
                ),
                "attempted_promotion": bool(row.get("attempted_promotion")),
                "grant_ids": tuple(
                    str(value)
                    for value in json.loads(str(row.get("grant_ids_json") or "[]"))
                ),
                "assertion_persisted": bool(row.get("assertion_persisted")),
            }
            for row in rows
        ]
