# R2 facet — production integration plan (CandidateSource.FACET)

## Status

**Phases 1–3 SHIPPED. Production activation remains PARKED; a Recall Lab-only active rerank seam
was added 2026-07-14 for renewed blinded A/B evaluation.**
The observe-only facet stack (mechanism → derivation → candidate source → Neo4j reader → recall
shadow) is built, tested, and live-verified with zero recall behavior change. Active wiring
(`enable_facet_candidates`) is **not** justified by the evidence and is parked, not wired: a
three-gate investigation (a: role, b: anchor-noise robustness, c: net-new over a real embedding
baseline — see below and `archolith-bench .agent/benchmark-notes/facet-r2-gate-b-anchor-noise.md`)
found FACET-as-reranker's genuine net-new over menhir's existing stack (real-embedding recall +
ScopeWarden scope discipline) is **marginal** (~1 gold / 23 on the draft fixture; ~0–1 on the real
graph once anchor noise degrades the symbol half). The shadow keeps measuring on the live graph for
free; revisit if a larger/different corpus or the live shadow shows real topical lift, or if the
mechanism is **repurposable** for a different job.

_History._ PLANNED 2026-07-05. The production-integration rung the R2 plan reserved "only if F
graduates." Precondition **met**: F graduates in gold + hybrid modes with a real embedder
(`facet-r2-real-embedder-run.md`), structural facts are real graph facts
(`facet-r2-structural-facet-decomposition.md`). Decision **A** (wire gated for the anchored slice)
over **B** (raise anchoring — dead, ~171 stragglers). Gates (a)/(b)/(c) then walked that back to
shadow-only on measured evidence.

_2026-07-14 experiment update._ `enable_facet_candidates` is now wired default-OFF as a bounded
rank-fusion experiment: it runs FACET over the already-retrieved candidate pool and fuses the
meet-point order with the base order. It does not scan a global facet index, expand graph scope,
grant FACET floor exemption, or change production defaults. Recall Lab preset D is its intended
consumer while the LLM-judge batch produces new evidence.

## What we know (the constraints this plan must respect)

- **Bounded win by construction.** Only **24.5%** of memories carry `ANCHORED_TO` edges to file
  nodes; the other 75.5% are genuine non-code memories. FACET must **fail safe** there: emit no
  candidates, never regress the unanchored path.
- **The structural facts are in the graph, not text.** File facets come from `(mem)-[:ANCHORED_TO]->
  (file{structure_path})`; symbol facets one hop further via `(file)-[:DEFINES]->(symbol{symbol_kind})`;
  scope from `namespace` / `structure_project`. The text extractor only reliably recovers *symbols*
  (recall 0.55) and *operation/evidence* interpretive facets.
- **The R1 seam already exists.** `CandidateSource.FACET` is a reserved enum; the source-aware floor
  + per-source prior machinery (R1) was built to accept exactly this new candidate family.
- **Evidence is still directional** — the draft fixture is not adversarially hardened (Risk #1).

## Design

### 1. Port the bench-local mechanism into menhir (pure domain, no I/O)
`archolith_bench/facet/{models,index,reranker}.py` -> `menhir/domain/facet_*.py`:
- `MemoryFacetSet` (the 14-facet model) — likely already partially covered by existing metadata.
- `MemoryFacetIndex` — candidate generation by facet **overlap** (not similarity).
- `MeetPointReranker` — `meet_score` over required-facet overlap + file/symbol/test + scope/time,
  minus stale/wrong-scope, with an explanation trace. Copy the graduated weights.
These are deterministic + unit-tested in the bench; port with their tests.

### 2. Derive query + candidate facets from the live graph (the new I/O)
- **Candidate (memory) facets:** `file` from `ANCHORED_TO` targets' `structure_path`; `symbol` from
  the anchored files' `DEFINES` symbols (bounded fan-out) + the improved text extractor; `scope`
  (repo/project/namespace) from node metadata; `belief_bucket`/time from existing lifecycle stamps.
- **Query facets:** the query's `file_context_project` + any structural anchors on the query +
  interpretive facets from the query text (the ported extractor). Query facets stay cheap.
- **Where it runs:** a `FacetCandidateSource` in the recall candidate-generation phase, gated. It
  MUST prefetch/bound the graph traversal (one bulk query over the candidate pool's anchors, like
  the R5 structural-expansion prefetch), not a per-candidate round-trip.

### 3. Wire into recall behind a flag
- `RetrievalTuningConfig.enable_facet_candidates: bool = False` (the seam pattern).
- When on: `FacetCandidateSource` contributes candidates tagged `CandidateSource.FACET`, with the
  reserved source prior + floor exemption (so a facet-overlap match survives the semantic floor,
  exactly the R1 rationale). Meet-point rerank applies within the facet-candidate set.
- **Shadow first:** emit a `facet_shadow` in the retrieval trace (like the assertion shadow) that
  records what FACET *would* contribute, before it changes live ranking — measure on prod recall.

## Phases

1. **Port mechanism** (domain + tests) — **DONE 2026-07-05**: `menhir/domain/facets.py`
   (`MemoryFacetSet` + `MemoryFacetIndex` overlap generator + `MeetPointReranker` with a
   `defer_discipline_to_wardens` seam so recall integration can hand scope/stale to the warden
   chain), `tests/test_facets.py` (6 unit tests, both fashions + warden-deferral). No recall change.
2. **Facet derivation + candidate source.**
   - **2a DONE 2026-07-05**: `menhir/domain/facet_derivation.derive_facets(...)` — pure mapping from
     graph anchors (`ANCHORED_TO` files, `DEFINES` symbols) + scope/belief/time metadata + content
     (interpretive facets: operation/object/actor/evidence + text symbols) into a `MemoryFacetSet`;
     both fashions, graph facts win, text fills the gaps. `tests/test_facet_derivation.py` (9 tests),
     no graph I/O.
   - **2b DONE 2026-07-05**: `menhir/domain/facet_candidate_source.py` — `FacetCandidateSource`
     composes derivation + index + meet-point rerank (`defer_discipline_to_wardens=True`) behind a
     `FacetGraphReader` protocol + `FacetInputs` carrier. One bounded prefetch over the pool ->
     facet-overlap candidates -> convergence rerank (scope/stale left to the wardens).
     `tests/test_facet_candidate_source.py` (5 tests vs a stub reader: convergence-first, warden
     deferral, no-overlap->no-candidate, single prefetch, regular-memory overlap). No graph I/O.
   - **2b-real DONE 2026-07-05**: `menhir/infrastructure/facet_graph_reader.Neo4jFacetGraphReader` —
     one bulk Cypher over the pool's `ANCHORED_TO`(file `structure_path`/`structure_project`) /
     `DEFINES`(symbol) + node metadata (namespace / conflict_status / created_at / content) ->
     `FacetInputs`. 5 offline mapping tests + **live-verified against the dummy** (real facets
     derived for anchored memories via one query; `FacetCandidateSource` end-to-end works). Notes:
     multi-project anchors -> ambiguous scope (None); `conflict_status` superseded/historical ->
     historical bucket. Minor refinement owed: DEFINES `test_*` names land in `symbol`, not `test`.
3. **Shadow wiring** — **DONE 2026-07-05**: `recall_service._run_facet_shadow` +
   `RetrievalTrace.facet_shadow` + `enable_facet_shadow` flag (default OFF). Observe-only, never
   changes results, wrapped so it never breaks recall; emits a `facet_shadow` telemetry event. 2
   recall-service tests + **live-verified** against the dummy (runs on real recall, results unchanged).
   **Initial live signal (this is what the shadow is for):** query facets extract correctly
   (`symbol=scoring_service/source_aware_floor/weighted_rrf`), but facet-overlap over the *recall
   pool* is **sparse** — the vector pool's candidates rarely carry the query's symbol facets, and
   scope facets need a namespace/project-scoped query. **Phase-4 design choice this surfaces:** FACET
   as a RE-RANKER over the recall pool (bounded, but sparse overlap) vs a GENERATOR over a full facet
   index (finds facet-matching memories the vector pool missed — heavier, needs a persistent index).
   Measure both against a scoped-query workload before choosing.
4. **Active production wiring** remains **PARKED**. A narrower Recall Lab-only, default-OFF active
   rerank seam landed 2026-07-14: bounded pool only, rank fusion only, and no FACET floor exemption.
   This is measurement infrastructure, not a production-default reversal. A broad/global facet
   generator still requires new evidence and a separate design.

### Gate (a) — reranker vs generator: MEASURED 2026-07-06 (dummy 7687, generator over broad pool)

Ran the generator role directly: hand `FacetCandidateSource.contribute()` the **broad cross-scope
pool** (all 1300 anchored dummy memories) with scoped queries (`project=cth.mcp.memory` + symbol),
via `scratch_facet_generator_measure.py`. Decisive result:

- **Generator candidate set ≈ the scope-filtered corpus.** Every scoped query returned ~157
  candidates = exactly the in-scope memories. The scope facet does real *filtering* (157 of 1300),
  but does **not rank** — scope-only candidates all sit flat at `score=2.00`. Pure scope filtering
  is **already the ScopeWarden's job** (the "do NOT duplicate the wardens" architecture note): the
  generator adds nothing there but a flood.
- **Structural convergence is the only within-scope signal.** For a symbol query, 13 candidates
  converged on `project+symbol` and meet-point ranked them on top (8/8 convergent, `score=5.50 >
  2.00`). The **mechanism is sound** — convergence rises correctly. But this only helps the anchored
  slice, and only when a structural (symbol/file/test) facet actually meets.
- **The dummy validates mechanism, NOT relevance.** The "convergent" hits were semantically wrong
  (`yawn_memory` explorer files for a `cth.mcp.memory` symbol query) because the dummy's
  `ANCHORED_TO`/`DEFINES` anchors are synthetic noise. **This is Risk #1 in the flesh: facet value
  is bounded by anchor quality**, so relevance can only be judged on a clean/hardened fixture.
- **Bug caught:** query test-names derive to the `test` facet, but DEFINES test-names land in
  `symbol` → the query `test=` never meets the candidate `symbol=` → test-name convergence silently
  fails (the "test_* lands in symbol" refinement the plan flagged as owed — now shown load-bearing).

**Decision: RE-RANKER, not generator.** FACET's net-new value is *structural convergence within an
already-relevant, already-scoped pool* — the reranker role over the recall pool (scope/stale deferred
to the wardens, exactly the current shadow wiring). The generator's scope-filtering is redundant with
the ScopeWarden and its flat whole-scope output is a flood without structural convergence, so a
persistent generator index is **not** justified. Phase 4 therefore wires FACET as a bounded
convergence re-ranker (bonus only when file/symbol/test converge), gated on: fix the test/symbol
facet-family mismatch first, then the hardened-fixture relevance check (Risk #1), then a live A/B.

- **test/symbol fix DONE 2026-07-06** (menhir `fix(facets)` 593fdce): test-named DEFINES symbols
  now route to the `test` facet regardless of origin, so query `test=` meets candidate `test=`.

### Gate (b) — anchor-noise relevance check: MEASURED 2026-07-06 (real anchors + bench regime)

"Measure the real graph first, then model the noise." Two steps:

1. **Real anchor quality** (`menhir-frontier/scripts/_measure_anchor_quality.py`, read-only on the
   live-menhir clone = dummy 7687 per `_clone_to_dummy.py`): **mean 9.0 anchors/memory (max 215),
   ~75% text-unsupported, boilerplate magnets** (`pyproject.toml` on 239 memories, `app/main.py`
   DEFINES 51 symbols on 110). The real noise model is dominant **spurious over-anchoring + true-
   anchor loss**, not gentle drop/swap.
2. **Bench regime** (`archolith-bench/archolith_bench/facet/anchor_noise.py` +
   `scripts/_gate_b_anchor_sweep.py`, calibrated to (1)): inject the noise into hybrid mode with
   gold relevance labels and sweep `true_drop_frac` 0.0→1.0, ± hygiene.

**Result: F is ROBUST to real anchor noise, including total (drop=1.0) true-structural-anchor
loss** — recall holds/rises (0.825→0.85), wrong_scope stays 0.07–0.11 vs 0.40 baseline, GRADUATES
in every regime (stub AND real OpenAI embedder). The win is **scope/belief discipline (noise-free
metadata) + interpretive overlap**, not structural convergence — which the sweep shows can be 100%
corrupted with no collapse. This **inverts the gate-(a) dummy alarm**: the dummy looked catastrophic
only because that raw measurement ran structural convergence WITHOUT scope discipline; with scope
discipline (ScopeWarden), noise is a non-issue. **Anchor quality is NOT a Phase-4 blocker; hygiene
is optional** (`text_support` restores 1.4 anchors/mem but the win doesn't need it).

**The go/no-go catch this surfaces:** since the win is scope/belief — already the ScopeWarden/
CurrentnessWarden job — FACET-as-reranker's *net-new over the existing warden chain is thin*. The
one thing wardens don't do (structural convergence) is negligible and noise-poisoned on the real
graph. So gate (b) passing on **robustness** does NOT by itself justify active wiring. Phase-4 go/no-go
now hinges on the remaining open question: does facet candidate **generation** by operation/object
overlap surface topically-related memories that vector recall misses (gate a said scope-generation
floods, but operation/object topical grouping was not isolated).

### Gate (c) — net-new over a REAL embedding baseline: MEASURED 2026-07-06 → PARK

Isolated the topical-generation question offline (`archolith-bench scripts/_gate_c_topical_lift.py`,
gold mode): of the gold memories BOTH baselines (BM25 + embedding) rank outside top-k, how many does
full-facet F recover via a NON-scope (operation/object/structural) match (from the meet-point
explanation trace; scope-only "recovery" is a tie-break flood artifact, discounted)?

| embedder | gold | vector-missed (both baselines) | F recovers via non-scope match |
|---|---|---|---|
| stub (lexical) | 23 | 4 | 4 |
| **openai (real)** | 23 | **1** | **1** (q15→m37, object+symbol) |

**Against a real embedding baseline, FACET's net-new recall is ~1 gold / 23 (~4%)** — the real
embedder finds 3 of the 4 the stub missed — and that 1 leans on symbol convergence, anchor-noise-
degraded on the real graph. **Decision: PARK Phase-4 active; keep FACET shadow-only.** The net-new
over (real-embedding recall + ScopeWarden) is marginal. Caveat: n=1 is within the noise of a
52-memory fixture, so the signal is "marginal", not "provably zero" — a larger/different corpus (or
the live shadow) could reopen it; the observe-only stack is retained precisely to keep watching.

## Risks / gates

- **Latency (hot path).** Facet derivation adds a graph traversal at recall time — must be one
  bounded bulk query, measured against the R0 latency baseline before active wiring.
- **Fixture not hardened (Risk #1).** RESOLVED as moot by gate (b): the fixture's structural facets
  were the "too clean" worry, but the reranker's win is scope/belief, robust to total structural
  corruption — so a hardened structural fixture would not change the parked verdict. FACET ships
  shadow/off regardless.
- **Bounded value.** ~24.5% coverage — set expectations: this improves the code-anchored slice's
  scope/stale discipline (wrong_scope 0.40 -> 0.07 in bench), not whole-corpus recall.

## Extension — facets for REGULAR (unanchored) memories: a different fashion

The bounded-win framing ("only the 24.5% anchored slice") is only true for **structural** facets
(file/symbol). But the meet-point reranker's *dominant* levers are the **scope penalty (5.0)** and
**stale penalty (4.0)** over `repo/project/namespace` + `belief_bucket` — facets **every** memory
carries (scope recall 0.71; belief from lifecycle stamps). File/symbol convergence is only a +1.5
bonus. So facets have **two fashions over one mechanism** (the `MemoryFacetIndex` overlap engine):

| fashion | facet population | applies to | new value |
|---|---|---|---|
| **structural** | file / symbol / test (from `ANCHORED_TO`/`DEFINES`) | anchored 24.5% | precise code-convergence candidate generation |
| **interpretive / scope** | operation / object / actor / evidence_type + repo/project/namespace + belief | **~all memories** | coarse topical grouping + scope/stale discipline |

**Crucial architecture note — do NOT duplicate the wardens.** The scope/stale *discipline* the
reranker's big penalties provide is already menhir's `ScopeWarden` + `CurrentnessWarden` job (they
gate any recalled memory on scope/currentness, corpus-wide). So:

- The facet system's **net-new** contribution is **candidate GENERATION by facet overlap** (surface
  memories that share operation/object/scope or file/symbol pairs — a signal vector similarity
  misses), plus **structural convergence** scoring for the anchored slice.
- The scope/stale **penalties** should be **deferred to the warden chain**, not re-implemented in a
  ported reranker. The ported meet-point scorer stays lean: required-facet overlap + structural
  convergence + evidence/time; scope/stale handled by the wardens that already run post-rank.
- "Facets for regular memories" is therefore realized as: (a) interpretive/scope **facet-overlap
  candidate generation** (new, corpus-wide), feeding (b) the **existing wardens** for discipline.
  This lifts the ceiling from "24.5% structural" to "corpus-wide topical + scope grouping," with the
  structural precision as the anchored-slice bonus.

Implication for the phases: Phase 1 ports the **generic** overlap mechanism (facet set + index +
lean convergence scorer) — it serves both fashions unchanged; only the facet *population* differs.
Phase 2's derivation supplies structural facets for anchored memories and interpretive/scope facets
for the rest. Warden integration replaces the reranker's scope/stale penalties.

**VALIDATED 2026-07-05 (empirical, not just asserted).** Re-ran the gold ladder with file/symbol/test
facets **stripped** (`run_facet_bench.py --facet-scope regular`, real embedder): **F still graduates
with zero structural facets** — wrong_scope 0.40→0.07, stale 0.27→0.12, no recall loss, nearly
identical to the all-facets F. So the dominant facet win is scope/belief-driven and
structural-independent → **corpus-wide, not bounded to 24.5%**; structural precision is a minor
anchored-slice bonus. Pinned by an archolith-bench regression test
(`test_regular_mode_keeps_scope_discipline_without_structural_facets`). Caveat: the draft fixture is
code-themed, so a conversational fixture is still owed to confirm beyond it — but scope facets (the
load-bearing ones) are ~0.71 metadata-derivable, the reliable path.

**Gate-(c) update:** the "corpus-wide topical generation" this section hoped for was isolated and
came back **marginal** against a real embedding baseline (~1 gold / 23 net-new; see gate (c) above).
The scope/stale *discipline* half is real but is the ScopeWarden's job (already shipped). So the
net-new the facet system would add on top is thin — the reason Phase-4 active is parked. The
"two fashions" framing stands as analysis; it just doesn't clear the bar for hot-path wiring here.

## Non-goals

- Do NOT invest in raising anchoring coverage (Option B — dead: ~171-memory ceiling).
- Do NOT wire `enable_facet_candidates` (Phase 4) — PARKED 2026-07-06; the seam stays reserved and
  unwired until new evidence of topical lift or a repurposing use-case appears.
- Do NOT build a per-candidate graph round-trip; the facet derivation is a single bounded prefetch.
