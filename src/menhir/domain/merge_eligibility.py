"""Central merge-eligibility policy (remediation plan Phase 3, section 4).

ONE decision function, used by both the service-level classifier and the final mutation-time
precondition, so a stale discovery result or a direct repository call cannot bypass the policy.
The function is pure: it maps already-gathered node signals to an allow/deny with a STABLE reason
code. Gathering the signals from Neo4j is the repository's job; deciding is this module's.

Option A semantics (owner-approved 2026-07-13): fresh entities are stamped ``scope='SESSION'`` with
NO ``freshness`` property, and only promotion sets ``freshness='ACTIVE'`` (see episode_stamping and
consolidation_queries.promote_to_persistent). A literal "ACTIVE and PERSISTENT for both" gate would
veto essentially every correlation-time auto-merge. So this policy is pure TIGHTENING: it hard-vetoes
the states that actually risk data loss or identity corruption, while treating unstamped freshness as
ACTIVE and allowing SESSION alongside PERSISTENT.

Hard vetoes:
  - either node missing (stale discovery)
  - same uuid (a node cannot absorb itself)
  - ineligible role: structural, path-shaped, or a View/instrumentation node
  - namespace mismatch (never merge identities across silos)
  - COMPRESSED or GONE freshness (must be rehydrated through the lifecycle path first)
  - PROMOTED scope (operator-curated ground truth is merge-immune)
  - user_flagged (user-protected)
  - a protected conflict state (pending_llm_review / unresolved)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Stable reason codes. These are contract surface -- telemetry, tests, and operator tooling key on
# them, so do not rename without updating those. ELIGIBLE is the single allow value.
ELIGIBLE = "ELIGIBLE"
NODE_MISSING = "NODE_MISSING"
SAME_UUID = "SAME_UUID"
INELIGIBLE_ROLE = "INELIGIBLE_ROLE"
NAMESPACE_MISMATCH = "NAMESPACE_MISMATCH"
NON_ACTIVE_FRESHNESS = "NON_ACTIVE_FRESHNESS"
PROMOTED_SCOPE = "PROMOTED_SCOPE"
USER_FLAGGED = "USER_FLAGGED"
PROTECTED_CONFLICT = "PROTECTED_CONFLICT"

#: Freshness values that hard-veto a merge. Null/unstamped and ACTIVE are allowed (Option A).
_VETOED_FRESHNESS = frozenset({"COMPRESSED", "GONE"})
#: conflict_status values that mark a node as under active review (see domain.models.ConflictStatus).
_PROTECTED_CONFLICT_STATES = frozenset({"pending_llm_review", "unresolved"})


def normalize_namespace(value: Any) -> str:
    """Normalize a namespace for equality comparison. Empty/None collapse to 'default'.

    Case and surrounding whitespace are preserved beyond a strip: namespaces are stamped through one
    path, so an exact match after coalescing the empty case is the correct 'normalized' comparison.
    """
    text = str(value).strip() if value is not None else ""
    return text or "default"


@dataclass(frozen=True)
class NodeSignals:
    """The material state of one merge participant, read from the graph.

    ``ineligible_role`` folds structural/path-shaped/View detection into one boolean at read time
    (the repository owns that Cypher); the policy stays a pure mapping.
    """

    uuid: str
    exists: bool
    ineligible_role: bool
    namespace: str
    freshness: str | None
    scope: str | None
    user_flagged: bool
    conflict_status: str | None


@dataclass(frozen=True)
class MergeEligibility:
    """The eligibility decision. ``allowed`` iff ``reason_code == ELIGIBLE``."""

    allowed: bool
    reason_code: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _veto(reason: str, **diag: Any) -> MergeEligibility:
    return MergeEligibility(allowed=False, reason_code=reason, diagnostics=diag)


def evaluate(survivor: NodeSignals, absorbed: NodeSignals) -> MergeEligibility:
    """Decide whether ``absorbed`` may be merged into ``survivor``.

    Predicates are checked in a fixed priority so the reported reason is deterministic and the most
    fundamental failure wins (existence before state). The FIRST failing predicate is reported.
    """
    # 1. Both nodes must still exist -- discovery may be stale.
    missing = [n.uuid for n in (survivor, absorbed) if not n.exists]
    if missing:
        return _veto(NODE_MISSING, missing=missing)

    # 2. A node cannot absorb itself.
    if survivor.uuid == absorbed.uuid:
        return _veto(SAME_UUID, uuid=survivor.uuid)

    # 3. Neither node may be structural, path-shaped, or a View/instrumentation node.
    role_ineligible = [n.uuid for n in (survivor, absorbed) if n.ineligible_role]
    if role_ineligible:
        return _veto(INELIGIBLE_ROLE, ineligible=role_ineligible)

    # 4. Identities never merge across namespaces.
    if normalize_namespace(survivor.namespace) != normalize_namespace(absorbed.namespace):
        return _veto(
            NAMESPACE_MISMATCH,
            survivor_namespace=normalize_namespace(survivor.namespace),
            absorbed_namespace=normalize_namespace(absorbed.namespace),
        )

    # 5. COMPRESSED/GONE must be rehydrated first (null/ACTIVE allowed -- Option A).
    bad_freshness = [
        (n.uuid, n.freshness) for n in (survivor, absorbed)
        if str(n.freshness or "").upper() in _VETOED_FRESHNESS
    ]
    if bad_freshness:
        return _veto(NON_ACTIVE_FRESHNESS, offenders=bad_freshness)

    # 6. PROMOTED nodes are operator-curated and merge-immune.
    promoted = [n.uuid for n in (survivor, absorbed) if str(n.scope or "").upper() == "PROMOTED"]
    if promoted:
        return _veto(PROMOTED_SCOPE, promoted=promoted)

    # 7. User-flagged nodes are protected.
    flagged = [n.uuid for n in (survivor, absorbed) if n.user_flagged]
    if flagged:
        return _veto(USER_FLAGGED, flagged=flagged)

    # 8. Nodes under active conflict review must not be silently merged.
    conflicted = [
        (n.uuid, n.conflict_status) for n in (survivor, absorbed)
        if str(n.conflict_status or "").strip().lower() in _PROTECTED_CONFLICT_STATES
    ]
    if conflicted:
        return _veto(PROTECTED_CONFLICT, conflicted=conflicted)

    return MergeEligibility(allowed=True, reason_code=ELIGIBLE)
