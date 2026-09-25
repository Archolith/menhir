"""Shared perception model: typed groups and gate decisions, plus the boundary's reducer vocabulary.

Every stage of the boundary (extract -> canonicalize -> gate -> fold) reads and writes these types;
the reducer tables are the boundary's only knowledge of the fold algebra's scalar sinks.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from menhir.domain.fold_algebra import Event, count, distinct_count, sum_

# ---------------------------------------------------------------------------- reducers this boundary can gate
#: measure kind -> scalar reducer. A counter View stores exactly one scalar, so perception only
#: gates the three scalar reducers (the D0 Arm-B demand: bike-spend SUM, tanks DISTINCT, acquires COUNT).
_SCALAR_REDUCERS: dict[str, Callable[[list[Event]], float]] = {
    "sum": lambda evs: float(sum_(evs)),
    "count": lambda evs: float(count(evs)),
    "distinct_count": lambda evs: float(distinct_count(evs)),
}

#: event kind -> the reducer its measure folds under. `assertion` is NOT here: a stated total is a
#: cross-check (triangulation), never folded into the primary value.
_KIND_REDUCER = {
    "purchase": "sum",
    "spend": "sum",
    "item": "distinct_count",
    "possession": "distinct_count",
    "acquire": "count",
    "occurrence": "count",
}


def _infer_reducer(kinds: list[str]) -> str:
    """A group's reducer = the reducer of its most common countable kind. distinct_count wins ties
    with sum only if items are present (a possession measure), else sum, else count."""
    reducers = [_KIND_REDUCER[k] for k in kinds if k in _KIND_REDUCER]
    if not reducers:
        return "count"
    return Counter(reducers).most_common(1)[0][0]


class _GraphAdapter(Protocol):
    def record_counter(self, **kwargs: Any) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Episode:
    uuid: str
    content: str


@dataclass
class PerceivedGroup:
    """One (subject, measure) fold target extracted from a single sample: the countable events, the
    inferred scalar reducer, and any independently STATED total.

    A stated total plays two roles: (a) triangulation against a fold when both exist (`stated_total`),
    and (b) the move-1 VALUE itself when there are no fold events — a user who says 'I have 20
    playlists' asserts the count directly. `stated_event` keeps the assertion's world-time + episode
    provenance so a move-1 commit carries `valid_at` and MENTIONS like any other View. `reducer` is
    'stated' for a pure move-1 group."""

    subject: str
    measure: str
    reducer: str
    events: list[Event] = field(default_factory=list)
    stated_total: float | None = None
    stated_event: Event | None = None


#: which guard produced a decision — a structured label (not just the free-text `reason`) so
#: abstentions can be bucketed by firing veto over time. This is the telemetry the recall-recovery
#: work needs ("which veto abstains most? is it recoverable?"). "commit" = passed all guards.
VETO_SELF_CONSISTENCY = "self_consistency"
VETO_COUNT_FLOOR = "count_floor"
VETO_TRIANGULATION = "triangulation"       # user stated total disagreed
VETO_CROSS_CHECK = "cross_check"           # holistic 2nd derivation disagreed
VETO_VERIFICATION = "verification"         # final audit of linked items failed
VETO_UNRESOLVED_COREFERENCE = "unresolved_coreference"  # ambiguous same-item cluster the judge didn't settle
VETO_UNSUPPORTED_STATED = "unsupported_stated"  # a STATED_MEASURE whose value isn't grounded in a span
VETO_COMMIT = "commit"


@dataclass
class GateDecision:
    """The committed-or-abstained verdict for one (subject, measure), plus the full deterministic
    evidence trail so abstention is observable (never a silent skip)."""

    subject: str
    measure: str
    reducer: str
    committed: bool
    reason: str
    value: float | None = None
    agreement: float = 0.0
    k: int = 0
    distribution: dict[str, int] = field(default_factory=dict)
    stated_total: float | None = None
    triangulated: bool | None = None
    cross_total: float | None = None
    events: list[Event] = field(default_factory=list)
    veto: str = VETO_COMMIT  # the guard that produced this decision (structured, for telemetry)
    #: verifier vote detail when a verifier ran (Lever C4) — receipt clarity for a SUM fail-closed:
    #: how close the audit was (votes/k) and how many attempts (1 + verify_retries) it took. None when
    #: no verifier ran or the injected verifier returned only a bool.
    verify_votes: int | None = None
    verify_k: int | None = None
    verify_attempts: int | None = None
    #: cross-check instrumentation (items 1-2): the value that was under test when the holistic
    #: cross-check vetoed (GateDecision.value is None on an abstention, so this carries it), and whether
    #: the SUM's arithmetic was DETERMINISTICALLY grounded from source spans (skipping the noisy
    #: holistic veto). `cross_margin` = |value - cross_total| when the holistic ran.
    abstained_value: float | None = None
    cross_margin: float | None = None
    sum_grounded: bool = False
