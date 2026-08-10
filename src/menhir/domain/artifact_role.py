"""Deterministic content-role derivation for candidates (IntentOracle producer).

The role axis (WHAT KIND of artifact) is distinct from the lifecycle-status axis
(current/superseded/historical). Status is owned by the temporal/belief producers and is
NOT recomputed here — `derive_content_role` reads a candidate's prefetched metadata
(artifact_type + anchors + evidence_kinds) and returns the SET of roles it carries
(usually one; multi-role is handled by max-affinity downstream, design 4A).

Ported from archolith-bench `archolith_bench/intent/roles.py`.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping


class ContentRole(str, Enum):
    FAILURE = "failure"
    INCIDENT = "incident"
    DECISION = "decision"
    EXPERIMENT = "experiment"
    BENCHMARK = "benchmark"
    TEST = "test"
    PLAN = "plan"
    RUNBOOK = "runbook"
    EVIDENCE = "evidence"
    REFERENCE = "reference"  # default


# artifact_type -> role. The L4 institutional artifacts (decision/failure/incident) map
# directly; the others are research/doc shapes the recall layer also stores.
_ARTIFACT_TYPE: dict[str, ContentRole] = {
    "failure": ContentRole.FAILURE,
    "incident": ContentRole.INCIDENT,
    "decision": ContentRole.DECISION,
    "experiment": ContentRole.EXPERIMENT,
    "benchmark": ContentRole.BENCHMARK,
    "test": ContentRole.TEST,
    "plan": ContentRole.PLAN,
    "runbook": ContentRole.RUNBOOK,
    "evidence": ContentRole.EVIDENCE,
    "reference": ContentRole.REFERENCE,
    "doc": ContentRole.REFERENCE,
}


def _anchor_roles(anchors: tuple[str, ...]) -> set[ContentRole]:
    roles: set[ContentRole] = set()
    for a in anchors:
        low = str(a).lower()
        if "/plans/" in low or low.endswith("-plan.md"):
            roles.add(ContentRole.PLAN)
        if "bench" in low:
            roles.add(ContentRole.BENCHMARK)
        if "runbook" in low:
            roles.add(ContentRole.RUNBOOK)
        if low.startswith("test_") or "/tests/" in low or low.endswith("_test.py"):
            roles.add(ContentRole.TEST)
    return roles


def derive_content_role(metadata: Mapping[str, object]) -> set[ContentRole]:
    """Derive the candidate's content role(s) from its snapshot. Deterministic.

    Primary signal is `artifact_type`; anchors and `test` evidence add secondary roles.
    Defaults to REFERENCE when nothing else is determinable (a doc-shaped memory)."""
    roles: set[ContentRole] = set()

    at = str(metadata.get("artifact_type", "")).lower()
    if at in _ARTIFACT_TYPE:
        roles.add(_ARTIFACT_TYPE[at])

    anchors = tuple(metadata.get("anchors", ()) or ())
    roles |= _anchor_roles(anchors)

    kinds = set(metadata.get("evidence_kinds", ()) or ())
    if "test" in kinds:
        roles.add(ContentRole.TEST)

    if not roles:
        roles.add(ContentRole.REFERENCE)
    return roles
