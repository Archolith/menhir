# Deferred verification — run when tests + benches are available

**Status:** LIVING DOC. Append as work lands in remote/sandbox sessions that cannot run the
pytest suite or archolith-bench. Check items off when verified locally; move fully-cleared sections
to the bottom "Done" log with a date.

**Why this exists:** remote sessions hit two hard limits —
1. the pytest suite cannot be collected (the private `cth-mcp-framework` dep is not on any index, and
   `tests/conftest.py` imports the full infra chain), and
2. `graphiti_core` is not importable, so anything touching live search / RRF scores is unverifiable.

So code lands compile-checked and logic-checked via standalone scripts, but **the checks below are
owed** before any of it is trusted or promoted.

## How to run (fill in once confirmed locally)

```bash
# full suite (CHANGELOG history: ~1329 passed; 2 known-deferred NaN-scoring failures)
python -m pytest tests/ -q
# focused R1 set
python -m pytest tests/test_hybrid_retrieval.py tests/test_scoring_service.py tests/test_recall_service.py -q
# archolith-bench: see docs/research/process/archolith-bench-operational-model.md (separate repo)
```

---

## R1 — hybrid candidate generation + source-aware priors (commit e8da67d)

### Tests — EXECUTED LIVE 2026-06-28 (local home env; sandbox §2 could not run these)
Env: full dep chain (graphiti_core, cth_mcp_framework, neo4j, httpx, pytest) imports here.
Run: frontier src via PYTHONPATH against the menhir `.venv`; offline/stubbed (online tests skipped).
- [x] `tests/test_hybrid_retrieval.py` passes (weighted_rrf, config validation, source attribution,
      determinism, hybrid_search vs stub). **Green.**
- [x] `tests/test_scoring_service.py` source-aware floor cases pass (vector floored, BM25/pending/
      file-linked exempt; vector-floor regression). **Green.**
- [x] `tests/test_recall_service.py` new cases pass (split-search routing, BM25-only survives floor,
      default path unchanged). **Green.** (58 R1-dedicated tests pass together.)
- [x] Full `pytest tests/ -q` run: **1385 passed, 29 skipped, 7 failed (540s)**. No regression from
      `CandidateData.source` / `source_map` / `SOURCE_PRIORS`. Failure triage:
      - 2 = the known deferred NaN-scoring failures (expected).
      - 4 = test/code DRIFT the sandbox never ran (NOT R1 production bugs), **FIXED** in commit
        `bb0c34a`: `test_api_routes::TestRecall` x2 (assertions missing the deliberate
        `include_session=False` the /api/recall route now sends) + `test_structural_anchoring::
        TestRecallFileContext` x2 (`_MockStructure` lacked `resolve_structural_neighbors_bulk`,
        the ported bulk-perf method `recall_service._resolve_file_context` now calls).
      - 1 = `test_expected_venv_python_resolves_project_root_not_src` — artifact of running frontier
        src through the main worktree venv; would clear under a frontier-native venv (NOT a code bug).
- [x] `tests/conftest.py` stub change (`search_ranked_by_method`) doesn't break other suites. **Green.**

### Live-graphiti verification (the open scale question)
- [x] **Score scale — CONFIRMED LIVE 2026-06-28.** Bench harness `archolith-bench/scripts/
      probe_rrf_scale.py` (imports menhir GraphitiClient as a library, throwaway neo4j 7688, nano
      ingest of 10 memories) measured real `node_reranker_scores`: **max = 2.0000** (= rank_const=1
      dual-method 1/1+1/1, exactly as predicted), range ~0.05-2.0, and the 0.15 floor dropped 12/24
      and 6/15 candidates as a **rank cut**, not a similarity cut. Matches the code analysis below
      verbatim. So: re-document the floor as a rank cutoff (or rescale it) — it is NOT a cosine
      threshold. Original code-derived characterization:
      `search_scored` returns `results.node_reranker_scores`, and graphiti's node path
      (`graphiti_core/search/search.py:282`) builds those via `rrf(...)` with the **default
      `rank_const=1`** (`search_utils.py:1780`, `score += 1/(rank0 + 1)`, no override at any node
      call site). So the score is **RRF, not cosine** — yet `scoring_service.MIN_SIMILARITY_THRESHOLD
      = 0.15` and its comment ("cosine similarity ... genuine matches typically >0.3") describe a
      cosine scale. With `rank_const=1` over 2 methods (bm25 + cosine): top dual-method hit = 2.0,
      single-method top = 1.0; the 0.15 floor actually cuts ~dual-method rank >12 and ~single-method
      rank >5. **Conclusion:** the floor is effectively a **rank cutoff (~top 6 single / ~13 dual)**,
      not a similarity cutoff; the value lands somewhere sane only by luck of `rank_const=1`, and the
      code comment's rationale is wrong for the actual RRF scale. Implication: a genuinely relevant
      result buried deep by rank is floored regardless of closeness, while a weak rank-0 hit (2.0)
      sails through — and R1's source-aware exemption is load-bearing precisely because BM25/file-
      linked candidates would otherwise be rank-floored. **Owed (BLOCKED on WSL/Docker — bench
      throwaway needs the WSL2 Docker backend, down 2026-06-28):** stand up the bench throwaway, log
      the real `node_reranker_scores` distribution on seeded data to confirm rank_const=1 is in
      effect and scores span ~0-2, then decide whether to re-document the floor as a rank cut or
      rescale it. Writes go to the throwaway only, never prod.
- [ ] **Hybrid path vs floor.** In hybrid mode `similarity` is a *normalized fused* score; confirm
      how the `0.15` floor behaves for **VECTOR-only** candidates there (it currently means "below
      15% of the top fused score", not a cosine cutoff). Decide whether the hybrid path needs its own
      floor handling / renormalization. Source-exempt candidates are unaffected by design.
- [ ] **`search_ranked_by_method` works on real graphiti** — single-method `SearchConfig` +
      `NodeReranker.rrf` returns rank-ordered `results.nodes`; episode exclusion holds; cosine
      dimension-mismatch path returns `[]` for the cosine pass without killing bm25.
- [ ] **Latency.** Two-pass hybrid vs one fused `search_scored` — quantify the added round-trip on
      the opt-in path.

### archolith-bench (R1 ladder A–E) — depends on R0 traces
> **BUILT + RAN LIVE on the DEMO 2026-06-28** — ladder lives in archolith-bench `archolith_bench/r1/`
> (branch `claude/menhir-chain-handoff-doc-7iuat2`): models/metrics/runner/win-gate + stub retriever
> (`run_r1_bench.py`, CI) + live driver (`run_r1_live.py`, seeds throwaway 7688, runs `recall(trace=True)`).
> Live demo run executed end-to-end (`.agent/benchmark-notes/r1-live-demo-run.md`). The boxes below for
> mechanism are done; the DEMO saturates (recall=1.0), so the gate cannot graduate and `hybrid_alpha`
> stays unset — owed on the labeled prod corpus where recall has headroom.
- [x] Fixture families: `exact_error_string`, `symbol_name_query`, `paraphrased_debug_question`,
      `stale_semantic_neighbor`, `wrong_repo_same_topic`, `buried_relevant_memory`,
      `historical_only_vs_current_truth`. *(modeled in `r1/models.py` + demo fixture; real fixture owed)*
- [x] Baselines A (current recall) → E (hybrid + `hybrid_alpha` sweep `0/0.25/0.5/0.75/1.0`; endpoints
      = vector-only / BM25-only). *(C dim-sweep deferred — secondary axis.)*
- [x] Metrics: `exact_string_recall`, `symbol_recall`, `recall_at_k`, `stale_hit_rate`,
      `wrong_scope_injection_rate`, `latency_ms`. *(shared `r1/metrics.py` + `aggregate_metrics`.)*
- [ ] **Headline win condition:** hybrid/source-prior path beats baseline A on `exact_string_recall`
      and `symbol_recall` **without** regressing `stale_hit_rate` or `wrong_scope_injection_rate`.
      *(gate implemented + unit-tested; DEMO does NOT graduate — recall saturated, no headroom. Owed on
      the real fixture. Honest demo signal: hybrid lowers stale-hit 0.094→~0.05, raises wrong-scope.)*
- [ ] **Then** set `hybrid_alpha` to the bench-chosen value (it currently ships at neutral `0.5` as a
      seam, not a tuned value) and flip ladder R1 status `planned → in-progress/done`. *(BLOCKED on a
      real-fixture graduation — do not tune on the saturated demo.)*
- [x] Blocker: this needs **R0** emitting per-candidate `source` / `prior` / `survived_floor` in the
      retrieval trace. **R0 menhir-side instrument LANDED `ac7204b`** — `recall(trace=True)` returns a
      `RetrievalTrace` (per-phase timings + per-candidate `source`/`similarity`/`survived_floor`, and
      survivor `score_parts`/`final_score`/`rank`), opt-in and default-off (production path unchanged).
      8 unit tests green. **Still owed:** the bench consumes the trace to run the A-E ladder + sweep
      `hybrid_alpha` (the items above) — that is the R1 deliverable, now unblocked.

---

## R2 — facet candidate generation (bench-first)

Full plan: [`../../archive/plans/r2-facet-candidate-generation.md`](../../archive/plans/r2-facet-candidate-generation.md). **Bench-first: no
menhir production change until F beats baselines.** Deliverables 2–4 live in **`archolith-bench`**.

> **Update 2026-06-27 (handoff session):** archolith-bench was **in scope** this session, so the
> benchmark-local mechanism + harness landed in `archolith_bench/facet/` (branch
> `claude/menhir-r1-r2-handoff-augrkw`), with 46 passing unit tests and a runnable ladder. A DEMO
> fixture (10 memories / 6 queries) proves the pipeline end-to-end; the **real 50/20 gold fixture is
> still owed** and is a ctharvey pairing task (Risk #1). See
> `archolith-bench/.agent/benchmark-notes/facet-r2-demo-run.md`. The mechanical-build boxes below are
> checked; the fixture, real-baseline, and promotion-gate boxes stay open.

### Build (in archolith-bench)
- [~] Benchmark fixture (JSON): 50 hand-authored memories, 20 queries, known support IDs per query.
      *(DRAFT `fixtures/facet_r2_draft.json` now exists — 50/20, grounded in REAL menhir+archolith
      history (R1 floor change, cth.mcp.memory→yawn_memory→menhir rename chain, CE-willow drift,
      real files/symbols/bugs, cross-repo collisions). Validates clean. STILL OWED: adversarial
      hardening with ctharvey (Risk #1) + confirm gold support IDs. DEMO `facet_demo.json` (10/6) kept.)*
- [x] Distractors: stale, wrong-repo, a symbol-rename case, ≥1 vague query where embedding should win.
      *(all present in the DRAFT and checked by the new validator; harden with ctharvey.)*
- [x] Fixture validator (`archolith_bench/facet/validate.py`): errors (missing support IDs, dup IDs,
      bad buckets) vs quality warnings (missing distractor families, uncontested/"too clean" queries,
      under-spec counts). Run before trusting any ladder result. *(not in the original plan — added
      to de-risk hand-authoring.)*
- [x] Each memory carries raw text **and** explicit gold facet labels (facet set: actor, object,
      operation, file, symbol, test, valid_time, learned_time, evidence_type, source_id, repo,
      project, namespace, belief_bucket). — `archolith_bench/facet/models.py`.
- [x] Benchmark-local `MemoryFacetSet`. — `archolith_bench/facet/models.py`.
- [x] Benchmark-local `FacetExtractor` (simple deterministic rules — not LLM-heavy). — `extractor.py`.
- [x] Benchmark-local `MemoryFacetIndex` — candidates by compatible facet **overlap**, not similarity. — `index.py`.
- [x] Benchmark-local `MeetPointReranker` — `meet_score` (required-facet overlap + file/symbol/test +
      evidence/source + time-window − stale/superseded − wrong-scope), with a per-candidate
      **explanation trace** (which facets matched, which penalties fired, why it ranked there). — `reranker.py`.

### Run (two facet modes, kept separate)
- [x] **Gold facets** mode — "do facets help if correct?" — wired in `runner.py`; DEMO run done.
- [x] **Extracted facets** mode — "can a cheap extractor recover enough?" — wired; DEMO run done
      (gate honestly FAILS on the DEMO, exposing the extractor gap — Risk #2 separation holds).
- [x] Conditions: A BM25 · B embedding top-k · C BM25+embedding · D existing graph/file-context ·
      E facet+embedding rerank · F facet+meet-point rerank. *(G +BeliefLayer gates: later, not built.
      **B/C/E use a deterministic lexical embedding stand-in**, D a file/symbol-overlap stand-in —
      swap in a real `EmbeddingScorer` / live graph retriever before trusting B–E.)*
- [x] Metrics together (not just scores): recall@5, precision@5, MRR, NDCG, paraphrase_stability,
      stale_hit_rate, wrong_scope_injection_rate, support_sufficiency, false_neighbor_rate,
      latency_ms — all in `metrics.py`. *(answer_grounding_accuracy needs a generation model — left as
      an explicit gap for the home run.)*
- [x] Preserve run artifacts: config, metrics, raw outputs, traces, failure notes —
      `scripts/run_facet_bench.py` writes `results/facet_run.json`; report in `.agent/benchmark-notes/`.

### Promotion gate (decision point)
- [ ] **F (facet + meet-point)** improves `stale_hit_rate`, `wrong_scope_injection_rate`, **or**
      `support_sufficiency` vs BM25/embedding/hybrid baselines **without unacceptable recall loss.**
      *(gate logic implemented in `runner.evaluate_promotion_gate`; on the DEMO fixture gold-mode F
      graduates and extracted-mode F does not — but DEMO numbers are a harness sanity check, NOT the
      decision. The gate must be re-run on the real fixture with real baselines before it counts.)*
- [ ] Write the short research report: does R2 move toward menhir production integration?
      *(interim demo report exists at `archolith-bench/.agent/benchmark-notes/facet-r2-demo-run.md`;
      the real-fixture report is owed.)*
- [ ] **Only if F graduates:** plan the production-integration rung (wire `CandidateSource.FACET` +
      its prior/floor exemption into recall — the R1 seam is reserved for exactly this). Until then,
      do **not** touch production recall.

### Remote-session note
- The benchmark-local implementation (`MemoryFacetSet`/`FacetExtractor`/`MemoryFacetIndex`/
  `MeetPointReranker`) is pure Python and **was drafted + unit-tested in this remote session** (it
  lives in `archolith-bench/archolith_bench/facet/`, not menhir's `src/`). Still owed at home, needing
  ctharvey's judgment: the real 50/20 gold fixture (avoid the "fixture too clean" risk, R1), a real
  `EmbeddingScorer` for conditions B/C/E, the live graph retriever for D, and a re-run of the
  promotion gate on that real setup.

---

## Perception / write-side gate

### temporal-ingest-backdating / occurred_at (plan archived 2026-07-11)
Product change shipped and verified (`occurred_at` -> persisted `reference_time` ->
`coalesce(reference_time, queued_at)` at Graphiti; integration test PASS showed 2023
`reference_time` distinct from `queued_at`). Owed bench runs (needs a full LME oracle build):
- [ ] **A/B on an oracle subset** — confirm temporal-reasoning + knowledge-update accuracy is
      materially higher than the timestamp-less baseline.
- [ ] **Clean full oracle-500 build** on the corrected (backdated) ingest.

### phase3-consumer-quality-pack-v1 (plan archived 2026-07-11)
All 5 items landed and verified in code (`count_spend_compound`/`count_vs_spend_partial`,
`verify_retries`/`verify_votes`/`verify_k`, correction `_PATTERNS`). Offline gate + suites pass.
Owed live characterization (needs a throwaway :8099; real :8090 untouched):
- [ ] **Items 1-2 stochastic effectiveness, live 2x run** — measure the real-world co-extraction
      rate of count-vs-spend and the fold-SUM retry lift under a live throwaway Menhir. Invariants
      must hold: `wrong_view_writes=0, silent_abstentions=0, duplicate_writes=0`.

### perception-dedup-signature + veto receipts (plan archived 2026-07-11)
Code landed and verified (`GateDecision.veto`, `VETO_UNRESOLVED_COREFERENCE`, certain-only
`_event_signature`, tri-state `coref_memo`). The plan is archived as code-complete; the one owed
observation is carried here so it is not lost:
- [ ] **Live benchmark receipt readout** — run an explicit `perception_write.py` pass and read the
      itemized by-veto abstention buckets / per-site veto receipts to confirm the certain-only
      signature narrowing did not over-abstain (was "verification step 3" in the archived plan).

---

## Cross-cutting / standing checks
- [x] **CI can run archolith-bench — LANDED 2026-07-04** (`.github/workflows/ci.yml`, first CI on
      the repo). Runs the offline bench suite on py3.11+3.12: installs `archolith-maintenance` from
      its public GitHub repo (not on PyPI), `pip install -e ".[dev]"`, `pytest tests/` (338 passing).
      A `tests/conftest.py` guard skips the R3/R5 ladder modules when `menhir` is absent (menhir is a
      separate repo with private deps, not CI-installable) and runs them unchanged when a menhir
      checkout is on `PYTHONPATH`. **Still owed:** a menhir-present lane (self-hosted or a menhir
      checkout step) to exercise R3/R5 in CI; wiring the R1/oracle *ladder* runs (not just unit
      tests) into a scheduled job once they need a live graph.
- [ ] Keep `RetrievalTuningConfig` default-off until a bench result justifies turning `enable_bm25`
      on by default anywhere.

---

## Done log
<!-- move cleared sections here with a date, e.g. "2026-07-xx R1 tests green (pytest 1330 passed)" -->
- _(nothing verified locally yet)_
