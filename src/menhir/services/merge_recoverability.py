"""Read-only inventory of what merge history can ACTUALLY be recovered (plan section 10).

The point of this module is to make operator expectations match reality. Production carries thousands
of absorbed-node lineage references but only a few hundred snapshots, so most historical merges are
NOT reversible -- and the worst possible outcome is an operator discovering that during an incident.

It classifies, and it never invents. In particular it will not synthesize an unmerge snapshot from
the survivor's current state: the absorbed node's before-state is unknown, and fabricating one would
manufacture false confidence in reversibility.

Tiers, most to least recoverable:

    EXACT              a journaled ENTITY_MERGE with a complete, versioned snapshot (Phase 4+).
                       Fully reversible via UnmergeCoordinator.
    LEGACY_SIDECAR     a lossy pre-journal snapshot in the durable telemetry sidecar.
                       Partially reversible via LegacyUnmergeCoordinator (degraded, manifest-gated).
    GRAPH_SNAPSHOT_ONLY a lossy snapshot that exists ONLY as a survivor.merge_audit property.
                       A WASTING ASSET: it dies with the survivor, and decay/delete can remove that
                       node at any time. Should be copied into the sidecar (backfill_merge_audit.py).
    LINEAGE_ONLY       survivor.merged_from records the absorption, but no snapshot exists anywhere.
                       NOT RECOVERABLE. Reported so nobody believes otherwise.
    MALFORMED          a snapshot exists but cannot be parsed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from menhir.domain import legacy_snapshot as ls

logger = logging.getLogger(__name__)

EXACT = "EXACT"
LEGACY_SIDECAR = "LEGACY_SIDECAR"
GRAPH_SNAPSHOT_ONLY = "GRAPH_SNAPSHOT_ONLY"
LINEAGE_ONLY = "LINEAGE_ONLY"
MALFORMED = "MALFORMED"

TIERS = (EXACT, LEGACY_SIDECAR, GRAPH_SNAPSHOT_ONLY, LINEAGE_ONLY, MALFORMED)


@dataclass
class RecoverabilityReport:
    counts: dict[str, int] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def recoverable_exactly(self) -> int:
        return self.counts.get(EXACT, 0)

    @property
    def not_recoverable(self) -> int:
        return self.counts.get(LINEAGE_ONLY, 0) + self.counts.get(MALFORMED, 0)

    def summary(self) -> str:
        lines = ["Merge recoverability inventory (read-only):", ""]
        for tier in TIERS:
            lines.append(f"  {tier:<20} {self.counts.get(tier, 0)}")
        lines += [
            "",
            f"  Exactly reversible:     {self.recoverable_exactly}",
            f"  NOT recoverable:        {self.not_recoverable}",
            "",
        ]
        if self.counts.get(GRAPH_SNAPSHOT_ONLY):
            lines.append(
                f"  WARNING: {self.counts[GRAPH_SNAPSHOT_ONLY]} snapshot(s) exist only as a "
                "survivor.merge_audit property."
            )
            lines.append(
                "  That property dies with the survivor (decay/delete can remove it), so this is a "
                "WASTING asset."
            )
            lines.append("  Preserve them now:  python scripts/backfill_merge_audit.py")
            lines.append("")
        if self.not_recoverable:
            lines.append(
                f"  {self.not_recoverable} absorption(s) have NO usable snapshot and cannot be "
                "reversed by any tool."
            )
            lines.append(
                "  Their absorbed before-state is unknown; it will not be reconstructed from the "
                "survivor."
            )
        return "\n".join(lines)


def inventory(*, neo4j: Any, journal: Any, telemetry_store: Any = None) -> RecoverabilityReport:
    """Classify every recorded absorption. Read-only: performs no writes of any kind."""
    if telemetry_store is None:
        from menhir.infrastructure.telemetry import telemetry_store as default_store

        telemetry_store = default_store

    # 1. Journaled merges with a complete snapshot -> exactly reversible.
    exact: dict[str, dict[str, Any]] = {}
    for row in journal.list_by_state("COMMITTED", limit=1_000_000):
        if row.get("operation_kind") != "ENTITY_MERGE" or not row.get("before_snapshot_json"):
            continue
        try:
            req = json.loads(row["request_json"])
        except (TypeError, ValueError):
            continue
        exact[str(req["absorbed_uuid"])] = {
            "tier": EXACT,
            "absorbed_uuid": req["absorbed_uuid"],
            "survivor_uuid": req["survivor_uuid"],
            "merge_op_id": row["op_id"],
        }

    # 2. Lossy snapshots in the durable sidecar -> degraded lane.
    sidecar: dict[str, dict[str, Any]] = {}
    malformed: dict[str, dict[str, Any]] = {}
    for row in telemetry_store.fetch_merge_audit(limit=1_000_000):
        absorbed = str(row["absorbed_uuid"])
        if absorbed in exact:
            continue
        try:
            ls.parse(row["snapshot_json"])
        except ls.LegacySnapshotError as exc:
            malformed[absorbed] = {
                "tier": MALFORMED, "absorbed_uuid": absorbed,
                "survivor_uuid": row["survivor_uuid"], "error": str(exc),
            }
            continue
        sidecar[absorbed] = {
            "tier": LEGACY_SIDECAR, "absorbed_uuid": absorbed,
            "survivor_uuid": row["survivor_uuid"],
        }

    # 3. Every absorption the GRAPH still claims, with whatever snapshot lives on the survivor.
    graph_rows = neo4j.execute(
        """
        MATCH (s:Entity)
        WHERE s.merged_from IS NOT NULL AND size(s.merged_from) > 0
        RETURN s.uuid AS survivor_uuid,
               s.merged_from AS merged_from,
               coalesce(s.merge_audit, []) AS merge_audit
        """
    )

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in graph_rows:
        survivor = str(row["survivor_uuid"])
        audit_blobs = [str(x) for x in (row.get("merge_audit") or [])]
        for absorbed in [str(x) for x in (row.get("merged_from") or [])]:
            if absorbed in seen:
                continue
            seen.add(absorbed)
            if absorbed in exact:
                rows.append(exact[absorbed])
            elif absorbed in sidecar:
                rows.append(sidecar[absorbed])
            elif absorbed in malformed:
                rows.append(malformed[absorbed])
            elif any(absorbed in blob for blob in audit_blobs):
                # A snapshot exists, but ONLY on the survivor node -- it dies with it.
                rows.append({
                    "tier": GRAPH_SNAPSHOT_ONLY,
                    "absorbed_uuid": absorbed,
                    "survivor_uuid": survivor,
                })
            else:
                rows.append({
                    "tier": LINEAGE_ONLY,
                    "absorbed_uuid": absorbed,
                    "survivor_uuid": survivor,
                })

    # Snapshots whose survivor is gone from the graph still count: the sidecar outlives the node,
    # which is the entire reason it exists.
    for absorbed, rec in list(exact.items()) + list(sidecar.items()) + list(malformed.items()):
        if absorbed not in seen:
            seen.add(absorbed)
            rows.append(rec)

    counts = {tier: 0 for tier in TIERS}
    for r in rows:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    return RecoverabilityReport(counts=counts, rows=rows)
