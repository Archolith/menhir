"""Erasure-aware archive reads for original (pre-compression) content."""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


class TelemetryRecallArchiveMixin:
    def _erasure_suppresses(self, node_uuid: str) -> bool:
        """Whether a live erasure covers this node. Fails CLOSED.

        A lookup that raises means suppressed: a veto that failed open would serve exactly the
        content it exists to withhold, which is a worse outcome than a missing archive read.
        """
        from menhir.infrastructure.erasure_subjects import suppressed_node_uuids

        return bool(suppressed_node_uuids(self.db_path, [node_uuid]))

    def get_original_content(self, node_uuid: str) -> str | None:
        """Retrieve the original (pre-compression) content for a node from the revision archive.

        Returns the `old_value` of the earliest `memory_revisions` row where
        `node_uuid` matches and `field = 'content'`. This is the content before
        the first lossy rewrite (compression).

        Returns None if no such row exists (archive miss / node was never compressed).
        Intended for rehydration recovery: use this archive content instead of
        LLM-merging the compressed summary alone, preventing photocopy-loss from
        repeated compress/rehydrate cycles.
        """
        if not node_uuid:
            return None
        self._ensure_ready()
        # CF-165 read veto. This query is the finding's own proof that erased content stayed
        # readable: it is keyed on node_uuid alone, with no graph-existence check, so it answers
        # for a node that was deleted. An erasure that has committed its intent but not finished
        # purging must already suppress it -- otherwise the window between the two is exactly
        # when the supposedly erased text is served.
        #
        # Checked against the subject inventory directly rather than through services'
        # ErasureVeto: infrastructure importing services would invert the layering. The predicate
        # is the same one that veto calls.
        if self._erasure_suppresses(node_uuid):
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT old_value FROM memory_revisions
                    WHERE node_uuid = ? AND field = 'content'
                    ORDER BY recorded_at ASC
                    LIMIT 1
                    """,
                    (node_uuid,),
                ).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            logger.warning(
                "Failed to fetch original content for node=%s",
                node_uuid,
                exc_info=True,
            )
            return None
