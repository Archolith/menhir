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
from typing import Any

from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import (
    evidence_rank,
    normalize_scalar,
    slot_of as _slot_of,
)

# abstention reason codes (stable, for telemetry).
NO_ANCHOR = "no_anchor"
AMBIGUOUS_ANCHOR = "ambiguous_anchor"
DELTA_ON_RANGE = "delta_on_range"

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
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def fold_assertions(
    rows: list[dict[str, Any]], *, as_of: datetime | None = None,
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
    if as_of is not None:
        rows = [r for r in rows if (_parse_dt(r.get("valid_at")) or _EPOCH) <= as_of]
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
            result.abstentions.append(Abstention(slot_key, NO_ANCHOR, subject_uuid))
            continue

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
