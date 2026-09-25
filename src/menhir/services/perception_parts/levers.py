"""The independent levers over a candidate value: holistic cross-check (Lever B), event
coreference (Lever C3), the final verification audit (Lever C4), and the embedding dedup that
backs `distinct_count` identity resolution (handoff sec 3).
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

from menhir.domain.fold_algebra import Event, coreference_candidates
from menhir.services.perception_parts.model import Episode
from menhir.services.perception_parts.prompts import (
    COREFERENCE_PROMPT,
    STATED_TOTAL_PROMPT,
    VERIFY_PROMPT,
    VERIFY_PROMPT_WITH_ANCHOR,
    VERIFY_SYSTEM_PROMPT,
)
from menhir.services.seam_types import Embed, LlmComplete

# ---------------------------------------------------------------------------- (1b) holistic cross-check (Lever B)


def _parse_json_object(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    text = re.sub(r"(:\s*)\+(\d)", r"\1\2", text)  # LLMs emit "+1"; invalid JSON, strip the +
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def extract_stated_total(episodes: list[Episode], measure: str, llm_complete: LlmComplete) -> float | None:
    """Lever B — a second, INDEPENDENT derivation of a measure's total, by a different method than the
    itemized extractor. One holistic, query-blind call ("reading everything, what is the total for
    <measure>?") whose error channel is disjoint from the itemized SUM: a double-count that inflates
    the itemized path (bike_spend -> 225) is not reproduced by the whole-picture read (185), so they
    disagree and the gate abstains. Returns the holistic total, or None when the model reports no basis
    for one (null / unparseable) — None means "no cross-check available", never "0". Deterministic parse
    of a `{"total": <number|null>}` object; k=1 (holistic totals are stable, per the plan cost guard)."""
    if not episodes or not measure:
        return None
    log = "\n".join(f"[{i}] {e.content}" for i, e in enumerate(episodes))
    obj = _parse_json_object(
        llm_complete(STATED_TOTAL_PROMPT, f"quantity: {measure}\n\nepisodes:\n{log}")
    )
    raw = obj.get("total")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------- (1c) event coreference (Lever C3)


def _mention_line(e: Event) -> str:
    quote = (e.what or e.identity or "").strip()
    val = f"${e.value:g}" if e.value is not None else "?"
    return f"- [{str(e.when)[:10]}] {val}: \"{quote[:120]}\""


def _cluster_signature(cluster: list[Event]) -> tuple[str, str]:
    e = cluster[0]
    return ((e.category or e.identity or "").strip().lower(),
            f"{float(e.value):.2f}" if e.value is not None else "")


#: tri-state coreference resolution stored in the shared memo. `merge` = confidently one purchase
#: (votes/k ≥ threshold); `separate` = confidently distinct (zero same-votes); `unsure` = a split
#: judge in between. Only `merge` and `separate` are RESOLUTIONS the gate trusts; `unsure` (and a
#: candidate never judged at all — coref disabled) is unresolved ambiguity that vetoes the write.
COREF_MERGE = "merge"
COREF_SEPARATE = "separate"
COREF_UNSURE = "unsure"


def resolve_coreference(
    events: list[Event], judge: LlmComplete, *, k: int = 3, threshold: float = 1.0,
    memo: "dict[tuple[str, str], str] | None" = None,
) -> list[Event]:
    """Lever C3 — collapse a purchase RE-NARRATED (across dates, or the same day in different words)
    into one, using the 'determinism finds candidates → LLM judges → confidence gates' design.

    `coreference_candidates` (deterministic, pure) proposes same-value/same-group clusters that exact
    dedup can't resolve — the ambiguous case. For each, the `judge` is asked k times (temp>0) whether
    the mentions are ONE purchase, yielding a TRI-STATE verdict (`merge`/`separate`/`unsure`) recorded
    in `memo`. We merge ONLY on `merge` (agreement ≥ `threshold`). Precision-first: `separate` and
    `unsure` leave the cluster intact — but the distinction matters to the gate, which vetoes a measure
    whose ambiguity is `unsure` (or never judged) rather than folding an unresolved cluster. Merging
    keeps the earliest mention as the representative and drops the rest.

    `memo` caches the tri-state verdict by (item, value) signature so the SAME cluster isn't re-judged
    across the gate's k extraction samples (cost guard), AND so the gate can read the resolution state."""
    clusters = coreference_candidates(events)
    if not clusters:
        return events

    drop: set[int] = set()
    for cluster in clusters:
        sig = _cluster_signature(cluster)
        if memo is not None and sig in memo:
            state = memo[sig]
        else:
            votes = sum(
                1 for _ in range(max(1, k))
                if _parse_json_object(judge(COREFERENCE_PROMPT.format(
                    mentions="\n".join(_mention_line(e) for e in cluster)), "")).get("same_purchase") is True
            )
            frac = votes / max(1, k)
            state = COREF_MERGE if frac >= threshold else (COREF_SEPARATE if votes == 0 else COREF_UNSURE)
            if memo is not None:
                memo[sig] = state
        if state != COREF_MERGE:
            continue  # separate/unsure → leave intact; the gate reads memo state to veto if unsure
        rep = min(cluster, key=lambda e: str(e.when) or "~")  # keep the earliest mention
        for e in cluster:
            if e is not rep:
                drop.add(id(e))
    return [e for e in events if id(e) not in drop]


def verify_candidate(
    measure: str, value: float, events: list[Event], judge: LlmComplete, *,
    k: int = 3, threshold: float = 1.0, anchor: Event | None = None,
) -> bool:
    """Lever C4 — the FINAL commit gate: a focused LLM audit of an assembled candidate against the
    linked memories that produced it. Unlike the Lever-B cross-check (a BLIND holistic re-derivation,
    hence noisy — 165 vs a correct 185 on a hard question), this shows the judge the exact constituent
    items (quote + date + amount) and asks whether they correctly total the measure — all on-topic,
    none double-counted, arithmetic sound. k-sample; returns True only if confidently correct
    (agreement ≥ `threshold`). Precision-first: an unsure verdict fails closed (abstain). Meant to
    replace the noisy cross-check as the second opinion — it reviews the evidence instead of re-guessing.

    When `anchor` is provided (Law-3 anchor+delta reconciliation), the prompt is extended to ask
    question (d): could any listed post-anchor item already be included in the stated base? This
    guards against re-mention-as-delta errors. Non-Law-3 candidates produce the same three-question
    prompt."""
    if not events:
        return True  # nothing itemized to audit (e.g. a move-1 stated total); no veto
    items = "\n".join(_mention_line(e) for e in events)
    if anchor is not None and anchor.value is not None:
        # Law-3 candidate: render the anchor as the stated base, and post-anchor items as deltas
        prompt = VERIFY_PROMPT_WITH_ANCHOR.format(
            measure=measure,
            stated_value=f"{anchor.value:g}",
            stated_when=anchor.when or "unknown",
            value=f"{value:g}",
            items=items
        )
    else:
        # Non-Law-3: use the standard three-question prompt
        prompt = VERIFY_PROMPT.format(measure=measure, value=f"{value:g}", items=items)
    votes = sum(
        1 for _ in range(max(1, k))
        if _parse_json_object(judge(VERIFY_SYSTEM_PROMPT, prompt)).get("correct") is True
    )
    return votes / max(1, k) >= threshold


def verify_candidate_detailed(
    measure: str, value: float, events: list[Event], judge: LlmComplete, *,
    k: int = 3, threshold: float = 1.0, anchor: Event | None = None,
) -> tuple[bool, int, int]:
    """Like `verify_candidate`, but also returns (ok, votes, k) so a fail-closed SUM carries HOW CLOSE
    the audit was (votes/k) into the abstention receipt — the 'verifier receipt clarity' the fold-SUM
    stochasticity work needs. Same unanimity bar as `verify_candidate`; the extra return is additive
    (the plain `verify_candidate` is untouched for its existing callers)."""
    kk = max(1, k)
    if not events:
        return True, kk, kk  # nothing itemized to audit (move-1 stated total); no veto
    items = "\n".join(_mention_line(e) for e in events)
    if anchor is not None and anchor.value is not None:
        prompt = VERIFY_PROMPT_WITH_ANCHOR.format(
            measure=measure, stated_value=f"{anchor.value:g}",
            stated_when=anchor.when or "unknown", value=f"{value:g}", items=items)
    else:
        prompt = VERIFY_PROMPT.format(measure=measure, value=f"{value:g}", items=items)
    votes = sum(
        1 for _ in range(kk)
        if _parse_json_object(judge(VERIFY_SYSTEM_PROMPT, prompt)).get("correct") is True
    )
    return (votes / kk >= threshold, votes, kk)


# ---------------------------------------------------------------------------- (3b) embedding dedup


def _canonicalize_identities(events: list[Event], embed: Embed, threshold: float) -> list[Event]:
    """DISTINCT identity resolution (handoff sec 3): cluster item identities by cosine similarity and
    rewrite each to its cluster's canonical label, so the deterministic `distinct_count` matches what
    a human would count. Conservative single-link with a HIGH threshold — bias toward keeping items
    SEPARATE unless clearly the same (precision-first; over-merging silently under-counts). On any
    embed failure the identity is left as-is (degrades to exact-string DISTINCT, never blocks)."""
    idents = [e.identity for e in events if e.identity]
    if len(set(idents)) < 2:
        return events

    canon: dict[str, str] = {}
    vecs: dict[str, list[float]] = {}
    for ident in dict.fromkeys(idents):  # unique, order-preserving
        try:
            v = embed(ident)
        except Exception:
            v = None
        if not v:
            canon[ident] = ident
            continue
        matched = None
        for rep, rv in vecs.items():
            if _cosine(v, rv) >= threshold:
                matched = rep
                break
        if matched is None:
            vecs[ident] = v
            canon[ident] = ident
        else:
            canon[ident] = canon[matched]

    return [
        e if not e.identity else replace(e, identity=canon.get(e.identity, e.identity))
        for e in events
    ]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
