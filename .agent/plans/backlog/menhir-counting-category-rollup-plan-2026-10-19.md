---
artifact_schema: 1
artifact_uuid: 71a4e13c-4244-4d20-a164-f905132a9a48
artifact_type: plan
artifact_status: DEFERRED
---

# Menhir: category roll-ups over counted Views ("meta-views")

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Current state (shelved 2026-10-19; nothing implemented; blocked on the parent counted-View selection plan, whose PRs #320 and #321 are open drafts; prototypes v1 and v2 failed their gate; restart when PR A + PR B are measured and category questions remain the main miss; no separate issue, #370 tracks the counting remainder) (was: SHELVED)
Last verified: 2026-10-21 - NOT STARTED (high); Menhir main tarball scan has 0 matches for CountingFamilyCategories, category_rollup_enabled, categorize_families, categorizer_version, missing_family_categories and rollup_pending; control symbol personal_memory_counting_enabled found (30); no roll-up PR or issue; prototype results are the plan's own paid runs, not re-checked
Original note: SHELVED 2026-10-19 (owner: "shelve ... move on"). Owner asked for meta-views over categories
such as furniture and clothes; prototypes v1 and v2 failed the gate (labels wrong and unstable), and the
parent plan's PRs are shelved too. Nothing implemented.
Depends on: menhir-counted-view-selection-and-extract-rules-plan-2026-10-19.md (PR A #320; PR B
counted-View embeddings + view-level selection, which this plan reuses for recall).
Out of scope: distinct-item identity across families (owner decision 2, later); grand totals across
categories; time windows ("past month") on counted Views.

## Why (ms30, counted-4r snapshot, view-selection.log)

Counted Views are one per narrow family (match mode: family = normalized count_object, merged only
within the decide step's related set). Category questions need a sum across families:

| question | member Views today | category sum | gold |
|---|---|---|---|
| clothing to pick up/return | shirt 1, boot 1, jean 1 | 3 | 3 |
| luxury spend | gown 800, handbag 1200, leather boot 500 (not graphic tee 20) | 2500 | 2500 |
| furniture | bookshelf, coffee table, mattress (+kitchen table after PR A) | 3 (4) | 4 |
| baking | cookie, cake, baguette, bread recipe (not stand mixer, starter) | 4 | 4 |
| art events | exhibition, lecture, museum event (only 1 View today) | - | 4 |

The luxury question already had all three member Views in recall and was still answered wrong: one
total beats three fragments. Ranking Views by query similarity alone cannot separate members from
neighbours (baking: bread .32 vs mixer .24 vs starter .22), so membership is a write-time label.

The old perception counter lane (`perceive_and_fold`, off in bench and prod default) has a per-event
free-text category and `_category_spend_groups`, but stores no category and is not keyed to counting
families. Reuse the idea, not the data.

## Safety property

A roll-up verdict is shown only when it equals the sum of the subject's CURRENT member family Views
under a category map that covers EVERY current counted View of that subject; otherwise no roll-up
verdict (members still recall as today). A roll-up never feeds another roll-up.

## Design

### 1. Category map (counting lane, LLM, fenced)

- Durable node `:CountingFamilyCategories {namespace, subject_uuid, attribute, value_kind, unit,
  family_text_hash, categorizer_version, categories: [1..3 lowercase labels], updated_at}` keyed by
  (namespace, subject_uuid, attribute, value_kind, unit). Not a View property; not on the watermark.
- Step `categorize_families` runs in `run_counting_consolidation` after `_decide_and_rebuild` (page
  loop and leftover pass). Input per family: attribute name, up to 20 items, actions; plus the
  namespace's existing labels. Revised after the prototype: exactly ONE primary label (the most
  general everyday word a person would ask with, e.g. 'clothing' not 'footwear', 'baked goods' not
  'bread'), plus at most ONE secondary label only for a cross-cutting property such as price tier
  (e.g. 'luxury'); reuse existing labels; never the family itself.
  One call per batch of families (not per family), k=1, temperature 0, JSON out; labels normalized
  (lowercase, singular via family_tokens).
- Budget: guarded like decide (skip when call_budget cannot cover it; skipped families stay missing).
  Fence: `ctx.fence()` before the write, same as decisions.
- Re-run: a family whose `family_text_hash` (attribute + sorted items + actions) or
  `categorizer_version` differs is missing. Own sweep `missing_family_categories(namespace)` so a
  namespace with no new turns still gets labelled (mirrors `pending_decision_namespaces`).
  Categorizer version is its own setting, NOT a perceiver-version suffix: a relabel must not re-read
  and re-extract history.

### 2. Roll-up derivation (rebuild choke point, no LLM)

- At the end of `ScalarStateService.rebuild_scalar_state`, after the reconcile/retire loop, when the
  flag is on and counting is on: read the subject's current counted Views and their category map rows.
- Coverage gate: if any current counted View lacks a current map row (missing hash/version), write no
  roll-ups for that subject and retire existing ones (reason `rollup_pending`).
- For each (category, value_kind, unit) with >= 2 member Views: value = sum of member values;
  count_uncertain = sum; members = [(view_uuid, attribute, value)] sorted; items = union of member
  items (display only, capped 20); a roll-up of "number of distinct items" is not claimed.
- Identity: separate basis `rollup` and its own slot key (subject_uuid, `rollup:<category>`,
  value_kind, unit) so it cannot collide with a family named like the category, and the counted
  reconcile loop must EXCLUDE basis=rollup from its "not in desired_slots -> retire" listing (today
  that loop would retire any extra scalar View). Roll-ups own their own reconcile: desired roll-up
  slots vs current ones, retire the rest.
- Deterministic and idempotent: every rebuild path (counting lane, scalar lane, merge/unmerge,
  delete/orphan repair, flag sweep) recomputes it, so a changed or retired member re-derives it.
- Embedding: reuses PR B's view_embed on text "<category>: <member families>; <items>".

### 3. Recall

- PR B's `search_counted_view_embeddings` also returns basis=rollup Views (same eligibility/version
  checks).
- Selection (revised after the prototype): at most ONE roll-up per recall, and only when its cosine
  beats every member View's cosine (the question is about the category, not one family).
- Hide sub-views (owner 2026-10-19): when a roll-up verdict is shown, its member Views are not shown
  as separate verdicts and take no cap slot; under recall-authority-only their occurrence
  observations leave recall too (same rule as counted Views today). The members appear only as the
  breakdown inside the roll-up verdict.
- Rendering (context_builder): `[Scalar authority: ...] <category> total = N (sum of: shirt 1, boot 1,
  jean 1)`; lower-bound wording and count_uncertain carried as for counted Views; drill-down line
  points at the roll-up's uuid.
- Drill-down (owner 2026-10-19): `get_scalar_view_items(view_uuid=<roll-up>)` returns the members
  (view_uuid, attribute, value, count_uncertain) each with its items/contributors, same paging as
  today; a member's own uuid still drills into that member alone. Tests: roll-up uuid lists every
  current member; retired member absent; non-current roll-up uuid behaves like a non-current View
  today.

### 4. Flag and backfill

- `personal_memory_counting_category_rollup_enabled` (env MENHIR_PERSONAL_MEMORY_COUNTING_CATEGORY_
  ROLLUP_ENABLED), default off; requires counting, scalar view authority and view selection (PR B).
  Setting `personal_memory_counting_categorizer_version` (default "v1").
- CountingFlagState gains `rollup` (compared like `match`/`view_selection`): a flip sweeps counted
  subjects; off -> rebuild retires all roll-ups; no LLM call, no map write, no roll-up View with off.

## Hazards and how each is handled

- Double count: a row has exactly one count_family, so member Views are disjoint occurrence sets;
  roll-ups never sum roll-ups. Residual: the same physical thing under two unrelated families (e.g.
  "dining table" and "kitchen table" not related by decide) is counted twice. Accepted; measured in
  the prototype.
- Per-family, not per-item, labels: a family mixing a luxury and a cheap handbag is all-or-nothing.
  Accepted for v1.
- Pending members: undecided rows fold as uncertain already; the roll-up carries summed
  count_uncertain. Missing category rows block the subject's roll-ups entirely (coverage gate).
- Stale roll-ups: recomputed at the single rebuild choke point; members retired -> roll-up re-derived
  or retired in the same call.
- Label drift ("clothes" vs "clothing"): existing labels are passed in; selection is by embedding, so
  query wording need not match the label exactly. Measured in the prototype.

## Prototype result (2026-10-19, ms30 counted-4r, gpt-6-luna Flex, ~$0.0012)

Gate: FAIL.
- Category sums from a rank-1 roll-up: clothing 3 (pass), luxury 2500 (pass, tee excluded), furniture
  3 (pass), baking 3 of 4 (fail: bread recipe labelled 'bread'/'recipes', not 'baked goods'). Also
  plants gardening=3 (gold 3) and model kits hobby=5 (gold 5).
- Harm: multi-label families yield overlapping roll-ups that double count and crowd the 3 slots
  (cuisines d23cf73b loses recipe and fermented-food Views; projects 6d550036 gets business=3 beside
  the right View; citrus gets 3 roll-ups). Label drift splits categories (baked goods / desserts /
  bread; medical / medication / medical condition; clothing / footwear / accessories).
- Read-side rule checked by hand from both logs (fitted on these 30, unvalidated): select at most ONE
  roll-up, and only when its cosine beats every member View's cosine. Keeps clothing, luxury,
  furniture, baking; removes every roll-up on the RIGHT questions above; loses model kits (hobby .53
  < model kit .58); plants is a tie (.38).
- Proposed revision before any implementation: that selection rule, plus a labelling prompt that
  gives one primary label (most general everyday word a person would ask with) plus at most one
  secondary, reusing labels; rerun the prototype (paid, ask first) on ms30 and a fresh slice to
  check the rule is not overfit.

## Prototype v2 result (2026-10-19, two labelling runs, ~$0.0037)

Revised labels (one primary + optional price-tier secondary) and selection (one roll-up, must beat
every member). Gate: FAIL.
- Stable passes: clothing 3, luxury 2500 (both runs). Furniture passed run 1, failed run 2 (mattress
  labelled 'bedding'). Baking 3 of 4 both runs (bread recipe -> 'recipes'). Model kits and plants:
  roll-ups built but not eligible (just below the best member's cosine).
- Harm fixed: no chosen roll-up hid an answer-bearing View; double counting gone. Remaining RIGHT-
  question losses (case competition, kimchi) come from cosine ranking itself, not roll-ups.
- Unstable: primary labels changed in 13 of 20 namespaces between runs; the chosen roll-up changed in
  4. Chosen junk roll-ups only on already-wrong questions.
- Conclusion: per-family LLM labels decide membership wrongly (recipe vs baked good, mattress vs
  furniture) and inconsistently. Parked pending owner decision; candidate next steps: k-sample label
  voting over a fixed small taxonomy, or measure PR A + PR B first and revisit if category questions
  remain the main miss.

## Order and gates

0. PR B merged-ready first (this reuses its embedder and search).
1. Offline prototype (paid, Flex, ask first; est. < $0.01): on the counted-4r snapshot, label every
   ms30 counted family with the step-1 prompt (one batched call per namespace), derive roll-ups,
   embed, and run view_selection-style ranking with roll-ups added. Pass if: clothing 3, luxury 2500,
   baking 4 and furniture 3 come from a top-ranked roll-up, and no non-category question loses a
   correct verdict. Otherwise revise the prompt or drop.
2. Implementation PR (units 1-4, flag off = today). Tests: coverage gate blocks partial roll-ups;
   member change re-derives; family named like a category does not collide; counted reconcile loop
   leaves roll-ups alone; flag off writes nothing; flip sweeps; fence lost -> no map write; budget
   exhausted -> missing, no roll-up; roll-up member not double-capped in recall.
3. Paid ms30 A/B (ask first), then the wider slice.
4. Unit 7 (counter-lane retirement) can then count on category spend sums from the counting lane.
