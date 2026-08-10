"""Perception boundary — episodes -> typed `Event`s, precision-first and abstaining.

Design of record: `.agent/for-review/HANDOFF-2026-07-02-perception-boundary.md`.

The one invariant: **perception may be probabilistic; folds and Views must stay deterministic.**
The LLM's ONLY job is here — turn prose episodes into typed `fold_algebra.Event`s and decide
*whether* the derived value is trustworthy enough to materialize as a View. Once an Event list is
committed, everything downstream (`event_fold.fold_events_to_counter`, `ViewRepository`) is pure
arithmetic. No probability ever crosses into rho/delta.

The rule (the spine): **when uncertain, do not write the View.** A missed View is annoying; a wrong
current-state View is dangerous (it ranks well and looks authoritative — Arm B: FP >> FN). Abstention
is safe *for free*: raw episodes always ingest as normal memory and Views are purely additive, so the
absence of a View IS the fallback (recall returns the raw episode). No fallback code to write.

Confidence is a CONJUNCTIVE veto-gate over three signals (no fitted weights — ~14 labeled questions
is nowhere near enough to calibrate a score):

  1. self-consistency entropy  — extract k times (temp>0); commit only if the derived value is
     concentrated (near-unanimous). Scattered -> abstain. The primary gate.
  2. fold triangulation        — if perception emits BOTH item events and a STATED total, they are
     two independent derivations; SUM(items) must agree with the stated total or we abstain.
  3. embedding dedup           — DISTINCT-COUNT is right only if "5-gallon tank" and "the 5 gallon
     one" resolve to one item; cluster identities conservatively (bias toward SEPARATE).

Any single red flag -> abstain. Missing signals don't veto (no stated total => triangulation simply
doesn't apply). The agreement fraction, triangulation check, and cluster count are all computed
DETERMINISTICALLY from the stochastic samples — stochastic input, deterministic decision.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol

from menhir.domain.fold_algebra import (
    Event, _parse as _parse_dt, coreference_candidates, count, dedup_events, distinct_count,
    latest, sum_,
)
from menhir.infrastructure import consolidation_audit as _audit

logger = logging.getLogger(__name__)

#: (system, user) -> completion text. Injected so perception is decoupled from any specific LLM.
LlmComplete = Callable[[str, str], str]
Embed = Callable[[str], "list[float] | None"]

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

SYSTEM_PROMPT = (
    "You convert a user's memory episodes into COUNTABLE EVENTS for a deterministic aggregator. "
    "Read the numbered episodes and emit one event per concrete, dated fact that contributes to a "
    "running quantity the user might later ask about (how much they spent, how many things they own, "
    "how many times something happened). Each event is an object: "
    "{\"episode\": <number>, \"subject\": <who/what, e.g. 'user'>, \"measure\": <stable snake_case "
    "key naming the quantity, e.g. 'grocery_spend' or 'mugs_owned'>, \"kind\": one of "
    "\"purchase\"|\"item\"|\"acquire\"|\"assertion\", \"when\": <ISO date from the episode>, "
    "\"value\": <number, for purchase/assertion>, \"identity\": <normalized thing name, for item>, "
    "\"category\": <a short lowercase THEME the user might ask a running total for — the activity or "
    "area this belongs to, e.g. 'gardening', 'groceries', 'dining', 'home office'>, \"what\": <short "
    "quote>}. Give category for EVERY purchase and item (classify the single thing on its own — a "
    "trowel is 'gardening', a monitor is 'home office' — do NOT decide grouping yourself; just tag "
    "the item). "
    "Rules: use kind=purchase for a dated amount spent; kind=item for one distinct possessed thing "
    "(give a normalized identity); kind=acquire for a dated ACQUISITION of a thing (bought / got / "
    "received / adopted — 'I bought a cordless drill at the store', 'a friend gave me an old wrench'): "
    "put the acquisition DATE in when, the specific thing in identity, and key it to the CATEGORY in "
    "measure (e.g. measure='tools_acquired' identity='cordless drill'). Emit acquire ONLY for the event "
    "of getting a thing — NOT for merely owning or mentioning one you already had. Key acquisitions "
    "of the same category to ONE measure (identity distinguishes the items). kind=assertion "
    "when the user STATES a total explicitly: this includes an amount ('I've spent $60 total') AND a "
    "direct count claim ('I have 15 mugs', 'I own 2 cars') — put the number in value and name "
    "the thing in measure (e.g. measure='mugs' value=15). A stated count is an assertion, NOT a "
    "single item. Canonicalize the SAME quantity to the SAME measure key across episodes. Do NOT do "
    "arithmetic yourself; emit the atomic events and let the aggregator sum/count them. "
    "Output ONLY a JSON array of events."
)

#: The Lever-B holistic cross-check (a DIFFERENT derivation method from the itemized extractor above).
#: `SYSTEM_PROMPT` decomposes prose into atomic events that the deterministic sink SUMs/counts; a
#: double-count or spurious item there silently inflates the itemized total. This prompt asks for the
#: total HOLISTICALLY in one shot — a second, independent error channel. When the two derivations
#: disagree the gate abstains (veto-4). It never fabricates: no basis for a total -> null (no veto).
STATED_TOTAL_PROMPT = (
    "You are given a user's memory episodes and the NAME of one quantity they track. Reading ALL the "
    "episodes together, answer ONE question holistically: what is the single overall total for "
    "'{measure}'? Give your best whole-picture figure as a plain number — do NOT show itemized work, "
    "and do NOT invent a total the episodes give no basis for. If the episodes do not support a "
    "single total for this quantity, answer null. "
    "Output ONLY a JSON object: {{\"total\": <number or null>}}."
)

#: Lever C3 coreference judge. Determinism can't tell a purchase RE-NARRATED across dates ("I got new
#: bike lights, $40" said twice) from a RECURRING purchase (a $5 coffee every day) — both look like
#: same-value/different-day. Only the narrative distinguishes them, so the LLM judges; k-sample
#: self-consistency is the confidence, and we only MERGE on agreement (precision-first — an unsure
#: judge leaves them separate, and the cross-check backstop still catches the inflation).
COREFERENCE_PROMPT = (
    "A user's memory mentions these purchases, each a quote with its date and amount. Decide whether "
    "they all describe the SAME single real-world purchase mentioned more than once (people re-tell "
    "the same event on different days, and the recorded dates can differ), OR separate purchases "
    "(e.g. a recurring habit). Judge from the wording and context, not the dates alone. "
    "Mentions:\n{mentions}\n"
    "Output ONLY a JSON object: {{\"same_purchase\": true or false}}."
)

#: Lever C4 — the FINAL commit gate. Where the Lever-B cross-check re-derives the total BLIND over all
#: episodes (noisy: it misses or mis-includes items), this AUDITS the assembled candidate against the
#: exact linked memories that produced it: are all items on-topic for the measure, is any the same
#: purchase double-counted, does the arithmetic hold, is something obviously missing? A focused review
#: of the evidence, not a blind re-count. k-sample -> confidence; commit only if confidently correct.
VERIFY_PROMPT = (
    "A memory system assembled this stored fact and needs a final check before saving it.\n"
    "  measure: {measure}\n  computed total: {value}\n"
    "It was built by adding up exactly these recorded items (quote — date — amount):\n{items}\n"
    "Check, using ONLY the listed items: (a) does every item genuinely belong to '{measure}'? "
    "(b) is the same real-world purchase listed more than once (double-counted)? (c) does the total "
    "equal the sum of the amounts? Answer whether this is a correct, trustworthy fact to store. "
    "Output ONLY a JSON object: {{\"correct\": true or false}}."
)

VERIFY_PROMPT_WITH_ANCHOR = (
    "A memory system assembled this stored fact and needs a final check before saving it.\n"
    "  measure: {measure}\n  stated base: {stated_value} on {stated_when}\n  computed total: {value}\n"
    "The stated base was anchored on a specific date, and the current total was computed by: "
    "anchor + any items recorded AFTER that date. Those post-anchor items are:\n{items}\n"
    "Check, using ONLY the listed post-anchor items: (a) does every item genuinely belong to '{measure}'? "
    "(b) is the same real-world purchase listed more than once (double-counted)? (c) does the total "
    "equal the anchor plus the sum of the amounts? (d) could any listed post-anchor item already be "
    "included in the stated base? Answer whether this is a correct, trustworthy fact to store. "
    "Output ONLY a JSON object: {{\"correct\": true or false}}."
)


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


# ---------------------------------------------------------------------------- (1) extractor


def _parse_json_array(text: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    text = re.sub(r"(:\s*)\+(\d)", r"\1\2", text)  # LLMs emit "+1"; invalid JSON, strip the +
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def _infer_reducer(kinds: list[str]) -> str:
    """A group's reducer = the reducer of its most common countable kind. distinct_count wins ties
    with sum only if items are present (a possession measure), else sum, else count."""
    reducers = [_KIND_REDUCER[k] for k in kinds if k in _KIND_REDUCER]
    if not reducers:
        return "count"
    return Counter(reducers).most_common(1)[0][0]


def extract_once(episodes: list[Episode], llm_complete: LlmComplete) -> list[PerceivedGroup]:
    """One extraction pass: prose -> typed events grouped by (subject, measure). Each event is
    attributed to its source episode's uuid for provenance / the Law-2 replay ledger. Stated totals
    (kind=assertion) are peeled into `stated_event` (the LATEST is the live claim) — used to
    triangulate a fold, or, for a group with no fold events, to commit as the move-1 value directly."""
    if not episodes:
        return []
    log = "\n".join(f"[{i}] {e.content}" for i, e in enumerate(episodes))
    raw = _parse_json_array(llm_complete(SYSTEM_PROMPT, log))

    grouped: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"events": [], "kinds": [], "stated": []}
    )
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        subject = str(ev.get("subject") or "user").strip().lower()
        measure = str(ev.get("measure") or "").strip().lower()
        kind = str(ev.get("kind") or "").strip().lower()
        when = str(ev.get("when") or "").strip()
        if not measure or not when:
            continue
        try:
            idx = int(ev.get("episode"))
            uuid = episodes[idx].uuid if 0 <= idx < len(episodes) else None
        except (TypeError, ValueError):
            uuid = None

        cell = grouped[(subject, measure)]
        if kind == "assertion":
            try:
                val = float(ev.get("value"))
            except (TypeError, ValueError):
                continue
            cell["stated"].append(Event(
                when=when, kind="assertion", value=val,
                what=str(ev.get("what")).strip() if ev.get("what") is not None else None,
                episode_uuid=uuid,
            ))
            continue

        value = None
        if ev.get("value") is not None:
            try:
                value = float(ev.get("value"))
            except (TypeError, ValueError):
                value = None
        identity = ev.get("identity")
        category = ev.get("category")
        cell["events"].append(
            Event(
                when=when,
                kind=kind or "occurrence",
                value=value,
                identity=str(identity).strip() if identity is not None else None,
                what=str(ev.get("what")).strip() if ev.get("what") is not None else None,
                episode_uuid=uuid,
                category=str(category).strip().lower() if category else None,
            )
        )
        cell["kinds"].append(kind)

    out: list[PerceivedGroup] = []
    for (subject, measure), cell in grouped.items():
        # LWW register over stated totals: the latest-dated assertion is the live claim.
        stated_event = latest(cell["stated"]) if cell["stated"] else None
        if not cell["events"] and stated_event is None:
            continue
        out.append(
            PerceivedGroup(
                subject=subject,
                measure=measure,
                # 'stated' = a pure move-1 group (the assertion IS the value; no events to fold).
                reducer=_infer_reducer(cell["kinds"]) if cell["events"] else "stated",
                events=cell["events"],
                stated_total=stated_event.value if stated_event is not None else None,
                stated_event=stated_event,
            )
        )
    return out + _category_spend_groups(out)


def _category_spend_groups(groups: list[PerceivedGroup]) -> list[PerceivedGroup]:
    """Lever C1 — derive `<category>_spend` SUM groups from per-item purchase events' `category` tags.
    The extractor tags each item locally (helmet→'biking'), a stable judgment; grouping is then
    DETERMINISTIC — sum all purchase events sharing a category. This makes a HETEROGENEOUS total
    (bike lights + helmet + chain → biking_spend) a first-class measure the gate can vote on, where
    the item name alone never groups. Per-item measures are left intact (independent facts); the
    category total is additive. Only categories with ≥2 distinct items are synthesized — a single
    categorized item is already its own measure (and would trip the count-floor as a SUM of one)."""
    by_cat: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for g in groups:
        for e in g.events:
            if e.kind in ("purchase", "spend") and e.value is not None and e.category:
                by_cat[(g.subject, e.category)].append(e)
    synth: list[PerceivedGroup] = []
    for (subject, category), events in by_cat.items():
        if len({e.identity or e.what for e in events}) < 2:
            continue  # a lone item is already its own measure; no category aggregation to add
        synth.append(PerceivedGroup(subject=subject, measure=f"{category}_spend",
                                    reducer="sum", events=events))
    return synth


# ---------------------------------------------------------------------------- (1a) measure-key canonicalization

#: raw extractor measure label -> canonical key, applied BEFORE the consistency gate groups by
#: (subject, measure). Seeded ONLY from OBSERVED scatter (the live gpt-4.1-nano cycling/bike-spend
#: run where one concept was keyed many ways across k samples, plus the handoff's watch-list case).
#: Deliberately small: a targeted alias table, NOT an ontology. Canonical values are snake_case
#: (consistent with existing measures like `bike_spend`) and are never themselves keys, so
#: canonicalization is idempotent. NOTE: `bike_spend`/`playlists`/`bikes`/`tanks` are intentionally
#: NOT aliased — existing measures/tests depend on those names.
_MEASURE_ALIASES: dict[str, str] = {
    # cycling / bike-parts spend — one running total the extractor keyed many ways
    "cycling_cost": "cycling_spend",
    "cycling_parts_cost": "cycling_spend",
    "cycling_parts_spend": "cycling_spend",
    "cycling_accessories_cost": "cycling_spend",
    "cycling_accessories_spend": "cycling_spend",
    "bike_parts_spend": "cycling_spend",
    "biking_spend": "cycling_spend",
    # watch-list item count (handoff example)
    "to_watch_count": "watchlist_item_count",
    "watch_list_count": "watchlist_item_count",
    "watchlist_count": "watchlist_item_count",
    "watchlist_items": "watchlist_item_count",
    "movies_to_watch": "watchlist_item_count",
    "pending_media_count": "watchlist_item_count",
}


def canonicalize_measure_key(raw_key: str, text: str | None = None) -> str:
    """Map a raw extractor measure label to a stable canonical key. Deterministic and idempotent;
    `text` is accepted for future context-sensitive rules but unused today (small alias table only)."""
    key = (raw_key or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in _MEASURE_ALIASES:
        return _MEASURE_ALIASES[key]
    if key.endswith("_number"):
        key = key[: -len("_number")] + "_count"
    return key


#: "bought N <plural-noun> for $M [total]" — the count-vs-spend compound. A COUNT (N items acquired)
#: and a SUM (M spent) ride in one clause; the stochastic extractor usually emits only the spend. This
#: deterministic detector does NOT extract or write anything — it only lets the boundary NOTICE the
#: compound so a partial co-extraction (one side committed, the other not) is recorded as a legible
#: fail-closed receipt instead of a silent miss. N>=2 (a count of 1 is floored and isn't a count-case).
_COUNT_SPEND_RE = re.compile(
    r"\b(?:bought|got|purchased|acquired|picked\s+up)\s+(\d+)\s+"
    r"([a-z][a-z\-]+?)\s+for\s+\$?\s*(\d+(?:\.\d+)?)",
    re.I,
)


def count_spend_compound(text: str) -> tuple[str, int, float] | None:
    """Detect a 'bought N <plural-noun> for $M [total]' clause. Returns (singular_noun, count, spend)
    with count>=2, else None. Pure and deterministic; used ONLY to detect partial count/spend
    co-extraction for an observability receipt — it never emits an Event or writes a View."""
    m = _COUNT_SPEND_RE.search(text or "")
    if not m:
        return None
    try:
        count_n = int(m.group(1))
        spend = float(m.group(3))
    except (TypeError, ValueError):
        return None
    if count_n < 2:
        return None
    return (_singularize_token(m.group(2).strip().lower()), count_n, spend)


def _event_signature(e: Event) -> tuple:
    """Provenance signature for union-dedup when merging colliding groups (see `_merge_groups`)."""
    return (
        e.episode_uuid,
        str(e.when)[:10],
        None if e.value is None else round(float(e.value), 4),
        (e.identity or e.what or "").strip().lower(),
        e.kind,
    )


def _merge_groups(groups: list[PerceivedGroup], *, subject: str, measure: str) -> PerceivedGroup:
    """Merge groups WITHIN ONE sample that canonicalized to the same (subject, measure). Events are
    unioned and deduplicated by provenance so an overlapping sub-measure (e.g. `cycling_parts_spend`
    whose events are a subset of the category `cycling_spend`) does not double-count. The latest
    stated total wins; the reducer is the most common among the merged groups (re-inferred from the
    event kinds if a merged group that carried events was tagged 'stated')."""
    seen: set = set()
    events: list[Event] = []
    for g in groups:
        for e in g.events:
            sig = _event_signature(e)
            if sig in seen:
                continue
            seen.add(sig)
            events.append(e)
    stated_events = [g.stated_event for g in groups if g.stated_event is not None]
    stated_event = latest(stated_events) if stated_events else None
    reducer = Counter(g.reducer for g in groups).most_common(1)[0][0]
    if events and reducer == "stated":
        reducer = _infer_reducer([e.kind for e in events])
    return PerceivedGroup(
        subject=subject, measure=measure, reducer=reducer, events=events,
        stated_total=stated_event.value if stated_event is not None else None,
        stated_event=stated_event,
    )


def canonicalize_samples(
    samples: list[list[PerceivedGroup]],
) -> tuple[list[list[PerceivedGroup]], dict[str, str]]:
    """Rewrite every group's measure to its canonical key and merge within-sample collisions.
    Returns (canonical_samples, raw_measure -> canonical_measure) — the mapping feeds the debug
    report's raw->canonical collapse table."""
    raw_to_canon: dict[str, str] = {}
    out: list[list[PerceivedGroup]] = []
    for sample in samples:
        buckets: dict[tuple[str, str], list[PerceivedGroup]] = defaultdict(list)
        for g in sample:
            canon = canonicalize_measure_key(g.measure)
            raw_to_canon[g.measure] = canon
            buckets[(g.subject, canon)].append(g)
        merged: list[PerceivedGroup] = []
        for (subject, canon), grps in buckets.items():
            if len(grps) == 1:
                merged.append(grps[0] if grps[0].measure == canon else replace(grps[0], measure=canon))
            else:
                merged.append(_merge_groups(grps, subject=subject, measure=canon))
        out.append(merged)
    return out, raw_to_canon


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
    obj = _parse_json_object(llm_complete(STATED_TOTAL_PROMPT.format(measure=measure), log))
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
        if _parse_json_object(judge(prompt, "")).get("correct") is True
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
        if _parse_json_object(judge(prompt, "")).get("correct") is True
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


def _singularize_token(token: str) -> str:
    """Crude, deterministic de-pluralization for FAMILY MATCHING only (not display English). A plain
    trailing-'s' strip is enough to unify the observed scatter (`bikes`->`bike`, `movies`->`movie`,
    `playlists`->`playlist`, `tanks`->`tank`); '-ss' words and short tokens are left alone. Exact
    English correctness is unnecessary — only that variants of one concept map to one signature."""
    t = token.strip().lower()
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


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


# ---------------------------------------------------------------------------- (3) perceive -> fold


@dataclass
class PerceptionResult:
    committed: list[dict[str, Any]] = field(default_factory=list)
    abstained: list[GateDecision] = field(default_factory=list)
    decisions: list[GateDecision] = field(default_factory=list)
    #: raw extractor measure label -> canonical key, for the Phase 3 debug report's collapse table.
    raw_to_canonical: dict[str, str] = field(default_factory=dict)


def _emit_run_tally(
    run_tally: Any | None, graph_adapter: Any, *,
    subject: str, counter: str, value: float, namespace: str, source: str,
) -> None:
    """Record a run-level instrumentation tally.

    Prefers the :Metric saga (``run_tally.record_run_tally``) so the tally stays OUT of the
    semantic-recall layer. Falls back to the legacy :Entity ``record_counter`` only when no
    recorder is wired (non-scheduler callers / older tests). Never raises -- a failed diagnostic
    tally must not break perception.
    """
    try:
        if run_tally is not None:
            run_tally.record_run_tally(
                subject=subject, counter=counter, value=float(value), namespace=namespace
            )
        else:
            graph_adapter.record_counter(
                subject=subject, counter=counter, value=float(value),
                namespace=namespace, valid_at=None, source=source, name_embedding=None,
            )
    except Exception:
        logger.warning("failed to record run tally %s", counter, exc_info=True)


def perceive_and_fold(
    *,
    episodes: list[Episode],
    llm_complete: LlmComplete,
    graph_adapter: _GraphAdapter,
    k: int = 5,
    threshold: float = 1.0,
    namespace: str = "agent-experience",
    source: str = "perception",
    embed: Embed | None = None,
    dedup_threshold: float = 0.92,
    triangulation_tol: float = 0.0,
    record_abstentions: bool = False,
    run_tally: Any | None = None,
    cross_check: Callable[[str], float | None] | None = None,
    enable_cross_check: bool = False,
    coref_judge: LlmComplete | None = None,
    enable_coref: bool = False,
    coref_k: int = 3,
    verifier: Callable[[str, float, list[Event]], bool] | None = None,
    enable_verify: bool = False,
    verify_k: int = 3,
    verify_retries: int = 0,
    enable_stated_span_guard: bool = False,
    enable_sum_grounding: bool = False,
) -> PerceptionResult:
    """End-to-end perception boundary: extract k times -> gate -> commit XOR abstain.

    Committed groups fold to a counter View via `event_fold.fold_events_to_counter` (the deterministic
    sink). Abstained groups are a NO-OP by design — the raw episodes already carry the fallback, so
    absence of a View is the fallback (zero code). Optionally records one `perception_abstained`
    counter (the count of abstained measures this run) so the write-rate is itself a recallable fact
    (handoff sec 5). `k` samples require temp>0 in the injected `llm_complete` to be meaningful."""
    from menhir.services.event_fold import fold_events_to_counter

    # Lever B cross-check: an explicit `cross_check` wins; otherwise `enable_cross_check` builds the
    # default holistic derivation over the SAME episodes/LLM. `gate` calls it once per (subject,
    # measure), so k=1 for the cross-check falls out (per the plan's cost guard — holistic totals are
    # stable). Fully opt-in: neither set -> no veto-4, precision identical to before.
    if cross_check is None and enable_cross_check:
        cross_check = lambda measure: extract_stated_total(episodes, measure, llm_complete)  # noqa: E731

    samples = [extract_once(episodes, llm_complete) for _ in range(max(1, k))]

    # Lever C3 event coreference: collapse a purchase re-narrated across dates (which exact dedup
    # can't catch — different inferred dates). Applied per sample's sum/count groups BEFORE the gate;
    # a shared `memo` judges each (item, value) cluster once across all samples (cost guard). Opt-in:
    # explicit `coref_judge` wins, else `enable_coref` reuses `llm_complete`. Off -> behaviour unchanged.
    if coref_judge is None and enable_coref:
        coref_judge = llm_complete
    # The tri-state memo is the gate's window into coreference resolution: `None` when coref is off
    # (so the gate treats every ambiguous cluster as unresolved and vetoes — see veto 2b), a populated
    # dict when it ran (merge/separate = resolved, unsure = still vetoes).
    coref_memo: "dict[tuple[str, str], str] | None" = None
    if coref_judge is not None:
        coref_memo = {}
        for sample in samples:
            for g in sample:
                if g.reducer in ("sum", "count") and g.events:
                    g.events = resolve_coreference(g.events, coref_judge, k=coref_k, memo=coref_memo)

    # Lever C4 final verification: a focused audit of each candidate against its linked items, as the
    # last commit gate. Explicit `verifier` wins, else `enable_verify` builds the default over
    # `llm_complete`. Sharper than the blind cross-check (reviews the evidence); use one or both.
    # The default verifier supports the optional `anchor` kwarg for Law-3 candidates.
    if verifier is None and enable_verify:
        # detailed form returns (ok, votes, k) so a fail-closed SUM carries how-close it was into the
        # receipt (verifier receipt clarity); the gate accepts either a bool or this tuple.
        verifier = lambda measure, value, events, anchor=None: verify_candidate_detailed(  # noqa: E731
            measure, value, events, llm_complete, k=verify_k, anchor=anchor)

    # Measure-key canonicalization (pre-gate): collapse the same measure emitted under different
    # names across samples so the consistency gate votes on a stable canonical key, not raw
    # extractor labels. Identity map for measures that aren't in the alias table, so it is a no-op
    # for anything already stably keyed.
    samples, raw_to_canonical = canonicalize_samples(samples)

    decisions = gate(
        samples, threshold=threshold, embed=embed,
        dedup_threshold=dedup_threshold, triangulation_tol=triangulation_tol,
        cross_check=cross_check, verifier=verifier, coref_resolved=coref_memo,
        # episodes are needed by the stated-span guard AND the deterministic SUM-grounding path.
        episodes=episodes if (enable_stated_span_guard or enable_sum_grounding) else None,
        verify_retries=verify_retries,
        enable_sum_grounding=enable_sum_grounding,
    )

    result = PerceptionResult(decisions=decisions)
    result.raw_to_canonical = raw_to_canonical
    for d in decisions:
        if not d.committed:
            result.abstained.append(d)
            logger.info("perception abstained on (%s, %s): %s", d.subject, d.measure, d.reason)
            continue
        audit = {
            "view_audit_gate": "perception", "view_audit_agreement": round(d.agreement, 3),
            "view_audit_k": d.k, "view_audit_reason": d.reason,
            # corroboration verdict only — a deterministic bool. Lever-B veto-4 sets triangulated=True
            # when the holistic cross-check agrees, so the receipt records THAT it was corroborated
            # without persisting the model-stated total itself (invariant: model totals are gate inputs,
            # NEVER stored on a View). The raw cross_total stays in-memory on GateDecision for logs.
            "view_audit_triangulated": d.triangulated,  # None -> dropped by record()
            # deterministic-arithmetic corroboration used (SUM grounded from source spans, holistic
            # cross-check skipped). Only stamped when true, so existing Views are unchanged.
            **({"view_audit_sum_grounded": True} if d.sum_grounded else {}),
        }
        # a move-1 'stated' commit folds its single assertion event under SUM (sum of one = the
        # stated value); the deterministic sink only knows the scalar reducers.
        sink_reducer = "sum" if d.reducer == "stated" else d.reducer
        row = fold_events_to_counter(
            graph_adapter=graph_adapter, subject=d.subject, measure=d.measure,
            events=d.events, reducer=sink_reducer, namespace=namespace, source=source, embed=embed,
            audit=audit,
        )
        row.update({"agreement": d.agreement, "triangulated": d.triangulated})
        result.committed.append(row)

    # count-vs-spend partial co-extraction receipt (observability only — never changes what commits).
    # A 'bought N <noun> for $M' clause carries BOTH a COUNT and a SUM; the stochastic extractor
    # usually lands only the spend. When we detect the compound but did NOT commit both a count View
    # (==N) and a spend View (==M) for that noun, record a legible fail-closed receipt so the miss is
    # observable rather than silent (DECISION 1 = safety-only; co-extraction itself stays the
    # extractor's job and count-vs-spend stays a characterization case, not a gate).
    if record_abstentions:
        committed_decisions = [d for d in decisions if d.committed]

        def _committed(noun: str, target: float, reducers: tuple[str, ...]) -> bool:
            return any(
                d.reducer in reducers and d.value is not None
                and abs(float(d.value) - target) < 0.5 and noun in _measure_noun_sig(d.measure)
                for d in committed_decisions
            )

        partial = 0
        for ep in episodes:
            comp = count_spend_compound(ep.content)
            if comp is None:
                continue
            noun, cnt, spend = comp
            has_count = _committed(noun, float(cnt), ("count", "distinct_count", "stated"))
            has_spend = _committed(noun, float(spend), ("sum",))
            if not (has_count and has_spend):
                partial += 1
        if partial:
            _emit_run_tally(
                run_tally, graph_adapter, subject="perception",
                counter="count_vs_spend_partial", value=float(partial),
                namespace=namespace, source=source,
            )

    if record_abstentions and result.abstained:
        # bucket by firing veto, not one flat tally: the recall-recovery work needs to know WHICH
        # guard abstains most (and whether it is a recoverable class). Receipts stay out of semantic
        # recall (name_embedding=None), like perception_abstained always has.
        by_veto: dict[str, int] = defaultdict(int)
        for d in result.abstained:
            by_veto[d.veto] += 1
        for veto_label, n in {"perception_abstained": len(result.abstained), **{
                f"perception_abstained_{v}": c for v, c in by_veto.items()}}.items():
            _emit_run_tally(
                run_tally, graph_adapter, subject="perception", counter=veto_label,
                value=float(n), namespace=namespace, source=source,
            )
    _audit.audit(
        "counter", "fold", namespace=namespace,
        details={"episodes": len(episodes), "k": k,
                 "committed": len(getattr(result, "committed", []) or []),
                 "abstained": len(getattr(result, "abstained", []) or [])},
    )
    return result
