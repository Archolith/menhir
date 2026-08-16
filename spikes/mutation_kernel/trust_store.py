"""Immutable Neo4j persistence for purpose-sensitive trust profiles.

Trust profiles are governance sidecars attached to admitted Assertion interpretations. The generic
store does not interpret extension facets; it only fingerprint-checks them and reconstructs the exact
profile for purpose-specific resolvers.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .trust import TrustProfile, _hash_parts, _stable_json


class Neo4jTrustProfileStore:
    def __init__(self, neo4j: Any, *, namespace: str) -> None:
        if not namespace.strip():
            raise ValueError("Neo4jTrustProfileStore.namespace must be non-empty")
        self._neo4j = neo4j
        self.namespace = namespace.strip()

    def activate(self) -> None:
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_trust_profile_storage_key IF NOT EXISTS "
            "FOR (p:MutationTrustProfile) REQUIRE p.storage_key IS UNIQUE"
        )

    def record(self, profile: TrustProfile) -> dict[str, object]:
        storage_key = _hash_parts(self.namespace, "trust-profile", profile.profile_id)
        payload = {
            "profile_id": profile.profile_id,
            "assertion_id": profile.assertion_id,
            "source_key": profile.source_key,
            "profile_version": profile.profile_version,
            "facets": [list(pair) for pair in profile.facets],
        }
        payload_hash = _hash_parts(_stable_json(payload))
        write_token = uuid.uuid4().hex
        rows = self._neo4j.execute(
            """
            MERGE (profile:MutationTrustProfile {storage_key:$storage_key})
              ON CREATE SET profile.namespace=$namespace,
                            profile.profile_id=$profile_id,
                            profile.assertion_id=$assertion_id,
                            profile.source_key=$source_key,
                            profile.profile_version=$profile_version,
                            profile.facets_json=$facets_json,
                            profile.payload_hash=$payload_hash,
                            profile.created_token=$write_token,
                            profile.created_at=datetime()
            RETURN profile.payload_hash AS payload_hash,
                   profile.created_token=$write_token AS created
            """,
            {
                "storage_key": storage_key,
                "namespace": self.namespace,
                "profile_id": profile.profile_id,
                "assertion_id": profile.assertion_id,
                "source_key": profile.source_key,
                "profile_version": int(profile.profile_version),
                "facets_json": _stable_json([list(pair) for pair in profile.facets]),
                "payload_hash": payload_hash,
                "write_token": write_token,
            },
        )
        if not rows:
            raise RuntimeError("trust profile write returned no result")
        if str(rows[0].get("payload_hash") or "") != payload_hash:
            raise ValueError("trust profile identity collision: persisted profile differs")
        return {
            "storage_key": storage_key,
            "created": bool(rows[0].get("created")),
        }

    def load_for_assertion(self, assertion_id: str) -> list[TrustProfile]:
        rows = self._neo4j.execute(
            """
            MATCH (profile:MutationTrustProfile {namespace:$namespace})
            WHERE profile.assertion_id=$assertion_id
            RETURN profile.profile_id AS profile_id,
                   profile.assertion_id AS assertion_id,
                   profile.source_key AS source_key,
                   profile.profile_version AS profile_version,
                   profile.facets_json AS facets_json
            ORDER BY profile.profile_version ASC, profile.profile_id ASC
            """,
            {"namespace": self.namespace, "assertion_id": assertion_id},
        )
        return [
            TrustProfile(
                profile_id=str(row["profile_id"]),
                assertion_id=str(row["assertion_id"]),
                source_key=str(row["source_key"]),
                profile_version=int(row["profile_version"]),
                facets=tuple(
                    (str(name), str(value))
                    for name, value in json.loads(str(row.get("facets_json") or "[]"))
                ),
            )
            for row in rows
        ]

    def count(self) -> int:
        rows = self._neo4j.execute(
            "MATCH (p:MutationTrustProfile {namespace:$namespace}) RETURN count(p) AS n",
            {"namespace": self.namespace},
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0
