# Anecdotal-Recall Oracle Ladder

Branch: `claude/menhir-chain-handoff-doc-7iuat2`

> **Status note 2026-07-11: LIVING / REFERENCE — keep, do NOT archive.** This is the anecdotal-recall
> oracle *measurement narrative* (rungs A-E, MSC sweep, ProvenanceClassifier, BriefBuilder v1/v2), not
> an actionable build plan. It is the load-bearing rationale for **why the frontier oracle stack ships
> default-off**: the stratified answer-accuracy matrix showed plain node-only (0.367) beats
> oracle/edge configs (0.322) on LongMemEval — the stack is net-neutral-to-negative on anecdotal
> recall. Do not archive: the "why" behind the default-off levers (`.agent/default-off-features.md`)
> lives here. Reconciliation notes:
> - **BriefBuilder is BUILT, default-off** (`domain/brief_builder.py`, `frontier_brief_builder`); the
>   append-mode retest is safe/neutral (+0.03, within noise) — shippable without harm, lift unproven.
>   Tracked in `.agent/default-off-features.md`.
> - **The backdating BLOCKER flagged mid-doc is RESOLVED.** `occurred_at`->`reference_time` now lands on
>   the HTTP ingest path (see archived `menhir-temporal-ingest-backdating-plan.md`); the old inline
>   `ingest_episode` body that could stamp `now()` was deleted — it delegates to the unified queue path.
>   The mid-doc "menhir code fix is a tracked follow-up" and the `ingest_service.py:825 now()` note are
>   now stale.
> - Open research threads (ablation-derived oracle-routing table; provenance-based support metric;
>   multi-fragment entity clustering) remain live and gated on measurement — this doc tracks them.

## Design law (the north star)

> **Oracles rank usefulness. Wardens prevent unsafe assertion.**
> **A superseded fact is unsafe for *current truth*, not unsafe for *historical recall*.**
> **superseded ≠ useless; it only means not-current.**
> **Fact edges are indexes INTO memory, not memories themselves** (rung B, measured):
> use an edge to *find* the right memory/episode/entity, then hydrate that node's
> surrounding context for the answer — never return the terse edge fact as the answer.

The frontier stack currently violates this: `TemporalOracle` emits `CONTRADICT`
(a *ranking* signal) which drives `OracleAdmissionWarden` to *refuse* (a *gating*
action). Ranking leaked into gating. This ladder fixes that leak as a **measured**
path, one attributable step at a time — not a pile of toggles landed at once.

## Root cause recap (proven this session)

On LongMemEval Mode-B (oracle variant), question *"What was the first issue I had
with my new car after its first service?"*:

- The answer is a dated `RELATES_TO` fact edge (`the dealership replaced the GPS
  system … 2023-03-22`). Ingest **belief-superseded** it (`expired_at`/`invalid_at`
  set when a later turn re-mentioned it).
- Menhir recall searched entity **nodes only**, so the fact-edge was never a
  candidate; the fact only survived as embedded node text at rank 10.
- Even once retrieved, the default **`current` lens** made `TemporalOracle` read
  the superseded fact as `HISTORICAL → CONTRADICT`; combined prob collapsed
  (0.0036 vs 0.058) and `OracleAdmissionWarden` **refused** it — while vague
  **undated** facts ("Yelp provides reviews") stayed neutral and outranked it.
  "Known time lost to unknown time."

## Already shipped (rung A baseline)

- `350d1ce` **fact-edge retrieval** (`MENHIR_FRONTIER_FACT_EDGES`): `RELATES_TO`
  facts injected into the candidate pool as `CandidateSource.FACT_EDGE`, carrying
  their bitemporal anchors.
- `bb62258` **personal-history lens**: `TaskIntent.RECALL_PERSONAL_HISTORY`
  (first-person + past-verb cues, gated so code queries stay `current`) → routes
  to the `historical` lens; superseded dated facts become `SUPPORT`.

Proven end-to-end: dated answer edge went combiner rank **18 → 1**, warden
**REFUSE → ADMIT**, HTTP recall rank **10 → 1**. 158 intent/oracle unit tests green.

## Component decisions (locked)

**New capability (build):**
1. `EdgeFactOracle` — ranks answer-bearing edges above generic nodes.
2. `SpecificityOracle` — suppresses generic/reference facts when a concrete
   remembered answer is wanted.
3. `HistoricalAdmissionWarden` — defense-in-depth: under historical/episodic lens,
   never refuse *solely* because `expired_at` exists.

**Existing — promote/clean up, do NOT duplicate:**
1. `RECALL_PERSONAL_HISTORY` classification → promote to an explicit
   `QueryIntentOracle` **stage** (currently inside `classify_intent`). Presentation
   refactor, not new capability.
2. Supersession current-vs-historical role → **stays inside `TemporalOracle`**
   (already the `superseded_answers_historical` vs `superseded_under_current_intent`
   branch). Do not split into a separate oracle — same signal, one voter.
3. Temporal *specificity* (reward dated grounding / penalize undated) → **folds
   into `TemporalOracle`** as an explicit output. Not a separate voter — a second
   oracle keying on `valid_at` double-counts currentness in the multiplicative
   LogSpace combiner.

## Guardrails (what keeps this from becoming bench-overfit)

- **Edge boost must be lens-conditioned.** `EdgeFactOracle` boosts edges only under
  episodic/historical intent — never unconditionally (a type-based boost would
  regress code/current queries where nodes are the right answer).
- **Reference/generic penalty must be intent-conditioned.** `SpecificityOracle`
  suppresses REFERENCE/world-knowledge only under a personal-recall intent. Build
  it on the existing `ContentRole` taxonomy + `INTENT_ROLE_MATRIX`, not a new
  semantic classifier (keep it deterministic).
- **Temporal grounding must not double-count currentness.** One temporal voter.
- **Wardens block assertion, not relevance.** No warden decides ranking.

## Experimental ladder (measure between every rung; N ≥ 30, attributable deltas)

| Rung | Change | Measures | Result |
|---|---|---|---|
| **A** | Current frontier + `bb62258` personal-history lens (shipped) | **Baseline** the rest must beat | **0.300** (9/30) node-only |
| **A′** | (probe) naive `fact_edges=ON` standalone (floor-exempt, uniform timestamp boost) | — | **0.033** (1/30) — REGRESSION |
| **B** | fact-edge `mode` A/B: standalone vs **pointer** (hydrate endpoint nodes) | edge-as-answer vs edge-as-index | off **0.300** / standalone **0.033** / pointer **0.267** — pointer ties baseline (±1 q noise) |
| **B-gate** | pointer **lens-gated** (fire only when query lens = historical) | no-regression on oracle; setup for `s` | *(running)* |
| **B-`s`** | off vs pointer-gated on the **`s` variant** (494-turn haystack) | the real lift test (diluted nodes) | *(pending — needs `s` graph ingest)* |
| **C** | + `SpecificityOracle` via `ContentRole` / REFERENCE suppression | Distractor suppression lift | — |
| **D** | + `TemporalOracle` grounding adjustment + `HistoricalAdmissionWarden` | Grounding reward + gate backstop | — |
| **E** | Pipeline cleanup: explicit `QueryIntentOracle` stage; retire proven-out toggles | Architecture, once shape is earned by data | — |

### Rung A finding (2026-07-02, N=30, oracle-ON, persistent graph)

**Baseline node-only = 0.300. Naive `fact_edges=ON` = 0.033 — a severe regression, NOT a bug.**
Recall ran on all 30 queries (`candidates≈70 results=10`); the fact-edge path worked. The
score collapsed because the 20 **floor-exempt, uniformly `timestamp`-evidence-boosted** edges
**crowd out the richer entity nodes** and win the 10 result slots with terse one-line facts —
memory context fell from ~21.5k to ~5.5k tokens. The earlier single-question "rank 10→1" proof
was a cherry-pick (a question whose answer *is* a dated edge); at N=30 the edges are terse noise
for the other 29 and evict the node summaries that were carrying those answers.

Implications (redirects rung B):
- Keep `fact_edges` **default OFF** (vindicated — regression can't reach prod).
- The defect is "edges as floor-exempt standalone candidates that evict richer nodes," not
  "retrieve edges." Rung B must AUGMENT, not REPLACE:
  - drop the floor-exemption + uniform `timestamp` evidence boost (let edges compete honestly);
  - far fewer edges, **lens-conditioned** (inject/boost only under episodic/historical intent);
  - **preferred:** use an edge hit to **boost its parent node's rank** instead of injecting a
    thin standalone candidate — preserves node richness (context) AND fixes ranking.
- Pair with rung C `SpecificityOracle` to keep generic edges ("Yelp provides reviews") down.

### Rung B finding (2026-07-02, N=30, oracle-ON): edge-as-answer vs edge-as-index

Three arms on the persistent graph: **off 0.300 (9/30) / standalone 0.033 (1/30) /
pointer 0.267 (8/30)**.
- **Standalone (edge-as-answer) is dead** — reproduced 0.033; terse floor-exempt facts
  crowd out rich nodes and collapse context (5.5k tok).
- **Pointer (edge-as-index) is the right shape** — rescued the feature (1/30→8/30) and kept
  context rich (26.4k tok). But 8/30 vs 9/30 is **one question = noise**: pointer *ties* the
  baseline, no demonstrated lift.
- **Why the tie:** on the `oracle` variant the entity NODES already summarize the facts (the
  `dealership` node literally held "replaced the entire GPS system … 2023-03-22"), so the edge
  channel is largely redundant. The mechanism needs a haystack where nodes are diluted.
- **Residual −1 q:** pointer injection was *unconditional*; on non-episodic queries an
  edge-endpoint node sometimes displaced a better node hit. Fixed by **lens-gating** the
  pointer (`_query_wants_history`, `fact_edge_mode="pointer"` only hydrates when the query
  resolves to the historical lens) — non-episodic queries revert to the exact node baseline.

### Promotion rule (fact-edge pointer)
Accept only if **all** hold:
1. `oracle` slice: pointer-gated **≥ node-only baseline**, no context-bloat explosion.
2. `s` variant (long haystack): **meaningful lift** over node-only (the environment the
   index mechanism is for — diluted nodes, hard-to-surface specific facts).
3. standalone edge-as-answer stays **rejected**.

`s`-variant note: needs a graph built from `s` haystacks (~494 turns/item) — a real ingest
job (hours), not a 6-min recall-only run. Scope separately before running.

Rule: **do not add five moving parts at once.** Each rung is one A/B (fact_edges +
its oracle on vs off) on the persistent LME graph, so any delta is attributable to
that rung. A multiplicative combiner with 5 new interacting weights landed together
is a tuning swamp and unattributable.

## Measurement harness (rung A and up)

- Persistent graph: container `menhir-lme-neo4j`, bolt `7689`, ns `lme-gpt4_*`
  (READY ≈ 10.9k episodic nodes). **Never reset it** — recall-only A/B.
- Serve each arm from menhir-frontier's own `.venv` (interpreter guard), env
  `MENHIR_BENCHMARK_MODE=1`, `LONGMEMEVAL_VARIANT=oracle`, OpenAI embed dim **1536**
  explicit, answer `gpt-4o`, judge `gpt-4o-mini`, scorer `llm-judge`.
- Reusable probes (job tmp, promote into `scripts/longmemeval/` per the framework
  consolidation plan): `oracle_packet_dump.py` (per-candidate oracle packet),
  `edge_recall_probe2.sh` (HTTP off/on), `edge_recall_probe3.sh` (oracle-off isolation).

## STRATEGY PIVOT (2026-07-02) — measure the pipeline, stop guessing the bottleneck

Two measurement artifacts collapsed the "we're losing to Graphiti/Zep on recall" premise:
- **Sampling bug:** `--limit 30` took the first 30 oracle items, which are **100%
  temporal-reasoning** (the file is grouped by type) — the hardest category, and one where
  ~half the golds are *computed* values ("7 days", "6:45 AM") stored in no memory. Every score
  this session (0.30 / 0.033 / 0.267 / 0.333) was that one hard slice, not a fair sample.
- **Retrieval is NOT behind graphiti.** Deterministic gold-presence harness (no answer-model
  spend), stratified N=90 across all 6 types: **menhir node-only 28/90 present@10 vs
  graphiti-native 11/90 — menhir ahead on every category.** (Caveats: graphiti-native `search()`
  is edge-facts-only, not full Zep; the token-subset presence metric undercounts *paraphrased*
  golds like single-session-preference and *computed* golds — use the semantic llm-judge for
  answer scoring, and it already handles paraphrase.)

Conclusion: **do not pause frontier work to chase graphiti-native retrieval — menhir is
already ahead there.** The bottleneck is downstream: **answer generation / context packing /
computed-answer reasoning**, plus not regressing on oracle ranking.

### The decisive experiment set (tells us which subsystem earns the next month)
Decompose per config × question-type, separating the stages:
`retrieval presence → support presence → context packing → answer accuracy → computed reasoning`.
- **Answer-accuracy matrix** (running): 3 menhir configs (node-only-plain / frontier-default /
  edge-pointer-gated) × 6 question types × 15, recall-only, gpt-4o + gpt-4o-mini semantic judge.
  Cross-tab vs the presence data: gold present but answer wrong ⇒ packing/generation; gold
  absent & computed ⇒ reasoning; gold absent & retrievable ⇒ candidate-gen.
- **Minimal-sufficient-context (MSC) — the "very Menhir" metric.** Not "did we retrieve the
  answer" but *"what is the smallest context window that still answers correctly?"* Sweep recall
  top-k ∈ {1,2,3,5,10}; plot accuracy vs context-tokens; MSC = smallest k at the accuracy
  plateau. If menhir's temporal/structural organization answers at 3–5k tokens where flat
  retrieval needs 20k+, that IS the product moat (latency, cost, model reliability). Enabling
  change: thread `recall_limit` from an env (`LME_RECALL_LIMIT`) in bench `run_memory_ab`
  (currently hardcoded default 10) — do AFTER the matrix run, not during.

Compare arm for both: graphiti-native (deprioritized — menhir already ahead on presence).

### Answer-accuracy matrix result (2026-07-02, stratified 6 types × 15, llm-judge)

| type | node-plain | frontier (oracle) | pointer (edge, gated) |
|---|---|---|---|
| temporal-reasoning | 0.533 | 0.400 | 0.400 |
| multi-session | **0.133** | 0.067 | 0.067 |
| knowledge-update | 0.267 | 0.267 | 0.200 |
| single-session-user | **0.600** | 0.600 | 0.533 |
| single-session-assistant | 0.400 | 0.400 | 0.400 |
| single-session-preference | 0.267 | 0.200 | 0.333 |
| **average** | **0.367** | 0.322 | 0.322 |

**Decisive findings:**
1. **Plain node-only (no oracle, no edges) is the BEST config (0.367).** Both frontier additions
   (oracle ranking, edge-pointer) are net neutral-to-negative. The frontier oracle stack is a
   **net regression** on LongMemEval today — it loses on every reasoning-heavy category
   (temporal −0.13, multi-session −0.07, preference −0.07) and helps nowhere. → **do not
   optimize oracles; consider defaulting them OFF** until they earn their place.
2. The "0.30 gap vs Zep" was a sampling artifact (temporal-only). Fair stratified base ≈ **0.37**,
   with huge per-category spread (**multi-session 0.13 → single-session-user 0.60**).
3. **multi-session (0.13) is the weakest** — the multi-hop "tiny planning problem" (retrieve A +
   B, reconcile identity, order, merge). Where Chronostratum/belief should *eventually* win.
4. Retrieval is not the bottleneck vs graphiti-native (menhir 28/90 gold-present@10 vs 11/90).

**Support-presence metric caveat:** the token-overlap version undercounts (enrichment paraphrases
raw evidence-turn wording, so raw-turn tokens don't survive into enriched memories). Rebuild
**provenance-based**: retrieved memory → source episode uuid → was it a `has_answer` turn. That is
the trustworthy retrieval-vs-reasoning splitter; the token proxy is not.

### MSC sweep result (node-plain, k∈{1,2,3,5,10}, 60 q)

| k | acc | tokens |  | k | acc | tokens |
|---|---|---|---|---|---|---|
| 1 | 0.200 | 155 | | 5 | **0.350** | 408 |
| 2 | 0.233 | 226 | | 10 | 0.367 | 608 |
| 3 | 0.250 | 295 | | | | |

**MSC ≈ k=5 / ~400 tokens = 95% of the k=10 ceiling.** Two decisive conclusions:
1. **Compression thesis holds** — ~95% accuracy at ~400 tokens; the moat is real on the token axis.
2. **The plateau proves the remaining failures are NOT retrieval-starvation.** Adding context past
   k=5 barely moves accuracy (+0.017), so the ~63% still-wrong questions *have* the context they
   need and fail anyway ⇒ bottleneck is **reasoning / brief-construction / generation**, not
   retrieval and not more oracles. The ceiling (0.367) is a reasoning limit, not a context limit.

### Oracle-routing reframe (the deliverable)
One bench = one *domain* (anecdotal). The frontier regression means **oracles shouldn't FIRE on
anecdotal recall — not that they're bad.** Fix = a **classifier that routes**, and (per codex)
selects a **subset of oracles / per-family weight vector**, not binary on/off:
`query intent → oracle-family weight vector (0=drop) → combiner → warden subset`.
The classifier mostly exists already (`query_intent.TaskIntent`): anecdotal/recall intents →
lean; evidence/currentness/change intents (DEBUG_FAILURE, EXPLAIN_DECISION, CHANGE_ANALYSIS,
VERIFY_CURRENTNESS) → frontier. **Derive the table from the ablation, don't hand-author it.**
Conservative framing: leave-one-out/add-one-in gives MARGINAL effects, not optimal weights
(multiplicative LogSpace combiner ⇒ oracles interact; validate combinations + recalibrate).

**Ablation sweep** (running, `ablation_sweep.sh`): add-one-in ladder node_plain → oracle →
+lens → +warden_gate(±evidence_anchor) → aggressive, × 6 types × 10. Marginal effect of each
frontier component per query class = the anecdotal routing table.
**Missing half:** an oracle-favorable benchmark (code/agent/current-truth) to populate the
router's "on" entries — otherwise the on-branch stays an untested design belief.

## Next artifact: ProvenanceClassifier → FragmentationProfile → BriefBuilder

Justified by measurement (MSC plateau ⇒ bottleneck is brief-construction, not retrieval), not vibes.

**Provenance model (verified):** `Entity ← MENTIONS ← Episodic`; episode has `session_id`
(aligns exactly with dataset `haystack_session_ids`) + `content`. Oracle variant = 1 episode/turn.
**Support definition:** `has_answer` turns **+ their adjacent assistant replies** → episodes →
mentioned entities (the answer often lives in the assistant reply, not the marked user turn;
temporal types extend to the dated-episode set). Per-item correctness comes from the bench
`MemoryCheckpoint` jsonl (run answer harness with `--resume`).

**ProvenanceClassifier emits two products per question:**

*Product 1 — failure bucket:*
- `support absent` — no support entity in top-k recall
- `support present but not packed` — in `recall` but dropped by `build_context` (menhir's brief).
  DEGENERATE in recall-only (no packing stage) — measure BOTH recall and build_context to
  populate it; this bucket IS the measurement of build_context lossiness.
- `support packed but answer wrong` — in packed context, judged wrong → reasoning/generation
- `correct`

*Product 2 — merge shape (the BriefBuilder cluster key), priority-ordered:*
| shape | detector |
|---|---|
| supersession/belief chain | any support edge has `expired_at`/`invalid_at` |
| chronological/date chain | ≥2 distinct `valid_at` across support |
| single episode | support = 1 episode |
| same entity across sessions | 1 entity, episodes span ≥2 `session_id` |
| same entity across episodes | 1 entity, >1 episode, same session |
| multiple entities same session | ≥2 entities, 1 session |

Falsifiable prediction (validates the taxonomy): temporal→date-chain, knowledge-update→
supersession, multi-session→cross-session, single-session-user→single-episode. If shapes cluster
by type, BriefBuilder's per-type merge key is derived, not guessed.

**BriefBuilder** (the artifact): top memories → cluster by the profile's dominant shape → merge
supporting facts → preserve provenance → emit 3–6 compact evidence bundles. Keeps MSC token
efficiency while fixing the "many tiny fragments" (k needs 5, not 3) problem.

Sequence: ablation (running) → provenance classifier run → fragmentation profile → BriefBuilder.

### Results (2026-07-02) — classifier run + BriefBuilder built

**Scope prerequisite fixed:** LME memories are ingested SESSION-scoped, which `build_context`
(and plain recall) filter out unless `include_session=True` (recall_service.py:937) — so the
brief stage was returning empty. Corrected at the data layer: benchmark memories are now written
as regular **PERSISTENT** scope (`scripts/longmemeval/promote_persistent.sh`, run at end of every
build; existing recall-only A/B unaffected — it already passed `include_session=True`, and scope
does not feed scoring). Bench commits `5e1ae0a`, `cb55099` (+ 4 reorg-regression fixes found in the
same sweep: model-name typo, `recall_ab.sh` `${MENHIR_PY}` unbound, scorer default, `buildout_ab.sh`
stale ingest path).

**ProvenanceClassifier v2 (answer-anchored, rank-based; N=90, top-10).** v1's buckets were
untrustworthy (packed via build_context = off-path + empty; "support present" overcounted by broad
`has_answer` marking). v2 anchors support to the gold-**answer** entity (name+summary overlap with
answer-minus-question keywords) and reports recall **rank**:

| rank_1 | rank_2_5 | rank_6_10 | absent | unresolved |
|---|---|---|---|---|
| 36 | 20 | 5 | 7 | 22 |

- **Retrieval is strong for entity-answers:** of the 68 resolvable, 61 (90%) are in top-k, 36 at rank 1.
- **Lever 1 — non-entity answers (22/90 unresolved):** the gold answer maps to no single entity;
  concentrated in **multi-session (9/15)** and **knowledge-update (6/15)**. These are aggregates /
  counts / synthesized facts that need **assembly**, not lookup → the BriefBuilder's job.
- **Lever 2 — knowledge-update burying (absent 7, 4 of them knowledge-update):** the superseded
  value out-ranks the current one → currentness ranking (belief-gate/TemporalOracle axis).

**Product 2 validated the falsifiable prediction** (shapes cluster by type): temporal-reasoning
9/15 supersession + 3 chronological (~80% chains), preference 8 supersession + 6 chrono,
single-session-user local (single-episode/multi-entity). Per-type merge key is derived, not guessed.

**BriefBuilder v1 — BUILT** (`domain/brief_builder.py`, behind `frontier_brief_builder` /
`MENHIR_FRONTIER_BRIEF_BUILDER`, default off; wired into `context_builder`). Clusters recalled
memories on the temporal signal in `ScoredMemory.temporal_facts`: dated facts collapse into one
chronological **Timeline** bundle (world-time ordered, current beliefs marked) that leads the brief;
undated memories follow as compact per-memory bundles, score-ordered; ≤6 bundles, provenance
preserved. Directly serves temporal-reasoning (~80% chains) and surfaces currency for
knowledge-update. Unit-tested (chronology, currency marking, flat fallback, empty).

**BriefBuilder v2 — supersession in the Timeline — BUILT.** `build_context` now recalls with
`include_invalidated=self.brief_builder_enabled`, so superseded beliefs survive the current-belief
filter (recall_service.py:1137) and the Timeline renders the full belief progression, each event
tagged: `- [2022-01-01] lives in Boston (superseded until 2023-06-01)` / `- [2023-06-01] lives in
Seattle (current)`. Timeline is labelled `supersession_belief_chain` when any superseded fact is
present, else `chronological_date_chain`. Directly targets knowledge-update burying (classifier
lever 2): the current value is marked, so it can't be lost under the superseded one. Unit-tested
(supersession end-date, current marking, chronological order) + verified e2e on a knowledge-update
namespace with real superseded edges.

**⚠️ BLOCKER found while validating v2 — backdating is broken on ingest.** E2e on the real graph
showed **all 10,924 episodes + 8,557/8,810 dated edges stamped `2026-06-30`** (build day), not the
session dates → the Timeline's chronology is degenerate and temporal-reasoning is ungrounded.
Root cause (verified live): menhir does **not** honor `occurred_at` on ingest — an episode
ingested with `occurred_at=2020-01-01` lands `valid_at=now()`. `ingest.py` sends the correct date
and the route accepts it (`MemoryRequest.occurred_at` → `queue_episode_for_enrichment` →
`_parse_occurred_at` → `create_pending_episode(reference_time=…)`; the worker reads it back at
`enrichment_steps.py:458`), yet the world-time is lost before the graph node is written. The exact
drop was not fully localized this session (candidates: benchmark-mode path; pending→claim
round-trip; a second sync path `ingest_service.py:825` hardcodes `reference_time=now()`).
**Repaired without re-ingest** via a bench-side backfill (`scripts/longmemeval/lib/backfill_dates.py`,
`lme.sh backfill-dates`): `episode.session_id` → dataset date; `edge.episodes[0]` → source episode;
only `valid_at` rewritten (belief-time preserved), genuine extracted dates kept, revert snapshot
written. Verified: episodic distinct-days 1 → 152, 0 nodes/edges left on the build date. **Menhir
code fix is a tracked follow-up** — until then every build needs `lme.sh backfill-dates`.

**A/B RESULT (2026-07-02) — BriefBuilder is NET-NEGATIVE; do NOT ship, do NOT build v3.**
The Mode-B harness feeds `/api/recall` (flat list) to the answer model and never calls
`/api/context`, so it cannot measure the brief — measured instead with a standalone eval
(`brief_ab.py`: pull `/api/context` with flag OFF vs ON on the date-backfilled graph → gpt-4o
answer → gpt-4o-mini judge, 10/type × {temporal-reasoning, knowledge-update, multi-session}):

| type | brief OFF | brief ON | Δ |
|---|---|---|---|
| temporal-reasoning | 3/10 | 1/10 | **−0.20** |
| knowledge-update | 3/10 | 3/10 | 0.00 |
| multi-session | 1/10 | 0/10 | −0.10 |
| **overall** | **7/30 (.23)** | **4/30 (.13)** | **−0.10** |

N=30 is noisy (±~.15) so the magnitude is soft, but **0/3 categories improved** and the
mechanism is unambiguous from the regressions: the Timeline sorts the lead by **date**,
subordinating recall's **relevance** order. The answer is the top-relevance memory ~53% of the
time and is usually an **undated** entity summary → the Timeline (a) leads with irrelevant-but-
dated facts and (b) demotes the actual answer into the trailing undated section. Example
(`gpt4_2312f94c`, "which device first, S22 or XPS 13?", gold S22): OFF leads with
"Samsung Galaxy S22: … obtained … Feb 20" → correct; ON leads with "[2023-03-15] Anker PowerCore
can charge S22" → "I don't know."

**Design lesson:** "answers need a chronological timeline" was the wrong inference from Product 2.
Product 2's merge shapes describe the answer's *provenance structure*, not that the answer model
wants a date-sorted dump of all facts. Relevance order is the load-bearing signal; chronology is
a *secondary* view that must not displace it.

**REDESIGN (append, not replace) — retest 2026-07-02: harm eliminated, lift unproven.** Rebuilt
so the relevance-ranked K1–10 list **leads** the brief (identical to off) and the Timeline is
**appended below** as a supplementary view (`build_timeline_bundle` + context_builder append path).
Also found + fixed the budget confound: tiktoken was absent → heuristic mode halved the budget to
1000 tok → the Timeline got squeezed out on 24/30 briefs; installing tiktoken restored the full
2000-tok budget so all 30 briefs append a Timeline. Retest (same 10/type × 3):

| type | OFF | ON (append) | Δ |
|---|---|---|---|
| temporal-reasoning | 4/10 | 4/10 | 0.00 |
| knowledge-update | 3/10 | 4/10 | +0.10 |
| multi-session | 1/10 | 1/10 | 0.00 |
| **overall** | **8/30 (.27)** | **9/30 (.30)** | **+0.03** |

Append **removes the −0.10 harm** of replace (no category regresses). But **+0.03 is within noise**:
the OFF arm itself moved 7→8 between runs (gpt-4o non-determinism), so ±1 question at N=30 is the
floor. knowledge-update (+0.10) is the most promising cell (matches the supersession hypothesis)
but is one question. **Verdict: append BriefBuilder is safe/neutral — shippable default-off without
harm, but not yet justified on lift.** Resolving the knowledge-update signal needs a larger N
(≥40–50/cell to resolve a ~5% effect). Flag stays **default off**.

**Not pursued (v3):** entity-relation clustering for the multi-fragment non-entity answers
(the GPS case: `dealership`/`GPS antenna`/`map updates` are distinct entities; counts like "how
many bikes" need the countable items co-located). Needs a cluster key beyond exact entity name —
shared source session (`entity ← MENTIONS ← Episodic.session_id`) or graph adjacency (`RELATES_TO`),
both requiring a provenance fetch in the brief path. **Gate this on measurement:** now that dates
are backfilled, A/B the flag on the graph first (knowledge-update + temporal accuracy,
brief-builder on vs off) to confirm lift before investing in the fuzzier clustering.

## Out of scope / defer
- `EdgeFactOracle` "directly answers the query slot" (slot extraction) — v2; ship the
  cheap signals first (has predicate + `occurred_at`/`valid_at` + episode/user provenance).
- A 4th `episodic` lens distinct from `historical` — only if a measured need appears;
  `historical` suffices for now.
- The full `scripts/longmemeval/` framework consolidation (separate plan) — the probes
  above graduate into it once this ladder settles.
