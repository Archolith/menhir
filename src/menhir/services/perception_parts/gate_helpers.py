"""Deterministic helpers behind the gate: reduce one group to its scalar (with Law-3 anchor+delta
reconciliation), and the span-grounding proofs that can stand in for a noisy holistic cross-check.
"""

from __future__ import annotations

import re
from collections import defaultdict

from menhir.domain.fold_algebra import Event, _parse as _parse_dt, dedup_events
from menhir.services.perception_parts.levers import _canonicalize_identities
from menhir.services.perception_parts.model import Episode, PerceivedGroup, _SCALAR_REDUCERS
from menhir.services.seam_types import Embed

# ---------------------------------------------------------------------------- (2) the gate


def _after(when: str | None, anchor_when: str | None) -> bool:
    """True iff `when` is strictly after `anchor_when` (both tolerant-parsed). Unparseable/either-None
    -> False (conservative: not a post-anchor delta, so it stays with the redundant/triangulation path
    rather than being wrongly added on top of the anchor)."""
    a, b = _parse_dt(when), _parse_dt(anchor_when)
    return a is not None and b is not None and a > b


def _reduce(group: PerceivedGroup, embed: Embed | None, dedup_threshold: float
            ) -> tuple[float, list[Event], bool]:
    """Deterministically fold one sample's group to its scalar. Returns (value, provenance_events,
    law3_reconciled). Identity dedup runs first for distinct_count.

    Four cases, unified by the fold-algebra Law-3 rule `CURRENT = anchor + reduce(events after anchor)`:
      * 'stated' (move-1, no events): value = the stated total (its assertion carries provenance).
      * events, no anchor (move-2): value = reduce(all events).
      * events + anchor, with events AFTER the anchor (Law-3 anchor+delta): value = anchor.value +
        reduce(post-anchor events). The stated total is a BASE the later deltas accrue onto — e.g.
        "I have 3 tanks" then "bought another" -> 3 + 1 = 4. `law3_reconciled=True` tells the gate to
        skip the redundant-triangulation veto (anchor and deltas are additive, not two readings of one
        thing). Provenance = the anchor + the post-anchor events.
      * events + anchor, all events AT/BEFORE the anchor: NOT reconciled here — those events are what
        the anchor summarizes, so value = reduce(all events) and the gate triangulates it vs the anchor
        (the existing redundant-derivation cross-check; behaviour unchanged)."""
    if group.reducer == "stated":
        ev = group.stated_event
        return (float(ev.value) if ev and ev.value is not None else 0.0,
                [ev] if ev is not None else [], False)
    events = group.events
    if group.reducer == "distinct_count" and embed is not None:
        events = _canonicalize_identities(events, embed, dedup_threshold)
    elif group.reducer in ("sum", "count"):
        # Lever C2 — collapse one occurrence narrated across episodes before a NON-idempotent reduce
        # (else "$40 lights" in two episodes sums to $80). distinct_count already dedups by identity,
        # so only sum/count need this. Precision-first merge bias (undercount safer than inflation).
        events = dedup_events(events)
    fn = _SCALAR_REDUCERS.get(group.reducer, _SCALAR_REDUCERS["count"])

    anchor = group.stated_event
    if anchor is not None and anchor.value is not None:
        post = [e for e in events if _after(e.when, anchor.when)]
        if post:  # Law-3: anchor re-bases; only events after it are deltas
            return float(anchor.value) + fn(post), [anchor, *post], True
    return fn(events), events, False


def _quantize(value: float) -> str:
    """Bucket a derived value into the agreement key. Integers/counts compare exactly; money compares
    to the cent so trivial float noise doesn't fracture an otherwise-unanimous vote."""
    return f"{value:.2f}"


def _stated_value_grounded(
    value: float, groups: list["PerceivedGroup"], episodes: list[Episode]
) -> bool:
    """True if a STATED_MEASURE's numeric value is literally present (as digits) in a linked source
    span — the cheap 'no source span, no stated fact' guard. Checks the assertion's linked episode(s)
    when provenance is known, else every episode in the batch. Digits only, deliberately simple: a
    word-number ('twenty') is treated as ungrounded. This applies ONLY to reducer='stated' outputs;
    fold-derived sums/counts are lawfully computed and are never subject to this check."""
    text_by_uuid = {e.uuid: (e.content or "") for e in episodes}
    linked = [
        g.stated_event.episode_uuid
        for g in groups
        if g.stated_event is not None and g.stated_event.episode_uuid
    ]
    spans = [text_by_uuid[u] for u in linked if u in text_by_uuid]
    if not spans:
        spans = [e.content or "" for e in episodes]
    needles = {f"{value:g}"}
    if float(value).is_integer():
        needles.add(str(int(value)))
    return any(n in span for span in spans for n in needles)


def _price_token_count(span: str, amount: float) -> int:
    """Count occurrences of `amount` as a STANDALONE explicit price in `span` — digit-boundary guarded
    so it never matches a number embedded in a larger one ('50' must NOT match inside '150' or '50.5').
    Matches '50', '$50', '50.00', '50 dollars' for amount 50. Deliberately conservative: an amount we
    can't match as a clean standalone price simply counts 0 (→ 'not grounded' → the caller falls back
    to the holistic cross-check, never a wrong commit)."""
    if not span:
        return 0
    forms: set[str] = set()
    if float(amount).is_integer():
        forms.add(str(int(amount)))          # 50
        forms.add(f"{int(amount)}.00")       # 50.00
        forms.add(f"{int(amount)}.0")        # 50.0
    forms.add(f"{amount:g}")                  # 50 / 50.5
    forms.add(f"{amount:.2f}")                # 50.00 / 50.50
    total = 0
    for form in forms:
        # standalone number, optional leading '$': not preceded by a digit or '.', and not followed by
        # a digit OR a decimal continuation ('.<digit>'). So '50' rejects '150'/'2050'/'50.5'/'2.50'
        # but ALLOWS a trailing sentence period ('$50.'). Optional leading '$'.
        pat = re.compile(rf"(?<![\d.])\$?{re.escape(form)}(?!\d)(?!\.\d)")
        total += len(pat.findall(span))
    return total


def _sum_arithmetic_grounded(
    value: float, events: list[Event], episodes: list[Episode] | None
) -> bool:
    """DETERMINISTIC proof that a SUM candidate's arithmetic is sound from the SOURCE TEXT — no LLM.

    True IFF, for the summed provenance `events`:
      1. every event has a numeric `value` and a known source-episode span, AND
      2. `sum(values)` equals the candidate `value` to the cent, AND
      3. anti-double-count: within each source span, each distinct amount claimed from it is covered by
         at least that many DISTINCT standalone explicit-price occurrences (two $40 events grounded in
         one span that says '$40' once -> NOT grounded).
    Any miss -> False, and the caller falls through to the holistic cross-check unchanged. So this can
    NEVER rescue a hallucinated-price, in-span double-counted, or mis-summed candidate; it only proves
    the arithmetic for the clean case ('$50 and $75' -> 125) where the blind holistic re-derivation is
    pure false-abstention noise. Cross-episode re-narration double-counts are caught UPSTREAM (Lever C2
    dedup + the veto-2b unresolved-coreference gate), before this runs."""
    if not events or episodes is None:
        return False
    text_by_uuid = {e.uuid: (e.content or "") for e in episodes}
    by_uuid: dict[str, list[float]] = defaultdict(list)
    for e in events:
        if e.value is None:
            return False
        uid = e.episode_uuid
        if not uid or uid not in text_by_uuid:
            return False  # an event with no known source span can't be deterministically grounded
        by_uuid[uid].append(float(e.value))
    total = sum(a for amts in by_uuid.values() for a in amts)
    if abs(total - float(value)) >= 0.005:
        return False
    for uid, claimed in by_uuid.items():
        span = text_by_uuid[uid]
        for amt in set(claimed):
            if _price_token_count(span, amt) < claimed.count(amt):
                return False
    return True
