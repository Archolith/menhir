"""Neo4j queries for the personal-memory perception consolidation job.

The consolidation re-derives count/amount Views ("bike spend", "fish tanks owned") from a namespace's
USER episodes. To keep a short-interval pass cheap it only touches DIRTY namespaces — those with user
turns newer than the last consolidation. A per-namespace `(:ConsolidationWatermark)` records the last
run so a namespace where perception *abstained* (wrote no View) is still debounced and not re-LLM'd
every cycle until new user turns arrive.
"""

from __future__ import annotations

from typing import Any


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
        batch queries read this same :ScalarConsolidationWatermark."""
        self._neo4j.execute(
            """
            MERGE (w:ScalarConsolidationWatermark {group_id: $ns})
            SET w.cursor_at = datetime($cursor_at), w.cursor_uuid = $cu,
                w.perceiver_version = $pv, w.last_run_at = datetime($at)
            """,
            params={"ns": namespace, "cursor_at": cursor_at, "cu": cursor_uuid,
                    "pv": str(perceiver_version), "at": at},
        )

