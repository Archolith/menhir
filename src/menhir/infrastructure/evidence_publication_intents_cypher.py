"""Cypher statements for evidence publication intent transitions.

Each constant is hoisted verbatim from the repository method that executes it, including the
tenant-scope fragment concatenation, which is evaluated once at import time exactly as the
former in-method concatenation evaluated it on every call.
"""

from menhir.domain.namespace import tenant_scope_cypher

BEGIN_INTENT_CYPHER = """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime($now)
            SET f.lock_nonce = $operation_id, f.locked_at = datetime($now)
            WITH f
            MERGE (i:EvidencePublicationIntent {intent_key: $intent_key})
            ON CREATE SET
                i.operation_id = $operation_id,
                i.episode_uuid = $episode_uuid,
                i.namespace_key = $namespace_key,
                i.group_id = $group_id,
                i.expected_name = $expected_name,
                i.source_description = $source_description,
                i.reference_time = datetime($reference_time),
                i.generation = f.generation,
                i.status = 'PENDING',
                i.dispatch_token = $dispatch_token,
                i.created_at = datetime($now)
            SET i.updated_at = datetime($now)
            MERGE (f)-[:GOVERNS_PUBLICATION]->(i)
            RETURN i.intent_key AS intent_key,
                   i.operation_id AS operation_id,
                   i.episode_uuid AS episode_uuid,
                   i.namespace_key AS namespace_key,
                   i.group_id AS group_id,
                   i.expected_name AS expected_name,
                   i.source_description AS source_description,
                   toString(i.reference_time) AS reference_time,
                   i.generation AS generation,
                   i.status AS status,
                   i.resolved_episode_uuid AS resolved_episode_uuid,
                   i.lease_owner AS lease_owner,
                   i.lease_token AS lease_token,
                   i.lease_generation AS lease_generation,
                   i.dispatch_token = $dispatch_token AND i.status = 'PENDING'
                       AS dispatch_allowed
            """

GET_INTENT_CYPHER = """
            MATCH (i:EvidencePublicationIntent {intent_key: $intent_key})
            RETURN i.intent_key AS intent_key,
                   i.operation_id AS operation_id,
                   i.episode_uuid AS episode_uuid,
                   i.namespace_key AS namespace_key,
                   i.group_id AS group_id,
                   i.expected_name AS expected_name,
                   i.source_description AS source_description,
                   toString(i.reference_time) AS reference_time,
                   i.generation AS generation,
                   i.status AS status,
                   i.resolved_episode_uuid AS resolved_episode_uuid,
                   i.lease_owner AS lease_owner,
                   i.lease_token AS lease_token,
                   i.lease_generation AS lease_generation
            """

FINALIZE_REMOTE_OUTCOME_CYPHER = (
            """
            MATCH (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            SET f.lock_nonce = $operation_id, f.locked_at = datetime($now)
            WITH f
            MATCH (i:EvidencePublicationIntent {intent_key: $intent_key})
            OPTIONAL MATCH (e:Episodic)
            WHERE e.uuid <> $episode_uuid
              AND ($remote_episode_uuid IS NULL OR e.uuid = $remote_episode_uuid)
              AND e.name = $expected_name
              AND """ + tenant_scope_cypher("e") + """
              AND coalesce(e.source_description, '') = $source_description
              AND e.valid_at = datetime($reference_time)
            WITH f, i, [candidate IN collect(DISTINCT e)
                        WHERE candidate IS NOT NULL] AS candidates
            OPTIONAL MATCH (artifact_node)
            WHERE artifact_node.uuid IN $artifact_node_uuids
            WITH f, i, candidates,
                 [node IN collect(DISTINCT artifact_node) WHERE node IS NOT NULL] AS artifact_nodes
            OPTIONAL MATCH (artifact_start)-[artifact_edge]-(artifact_end)
            WHERE artifact_edge.uuid IN $artifact_edge_uuids
            WITH f, i, candidates, artifact_nodes,
                 [edge IN collect(DISTINCT artifact_edge) WHERE edge IS NOT NULL] AS artifact_edges
            OPTIONAL MATCH (t:EvidenceTombstone)
            WHERE coalesce(t.status, 'ACTIVE') IN ['ACTIVE', 'PENDING']
              AND coalesce(t.active, true)
              AND t.cleared_at IS NULL
              AND any(probe IN $tombstone_probes
                      WHERE t.digest = probe.digest AND t.key_id = probe.key_id)
            WITH f, i, candidates, artifact_nodes, artifact_edges,
                 [tombstone IN collect(DISTINCT t)
                  WHERE tombstone IS NOT NULL] AS tombstones
            WITH f, i, candidates, artifact_nodes, artifact_edges, tombstones,
                 i.status IN ['PENDING', 'CLAIMED']
                 AND ($lease_token IS NULL OR (
                    i.lease_owner = $lease_owner
                    AND i.lease_token = $lease_token
                    AND i.lease_generation = $lease_generation
                 )) AS transition_allowed,
                 i.status = 'FINALIZED'
                 AND i.resolved_episode_uuid = $remote_episode_uuid
                 AND i.generation = $generation
                 AND f.generation = $generation AS already_finalized,
                 i.status = 'QUARANTINED' AS already_quarantined
            WITH f, i, candidates, artifact_nodes, artifact_edges, tombstones,
                 already_finalized, already_quarantined,
                 transition_allowed
                 AND i.generation = $generation
                 AND f.generation = $generation
                 AND $remote_episode_uuid IS NOT NULL
                 AND size(candidates) = 1
                 AND size(artifact_nodes) = size($artifact_node_uuids)
                 AND size(artifact_edges) = size($artifact_edge_uuids)
                 AND size(tombstones) = 0 AS eligible
            FOREACH (artifact_node IN artifact_nodes |
                SET artifact_node.evidence_finalized = false,
                    artifact_node.evidence_quarantined = true,
                    artifact_node.publication_intent_key = $intent_key,
                    artifact_node.publication_operation_id = $operation_id,
                    artifact_node.publication_generation = $generation,
                    artifact_node.evidence_generation = $generation)
            FOREACH (artifact_edge IN artifact_edges |
                SET artifact_edge.evidence_finalized = false,
                    artifact_edge.evidence_quarantined = true,
                    artifact_edge.publication_intent_key = $intent_key,
                    artifact_edge.publication_operation_id = $operation_id,
                    artifact_edge.publication_generation = $generation,
                    artifact_edge.evidence_generation = $generation)
            FOREACH (artifact_node IN CASE WHEN eligible OR already_finalized
                                           THEN artifact_nodes ELSE [] END |
                SET artifact_node.evidence_finalized = true,
                    artifact_node.evidence_quarantined = false,
                    artifact_node.evidence_finalized_at = datetime($now)
                REMOVE artifact_node.evidence_quarantine_reason)
            FOREACH (artifact_edge IN CASE WHEN eligible OR already_finalized
                                           THEN artifact_edges ELSE [] END |
                SET artifact_edge.evidence_finalized = true,
                    artifact_edge.evidence_quarantined = false,
                    artifact_edge.evidence_finalized_at = datetime($now)
                REMOVE artifact_edge.evidence_quarantine_reason)
            FOREACH (artifact_node IN CASE WHEN eligible OR already_finalized
                                           THEN [] ELSE artifact_nodes END |
                SET artifact_node.evidence_quarantine_reason = CASE
                    WHEN f.generation <> $generation OR i.generation <> $generation
                        THEN 'generation_mismatch'
                    WHEN size(candidates) <> 1 THEN 'remote_identity_not_unique'
                    WHEN size(tombstones) > 0 THEN 'active_tombstone'
                    WHEN $lease_token IS NOT NULL AND (
                        i.lease_owner <> $lease_owner OR
                        i.lease_token <> $lease_token OR
                        i.lease_generation <> $lease_generation
                    ) THEN 'lease_lost'
                    ELSE 'intent_not_finalizable'
                END)
            FOREACH (artifact_edge IN CASE WHEN eligible OR already_finalized
                                           THEN [] ELSE artifact_edges END |
                SET artifact_edge.evidence_quarantine_reason = CASE
                    WHEN f.generation <> $generation OR i.generation <> $generation
                        THEN 'generation_mismatch'
                    WHEN size(candidates) <> 1 THEN 'remote_identity_not_unique'
                    WHEN size(artifact_nodes) <> size($artifact_node_uuids)
                      OR size(artifact_edges) <> size($artifact_edge_uuids)
                        THEN 'artifact_manifest_mismatch'
                    WHEN size(tombstones) > 0 THEN 'active_tombstone'
                    WHEN $lease_token IS NOT NULL AND (
                        i.lease_owner <> $lease_owner OR
                        i.lease_token <> $lease_token OR
                        i.lease_generation <> $lease_generation
                    ) THEN 'lease_lost'
                    ELSE 'intent_not_finalizable'
                END)
            SET i.status = CASE
                    WHEN already_finalized THEN 'FINALIZED'
                    WHEN already_quarantined THEN 'QUARANTINED'
                    WHEN eligible THEN 'FINALIZED'
                    ELSE 'QUARANTINED'
                END,
                i.resolved_episode_uuid = CASE
                    WHEN already_finalized THEN i.resolved_episode_uuid
                    WHEN size(candidates) = 1 THEN head(candidates).uuid ELSE null END,
                i.candidate_count = size(candidates),
                i.tombstone_count = size(tombstones),
                i.completed_at = coalesce(i.completed_at, datetime($now)),
                i.updated_at = datetime($now),
                i.quarantine_reason = CASE
                    WHEN eligible OR already_finalized THEN null
                    WHEN already_quarantined THEN i.quarantine_reason
                    WHEN f.generation <> $generation OR i.generation <> $generation
                        THEN 'generation_mismatch'
                    WHEN size(candidates) <> 1 THEN 'remote_identity_not_unique'
                    WHEN size(artifact_nodes) <> size($artifact_node_uuids)
                      OR size(artifact_edges) <> size($artifact_edge_uuids)
                        THEN 'artifact_manifest_mismatch'
                    WHEN size(tombstones) > 0 THEN 'active_tombstone'
                    WHEN $lease_token IS NOT NULL AND (
                        i.lease_owner <> $lease_owner OR
                        i.lease_token <> $lease_token OR
                        i.lease_generation <> $lease_generation
                    ) THEN 'lease_lost'
                    ELSE 'intent_not_finalizable'
                END
            REMOVE i.lease_owner, i.lease_token, i.lease_expires_at
            RETURN i.intent_key AS intent_key,
                   i.status AS status,
                   i.resolved_episode_uuid AS resolved_episode_uuid,
                   i.candidate_count AS candidate_count,
                   i.tombstone_count AS tombstone_count,
                   i.quarantine_reason AS reason
            """
)

CLAIM_PENDING_CYPHER = """
            MATCH (i:EvidencePublicationIntent)
            WHERE i.status = 'PENDING'
               OR (i.status = 'CLAIMED' AND i.lease_expires_at <= datetime($now))
            WITH i ORDER BY i.created_at, i.intent_key LIMIT $limit
            SET i.lease_generation = CASE
                    WHEN i.lease_token = $lease_token THEN i.lease_generation
                    ELSE coalesce(i.lease_generation, 0) + 1
                END,
                i.status = 'CLAIMED',
                i.lease_owner = $owner_id,
                i.lease_token = $lease_token,
                i.lease_expires_at = datetime($now) + duration({seconds: $lease_seconds}),
                i.updated_at = datetime($now)
            RETURN i.intent_key AS intent_key,
                   i.operation_id AS operation_id,
                   i.episode_uuid AS episode_uuid,
                   i.namespace_key AS namespace_key,
                   i.group_id AS group_id,
                   i.expected_name AS expected_name,
                   i.source_description AS source_description,
                   toString(i.reference_time) AS reference_time,
                   i.generation AS generation,
                   i.status AS status,
                   i.resolved_episode_uuid AS resolved_episode_uuid,
                   i.lease_owner AS lease_owner,
                   i.lease_token AS lease_token,
                   i.lease_generation AS lease_generation
            ORDER BY i.intent_key
            """

DISCOVER_EXACT_REMOTE_UUIDS_CYPHER = (
            """
            MATCH (e:Episodic)
            WHERE e.uuid <> $episode_uuid
              AND e.name = $expected_name
              AND """ + tenant_scope_cypher("e") + """
              AND coalesce(e.source_description, '') = $source_description
              AND e.valid_at = datetime($reference_time)
            RETURN collect(DISTINCT e.uuid) AS episode_uuids
            """
)

RELEASE_PENDING_CYPHER = """
            MATCH (i:EvidencePublicationIntent {intent_key: $intent_key})
            WHERE i.status = 'CLAIMED'
              AND i.lease_owner = $lease_owner
              AND i.lease_token = $lease_token
              AND i.lease_generation = $lease_generation
            SET i.status = 'PENDING', i.updated_at = datetime($now)
            REMOVE i.lease_owner, i.lease_token, i.lease_expires_at
            RETURN count(i) AS released
            """

FETCH_EPISODE_ARTIFACT_CYPHER = (
            """
            MATCH (e:Episodic {uuid: $remote_episode_uuid})
            WHERE """ + tenant_scope_cypher("e") + """
              AND e.publication_intent_key = $intent_key
              AND e.evidence_finalized = true
              AND NOT coalesce(e.evidence_quarantined, false)
            OPTIONAL MATCH (e)-[episode_rel]-(n:Entity)
            WITH e, collect(DISTINCT n) AS entities,
                 [rel IN collect(DISTINCT episode_rel)
                  WHERE rel.uuid IS NOT NULL | rel.uuid] AS episode_edge_uuids
            OPTIONAL MATCH (a:Entity)-[entity_rel]-(b:Entity)
            WHERE a IN entities AND b IN entities AND entity_rel.uuid IS NOT NULL
            RETURN e.uuid AS resolved_episode_uuid,
                   [entity IN entities WHERE entity IS NOT NULL | entity.uuid] AS entity_uuids,
                   episode_edge_uuids + collect(DISTINCT entity_rel.uuid) AS edge_uuids
            """
)
