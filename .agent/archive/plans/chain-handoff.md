# Chain handoff — menhir + archolith-bench (state as of 2026-06-29, now historical)

> **ARCHIVED — historical context only.** This handoff's title said "(current state)" as of
> 2026-06-29; it is not current. `.agent/README.md` links here explicitly as historical
> frontier/oracle context — for the live MVP board see
> `docs/roadmap/menhir-mvp-roadmap.md`'s RECONCILED STATUS banner instead.

**Last updated:** 2026-06-29 (**Intent-aware retrieval (IntentOracle): bench graduates + menhir Phases 1-3 port DONE — §7, §12.** `archolith_bench/intent/` (bench `1bf31fa`) + menhir `domain/query_intent.py`/`artifact_role.py`/`intent_affinity.py` + `IntentOracle` (32 tests pass; NOT in `default_oracles()` — gated). Only gated production integration remains. **L4 institutional-artifact loop v0 built — §6c.** Bench-first slice in `archolith_bench/l4/` (Evidence/Artifact models → ArtifactMutator → MemoryOracle → ColdStartBrief v0 → runner+fixture, 28 tests green) plus the menhir port `domain/artifacts.py` + `infrastructure/artifact_repository.py` + `services/artifact_service.py`/`memory_oracle_service.py` + schema indexes (logic-checked; live-graph walk owed per `.agent/plans/l4-commit6-live-verification.md`). Prior: Oracle Runtime weekend roadmap + extraction-model benchmark; R4-R7 oracle bench prototype + ablation + validator + embedder seam — `archolith_bench/oracle/`, §6b; weekend roadmap Days 1–3 drafted) **Doc-corpus restructure (2026-06-29):** the research notes were re-clustered into `docs/research/{direction,process,positioning,retrieval,schemas,belief-temporal,vision,archive}/` (git mv only, no content lost); every `docs/research/...` path in this handoff was updated to the new layout. See `docs/research/README.md` for the cluster index. **§7 oracle-pipeline scope-note corrected (2026-06-29):** the IntentOracle "not yet wired into `recall_service`" claim was stale — `_apply_frontier` now wires the oracle combiner + intent lens into production recall (defaults `oracle_ranking/intent_lens/shadow=ON`, `warden_gate/bm25=OFF`). Note updated to match code. **R8 SelfReinforcementGuard reconciliation (2026-06-29):** Guards 1-7 are ALL BUILT (domain/self_reinforcement.py + exhaustion.py + diversity.py + wardens in warden.py), default-off + bench-gated. Only CostAwareOracleScheduler + archolith-bench CE-willow graduation remain.
**Working branch (BOTH repos):** `claude/menhir-chain-handoff-doc-7iuat2`
**Status:** all work committed + pushed on that branch in both repos. This branch merged the prior
chain's `claude/menhir-r1-r2-handoff-augrkw` forward, so it carries the full R1/R2 + facet history
plus the newer work described below.

> **Purpose.** This is the single START-HERE doc for a fresh LLM chain picking up this work. Read this
> top-to-bottom first, then follow the "Doc map" (§10) for depth. It tells you what menhir is, what's
> been built, what's in flight, what's owed, and what NOT to re-litigate.

---

## 0. How to use this doc

1. Read §1–§5 for the mental model and current state (≈5 min).
2. If you're doing **retrieval / facet / bench** work, read §6 + §6a + §7 in full, then the bench package.
3. If you're doing **docs / research / architecture** work, read §3, §9, §10.
4. Before changing anything, read §8 (decisions not to re-litigate) and §2 (the hard constraints).
5. The authoritative, always-current owed-work list is `.agent/plans/deferred-verification.md`.

---

## 1. What menhir is

menhir is a **long-lived graph memory service** (Neo4j + Graphiti + LLM enrichment, exposed over MCP)
that is being grown into a **Semantic Operating System for software** / **Cognitive Infrastructure
Platform (CIP)**. The thesis, in one line:

> menhir is the system that manages what humans and AI agents *know* about software over time —
> preserving what code means, why it exists, how it evolved, and what was learned — on top of
> deterministic structural anchors, optimizing **decision quality per token**, not retrieval recall.

It attacks *cognitive debt / intent debt / memory debt*: source code is a lossy compression of intent.

**The architecture (from `docs/research/direction/semantic-operating-system.md` + `oracle-architecture.md`):**
a four-layer knowledge model with a hard structural-vs-semantic truth boundary —

```
Layer 4  Institutional Knowledge  (incidents, failed approaches, rationale, decisions)
Layer 3  Semantic Model           (capabilities, policies, constraints, invariants)   <- interpretive, evidence-backed
Layer 2  Structural Model         (symbols, types, deps, tests, git anchors)          <- deterministic, never LLM-derived
Layer 1  Source Code
```

Runtime stack: **Layers store knowledge → Oracles reason → Combiner synthesizes → Context Engine
packages → Mutators write.** Cold-start pipeline produces an evidence-first **Cold Start Brief**
(Known facts / Likely interpretations / Open questions / Risks / Evidence links / Context pack).

Spine rule: **structural truth is deterministic and never depends on an LLM; semantic truth may start
as an AI hypothesis but must carry provenance/confidence/valid-time/supersession and must not silently
become fact.** Retrieval is evidence of attention, not truth.

---

## 2. The two repos, the branch, and the hard constraints

| Repo | Role | Local path |
|---|---|---|
| `archolith/menhir` | the product/system: implementation, architecture, research notes | `/home/user/menhir` (`src/menhir/`) |
| `ctharvey/archolith-bench` | the falsification harness: fixtures, baselines, A/B runners, results | `/home/user/archolith-bench` (`archolith_bench/`) |

**Rule (do not blur):** *menhir proposes and implements behavior; archolith-bench proves or
falsifies it.* No research claim graduates `speculative → supported-by-eval` without a bench artifact.

**Both repos use the same working branch:** `claude/menhir-chain-handoff-doc-7iuat2` (the prior
chain's `claude/menhir-r1-r2-handoff-augrkw` was merged forward into it). Develop there; push there;
never push elsewhere without explicit permission.

**Hard remote-session constraints (why "nothing is verified live"):**
1. **menhir's pytest can't run here** — the private `cth-mcp-framework` dep isn't installable and
   `tests/conftest.py` imports the full infra chain. menhir code lands compile-/logic-checked only.
2. **`graphiti_core` isn't importable** — anything touching live search / RRF score scales is
   unverifiable here.
3. archolith-bench's `archolith_bench/facet/` package is **pure stdlib**, so it *does* run and test
   here (57 tests, re-confirmed green this refresh). The rest of the bench needs `httpx`/optional deps
   not present in remote sessions — in particular the **`extraction-bench`** suite (§6a) hits live
   provider APIs, so its model-comparison numbers were gathered live, not reproducible in a remote
   session.

So: the facet bench work is fully runnable/tested remotely; menhir production code and live-graphiti
behavior are not. Owed checks live in `deferred-verification.md`.

---

## 3. The mental model: research ladder + SOS, and how they map

Two framings of the same system. **The execution ladder is the build order; the SOS docs are the
architecture it serves.** Owner: `.agent/plans/menhir-research-execution-ladder.md` (read it).

```
R0  Retrieval + oracle observability (traces)              foundation, unblocks all
R1  Hybrid candidate generation + source-aware priors      LANDED (see §5)
R2  Facet candidate generation                             IN PROGRESS, bench-first (see §6)
R3  Belief buckets + currentness policy                    planned (belief.py substrate exists)
R4  RetrievalOracle interface + thread-safe executor       planned
R5  CostAwareOracleScheduler                               planned
R6  Cheap oracles (Semantic/Scope/Temporal/Structure)      planned
R7  One-pass OracleCombiner (log-space role logits)        planned  <- THE KILLER BASELINE
R8  SelfReinforcementGuard (anti-spiral rails)             BUILT (Guards 1-7, default-off bench-gated)
R9  MemoryMutator write boundary                           planned
R10 CrossEncoderRerankOracle                               parked-until-needed
R11 OracleAmplifiedRetrieval (iterative)                   BENCH-GATED: reject unless it beats R7
P4/P5/PR/PA  experience / background cognition / replay / model adapters
```

**Discipline (do not violate):** every rung lands a transparent baseline before heavy deps; the bench
decides graduation, not vibes; control rails must not change ranking nondeterministically; retrieval
alone must not promote truth/currentness; iterative amplification (R11) must beat the one-pass
combiner (R7) to ship.

**SOS ↔ ladder mapping** (full version in the ladder's "SOS direction reconciliation" section):
R1/R2 = Layer-2/3 candidate generation; R3 = temporal/belief (Program C); R4–R7 = the oracle reasoning
layer (R7 combiner output = the retrieval-shaped **OraclePacket** — kernel of, not, the **ColdStartBrief**;
see `docs/research/retrieval/oracle-runtime-interfaces.md`); R8/R9 = observe/decide/write boundary + Mutator.
**GAP, needs ctharvey to sequence:** the SOS Layer-3/Layer-4 *semantic overlay* (Programs B & D —
LLM-proposed Capability/Policy nodes, evidence-as-first-class entity, knowledge-promotion lifecycle)
has **no rung yet**. It's the highest-scope-risk part; don't silently invent rungs for it.
**Update (this refresh):** `docs/roadmap/weekend-oracle-runtime-roadmap.md` takes a first pass at
*specifying* (not building) this overlay — Oracle Runtime interfaces (R4–R7), a generic Layer-4
knowledge-artifact schema, and the Cold Start Brief spec. It is **design/spec work, still pending
ctharvey's sequencing into the ladder** — it does not retire the GAP or create a rung, it just sketches
the shape. See §5 and §7. **To choose an implementation:** `docs/roadmap/l3l4-overlay-sequencing-options.md`
compares five build strategies (capture-first / LLM-proposed review-gated / bench-first / reuse-shipped /
brief-driven) with a matrix + recommended hybrid — a proposal menu for ctharvey, not a decision.
**Update (this refresh — the GAP now has its first built slice):** the smallest safe slice of the
Layer-4 overlay is **built** (§6c) — institutional artifacts (Decision/Failure/Incident) backed by
first-class Evidence, a single-writer R9-lite mutator, a read-only MemoryOracle, and a ColdStartBrief v0.
It deliberately reuses the shipped CANDIDATE review tier + scope/conflict machinery rather than inventing
a parallel one (the "reuse-shipped" + "bench-first" + "capture-first" arms of the hybrid). It does **not**
retire the GAP: L3 capabilities/policies, the LLM proposer emitter, and a full ColdStartOracle remain
unsequenced and explicitly out of this slice. Plan + locked decisions: `.agent/plans/l4-artifact-loop-v0.md`.

---

## 4. What has shipped vs what's research build-out

- **Shipped v1 (M0–M7, 2026-03-18, 722+ tests):** Graphiti-backed ingestion, deferred enrichment,
  two-phase recall + scoring, lifecycle decay, conflict governance, MCP server (23 tools), circuit
  breakers, budget caps, embedding cache, the code-structure graph (`ingest_project`,
  `structural_anchoring`, `query_structure`), sage-wiki integration. This is a real system, not a
  scaffold. Owner: `.agent/memory-roadmap.md`; shipped-system TODOs: `.agent/post-v1-todo.md`.
- **Research build-out:** the R0–R11 ladder above, drawn from the `docs/research/` corpus. Kept
  strictly separate from shipped-system work.
- **Top infrastructure gap:** CI/CD. The whole bench-gated ladder assumes archolith-bench runs in CI;
  it doesn't yet. That's an R0 prerequisite.

---

## 5. Current state of THIS line of work

### R1 — landed (commit `e8da67d`)
Attributed hybrid candidate generation + a source-aware floor, **behind a default-off flag** (today's
recall behavior is unchanged until a caller opts in).
- `src/menhir/domain/retrieval_tuning.py` — `CandidateSource`, `SOURCE_PRIORS`, `FLOOR_EXEMPT_SOURCES`,
  `RetrievalTuningConfig`.
- `src/menhir/services/hybrid_retrieval.py` — `weighted_rrf` blends vector + BM25 on **rank, not raw
  score** (avoids the BM25/cosine scale mismatch).
- `scoring_service.py` floor is now **source-aware** (gates only VECTOR; BM25/facet/file exempt).
- `graphiti_client.py` — `search_ranked_by_method` added; `search_scored` unchanged + still default.
- `hybrid_alpha` ships at neutral `0.5` as a **seam, not a tuned value** (the bench tunes it).
- Owed: run the 3 R1 test files + full suite; confirm the score scale on live graphiti; bench ladder
  A–E; then set `hybrid_alpha`. All blocked on the remote constraints (§2) + needs R0 traces.

### R2 — in progress, **bench-first** (see §6 for depth)
The facet mechanism + benchmark live entirely in archolith-bench. **No menhir production change lands
until condition F (facet + meet-point) beats baselines on stale-hit / wrong-scope / support-sufficiency
without unacceptable recall loss, on a real fixture with real baselines.** R1 reserved the
`CandidateSource.FACET` seam for post-graduation wiring only — do not wire facet into production recall
as part of R2.

### Workspace coherence pass (commit `a1cebe8`)
After reading the full corpus this chain did a coherence pass: registered the two new SOS docs in the
research index + reconciled them with the ladder; swept the stale project name (`yawn_memory` /
`cth.mcp.memory` → `menhir`) across `.agent` docs (fixing dead paths in `verified-current-findings.md`);
bumped statuses to match reality (R1/R2 → in-progress; `facet-retrieval.md` /
`oracle-execution-and-performance.md` → supported-by-spike). See §9 for the naming history.

### Since the last handoff (this refresh — two new tracks)
While the R2 promotion decision sits blocked on a real embedder + live graph (§7), two pieces of work
landed that a fresh chain must know about:

1. **Oracle Runtime weekend roadmap** (menhir, `docs/roadmap/weekend-oracle-runtime-roadmap.md`).
   A short-term plan for the embedder-blocked window. Guiding principle: *don't tune retrieval around a
   missing component* — use the wait to build the architecture retrieval will feed (Oracle Runtime,
   Layer 4 knowledge model, Cold Start brief). Six priorities: (1) Oracle Runtime I/O interfaces
   (`OracleInput`/`OracleFinding`; **oracles observe & explain, they do not mutate**), (2) primitive vs
   composite oracle taxonomy, (3) Cold Start Brief spec, (4) a **generic** Layer-4 knowledge-artifact
   schema (store generic artifacts; let oracles interpret — *not* one table per memory type),
   (5) evidence-first context assembly (the Context Engine **packages**, it does not decide truth),
   (6) a facet-extraction improvement plan. Explicit non-goals: embedder selection, retrieval tuning,
   more benchmark cases, fixture rewrite, live-graph promotion. **This is a spec, not built code** —
   and it touches the §3 GAP, so it stays pending ctharvey's sequencing.

2. **Extraction-model benchmark** (archolith-bench — see §6a). The bench grew a real, runnable
   `extraction-bench` harness that replays menhir's actual 3-call extraction pipeline against any
   OpenAI-compatible model and measures speed / quality / cache-aware cost. It directly attacks R2's
   central open question ("**extracted facets fail**") and weekend Priority 6: the model that populates
   the graph is the engine extracted-mode facets depend on. Default blessed keepers are now
   `gpt-4.1-nano` + `qwen3-next-80b`.

3. **Oracle pipeline bench prototype** (archolith-bench — see §6b). The R4–R7 retrieval-oracle layer
   (the build-first part of the ladder's Track C) now has a pure-stdlib, deterministic bench prototype:
   the `RetrievalOracle`/`OracleResult`/`OraclePacket` interface, a deterministic executor, cheap
   oracles, and **both** the weighted (E) and log-space role-logit (F, the R7 killer baseline)
   combiners, with the A/E/F ladder + promotion gate. F ties E on the easy fixture but **GRADUATES on a
   harder real-history fixture** (`oracle_hard.json`) by suppressing a high-support stale item E keeps.

---

## 6. R2 facet work in depth (the active deliverable)

R2 tests whether **deterministic facet retrieval + meet-point reranking** improves recall behavior
(less stale / wrong-scope injection, more paraphrase stability) vs honest baselines, BEFORE adding any
production surface. Owner doc: `docs/research/retrieval/facet-retrieval.md`. Plan: `.agent/plans/r2-facet-candidate-generation.md`.

### The benchmark-local package: `archolith_bench/facet/`
Pure-Python, deterministic, explainable. (Lives in the BENCH repo, never in menhir `src/`.)
- `models.py` — `MemoryFacetSet`, `Memory`, `Query`, `FacetFixture`; the R2 facet vocabulary
  (`actor, object, operation, file, symbol, test, valid_time, learned_time, evidence_type, source_id,
  repo, project, namespace, belief_bucket`).
- `extractor.py` — `FacetExtractor`: cheap deterministic rules (regex/vocab, no LLM).
- `index.py` — `MemoryFacetIndex`: candidates by compatible facet **overlap**, not similarity.
- `reranker.py` — `MeetPointReranker`: `meet_score` (required-facet overlap + file/symbol/test +
  evidence/source + time-window − stale/superseded − wrong-scope) with a per-candidate **explanation
  trace**.
- `baselines.py` — BM25 (Okapi), a deterministic **lexical embedding STAND-IN** (`LexicalEmbeddingStub`,
  pluggable via the `EmbeddingScorer` protocol), `rrf_fuse`, `file_context_rank` (graph stand-in).
- `metrics.py` — recall@5, precision@5, MRR, NDCG, stale_hit_rate, wrong_scope_injection_rate,
  support_sufficiency, false_neighbor_rate, paraphrase_stability, latency.
- `runner.py` — the condition ladder A–F × {gold, extracted} facet modes + `evaluate_promotion_gate`.
- `validate.py` — fixture validator (see below).
- Run: `python scripts/run_facet_bench.py [fixture]` → writes `results/facet_run.json` (gitignored).
- Tests: `tests/test_facet_*.py` (57 tests, ruff clean).

**Conditions:** A BM25 · B embedding(stand-in) · C BM25+embedding(RRF) · D graph/file-context(stand-in)
· E facet+embedding rerank · F facet+meet-point rerank. (G +BeliefLayer gates = later.)
**Two facet modes, kept separate:** `gold` (hand-authored facets — "do facets help if correct?") vs
`extracted` (FacetExtractor — "can a cheap extractor recover enough?"). Correctness is always judged
against the GOLD corpus; only the retriever's view changes.

### The fixtures
- `fixtures/facet_demo.json` — 10/6 smoke/illustration fixture (NOT the benchmark).
- `fixtures/facet_r2_draft.json` — **DRAFT 52-memory / 20-query fixture**, grounded in REAL
  menhir+archolith history (the R1 floor change superseding the old cosine floor; the
  cth.mcp.memory→yawn_memory→menhir rename chain; the documented CE-willow belief drift E1–E5 incl. the
  anergic "patch fixed it" distractor; real files/symbols/bugs; cross-repo collisions). It has been
  through one **adversarial hardening pass** (all 7 validator findings resolved: q01/q02 made genuine
  2-support, q07 demoted, q13/q15 given real stale & wrong-repo distractors, q11/q14 marked
  accepted-easy). **Still a DRAFT — needs ctharvey's judgment + gold-support-ID confirmation.**

### The validator (`archolith_bench/facet/validate.py`)
Makes bad fixtures hard to write; it flags, it does NOT design the benchmark. **Errors** (can't run):
missing/dup support IDs, bad belief buckets, empty text. **Quality warnings** (too-clean guards): no
stale/rename/wrong-repo/vague distractor, under-spec counts, + four hardening heuristics —
(1) uncontested current queries (named, with the missing distractor family), (2) fake-paraphrase
(query is a near-verbatim copy of its support, ≥85% content-token overlap), (3) facet-less vague check
(an embedding-should-win query must carry no repo/file/symbol/valid_time facet), (4) multi-support
dependency (one support facet-dominates, or two supports are near-duplicate text → one may suffice).
Run it on every fixture before trusting a result.

### What the numbers say (DRAFT, gold mode — and the big caveat)
On the hardened draft: BM25 is a **strong** baseline (R@5 ≈ 0.85). Condition **F (facet+meet-point)**
trades ~0.025 recall to cut **wrong-scope 0.40 → 0.07** and **stale 0.27 → 0.15** — the win is exactly
on the targeted metrics, and the gold-mode gate **graduates**. In **extracted** mode F collapses
(recall loss ~0.6, gate fails) — the cheap extractor can't recover facets from real prose. That gap is
the honest, correct signal: gold facets help; the next real problem is extraction.
**Hybrid mode (Priority 6 — built) closes that gap:** reading the deterministic facets from
structure/Git (gold stand-in) and extracting only the interpretive facets takes F's recall **0.28 → 0.83**
(gold 0.85) and re-graduates, with stale/wrong-scope at gold levels — confirming the bottleneck is
structural-facet *extraction*, not the engine (`docs/research/retrieval/facet-extraction-plan.md`).
**CAVEAT: these numbers use the lexical embedding STAND-IN + DRAFT fixture — a harness sanity check,
NOT a promotion decision.** Report: `archolith-bench/.agent/benchmark-notes/facet-r2-demo-run.md`.

## 6a. Extraction-model benchmark (archolith-bench — the second active bench track)

The "extracted facets fail" finding (§6) points at the **extraction model** — menhir's backend turns
each episode into graph structure with a **multi-call LLM pipeline** (extract entities → resolve/dedupe
→ extract edges/facts), hundreds of thousands of small structured-JSON calls at scale. Choosing that
model is a real speed/cost/quality decision, and the bench now measures it.

- **Harness:** `archolith-bench extraction-bench` (`archolith_bench/extraction_sim.py`) replays the real
  3-call pipeline over a growing-context corpus against any OpenAI-compatible model; reports per-call
  p50/p95, episode throughput, entity/fact recall vs gold, valid-JSON rate, measured cache-hit rate,
  and cache-aware `$/1k episodes`. **Needs `httpx` + live API keys → not runnable in remote sessions**
  (§2); numbers were gathered live. OpenRouter is wired (`OPENROUTER_API_KEY`) for one-key access to
  many open-weight models — its `:free` tier is too rate-limited to benchmark, use paid routes.
- **Owner docs:** `EXTRACTION_MODELS.md` (the full measured comparison + reproduce instructions) and
  the one-page site `extraction-models.html`.
- **Headline findings (June 2026, live):** extraction quality is ~a **tie** among capable small models
  (it's a structured, low-creativity task), so the decision is **speed & cost**, not accuracy.
  - **Best value:** `gpt-4.1-nano` — ~0.5 s/call, ~$0.10/1k ep, native `json_schema`.
  - **Best quality:** `gemini-3.1-flash-lite` — best fact recall (0.90), nano-class speed, but ~2× cost.
  - **Best open-weight:** `qwen3-next-80b` (via OpenRouter) — 0.85 fact recall, 100% JSON, $0.22/1k —
    gemini-3.1 tier without Google. **One of the two default blessed keepers** (with `gpt-4.1-nano`).
  - **Cheapest at scale:** `deepseek-v4-flash` — caching (~$0.0028/1M cached in) makes it cheapest;
    needs `json_object` + schema-in-prompt (it 400s on `json_schema`).
  - **Avoid:** reasoning models (gpt-5-nano/mini, default Gemini-Flash thinking — disable thinking if
    forced); and Cerebras `gpt-oss-120b` — *fastest measured* (0.30 s) but 0.40 fact recall, so
    disqualified for graph memory (edge/fact extraction *is* the job). `gpt-oss-120b`'s weak facts
    reproduced cross-provider (0.45 on OpenRouter), so it's a model trait, not a provider artifact.
- **Why it matters for R2:** gold facets help but extracted facets collapse; a stronger extraction model
  (and the weekend Priority-6 extractor plan) is the realistic path to making extracted-mode viable
  without changing the retrieval engine. This is the bench-side complement to the menhir-side roadmap.

## 6b. Oracle pipeline bench prototype (archolith-bench — the R4-R7 build-first track)

The ladder's Track C (oracle pipeline) has its build-first rungs prototyped in the bench, same
bench-first discipline as facet: `archolith_bench/oracle/`.

- **Interface (R4):** `QueryContext` / `CandidateMemory` / `OracleResult` / `OraclePacket` (immutable
  value objects), faithful to `docs/research/retrieval/oracle-amplified-retrieval.md`, plus a bounded,
  deterministic `OracleExecutor`.
- **Cheap oracles (R6):** Semantic (pluggable `SemanticScorer` — lexical stand-in by default, with a
  **real-embedder seam** so the whole ladder swaps to a real model in one line), Structure (file/symbol/test overlap — recovers
  buried-by-embedding memories), Scope (repo/branch/project/namespace — wrong-scope guard), Temporal
  (valid/invalid/as-of × intent), Evidence (provenance strength).
- **Combiners (R7):** `WeightedOracleCombiner` (ladder E, naive sum) and `LogSpaceOracleCombiner`
  (ladder F — role-specific log-space logits, contradiction as `D=λ·q^γ`, source-family independence
  + caps, missing≠falsity). The R7 OraclePacket (NOT the ColdStartBrief — see oracle-runtime-interfaces.md).
- **Ladder + gate:** `A_semantic / E_weighted / F_logspace` + promotion gate; 38 tests, ruff clean,
  runs in remote sessions (pure stdlib). Run: `python scripts/run_oracle_bench.py`.
- **Headline finding:** *a linear combiner (E) lets relevance buy back currentness; role-separated
  logits (F) don't.* That failure mode — "very relevant but stale" buying back into top-k — is exactly
  what menhir exists to prevent, and is why R7 is not just E with extra steps.
- **Structured Temporal oracle (this pass):** the Temporal oracle is no longer a stale/not-stale boolean
  — it classifies CURRENT / SUPERSEDED / NOT_YET_VALID / **NOT_YET_KNOWN (anachronism — a memory learned
  after the query's as-of point; temporal leakage)** / UNKNOWN, with graded directness (explicit
  timestamp 1.0 vs belief-bucket 0.6). Anachronism + not-yet-valid guards are new and independently
  valuable.
- **Results (lexical stand-in, harness sanity — NOT headline numbers):** F ties E on the easy demo. On
  `oracle_hard.json` **F graduates** (edge now on wrong-scope, no recall loss). On `oracle_correlated.json`
  (five stale echoes of one belief) **E now TIES F** — and that is the key honest finding: *structuring
  the temporal oracle lifted the linear baseline*, so part of the earlier "F beats E" gap was
  oracle-quality, not combiner architecture. A well-evidenced current truth is **not** auto-buried by a
  chorus even under E.
- **The deeper finding (matters more than F):** *oracle quality and combiner quality are not
  independent.* A better temporal oracle lifts **every** downstream combiner and shrinks the combiner
  gap. So the contribution reframes: retrieval is a **benchmarkable decomposition** (semantic →
  specialized oracles → combiner → ranking), not one ranking formula — and a future neural combiner can
  be swapped in without losing the oracle layer or the eval.
- **R7.5 oracle ablation (built — `run_ablation` / `--ablate`):** contribution matrix on `oracle_hard`
  shows the gains are overwhelmingly in the **oracle layer** — Temporal owns stale/current-truth, Scope
  owns wrong-scope, Structure owns recall; the combiner choice E→F is a *smaller* move than any single
  strong oracle.
- **Guardrail (`oracle/validate.py`):** a fixture validator flags errors (dangling/stale-gold/
  anachronistic-gold/date-order) + silliness (uncontested, fake-paraphrase, **thin-scope < k**,
  no-stale, no-scope-var); auto-runs before every ladder run. It caught real issues in our own fixtures.
- **Sharpened verdict (this pass — F looks *less* impressive, and that's the finding):** the cap is
  **dormant** (one oracle per family ⇒ it never binds, so it was never the lever), and on a
  validator-clean deep scope fixture (`oracle_scope.json`) **E and F converge**. In fact the only
  fixtures where F beat E on wrong-scope are the ones the validator flags as **thin-scope artifacts**.
  Net: F's edges over a well-tuned weighted sum are small + boundary/thin-corpus driven. The robust,
  defensible contribution is **the decomposition + instruments** (independently benchmarkable oracles,
  the R7.5 ablation, the validator) — *not* the combiner formula; a neural combiner could replace F
  without disturbing them. **Do NOT claim** real-embedding performance, calibrated weights, latency
  viability, general benchmark superiority, or production readiness. **R11 remains blocked.** Details:
  ladder R7/R7.5 + `archolith-bench/.agent/benchmark-notes/oracle-r4-r7-demo-run.md`.

## 6c. L4 institutional-artifact loop v0 (the newest track — first built slice of the §3 GAP)

The smallest safe slice of the Layer-4 overlay, built bench-first then ported to menhir. It answers
one task-shaped question: *does surfacing a known Failure (and its corrective Decision) stop an agent
repeating a dead end?* Plan + locked decisions D1–D5 + the 9 invariants: `.agent/plans/l4-artifact-loop-v0.md`.

**Bench side (`archolith_bench/l4/`, pure-stdlib, 28 tests green, runs remotely):**
- `models.py` — `Evidence` (first-class value object) + `Artifact` (Decision/Failure/Incident, status
  CANDIDATE/TRUSTED/HISTORICAL, source HUMAN/LLM) + `ArtifactFixture`.
- `mutator.py` — `ArtifactMutator` (R9-lite single writer). The invariants are **structural**: `create`
  has no status param, so an LLM artifact can't be born TRUSTED (inv. 4) and a human one is TRUSTED only
  with evidence (inv. 5); `promote` refuses without evidence (inv. 3); `supersede` marks HISTORICAL and
  never deletes (inv. 7).
- `memory_oracle.py` — read-only retrieval (anchor overlap strong + topic token overlap weak); returns
  history/candidates with status intact (never writes — inv. 1/2).
- `brief.py` — `ColdStartBriefV0`: buckets by epistemic status so a CANDIDATE can never be presented as a
  fact and a superseded artifact reads stale (inv. 8); deterministic `recommended_first_action` (avoid
  the top Failure, prefer its corrective Decision).
- `runner.py` + `fixtures/l4_failure_demo.json` + `scripts/run_l4_bench.py` — `without_l4` vs `with_l4`
  on five metrics. **Headline reproduced:** `failed_approach_surfaced`, `first_action_quality`,
  `stale_or_conflict_flagged` flip 0→1 with L4 while `evidence_present` holds at 1 (invariant audit).

**menhir port (logic-checked in-sandbox; live-graph walk owed — see §7):**
- `src/menhir/domain/artifacts.py` — the same types + Evidence + the R9-lite trust policy as pure
  functions (`decide_status`, `can_promote`, `scope_for_status`) mapped onto existing `NodeScope`
  (TRUSTED→PERSISTENT, CANDIDATE→review tier, HISTORICAL→PERSISTENT+superseded).
- `src/menhir/infrastructure/artifact_repository.py` — the single Cypher writer, modeled on the
  CANDIDATE direct-write path: `MERGE :Entity` on `artifact_id` (idempotent, reuses SEMANTIC), **first-
  class `:Evidence` nodes via `(a)-[:SUPPORTED_BY]->(e)`** (the one genuinely new structure), fail-closed
  `promote` (an `EXISTS{}` guard refuses evidence-less trust at the write boundary, inv. 3), `supersede`
  (`SUPERSEDES` edge + HISTORICAL, never deletes, inv. 7), `find/fetch`. Wired as `MemoryGraphAdapter`
  delegates.
- `src/menhir/services/artifact_service.py` (R9-lite facade, async, mirrors `CandidateService`) +
  `services/memory_oracle_service.py` (read-only, **same ranking as the bench oracle**).
- `src/menhir/infrastructure/schema.py` — `:Evidence` registered + `artifact_id`/`is_artifact`/status
  indexes added to the bootstrap (Evidence deliberately NOT in `MEMORY_NODE_LABELS`, so it doesn't
  inherit the full Entity backfill).
- **Local confidence (no live graph):** `tests/test_artifacts_domain.py`, `test_artifact_repository.py`
  (Cypher-capture stub), `test_artifact_service.py` (fake adapter), and `test_l4_artifact_loop_integration.py`
  (the whole service stack over an in-memory graph, feeding the bench corpus → **bench-parity statuses
  confirmed**). All exercised in-sandbox via direct-import probes (full pytest needs httpx/graphiti, §2).

**Reuse vs new (the honest inventory):** governance substrate (CANDIDATE tier, scope/conflict/decay) is
**reused**, not rebuilt; net-new is the artifact *types*, the first-class `:Evidence` node, and the
brief's epistemic bucketing. Deferred by design: the LLM proposer emitter, a full ColdStartOracle, and
L3 capabilities/policies.

---

## 7. Owed work (the real "what's next") → `deferred-verification.md` is authoritative

**R1:** run the test files + full suite (watch 2 pre-existing NaN-scoring failures — not ours); confirm
the RRF/cosine score scale vs the `0.15` floor on live graphiti; bench ladder A–E; then set
`hybrid_alpha`. Blocked on remote constraints + needs R0 traces.

**R2 (to make the gate count):**
1. **Pair-author / harden the real fixture with ctharvey** (the draft is a strong start; confirm gold
   support IDs, harden the 2 accepted-easy queries if desired).
2. **Inject a real `EmbeddingScorer`** (conditions B/C/E) and, if available, the **live menhir graph
   retriever** (condition D) — replace the stand-ins.
3. **Re-run** the ladder; report all metrics together; apply the promotion gate.
4. **Only if F graduates on the real setup**, plan the production-integration rung (wire
   `CandidateSource.FACET` + its prior/floor exemption — the R1 seam). Until then, **do not touch
   production recall.**

**Oracle Runtime (weekend roadmap, embedder-blocked window — `docs/roadmap/weekend-oracle-runtime-roadmap.md`):**
spec-only deliverables, each pending ctharvey's sequencing before any build (§3 GAP, §8): (1) the
`OracleInput`/`OracleFinding` interface + observe-not-mutate rule **— DRAFTED this refresh in
`docs/research/retrieval/oracle-runtime-interfaces.md`** (reconciles the retrieval-level RetrievalOracle/combiner
[R4–R7] with the task-level runtime; composite/Cold-Start-Brief layer stays in the GAP); (2) primitive/composite oracle
taxonomy; (3) Cold Start Brief schema **— DRAFTED `docs/research/schemas/cold-start-brief.md`**; (4) generic
Layer-4 knowledge-artifact schema **— DRAFTED `docs/research/schemas/layer4-knowledge-artifacts.md`**;
(5) evidence-first context-assembly + provenance rules **— DRAFTED (context-pack provenance section of
cold-start-brief.md)**; (6) the facet-extraction improvement plan **— DRAFTED
`docs/research/retrieval/facet-extraction-plan.md`**. **Day 3 (integration) DRAFTED:
`docs/roadmap/oracle-integration-plan.md`** (buildable-now vs gated map + Context Engine sketch + first
ColdStartBrief benchmark sketch + a written, unfiled issue list). All schema/plan specs are spec-only and
the L3/L4 GAP items stay pending ctharvey's sequencing. Do **not** spend this window on embedder
selection, retrieval tuning, more fixtures, or live-graph promotion.

**L4 artifact loop (§6c) — the live-graph walk:** the bench slice is green and the menhir port is
logic-checked, but commit 6 is the only graph-schema change and is **confirmed live at home**, commit by
commit, per **`.agent/plans/l4-commit6-live-verification.md`** — run the three new `test_artifact_*` files
under the full env, then walk the Cypher asserts against real Neo4j (artifact_* fields + scope on create,
`:Evidence` via `SUPPORTED_BY`, idempotent re-emit, fail-closed promote, supersede-not-delete, oracle
anchor ranking, end-to-end parity with `run_l4_bench.py`). **The one thing to confirm not assume:**
decay/recall coupling — trusted artifacts are PERSISTENT SEMANTIC nodes and inherit that machinery.
Deferred by design (do not build without sequencing): the LLM proposer emitter, a full ColdStartOracle,
L3 capabilities/policies, and the `DERIVED_FROM` provenance edge.

**Extraction model (`EXTRACTION_MODELS.md`, §6a):** open items — wire menhir's actual backend extraction
config to a blessed keeper (`gpt-4.1-nano` value / `qwen3-next-80b` open-weight); benchmark the still-
untested models (Mistral ministral, Llama-4 Scout, larger Gemma 4, Groq `gpt-oss-120b` direct); add CI
so `extraction-bench` + `facet` both run (the R0 prerequisite below covers both).

**Intent-aware retrieval (IntentOracle) — DESIGN + PLAN written, no code yet:** a deterministic
(no-LLM) component that answers *"which candidate best helps THIS task?"* — the dimension the
stack does not yet reason about (it has semantic/temporal/scope/evidence/structure but not task
intent). Design `docs/research/retrieval/intent-warden.md`; plan `.agent/plans/menhir-intent-oracle-plan.md`
(commits `8236d66`, `fc8cb2c`). **Decisions locked — do NOT re-litigate:**
- **Ships as an `IntentOracle` (RELEVANCE family), NOT a Warden — APPROVED by ctharvey** (design
  §9 #1 resolved; framing is locked, not just recommended). Pairing rule: an oracle earns
  a paired warden iff its dimension has a binary "must not enter current-truth context" line
  (scope/currentness/anchoring do; semantic/structure/**intent** don't). A wrong-role-for-task
  hit is *less helpful*, not *unsafe* — rank it down, never refuse. (A warden also physically
  can't promote to #1, which is the whole point.)
- **8 single-purpose intents over a data-driven matrix** (DEBUG_FAILURE, AVOID_REPEAT,
  EXPLAIN_DECISION, VERIFY_CURRENTNESS, EVIDENCE_LOOKUP, CHANGE_ANALYSIS, PLAN_NEXT_ACTION,
  UNDERSTAND_SYSTEM=default). Adding a task = a matrix row; adding an artifact kind = a column;
  no consumer code changes. That extensibility is *why* it's 8, not fewer.
- **Multiple hits = max-affinity** ("most-helpful-wins", the ranking dual of WardenChain's
  most-restrictive-wins): a query with several intents and a candidate with several roles reduce
  by `max over (intents x roles)`; intent lifts the relevance *band*, semantics orders *within*
  it; history-wanting temporal lens wins on conflict.
- **No second supersession logic and no combiner redesign** (the two non-goals): LifecycleStatus
  is consumed from `temporal.py`/`belief.py`; the classifier only *selects the temporal lens*
  (feeds `QueryIntent`). IntentOracle is just one more capped RELEVANCE family in the combiner.
- **Bench-first/gated:** stays out of `default_oracles()` until `archolith_bench/intent/` shows
  `intent-correct@1` >> baseline AND a shuffle-ablation collapses it (no topic leakage) AND a
  no-harm arm holds. Signs (P/N/X/-) are the human contract; magnitudes are bench-tuned.
- **Phase 4 (bench) is BUILT and the gate GRADUATES** (bench repo `1bf31fa`, pkg
  `archolith_bench/intent/`, 28 tests, lexical stand-in): intent-correct@1 0.143 -> 1.000,
  shuffle collapses (+0.309 over a random wrong intent), no-harm holds, determinism 1.0. Notes:
  bench `.agent/benchmark-notes/intent-oracle-demo-run.md`. Three findings fed back to the plan:
  the real proof is the shuffle collapse not the 1.000; no-harm must hold the stack constant
  (+/- intent); and `VERIFY_CURRENTNESS` routes to the *neutral* lens (CONFLICT/`any`), NOT
  historical (else the temporal producer boosts the stale item).
- **Real-embedder run DONE (bench `05a89da`) — IntentOracle graduates embedder-invariantly.**
  Swapped a real embedder (`text-embedding-nomic-embed-text-v1.5` on LM Studio :1234, via
  `intent/embedder.py --embedder`). It *changed the conclusion* on the single-topic floor
  fixture (it does NOT graduate under the embedder — the prose names the role, so the embedder
  recovers role-matching itself and a wrong intent rides the same floor; shuffle won't collapse).
  **Fixture-design law learned:** carry role in metadata (`artifact_type`), not prose. The new
  controlled fixture (within-topic text identical) graduates **identically under lexical AND
  embedder**. **Now hardened (bench `d3811a2`):** grown to a **4-topic** controlled corpus
  (`intent_multi_topic_corpus.json`, 28 mem/28 q), baseline **de-biased** (role-neutral hashed-id
  tiebreak — was inflated by alphabetical `benchmark_*`), and run across **three backends** —
  lexical, local nomic-embed, AND **OpenAI `text-embedding-3-small`** (`--scorer openai`, key from
  env). All three are **byte-identical**: baseline 0.393, intent_on 0.714, shuffle 0.429 → GRADUATES
  (lift +0.321, shuffle collapse +0.286). The IntentOracle contribution is embedder-invariant. The
  validator caught a real bug en route (topic name "blast radius" collided with the CHANGE_ANALYSIS
  cue). This fully satisfies the real-embedder gate.
- **Production integration DONE** (menhir `c979ca4`): `IntentOracle` is now in
  `default_oracles()` (graduated after the real-embedder gate), and `AssertionPipeline`
  (`auto_intent=True`) auto-derives the temporal lens from the query text at the pipeline entry
  (default `current` -> task-intent lens; explicit intent honored; no-cue stays current). 70
  oracle/intent/pipeline tests green. **Scope note (UPDATED 2026-06-29 — this superseded the
  earlier "not yet wired into recall_service" claim):** the oracle pipeline is **now wired into
  production recall** via `recall_service._apply_frontier` (`3bac9b5` end-to-end wiring, `724c0b5`
  credible-default config, `30c58d0` real cosine into the combiner). It runs the
  `LogSpaceOracleCombiner` + intent lens over the ScoringService survivors, with per-portion env
  toggles. Shipped defaults (`config/settings.py`): `oracle_ranking=ON`, `intent_lens=ON`,
  `shadow=ON`, `warden_gate=OFF` (aggressive on sparse agent-written evidence — env-flippable via
  `MENHIR_FRONTIER_WARDEN_GATE`), `bm25=OFF`. With no `MENHIR_FRONTIER_*` env set the recall path is
  byte-for-byte today's ScoringService behavior. **Only the merge to `main` is outstanding** (the
  live memory service installs from `main`). Intent rides this path for free.
- **Phases 1-3 (menhir pure-domain port) are DONE** (menhir `dcf795e`): `domain/query_intent.py`,
  `domain/artifact_role.py`, `domain/intent_affinity.py`, and `IntentOracle` in
  `services/retrieval_oracles.py` (a RELEVANCE-target oracle, deliberately NOT in
  `default_oracles()`). Tests `test_query_intent.py` / `test_artifact_role.py` /
  `test_intent_oracle.py` — **32 pass, ruff clean, 36 existing oracle tests still green** (full
  pytest DID run this session — graphiti_core was importable here). **Remaining = gated
  production integration ONLY:** add IntentOracle to `default_oracles()` and wire
  `task_intents_to_lens` at the recall entry point, after a real-embedder + grown-fixture re-run
  re-confirms the gate. Production recall is untouched by design until then.

**Cross-cutting:** CI for archolith-bench (R0 prerequisite, now covers facet **and** extraction-bench);
keep `RetrievalTuningConfig` default-off.

---

## 8. Decisions — do NOT re-litigate

- **R1:** un-fuse for attribution + a source-aware floor was the win; `weighted_rrf` fuses on **rank,
  not raw score** (linear `alpha*cosine+(1-alpha)*bm25` was rejected for the scale mismatch);
  `hybrid_alpha=0.5` is a seam the bench tunes, not a tuned value.
- **R2 is bench-first.** No menhir production change until F beats baselines on a real fixture. The
  `CandidateSource.FACET` seam is reserved for post-graduation only.
- **Public datasets (LongMemEval etc.) are the wrong fixture source** — wrong domain (no code facets),
  no scope/stale/rename distractors, no per-memory support labels. Fixtures are authored from our own
  real history instead. (LongMemEval is fine as a separate cross-domain sanity A/B.)
- **The validator flags, it does not design the benchmark.** Keep its heuristics modest; don't overfit.
- **R11 (amplification) is bench-gated** — reject it unless it beats the R7 one-pass combiner.
- **Repo split is load-bearing:** menhir implements, archolith-bench falsifies. Benchmark code never
  goes in menhir `src/`.

---

## 9. Naming history (avoid confusion)

The project was renamed **`cth.mcp.memory` → `yawn_memory` → `menhir`**. The code is now consistently
`menhir` (package `src/menhir/`, console script `menhir serve`, env vars `MENHIR_*`,
`MENHIR_BACKEND_URL`). The coherence pass (`a1cebe8`) swept the stale names out of the `.agent` docs.
**Distinct, real, KEEP:** `yawn.scheduler` (a *separate* component — the llama-server lifecycle
manager) and the `x-yawn-bg-warnings` HTTP header. A few `cth.mcp.memory` display strings still live in
*code* (explorer title, prompt examples, scheduler display name) — a known, low-priority code cleanup,
deliberately not changed in the doc sweep.

---

## 10. Doc map — what to read, in what order

**Research corpus (`docs/research/`), reading order from its `README.md`:**
0. Direction: `semantic-operating-system.md`, `oracle-architecture.md` (the four-layer + oracle stack).
1. Process/eval: `research-process.md`, `archolith-bench-operational-model.md`.
2. Positioning: `positioning.md` (CIP category + 3 lenses + decision-quality-per-token).
3. Retrieval pipeline: `retrieval-tuning-stack.md`, `facet-retrieval.md`, `oracle-amplified-retrieval.md`
   (R4–R7/R11, the combiner math + killer baseline), `oracle-execution-and-performance.md` (write
   boundary, snapshot rule, candidate priors), `retrieval-control-rails.md`, `intent-warden.md`
   (task-intent-aware ranking; the IntentOracle determination + pairing rule — §7).
4. Belief/temporal: `belief-layer.md` (BeliefCircuit, buckets, anergy/apoptosis), `connected-data-substrates.md`,
   `tracehead-braidtrace.md`.
5. Future: `cognitive-replay-and-phasing.md` (phase ladder, Cognitive Replay, epistemic-separation law).

**Bridge / plans (`.agent/plans/`):**
- `menhir-research-execution-ladder.md` — build order (READ for "what to build next").
- `deferred-verification.md` — the living owed-work checklist (READ when you can run things).
- `r1-hybrid-candidate-generation.md`, `r2-facet-candidate-generation.md` — the R1/R2 design notes.
- `l4-artifact-loop-v0.md` — the L4 artifact loop (§6c): bench-first slice + menhir port, decisions D1–D5
  + 9 invariants, 6-commit build plan. **BUILT** — bench green, port logic-checked (status table inside).
- `l4-commit6-live-verification.md` — the commit-by-commit Cypher walk to confirm the menhir port against
  live Neo4j at home (the §2 step the sandbox can't run). READ before touching the artifact graph at home.
- `menhir-intent-oracle-plan.md` — the IntentOracle build plan (Phases 1-4, bench-gated); pairs with
  `docs/research/retrieval/intent-warden.md`. **Design-only — no code yet.** See §7 "Intent-aware retrieval".
- this file (`chain-handoff.md`).

**Oracle/SOS schema specs (`docs/research/`, spec-only, the L3/L4 GAP):**
- `oracle-runtime-interfaces.md` — OracleInput/OracleFinding + primitive/composite taxonomy (Day 1).
- `layer4-knowledge-artifacts.md` — generic L3/L4 knowledge-artifact schema + promotion lifecycle (Day 2).
- `cold-start-brief.md` — task-shaped ColdStartBrief schema + context-pack provenance (Day 2).
- `facet-extraction-plan.md` — the R2 extractor-improvement path (Priority 6 / Day 3).

**Build sequencing (`docs/roadmap/`):**
- `weekend-oracle-runtime-roadmap.md` — the embedder-blocked-window plan (Days 1–3 now all drafted).
- `oracle-integration-plan.md` — Day-3 capstone: buildable-now vs gated map + Context Engine sketch +
  first ColdStartBrief benchmark sketch + a written (unfiled) issue list.
- `l3l4-overlay-sequencing-options.md` — **GAP decision-support**: five implementation strategies for the
  L3/L4 overlay + comparison matrix + recommended hybrid (C→A→B). Proposal only; ctharvey chooses.
- `l3l4-hybrid-sketch.md` — the C→A→B hybrid sketched into phases + a **decision register** (the choices
  inside each phase); flags the load-bearing four (promotion authority, human-capture default, store
  backend, proposal trigger). Decide those four and it becomes a rung breakdown.

**Roadmap (`docs/roadmap/`):** `weekend-oracle-runtime-roadmap.md` — the embedder-blocked-window plan
(Oracle Runtime interfaces, Layer-4 schema, Cold Start Brief spec; see §5/§7). Spec, not built.

**Operational (`.agent/`):** `README.md` (router), `architecture.md`, `data_models.md`, `endpoints.md`,
`memory-roadmap.md` (shipped milestones), `post-v1-todo.md`, `verified-current-findings.md` (known bugs).

**Bench (`archolith-bench/`):** `AGENTS.md` + `.agent/` (conventions); `archolith_bench/facet/` (R2);
`.agent/benchmark-notes/facet-r2-demo-run.md` (R2 results + caveats); `EXTRACTION_MODELS.md` +
`extraction-models.html` + `archolith_bench/extraction_sim.py` (the extraction-model benchmark, §6a);
`archolith_bench/oracle/` + `.agent/benchmark-notes/oracle-r4-r7-demo-run.md` (the R4-R7 oracle bench, §6b).

---

## 11. Session commit log (both on `claude/menhir-chain-handoff-doc-7iuat2`)

This branch = the prior chain's `claude/menhir-r1-r2-handoff-augrkw` merged forward + the newer work.

**menhir** (newest first; the R1/R2 + handoff history is below the merge):
```
(this refresh) docs: refresh chain-handoff for the L4 artifact loop (§6c)         ┐
06b3ea6 l4(6d): live-graph verification checklist + plan status                    │ L4 artifact loop
c990a68 l4(6c): ArtifactService (R9-lite facade) + read-only MemoryOracleService   │ — menhir port,
4fb3f10 l4(6b): ArtifactRepository — Cypher writer with first-class :Evidence      │ logic-checked
6832f79 l4(6a): artifact domain model + R9-lite trust policy                       │ (+ schema indexes
069c3d3 plans: mark L4 artifact-loop commits 1-5 done; commit 6 still gated        │  + integration test
c24d56d docs(plan): L4 artifact loop v0 — minimal safe slice (bench-first)         │  in this refresh)
f94774b docs: add research-vs-shipped inventory (EXISTS/PARTIAL/NEW)               │
a0c521b docs: reconcile research docs with prior art (L3/L4 substrate exists)      ┘
6899ff5 Merge claude/menhir-r1-r2-handoff-augrkw -> chain-handoff-doc branch
b4e5aa6 docs: add cross-chain handoff doc for fresh LLM onboarding
093a44a docs: add weekend oracle runtime roadmap                                (NEW since last handoff)
f0d6a30 docs(r2): mark R2 fixture drafted + validator added
a1cebe8 docs: workspace coherence pass — naming sweep, status bumps, SOS reconciliation
2a1bdd6 docs(r2): mark facet benchmark mechanism built in archolith-bench
d9424b0 docs(plans): R2 bench-first design note + expanded deferred-verification
3419508 docs: living deferred-verification checklist
e8da67d R1: attributed hybrid candidate generation + source-aware floor
603c880 docs(plans): R1 hybrid candidate generation design handoff
```
(plus this refresh's own doc-update commit on top.)

**archolith-bench** (newest first — the R2 facet work sits on top of the extraction-bench line):
```
79175a1 l4: benchmark runner + failure-demo fixture (without vs with_l4)          ┐ L4 artifact loop
dbaa0a3 l4: ColdStartBrief v0 — task-shaped brief carrying epistemic status        │ — bench slice,
91251ef feat(l4): commit 3 — read-only MemoryOracle                                │ 28 tests green
442df3f feat(l4): commit 2 — ArtifactMutator (R9-lite), invariants fail-closed     │ (§6c)
156c487 feat(l4): commit 1 — Evidence + Artifact models (bench-first L4 slice)     ┘
be55f58 feat(facet): hybrid extractor (Priority 6) — closes the extracted-mode gap
28ee9f3 docs: point to menhir chain-handoff from bench README
83357bb fixture(facet): contest q13/q15 + accept q11/q14 (hardening items 3-6)
875c373 fixture(facet): demote q07 to single-support (hardening item 2)
776c70e fixture(facet): make q01/q02 genuine 2-support (hardening item 1)
62daf75 feat(facet): expand fixture validator with four hardening heuristics
1741578 feat(facet): fixture validator + real-grounded 50/20 R2 DRAFT fixture
98395a3 feat(facet): benchmark-local facet retrieval + meet-point ladder (menhir R2)
c8291ec feat: default extraction-bench to blessed keepers (nano + qwen3-next-80b)  ┐
079ee96 docs: one-page extraction-model benchmark site with charts                 │ extraction-model
610c118 feat: OpenRouter paid routes tested — qwen3-next-80b is best open-weight    │ benchmark (§6a)
972682d feat: OpenRouter free-tier provider + rate-limit-excluded latency           │ — newly surfaced
6cc2f44 docs: Cerebras tested — fastest (0.30s) but 0.40 fact recall, rejected      │   in this refresh
…  (earlier extraction-bench commits: Gemini/Groq/DeepSeek targets, pricing, etc.) ┘
```

---

## 12. Immediate next moves for a new chain

1. **If continuing R2:** the mechanism + validator + a hardened draft fixture all exist and pass.
   The bottleneck is human/live: harden the fixture with ctharvey, plug in a real embedder + live
   graph, re-run the gate. Don't add more validator heuristics (it's done) and don't wire facet into
   menhir production until F graduates on the real setup.
2. **If continuing R1:** it's owed bench verification, blocked on the live stack — needs R0 traces and
   a runnable graphiti/pytest environment.
3. **If working the SOS direction / the embedder-blocked window:** the weekend Oracle Runtime roadmap
   (`docs/roadmap/weekend-oracle-runtime-roadmap.md`) is the place to start — it specs the L3/L4
   semantic-overlay (Programs B/D). But it's **spec work pending ctharvey's sequencing** into the
   ladder before building — still the biggest scope risk. Don't invent rungs.
4. **If working extraction quality (R2's real bottleneck):** the bench's `extraction-bench` (§6a) is the
   tool; `gpt-4.1-nano` / `qwen3-next-80b` are the blessed keepers. Next concrete moves: wire menhir's
   backend extraction config to a keeper, and write the weekend Priority-6 extractor-improvement plan.
5. **If continuing the L4 artifact loop (§6c) — the most recently active track:** the bench slice is green
   and the menhir port is logic-checked + bench-parity confirmed in-sandbox. The next move is the **live-
   graph walk at home** (`.agent/plans/l4-commit6-live-verification.md`): run the `test_artifact_*` files
   under the full env, walk the Cypher asserts against Neo4j, and confirm the decay/recall coupling.
   Do **not** start the LLM proposer, ColdStartOracle, or L3 layer without ctharvey sequencing them.
6. **If picking up Intent-aware retrieval (newest track):** decisions locked, **Phase 4 (bench)
   graduates** (`archolith_bench/intent/`, bench `1bf31fa`), and **Phases 1-3 (menhir pure-domain
   port + tests) are DONE** (`domain/query_intent.py`, `domain/artifact_role.py`,
   `domain/intent_affinity.py`, `IntentOracle` in `services/retrieval_oracles.py`; 32 tests pass).
   The ONLY remaining work is the **gated production integration**: add IntentOracle to
   `default_oracles()` and wire `task_intents_to_lens` at the recall entry point — but ONLY after
   a real-embedder + grown-fixture run re-confirms the bench gate. Do **not** do that wiring
   before the re-run (it would change the temporal lens for every query un-gated). Mind the three
   bench findings (§7): shuffle-collapse is the proof, no-harm holds the stack constant,
   verify->neutral lens.
7. **Always:** respect §2 (constraints), §8 (settled decisions), and the repo split (§2). Update
   `deferred-verification.md` and the relevant owner doc as you go; the bench decides graduation.
   **This doc is static** — if more work lands, refresh the "Last updated" line and §5/§6c/§7/§11.
