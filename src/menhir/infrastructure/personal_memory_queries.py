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
    """Dirty-namespace detection, user-episode loading, and the consolidation watermark."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    def list_dirty_namespaces(self, *, limit: int = 200) -> list[str]:
        """Namespaces whose newest USER episode is newer than their consolidation watermark (or that
        have never been consolidated). Only namespaces that actually contain user turns qualify, so
        agent-* namespaces (no user turns) are naturally excluded."""
        rows = self._neo4j.execute(
            """
            MATCH (e:Episodic)
            WHERE e.content STARTS WITH 'user:' AND e.group_id IS NOT NULL
            WITH e.group_id AS ns, max(coalesce(e.created_at, e.valid_at)) AS newest_ep
            WHERE newest_ep IS NOT NULL
            OPTIONAL MATCH (w:ConsolidationWatermark {group_id: ns})
            WITH ns, newest_ep, w.last_run_at AS watermark
            WHERE watermark IS NULL OR newest_ep > watermark
            RETURN ns AS namespace
            ORDER BY newest_ep DESC
            LIMIT $limit
            """,
            params={"limit": int(limit)},
        )
        return [str(r["namespace"]) for r in rows]

    def load_user_episodes(self, namespace: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """USER-turn episodes for a namespace (real uuids -> real MENTIONS provenance), oldest first,
        so the fold sees the timeline in order. Returns raw rows; the caller formats Episode content."""
        rows = self._neo4j.execute(
            """
            MATCH (e:Episodic {group_id: $ns})
            WHERE e.content STARTS WITH 'user:'
            RETURN e.uuid AS uuid, toString(e.valid_at) AS valid_at, e.content AS content
            ORDER BY e.valid_at
            LIMIT $limit
            """,
            params={"ns": namespace, "limit": int(limit)},
        )
        return [dict(r) for r in rows]

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

    # ---- scalar-state consolidation cursor (ScalarStateView C.4.3) ----------------------------
    #
    # The typed-scalar shadow path (enable_scalar_state) has its OWN scheduling state, SEPARATE from
    # the counter `ConsolidationWatermark`. Sharing the counter watermark would let counter
    # consolidation double as proof the scalar sink ran (enabling the feature after namespaces were
    # counter-consolidated would skip all historical backfill; a scalar-activation failure would be
    # debounced and never retried). The scalar state is a per-namespace :ScalarConsolidationWatermark
    # that stores a RESUMABLE CURSOR plus the perceiver_version that produced it, NOT a "last run at"
    # timestamp — truncation-safe, so a namespace larger than one batch advances the cursor only
    # through the episodes actually processed and stays scalar-dirty until the final page. A newer
    # perceiver_version resets the cursor (dirty from the beginning) so a version bump revisits history.
    #
    # CRITICAL: the cursor key is `cursor_at = coalesce(created_at, valid_at)` — a MONOTONIC
    # work-discovery identity (INGESTION/finalization order), NOT world-time. Ordering the cursor on
    # world-time `valid_at` would strand a late-finalized episode whose reference date is in the past:
    # it would sort BEHIND an already-passed cursor and never be discovered as dirty. `created_at`
    # only ever increases as episodes finalize, so a newly-available episode always sorts AFTER the
    # cursor. This key is DISTINCT from the assertion's semantic `valid_at` (the genuine world-time,
    # possibly null), which the loader returns separately so it is never conflated with the cursor.

    def list_scalar_dirty_namespaces(self, *, perceiver_version: str, limit: int = 200) -> list[str]:
        """Namespaces with at least one USER episode BEYOND the scalar cursor for `perceiver_version`
        — never scalar-consolidated, consolidated by a DIFFERENT perceiver_version (reset), or
        carrying episodes whose work-discovery key `coalesce(created_at, valid_at)` is past the stored
        `cursor_at`. Independent of counter consolidation."""
        rows = self._neo4j.execute(
            """
            MATCH (e:Episodic)
            WHERE e.content STARTS WITH 'user:' AND e.group_id IS NOT NULL
            OPTIONAL MATCH (w:ScalarConsolidationWatermark {group_id: e.group_id})
            WITH e.group_id AS ns, w,
                 coalesce(e.created_at, e.valid_at) AS ckey, e.uuid AS euuid
            WITH ns,
                 max(CASE
                     WHEN w IS NULL OR w.perceiver_version IS NULL
                          OR w.perceiver_version <> $pv OR w.cursor_at IS NULL
                          OR ckey > w.cursor_at
                          OR (ckey = w.cursor_at AND euuid > w.cursor_uuid)
                     THEN 1 ELSE 0 END) AS unprocessed
            WHERE unprocessed = 1
            RETURN ns AS namespace
            ORDER BY ns
            LIMIT $limit
            """,
            params={"pv": str(perceiver_version), "limit": int(limit)},
        )
        return [str(r["namespace"]) for r in rows]

    def load_next_scalar_batch(
        self, namespace: str, *, perceiver_version: str, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """The next page of USER episodes for a namespace AFTER its scalar cursor (for
        `perceiver_version`), oldest first by the WORK-DISCOVERY key `(coalesce(created_at, valid_at),
        uuid)` — ingestion order, so a late-finalized episode is never stranded behind a past
        world-time. A cursor stamped by a different perceiver_version is IGNORED (reset). Each row
        carries TWO distinct times: `cursor_at` (the monotonic key the caller advances the cursor
        with) and `valid_at` (the GENUINE world-time, possibly null, for assertion time-basis
        resolution) — never conflate them. Bounded by `limit` — a full page means more may remain."""
        rows = self._neo4j.execute(
            """
            OPTIONAL MATCH (w:ScalarConsolidationWatermark {group_id: $ns})
            WITH w, (w IS NULL OR w.perceiver_version IS NULL OR w.perceiver_version <> $pv) AS reset
            WITH CASE WHEN reset THEN null ELSE w.cursor_at END AS cca,
                 CASE WHEN reset THEN null ELSE w.cursor_uuid END AS cu
            MATCH (e:Episodic {group_id: $ns})
            WHERE e.content STARTS WITH 'user:'
            WITH e, cca, cu, coalesce(e.created_at, e.valid_at) AS ckey
            WHERE cca IS NULL OR ckey > cca OR (ckey = cca AND e.uuid > cu)
            RETURN e.uuid AS uuid,
                   toString(e.valid_at) AS valid_at,
                   toString(ckey) AS cursor_at,
                   e.content AS content
            ORDER BY ckey, e.uuid
            LIMIT $limit
            """,
            params={"ns": namespace, "pv": str(perceiver_version), "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    def advance_scalar_cursor(
        self, namespace: str, *, cursor_at: str, cursor_uuid: str,
        perceiver_version: str, at: str,
    ) -> None:
        """Advance the namespace's scalar cursor to the last episode ACTUALLY processed, keyed on its
        monotonic work-discovery `cursor_at` (NOT world-time), stamping the perceiver_version. Called
        after each processed batch — never on activation refusal — so a partial backfill resumes
        exactly where it stopped and never skips the untouched tail."""
        self._neo4j.execute(
            """
            MERGE (w:ScalarConsolidationWatermark {group_id: $ns})
            SET w.cursor_at = datetime($cursor_at), w.cursor_uuid = $cu,
                w.perceiver_version = $pv, w.last_run_at = datetime($at)
            """,
            params={"ns": namespace, "cursor_at": cursor_at, "cu": cursor_uuid,
                    "pv": str(perceiver_version), "at": at},
        )
