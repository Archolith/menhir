# Plan: Phase 3 measure-key canonicalization + guards + debug report

**Status: IN PROGRESS 2026-07-07.** Narrow, unblocked slice of the "Fix Phase 3" handoff.
Deliberately does NOT build conversational user-turn capture, and does NOT adopt global
STATEMENT-only semantics. Verified precursor: `HANDOFF-2026-07-07.md` + this session's finding
that (a) 0/1718 live episodes carry `user:`/role metadata, and (b) prod `gpt-4.1-nano` k=3
abstains on a clean bike-spend case purely from measure-key scatter.

## Corrected boundary (locked by user)
```
Perception may emit typed base events grounded in spans.
Folds emit aggregate/current derived facts.
Perception should not directly emit aggregate/current facts unless the source states the total.
```
So the bike-spend derivation (purchase events -> fold_algebra SUM = 125) STAYS VALID. Do not
reject a value just because the final SUM was derived.

## Scope (this patch only)
1. **Measure-key canonicalization** before the consistency gate groups by `(subject, measure)`.
2. **Stated-value span guard** — a STATED_MEASURE (`reducer="stated"`) whose numeric value is not
   present in a linked source span is quarantined. Fold-derived sums/counts are EXEMPT.
3. **Phase 3 debug report** — makes scatter->collapse and the missing capture metadata obvious.
4. **Tests** proving fold-algebra derivation still works and the guards behave.

## Key decisions
- **Alias table seeded ONLY from observed scatter** (this session's `cycling_*` spend scatter +
  the handoff's watch-list example). The handoff's illustrative aliases (`bikes`, `playlists`,
  `page_count`, ...) are NOT adopted verbatim: existing tests assert on `bike_spend`, `playlists`,
  `bikes`, `tanks`, `spend`, `fish_tanks_owned`, and aliasing those would break them.
- **Canonical keys are snake_case** (`cycling_spend`, `watchlist_item_count`), consistent with
  existing measures, not the handoff's dotted `watchlist.item_count` (avoids introducing the first
  dotted measure names into View/recall). Collapse behavior is identical.
- **Within-sample collision** (two raw measures in one sample mapping to one canonical, e.g. a
  category `cycling_spend` total plus a `cycling_parts_spend` subset) merges by UNION + provenance
  dedup, so overlapping sub-measures do not double-count.
- **Stated-span guard is opt-in** (`enable_stated_span_guard`, default False) — mirrors
  cross_check/coref/verify. `consolidate_personal_memory` pins it ON. Existing tests that don't
  pass it keep current behavior.
- Canonicalization is **always-on** in `perceive_and_fold` but is an identity map for every
  measure existing tests use, so it is a no-op for them.

## Future ADR (NOT this patch)
`Conversation Turn Capture Surface` — how Menhir captures role=user|assistant|tool, speaker/
declarant, source_kind, raw turn span, message/session id, recorded_at, asserted_at/anchor. This
is the real upstream fix for "Phase 3 sees no user turns"; deferred by decision.

## Done criteria
Phase 3 no longer rejects honest facts due to measure-key scatter, does not admit obvious
unsupported stated aggregates, and the debug report clearly reports that user-turn capture is
absent upstream. Not "Phase 3 solved."
