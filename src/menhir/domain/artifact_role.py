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
from typing import Mapping, Sequence


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
#
# The work_artifact types (domain/work_artifact.py: ArtifactType) land here so they rank as
# what they are instead of falling through to REFERENCE (which PENALIZEs them in the intent
# affinity matrix). Meanings:
#   review                 -> DECISION  : a review renders a verdict (approve/revise/reject),
#                                         a decision on whether the artifact is acceptable.
#   implementation_report  -> EVIDENCE  : a wrapup is the recorded, verifiable account of work
#                                         actually done.
#   investigation          -> EVIDENCE  : an investigation's deliverable is the findings/root
#                                         cause that answers a question.
#   handoff                -> EVIDENCE  : a handoff is a recorded statement of the working
#                                         state handed to a successor.
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
    "review": ContentRole.DECISION,
    "implementation_report": ContentRole.EVIDENCE,
    "investigation": ContentRole.EVIDENCE,
    "handoff": ContentRole.EVIDENCE,
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

    for extra_type in metadata.get("artifact_types", ()) or ():
        et = str(extra_type).strip().lower()
        if et in _ARTIFACT_TYPE:
            roles.add(_ARTIFACT_TYPE[et])

    anchors = tuple(metadata.get("anchors", ()) or ())
    roles |= _anchor_roles(anchors)

    kinds = set(metadata.get("evidence_kinds", ()) or ())
    if "test" in kinds:
        roles.add(ContentRole.TEST)

    if not roles:
        roles.add(ContentRole.REFERENCE)
    return roles


def derive_work_artifact_projection(
    work_artifacts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Project :WorkArtifact lifecycle/role signals onto a linked :Entity (#579).

    Conservative by design:
      - ``artifact_status='historical'`` and ``superseded_by`` are projected ONLY when
        every linked :WorkArtifact for the entity is superseded. A live concept entity
        linked to both an old superseded plan and its active replacement stays current.
      - ``artifact_type`` / ``artifact_types`` prefer active linked artifacts when mixed,
        falling back to superseded ones when all linked artifacts are superseded.
    """
    active: list[Mapping[str, object]] = []
    superseded: list[Mapping[str, object]] = []
    for raw in work_artifacts:
        if not isinstance(raw, Mapping):
            continue
        status = str(raw.get("status") or "").strip().upper()
        sup_by = str(raw.get("superseded_by") or "").strip()
        art_type = str(raw.get("artifact_type") or "").strip().lower()
        if not status and not sup_by and not art_type:
            continue
        if status == "SUPERSEDED" or sup_by:
            superseded.append(raw)
        else:
            active.append(raw)

    projected: dict[str, object] = {}
    if superseded and not active:
        projected["artifact_status"] = "historical"
        replacements = sorted(
            {
                str(r.get("superseded_by") or "").strip()
                for r in superseded
                if str(r.get("superseded_by") or "").strip()
            }
        )
        if replacements:
            projected["superseded_by"] = replacements[0]

    preferred = active if active else superseded
    known_types = sorted(
        {
            str(r.get("artifact_type") or "").strip().lower()
            for r in preferred
            if str(r.get("artifact_type") or "").strip().lower() in _ARTIFACT_TYPE
        }
    )
    if len(known_types) == 1:
        projected["artifact_type"] = known_types[0]
    elif len(known_types) > 1:
        projected["artifact_types"] = tuple(known_types)
    return projected
