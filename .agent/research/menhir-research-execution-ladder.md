# Menhir Research → Production Execution Ladder

## Status

active (read-side only) — the ordered plan for taking the `docs/research/` corpus into
code. Its read-side rungs are benched and closed; the live write-side arc is not yet
sequenced here. See "Write-side verdict" and "What to build next (current)".

## Bench verdicts — reconciliation (2026-07-04)

The read-side ladder (R1 hybrid, R4–R7 oracle stack) has now been RUN on real corpora, and the
honest verdicts are in. They do not invalidate the shipped-gated stack (it ships behavior-neutral,
default-off/shadow), but they overtake the "graduate → flip default-on" premise below: several
rungs still read `in-progress`, yet the bench result already landed neutral-to-negative, and the
live direction has moved to write-side consolidation. Detail lives in the linked benchmark-notes;
do not re-derive it in the rung blocks.

- **R1 — does NOT graduate (real, live-validated 2026-07-05); `hybrid_alpha` stays unset.**
  `archolith-bench/.agent/benchmark-notes/r1-dummy-gold-run.md`. The gate has now been **recalibrated**
  (saturated metrics like `exact_string_recall`=1.0 are exempt from the must-beat test — the old
  "beat exact" bug is gone) and the miner's symbol/scope families **fixed** (paraphrase vehicle +
  raw-identifier fallback). A fresh live re-run on the 23.8k-node prod clone (155 queries) then gives
  a **genuine** verdict: `E_hybrid_a0` symbol_recall 0.700 < baseline 0.710 and regresses wrong_scope
  (0.034→0.081); on the sole headroom family (`paraphrased_debug_question`) the source-aware floor is
  **neutral-to-negative** (0.517 vs 0.533) — the opposite of the earlier 40-query run (+0.05), so the
  two bracket zero (within noise). symbol/scope now **saturate** (raw identifier: 0.975 / 1.000; only
  22 unique classes / 0 scope pairs have rich summaries to paraphrase). Conclusion: R1's attributed-
  hybrid floor does **not** earn graduation on the dummy corpus — it joins the oracle stack as a
  read-time lever that lands neutral-to-negative. `hybrid_alpha` deliberately **left unset**.
- **R6/R7 — oracle stack LOSES on LongMemEval.**
  `archolith-bench/.agent/benchmark-notes/lme-score-campaign.md`: isolated A/B on
  temporal-reasoning + knowledge-update scored **node-only 0.400 > semantic+temporal 0.367 > full
  oracle stack 0.333**. Every read-time lever landed neutral-to-negative; the EvidenceAnchorWarden
  *zeroes* anecdotal questions. The elegant mechanisms (a historical-lens TemporalOracle promoting
  a superseded answer to rank 1) are correct on individual questions but do not generalize to
  measurable lift. Node-only relevance ranking is the champion. This is the harder-benchmark
  opposite of the small dummy-nDCG result that shipped `oracle_ranking` on-by-default — so on
  LongMemEval the oracle gates do not earn their keep.
- **The pivot — aggregation is a consolidation problem, not a retrieval problem.**
  The campaign's own conclusion: "you cannot re-rank or re-format your way to information candidate
  generation never assembled." So 2026-07-01→04 moved off oracle-ladder rungs into the perception /
  Event→Fold→View / D0 write-side arc (`aggregation-as-consolidation.md`): maintain quantitative
  state at write/consolidation time so multi-session answers are a lookup, not a fuzzy re-rank.

Standing effect on the rungs below: R1/R6/R7 statuses stay `in-progress` (mechanism built, gated,
behavior-neutral) but are **not** "flip default-on once benched" — the bench spoke. Re-opening any
of them means new bench headroom (a recalibrated R1 gate, a real R2 facet fixture), not more
read-time ranking.

## Write-side verdict — the pivot paid off (2026-08-09)

The write-time consolidation arc the 2026-07-04 pivot moved to has now been measured on the same
78-item LongMemEval knowledge-update oracle fixture the read-side campaign used
(SHA256 `bba252a302e7b257a0f7457fe97411f7de144aafd1c6b44c98a0e88ee8570907`), so the numbers are
directly comparable.

| Run | Score | Recall | Notes |
|---|---|---|---|
| `scalar-canonical-ku78-v1-20260806` | 0.872 | 68/78 | Previous canonical baseline |
| `scalar-event-activity-ku78-v4-20260809` | 0.885 | 69/78 | Fresh clean run; superseded by v6 |
| **`scalar-event-activity-ku78-v6-20260809`** | **0.910** | **71/78** | **Current canonical evidence** |

Against a `no_memory` arm of 6/78 (0.077), v6 is a **+0.833 delta**. Provenance: Menhir
`1fa57955`, Bench `d5e97cc4`, both tracked-clean; fresh non-resumed graph; one attempt; two-item
checkpoint passed before release; 78/78 manifest rows with `failed_remaining=0`; harness exit 0.
Configuration: adaptive segmentation, 2/3 scalar agreement, `k=3`, 1/1/1 attribute/scope/subject
reconciliation, scalar state/history and View authority enabled, Event History and Event History
authority enabled, deterministic scalar router and shadow paths disabled, TurnEvidence required.
Models: gpt-4o-mini extraction/enrichment, text-embedding-3-small embeddings, gpt-4o answers,
gpt-4o-mini judge.

Sources: archolith-bench `results/lme-ku-buildout/LEDGER.md` is the evidence authority, and
`.agent/plans/menhir-cumulative-activity-scalars-2026-08-08.md` carries the same acceptance record
in this repo (integrity counts, score, token/cost, per-miss reasoning). Prefer the ledger when they
disagree; the plan is the in-repo copy, not the register.

Read this against the read-side verdicts above. The oracle stack could not re-rank its way past
0.400 on node-only; write-time consolidation reaches 0.910 on the knowledge-update slice. That is
the empirical case for the pivot, not just its rationale.

Three caveats the ledger states and this doc inherits:

- **It is benchmark evidence, not an approved launch headline.** The subset is knowledge-update,
  not all of LongMemEval, and the comparison arm is `no_memory`.
- **"Canonical" is a documentation-level designation.** The immutable artifacts carry provenance
  (clean commits, fixture hash, fresh graph, exit 0) but no machine-readable `evidence_status`
  field. `arm: candidate` in `run_provenance.json` names the 2/3-reconciliation benchmark arm; it
  does not make the run noncanonical. Future runs should emit an explicit `evidence_status` that
  the runner validates. Do not retroactively edit v6's artifacts.
- **Seven misses remain** (`f9e8c073`, `c4ea545c`, `e61a7584`, `a2f3aa27`, `26bdc477`,
  `031748ae_abs`, `07741c45`). They are heterogeneous. The clearest deterministic defect is
  `26bdc477`: `trip_count=3` and `trip_count=5` both minted but left `binding_pending` because
  possessive "my camera" did not bind to the co-mentioned "Canon EOS 80D camera". Backlog until a
  non-benchmark panel establishes the general alias pattern — do not tune it against the fixture.

## What this owns

This is the dependency-ordered build sequence that turns the research notes into
implemented, benched mechanisms. It is the execution counterpart to the research
index.

Ownership boundaries (no overlap):

```text
.agent/memory-roadmap.md      shipped v1 milestones (M0-M7) — history.
.agent/post-v1-todo.md        bugs/ops/deferred features on the SHIPPED system.
docs/research/README.md       the research notes + their promotion conditions.
docs/research/vision/cognitive-replay-and-phasing.md   the CONCEPTUAL phase ladder.
THIS doc                       the executable rung order: research -> code -> bench.
```

This doc replaces the loose "Current next implementation targets" /
"Current immediate process" lists that used to live in
`docs/research/README.md`; that index now points here.

Sequencing companion: `docs/roadmap/oracle-integration-plan.md` (weekend Day 3) maps the specced oracle
pieces to buildable-now vs gated (embedder / L3-L4 sequencing) and carries a written issue list.

## How to read a rung

Each rung is one shippable step. Fields:

```text
goal        what is true after this rung
owner       research doc that owns the mechanism
code        primary code surface(s)
bench       archolith-bench fixture(s) that prove it
metric      what archolith-bench measures
depends_on  rungs that must land first
status      planned | in-progress | done | parked
```

Discipline (from research-process.md / the index anti-sprawl rules):

```text
1. Every rung lands a transparent baseline before any heavy dependency.
2. No rung promotes on vibes; archolith-bench decides graduation.
3. Retrieval-control rails must not change final ranking nondeterministically.
4. Retrieval alone must not promote truth, currentness, or durable usefulness.
5. Iterative amplification must beat the one-pass oracle combiner to ship.
```

## Dependency graph (edge index)

```text
R0  feeds        R1, R3, R4
R1  depends_on   R0
R2  depends_on   R1
R3  depends_on   R0
R4  depends_on   R1, R0
R5  depends_on   R4
R6  depends_on   R4, R5
R7  depends_on   R6, R3
R7.5 depends_on  R6, R7    (ablation/analysis; feeds R10/R11 go-no-go)
R8  depends_on   R7
R9  depends_on   R7, R8
R10 depends_on   R5, R7
R11 depends_on   R7        (bench-gated; may be rejected)
P4  depends_on   R3, R9    (experience memory)
P5  depends_on   R8, R9    (background cognition)
PR  depends_on   R3, R9, P4 (cognitive replay)
PA  depends_on   PR        (model adapters)
```

## SOS direction reconciliation

The `docs/research/direction/semantic-operating-system.md` + `oracle-architecture.md` synthesis
(four-layer knowledge model; Programs A–E; the 6-phase build) is the architectural
*direction*; this ladder is its *build order*. They mostly describe the same system from
two angles. The mapping:

```text
SOS Program / Phase                        ->  this ladder
Program A  Deterministic structural        ->  already shipped (ingest_project, structural_anchoring,
           foundation (Layer 1/2)              structure_queries, query_structure) + R0 traces
Program E  Oracle-driven context assembly  ->  R4 → R5 → R6 → R7 (oracle interface → scheduler →
           (cold start brief, Layer 2/3/4)     cheap oracles → one-pass combiner) ; the R7 combiner
                                               output is the retrieval-shaped OraclePacket — the
                                               kernel of, not the full, ColdStartBrief (see
                                               oracle-runtime-interfaces.md: two altitudes)
Program C  Knowledge evolution (temporal,  ->  R3 (belief buckets + currentness) + the temporal
           supersession, confidence)           rungs; belief-layer.md owns the policy
Phase 2 Progressive Retrieval              ->  R1 (hybrid) + R2 (facet) candidate generation
Phase 3 Temporal Intelligence              ->  R3
Phase 4/5/7 Experience/Background/Adapters ->  P4 / P5 / PA
Cognitive Replay                           ->  PR
observe/decide/write + epistemic law       ->  R8 (rails) + R9 (MemoryMutator write boundary)
```

GAP (needs sequencing by ctharvey, do not silently invent rungs): the SOS **Layer 3
Semantic Model** (LLM-proposed Capability/Policy/Constraint/Invariant/Decision nodes) and
**Layer 4 Institutional Knowledge** (incidents, failed approaches, rationale) — i.e. SOS
**Program B (semantic understanding)** and **Program D (institutional memory)**, plus the
**evidence-as-first-class entity** and **knowledge-promotion lifecycle** — are the one part
of the SOS direction with **no rung here yet**. They are a new modeling subsystem, not a
retrieval rung, and they carry the most scope risk (LLM-generated semantics, review state,
supersession). When promoted they should land as their own track with the same discipline:
transparent baseline first, evidence-first (every semantic node starts untrusted/low-confidence),
bench-gated, no heavy deps before a baseline fails. Until then this ladder remains the
retrieval/oracle/belief build order and Program B/D stay design-only. **Schema specs now exist
(spec-only, still unsequenced):** `docs/research/schemas/layer4-knowledge-artifacts.md` (the generic L3/L4
knowledge-artifact schema + promotion lifecycle) and `docs/research/schemas/cold-start-brief.md` (the task-shaped
ColdStartBrief + context-pack provenance). They define the shape; they do not create a rung.
**Implementation options to choose from:** `docs/roadmap/l3l4-overlay-sequencing-options.md` lays out five
distinct build strategies (evidence-first capture / LLM-proposed review-gated / bench-first falsification
/ reuse-shipped-substrate / brief-driven) + a comparison matrix + a recommended hybrid. ctharvey picks
one; only then does it become rungs. **The hybrid (C→A→B) is now fully decided** —
`docs/roadmap/l3l4-hybrid-sketch.md` (decision register + prior-art audit). Key audit finding: the
governance substrate is **largely already built** (the `scope='CANDIDATE'` review tier +
approve/reject + conflict/decay pipeline), so the overlay's net-new work shrinks to artifact TYPES, a
first-class `Evidence` node, an LLM proposer (another candidate emitter), the bench gate, and the
ColdStartOracle/brief. Still pending ctharvey's go-ahead to turn the decisions into rungs. **First concrete slice planned:**
`.agent/plans/l4-artifact-loop-v0.md` — a minimal, bench-first L4 artifact loop (Decision/Failure/
Incident → evidence → CANDIDATE/TRUSTED → R9-lite writer → MemoryOracle → ColdStartBrief v0), with a
failed-approach-avoidance benchmark and a 6-commit build plan (commits 1–5 bench-only; commit 6 the
gated menhir port + the one `:Evidence` graph node).

## Track 0 — Foundation

### R0 — Retrieval + oracle observability

```text
goal    every recall emits a trace: phase timings, candidate source, score
        parts, source family, survived-filters flag; oracle runtime telemetry.
owner   oracle-execution-and-performance.md (snapshot/budget), research-process.md
code    src/menhir/services/recall_service.py, scoring_service.py (+ trace emitter)
bench   trace fixtures; establishes the determinism/latency baseline
metric  latency_ms (per phase), ranking_determinism, baseline recall_at_k
depends_on  —
status  in-progress  (menhir-side instrument BUILT ac7204b: inline RetrievalTrace
        — per-phase timings + per-candidate source/similarity/survived_floor +
        survivor score-parts/rank, opt-in recall(trace=True), default-off no-op.
        8 unit tests green. NOT done: the bench consumes it for the R1 A-E ladder;
        oracle-runtime telemetry + persistence/log sinks deferred.)
```

Rationale: nothing below can be tuned or benched without traces. Build first.

> CI/CD note: post-v1-todo flags CI/CD as the top infrastructure gap. The bench
> ladder below assumes archolith-bench can run in CI. If it cannot yet, that is a
> prerequisite sub-task of R0.

## Track A — Candidate generation (lower stack)

### R1 — Hybrid candidate generation + source-aware priors

```text
goal    vector + lexical/BM25 candidate paths with a tunable hybrid alpha;
        each candidate source carries an explicit prior so BM25/facet/structure
        candidates survive the semantic-similarity floor.
owner   retrieval-tuning-stack.md; oracle-execution-and-performance.md (priors/floor)
code    src/menhir/domain/retrieval_tuning.py (RetrievalTuningConfig),
        src/menhir/services/hybrid_retrieval.py,
        recall_service.py / scoring_service.py (min-similarity -> source-aware filter)
bench   exact_error_string, symbol_name_query, paraphrased_debug_question,
        stale_semantic_neighbor, wrong_repo_same_topic, buried_relevant_memory,
        historical_only_vs_current_truth
metric  exact_string_recall, symbol_recall, recall_at_k, stale_hit_rate,
        wrong_scope_injection_rate, latency_ms
depends_on  R0
status  in-progress  (increment 1 landed e8da67d: attributed hybrid candidate
        generation + source-aware floor, code + tests, default-off. NOT done:
        bench ladder A–E + live-graphiti scale check + hybrid_alpha tuning owed
        — see deferred-verification.md. "done" requires the bench, per discipline.
        VERDICT 2026-07-05 (live re-run, recalibrated gate + fixed miner vehicle): does NOT
        graduate — REAL this time, not the gate artifact. Gate correctly exempts saturated
        exact and tests symbol_recall; E_hybrid_a0 loses (0.700<0.710) + regresses wrong_scope;
        on the sole headroom family (paraphrase) the floor is neutral-to-negative (0.517 vs
        0.533). hybrid_alpha stays UNSET. See "Bench verdicts — reconciliation" up top +
        archolith-bench r1-dummy-gold-run.md.)
```

### R2 — Facet candidate generation

```text
goal    MemoryFacetIndex as a deterministic candidate generator; MeetPointReranker
        as convergence scoring over facets/structure/time/evidence.
owner   facet-retrieval.md (extractor path: facet-extraction-plan.md)
code    benchmark-local (R2 is bench-first): archolith_bench/facet/ —
        models.py / extractor.py / index.py / reranker.py / baselines.py /
        metrics.py / runner.py. Production CandidateSource.FACET seam reserved in
        retrieval_tuning.py for post-graduation only.
bench   facet-first vs BM25/embedding/hybrid/file-context baselines, ladder A–F
        × {gold, extracted} (archolith-bench)
metric  recall@5, precision@5, MRR, NDCG, paraphrase_stability, stale_hit_rate,
        wrong_scope_injection_rate, support_sufficiency, false_neighbor_rate, latency_ms
depends_on  R1
status  in-progress  (bench-first; mechanism + A–F ladder + promotion-gate logic
        built in archolith-bench with 59 unit tests; gold-mode F graduates,
        extracted-mode collapses, and HYBRID mode (Priority 6, facet-extraction-plan.md)
        recovers F recall 0.28->0.83 (gold 0.85) on the DRAFT fixture — confirming the
        bottleneck is structural-facet extraction, not the engine. NOT done: real
        deterministic facets (Layer-2/Git) + real extraction model + real embedder +
        ctharvey's hardened fixture. No menhir production change until F graduates on
        the real setup.
        UPDATE 2026-07-05: real embedder swapped in (run_facet_bench.py --embedder openai,
        text-embedding-3-small) — F still GRADUATES gold+hybrid on the DRAFT fixture even
        as it lifts the baselines (wrong_scope 0.07 vs 0.38-0.40; <=0.05 recall loss). The
        "real embedder" owed item is DONE. See archolith-bench facet-r2-real-embedder-run.md.
        UPDATE 2026-07-05b: the "real DERIVED structural facets" owed piece is DECOMPOSED
        (facet-r2-structural-facet-decomposition.md): symbol facets are text-improvable
        (extractor snake/SCREAMING_SNAKE rules -> symbol recall 0.11->0.55, extracted-mode F
        recall 0.275->0.425), but FILE facets have recall 0.00 -- the gold paths are NOT in
        the prose, so they require the code graph's ANCHORED_TO edges, not extraction. So
        hybrid's gold-structural stand-in is the CORRECT model for graph-anchored facts; the
        remaining owed question is production ANCHORED_TO coverage (graph-gated), plus
        ctharvey's hardened fixture (Risk #1). Extraction alone has a ceiling at file facets.
        UPDATE 2026-07-05c (LIVE, prod-clone dummy 7687): ANCHORED_TO gives memories their exact
        FILE facets from the graph (targets are structure_path file nodes) -- but only 24.5% of
        memories are anchored (1300/5314; anchored ones avg 9 files, 750 with >=3; other 75.5% have
        NONE). So CandidateSource.FACET is a BOUNDED win: it works on the ~1/4 code-anchored slice
        (where hybrid's gold-structural stand-in is realistic), not the whole corpus. The lever to
        grow it is ingest-time anchoring coverage, not a better engine/extractor.)
```

## Track B — Belief (extend shipped BeliefCircuit)

### R3 — Belief buckets + currentness policy

```text
goal    extend BeliefCircuit with historical/anergic/blocked buckets; retrieval-
        time policy keyed on current-vs-historical query intent; feed Git/structure
        stale signals into belief evidence.
owner   belief-layer.md
code    src/menhir/domain/belief.py (exists), recall/scoring/lifecycle integration
bench   stale assertion, belief drift, Git-aware invalidation
metric  current_truth_suppression_accuracy, historical_context_preservation
depends_on  R0
status  in-progress  (Rung-0 BeliefScorer pre-existing. R3 currentness policy LANDED
        0c59bef: RecallBucket += HISTORICAL_ONLY/ANERGIC_CURRENT/BLOCKED, QueryIntent,
        currentness_bucket() + build_intent_aware_packet() — additive, Rung-0 path
        untouched, 8 tests. Bench ladder LANDED e58a5bc (archolith-bench r3/):
        A_assert_all/C_belief_buckets/D_currentness; on the CE-willow demo D GRADUATES
        — stale-current 0.60->0.00 with zero historical loss (C loses half). All 7 real
        fixture families landed (abf569d/7261c4c/fad086b) — synthesis: R3 is essential for
        confidently-held stale beliefs (refactor case, where Rung-0 is useless). Rung E
        (RetrievalExhaustionPenalty) LANDED 6924494 (domain/exhaustion.py) + session-replay
        bench 35b0de0: loop injections 0.40->0.00, productive/exempt retention 1.0. Rung F
        (bounded structural expansion) LANDED f7216cf (domain/structural_expansion.py) + bench
        f145d40: structural_neighbor_recall 0.0->1.0, hub suppressed, pool bounded. Rung B
        (temporal metadata / Chronostratum clock model) LANDED ac6f54d (domain/temporal.py) + bench
        98a3c31: temporal precision 0.58->1.0, leak 0.42->0.0, no recall loss. R3 LADDER COMPLETE
        (B + C/D + E + F + 7 real fixtures). Git/structure stale-evidence LANDED (belief-layer
        Rung 2): domain/git_staleness.py — ancestry/branch/stash/rename-correct (NOT date-only).
        Chronostratum domain rungs also landed (see the temporal-track note below): 1C formatter +
        Rung 2 intent (domain/temporal_intent.py) + Rung 3 ingestion-order independence + Rung 4.5
        durable identity (domain/repo_snapshot.py). Warden trio (Currentness/Exhaustion/Scope) +
        WardenChain + consolidation bench landed (decide layer). NOT done: production recall/graph
        wiring (gated until graduation on confirmed labels). Chronostratum Rung 5 LIFTED to its own
        Oracle-altitude plan: menhir-structure-temporal-oracle-plan.md (root).)
```

Note: can proceed in parallel with Track A; both only need R0.

### The three object types: Oracle / Warden / Mutator (observe / decide / write)

menhir's recall/knowledge stack has three object types, peers at three altitudes:

```
Oracle   observe -> a score/signal over a candidate (read-only)        [R4-R7, archolith_bench/oracle/]
Warden   guard   -> an operational decision at the assertion boundary  [domain/warden.py]
                    (ADMIT / FLAG / ATTENUATE / REFUSE)
Mutator  write   -> the persistence boundary                            [R9, services/*]
```

A **Warden** decides whether a retrieved memory may enter the agent's current-truth context.
It consumes signals (from Oracles and from the Chronostratum temporal/git producers) and
emits a `WardenVerdict`; it neither scores (Oracle) nor writes (Mutator). Built wardens:
`CurrentnessWarden` (superseded -> refuse/flag), `ExhaustionWarden` (loop -> attenuate/refuse),
and `ScopeWarden` (SelfToleranceGate: wrong repo/project/branch/namespace -> refuse, fed by
`domain/scope.py`); they compose via `WardenChain` (most-restrictive-wins), the way Oracles
compose via a combiner. ScopeWarden closes the temporal-vs-scope gap the `r3_rename_wrong_scope`
fixture exposed (currentness ADMITs a current belief in the wrong repo; scope REFUSEs it).

### Temporal-track reconciliation (one producer, named consumers)

"Temporal" appears in three plans — R3-belief (Program C), the Chronostratum plan
(`.agent/plans/menhir-temporal-chronostratum-plan.md`, root workspace), and the oracle
**TemporalOracle** (R6). Ownership, to stop them drifting:

- **Producer (single):** `domain/temporal.py` is the one deterministic bitemporal substrate
  (four-stamp clock model + validity/supersession). `domain/git_staleness.py` grounds
  supersession in recorded git (ancestry/branch/stash/rename, not dates);
  `domain/repo_snapshot.py` supplies durable file identity (Rev 5); `domain/temporal_intent.py`
  selects the lens. These are the Chronostratum rungs — the temporal SIGNAL layer.
- **Consumer A — Wardens** (`domain/warden.py`): `CurrentnessWarden` turns the supersession
  signal into an assertion-boundary decision (REFUSE/FLAG). This is where Chronostratum
  meets the belief plan: temporal/git PRODUCE, Wardens DECIDE.
- **Consumer B — oracle TemporalOracle** (R6): turns the same signal into a ranking score.
  It must consume `domain/temporal.py`, not re-derive supersession.

Rule: supersession/validity is computed ONCE (the producer); the Warden decision and the
oracle rank are two consumers of that one signal. No second supersession implementation.

## Track C — Oracle pipeline

> Two oracle altitudes (see `docs/research/retrieval/oracle-runtime-interfaces.md`): the **retrieval** oracle
> (`RetrievalOracle.evaluate(query, candidate) -> OracleResult`) is R4/R6, and its **combiner** (ranking,
> output = the R7 **OraclePacket**) is R7 — these need no LLM and no L3/L4, so they are the build-first
> part. The **task** oracle (`Oracle(OracleInput) -> OracleFinding`; primitive vs composite;
> ColdStartOracle et al.) is the richer runtime that produces the task-shaped **ColdStartBrief** (NOT
> the OraclePacket) — it depends on the L3/L4 semantic overlay and is therefore the **unsequenced GAP**
> below, not a numbered rung. Build R4–R7 first.

### R4 — RetrievalOracle interface + thread-safe executor

```text
goal    immutable RetrievalOracle/OracleResult API; bounded parallel OracleExecutor;
        query-snapshot prefetch (commit range, dependency cone, bulk metadata);
        deterministic reduction order. Oracles are read-only.
owner   oracle-amplified-retrieval.md; oracle-execution-and-performance.md
code    src/menhir/domain/oracles.py, services/retrieval_oracles.py,
        services/oracle_executor.py
bench   oracle fixture shape with known relevance labels + per-signal scores
metric  ranking_determinism, oracle_wall_time_ms
depends_on  R1, R0
status  in-progress  (bench-first prototype in archolith_bench/oracle/ — 38 tests.
        MENHIR PORT of R4 LANDED 14b3e4f: domain/oracles.py (immutable RetrievalOracle/
        QueryContext/CandidateMemory/OracleResult; read-only ENFORCED via MappingProxyType)
        + services/oracle_executor.py (bounded async fan-out, deterministic post-gather
        reduction, per-oracle timeout->neutral, error->neutral, prefetch seam), 12 tests.
        NOT done: R6 cheap oracles port (Semantic/Structure/Scope/Temporal/Evidence — these
        should CONSUME the existing domain producers: TemporalOracle<-temporal.py,
        ScopeOracle<-scope.py, StructureOracle<-structural_expansion/structure_temporal) +
        R7 combiner port + real semantic scorer. See bench oracle-r4-r7-demo-run.md.)
```

### R5 — CostAwareOracleScheduler

```text
goal    static cost classes (cheap/io/expensive/model), bounded lanes, longest-
        runner-first by estimated p95, runtime telemetry, deterministic reduction.
owner   retrieval-control-rails.md; oracle-execution-and-performance.md
code    src/menhir/services/oracle_scheduler.py
bench   tail-latency fixtures
metric  oracle_p95_latency, oracle_tail_wait_ms, oracle_parallel_speedup,
        oracle_timeout_rate
depends_on  R4
status  planned
```

### R6 — Cheap oracles

```text
goal    SemanticOracle, ScopeOracle, TemporalOracle (metadata), StructureOracle
        (prefetched file context) running through the executor/scheduler.
owner   oracle-amplified-retrieval.md
code    services/retrieval_oracles.py (per-oracle)
bench   oracle baseline ladder A-D vs current scoring
metric  recall_at_k, temporal_accuracy, wrong_scope_injection_rate
depends_on  R4  (R5 scheduler is an optional optimization, not required for correctness)
status  in-progress  (bench-first set in archolith_bench/oracle/. MENHIR PORT LANDED bfcf87c:
        services/retrieval_oracles.py — Semantic/Structure/Scope/Temporal/Evidence +
        default_oracles(), run on the R4 executor. Menhir difference: TemporalOracle CONSUMES
        domain/temporal.temporal_role and ScopeOracle CONSUMES domain/scope.scope_conflict
        (one-producer rule — the oracle that ranks uses the same classifier the warden decides
        on); EvidenceOracle consumes the self_reinforcement anchor vocab; Semantic keeps the
        pluggable real-embedder seam (lexical stub default). 11 tests. NOT done: real embedder
        injected; R5 cost-aware scheduler.
        VERDICT 2026-07-04: on LongMemEval the oracle stack LOSES to node-only
        (0.400 > sem+temporal 0.367 > full 0.333). See "Bench verdicts — reconciliation" up top.)
```

### R7 — One-pass OracleCombiner (role-specific log-space)

> **A linear combiner lets relevance buy back currentness; role-separated logits don't.**
> That failure mode — "very relevant but stale" buying its way back into top-k — is exactly what
> menhir exists to prevent, and is the whole reason R7 is not just E with extra steps.

```text
goal    combine oracle results into z_relevant/current/historical/conflict/blocked
        logits; contradiction as negative log-evidence; source-family caps;
        missing-evidence = uncertainty, not falsity. No single hidden score.
owner   oracle-amplified-retrieval.md (combiner math)
code    src/menhir/services/oracle_combiner.py
bench   oracle ladder E (one-pass weighted) vs F (log-space role logits)
metric  oracle_ablation_delta, current_truth_suppression_accuracy,
        historical_context_preservation
depends_on  R6, R3
status  in-progress  (bench-first: WeightedOracleCombiner (E) + log-space
        LogSpaceOracleCombiner (F) in archolith_bench/oracle/; Temporal oracle
        now structured (CURRENT/SUPERSEDED/NOT_YET_VALID/NOT_YET_KNOWN-anachronism/
        UNKNOWN + graded directness). F graduates on oracle_hard.json (edge now on
        wrong-scope, no recall loss); on oracle_correlated.json E TIES F after the
        temporal restructure — i.e. a better temporal oracle lifted the linear
        baseline, so the currentness gap was partly oracle-quality, not combiner.
        The per-family cap is DORMANT (one oracle per family => never binds), so it
        is not the lever; on a validator-clean deep scope fixture E and F CONVERGE
        (F's wrong-scope wins were thin-scope artifacts the validator now flags).
        VERDICT: bench-justified but NARROW — F's edge over a well-tuned weighted
        sum is small + boundary-driven; the robust contribution is the decomposition
        + instruments (oracles, R7.5 ablation, validator), not the combiner formula.
        MENHIR PORT LANDED e1d2540: domain/oracle_combiner.py (WeightedOracleCombiner E +
        LogSpaceOracleCombiner F) + OraclePacket in domain/oracles.py. End-to-end
        executor(R4)->oracles(R6)->combiner(R7) tested; output feeds the Wardens. 8 tests.
        Owed: real embedder + weight calibration; R5 scheduler.
        VERDICT 2026-07-04: the full stack LOSES on LongMemEval (node-only 0.400 > full 0.333) —
        the narrow bench-justified F edge does not survive the harder benchmark; the read-time
        levers are exhausted and the direction moved to write-side consolidation. See "Bench
        verdicts — reconciliation" up top.)
```

This is the **killer baseline**. Everything iterative (R11) must beat it — **R11 remains blocked.**

### R7.5 — Oracle ablation (contribution matrix)

```text
goal    attribute gains to LAYERS, not the opaque pipeline: for each fixture run
        semantic-only, then semantic+{temporal,scope,evidence,structure} with the
        combiner held fixed (E) to isolate each oracle's marginal value, then
        all[E] vs all[F] to isolate the combiner. Output = a contribution matrix.
owner   archolith-bench-operational-model.md (eval); oracle-amplified-retrieval.md
code    archolith_bench/oracle/runner.py (run_ablation) + run_oracle_bench --ablate
bench   ablation over the oracle fixtures
metric  per-oracle Δ on recall / stale_hit / wrong_scope / current_truth_suppression
depends_on  R6, R7
status  in-progress  (built; on oracle_hard the gains are overwhelmingly in the
        ORACLE layer — Temporal owns stale/current-truth, Scope owns wrong-scope,
        Structure owns recall; the combiner choice E→F is a smaller move than any
        single strong oracle. This is the result that reframes the contribution:
        retrieval is a benchmarkable decomposition, not one ranking formula.)
```

Why this rung exists: the bench found that **oracle quality and combiner quality are not independent** —
a better temporal oracle lifts *every* downstream combiner and shrinks the combiner gap. R7.5 quantifies
where gains actually come from, so we don't credit the combiner for an oracle's work (or vice-versa),
and so a future neural combiner can be swapped in without losing the oracle decomposition or the eval.

**Eval instrument (cross-cutting): the oracle fixture validator** (`archolith_bench/oracle/validate.py`)
flags errors (dangling/stale-gold/anachronistic-gold/date-order) + silliness (uncontested, fake-paraphrase,
thin-scope < k, no-stale, no-scope-var). It auto-runs before every ladder/ablation run so a "silly"
fixture can't quietly produce a misleading result — and it sharpened the R7 verdict above by showing
F's wrong-scope wins were thin-scope artifacts.

**R7 is bench-only machinery — do NOT claim from this:** real embedding performance · calibrated
weights · latency viability · general benchmark superiority · production readiness.

**Real-setup promotion gate (the bar to earn a menhir production surface):** F must beat E when *all*
hold — (1) the semantic scorer is real; (2) the stale truth has a stronger semantic match than the
current truth; (3) duplicate stale evidence is present; (4) scoped corpus depth ≥ k per scoped query;
(5) no recall@5 loss. Until then R7 stays in archolith-bench.

## Track D — Control rails + write boundary

### R8 — SelfReinforcementGuard

```text
goal    pending-touch vs productive-touch; EvidenceAnchorGate (>=1 non-self anchor);
        meta-memory depth budget; session-local RetrievalExhaustionPenalty;
        ContradictionInterrupt freezes reinforcement.
owner   retrieval-control-rails.md
code    src/menhir/domain/retrieval_control.py,
        src/menhir/services/self_reinforcement_guard.py
bench   CE willow self-reinforcement fixture; control-rails ladder A-F
metric  productive_touch_rate, stale_heat_leak, self_reference_ratio,
        retrieval_entropy, top_memory_dominance
depends_on  R7
status  planned
```

### R9 — MemoryMutator write boundary

```text
goal    name the Mutator surface; only layer allowed to change state, only after
        belief reduction; verbs = create/assign/expire; promotion, decay,
        contradiction updates, cache refresh, lifecycle transitions live here.
owner   oracle-execution-and-performance.md
code    src/menhir/services/memory_mutator.py
bench   write-boundary invariants (oracles never mutate during evaluate)
metric  ranking_determinism (unchanged by mutation), no-write-in-evaluate assertion
depends_on  R7, R8
status  planned  (NOT greenfield: the write OPS already exist, scattered —
        candidate_repository.promote_candidate/delete, ConsolidationRepository decay +
        conflict-resolve, memory_graph_adapter. R9 = NAME/CONSOLIDATE them behind one
        boundary + add the no-write-in-evaluate assertion, not build from zero.)
```

## Track E — Optional / bench-gated

### R10 — CrossEncoderRerankOracle

```text
goal    local reranker over top-N, behind MeasurementBudgetGate / explicit budget.
owner   retrieval-tuning-stack.md; oracle-amplified-retrieval.md
code    src/menhir/services/rerank_oracle.py
bench   oracle ladder G (hybrid+facet+rerank)
metric  rerank_wall_time_ms, quality delta vs R7
depends_on  R5, R7
status  parked-until-needed
```

### R11 — OracleAmplifiedRetrieval (bench only)

```text
goal    iterative probability amplification simulator; promote ONLY if it beats
        the one-pass log-space combiner (R7).
owner   oracle-amplified-retrieval.md
code    services/oracle_amplified_retrieval.py (simulator first)
bench   oracle ladder G/H + MeasurementBudgetGate
metric  recall_at_k on buried memories, entropy_reduction, convergence_iterations
depends_on  R7
status  bench-gated (reject if it does not beat R7)
```

## Phase rungs (post-pipeline, conceptual phases 4-7)

These map to `docs/research/vision/cognitive-replay-and-phasing.md`. They need the
pipeline + belief + mutator below them.

```text
P4  Experience Memory      experience records (state/goal/.../friction)        depends_on R3, R9
P5  Background Cognition    PainScan, consolidation, candidate pool rebuild,    depends_on R8, R9
                           cache refresh, durable extraction, skill/hook promotion
PR  Cognitive Replay        reconstruct belief/understanding state over time;  depends_on R3, R9, P4
                           requires the epistemic-separation law in force
PA  Model Adapters          swap reasoning engines over the stable substrate    depends_on PR
```

## Cross-cutting track — eval evolution (CIP metrics)

Runs alongside every rung; owned by `archolith-bench-operational-model.md`.

```text
Standard metrics (today):    recall_at_k, precision_at_k, MRR, NDCG, latency_ms
Temporal/belief:             temporal_accuracy, stale_hit_rate, historical_context_preservation,
                             current_truth_suppression_accuracy
Control rails:               productive_touch_rate, stale_heat_leak, retrieval_entropy
CIP metrics (new, phase in): Context Compression Ratio, Decision Accuracy per Retrieved Token,
                             Explanation Completeness, Provenance Fidelity, Belief Consistency,
                             Contradiction Detection Rate, Cognitive Cost
```

Add at least one CIP metric (Decision Accuracy per Retrieved Token is the
headline) by the time R7 lands, so the "decision quality per token" thesis is
measured, not just asserted.

## Suggested near-term order

> **Superseded 2026-08-09.** The order below was written before the bench ran. It sequences the
> read-side stack that then landed neutral-to-negative, and following it now walks into known-dead
> work. It is kept because the dependency reasoning inside it is still correct *if* a rung is ever
> reopened on new headroom — not because it describes what to build next.

```text
HISTORICAL — do not execute as written:
1. R0  (observability) — unblocks everything
2. R1  (hybrid + source-aware priors) — biggest near-term retrieval win
3. R3  (belief buckets) — parallel; extends existing belief.py
4. R4 -> R5 -> R6 -> R7 -> R7.5 (oracle pipeline to the killer baseline + ablation matrix)
5. R8 -> R9 (rails + write boundary)
6. then bench-gate R10/R11 (use the R7.5 matrix for the go/no-go); only then start Phase rungs
```

### What to build next (current)

The active arc is write-time consolidation, and it has no rungs in this ladder yet — its design
docs sit in `.agent/plans/backlog/` (`aggregation-as-consolidation.md`, `quantstate-agent-counter.md`,
`event-fold-view-architecture.md`) with statuses frozen at 2026-07-02, while the code shipped and
benched at 0.910. **That gap is open work on this document, not a claim that the arc is unplanned.**
Until it is closed, treat the ledger and those three docs as the authority on write-side order, and
this ladder as the authority on read-side order only.

Concrete near-term items that are already evidence-backed:

```text
1. Give the write-side arc rungs here (D0 retrieval-entropy, D1 QuantState, Event -> Fold -> View,
   agent-experiential counters), each with its code surface and its KU78 metric.
2. Decide whether those three design docs stay in plans/backlog/ or move up — "backlog" now
   understates them. Direction call, not a filing call.
3. Camera/possessive alias binding (v6 miss 26bdc477) — from a non-benchmark panel, not the fixture.
4. Widen beyond knowledge-update before any external claim: 0.910 is one subset.
```

## Non-goals

```text
do not start R11 (amplification) before R7 exists and is benched
do not add reranker/heavy deps before R1/R7 transparent baselines are measured
do not let any rung's mechanism doc get re-litigated here; this doc owns ORDER,
  the research docs own the mechanisms
do not merge this with post-v1-todo (that owns the shipped system, not the
  research build-out)
```
