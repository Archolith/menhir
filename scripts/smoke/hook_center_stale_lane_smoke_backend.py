"""In-process real backend for the Hook Center stale-anchor lane smoke.

Extracted verbatim from ``hook_center_stale_lane_smoke.py``. REAL RecallService /
ContextBuilder / formatter against a REAL throwaway Neo4j. Only graphiti vector
search is seeded.
"""

from __future__ import annotations

from hook_center_stale_lane_smoke_constants import MEMORY_SENTINEL, QUERY


class _SeededGraphiti:
    """Stub for the embedding-dependent vector search only. It decides which memory
    is a candidate; every downstream label/advisory/enrichment step is the real code
    path against real Cypher."""

    def __init__(self, hits: list[tuple[str, str, float]]) -> None:
        self._hits = list(hits)

    async def search_scored(self, query: str, *, num_results: int = 50, group_ids=None):
        return list(self._hits)


class Neo4jBackend:
    """Real Neo4j-backed operations for fixtures + in-process recall/context/formatter."""

    def __init__(self, uri: str, user: str, password: str, database: str) -> None:
        from menhir.infrastructure.neo4j import Neo4jRepository
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

        self._neo = Neo4jRepository(uri, database, user, password)
        self.adapter = MemoryGraphAdapter(self._neo)

    # -- lifecycle ---------------------------------------------------------

    def ping(self) -> bool:
        return self._neo.ping()

    def close(self) -> None:
        self._neo.close()

    def any_smoke_data(self, project: str, uuids: list[str]) -> bool:
        # Scope entirely by the smoke_project marker (set on every fixture node) +
        # the verification receipt's project — never by UUID, so a custom
        # --memory-uuid can never widen the blast radius.
        rows = self._neo.execute(
            """
            OPTIONAL MATCH (n:Entity {smoke_project: $project})
            OPTIONAL MATCH (v:StaleAnchorVerification {project: $project})
            RETURN count(DISTINCT n) + count(DISTINCT v) AS total
            """,
            params={"project": project},
        )
        return bool(rows and int(rows[0].get("total") or 0) > 0)

    def clean(self, project: str, uuids: list[str]) -> None:
        """Delete ONLY smoke-project-scoped data. Every fixture node carries a
        ``smoke_project`` marker, so cleanup deletes strictly by that marker (and the
        receipt's project) — never by bare UUID. Never touches other projects and
        never clears dirty flags globally."""
        self._neo.execute(
            "MATCH (v:StaleAnchorVerification {project: $project}) DETACH DELETE v",
            params={"project": project},
        )
        self._neo.execute(
            "MATCH (n:Entity {smoke_project: $project}) DETACH DELETE n",
            params={"project": project},
        )

    # -- fixture -----------------------------------------------------------

    def create_fixture(self, project: str, path: str, memory_uuid: str,
                       control_uuid: str, anchored_at: str) -> None:
        """SMOKE FIXTURE ONLY — not app behavior.

        Creates the smallest real graph that yields a stale file anchor:
          (:Entity {structure_role:'file'}) <-[:ANCHORED_TO {created_at}]- (:Entity memory)
        plus a non-stale control memory. dirty_at is NOT set here — it is set by the
        real POST /api/tool-events path so the stale signal is produced by the server."""
        self._neo.execute(
            """
            // SMOKE FIXTURE ONLY — not app behavior
            // Every node carries smoke_project so teardown can delete strictly by marker.
            CREATE (f:Entity {
                uuid: randomUUID(), structure_role: 'file',
                structure_path: $path, structure_project: $project, name: $path,
                smoke_project: $project
            })
            CREATE (sem:Entity {
                uuid: $memory_uuid, name: 'smoke stale-anchored memory ' + $sentinel,
                scope: 'PERSISTENT', type: 'SEMANTIC', freshness: 'ACTIVE',
                content: 'Smoke memory ' + $sentinel + ' anchored to ' + $path + ' describing its behavior.',
                summary: 'Smoke memory ' + $sentinel + ' anchored to a file that will change after anchoring.',
                namespace: 'default', smoke_project: $project, created_at: datetime($anchored_at)
            })
            CREATE (sem)-[:ANCHORED_TO {created_at: datetime($anchored_at)}]->(f)
            CREATE (ctrl:Entity {
                uuid: $control_uuid, name: 'smoke control memory',
                scope: 'PERSISTENT', type: 'SEMANTIC', freshness: 'ACTIVE',
                content: 'Smoke control memory, not anchored to any dirty file.',
                summary: 'Smoke control memory (never stale).',
                namespace: 'default', smoke_project: $project, created_at: datetime($anchored_at)
            })
            RETURN sem.uuid AS uuid
            """,
            params={"project": project, "path": path, "memory_uuid": memory_uuid,
                    "control_uuid": control_uuid, "anchored_at": anchored_at,
                    "sentinel": MEMORY_SENTINEL},
        )

    def file_event_metadata(self, project: str, path: str) -> dict:
        """Read the dirty-marking provenance written by POST /api/tool-events so the
        smoke can assert operation + hash were stored (the /dirty endpoint surfaces
        operation but not the hash)."""
        rows = self._neo.execute(
            """
            MATCH (f:Entity {structure_role: 'file', structure_project: $project, structure_path: $path})
            RETURN f.last_event_op AS operation, f.last_event_after_hash AS after_hash
            """,
            params={"project": project, "path": path},
        )
        return dict(rows[0]) if rows else {}

    # -- in-process real recall / context / formatter ----------------------

    def _recall_service(self, project: str, hits: list[tuple[str, str, float]]):
        from menhir.services.recall_service import RecallService
        from menhir.services.scoring_service import ScoringService

        return RecallService(
            graphiti_client=_SeededGraphiti(hits),
            graph_adapter=self.adapter,
            scoring_service=ScoringService(),
        )

    async def recall_items(self, project: str, hits: list[tuple[str, str, float]]) -> list[dict]:
        """Run the REAL recall path and return, per result, the raw recall stale label
        plus the REAL MCP formatter output."""
        from menhir.mcp.formatters import _compact_scored_item

        svc = self._recall_service(project, hits)
        result = await svc.recall(QUERY, file_context_project=project, update_access=False)
        items = []
        for sm in result.results:
            items.append({
                "uuid": sm.uuid,
                "stale_anchor_info": sm.stale_anchor_info,      # recall-labeled truth
                "formatted": _compact_scored_item(sm, compact=False),  # real formatter
            })
        return items

    async def build_context(self, project: str, hits: list[tuple[str, str, float]],
                            max_tokens: int) -> str:
        from menhir.services.context_builder import ContextBuilderService

        svc = ContextBuilderService(recall_service=self._recall_service(project, hits),
                                    graph_adapter=self.adapter)
        result = await svc.build_context(QUERY, max_tokens=max_tokens)
        return result.context

    # -- lifecycle assertions for the no-mutation checks -------------------

    def memory_exists(self, memory_uuid: str) -> bool:
        rows = self._neo.execute(
            "MATCH (n:Entity {uuid: $uuid}) RETURN count(n) AS c",
            params={"uuid": memory_uuid},
        )
        return bool(rows and int(rows[0].get("c") or 0) > 0)

    def dirty_flag_set(self, project: str, path: str) -> bool:
        rows = self._neo.execute(
            """
            MATCH (f:Entity {structure_role: 'file', structure_project: $project, structure_path: $path})
            RETURN f.structure_dirty AS dirty
            """,
            params={"project": project, "path": path},
        )
        return bool(rows and rows[0].get("dirty") is True)

    def stale_count(self, project: str) -> int:
        return len(self.adapter.stale_anchored_memories(project=project, limit=200))
