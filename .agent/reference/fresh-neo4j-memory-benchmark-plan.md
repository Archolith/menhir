# Fresh Neo4j Memory Benchmark Plan

## Why

Menhir needs a launch-grade benchmark that measures memory retrieval quality in a controlled graph, not in the developer's long-lived Neo4j instance. The current baseline test at `tests/test_m0_retrieval_baseline.py` proves the shape of the problem, but it depends on a populated live graph and checks keyword hits rather than explicit relevance judgments. That is useful for smoke coverage, not enough for launch evidence.

The benchmark should answer three questions:

- Can Menhir retrieve the right memory from a known corpus?
- Does graph-augmented recall beat or tie raw vector search?
- Do lifecycle, scope, conflict, and stale-memory rules protect result quality?

## Scope

In scope:

- Add a Menhir-owned benchmark suite under `benchmarks/`.
- Start a fresh disposable Neo4j Docker container by default.
- Seed a deterministic memory corpus into the fresh graph.
- Run a labeled query set with explicit expected memory IDs.
- Compare Menhir graph-augmented recall against vector-only Graphiti search.
- Emit machine-readable JSON and launch-facing Markdown reports.
- Add contract tests for fixture validation, metric calculation, dirty-graph protection, and report shape.
- Update Menhir docs so operators know how to run the benchmark.

Out of scope for this pass:

- Publishing leaderboard-equivalent MTEB or BEIR scores.
- Adding an `archolith-bench memory` wrapper before Menhir has a stable benchmark artifact.
- Benchmarking arbitrary live workspace memory.
- Replacing existing online tests.
- Tuning recall scoring to improve the benchmark. Scoring changes should be a separate implementation pass after baseline evidence exists.

## Current Constraints

- Menhir is a Python 3.12+ project with Neo4j and Graphiti dependencies declared in `pyproject.toml`.
- Runtime settings are env-backed through `src/menhir/config/settings.py`; Neo4j defaults are `bolt://localhost:7687`, database `neo4j`, user `neo4j`, and empty password.
- Service assembly already exists in `src/menhir/core/bootstrap.py` via `build_memory_services()` and `prepare_memory_runtime()`.
- Full runtime startup in `src/menhir/core/runtime.py` also starts preflight, scheduler handling, pending episode resume, and orphan recovery. The benchmark should prefer direct service assembly unless it intentionally needs full runtime behavior.
- Online tests are skipped unless `--run-online` is passed; this is enforced in `tests/conftest.py`.
- Existing replay support in `tests/replay/runner.py` can drive ingest, recall, consolidation, decay, and orphan recovery steps, but its current expectation model is scenario/pass-fail oriented rather than IR metric oriented.
- The current M0 retrieval baseline uses `tests/fixtures/milestone0_query_set.json`, keyword checks, and a live populated graph. Keep it as a smoke test, but do not treat it as the launch benchmark.
- Existing run docs in `.agent/workflows/run_and_test.md` still mention older `cth.mcp.memory` paths and `docker compose up -d neo4j`; benchmark docs should use current `menhir` paths and avoid mutating the developer's normal `yawn-neo4j` container.

## Proposed Design

### 1. Benchmark Fixtures

Files to add:

- `benchmarks/corpora/menhir_memory_seed.jsonl`
- `benchmarks/queries/menhir_memory_qrels.jsonl`
- `benchmarks/README.md`

Seed record shape:

```json
{
  "id": "mem-archolith-bench-openai-default",
  "type": "SEMANTIC",
  "scope": "PERSISTENT",
  "text": "archolith-bench launch defaults use the OpenAI-compatible endpoint https://api.openai.com/v1 with gpt-4o-mini unless overridden.",
  "source": "benchmark-seed",
  "tags": ["archolith-bench", "launch", "configuration"]
}
```

Query record shape:

```json
{
  "id": "menhir-q001",
  "query": "What endpoint is archolith-bench supposed to use by default?",
  "preset": "knowledge",
  "relevant_ids": ["mem-archolith-bench-openai-default"],
  "must_not_return": ["mem-archolith-bench-old-nvidia-default"],
  "category": "semantic"
}
```

The first launch corpus should include at least these categories:

- semantic project facts
- procedural run commands
- user/workflow preferences
- conflict/superseded facts
- session-scope facts that must not leak by default
- temporal or stale facts that should be suppressed or demoted
- structural facts tied to file paths

### 2. Fresh Neo4j Harness

Files to add:

- `benchmarks/docker_neo4j.py`

Behavior:

- Start `neo4j:5` in a uniquely named container such as `menhir-bench-<run_id>`.
- Use random host ports for Bolt and HTTP to avoid collisions with `yawn-neo4j`.
- Use an ephemeral Docker volume by default.
- Generate a benchmark-local password at runtime and pass it through `MemorySettings`, not through a committed file.
- Wait for Bolt readiness with bounded retries.
- Destroy container and volume after the run unless `--keep-neo4j` is passed.
- Write the container name, image tag, ports, and fresh/dirty state into the benchmark report.

Safety requirements:

- Default mode must fail if the target graph is not empty before seed ingest.
- `--neo4j-mode existing` may be supported for debugging, but it must require `--allow-dirty-graph` when any nodes or relationships exist.
- The harness must never stop or remove a container that it did not create.

### 3. Benchmark Runner

Files to add:

- `benchmarks/run_retrieval.py`
- `benchmarks/report.py`
- `benchmarks/__init__.py`

Runner flow:

1. Parse CLI options:
   - `--suite launch`
   - `--neo4j-mode docker-fresh|existing`
   - `--neo4j-image neo4j:5`
   - `--seed benchmarks/corpora/menhir_memory_seed.jsonl`
   - `--queries benchmarks/queries/menhir_memory_qrels.jsonl`
   - `--out benchmarks/results/menhir-retrieval-YYYY-MM-DD.json`
   - `--markdown benchmarks/results/menhir-retrieval-YYYY-MM-DD.md`
   - `--keep-neo4j`
   - `--allow-dirty-graph`
2. Start or connect to Neo4j.
3. Build `MemorySettings` with the benchmark Neo4j URI/user/password/database and benchmark-selected LLM/embed provider settings.
4. Build services with `build_memory_services(settings)` and call `prepare_memory_runtime()`.
5. Confirm graph is empty before seeding unless dirty mode is explicitly allowed.
6. Ingest seed records and wait until enrichment completes or fails with a clear timeout.
7. Resolve benchmark seed IDs to actual graph node UUIDs.
8. Run each query through:
   - vector-only Graphiti search via `graphiti_client.search_scored()`
   - Menhir recall via `recall_service.recall()`
9. Score results against qrels.
10. Write JSON and Markdown.
11. Close Menhir services and tear down Docker resources.

The runner should instantiate services directly instead of calling `start_runtime()` because `start_runtime()` also resumes pending episodes, starts scheduler behavior, and launches orphan recovery. Those are valuable production behaviors but add noise to a deterministic benchmark.

### 4. Metrics

Files to add:

- `benchmarks/metrics.py`

Required metrics:

- `hit_at_1`
- `hit_at_3`
- `hit_at_10`
- `mrr_at_10`
- `ndcg_at_10`
- `wrong_memory_rate`
- `must_not_return_rate`
- `stale_leakage_rate`
- `session_leakage_rate`
- `mean_latency_ms`
- `p95_latency_ms`
- `mean_results_returned`
- `mean_context_chars`

Comparative metrics:

- vector-only `MRR@10`
- Menhir graph recall `MRR@10`
- delta between graph recall and vector-only
- count of queries where graph recall improved, tied, or regressed

Launch gate proposal:

- `Hit@3 >= 0.80` on the launch query set.
- Menhir graph recall must tie or beat vector-only on `MRR@10`.
- `must_not_return_rate == 0` for explicitly stale/superseded facts.
- `session_leakage_rate == 0` when `include_session` is false.
- Every returned Menhir result must include scoring/explainability metadata.

### 5. Tests

Files to add:

- `tests/test_memory_benchmark_contract.py`
- `tests/test_memory_benchmark_metrics.py`
- `tests/test_memory_benchmark_docker_plan.py` or equivalent pure-unit harness tests

Test coverage:

- fixture files parse and contain required fields
- every query references at least one seed ID
- every `must_not_return` ID exists in the seed corpus
- metric functions produce expected `Hit@k`, `MRR@k`, and `nDCG@k` on synthetic rankings
- dirty-graph detection refuses to run by default
- Docker cleanup only targets benchmark-owned container names
- report JSON contains run metadata, corpus hash, query hash, commit hash, Neo4j image, provider config summary, and metric blocks

Online test coverage:

- add one opt-in `@pytest.mark.online` smoke test that runs a tiny benchmark against a fresh container if Docker is available
- keep it skipped by default under the existing `--run-online` policy

### 6. Report Artifacts

Files/directories to add:

- `benchmarks/results/README.md`
- optional tracked summary: `benchmarks/results/menhir-retrieval-launch-template.md`

JSON report fields:

- `run_id`
- `timestamp`
- `menhir_commit`
- `neo4j_image`
- `neo4j_mode`
- `fresh_graph`
- `seed_hash`
- `query_hash`
- `seed_count`
- `query_count`
- `node_count_after_ingest`
- `edge_count_after_ingest`
- `providers`
- `metrics`
- `per_query`
- `warnings`

Markdown report sections:

- run metadata
- launch gate verdict
- graph-recall vs vector-only summary
- per-category metrics
- failures and regressions
- caveats
- reproduction command

### 7. Documentation

Files to update:

- `.agent/README.md`
- `.agent/workflows/run_and_test.md`
- `.agent/data_models.md`
- `.agent/architecture.md`
- `.agent/CHANGELOG.md`
- `README.md`

Doc requirements:

- Document that the benchmark uses fresh Neo4j by default.
- Document that existing live memory is not valid launch evidence.
- Document required external dependencies: Docker, Neo4j image, LLM/embed provider.
- Document how to run the benchmark in clean mode and debugging mode.
- Document how the results relate to `archolith-bench` industry coverage.

### 8. Optional Later Archolith-Bench Wrapper

Files to touch later, not in the first Menhir implementation:

- `projects/archolith/archolith-bench/archolith_bench/core/industry.py`
- `projects/archolith/archolith-bench/archolith_bench/suites/industry.py`
- possible future `projects/archolith/archolith-bench/archolith_bench/suites/memory.py`

Condition for this step:

- Menhir must first produce stable JSON/Markdown artifacts from its own benchmark runner.
- The wrapper should call Menhir's benchmark, not duplicate Menhir's harness logic.

## Alternatives Considered

### Reuse Live Developer Neo4j

Rejected for launch evidence. Live graphs contain historical workspace memory, unknown timestamps, stale queue state, and prior enrichment artifacts. That makes results non-reproducible and easy to overstate.

### Use Existing M0 Online Test Only

Rejected as insufficient. `tests/test_m0_retrieval_baseline.py` uses keyword hits and assumes a populated graph. It should stay as online smoke coverage, but the launch benchmark needs explicit qrels, fresh state, and artifact output.

### Implement Only MTEB/BEIR

Rejected for the first pass. MTEB and BEIR provide credible retrieval metric framing, but Menhir's value includes graph adjacency, lifecycle, conflicts, session scope, and structural anchoring. A Menhir-native benchmark should use MTEB/BEIR-style metrics without claiming official MTEB/BEIR scores.

### Put Everything In Archolith-Bench First

Rejected for sequencing. Menhir owns the graph lifecycle, runtime config, and recall semantics. Archolith-bench should consume Menhir artifacts after the benchmark contract stabilizes.

## Risks

### Blocked

- Docker unavailable or not running on the benchmark machine. The fresh-container default cannot run without Docker.
- No usable LLM/embed provider. Graphiti enrichment and vector search need configured providers.
- Graphiti extraction nondeterminism may make seeded graph contents unstable unless the seed corpus and provider settings are constrained.

### Mitigable

- Port collisions with local Neo4j. Use random host ports and pass the resulting Bolt URI directly to `MemorySettings`.
- Long enrichment time. Add per-seed and full-run timeouts, then report timeout failures clearly.
- Dirty graph contamination in `existing` mode. Refuse by default; require `--allow-dirty-graph`.
- Docker cleanup failure. Use benchmark-owned container labels/names and print cleanup instructions when teardown fails.
- Windows temp/path issues. Reuse the project's local temp handling pattern from `tests/conftest.py`.
- Scheduler side effects. Use direct service assembly instead of `start_runtime()`.

### Acceptable

- First launch corpus will be small. That is acceptable if the report labels query count and corpus hash clearly.
- External benchmark comparison will be methodology-level at first, not official MTEB/BEIR scoring.
- `archolith-bench` integration can lag behind Menhir by one implementation pass.

## Invariants

- The benchmark must never mutate the user's normal `yawn-neo4j` container.
- A clean benchmark must start from an empty graph.
- Results must be reproducible from committed seed/query fixtures plus documented provider settings.
- Candidate mappings in `archolith-bench` must remain labeled as gates until a tracked Menhir benchmark artifact exists.
- Existing online test behavior stays opt-in.
- No secrets are committed; reports may name provider/model and env var names, not API key values.

## Open Questions

- Which provider should be canonical for launch evidence: local OpenAI-compatible llama.cpp, OpenAI, or both?
- Should the launch corpus be synthetic, harvested from prior Archolith decisions, or a mix?
- What is the minimum acceptable launch query count: 25, 50, or 100?
- Should temporal/stale behavior be tested through direct graph state setup or through public lifecycle APIs only?
- Should fresh Docker use a managed Python Docker SDK dependency or plain `docker` CLI subprocess calls? Plain CLI avoids a new dependency but is harder to mock cleanly.

## Validation

Before implementation is considered ready:

```powershell
python -m pytest tests/test_memory_benchmark_contract.py tests/test_memory_benchmark_metrics.py -q
```

```powershell
python -m pytest -m "not online" -q
```

Opt-in integration validation:

```powershell
python benchmarks/run_retrieval.py --suite launch --neo4j-mode docker-fresh --out benchmarks/results/menhir-retrieval-local.json --markdown benchmarks/results/menhir-retrieval-local.md
```

Optional online pytest smoke:

```powershell
python -m pytest --run-online -m online tests/test_memory_benchmark_online.py -q
```

Manual report checks:

- JSON has metadata, hashes, metric blocks, and per-query records.
- Markdown includes a launch gate verdict and reproduction command.
- Docker container and volume are removed after a normal run.
- `--keep-neo4j` leaves only the benchmark-owned container alive.
- Existing `yawn-neo4j` remains untouched.

## Docs To Update During Implementation

- `README.md`
- `.agent/README.md`
- `.agent/architecture.md`
- `.agent/data_models.md`
- `.agent/workflows/run_and_test.md`
- `.agent/CHANGELOG.md`
- `benchmarks/README.md`
- `projects/archolith/archolith-bench/benchmarks/industry-trusted-benchmark-coverage.md` after Menhir evidence exists

## Coverage Summary

Inspected:

- `README.md`
- `pyproject.toml`
- `src/menhir/config/settings.py`
- `src/menhir/core/bootstrap.py`
- `src/menhir/core/runtime.py`
- `src/menhir/infrastructure/neo4j.py`
- `tests/conftest.py`
- `tests/test_m0_retrieval_baseline.py`
- `tests/fixtures/milestone0_query_set.json`
- `tests/test_recall_service.py`
- `tests/replay/runner.py`
- `tests/replay/fixtures/basic_ingest_recall.json`
- `.agent/README.md`
- `.agent/memory-foundations.md`
- `.agent/memory-policy.md`
- `.agent/memory-ingest-queries.md`
- `.agent/workflows/feature_planning.md`
- `.agent/workflows/run_and_test.md`

Not inspected:

- Full `.agent/architecture.md` and `.agent/data_models.md`; the plan only needed their update targets, not full schema details.
- All MCP tool implementations; the first benchmark should use service-layer calls, not MCP surfaces.
- Full Graphiti internals; benchmark should treat Graphiti through Menhir's existing `GraphitiClient` boundary.
- Docker compose files; this plan intentionally avoids the developer's normal compose stack and proposes a benchmark-owned container.

Assumptions without direct code evidence:

- Docker CLI is available on the implementation machine.
- `neo4j:5` is acceptable as the launch benchmark image tag unless a more exact patch version is chosen.
- A stable LLM/embed provider will be selected before launch evidence is generated.
- A small Menhir-native qrels corpus is acceptable as launch evidence if it is clearly labeled and reproducible.
