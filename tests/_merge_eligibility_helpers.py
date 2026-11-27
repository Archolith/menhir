"""Shared builders for merge-eligibility tests."""

from __future__ import annotations

from menhir.domain import merge_eligibility as me

#: A repository row for an eligible node; tests override the lineage fields.
REPO_ROW_BASE = {
    "ineligible_role": False, "namespace": "default", "freshness": "ACTIVE",
    "scope": "SESSION", "user_flagged": False, "memory_retired": False,
    "source_retention_protected": False, "conflict_status": None,
}


def node_signals(uuid, *, restored_from_merge=None, merged_from=()) -> me.NodeSignals:
    return me.NodeSignals(
        uuid=uuid, exists=True, ineligible_role=False, namespace="default", freshness="ACTIVE",
        scope="SESSION", user_flagged=False, conflict_status=None,
        restored_from_merge=restored_from_merge, merged_from=tuple(merged_from),
    )
