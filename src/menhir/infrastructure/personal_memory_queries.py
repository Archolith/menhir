"""Neo4j watermark queries for the personal-memory perception consolidation job.

A per-namespace `(:ConsolidationWatermark)` records the last consolidation run, so a namespace where
perception *abstained* (wrote no View) is still debounced and not re-LLM'd every cycle until new user
turns arrive. Dirty-namespace discovery and loading of user turns read :TurnEvidence and live in
turn_evidence_repository (#271).
"""

from __future__ import annotations

from typing import Any

from menhir.domain.namespace import turn_evidence_cursor_keys
from menhir.infrastructure.turn_evidence_repository import advance_keys_cypher


class PersonalMemoryRepository:
    """Consolidation watermarks. Dirty-namespace discovery reads :TurnEvidence (#271)."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    def mark_consolidated(self, namespace: str, *, at: str) -> None:
        """Stamp the namespace's consolidation watermark so it is not re-processed until a newer user
        turn arrives. Called after every namespace pass — commit OR abstain — to debounce both."""
        self._neo4j.execute(
            """
            MERGE (w:ConsolidationWatermark {group_id: $ns})
            SET w.last_run_at = datetime($at)
            """,
            params={"ns": namespace, "at": at},
        )

    def advance_scalar_cursor(
        self, namespace: str, *, cursor_at: str, cursor_uuid: str,
        perceiver_version: str, at: str,
    ) -> None:
        """Advance the namespace's scalar cursor to the last evidence ACTUALLY processed, keyed on its
        monotonic work-discovery `cursor_at` (NOT world-time), stamping the perceiver_version. Called
        after each processed batch -- never on activation refusal -- so a partial backfill resumes
        exactly where it stopped and never skips the untouched tail. The TurnEvidence discovery and
        batch queries read this same :ScalarConsolidationWatermark. Every stored spelling of the
        silo keeps its own key and moves together, never backward (#170)."""
        self._neo4j.execute(
            """
            UNWIND $keys AS k
            MERGE (w:ScalarConsolidationWatermark {group_id: k})
            """ + advance_keys_cypher(),
            params={"keys": turn_evidence_cursor_keys(namespace), "cursor_at": cursor_at,
                    "cu": cursor_uuid, "pv": str(perceiver_version), "at": at},
        )

