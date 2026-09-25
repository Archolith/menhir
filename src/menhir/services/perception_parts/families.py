"""F1 measure-family voting: collapse morphological/synonym measure-key scatter (e.g.
`bike_spend` / `bikes_spend` / `bikes_purchased`) into one semantic cluster per (subject, noun,
reducer) so the gate's self-consistency vote counts them as agreement instead of abstention.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import replace
from typing import Any

from menhir.services.perception_parts.keys import _merge_groups, _singularize_token
from menhir.services.perception_parts.model import PerceivedGroup

# ---------------------------------------------------------------------------- (1d) measure-family voting (F1)

#: tokens that name HOW a quantity is aggregated, not WHAT it is. Stripped from a measure key to leave
#: the bare noun signature, so `bike_spend`, `bikes_spend`, and `bikes_purchased` all reduce to the
#: same family {bike}. Reducer identity is carried SEPARATELY (the group's actual reducer, inferred
#: from event kinds — never from these words), so a genuine COUNT and a SUM of the same noun stay
#: distinct families and never merge.
_REDUCER_WORDS = frozenset({
    "spend", "spent", "spending", "cost", "costs", "price", "prices", "priced", "paid", "pay",
    "payment", "payments", "purchase", "purchases", "purchased", "bought", "buy", "buys", "buying",
    "count", "counts", "number", "num", "nums", "total", "totals", "amount", "amounts", "qty",
    "quantity", "quantities", "owned", "own", "owns", "have", "has", "acquired", "acquire",
    "acquires", "sum", "tally",
})

#: canonical suffix per reducer, used only when RENAMING a scattered family (never a solo measure).
_REDUCER_SUFFIX = {"sum": "spend", "count": "count", "distinct_count": "count", "stated": "count"}


def _measure_noun_sig(measure: str) -> frozenset[str]:
    """The bare-noun signature of a measure key: singularized tokens with the aggregation words
    removed, order-independent. Empty when the key is ALL aggregation words (e.g. a bare `spend`) —
    such keys are deliberately NOT family-merged (no noun to match on -> too generic to safely fold
    together), so they fall through to solo handling."""
    tokens = [tok for tok in re.split(r"[_\s\-]+", (measure or "").lower()) if tok]
    nouns = {_singularize_token(tok) for tok in tokens if tok not in _REDUCER_WORDS}
    return frozenset(n for n in nouns if n)


def _canonical_family_label(labels: list[str], reducer: str) -> str:
    """Deterministic canonical key for a SCATTERED family (>=2 distinct observed labels). Pick the
    representative that is already in canonical (singular) form, else the shortest/lexicographically
    smallest, then singularize every token so the output is stable regardless of which variant subset
    a given run happened to observe (`{bike_spend, bikes_spend, bikes_purchased}` -> `bike_spend`;
    `{bikes_spend, bikes_purchased}` -> `bike_spend`). Reducer words in the chosen label are kept as
    written (they are already conventional, e.g. `_spend`); a wholly noun-only label gets the reducer's
    canonical suffix appended so a count/sum is never mistaken for the other."""
    def _is_singular(m: str) -> bool:
        return all(_singularize_token(t) == t for t in re.split(r"[_\s\-]+", m) if t)

    rep = min(labels, key=lambda m: (0 if _is_singular(m) else 1, len(m), m))
    toks = [_singularize_token(t) for t in re.split(r"[_\s\-]+", rep) if t]
    if not any(t in _REDUCER_WORDS for t in toks):
        toks.append(_REDUCER_SUFFIX.get(reducer, "count"))
    return "_".join(toks)


def _merge_slot_lists(
    a: "list[PerceivedGroup | None]", b: "list[PerceivedGroup | None]",
    *, subject: str, measure: str,
) -> "list[PerceivedGroup | None]":
    """Per-sample union of two family members' slot lists: at each index keep whichever group is
    present, or merge both (provenance-deduped) when a single sample emitted two variants at once."""
    out: "list[PerceivedGroup | None]" = []
    for ga, gb in zip(a, b):
        present = [g for g in (ga, gb) if g is not None]
        if not present:
            out.append(None)
        elif len(present) == 1:
            g = present[0]
            out.append(g if g.measure == measure else replace(g, measure=measure))
        else:
            out.append(_merge_groups(present, subject=subject, measure=measure))
    return out


def _collapse_measure_families(
    by_key: "dict[tuple[str, str], list[PerceivedGroup | None]]",
) -> "dict[tuple[str, str], list[PerceivedGroup | None]]":
    """F1 — vote on the SEMANTIC CLUSTER, not the literal key. Group the per-sample measure slots by
    (subject, noun-signature, reducer) so morphological / synonym variants the alias table doesn't
    cover (`bike_spend` / `bikes_spend` / `bikes_purchased`, all SUM=125) are counted as AGREEMENT
    instead of scattering the self-consistency vote to abstention. Only families with >=2 distinct
    observed labels are rewritten (to a deterministic canonical key); a family of one passes through
    byte-for-byte, so existing single-label measures and all prior behavior are untouched. Reducer is
    part of the family identity, so a genuine COUNT and a SUM of the same noun never merge."""
    if not by_key:
        return by_key
    k = len(next(iter(by_key.values())))

    families: "dict[Any, list[tuple[str, str]]]" = defaultdict(list)
    for (subject, measure), slots in by_key.items():
        present = [g for g in slots if g is not None]
        reducer = Counter(g.reducer for g in present).most_common(1)[0][0] if present else "count"
        sig = _measure_noun_sig(measure)
        # no noun to match on -> keep solo (a bare `spend`/`count` is too generic to fold blindly).
        fam = (subject, sig, reducer) if sig else ("__solo__", subject, measure)
        families[fam].append((subject, measure))

    out: "dict[tuple[str, str], list[PerceivedGroup | None]]" = {}

    def _absorb(key: tuple[str, str], slots: "list[PerceivedGroup | None]") -> None:
        if key in out:
            out[key] = _merge_slot_lists(out[key], slots, subject=key[0], measure=key[1])
        else:
            out[key] = slots

    for fam, keys in families.items():
        if len(keys) == 1:
            _absorb(keys[0], by_key[keys[0]])  # solo family: unchanged (may still merge on canon collision)
            continue
        subject = keys[0][0]
        canon = _canonical_family_label([m for (_s, m) in keys], fam[2])
        merged: "list[PerceivedGroup | None]" = [None] * k
        for key in keys:
            merged = _merge_slot_lists(
                merged, [None if g is None else replace(g, measure=canon) for g in by_key[key]],
                subject=subject, measure=canon,
            )
        _absorb((subject, canon), merged)
    return out
