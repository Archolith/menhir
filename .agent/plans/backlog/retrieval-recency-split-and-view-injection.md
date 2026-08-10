# Plan: recency split + A6 View injection + lens router

**Status: PLANNED 2026-07-03 (not started; DECISION-GATED — Parts 2–3 consume the reachability
data from `retrieval-reachability-receipts-and-bundle-honesty.md` Part 1; Part 1 requires a
measured A/B via the §6 promotion ladder).**
The experiment-gated slice of the 2026-07-03 retrieval design review. Design authority:
`.agent/memory-retrieval-under-uncertainty.md` §4b (self-reinforcing relevance), §4f (intent
mismatch), §6 (shadow → flag → measure → default), §7 (the A6 seam decision).

## Part 1 — split "world recency" from "access recency" (change 3)

### The defect
The recency lane scores `exp(-λ · days_since_last_accessed)` — and recall **touches**
`last_accessed` on every result it returns (`_post_recall_updates`). The lane therefore measures
"recently returned," not "recently happened," and re-arms to maximum on each return. The
edge-weight half of the reinforcement loop is capped at 5.0; this half has no equivalent bound.
The RECENT preset (β=0.5) half-weights a signal the ranker itself generates.

### The change (ladder-disciplined; this WILL change rankings, so it is measured, never argued)
1. **Flag**: `RetrievalTuningConfig.recency_basis: "access" | "world"` (default `"access"` ==
   today, byte-for-byte). `"world"` scores recency on `created_at` (fallback: `valid_at`; absent →
   recency 0, never invented). `last_accessed` remains untouched for lifecycle decay protection —
   that is what it is actually for.
2. **Measure**: A/B on the bench oracle slice, both presets that lean on recency (RECENT,
   EMOTIONAL) and KNOWLEDGE as control. Pre-registered decision rule: flip the default to
   `"world"` only if it is ≥ parity on the oracle slice overall AND strictly better on the
   staleness-sensitive questions; if worse, keep `"access"`, record the number at the flag site,
   and keep the arm for comparison (§6: failed arms stay, labeled).
3. Per-type `scoring_recency_lambda` (memory_types policy) is untouched — only the basis changes.

## Part 2 — A6: lens-gated View injection (addition 1)

### The decision gate (pre-registered, from reachability receipts)
Build this ONLY if the lens-bucketed reachability data shows Views under-surfacing on the queries
they exist to answer: median first-View rank > 3 OR `view_absent` share > 25% on aggregate-lens
recalls over a namespace where committed Views exist. If reachability is already effectively
rank-1, **do not build** — instead amend `memory-aggregation-under-uncertainty.md` §2 to state the
premise is delivered empirically, and close the seam disagreement that way. Either outcome ends
the docs' disagreement; that is the point.

### The mechanism (if built)
Template: fact-edge pointer hydration (`recall_service.py:867-922`) — the measured-good shape for
injecting a source.
1. `CandidateSource.VIEW`, floor-exempt, `SOURCE_PRIORS[VIEW] = 1.0` (top of the Part-1-normalized
   scale — a current View whose measure matches the query IS maximally relevant to it).
2. On aggregate/current-state lens queries only (router, Part 3): deterministic lookup of current
   Views in the query's namespace whose subject/measure tokens match query terms
   (`list_views`/`fetch_counter` path; lexical match first, no new embedding call), injected into
   the normal candidate pool — metadata, scope filters, scoring, and packing all apply unchanged.
3. Superseded versions never inject (`view_current` filter already enforced); receipts stay out
   of the surface (seam obligation).
4. Ladder: shadow first (log would-have-injected + would-be rank), then flag
   (`enable_view_injection`, default off), then the oracle-slice A/B, then default.

## Part 3 — lens → source router (addition 2)

`_query_wants_history` (`recall_service.py:69-77`) is one lens gating one source; Part 2 adds a
second; the belief gate is a natural third. Extract the dispatch into one deterministic table —
`lens -> {fact_edge_pointer, view_injection, belief_gate}` — in `domain/` beside the intent
classifier:
- historical → fact-edge pointer hydration (as today)
- aggregate / current-state → View injection (Part 2)
- current-state → belief/currentness gating priority
Each source keeps its tuning flag as the master switch; the router decides only *when* an enabled
source fires. Wrong-lens injection is a regression, not a no-op (§4f) — the router is where that
rule lives, once, instead of scattered per-source conditionals. Pure refactor for the existing
fact-edge gate (behavior-preserving, tested), additive for the rest.

## Explicitly NOT in scope (decided, not forgotten)
- Interval/provisional View rendering contracts — no such rungs exist yet (write-side §9 gates
  them on abstention telemetry).
- Preset weight retuning, hybrid_alpha, floor value — the scale-contract plan owns the floor;
  tuning follows it.
- Brief-builder default flip; edge-weight decay (backlogged in the gap plan).

## Verification
1. Part 1: A/B numbers recorded at the flag site; default path byte-identical with
   `recency_basis="access"`; world-basis unit tests (created_at present/absent/valid_at fallback).
2. Part 2: decision memo (build vs amend-doc) written into this plan with the reachability
   numbers BEFORE any code; if built — shadow logs, then flag-on A/B on the aggregate-lens slice;
   a committed View for an invented measure reaches rank ≤ 2 on its own query in the fixture test;
   flag-off remains byte-identical.
3. Part 3: fact-edge gating behavior-preserving under the router (existing lens tests
   unmodified and green); router unit tests per lens.
