"""Deterministic scalar-state fold — turns durable :TypedAssertion rows into a current value per
slot (ScalarStateView Piece C, commit 2).

Pure and offline: no Neo4j, no LLM. Given the CURRENT assertions for an entity, it produces one
`FoldedScalarState` per (entity, attribute, scope, value_kind, unit) slot, or an `Abstention`.
The same function backs both the live fold (at ingest) and `rebuild_scalar_state` (replay from the
log), so live-vs-rebuild parity is by construction.

Rules (from the plan, review-tightened):
* **Latest absolute anchor.** Within a slot, the base is the absolute assertion with the greatest
  `valid_at` (LWW / fold-algebra Law 1). Multiple legitimate absolutes over time are normal; the
  latest wins.
* **Only subsequent deltas.** Signed deltas with `valid_at` STRICTLY after the anchor are summed
  onto it; deltas at/before the anchor are already subsumed by that stated absolute.
* **Ambiguous-anchor abstention.** If the latest `valid_at` carries two or more DISTINCT absolute
  values (an unbreakable tie), or a delta would apply to a range anchor, abstain — emit no value.
  A slot with deltas but no absolute anchor also abstains (nothing to ground).
* **Contributors + authority.** Each folded value reports the exact contributing assertion_ids
  (anchor + applied deltas) and an EFFECTIVE evidence tier = the WEAKEST tier among those required
  contributors (a derived value is only as trustworthy as its weakest input) — never the slot-wide
  maximum. C.4/Piece D read this, not the View's first-write receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from menhir.domain.count_match import (
    COUNTED,
    TARGETED_STATUSES,
    UNCERTAIN,
    match_slot_row,
    status_of,
)
from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import (
    COUNTABLE_KINDS,
    MATERIALIZING_IMPORTANCE,
    evidence_rank,
    normalize_scalar,
    slot_of as _slot_of,
)

# abstention reason codes (stable, for telemetry).
NO_ANCHOR = "no_anchor"
AMBIGUOUS_ANCHOR = "ambiguous_anchor"
DELTA_ON_RANGE = "delta_on_range"
ROUTINE_ONLY = "routine_only"
NOT_COUNTABLE = "not_countable"
INVALID_AMOUNT = "invalid_amount"

BASIS_STATED = "stated"
BASIS_COUNTED = "counted"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class FoldedScalarState:
    """One folded current value for a slot, with its exact contributors and effective authority."""

    subject_uuid: str
    subject_display: str
    attribute: str
    scope: str
    value_kind: str
    unit: str
    value: Any                       # the folded current value (number, range, bool, or string)
    valid_at: str                    # chronologically latest contributing valid_at
    contributor_ids: list[str]       # anchor + applied delta assertion_ids (exact provenance)
    effective_tier: str              # weakest tier among contributors (authority signal)
    episode_uuids: list[str]         # contributing episodes (View episode provenance)
    anchor_value: Any                # the stated absolute base (audit)
    delta_total: float = 0.0         # net delta applied (audit)
    # Phase 3 provenance-edge inputs (7.D/G11): the fold's algebra split by LABEL. `anchor_id` is the
    # anchoring absolute (CURRENT_ANCHOR); `contributed_delta_ids` are the APPLIED deltas
    # (CONTRIBUTED_TO, live inputs); `superseded_anchor_ids` are EXCLUDED prior anchors — a replaced
    # absolute or a pre-anchor unapplied delta (SUPERSEDED_ANCHOR, never a live contributor).
    anchor_id: str = ""
    contributed_delta_ids: list[str] = field(default_factory=list)
    superseded_anchor_ids: list[str] = field(default_factory=list)
    # stated: anchored on a stated absolute. counted: a lower bound folded from occurrences only.
    basis: str = "stated"
    contributed_occurrence_ids: list[str] = field(default_factory=list)
    absorbed_occurrence_ids: list[str] = field(default_factory=list)
    # Match mode (unit 4R) only; None/empty otherwise. `count_uncertain` sums the happenings that may
    # be repeats (not in `value`); `count_actions` sums counted happenings per action; `count_items`
    # lists the distinct counted items.
    count_uncertain: Any = None
    count_actions: dict[str, Any] | None = None
    count_items: list[str] | None = None
    uncertain_occurrence_ids: list[str] = field(default_factory=list)

    @property
    def slot_key(self) -> tuple[str, str, str, str, str]:
        return (self.subject_uuid, self.attribute, self.scope, self.value_kind, self.unit)


@dataclass(frozen=True)
class Abstention:
    slot_key: tuple[str, str, str, str, str]
    reason: str
    subject_uuid: str


@dataclass(frozen=True)
class Expiry:
    """A slot whose value has ENDED with no current replacement ("I used to X").

    Distinct from an Abstention: an abstention means we cannot ground a value (no anchor / ambiguous);
    an Expiry means we KNOW the prior value is no longer current but no new value was stated, so the
    slot must carry NO current View (any existing one is retired). `expired_value` is the old value it
    ended (provenance)."""

    slot_key: tuple[str, str, str, str, str]
    subject_uuid: str
    subject_display: str
    valid_at: str
    expired_value: Any
    contributor_ids: list[str]
    episode_uuids: list[str]


@dataclass
class FoldResult:
    states: list[FoldedScalarState] = field(default_factory=list)
    abstentions: list[Abstention] = field(default_factory=list)
    expiries: list[Expiry] = field(default_factory=list)


#: Tolerant Neo4j-timestamp parse. Single definition in `domain.temporal` -- this parser was
#: fixed independently in three modules before it was consolidated; do not re-inline it.
#: Unparseable here collapses to the _EPOCH fallback below, which is why the zone-id strip matters.
_parse_dt = parse_iso8601


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float, Decimal)) and not isinstance(x, bool)


def _money_decimal(value: Any) -> Decimal | None:
    """Return a finite Decimal for a hydrated or legacy numeric money value."""
    if isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _turn_key(row: dict[str, Any]) -> str:
    """The turn a row was read from. `grounding_id` is the raw grounding (the TurnEvidence turn_id for
    turn-grounded rows); `episode_uuid` is only the admitting Episodic and is empty for most hook
    turns, so it cannot identify the turn."""
    return str(row.get("grounding_id") or row.get("episode_uuid") or "")


def _source_keys(row: dict[str, Any]) -> set[str]:
    """Every id the row's source is known by: raw grounding and admitting Episodic."""
    return {str(row.get("grounding_id") or ""), str(row.get("episode_uuid") or "")} - {""}


_STATUS_RANK = {COUNTED: 0, UNCERTAIN: 1}


def _item_key(row: dict[str, Any]) -> str:
    return " ".join(str(row.get("count_item") or "").lower().split())


def _dedup_occurrences_per_turn(
    rows: list[dict[str, Any]], *, count_match: bool = False,
) -> list[dict[str, Any]]:
    """Keep at most one occurrence per (subject, attribute, value_kind, unit, turn), ignoring scope.
    Survivor: largest value, then scope == "", then smallest assertion_id. Other rows pass through.
    Match mode (rows already family-keyed): the key adds the item, so two kits bought in one turn
    stay two; the survivor prefers a counted row, then an uncertain one, so a dedup never turns a
    counted happening into an excluded one."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        turn = _turn_key(row)
        if str(row.get("operation")) != "occurrence" or not turn:
            continue
        s = _slot_of(row)
        key: tuple = (s[0], s[1], s[3], s[4], turn)
        if count_match:
            key = key + (_item_key(row),)
        groups.setdefault(key, []).append(row)
    dropped: set[int] = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        def _num(m: dict[str, Any]) -> Decimal:
            v = _money_decimal(m.get("value"))
            return Decimal("-Infinity") if v is None else v

        keep = min(members, key=lambda m: (
            _STATUS_RANK.get(status_of(m), 2) if count_match else 0,
            -_num(m),
            str(m.get("scope") or "").strip() != "",
            str(m.get("assertion_id") or ""),
        ))
        dropped.update(id(m) for m in members if m is not keep)
    if not dropped:
        return rows
    return [r for r in rows if id(r) not in dropped]


def fold_assertions(
    rows: list[dict[str, Any]], *, as_of: datetime | None = None, include_occurrences: bool = True,
    count_match: bool = False,
) -> FoldResult:
    """Fold current assertion rows (shape of TypedAssertionRepository.assertions_for_entity) into
    one FoldedScalarState per slot, or an Abstention. Deterministic given the input set AND `as_of`.

    AS-OF ACTIVATION (Phase B): when `as_of` is given, an assertion affects the fold only once it has
    become valid at that evaluation time -- `valid_at <= as_of`. FUTURE assertions (`valid_at > as_of`)
    are ignored for ALL operation kinds (absolute, delta, expire), so a scheduled value ("starting Monday
    I wake at 6:30") does not become current prematurely and a future expiry does not retire a View
    early. `as_of=None` means NO time filter -- a pure set-fold over exactly the rows given (the
    orchestration boundary, `ScalarStateService`, captures a concrete `as_of=now(UTC)` and passes it, so
    LIVE folding is always time-filtered while a direct caller/test stays a pure set-fold). Parity: same
    rows + same `as_of` always fold identically (live == rebuild); a later real-time evaluation MAY differ
    because future assertions have since activated -- temporal evolution, not a parity break. A row with
    an unparseable/absent `valid_at` sorts as epoch (always <= as_of), i.e. included."""
    if not include_occurrences:
        rows = [r for r in rows if str(r.get("operation")) != "occurrence"]
    if as_of is not None:
        rows = [r for r in rows if (_parse_dt(r.get("valid_at")) or _EPOCH) <= as_of]
    # Match mode (unit 4R): occurrences fold by object family and by their stored decision; only
    # `counted` rows add to the value (see _fold_counted_match). Off -> unchanged.
    counted_ids: set[str] = set()
    if count_match:
        counted_ids = {
            str(r.get("assertion_id") or "") for r in rows
            if str(r.get("operation")) == "occurrence" and status_of(r) == COUNTED
        } - {""}
        rows = [match_slot_row(r) if str(r.get("operation")) == "occurrence" else r for r in rows]
    rows = _dedup_occurrences_per_turn(rows, count_match=count_match)
    slots: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        slots.setdefault(_slot_of(row), []).append(row)

    result = FoldResult()
    for slot_key in sorted(slots):  # sorted -> deterministic output order
        subject_uuid, attribute, scope, value_kind, unit = slot_key
        members = slots[slot_key]
        subject_uuid = str(members[0].get("subject_uuid", subject_uuid))

        absolutes = [m for m in members if str(m.get("operation")) == "absolute"]
        deltas = [m for m in members if str(m.get("operation")) == "delta"]
        expires = [m for m in members if str(m.get("operation")) == "expire"]
        occurrences = [m for m in members if str(m.get("operation")) == "occurrence"]

        # Expiry reconciliation (owner spec: "used to X" ends X as a current fact but need not supply a
        # replacement). If the latest expire is STRICTLY after every absolute (or there is no absolute),
        # the slot's value has ended with no current replacement -> Expiry (no View; existing retired).
        # An expire at/before the latest absolute is subsumed: it ended an old value a newer absolute
        # already replaced.
        if expires:
            te_max = max((_parse_dt(e.get("valid_at")) or _EPOCH) for e in expires)
            ta_max = (
                max((_parse_dt(a.get("valid_at")) or _EPOCH) for a in absolutes) if absolutes else None
            )
            if ta_max is None or te_max > ta_max:
                latest_expires = [e for e in expires if (_parse_dt(e.get("valid_at")) or _EPOCH) == te_max]
                chosen = sorted(
                    latest_expires,
                    key=lambda e: (str(e.get("learned_at") or ""), str(e.get("assertion_id") or "")),
                )[-1]
                ep_uuids: list[str] = []
                for e in latest_expires:
                    eu = str(e.get("episode_uuid") or "")
                    if eu and eu not in ep_uuids:
                        ep_uuids.append(eu)
                result.expiries.append(Expiry(
                    slot_key=slot_key, subject_uuid=subject_uuid,
                    subject_display=str(chosen.get("subject_display", "") or subject_uuid),
                    valid_at=str(chosen.get("valid_at") or ""), expired_value=chosen.get("value"),
                    contributor_ids=[str(e.get("assertion_id") or "") for e in latest_expires],
                    episode_uuids=ep_uuids,
                ))
                continue
            # else: expires are subsumed by a newer absolute -> fall through to normal absolute fold.

        if not absolutes:
            if occurrences and count_match:
                _fold_counted_match(result, slot_key, subject_uuid, value_kind, occurrences, deltas,
                                    counted_ids)
            elif occurrences:
                _fold_counted(result, slot_key, subject_uuid, value_kind, occurrences, deltas)
            else:
                result.abstentions.append(Abstention(slot_key, NO_ANCHOR, subject_uuid))
            continue
        # A stated absolute exists: occurrences do not touch it (adding them onto a stated total is a
        # separate, separately-flagged step). They are not contributors and not superseded anchors, so
        # this slot's state is byte-identical to a fold without them.

        # latest absolute by valid_at; tie on distinct values -> ambiguous UNLESS exactly one distinct
        # value was explicitly stated as a CORRECTION of the others (an equal-time-only tiebreak).
        anchor_dt = max((_parse_dt(a.get("valid_at")) or _EPOCH) for a in absolutes)
        latest = [a for a in absolutes if (_parse_dt(a.get("valid_at")) or _EPOCH) == anchor_dt]
        distinct_values = {normalize_scalar(a.get("value")) for a in latest}
        if len(distinct_values) > 1:
            # Correction resolves the tie ONLY when exactly one distinct VALUE GROUP is corrected. Group
            # by normalized value so two corrections agreeing on the same value are one group (agreement,
            # not ambiguity); zero or >=2 corrected value groups stay AMBIGUOUS_ANCHOR (never guess). This
            # is an equal-`valid_at`-only rule: an older correction is not in `latest`, so it can never
            # override a genuinely later ordinary value.
            corrected_values = {
                normalize_scalar(a.get("value")) for a in latest
                if str(a.get("absolute_semantics") or "ordinary") == "correction"
            }
            if len(corrected_values) == 1:
                latest = [a for a in latest if normalize_scalar(a.get("value")) in corrected_values]
            else:
                result.abstentions.append(Abstention(slot_key, AMBIGUOUS_ANCHOR, subject_uuid))
                continue
        # deterministic pick among the tied-and-equal latest: last by (learned_at, assertion_id).
        anchor = sorted(
            latest, key=lambda a: (str(a.get("learned_at") or ""), str(a.get("assertion_id") or ""))
        )[-1]
        anchor_value = anchor.get("value")

        applied = [
            d for d in deltas
            if (_parse_dt(d.get("valid_at")) or _EPOCH) > anchor_dt and _is_number(d.get("value"))
        ]

        if applied and not _is_number(anchor_value):
            # a delta onto a range/non-numeric anchor is not well-defined -> abstain.
            result.abstentions.append(Abstention(slot_key, DELTA_ON_RANGE, subject_uuid))
            continue

        contributors = [anchor]
        delta_total = 0.0
        value: Any = anchor_value
        if applied:
            applied.sort(key=lambda d: (str(d.get("valid_at") or ""), str(d.get("assertion_id") or "")))
            if value_kind == "money":
                anchor_decimal = _money_decimal(anchor_value)
                delta_decimals = [_money_decimal(d.get("value")) for d in applied]
                if anchor_decimal is None or any(delta is None for delta in delta_decimals):
                    result.abstentions.append(Abstention(slot_key, DELTA_ON_RANGE, subject_uuid))
                    continue
                exact_delta = sum((delta for delta in delta_decimals if delta is not None), Decimal("0"))
                value = anchor_decimal + exact_delta
                delta_total = float(exact_delta)  # audit mirror only; register identity stays Decimal-exact
            else:
                delta_total = float(sum(float(d.get("value")) for d in applied))
                folded = float(anchor_value) + delta_total
                value = int(folded) if folded == int(folded) else folded
            contributors += applied

        # Phase 3 (7.D/G11) edge inputs, split by the fold's algebra. The anchor + APPLIED deltas are
        # the live contributors; EXCLUDED prior anchors (every OTHER absolute) and pre-anchor UNAPPLIED
        # deltas are superseded (present in the log, not in the current value).
        anchor_id = str(anchor.get("assertion_id") or "")
        contributed_delta_ids = [str(d.get("assertion_id") or "") for d in applied]
        _applied_ids = set(contributed_delta_ids)
        superseded_anchor_ids = [
            str(a.get("assertion_id") or "") for a in absolutes
            if str(a.get("assertion_id") or "") != anchor_id
        ] + [
            str(d.get("assertion_id") or "") for d in deltas
            if str(d.get("assertion_id") or "") not in _applied_ids
        ]

        tiers = [str(c.get("evidence_tier") or "agent") for c in contributors]
        effective_tier = min(tiers, key=evidence_rank)  # weakest required contributor
        contributor_ids = [str(c.get("assertion_id") or "") for c in contributors]
        # Display comes from the ANCHOR (a chosen deterministic contributor), not members[0], so
        # input order cannot change the materialized surface.
        display = str(anchor.get("subject_display", "") or subject_uuid)
        # valid_at is the CHRONOLOGICALLY latest contributor's own timestamp (parsed), not a lexical
        # string max; the latest contributor is the anchor unless a later delta applied.
        latest_contributor = max(
            contributors, key=lambda c: (_parse_dt(c.get("valid_at")) or _EPOCH))
        valid_at = str(latest_contributor.get("valid_at") or "")
        # dedup episode provenance, order-stable by first appearance among contributors.
        episode_uuids: list[str] = []
        for c in contributors:
            eu = str(c.get("episode_uuid") or "")
            if eu and eu not in episode_uuids:
                episode_uuids.append(eu)

        result.states.append(FoldedScalarState(
            subject_uuid=subject_uuid, subject_display=display,
            attribute=attribute, scope=scope, value_kind=value_kind, unit=unit,
            value=value, valid_at=valid_at, contributor_ids=contributor_ids,
            effective_tier=effective_tier, episode_uuids=episode_uuids,
            anchor_value=anchor_value, delta_total=delta_total,
            anchor_id=anchor_id, contributed_delta_ids=contributed_delta_ids,
            superseded_anchor_ids=superseded_anchor_ids,
        ))
    return result


def _fold_counted(
    result: FoldResult,
    slot_key: tuple[str, str, str, str, str],
    subject_uuid: str,
    value_kind: str,
    occurrences: list[dict[str, Any]],
    deltas: list[dict[str, Any]],
) -> None:
    """Fold a slot with NO stated absolute from its occurrences: a lower bound ("at least N you
    mentioned"), basis=counted. Appends one state or one abstention to `result`.

    * Double-count guard: an occurrence from a turn that already gave this slot a delta is absorbed
      (that turn's change is the delta's, e.g. "visited three more times").
    * Importance: the slot materializes only if at least one applied occurrence is tracked or
      notable; routine-only (or unlabelled) slots abstain.
    * Deltas themselves are not added here (no anchor to apply them to); they stay unapplied.
    """
    if value_kind not in COUNTABLE_KINDS:
        result.abstentions.append(Abstention(slot_key, NOT_COUNTABLE, subject_uuid))
        return
    # A delta claims every id its source is known by (raw grounding AND admitting Episodic), so an
    # episode-grounded delta still absorbs a turn-grounded occurrence of the same admitted turn.
    claimed_turns = {k for d in deltas for k in _source_keys(d)}
    applied = [
        o for o in occurrences
        if not (_source_keys(o) & claimed_turns) and _is_number(o.get("value"))
    ]
    applied_ids = {str(o.get("assertion_id") or "") for o in applied}
    absorbed_ids = sorted(
        str(o.get("assertion_id") or "") for o in occurrences
        if str(o.get("assertion_id") or "") not in applied_ids
    )
    if not applied:
        result.abstentions.append(Abstention(slot_key, NO_ANCHOR, subject_uuid))
        return
    if not any(str(o.get("importance") or "") in MATERIALIZING_IMPORTANCE for o in applied):
        result.abstentions.append(Abstention(slot_key, ROUTINE_ONLY, subject_uuid))
        return

    applied.sort(key=lambda o: (
        _parse_dt(o.get("valid_at")) or _EPOCH, str(o.get("assertion_id") or "")))
    if value_kind == "money":
        amounts = [_money_decimal(o.get("value")) for o in applied]
        if any(a is None for a in amounts):
            result.abstentions.append(Abstention(slot_key, INVALID_AMOUNT, subject_uuid))
            return
        value: Any = sum((a for a in amounts if a is not None), Decimal("0"))
    else:
        total = sum(float(o.get("value")) for o in applied)
        value = int(total) if total == int(total) else total

    tiers = [str(o.get("evidence_tier") or "agent") for o in applied]
    episode_uuids: list[str] = []
    for o in applied:
        eu = str(o.get("episode_uuid") or "")
        if eu and eu not in episode_uuids:
            episode_uuids.append(eu)
    first = applied[0]
    result.states.append(FoldedScalarState(
        subject_uuid=subject_uuid,
        subject_display=str(first.get("subject_display", "") or subject_uuid),
        attribute=slot_key[1], scope=slot_key[2], value_kind=value_kind, unit=slot_key[4],
        value=value, valid_at=str(applied[-1].get("valid_at") or ""),
        contributor_ids=[str(o.get("assertion_id") or "") for o in applied],
        effective_tier=min(tiers, key=evidence_rank), episode_uuids=episode_uuids,
        anchor_value=None, basis=BASIS_COUNTED,
        contributed_occurrence_ids=[str(o.get("assertion_id") or "") for o in applied],
        absorbed_occurrence_ids=absorbed_ids,
    ))


#: bound on the item list stamped on a match-mode View.
_COUNT_ITEMS_MAX = 20


def _sum_values(rows: list[dict[str, Any]], value_kind: str) -> Any:
    if value_kind == "money":
        return sum((_money_decimal(o.get("value")) or Decimal("0") for o in rows), Decimal("0"))
    total = sum(float(o.get("value")) for o in rows)
    return int(total) if total == int(total) else total


def _fold_counted_match(
    result: FoldResult,
    slot_key: tuple[str, str, str, str, str],
    subject_uuid: str,
    value_kind: str,
    occurrences: list[dict[str, Any]],
    deltas: list[dict[str, Any]],
    counted_ids: set[str],
) -> None:
    """Match-mode counted fold (unit 4R). Safety: the value is the sum of `counted` rows only, so a
    retelling decided repeat/same_event never adds twice; every other live row (uncertain, no
    decision, or a repeat whose target is not a current counted row of this subject) is reported in
    `count_uncertain`, so no happening drops out of both.

    `counted_ids` are the current counted occurrences of the whole fold input (one subject, one
    namespace), so a repeat may target a counted row in another family slot. The delta absorb and
    the importance gate are as in `_fold_counted`."""
    if value_kind not in COUNTABLE_KINDS:
        result.abstentions.append(Abstention(slot_key, NOT_COUNTABLE, subject_uuid))
        return
    claimed_turns = {k for d in deltas for k in _source_keys(d)}
    live = [
        o for o in occurrences
        if not (_source_keys(o) & claimed_turns) and _is_number(o.get("value"))
    ]
    live_ids = {str(o.get("assertion_id") or "") for o in live}
    absorbed = [str(o.get("assertion_id") or "") for o in occurrences
                if str(o.get("assertion_id") or "") not in live_ids]

    counted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    for o in live:
        status = status_of(o)
        own_id = str(o.get("assertion_id") or "")
        target = str(o.get("count_target_id") or "")
        if status == COUNTED:
            counted.append(o)
        elif status in TARGETED_STATUSES and target and target != own_id and target in counted_ids:
            excluded.append(o)
        else:
            uncertain.append(o)

    shown = counted + uncertain
    if not shown:
        result.abstentions.append(Abstention(slot_key, NO_ANCHOR, subject_uuid))
        return
    if not any(str(o.get("importance") or "") in MATERIALIZING_IMPORTANCE for o in shown):
        result.abstentions.append(Abstention(slot_key, ROUTINE_ONLY, subject_uuid))
        return
    if value_kind == "money" and any(_money_decimal(o.get("value")) is None for o in shown):
        result.abstentions.append(Abstention(slot_key, INVALID_AMOUNT, subject_uuid))
        return

    def _order(o: dict[str, Any]) -> tuple:
        return (_parse_dt(o.get("valid_at")) or _EPOCH, str(o.get("assertion_id") or ""))

    counted.sort(key=_order)
    uncertain.sort(key=_order)
    basis_rows = counted or uncertain

    actions: dict[str, Any] = {}
    for o in counted:
        action = " ".join(str(o.get("count_action") or "").lower().split())
        if action:
            actions[action] = _sum_values(
                [o], value_kind) + actions.get(action, 0)
    items: list[str] = []
    seen_items: set[str] = set()
    for o in counted:
        item = " ".join(str(o.get("count_item") or "").split())
        if item and item.lower() not in seen_items and len(items) < _COUNT_ITEMS_MAX:
            seen_items.add(item.lower())
            items.append(item)

    episode_uuids: list[str] = []
    for o in counted + uncertain:
        eu = str(o.get("episode_uuid") or "")
        if eu and eu not in episode_uuids:
            episode_uuids.append(eu)
    counted_ids_here = [str(o.get("assertion_id") or "") for o in counted]
    first = basis_rows[0]
    result.states.append(FoldedScalarState(
        subject_uuid=subject_uuid,
        subject_display=str(first.get("subject_display", "") or subject_uuid),
        attribute=slot_key[1], scope=slot_key[2], value_kind=value_kind, unit=slot_key[4],
        value=_sum_values(counted, value_kind),
        valid_at=str(basis_rows[-1].get("valid_at") or ""),
        contributor_ids=counted_ids_here,
        effective_tier=min((str(o.get("evidence_tier") or "agent") for o in shown),
                           key=evidence_rank),
        episode_uuids=episode_uuids,
        anchor_value=None, basis=BASIS_COUNTED,
        contributed_occurrence_ids=counted_ids_here,
        absorbed_occurrence_ids=sorted(absorbed + [str(o.get("assertion_id") or "")
                                                   for o in excluded]),
        count_uncertain=_sum_values(uncertain, value_kind),
        count_actions=actions,
        count_items=items,
        uncertain_occurrence_ids=[str(o.get("assertion_id") or "") for o in uncertain],
    ))
