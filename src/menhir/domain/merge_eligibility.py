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
  - ineligible role: structural, path-shaped, View/instrumentation, or canonical-self node
  - namespace mismatch (never merge identities across silos)
  - COMPRESSED or GONE freshness (must be rehydrated through the lifecycle path first)
  - PROMOTED scope (operator-curated ground truth is merge-immune)
  - user_flagged (user-protected)
  - a protected conflict state (pending_llm_review / unresolved)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from menhir.domain.namespace import normalize_namespace


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

#: Freshness values that hard-veto a merge. Retained because the reason code and its telemetry
#: are named for them, and because the veto message reports which value offended.
_VETOED_FRESHNESS = frozenset({"COMPRESSED", "GONE"})
#: Freshness values a merge may proceed under: ACTIVE, and unstamped (Option A treats a node with
#: no freshness property as ACTIVE). Stated as an ALLOWLIST rather than as "not COMPRESSED and not
#: GONE" so that a future `FreshnessState` value is refused by default instead of silently
#: becoming mergeable -- merging DETACH-DELETEs the absorbed node, so the unknown case must fail
#: closed. Today the two forms describe the same set: `FreshnessState` is exactly
#: {ACTIVE, COMPRESSED, GONE}.
_MERGEABLE_FRESHNESS = frozenset({"ACTIVE", ""})
#: conflict_status values that mark a node as under active review (see domain.models.ConflictStatus).
_PROTECTED_CONFLICT_STATES = frozenset({"pending_llm_review", "unresolved"})


#: Parameter names the emitted mutation predicate binds. The repository must supply these.
MUTABLE_PREDICATE_PARAMS: dict[str, Any] = {
    "mergeable_freshness": sorted(_MERGEABLE_FRESHNESS),
    "protected_conflict_states": sorted(_PROTECTED_CONFLICT_STATES),
}


def mutable_eligibility_cypher(survivor: str = "survivor", absorbed: str = "absorbed") -> str:
    """Emit the mutation-time form of the MUTABLE predicates decided above (CF-47).

    The mutation has to repeat these -- another writer can change freshness, scope, flag,
    conflict state or namespace between the preflight and the write, and the guard must be inside
    the statement that writes. What it must NOT do is restate them independently, which is what it
    used to. Two hand-written copies of one rule disagreed in four places:

    | predicate | Python | Cypher |
    |---|---|---|
    | freshness | `.upper()` then veto COMPRESSED/GONE | exact `IN ['ACTIVE']` |
    | scope | `.upper() == 'PROMOTED'` | exact `<> 'PROMOTED'` |
    | conflict | `.strip().lower()` then membership | exact membership |
    | namespace | `normalize_namespace` | `coalesce(namespace, group_id, 'default')` |

    Emitting the Cypher from the same constants is what makes them one rule rather than two that
    happen to agree today. Editing `_MERGEABLE_FRESHNESS` or `_PROTECTED_CONFLICT_STATES` now
    changes both sides at once, which is the only version of this that stays fixed.

    **Where the two disagreed, the stricter side wins.** Merging DETACH-DELETEs the absorbed
    node, so a predicate that cannot decide must refuse:

    - scope and conflict: Python normalized and Cypher did not, so the Cypher admitted a
      lowercase `promoted` and a `' Unresolved'`. Normalization moves into the Cypher, which
      NARROWS it. A PROMOTED node is merge-immune by policy; case is not consent.
    - freshness: Cypher was the stricter one, admitting only ACTIVE-or-null. It stays that way
      here. Note the register's worked example -- `freshness = 'STALE'` passing preflight and
      being dropped at mutation -- is not reachable: `FreshnessState` is exactly
      {ACTIVE, COMPRESSED, GONE}, so there is no fourth value for the two rules to differ on.
      The reachable divergence was always the normalization one, which is what this fixes.

    `normalize_namespace` and the Cypher's namespace expression are left as they are and are
    already equivalent in practice: the gather query at `correlation_queries.py:148` projects
    `coalesce(n.namespace, n.group_id, 'default')` into the very field `normalize_namespace`
    then strips. Namespace is the load-bearing tenancy boundary, so it is not restructured as a
    side effect of this fix.

    The returned fragment is a constant with no caller-controlled interpolation: `survivor` and
    `absorbed` are Cypher variable names chosen by this codebase, never user input.
    """
    clauses = []
    for var in (survivor, absorbed):
        clauses.extend([
            f"coalesce({var}.is_self, false) = false",
            f"toLower(trim(coalesce({var}.entity_role, ''))) <> 'self'",
            f"toUpper(trim(coalesce({var}.freshness, 'ACTIVE'))) IN $mergeable_freshness",
            f"toUpper(trim(coalesce({var}.scope, ''))) <> 'PROMOTED'",
            f"coalesce({var}.user_flagged, false) = false",
            f"NOT toLower(trim(coalesce({var}.conflict_status, ''))) IN $protected_conflict_states",
        ])
    clauses.append(
        f"coalesce({survivor}.namespace, {survivor}.group_id, 'default')"
        f" = coalesce({absorbed}.namespace, {absorbed}.group_id, 'default')"
    )
    return "\n              AND ".join(clauses)


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

    # 5. Only ACTIVE or unstamped may merge (Option A). COMPRESSED/GONE must be rehydrated first.
    #    An allowlist, not a veto list: the comment here used to say "null/ACTIVE allowed" while
    #    the code allowed everything except two values, and the mutation allowed only ACTIVE --
    #    which is how preflight came to say allowed while the write silently abstained (CF-47).
    bad_freshness = [
        (n.uuid, n.freshness) for n in (survivor, absorbed)
        if str(n.freshness or "").strip().upper() not in _MERGEABLE_FRESHNESS
    ]
    if bad_freshness:
        return _veto(NON_ACTIVE_FRESHNESS, offenders=bad_freshness)

    # 6. PROMOTED nodes are operator-curated and merge-immune.
    # `.strip()` alongside `.upper()`, and the same trim in the emitted Cypher: a PROMOTED node
    # is operator-curated and merge-immune by policy, so neither case nor stray whitespace is
    # consent to merge it (CF-47).
    promoted = [
        n.uuid for n in (survivor, absorbed)
        if str(n.scope or "").strip().upper() == "PROMOTED"
    ]
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
