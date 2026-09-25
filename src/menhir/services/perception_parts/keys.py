"""Measure-key canonicalization: sanitize model-authored labels at their origin, then merge collisions.

`sanitize_subject_name` / `sanitize_measure_key` constrain what a model-authored string can be
before it reaches any prompt site or durable View property; `canonicalize_measure_key` +
`canonicalize_samples` collapse the same measure emitted under different labels across the k
extraction samples so the gate votes on a stable canonical key.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import replace

from menhir.domain.fold_algebra import Event, latest
from menhir.services.perception_parts.model import PerceivedGroup, _infer_reducer

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


#: A subject is a NAME, not an identifier -- "my wife", "Alice's laptop", "the 2019 Corolla" are
#: all legitimate and none survives the `measure` rule. So this constrains SHAPE-OF-A-NAME rather
#: than shape-of-a-key: names are single-line, free of control characters, and short.
_SUBJECT_MAX_CHARS = 120


def sanitize_subject_name(raw: str) -> str:
    """Constrain a model-authored subject to something that can still be a name (CF-219).

    `subject` is read from the same parsed model output as `measure`, one line apart, and reaches
    the same durable destinations -- `ViewRepository.retrieval_text`, whose output is embedded as
    the View's retrieval surface, and the View's own `subject` property. CF-5's mechanism is
    therefore closed for one of the two strings composing that surface and was open for the other.

    **Deliberately NOT the `measure` rule.** Applying an identifier shape would drop real subjects
    or silently relabel another entity's data as "user", and both are worse than the gap. What is
    removed here is only what stops a value being a name:

    * control characters, INCLUDING NUL -- a NUL reaching a durable store is a data-integrity
      problem in its own right, independent of any prompt concern, and it survived before this;
    * newlines, which are what let a subject stop being a name and start being a new line of
      structure in whatever renders it;
    * unbounded length -- a 5000-character "name" is not a name, and it was being embedded and
      stored at full size.

    **What this does not claim.** Downstream containment already holds for markdown structure:
    an injected `### System` in a subject lands INSIDE the hook's context fence and is inert as
    markup (verified). This is not a second fence; it is the origin constraint that keeps
    obviously-not-a-name values out of durable storage in the first place, so containment is not
    the only thing standing between a hostile subject and the recall surface.

    Returns "" only for input that is empty after cleaning; the caller's existing `or "user"`
    default then applies, exactly as before.
    """
    # `isprintable()` is False for every control character INCLUDING newline and NUL, so one test
    # covers both concerns. Control characters become a SPACE rather than being deleted: deleting
    # them runs the surrounding words together ("wife\\n### System" -> "wife### system"), which
    # corrupts the very embedding surface this value exists to feed. `split()/join()` then
    # collapses the runs.
    cleaned = "".join(ch if (ch.isprintable() or ch == " ") else " " for ch in (raw or ""))
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _SUBJECT_MAX_CHARS:
        cleaned = cleaned[:_SUBJECT_MAX_CHARS].rstrip()
    return cleaned.lower()


#: A measure key is a snake_case identifier. `SYSTEM_PROMPT` already asks the extractor for
#: "stable snake_case", so this enforces the contract the prompt states rather than adding one.
_MEASURE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def sanitize_measure_key(raw_key: str) -> str:
    """Normalize a model-authored measure label, or return "" if it is not a measure key at all.

    This is the origin of the CF-4/CF-5/CF-69 chain and therefore the only place worth fixing it.
    `measure` comes straight out of parsed LLM output, and the episode author controls the text
    that model reads -- so `measure` is attacker-influenced free text. It used to receive only
    `.strip().lower()` before travelling to three destinations: the system prompt of the
    cross-check call, the system prompt of the verification call whose verdict gates commitment,
    and the View's durable `counter` property, which `ViewRepository.retrieval_text` embeds as
    the retrieval surface of later recall turns. Sanitizing at each destination would have left
    the next one to be added unguarded; sanitizing here means every consumer inherits it.

    Shape, not an allowlist. The vocabulary is deliberately open -- the extractor coins keys for
    quantities nobody has tracked before -- so a closed set would reject new measures. What an
    identifier shape does buy is structural: no newlines, quotes, colons, braces or sentence
    punctuation survive, which is what let injected text stop looking like a key and start
    looking like a new instruction or a JSON literal.

    Being exact about the residue: this bounds the injection, it does not eliminate it.
    `ignore_prior_instructions` is a well-formed snake_case identifier. That is why the prompt
    restructuring in `extract_stated_total` and `verify_candidate` matters as well -- shape
    limits what can be said, position limits where it is said from.
    """
    key = (raw_key or "").strip().lower().replace("-", "_").replace(" ", "_")
    return key if _MEASURE_KEY_RE.match(key) else ""


def canonicalize_measure_key(raw_key: str, text: str | None = None) -> str:
    """Map a raw extractor measure label to a stable canonical key. Deterministic and idempotent;
    `text` is accepted for future context-sensitive rules but unused today (small alias table only)."""
    key = (raw_key or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in _MEASURE_ALIASES:
        return _MEASURE_ALIASES[key]
    if key.endswith("_number"):
        key = key[: -len("_number")] + "_count"
    return key


def _singularize_token(token: str) -> str:
    """Crude, deterministic de-pluralization for FAMILY MATCHING only (not display English). A plain
    trailing-'s' strip is enough to unify the observed scatter (`bikes`->`bike`, `movies`->`movie`,
    `playlists`->`playlist`, `tanks`->`tank`); '-ss' words and short tokens are left alone. Exact
    English correctness is unnecessary — only that variants of one concept map to one signature."""
    t = token.strip().lower()
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


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
