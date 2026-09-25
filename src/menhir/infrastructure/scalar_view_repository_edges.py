"""Atomic edge-rewrite operations for the scalar View repository.

Extracted verbatim from ``scalar_view_repository.py``; the composed ``ScalarViewRepositoryMixin``
picks these operations up through inheritance, so every existing import site is unchanged.
"""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.view_models import normalize_history_entry


class ScalarViewEdgeOpsMixin:
    """Single-statement edge redraws for provenance and history-entry relations."""

    def draw_scalar_state_provenance_edges(
        self, *, view_uuid: str, anchor_id: str | None,
        contributed_delta_ids: list[str], superseded_anchor_ids: list[str],
    ) -> dict[str, int]:
        """Phase 3 (decision 7.D/§10.D, G11): rewrite a scalar_state View's provenance edges to the
        exact fold inputs, LABELED to match the fold's algebra (a COMPUTED head has no single source):
          - `CURRENT_ANCHOR`   -> the anchoring absolute the head is computed from,
          - `CONTRIBUTED_TO`   -> each APPLIED delta (a LIVE input to the current value),
          - `SUPERSEDED_ANCHOR`-> each EXCLUDED prior anchor (a replaced absolute / a pre-anchor
                                  unapplied delta) -- supersession is reserved for exclusion, never a
                                  live contributor.
        ATOMIC + idempotently crash-repairable (G11): the whole managed edge set is DELETED and redrawn
        in ONE statement/transaction, so a rebuild never leaves two `CURRENT_ANCHOR`s or a stale
        `CONTRIBUTED_TO`, and a mid-rebuild crash is repaired by the next rebuild's redraw. Edges MERGE
        onto EXISTING :TypedAssertion nodes only (MATCH, never MERGE a node -- never creates a stub).
        Returns the drawn counts per label."""
        anchor_ids = [anchor_id] if anchor_id else []
        rows = self.neo4j.execute(
            """
            MATCH (v:Entity {uuid: $view_uuid})
            OPTIONAL MATCH (v)-[r:CURRENT_ANCHOR|CONTRIBUTED_TO|SUPERSEDED_ANCHOR]->()
            DELETE r
            WITH DISTINCT v
            CALL {
                WITH v
                UNWIND $anchor_ids AS aid
                MATCH (a:TypedAssertion {assertion_id: aid})
                MERGE (v)-[:CURRENT_ANCHOR]->(a)
                RETURN count(*) AS ca
            }
            CALL {
                WITH v
                UNWIND $delta_ids AS did
                MATCH (d:TypedAssertion {assertion_id: did})
                MERGE (v)-[:CONTRIBUTED_TO]->(d)
                RETURN count(*) AS cd
            }
            CALL {
                WITH v
                UNWIND $superseded_ids AS sid
                MATCH (s:TypedAssertion {assertion_id: sid})
                MERGE (v)-[:SUPERSEDED_ANCHOR]->(s)
                RETURN count(*) AS cs
            }
            RETURN ca, cd, cs
            """,
            {"view_uuid": view_uuid, "anchor_ids": anchor_ids,
             "delta_ids": list(contributed_delta_ids or []),
             "superseded_ids": list(superseded_anchor_ids or [])},
        )
        r = rows[0] if rows else {}
        return {"current_anchor": int(r.get("ca") or 0),
                "contributed_to": int(r.get("cd") or 0),
                "superseded_anchor": int(r.get("cs") or 0)}

    def draw_scalar_history_entries(
        self, *, view_uuid: str, entries: list[Any],
    ) -> dict[str, int]:
        """Atomically rewrite a scalar_history View's HISTORY_ENTRY edges to the exact ordered
        assertion set. Removes stale edges and merges the complete ordered current set in one
        transaction so a crash never leaves a mixed contributor set.

        Each edge carries: ordinal (0-based position), operation, valid_at.

        Returns drawn edge count."""
        if not entries:
            # Clear any existing HISTORY_ENTRY edges.
            self.neo4j.execute(
                """
                MATCH (v:Entity {uuid: $view_uuid})-[r:HISTORY_ENTRY]->()
                DELETE r
                """,
                {"view_uuid": view_uuid},
            )
            return {"history_entries": 0}

        # Normalize and validate the entire set BEFORE the query deletes any prior edges.
        # This accepts the real HistoryEntry dataclass as well as arbitrary Mapping objects.
        normalized = [normalize_history_entry(e, index=i) for i, e in enumerate(entries)]
        entry_params = [
            {
                "assertion_id": e["assertion_id"],
                "ordinal": i,
                "operation": e["operation"],
                "valid_at": e["valid_at"],
            }
            for i, e in enumerate(normalized)
        ]

        rows = self.neo4j.execute(
            """
            MATCH (v:Entity {uuid: $view_uuid})
            OPTIONAL MATCH (v)-[r:HISTORY_ENTRY]->()
            DELETE r
            WITH DISTINCT v
            UNWIND $entries AS entry
            MATCH (a:TypedAssertion {assertion_id: entry.assertion_id})
            MERGE (v)-[he:HISTORY_ENTRY {ordinal: entry.ordinal}]->(a)
            SET he.operation = entry.operation, he.valid_at = entry.valid_at
            RETURN count(*) AS drawn
            """,
            {"view_uuid": view_uuid, "entries": entry_params},
        )
        drawn = int(rows[0].get("drawn") or 0) if rows else 0
        return {"history_entries": drawn}
