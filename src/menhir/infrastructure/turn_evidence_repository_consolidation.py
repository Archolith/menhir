"""Scalar- and event-consolidation discovery over `:TurnEvidence`.

Split out of ``turn_evidence_repository.py`` (file-size refactor). ``TurnEvidenceRepository``
composes ``TurnEvidenceConsolidationMixin``; methods run against the facade's ``self._neo4j``.
"""

from __future__ import annotations

from typing import Any


class TurnEvidenceConsolidationMixin:
    """Cursor-driven work discovery for the scalar (G14) and event consolidation paths."""

    # ---- scalar-consolidation discovery (G14 bridge) -----------------------------------------
    # The typed-scalar path (ScalarStateView C.4.3) discovered work via :Episodic 'user:' ONLY, so in
    # a Turn-capturing production box it saw NO user input and its declarant foundation was unreachable
    # (G14/F2). These mirror the counter-path switch: when :TurnEvidence exists, scalar discovery reads
    # it, using the SAME per-namespace :ScalarConsolidationWatermark cursor (keyed by the namespace
    # string in `group_id`) as the Episodic path -- so a box transitions with no cursor reset. The
    # work-discovery key is `recorded_at` (monotonic capture time, always present). Imported/replayed
    # evidence may also carry `occurred_at` world time; it drives assertion validity while recorded_at
    # continues to drive cursor advancement. The loaded row's `uuid` is the `turn_id`, which the G14
    # grounding Cypher resolves as a :TurnEvidence anchor and FOUNDS the assertion.

    def list_scalar_dirty_evidence_namespaces(
        self, *, perceiver_version: str, limit: int = 200,
    ) -> list[str]:
        """Namespaces with at least one role=user :TurnEvidence BEYOND the scalar cursor for
        `perceiver_version` (never scalar-consolidated, a different perceiver_version, or evidence past
        the stored `cursor_at`). The :TurnEvidence analogue of PersonalMemoryRepository.
        list_scalar_dirty_namespaces; independent of counter consolidation."""
        rows = self._neo4j.execute(
            """
            MATCH (t:TurnEvidence)
            WHERE t.role = 'user' AND t.declarant = 'user' AND t.namespace IS NOT NULL
                  AND t.text IS NOT NULL AND t.text <> ''
            OPTIONAL MATCH (w:ScalarConsolidationWatermark {group_id: t.namespace})
            WITH t.namespace AS ns, w, t.recorded_at AS ckey, t.turn_id AS tuuid
            WITH ns,
                 max(CASE
                     WHEN w IS NULL OR w.perceiver_version IS NULL
                          OR w.perceiver_version <> $pv OR w.cursor_at IS NULL
                          OR ckey > w.cursor_at
                          OR (ckey = w.cursor_at AND tuuid > w.cursor_uuid)
                     THEN 1 ELSE 0 END) AS unprocessed
            WHERE unprocessed = 1
            RETURN ns AS namespace
            ORDER BY ns
            LIMIT $limit
            """,
            params={"pv": str(perceiver_version), "limit": int(limit)},
        )
        return [str(r["namespace"]) for r in rows]

    def load_next_scalar_evidence_batch(
        self, namespace: str, *, perceiver_version: str, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """The next page of role=user :TurnEvidence for a namespace AFTER its scalar cursor (for
        `perceiver_version`), oldest first by `(recorded_at, turn_id)`. A cursor stamped by a different
        perceiver_version is IGNORED (reset). Rows are shaped like PersonalMemoryRepository.
        load_next_scalar_batch: `uuid`=turn_id (the G14 grounding anchor), `cursor_at`=recorded_at (the
        monotonic key the caller advances the cursor with), `valid_at`=occurred_at when supplied and
        recorded_at otherwise (the assertion's world time), `content`=raw prompt text. Bounded by
        `limit`.

        valid_at MUST be non-null: a null `valid_at` forces the assertion to
        `learned_fallback` (the perception batch's `learned_at`), which is captured AFTER LLM extraction
        and therefore AFTER the rebuild's `as_of` (captured before extraction) -- so the fold's
        `valid_at <= as_of` filter EXCLUDES the just-learned assertion as "future" and NO View
        materializes on the first (and, absent a re-dirtying event, only) consolidation pass. Both the
        source time and receive-time fallback precede consolidation, so either folds normally."""
        rows = self._neo4j.execute(
            """
            OPTIONAL MATCH (w:ScalarConsolidationWatermark {group_id: $ns})
            WITH w, (w IS NULL OR w.perceiver_version IS NULL OR w.perceiver_version <> $pv) AS reset
            WITH CASE WHEN reset THEN null ELSE w.cursor_at END AS cca,
                 CASE WHEN reset THEN null ELSE w.cursor_uuid END AS cu
            MATCH (t:TurnEvidence {namespace: $ns})
            WHERE t.role = 'user' AND t.declarant = 'user' AND t.text IS NOT NULL AND t.text <> ''
            WITH t, cca, cu, t.recorded_at AS ckey
            WHERE cca IS NULL OR ckey > cca OR (ckey = cca AND t.turn_id > cu)
            RETURN t.turn_id AS uuid,
                   toString(coalesce(t.occurred_at, t.recorded_at)) AS valid_at,
                   toString(ckey) AS cursor_at,
                   t.text AS content
            ORDER BY ckey, t.turn_id
            LIMIT $limit
            """,
            params={"ns": namespace, "pv": str(perceiver_version), "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    # ---- event-consolidation discovery (event-history cursor) ----------------------------------
    # The typed-event path needs an INDEPENDENT, truncation-safe cursor over canonical user
    # TurnEvidence. It is a SEPARATE label (:EventConsolidationWatermark) keyed by the namespace string
    # in `group_id` — deliberately NOT :ScalarConsolidationWatermark or :ConsolidationWatermark — so
    # event consolidation advances without disturbing the scalar/counter cursors. Same truncation-safe
    # ordering as the scalar path: (recorded_at, turn_id). Only role=user, declarant=user, nonempty
    # text participates. `valid_at` = occurred_at or recorded_at; the caller advances the cursor with
    # the monotonic `cursor_at` (recorded_at), never world-time.

    def list_event_dirty_evidence_namespaces(
        self, *, perceiver_version: str, limit: int = 200,
    ) -> list[str]:
        """Namespaces with at least one role=user :TurnEvidence BEYOND the event cursor for
        `perceiver_version` (never event-consolidated, a different perceiver_version, or evidence past
        the stored `cursor_at`). Independent of scalar and counter consolidation."""
        rows = self._neo4j.execute(
            """
            MATCH (t:TurnEvidence)
            WHERE t.role = 'user' AND t.declarant = 'user' AND t.namespace IS NOT NULL
                  AND t.text IS NOT NULL AND t.text <> ''
            OPTIONAL MATCH (w:EventConsolidationWatermark {group_id: t.namespace})
            WITH t.namespace AS ns, w, t.recorded_at AS ckey, t.turn_id AS tuuid
            WITH ns,
                 max(CASE
                     WHEN w IS NULL OR w.perceiver_version IS NULL
                          OR w.perceiver_version <> $pv OR w.cursor_at IS NULL
                          OR ckey > w.cursor_at
                          OR (ckey = w.cursor_at AND tuuid > w.cursor_uuid)
                     THEN 1 ELSE 0 END) AS unprocessed
            WHERE unprocessed = 1
            RETURN ns AS namespace
            ORDER BY ns
            LIMIT $limit
            """,
            params={"pv": str(perceiver_version), "limit": int(limit)},
        )
        return [str(r["namespace"]) for r in rows]

    def load_next_event_evidence_batch(
        self, namespace: str, *, perceiver_version: str, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """The next page of role=user :TurnEvidence for a namespace AFTER its event cursor (for
        `perceiver_version`), oldest first by (recorded_at, turn_id). A cursor stamped by a different
        perceiver_version is IGNORED (reset). Rows are shaped like the scalar loader: `uuid`=turn_id,
        `cursor_at`=recorded_at (the monotonic key the caller advances the cursor with),
        `valid_at`=occurred_at when supplied and recorded_at otherwise, `content`=raw prompt text,
        plus generic source fields (source_kind, session_id) genuinely useful to future Episode
        construction. Bounded by `limit` — a full page means more may remain."""
        rows = self._neo4j.execute(
            """
            OPTIONAL MATCH (w:EventConsolidationWatermark {group_id: $ns})
            WITH w, (w IS NULL OR w.perceiver_version IS NULL OR w.perceiver_version <> $pv) AS reset
            WITH CASE WHEN reset THEN null ELSE w.cursor_at END AS cca,
                 CASE WHEN reset THEN null ELSE w.cursor_uuid END AS cu
            MATCH (t:TurnEvidence {namespace: $ns})
            WHERE t.role = 'user' AND t.declarant = 'user' AND t.text IS NOT NULL AND t.text <> ''
            WITH t, cca, cu, t.recorded_at AS ckey
            WHERE cca IS NULL OR ckey > cca OR (ckey = cca AND t.turn_id > cu)
            RETURN t.turn_id AS uuid,
                   toString(coalesce(t.occurred_at, t.recorded_at)) AS valid_at,
                   toString(ckey) AS cursor_at,
                   t.text AS content,
                   t.source_kind AS source_kind,
                   t.session_id AS session_id
            ORDER BY ckey, t.turn_id
            LIMIT $limit
            """,
            params={"ns": namespace, "pv": str(perceiver_version), "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    def advance_event_cursor(
        self, namespace: str, *, cursor_at: str, cursor_uuid: str,
        perceiver_version: str, at: str,
    ) -> None:
        """Advance the namespace's event cursor to the last TurnEvidence ACTUALLY processed, keyed on
        its monotonic `cursor_at` (recorded_at, NOT world-time), stamping the perceiver_version. The
        watermark is :EventConsolidationWatermark, keyed by the namespace string in `group_id` — fully
        independent of the scalar/counter cursors. Called after each processed batch so a partial
        backfill resumes exactly where it stopped."""
        self._neo4j.execute(
            """
            MERGE (w:EventConsolidationWatermark {group_id: $ns})
            SET w.cursor_at = datetime($cursor_at), w.cursor_uuid = $cu,
                w.perceiver_version = $pv, w.last_run_at = datetime($at)
            """,
            params={"ns": namespace, "cursor_at": cursor_at, "cu": cursor_uuid,
                    "pv": str(perceiver_version), "at": at},
        )
