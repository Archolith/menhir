"""Current-state-only View authority decision (Step 7 canary).

A PURE, side-effect-free gate that decides whether a *current* ``scalar_state`` View may
SUPPRESS an older overlapping graph fact during recall. This is the first user-facing use of
a scalar View to change what recall returns, so the bar is deliberately high and narrow.

This module does NOT touch recall. It only computes a decision from already-materialised
inputs so it can be unit-tested offline and shadow-measured (Phase D) before any production
wiring. Live wiring is a separate, flag-gated micro-step.

The six gates (owner contract, all must pass or the View stays ADVISORY):

  1. current-state query - the question asks for the CURRENT value. History / previous-value /
     comparison / ambiguous intents are ADVISORY (a View must never suppress the stale fact
     that is the RIGHT answer to "what did I used to own?").
  2. subject resolved    - the query subject, the View subject, and the candidate fact's
     subject all resolve to the SAME non-blank entity uuid.
  3. exact slot match    - View and fact address the same slot: same ``attribute`` and
     ``scope``. The View's own ``value_kind``/``unit`` must be a well-formed slot identity.
  4. grounded evidence   - the View's effective evidence tier is a real grounded tier
     (:data:`GROUNDED_TIERS`); a missing/unknown tier is NOT trusted.
  5. unambiguous current - exactly ONE current View for the slot and it is ``view_current``.
  6. complete overlap    - the fact is the SAME slot with a DIFFERENT value the View
     supersedes, and the fact is strictly OLDER (``fact.valid_at < view.valid_at``). A fact
     with equal/newer/absent timing, or the same value, is NOT a proven predecessor.

If any gate fails the outcome is ADVISORY and recall behaves exactly as it does today.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import EVIDENCE_TIERS

__all__ = [
    "QueryIntent",
    "AuthorityOutcome",
    "AuthorityQuery",
    "ViewCandidate",
    "OverlappingFact",
    "AuthorityDecision",
    "GROUNDED_TIERS",
    "decide_view_authority",
]

#: A View may only exert authority when its effective evidence tier is one of these grounded
#: tiers. Every committed TypedAssertion tier qualifies; a None/unknown tier does not.
GROUNDED_TIERS: frozenset[str] = frozenset(EVIDENCE_TIERS)

#: FOUNDATION tiers (decision 7.G/10.G, gotcha G10): a View may LEAD as the authoritative CURRENT
#: verdict only when its effective tier rests on a SOURCE FOUNDATION -- an admitted user statement or a
#: trusted non-probabilistic write -- NOT merely that a probabilistic extractor produced a typed
#: assertion. `agent` is exactly that probabilistic-extraction tier (`PERCEPTION_EVIDENCE_TIER`), which
#: `memory-governance.md` forbids from self-authorizing, so it is EXCLUDED here. A View whose effective
#: tier is `agent`-only stays ADVISORY (injected + visible, but never marked the current authority).
#: NOTE: today typed perception forces every assertion to `agent`, so until a user-asserted foundation
#: tier becomes reachable (the G14 `:TurnEvidence` declarant bridge + a write path that stamps a
#: foundation tier), this correctly yields advisory-only -- the safe governance posture, degrading to
#: leading automatically once a real foundation exists. This is the tier PROXY for the declarant-admission
#: check; the full 10.G basis gate (declarant=user independent of tier) supersedes it when G14 lands.
FOUNDATION_TIERS: frozenset[str] = frozenset(EVIDENCE_TIERS) - {"agent"}


class QueryIntent(str, Enum):
    """How the user's question relates to a slot's value over time."""

    CURRENT_STATE = "current_state"   # "How many coins do I own?"  -> may suppress
    HISTORY = "history"               # "How many coins have I owned over time?"
    PREVIOUS_VALUE = "previous_value" # "How many coins did I used to own?"
    COMPARISON = "comparison"         # "Do I own more coins than last year?"
    AMBIGUOUS = "ambiguous"           # intent could not be resolved


class AuthorityOutcome(str, Enum):
    SUPPRESS = "suppress"   # the View is authoritative; the older fact may be suppressed
    ADVISORY = "advisory"   # recall unchanged; the View is context only


@dataclass(frozen=True)
class AuthorityQuery:
    """The resolved question context."""

    intent: QueryIntent
    subject_uuid: Optional[str] = None


@dataclass(frozen=True)
class ViewCandidate:
    """A current scalar_state View proposed as authoritative for one slot."""

    subject_uuid: str
    attribute: str
    scope: str
    value_kind: str
    unit: str
    value: object
    evidence_tier: Optional[str]
    view_current: bool
    is_sole_current: bool          # exactly one current View exists for this slot
    valid_at: Optional[str] = None


@dataclass(frozen=True)
class OverlappingFact:
    """An older graph fact recall would otherwise surface for the same slot."""

    subject_uuid: str
    attribute: str
    scope: str
    value: object
    valid_at: Optional[str] = None


@dataclass(frozen=True)
class AuthorityDecision:
    outcome: AuthorityOutcome
    gate: str      # the first failing gate, or "pass"
    reason: str

    @property
    def suppress(self) -> bool:
        return self.outcome is AuthorityOutcome.SUPPRESS


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


#: Tolerant Neo4j-timestamp parse (fail-safe: None when absent/unparseable, which gate 6 treats as
#: "predecessor unproven"). Single definition in `domain.temporal` -- do not re-inline it.
_parse_dt = parse_iso8601


def _norm(text: object) -> str:
    return str(text or "").strip().lower()


def _advisory(gate: str, reason: str) -> AuthorityDecision:
    return AuthorityDecision(AuthorityOutcome.ADVISORY, gate, reason)


def decide_view_authority(
    query: AuthorityQuery, view: ViewCandidate, fact: OverlappingFact
) -> AuthorityDecision:
    """Return whether ``view`` may suppress ``fact`` for ``query``. Pure; never raises."""
    # Gate 1 - current-state query only.
    if query.intent is not QueryIntent.CURRENT_STATE:
        return _advisory("query_intent", f"intent {query.intent.value} is not current-state")

    # Gate 2 - subject resolved and identical across query, View, and fact.
    qs, vs, fs = _norm(query.subject_uuid), _norm(view.subject_uuid), _norm(fact.subject_uuid)
    if not qs or not vs or not fs:
        return _advisory("subject", "subject uuid unresolved on query, view, or fact")
    if not (qs == vs == fs):
        return _advisory("subject", "subject uuid mismatch across query/view/fact")

    # Gate 3 - exact slot match (attribute + scope) and a well-formed View slot identity.
    if not _norm(view.attribute) or not _norm(view.value_kind):
        return _advisory("slot", "view slot identity incomplete (attribute/value_kind)")
    if _norm(view.attribute) != _norm(fact.attribute) or _norm(view.scope) != _norm(fact.scope):
        return _advisory("slot", "view and fact address different slots")

    # Gate 4 - grounded evidence tier.
    if _norm(view.evidence_tier) not in GROUNDED_TIERS:
        return _advisory("evidence", f"evidence tier {view.evidence_tier!r} is not grounded")

    # Gate 5 - unambiguous single current View.
    if not view.view_current or not view.is_sole_current:
        return _advisory("uniqueness", "view is not the sole current view for the slot")

    # Gate 6 - complete overlap proof: different value, fact strictly older.
    if _norm(view.value) == _norm(fact.value):
        return _advisory("overlap", "fact carries the same value; nothing to suppress")
    view_at, fact_at = _parse_dt(view.valid_at), _parse_dt(fact.valid_at)
    if view_at is None or fact_at is None:
        return _advisory("overlap", "missing valid_at on view or fact; predecessor unproven")
    if not (fact_at < view_at):
        return _advisory("overlap", "fact is not strictly older than the view")

    return AuthorityDecision(AuthorityOutcome.SUPPRESS, "pass", "all six authority gates passed")
