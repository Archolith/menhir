"""Capture stats for the Phase 3 debug report.

Split out of ``turn_evidence_repository.py`` (file-size refactor). ``TurnEvidenceRepository``
composes ``TurnEvidenceStatsMixin``; methods run against the facade's ``self._neo4j``.
"""

from __future__ import annotations

from typing import Any


class TurnEvidenceStatsMixin:
    """The ``evidence_stats`` debug-report aggregation over `:TurnEvidence`."""

    def evidence_stats(self) -> dict[str, Any]:
        """Capture stats for the Phase 3 debug report, including triage breakdowns.

        CF-114: was SIX sequential full-label scans of `:TurnEvidence`, one per metric, each its own
        round trip. Now THREE, and every aggregation stays server-side.

        The obvious collapse -- one pass that `collect()`s the grouped fields and counts them in
        Python -- was implemented, measured, and rejected. It trades scan count for unbounded
        transfer, which is the wrong trade for a label the entry itself says grows without bound
        (Neo4j 5.26.21, test instance):

            n=50,000   6 queries (before)   400,007 dbHits    20 rows over wire    105 ms
                       1 query  (collect)   300,001 dbHits   250,000 values        303 ms
                       3 queries (this)     400,003 dbHits    15 rows over wire    100 ms

        The `collect` version is 3x slower at 50k and degrades further with N. This shape keeps the
        payload bounded by DISTINCT-value cardinality rather than row count.

        `role` and `triage_version` are folded into one composite grouping, and the marginals plus
        `total` and `latest` are derived from it -- correct because summing a partition gives the
        whole, and max-of-maxes is the max. `source_kind` keeps its own query so its `LIMIT 10`
        stays server-side. `triage_reason` keeps its own because `UNWIND` on a list property
        multiplies cardinality and would corrupt every other aggregate sharing the pass.

        Ordering (count DESC) and the `source_kind` top-10 truncation are unchanged, so the
        returned dict is identical in shape and content to the six-query version.
        """
        composite = self._neo4j.execute(
            """
            MATCH (t:TurnEvidence)
            RETURN t.role AS role, t.triage_version AS version, count(*) AS c,
                   toString(max(t.recorded_at)) AS latest
            """
        )

        total = 0
        role_counts: dict[str, int] = {}
        version_counts: dict[str, int] = {}
        latest: str | None = None
        for row in composite:
            count = int(row["c"] or 0)
            total += count
            role_counts[str(row["role"])] = role_counts.get(str(row["role"]), 0) + count
            version_counts[str(row["version"])] = version_counts.get(str(row["version"]), 0) + count
            row_latest = row["latest"]
            if row_latest is not None and (latest is None or str(row_latest) > latest):
                latest = str(row_latest)

        def _desc(counts: dict[str, int]) -> dict[str, int]:
            return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

        by_role = _desc(role_counts)
        by_version = _desc(version_counts)

        by_source: dict[str, int] = {}
        for row in self._neo4j.execute(
            "MATCH (t:TurnEvidence) RETURN t.source_kind AS sk, count(t) AS c ORDER BY c DESC LIMIT 10"
        ):
            by_source[str(row["sk"])] = int(row["c"])

        by_reason: dict[str, int] = {}
        for row in self._neo4j.execute(
            "MATCH (t:TurnEvidence) UNWIND t.triage_reason AS reason "
            "RETURN reason AS reason, count(*) AS c ORDER BY c DESC"
        ):
            by_reason[str(row["reason"])] = int(row["c"])

        return {
            "turn_evidence_table_exists": total > 0,
            "total_turn_evidence": total,
            "by_role": by_role,
            "by_source_kind": by_source,
            "claude_code_hook_evidence": int(by_source.get("claude_code_hook", 0)),
            "triage_version_counts": by_version,
            "triage_reason_counts": by_reason,
            "user_evidence": int(by_role.get("user", 0)),
            "latest_recorded_at": latest,
        }
