"""Profile recall latency phases against live Neo4j.

This script directly queries Neo4j to measure each phase of the recall pipeline
without needing the full service stack (GraphitiClient, OpenAI keys, etc.).
"""

from __future__ import annotations

import time
from neo4j import GraphDatabase

URI = "bolt://localhost:7687"
AUTH = ("neo4j", "password")


def main() -> None:
    driver = GraphDatabase.driver(URI, auth=AUTH)
    driver.verify_connectivity()
    print("Connected to Neo4j")

    with driver.session() as session:
        # --- Indexes and stats ---
        result = session.run("SHOW INDEXES YIELD name, type, state WHERE state = 'ONLINE' RETURN name, type")
        for r in result:
            print(f"  Index: {r['name']} type={r['type']}")

        result = session.run("MATCH (e:Entity) RETURN count(e) AS cnt")
        for r in result:
            print(f"  Entity count: {r['cnt']}")

        result = session.run("MATCH (e:Episode) RETURN count(e) AS cnt")
        for r in result:
            print(f"  Episode count: {r['cnt']}")

        # --- Phase 2: Metadata fetch benchmark ---
        # Simulate fetch_candidate_metadata with 50 UUIDs
        print("\n--- Phase: Metadata fetch (50 UUIDs) ---")
        result = session.run(
            "MATCH (e:Entity) RETURN e.uuid AS uuid LIMIT 50"
        )
        uuids = [r["uuid"] for r in result]
        if uuids:
            times = []
            for _ in range(5):
                t0 = time.perf_counter()
                session.run(
                    "MATCH (e:Entity) WHERE e.uuid IN $uuids "
                    "RETURN e.uuid AS uuid, e.name AS name, e.summary AS summary, "
                    "e.scope AS scope, e.type AS type, e.freshness AS freshness, "
                    "e.last_accessed AS last_accessed, e.source_confidence AS source_confidence, "
                    "e.conflict_group_id AS conflict_group_id, e.conflict_status AS conflict_status",
                    uuids=uuids,
                ).data()
                ms = int((time.perf_counter() - t0) * 1000)
                times.append(ms)
            print(f"  Metadata fetch (50 UUIDs): {times} ms  avg={sum(times)//len(times)}ms")

        # --- Phase 3: Adjacency fetch benchmark ---
        print("\n--- Phase: Adjacency fetch ---")
        if uuids:
            times = []
            for _ in range(5):
                t0 = time.perf_counter()
                session.run(
                    "MATCH (a:Entity)-[r]-(b:Entity) "
                    "WHERE a.uuid IN $uuids AND b.uuid IN $uuids "
                    "RETURN a.uuid AS source, b.uuid AS target, r.weight AS weight, "
                    "elementId(r) AS edge_uuid",
                    uuids=uuids,
                ).data()
                ms = int((time.perf_counter() - t0) * 1000)
                times.append(ms)
            print(f"  Adjacency fetch (50 UUIDs): {times} ms  avg={sum(times)//len(times)}ms")

        # --- Phase: Fulltext search benchmark ---
        print("\n--- Phase: Fulltext search simulation ---")
        # Check if there's a vector index
        result = session.run(
            "SHOW INDEXES YIELD name, type, indexType WHERE type = 'VECTOR' OR indexType = 'VECTOR' "
            "RETURN name, type, indexType"
        )
        vector_indexes = list(result)
        if vector_indexes:
            for vi in vector_indexes:
                print(f"  Vector index: {vi['name']} type={vi['type']} indexType={vi['indexType']}")
        else:
            print("  No vector indexes found — vector search is done by Graphiti (external)")

        # --- Check embedding cache ---
        print("\n--- Phase: Embedding cache check ---")
        result = session.run(
            "MATCH (e:Entity) WHERE e.embedding IS NOT NULL "
            "RETURN count(e) AS with_embedding, "
            "count(CASE WHEN size(e.embedding) > 0 THEN 1 END) AS nonzero_embedding"
        )
        for r in result:
            print(f"  Entities with embeddings: {r['with_embedding']} (nonzero: {r['nonzero_embedding']})")

        # --- Phase: Post-recall touch benchmark ---
        print("\n--- Phase: Post-recall updates (touch 10 nodes) ---")
        if uuids:
            touch_uuids = uuids[:10]
            times = []
            for _ in range(5):
                t0 = time.perf_counter()
                session.run(
                    "MATCH (e:Entity) WHERE e.uuid IN $uuids "
                    "SET e.last_accessed = datetime()",
                    uuids=touch_uuids,
                )
                ms = int((time.perf_counter() - t0) * 1000)
                times.append(ms)
            print(f"  Touch 10 nodes: {times} ms  avg={sum(times)//len(times)}ms")

        # --- Edge weight increment benchmark ---
        print("\n--- Phase: Edge weight increment (10 edges) ---")
        result = session.run(
            "MATCH (a:Entity)-[r]-(b:Entity) "
            "RETURN elementId(r) AS eid, r.weight AS w LIMIT 10"
        )
        edges = [r["eid"] for r in result]
        if edges:
            times = []
            for _ in range(5):
                t0 = time.perf_counter()
                for eid in edges:
                    session.run(
                        "MATCH ()-[r]-() WHERE elementId(r) = $eid "
                        "SET r.weight = coalesce(r.weight, 1.0) + 0.1, "
                        "r.last_traversed = datetime()",
                        eid=eid,
                    )
                ms = int((time.perf_counter() - t0) * 1000)
                times.append(ms)
            print(f"  Increment 10 edges (individual): {times} ms  avg={sum(times)//len(times)}ms")

        # --- Stale conflict groups count ---
        print("\n--- Stale conflict groups ---")
        result = session.run(
            "MATCH (e:Entity) WHERE e.conflict_group_id IS NOT NULL "
            "AND e.conflict_status = 'pending_llm_review' "
            "RETURN e.conflict_group_id AS gid, count(e) AS cnt, "
            "min(e.created_at) AS oldest "
            "GROUP BY e.conflict_group_id "
            "ORDER BY oldest "
            "LIMIT 20"
        )
        for r in result:
            print(f"  Group: {r['gid']} count={r['cnt']} oldest={r['oldest']}")

        # --- Flagged structural nodes ---
        print("\n--- Flagged structural nodes ---")
        result = session.run(
            "MATCH (e:Entity {flagged: true}) "
            "WHERE e.structure_role IS NOT NULL "
            "RETURN e.uuid AS uuid, e.name AS name, e.structure_role AS role LIMIT 20"
        )
        count = 0
        for r in result:
            print(f"  {r['uuid'][:8]}... name={r['name']} role={r['role']}")
            count += 1
        if count == 0:
            print("  None found (already clean)")

        # --- Unflagged structural nodes (that should be cleaned) ---
        result = session.run(
            "MATCH (e:Entity {flagged: true}) "
            "WHERE e.group_id STARTS WITH 'structure_' "
            "RETURN e.uuid AS uuid, e.name AS name, e.group_id AS gid LIMIT 20"
        )
        count2 = 0
        for r in result:
            print(f"  FLAGGED structural: {r['uuid'][:8]}... name={r['name']} gid={r['gid']}")
            count2 += 1
        if count2 == 0:
            print("  No flagged structural nodes with group_id prefix (clean)")

    driver.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
