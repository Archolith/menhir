---
artifact_schema: 1
artifact_uuid: 2f7fa387-aa53-4e22-aca5-22f5a10f25ae
artifact_type: plan
artifact_status: DEFERRED
---

# Menhir: counted-View selection in recall, and two counting-extraction rules

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Current state (shelved 2026-10-19 by owner; PRs #320 and #321 still open drafts, not merged, 0 matches for their symbols on main; tracked by the draft PRs, no separate issue) (was: SHELVED)
Last verified: 2026-10-21 - NOT MERGED (high); gh shows #320 and #321 OPEN and draft, last updated 2026-10-19; Menhir main tarball scan has 0 matches for the span-rule, captured-states and view-selection flags, search_counted_view_embeddings and count_view_embedding; control symbol personal_memory_counting_enabled found (30); A/B numbers and cost are the plan's own, not re-checked
Original note: SHELVED 2026-10-19 by owner ("shelve ... move on"). PR A #320 and PR B #321 implemented,
CI green, now drafts, not merged. Paid ms30 A/B (gpt-6-luna Flex, one run per arm, official/unofficial):
baseline 12/13, B only 14/14, A only 15/15, A+B 14/14. Only 6 of 30 questions changed between arms,
mostly count answers flipping, so read as noise; plants (2 to 3 with A) is the one consistent change.
Cost ~$0.09.
Restart condition: repeated runs or a larger slice showing a real effect.
Parent: menhir-scalar-counting-plan-2026-10-15.md (deferred "View embeddings: only after unit R").
Out of scope: meta-views (combining Views by item or across families); separate plan later.

## Evidence (ms30, gpt-6-luna, counted-4r snapshot, 2026-10-18/19)

- Plants (3a704032): the extractor found the succulent in 9/9 replay samples, but the k=3 gate split
  votes because quotes varied ("peace lily", "which I got from...", "got ... along with a
  succulent"). Current prompt: all items committed in 1/3 gate trials; with a short-quote/companion
  rule: 3/3.
- Furniture (gpt4_15e38248): (a) the kitchen-table fix was never extracted (0/3 real samples, 18/18
  in replay without the captured block); the captured block listed `repair_status = "fixed"` for that
  clause and the prompt forbids emitting for listed clauses. (b) `mattress_count` was a current
  counted View but got no verdict: recall picks counted Views only through the top-10
  TypedAssertions by cosine, and the mattress occurrence ranked 12th behind descriptive facts.
- Ranking current counted Views directly by cosine(question, View text) recovered the mattress and
  changed picks on doctors, baking, citrus; counted Views per namespace on ms30: 0-7 (median 2).

## PR A: two counting-extraction flags (versioned, prompt-only)

Mirror `personal_memory_counting_extract_v2` plumbing (settings field + env, feature_flags FEATURES
/ _REQUIRES (counting) / _REQUIREMENT_SCOPE / _VERSIONED, runtime -> scheduler -> scheduler_tasks ->
CountingConsolidationConfig). Each flag adds a perceiver-version suffix so flipping it re-reads
history (3a.2); with both off, prompt, version and context are byte-identical to today. Order:
configured, +x2, +s, +c, then +m LAST, because status_of and count_decision_repository._counted
detect a match-stamped row by the version ending in "+m" (a suffix after it would make undecided
match rows read as counted).

A1. `personal_memory_counting_span_rule_enabled` (MENHIR_PERSONAL_MEMORY_COUNTING_SPAN_RULE_ENABLED),
    suffix "+s". Prompt addendum: quote only the verb phrase that states each thing's own happening,
    without leading "which", "my", "I", "and"; when a sentence adds further things to one happening
    ("along with", "together with", "as well as", "plus", "and also"), emit one observation per
    thing, each quoting only its own words ("got A", "along with B"). Generic examples only.
A2. `personal_memory_counting_captured_states_enabled`
    (MENHIR_PERSONAL_MEMORY_COUNTING_CAPTURED_STATES_ENABLED), suffix "+c".
    `scalar_claims_for_turns` also returns `a.value_kind` (additive). Under the flag the captured line
    gains `kind=<value_kind>` and the prompt narrows the rule: only a captured count or money fact
    (kind=count or kind=money: a stated total or change to a total) blocks a happening in that
    clause; a captured state or property (status, boolean, measurement, ...) does not, e.g. a
    captured "repair status = fixed" still lets the fixing be counted.

Tests (new file): flag off -> prompt/version/context unchanged; each flag adds its addendum and
suffix; suffix order with x2/m; kind rendered only under A2; claims query returns value_kind.

## PR B: counted-View selection (one flag, write + read)

`personal_memory_counting_view_selection_enabled`
(MENHIR_PERSONAL_MEMORY_COUNTING_VIEW_SELECTION_ENABLED); requires counting and scalar view authority.

Write (choke point: ScalarStateService.rebuild_scalar_state; every counted View write goes through it):
- The service takes optional `view_embed` / `view_embed_version`, injected via the adapter factory
  `scalar_state_service(...)` (adapter fields set at bootstrap from make_view_embedder when the flag
  is on), so all rebuild callers get it.
- For basis=counted only: text = pure domain helper over (ss_attribute, count_items, count_actions);
  audit gains `count_view_text`, `count_view_embedding`, `count_view_embed_version`. Audit props are
  also refresh props, so unchanged Views are refreshed on rebuild. Embed failure writes all three as
  null (falls back to today's path) rather than leaving a stale vector. Per-service text->vector cache.
- NOT `name_embedding`: Graphiti entity search reads that property; Views must not enter general recall.
- Backfill: the CountingFlagState marker gains `view_selection`, compared like `match`, so a flag flip
  sweeps (rebuilds) counted subjects and embeds existing Views.

Read (recall_pipeline authority lane):
- New brute-force query `search_counted_view_embeddings(vec, namespace, limit=10)` (same shape as
  search_assertion_embeddings) over current counted scalar_state Views with an embedding; returns the
  slot tuple (view_subject_uuid, ss_attribute, ss_scope, ss_kind, ss_unit) and cosine.
- After the observation hit loop, under the flag, in its own try/except with a getattr guard: put
  View slots in a SEPARATE set and cosine dict; only the authority loop sees `obs_slots | view_slots`
  ordered by the merged cosine. The history lane's `_obs_slots_for_history` is untouched.
- View slots go through the same subject gate, min-similarity gate, fetch_current_scalar_view_for_slot,
  recall_eligible check and counted_verdict_limit cap. The new path can only add candidates.

Tests: mattress case (assertion hits miss it, View search adds it, within cap); flag off -> View
search never called, output identical; other-subject View gated for first-person query; View search
raising leaves assertion verdicts intact; history-lane slots unchanged; ineligible View not emitted;
write: counted View gets text/embedding/version, non-counted untouched, failure nulls, flag off no
keys; marker mismatch on view_selection triggers the sweep.

## Safety notes

- Off = today: no new Cypher, LLM or embed calls, byte-identical prompts and recall output.
- Query vectors (graphiti embed_query) and stored vectors (make_view_embedder) use the same model in
  bench and prod config; same assumption the assertion lane already makes. Version stored per View.
- Cost: one embed per changed counted View text per rebuild (cached); one extra Cypher scan per recall.

## Order and gates

1. PR A, PR B (separate branches). Focused tests per PR; full suite once at the end; CI green.
2. Paid A/B on ms30 (ask first): re-count from the -base snapshot with A1/A2, score with B on/off.
   Each flag stays off unless ON is equal or better than OFF.
3. Meta-views plan (combine Views by item and across families) after the A/B.
