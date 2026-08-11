# menhir frontier — undone work, grouped into executable chunks

## Status

active — execution grouping of the OPEN work on branch
`claude/menhir-chain-handoff-doc-7iuat2`. Snapshot 2026-06-30. Reconciled 2026-07-04 — the
read-side bench verdicts are in (see the verdict note in "Reconciled state" below).

## What this owns

This doc groups the *undone* work into cohesive, right-sized chunks so we can
pick one up and finish it (with the test that earns it). It does **not** restate
mechanisms — each chunk points at the rung in
`menhir-research-execution-ladder.md` (rung detail) and the research doc that owns
the mechanism. Ownership boundaries unchanged:

```text
menhir-research-execution-ladder.md   per-rung goal/code/bench/status (the WHAT + ORDER)
post-v1-todo.md                       shipped-system bugs/ops/deferred (separate track)
THIS doc                              the GROUPING of open rungs into work units + test gate
```

## Reconciled state (why "wired" is not "done")

The oracle pipeline (R4–R7), the warden trio, the Chronostratum signal layer, the
R3 belief buckets, and the L4 institutional types are **all ported into `src` and
wired into production recall** (`recall_service._apply_frontier`; shipped defaults
**ALL** `frontier_*` ship **default-OFF** as of `config/settings.py` today, each
`MENHIR_FRONTIER_*`-overridable). The real-cosine semantic signal is fixed
(SemanticOracle consumes precomputed similarity).

> **Correction 2026-07-11 (code-reconciled).** The clause above originally read
> "shipped defaults `oracle_ranking/intent_lens/shadow=ON`, `warden_gate/belief_gate=OFF`." That is
> stale: per `config/settings.py`, **every** `frontier_*` flag now defaults **False** (the 2026-07-04
> read-side bench verdict in the section below flipped `oracle_ranking/intent_lens/shadow` OFF too, so
> the shipped recall path is byte-for-byte the pre-frontier `ScoringService`). See
> `.agent/default-off-features.md` for the full default-off lever inventory.

What is NOT done is **graduation**: most of this landed with unit tests + a small
dummy nDCG eval, but the discipline rule is *archolith-bench decides promotion, not
vibes*. Nothing here is merged to `main` (the live service installs from `main`), and
the bench A–E ladders that would justify flipping gates ON are still owed. So the
open work is mostly **earn-the-gate** plus three genuinely unbuilt rungs (R5, R9,
the L3/L4 proposer/brief).

### Bench verdict update (2026-07-04) — the read-side gates were benched; they lose

The earn-the-gate framing above ("graduate the bench → flip oracle defaults ON → merge") is
**overtaken by evidence.** The read-side ladder has now been RUN on real corpora:

- **R1 (Chunk A):** narrow win, does NOT graduate on the 23.8k-node prod clone — the source-aware
  floor lifts the headroom families (paraphrase recall@5 0.550→0.600, +0.015 overall) but can't
  clear a gate miscalibrated against an already-saturated `exact_string_recall` (graphiti's
  internal RRF makes `enable_bm25` largely redundant). `hybrid_alpha` left unset.
  (`archolith-bench/.agent/benchmark-notes/r1-dummy-gold-run.md`.)
- **R6/R7 oracle stack:** LOSES on LongMemEval — node-only 0.400 > sem+temporal 0.367 > full stack
  0.333; every read-time lever neutral-to-negative, the EvidenceAnchorWarden zeroes anecdotal
  questions. (`archolith-bench/.agent/benchmark-notes/lme-score-campaign.md`.)
- **Chunk B (real semantic signal):** effectively DONE — the LME campaign runs against real menhir
  recall with live graphiti embeddings, so the bench-vs-production semantic gap is closed.
- **CI (Chunk A):** LANDED 2026-07-04 — `archolith-bench/.github/workflows/ci.yml` runs the
  offline suite on py3.11/3.12 (siblings installed from public GitHub). Top infra gap closed.

Consequence: the on-by-default oracle rungs (Chunks A/D/H) are **not** "graduate to turn on"
anymore — the honest bench result is already in, and the live direction moved to **write-side
aggregation / consolidation** (Track W in `.agent/research/menhir-research-execution-ladder.md`;
historical thesis in `.agent/archive/plans/aggregation-as-consolidation.md`). The shipped-gated stack is
unaffected (behavior-neutral, default-off/shadow). Re-opening R1/R2 means new bench headroom (a
recalibrated gate, a real facet fixture), not more read-time ranking.

---

## Chunk B — Honest semantic signal end-to-end *(prerequisite, small)* — DONE 2026-07-04

**Status: DONE 2026-07-04** — the LongMemEval campaign runs the full stack against real menhir
recall with live graphiti embeddings, so the bench/production semantic gap is closed. (The bench
*oracle-ablation* harness may still default to the lexical stub for offline unit runs, but the
graduation-relevant campaign is on real cosine.)

**Why first:** it is the cheapest unblock and bench-gate condition #1 ("the semantic
scorer is real") for every oracle rung. Production recall already injects real
cosine into the SemanticOracle; the bench/ablation still defaults to the lexical
stub, so bench numbers don't reflect production.

- Inject the real embedder into the bench oracle ladder + ablation (R4/R6/R7 owed
  "real embedder injected").
- Confirm combiner + ablation run on real cosine, not `LexicalSemanticScorer`.

**Test:** unit (scorer injection wiring) + re-run the oracle ablation with the real
embedder and diff vs the lexical-stub baseline (sanity, not promotion).
**Rungs:** R4, R6, R7 (the shared "real embedder" debt).

## Chunk A — Bench graduation + CI (the gating debt) *(highest value)*

**Why:** turns the already-built, already-wired stack from "shipped off by a dummy
eval" into "promotable by the real ladder." This is what lets the gates flip ON and
the branch become merge-worthy.

- **R0:** bench consumes the inline `RetrievalTrace` for the R1 A–E ladder.
- **CI prerequisite: DONE 2026-07-04** — archolith-bench runs in CI
  (`.github/workflows/ci.yml`, py3.11/3.12, siblings from public GitHub). Top infra gap closed.
- **R1:** bench ladder A–E + live-graphiti scale check + `hybrid_alpha` tuning. **RAN 2026-07-04**
  on the real dummy corpus — narrow win, does NOT graduate (gate-calibration artifact; see the
  verdict note above). Recalibrate the gate + fix the symbol/scope families before the
  `hybrid_alpha` call.
- **R3:** graduation on confirmed labels (belief-gate stays OFF until this passes).
- **R7:** weight calibration on a real labeled corpus; meet the 5-condition
  promotion gate (real scorer / stronger-stale-semantic / dup stale evidence /
  scoped depth ≥ k / no recall@5 loss).
- **Cross-cutting:** add the headline CIP metric *Decision Accuracy per Retrieved
  Token* by the time R7 graduates.

**Test:** archolith-bench fixtures **are** the test (A–E ladders per rung + R7.5
ablation matrix + the fixture validator). Promotion is bench-decided.
**Rungs:** R0, R1, R3, R7, R7.5, eval-evolution track. **Depends on:** Chunk B.

## Chunk D — Control rails + write boundary *(R8 → R9)*

**Why:** the "decide" and "write" altitudes above the killer baseline. R8 guards are
BUILT in domain/bench (default-off); the rung is production wiring. R9 is
consolidation, not greenfield — name the scattered write ops behind one Mutator and
add the invariant.

- **R8:** wire SelfReinforcementGuard (Guards 1–7) into the live path behind a flag.
- **R9:** `services/memory_mutator.py` — single write boundary over the existing
  scattered ops (promote_candidate / decay / conflict-resolve / lifecycle) + the
  enforced **no-write-in-evaluate** assertion.

**Test:** control-rails ladder A–F bench (R8) + write-boundary invariant unit tests
(oracles never mutate during `evaluate`; ranking determinism unchanged by mutation).
**Rungs:** R8 (depends R7), R9 (depends R7, R8).

## Chunk E — L3/L4 semantic overlay *(largest net-new; decided, bench-first)*

**Why:** the one part of the SOS direction with no rung yet. The C→A→B hybrid is
**decided** (`docs/roadmap/l3l4-hybrid-sketch.md`); the governance substrate is
largely already built (CANDIDATE review tier + approve/reject + conflict/decay). L4
institutional **types** + first-class `:Evidence` node already LANDED in
`domain/artifacts.py` (pure-domain/bench, production wiring gated).

- **The v0 slice plan is written and mostly built, not "not yet authored" as this line
  previously said** (status note 2026-08-08, curator audit): `.agent/archive/plans/l4-artifact-loop-v0.md`
  exists — commits 1-5 DONE (bench, 28 L4 tests green) and commit 6 BUILT + logic-checked
  (menhir port: `domain/artifacts.py`, `artifact_repository.py`, `artifact_service.py`,
  confirmed present in `src/menhir`), live-verified per `.agent/plans/l4-commit6-live-verification.md`.
  Decision/Failure/Incident → evidence → CANDIDATE/TRUSTED → R9-lite writer → MemoryOracle →
  ColdStartBrief v0. Remaining scope for this chunk is production graph wiring (gated) and the
  LLM semantic-node proposer / L3 types below, not the v0 slice itself.
- Then: LLM semantic-node proposer (another candidate emitter); L3 semantic types
  (capability/policy/constraint/invariant — none exist today); ColdStartOracle /
  Brief / Context Engine.

**Test:** failed-approach-avoidance benchmark (per the v0 plan); CANDIDATE-tier
review + promotion invariants.
**Rungs:** the unsequenced GAP track. **Depends on:** R3, R9 (for the writer).

## Chunk F — Facet candidate generation graduation *(R2, bench-gated, parallelizable)*

**Why:** R2 is bench-first and HYBRID mode recovers recall on the draft fixture
(0.28→0.83), proving the bottleneck is facet *extraction*, not the engine. No menhir
production change until F graduates on the real setup. `CandidateSource.FACET` is a
reserved enum.

- Real deterministic facets (Layer-2 / Git), real extraction model, real embedder,
  ctharvey's hardened fixture; graduate ladder F; then wire `CandidateSource.FACET`.
- **UPDATE 2026-07-05 — real embedder DONE, F graduates.** Swapped a real OpenAI embedder
  (`run_facet_bench.py --embedder openai`) into conditions B/C/E: F still **graduates gold +
  hybrid** on the draft fixture (wrong_scope 0.07 vs 0.38-0.40 baselines, <=0.05 recall loss) —
  the positive counterpoint to R1's neutral-to-negative floor.
  See `archolith-bench .agent/benchmark-notes/facet-r2-real-embedder-run.md`.
- **UPDATE 2026-07-05b — "real derived structural facets" DECOMPOSED.** Symbol facets are
  text-improvable (added snake/SCREAMING_SNAKE extractor rules -> symbol recall 0.11->0.55,
  extracted-mode F 0.275->0.425), but **file facets have recall 0.00 — the gold paths are not in
  the prose**, so they require the code graph's `ANCHORED_TO` edges, not a better regex. Extracted
  mode still can't graduate without them; hybrid's gold-structural stand-in is the CORRECT model
  for graph-anchored facts. So the remaining owed work is **production `ANCHORED_TO` coverage**
  (graph-gated) + ctharvey's hardened fixture (Risk #1), then wire `CandidateSource.FACET`.
  See `archolith-bench .agent/benchmark-notes/facet-r2-structural-facet-decomposition.md`.
- **UPDATE 2026-07-05c — ANCHORED_TO coverage MEASURED (live prod-clone).** ANCHORED_TO gives
  memories their exact file facets from the graph, but **only 24.5% of memories are anchored**
  (1300/5314; anchored ones avg 9 files, 750 with >=3; the other 75.5% carry none). So
  `CandidateSource.FACET` is a **bounded win** — it helps the ~1/4 code-anchored slice (where the
  gold-structural stand-in is realistic), not the whole corpus. The lever to grow it is
  **ingest-time anchoring coverage**, not the engine/extractor. Decision owed: wire FACET for the
  anchored slice now (bounded but real) and/or invest in raising anchoring at ingest.

**Test:** facet ladder A–F × {gold, extracted} bench (graduation gate).
**Rungs:** R2. **Depends on:** R1 (and Chunk B's real embedder).

## Chunk C — CostAwareOracleScheduler *(R5, optimization, deferrable)*

**Why:** R5 is the only un-started oracle-pipeline rung and is an *optimization*, not
a correctness requirement (R6 runs on the R4 executor without it). Do it when oracle
tail latency becomes the constraint.

- `services/oracle_scheduler.py`: static cost classes, bounded lanes,
  longest-runner-first by p95, runtime telemetry, deterministic reduction.

**Test:** tail-latency fixtures (oracle_p95/tail_wait/parallel_speedup/timeout_rate)
+ unit (deterministic reduction order). **Rungs:** R5. **Depends on:** R4.

## Chunk G — Phase rungs *(future; on top of D)*

Experience memory (P4), background cognition / PainScan / consolidation / skill-hook
promotion (P5), cognitive replay (PR), model adapters (PA). All depend on R3 + R9, so
they sit on top of Chunk D and are not startable yet. Park until D lands.

## Chunk H — Optional / bench-gated last *(R10, R11)*

R10 CrossEncoderRerankOracle (parked-until-needed) and R11 OracleAmplifiedRetrieval
(**bench-gated — reject unless it beats the R7 killer baseline**). Use the R7.5
ablation matrix for the go/no-go. Do not start R11 before R7 graduates.

---

## Recommended order

```text
1. Chunk B   real semantic signal in the bench         (small unblock)
2. Chunk A   bench graduation + CI                       (earns the gates / merge)
3. Chunk D   R8 rails -> R9 write boundary
4. Chunk E   L3/L4 overlay (v0 slice DONE per l4-artifact-loop-v0.md, archived; production
             graph wiring + LLM proposer/L3 types remain)
   Chunk F   facet graduation (parallelizable with D/E; needs B)
5. Chunk C   R5 scheduler (when latency bites)
6. Chunk G   phase rungs (after D)
7. Chunk H   R10/R11 (optional, after A's R7 graduation)
```

## Non-goals

```text
- do not merge frontier -> main until Chunk A graduates the on-by-default portions
- do not restate rung mechanisms here (the ladder + research docs own them)
- do not start R11 before R7 is benched; do not wire facet before R2 graduates
```
