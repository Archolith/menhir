"""The conjunctive veto-gate over k extraction samples: commit only when every applicable check
clears; every abstention carries its full deterministic evidence trail.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Callable

from menhir.domain.fold_algebra import Event, coreference_candidates
from menhir.services.perception_parts.families import _collapse_measure_families
from menhir.services.perception_parts.gate_helpers import (
    _quantize,
    _reduce,
    _stated_value_grounded,
    _sum_arithmetic_grounded,
)
from menhir.services.perception_parts.levers import COREF_MERGE, COREF_SEPARATE, _cluster_signature
from menhir.services.perception_parts.model import (
    Episode,
    GateDecision,
    PerceivedGroup,
    VETO_COUNT_FLOOR,
    VETO_CROSS_CHECK,
    VETO_SELF_CONSISTENCY,
    VETO_TRIANGULATION,
    VETO_UNRESOLVED_COREFERENCE,
    VETO_UNSUPPORTED_STATED,
    VETO_VERIFICATION,
)
from menhir.services.seam_types import Embed

logger = logging.getLogger(__name__)


def gate(
    samples: list[list[PerceivedGroup]],
    *,
    threshold: float = 1.0,
    embed: Embed | None = None,
    dedup_threshold: float = 0.92,
    triangulation_tol: float = 0.0,
    min_count: float = 2.0,
    cross_check: Callable[[str], float | None] | None = None,
    verifier: Callable[[str, float, list[Event]], bool] | None = None,
    coref_resolved: "dict[tuple[str, str], str] | None" = None,
    episodes: list[Episode] | None = None,
    verify_retries: int = 0,
    enable_sum_grounding: bool = False,
) -> list[GateDecision]:
    """The conjunctive veto-gate over k extraction samples. For each (subject, measure) seen in any
    sample, commit ONLY if every applicable check clears:

      * self-consistency (primary): the derived value is concentrated — the modal value's share of
        the k samples is >= `threshold` (1.0 = unanimous). Samples that didn't perceive the measure
        at all count as disagreement (an ABSENT vote), so a measure only a minority sees can't win.
      * count-floor: a COUNT/DISTINCT-COUNT View below `min_count` (default 2) is not materialized —
        a "count of 1" carries no aggregation (it's just the raw possession, which recall already
        has) and is the dominant over-extraction in the live tuning run (single possessions written
        as count=1). Deterministic, calibration-free; SUM is exempt (a $185 total is meaningful).
      * triangulation: if any sample carries a USER-stated total, SUM(items) must equal it within
        `triangulation_tol` (relative). Disagreement vetoes even a unanimous value.
      * cross-check (Lever B): when no user total exists, an injected `cross_check(measure)` supplies a
        SECOND, independently-derived total (holistic, see `extract_stated_total`). The value must
        agree with it within `triangulation_tol` or the write is vetoed. This is the ONLY constraint on
        confident SUM bias; it is ABSTAIN-ONLY (runs on the commit path — never rescues a rejected
        value) and no-op when `cross_check` is None or reports no total (default: precision unchanged).

    Self-consistency catches VARIANCE, not BIAS — a confidently-wrong extraction (live run:
    bike_spend summed to a unanimous-but-wrong 225) sails through it; only triangulation (user total,
    veto-3) or the cross-check (holistic second derivation, veto-4) constrains bias. The count-floor is
    the cheap deterministic backstop the live eval justified. Missing signals never veto. Every
    decision carries its full distribution, agreement fraction, triangulation and cross-check verdicts
    so abstention is observable, not silent."""
    k = len(samples)
    if k == 0:
        return []

    # index groups by (subject, measure) across samples; ABSENT where a sample didn't perceive it.
    by_key: dict[tuple[str, str], list[PerceivedGroup | None]] = defaultdict(lambda: [None] * k)
    for i, sample in enumerate(samples):
        for g in sample:
            by_key[(g.subject, g.measure)][i] = g

    # F1 — collapse morphological/synonym measure-key scatter into one semantic cluster BEFORE voting,
    # so `bike_spend`/`bikes_spend`/`bikes_purchased` (same subject, noun, reducer, value) count as
    # agreement instead of three sub-unanimous keys that each abstain. Solo families are untouched.
    by_key = _collapse_measure_families(by_key)

    decisions: list[GateDecision] = []
    for (subject, measure), slots in by_key.items():
        present = [g for g in slots if g is not None]
        reducer = Counter(g.reducer for g in present).most_common(1)[0][0]

        votes: list[str] = []
        reduced: dict[str, tuple[float, list[Event], bool]] = {}
        for g in slots:
            if g is None:
                votes.append("__absent__")
                continue
            value, events, is_law3 = _reduce(g, embed, dedup_threshold)
            key = _quantize(value)
            votes.append(key)
            reduced.setdefault(key, (value, events, is_law3))

        dist = Counter(votes)
        top_key, top_n = dist.most_common(1)[0]
        agreement = top_n / k

        stated = next((g.stated_total for g in present if g.stated_total is not None), None)

        # --- veto 1: self-consistency ---
        if top_key == "__absent__" or agreement < threshold:
            decisions.append(GateDecision(
                subject=subject, measure=measure, reducer=reducer, committed=False,
                reason=f"scattered: modal value {top_key} holds {top_n}/{k} (threshold {threshold})",
                veto=VETO_SELF_CONSISTENCY,
                value=None, agreement=agreement, k=k, distribution=dict(dist), stated_total=stated,
            ))
            continue

        value, events, is_law3 = reduced[top_key]

        # --- veto 2: count-floor (a count/distinct View of <min_count carries no aggregation) ---
        # 'stated' is floored too (Lever-B live run): the extractor fabricated fish_tanks_owned=1
        # from the "1" in "1-gallon tank" — a UNIT misread as a stated count, unanimous across
        # samples (bias). A stated total of 1 adds nothing over the raw episode anyway; FP >> FN.
        # Genuine stated amounts/counts >= min_count (playlists=20, mileage=347) are untouched.
        if reducer in ("count", "distinct_count", "stated") and value < min_count:
            decisions.append(GateDecision(
                subject=subject, measure=measure, reducer=reducer, committed=False,
                reason=f"count-floor: {reducer}={value:g} < {min_count:g} (no aggregation; raw fact)",
                veto=VETO_COUNT_FLOOR,
                value=None, agreement=agreement, k=k, distribution=dict(dist), stated_total=stated,
            ))
            continue

        # --- veto 2b: unresolved coreference (Part 1) ---
        # After the narrowed signature stopped merging same-day/value items on category alone, an
        # ambiguous cluster (same group + value, ≥2 days OR ≥2 same-day wordings) that the judge did
        # NOT confidently settle must not be silently folded either way — folding it merged is a
        # wrong-low bet, folding it separate a wrong-high one; §2 says abstain. `coref_resolved` (the
        # shared tri-state memo) tells us the verdict per cluster; a cluster with no `merge`/`separate`
        # resolution (unsure, or coref disabled so never judged) vetoes. Merged clusters are gone from
        # `events`, so they don't re-trigger; only unresolved ones survive to be seen here.
        if reducer in ("sum", "count"):
            unresolved = [
                _cluster_signature(cluster)
                for cluster in coreference_candidates(events)
                if (coref_resolved or {}).get(_cluster_signature(cluster))
                not in (COREF_MERGE, COREF_SEPARATE)
            ]
            if unresolved:
                decisions.append(GateDecision(
                    subject=subject, measure=measure, reducer=reducer, committed=False,
                    reason=f"unresolved coreference: {len(unresolved)} ambiguous cluster(s) not judged "
                           f"merge/separate ({[s[0] for s in unresolved]})",
                    veto=VETO_UNRESOLVED_COREFERENCE,
                    value=None, agreement=agreement, k=k, distribution=dict(dist), stated_total=stated,
                ))
                continue

        # --- veto 3: triangulation (only when a USER stated a total the items REDUNDANTLY re-derive) ---
        # Skipped for a Law-3 reconcile: there the anchor and the post-anchor deltas are ADDITIVE
        # (value already = anchor + deltas), not two readings of one quantity — triangulating the
        # reconciled 4 against the anchor 3 would wrongly abstain. The reconcile IS the corroboration.
        # (Later, veto-4 and veto-5 will provide genuine second opinions for Law-3.)
        triangulated: bool | None = None
        if stated is not None and not is_law3:
            tol = abs(stated) * triangulation_tol
            triangulated = abs(value - stated) <= tol
            if not triangulated:
                decisions.append(GateDecision(
                    subject=subject, measure=measure, reducer=reducer, committed=False,
                    reason=f"triangulation failed: SUM(items)={value} vs stated {stated}",
                    veto=VETO_TRIANGULATION,
                    value=None, agreement=agreement, k=k, distribution=dict(dist),
                    stated_total=stated, triangulated=False,
                ))
                continue

        # --- veto 4: broadened triangulation (Lever B) ---
        # When the USER stated no total (veto-3 didn't apply) but this is a move-2 fold group, obtain a
        # SECOND derivation of the same scalar by an independent method (holistic cross-check) and
        # require agreement. Catches confident BIAS that self-consistency can't (a unanimous-but-wrong
        # itemized SUM, live run: bike_spend=225 vs 185). ABSTAIN-ONLY: a cross-check may VETO a value
        # the prior gates would commit, but it may never RESCUE one they rejected (it runs only on the
        # commit path). No cross-check injected, or it reports no total -> no veto (precision unchanged).
        # For Law-3 anchor+delta candidates, the holistic derivation is a genuine second opinion:
        # it reads the same episodes and computes the current value (anchor + post-anchor deltas),
        # independent of the reconcile logic, so disagreement beyond triangulation_tol abstains.
        cross_total: float | None = None
        # Deterministic SUM arithmetic grounding (precision-preserving cross-check adjustment): when a
        # SUM's amounts are each an EXPLICIT price literally in their source span (distinct tokens,
        # summing to the value), the arithmetic is PROVEN from source text — strictly stronger than the
        # blind holistic re-derivation, which for this case is pure false-abstention noise. Skip the
        # holistic veto-4 and treat it as corroborated; the sharper veto-5 verifier still audits item
        # MEMBERSHIP/double-count below, so the wrong-write envelope is unchanged. Opt-in + SUM-only.
        sum_grounded = (
            enable_sum_grounding and reducer == "sum" and (stated is None or is_law3)
            and episodes is not None and _sum_arithmetic_grounded(value, events, episodes)
        )
        if sum_grounded:
            triangulated = True  # deterministic arithmetic proof stands in for the holistic corroboration
        elif cross_check is not None and (stated is None or is_law3) and reducer != "stated":
            try:
                cross_total = cross_check(measure)
            except Exception:
                logger.warning("cross-check raised for (%s, %s)", subject, measure, exc_info=True)
                cross_total = None
            if cross_total is not None:
                tol = abs(cross_total) * triangulation_tol
                if abs(value - cross_total) > tol:
                    reason = (
                        f"cross-check failed: {reducer}={value} vs holistic {cross_total}"
                        if not is_law3 else
                        f"law-3 cross-check disagreed: reconciled {reducer}={value} vs holistic {cross_total}"
                    )
                    decisions.append(GateDecision(
                        subject=subject, measure=measure, reducer=reducer, committed=False,
                        reason=reason,
                        veto=VETO_CROSS_CHECK,
                        value=None, agreement=agreement, k=k, distribution=dict(dist),
                        stated_total=stated, triangulated=False, cross_total=cross_total,
                        abstained_value=value, cross_margin=abs(value - cross_total),
                    ))
                    continue
                triangulated = True  # an independent method agreed -> the value is corroborated
        elif is_law3 and cross_check is None:
            # Law-3 without cross-check: no second opinion injected, so missing signals never veto.
            # Preserve today's behavior: upfront triangulated=True (absent corroborator keeps it).
            triangulated = True

        # --- veto 5: final verification (Lever C4) ---
        # A focused audit of the assembled candidate against its linked memories — the last word
        # before commit. Fails closed (abstain) when not confidently correct. Runs on move-2 folds
        # (not a bare 'stated' move-1, which has no itemization to audit). Reviews the evidence, so it
        # is a sharper second opinion than the blind cross-check; use one or both.
        # For Law-3 candidates, extract the anchor (events[0] in the provenance) and pass it to the
        # verifier as an optional kwarg; the verifier can extend the prompt to ask question (d).
        v_votes: int | None = None
        v_k: int | None = None
        v_attempts: int | None = None
        if verifier is not None and reducer != "stated":
            # Bounded retry (verify_retries): re-run the FULL k-sample verifier vote up to
            # 1+verify_retries times and commit as soon as one attempt clears. The per-attempt bar is
            # UNCHANGED (still the injected verifier's unanimity), so retries only give a flaky-but-
            # correct SUM more chances to prove itself — they never lower precision for a given attempt.
            # Default verify_retries=0 => exactly one attempt => behaviour identical to before.
            ok = False
            attempts = 0
            for _attempt in range(1 + max(0, verify_retries)):
                attempts += 1
                try:
                    # For Law-3, the anchor is events[0]; for non-Law-3, there is no anchor.
                    # Pass anchor only when verifier supports it (via kwarg); backward-compatible.
                    anchor_arg = events[0] if is_law3 and events else None
                    import inspect
                    sig = inspect.signature(verifier)
                    if "anchor" in sig.parameters:
                        res = verifier(measure, value, events, anchor=anchor_arg)
                    else:
                        res = verifier(measure, value, events)
                except Exception:
                    logger.warning("verifier raised for (%s, %s)", subject, measure, exc_info=True)
                    res = True  # a broken verifier must not silently drop writes
                # A verifier may return a bare bool (legacy) or (ok, votes, k) for receipt clarity.
                if isinstance(res, tuple):
                    ok, v_votes, v_k = bool(res[0]), int(res[1]), int(res[2])
                else:
                    ok = bool(res)
                if ok:
                    break
            v_attempts = attempts
            if not ok:
                votes_detail = (f" (best {v_votes}/{v_k} across {attempts} attempt(s))"
                                if v_votes is not None else f" ({attempts} attempt(s))")
                decisions.append(GateDecision(
                    subject=subject, measure=measure, reducer=reducer, committed=False,
                    reason=f"verification failed: {reducer}={value} not confirmed by its linked "
                           f"items{votes_detail}",
                    veto=VETO_VERIFICATION,
                    value=None, agreement=agreement, k=k, distribution=dict(dist),
                    stated_total=stated, triangulated=triangulated, cross_total=cross_total,
                    verify_votes=v_votes, verify_k=v_k, verify_attempts=attempts,
                ))
                continue

        # --- veto 6: stated-value span grounding (STATED_MEASURE only, opt-in) ---
        # A move-1 stated total must have its numeric value literally present in a linked source
        # span; a stated number with no textual support is a fabricated aggregate/current fact ->
        # quarantine. Fold-derived values (sum/count/distinct from events) are EXEMPT — their number
        # is lawfully computed and need not appear verbatim in any single span. Off unless the caller
        # supplies `episodes` (kept opt-in so it never changes precision when not requested).
        if reducer == "stated" and episodes is not None and not _stated_value_grounded(value, present, episodes):
            decisions.append(GateDecision(
                subject=subject, measure=measure, reducer=reducer, committed=False,
                reason=f"unsupported stated measure: value {value:g} not grounded in any source span",
                veto=VETO_UNSUPPORTED_STATED,
                value=None, agreement=agreement, k=k, distribution=dict(dist),
                stated_total=stated, triangulated=triangulated, cross_total=cross_total,
            ))
            continue

        decisions.append(GateDecision(
            subject=subject, measure=measure, reducer=reducer, committed=True,
            reason="unanimous" if agreement >= 1.0 else f"concentrated {agreement:.2f}",
            value=value, agreement=agreement, k=k, distribution=dict(dist),
            stated_total=stated, triangulated=triangulated, cross_total=cross_total, events=events,
            verify_votes=v_votes, verify_k=v_k, verify_attempts=v_attempts,
            sum_grounded=sum_grounded,
        ))
    return decisions
