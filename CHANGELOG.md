## 2026-11-09 - v0.3.8: Similarity search uses Neo4j vector indexes (#574); recall rerank threads follow the CPU quota (#567)

- Every start creates four cosine vector indexes if they are missing (entity names, relationship
  facts, episodes, typed assertions), and similarity search reads candidates from them instead of
  scanning every row (#574). Results are rescored exactly and fall back to the scan while an index
  builds. On a 17k-entity test graph a search took ~55 ms instead of ~1 s. An upgrade builds the
  indexes in the background on its first start; unquantized, the two large ones hold about 100 MB
  of vectors each.
- `MENHIR_RECALL_RERANK_THREADS` (default 0 = auto) caps the reranker's ONNX thread pool at the
  container's CPU quota (#567). No change on hosts without a quota.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.8. No dependency changes in this commit.

## 2026-11-09 - Recall rerank threads follow the container's CPU quota (#567)

- New `MENHIR_RECALL_RERANK_THREADS` (default 0 = auto). Auto caps ONNX Runtime's intra-op pool
  at the cgroup CPU quota when that is below the visible cores, and uses one inter-op thread.
  On a 1-CPU container with 6 visible cores the uncapped pool was throttled by the quota: 50
  pairs took ~15 s instead of ~6 s, and the throttling stalled the rest of recall. With no
  quota, ONNX Runtime keeps its own default. A positive value sets the count directly.
- The thread count is logged in `recall rerank loaded` and `recall rerank ready`, and is part
  of the loaded-model cache key.
- `.env.example` documents the cost of `MENHIR_RECALL_RERANK_TOP_N` on small hosts (~120 ms per
  pair on a shared cloud vCPU): lower it, or give the container more CPU.

## 2026-11-09 - Similarity search uses Neo4j vector indexes (#574)

- Every start now creates four cosine vector indexes if they are missing: `Entity.name_embedding`,
  `RELATES_TO.fact_embedding`, `Episodic.content_embedding` and `TypedAssertion.name_embedding`.
  The dimension comes from the configured embedder, or from a stored embedding when the model is
  unknown. Indexes are created unquantized, so their scores are exact. An existing upgrade gets
  them on its next start; `candidate-readonly` mode still creates no schema.
- Graphiti's node and edge similarity search, and Menhir's source-memory and scalar-observation
  lanes, read candidates from an ONLINE index instead of computing cosine over every row. Each
  result is rescored exactly and keeps the same filters and `min_score`. It is used only when the
  candidates' scores show that no row outside them could rank in the top results, which holds up
  to the index's nearest-neighbour recall. Otherwise the search fetches more candidates, up to
  1024, then scans as before.
- The scan is still used while an index is missing, populating, or of another dimension, and for
  searches pinned to a few known rows (edge dedup's endpoint pair, `edge_uuids`, subject-scoped
  source memory). Index state is cached for 30 s, and a failed index call scans and re-reads it.
- Startup reports each index's state under `vector_indexes` and logs it. Index state is not part
  of schema readiness, so a large index building in the background never blocks writers.

## 2026-11-09 - Credentials are redacted from memory text on write and on recall (#577)

- New `MENHIR_SECRET_POLICY`: `redact` (default), `refuse` or `off`. Under `redact`, provider keys,
  JWTs, private-key headers and `password =`-style assignments in submitted memory text become
  `[REDACTED:<kind>]` at intake, before the idempotency digest, turn evidence, the queue node, the
  embedder and the extraction LLM. The TEMPORAL, todo, candidate, document and raw-capture writers
  scrub too. Recall redacts ranked results, their temporal facts and `source_memories`, and returns
  nothing if the scan fails. `off` keeps and returns credentials as given.
- `menhir secrets scan --namespace <ns>`: read-only count of stored credentials by memory type and
  kind; prints no values; exits 1 when it finds any.
- New dependency: `detect-secrets` 1.5.0 (no entropy detection).
- The same scrub covers every read: MCP tool and resource results, `/internal/backend`, all JSON
  `/api` responses, the CLI hook and the explorer. A failed scan withholds the output.
- Limits: defence in depth only. Facts the extraction LLM writes, plaintext stored earlier,
  non-JSON responses and the explorer's stored lab-run records are not covered.

## 2026-11-09 - v0.3.7: Approximate scalar claims on by default (#519), cross-encoder recall rerank with its security hardening, recall query telemetry, and telemetry and schema fixes

- Approximate scalar claims are on by default (#519). A hedged single number is stored as
  `approximate`: it never sets a View's value, and a newer one keeps the View advisory and is
  listed under it. Set `MENHIR_PERSONAL_MEMORY_SCALAR_APPROXIMATE_ENABLED=false` for the old
  behaviour.
- Recall reranks its top 50 candidates with a local ONNX cross-encoder, on by default, and caps
  the results at 7 when the rerank runs. A long-running server downloads the pinned model (~92 MB)
  from Hugging Face in the background on first start; until it loads, and offline, recall keeps
  the RRF floor. Opt out with `MENHIR_RECALL_RERANK_ENABLED=false`, or set
  `MENHIR_RECALL_RERANK_AUTO_DOWNLOAD=false` with `MENHIR_RECALL_RERANK_MODEL_DIR` for air-gapped
  installs. The tokenizer and model are digest-pinned on every load.
- New dependencies: `onnxruntime`, `tokenizers` and `huggingface-hub`.
- Opt-in recall query log (`MENHIR_TELEMETRY_RECORD_RECALL_QUERIES`, default off).
- Telemetry: the off-loop writer skips the per-commit fsync, and a read that may miss queued
  writes logs a warning. Two stores migrating one SQLite file at once no longer fail.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.7. No dependency changes in this commit.

## 2026-11-09 - Approximate scalar claims are on by default (#519)

- `MENHIR_PERSONAL_MEMORY_SCALAR_APPROXIMATE_ENABLED` now defaults to on. A hedged single number
  ("about 400,000", "close to 1300 now") is stored as an `approximate` claim instead of being
  dropped; it never sets a View's value, but a newer one keeps the View advisory and is listed
  under it. The scalar prompt changes accordingly. Set it to `false` for the old behaviour.
- Evidence: o500 items whose user turns hold a hedged number (26 of them, knowledge-update and
  single-session types), scalar lane rerun from the same base with the flag off and on: 22/26 vs
  24/26, no losses. The flag-attributable gain is the Instagram case that opened #519 (a View of
  1250 led; "close to 1300 now" was newer). The other gain had identical recall. Early read on a
  small set.
- Only new scalar consolidation writes approximate claims; hedged numbers already dropped are not
  backfilled.

## 2026-11-09 - Recall rerank hardening (security audit Low findings)

- The tokenizer is now digest-pinned on every load, including an explicit
  `MENHIR_RECALL_RERANK_MODEL_DIR`. Model and tokenizer are read once, checked, and built from
  those bytes, so a file swapped after the check is never loaded.
- A cached copy that fails its pin is downloaded again once at server start, instead of leaving
  recall on the RRF floor until someone clears the cache. A read error while checking a file now
  keeps the floor instead of breaking hook recall.
- `MENHIR_RECALL_RERANK_ENABLED=false` now gives recall no model dir anywhere, so Recall Lab
  arms and other tunings that default the rerank on stay off.
- Scoring runs one call at a time per process. Recall Lab caps `rerank_top_n` at 100. A
  runtime-init retry joins the running model fetch instead of starting another.
- The download sends User-Agent `menhir-recall-rerank` instead of huggingface_hub's default,
  which names the agent harness. ONNX Runtime telemetry events are turned off at load.
  `docs/your-data.md` lists the download under what leaves your machine.

## 2026-11-08 - A newer approximate value is listed under the advisory View (#519 phase 3)

- When a same-slot `approximate` claim is newer than a View, the View is now advisory and the
  verdict lists that claim: its "about N" value, the user's quote and its time. The View's value is
  never changed by it. Phase 1 taught the freshness gate this, but recall dropped approximate rows
  before the gate saw them, so it never fired outside unit tests.
- Context output adds one line per entry under the View, `newer, approximate: about N ("quote",
  time)`; the recall API verdict gains an optional `newer_approximate` list (empty by default).
  With nothing newer, output is unchanged.
- Occurrences that demote a View are not listed here; they behave as before. Approximate rows are
  read for this only, and never enter the ranked list or the fold.

## 2026-11-08 - Cross-encoder recall rerank (on by default, model downloads itself)

- `MENHIR_RECALL_RERANK_ENABLED` (default true) orders the first `MENHIR_RECALL_RERANK_TOP_N`
  (50) recall candidates with a local ONNX cross-encoder (ms-marco-MiniLM-L6-v2) instead of
  cutting them at the RRF rank floor. Candidates past the top N keep the floor. Wardens still
  gate the result; the oracle blend does not reorder it. Shown scores are sigmoid(logit).
- A reranked list skips the floor, so it would always fill the limit. `MENHIR_RECALL_RERANK_MAX_RESULTS`
  (7) caps the results returned when rerank ran. In an offline sweep, the top 7 keep the rerank's
  top-5 recall gain at about the old context size; score floors lost evidence.
- One install: `onnxruntime`, `tokenizers` and `huggingface-hub` are now normal dependencies. With
  no `MENHIR_RECALL_RERANK_MODEL_DIR`, a long-running server downloads the pinned model (~92 MB,
  `cross-encoder/ms-marco-MiniLM-L6-v2` @233902d2) into the Hugging Face cache in the background,
  checks the tokenizer and model digests, loads it, and then starts reranking. Boot never waits;
  until then, offline, or on any failure, recall keeps the RRF floor and the server logs why once.
  Hooks only use an already cached copy and never download. No Hugging Face token is sent.
- `MENHIR_RECALL_RERANK_AUTO_DOWNLOAD=false` turns the download off (air-gapped installs); set
  `MENHIR_RECALL_RERANK_MODEL_DIR` to a flat dir or a Hugging Face snapshot instead. Only a
  sha256-pinned model loads. `MENHIR_RECALL_RERANK_DEVICE` is `auto`, `cpu` or `cuda`; `cuda`
  never falls back to CPU. For CUDA, replace `onnxruntime` with `onnxruntime-gpu`.
- The server refuses to start if a model dir is set explicitly and cannot load. A failure at
  recall time keeps the floor for that call and says so in the note. Set
  `MENHIR_RECALL_RERANK_ENABLED=false` to opt out entirely.
- Recall Lab arms can toggle it (`enable_cross_encoder_rerank`, `rerank_top_n`,
  `rerank_max_results`).
- Evidence (`.agent/reviews/menhir-recall-rerank-ranking-stats-2026-11-08.md` in the workspace):
  LME distractor graph, 414 questions, answer accuracy 0.850 -> 0.872 with the cap, +7.2% input
  tokens; base LME graph IR support Hit@3 0.250 -> 0.410, answer accuracy unchanged (0.818 / 0.814,
  uncapped). Offline (tiny-model-surgery recall arm, #516): judged-only Recall@5 on the mix set
  0.555 -> 0.878. About 200 ms per recall on 12 CPU threads, 600 ms on 2, 44 ms on CUDA.
- `uv.lock` and `THIRD-PARTY-LICENSES.txt` gain the new packages. The manifest also picks up
  version drift already in the lock (jiter, joserfc, markupsafe, numpy, pydantic).

## 2026-11-08 - Approximate scalar claims (#519, default off)

- New setting `MENHIR_PERSONAL_MEMORY_SCALAR_APPROXIMATE_ENABLED` (default off). When on, a single
  number hedged only by an approximation cue ("about 400,000", "close to 1300", "nearly 50", "I think
  it's 50") is stored as an `approximate` claim instead of being dropped. It is listed in scalar
  history and shown as "about N"; it never sets or changes a View's value. Alternatives ("30 or 40"),
  ranges, hedged deltas, non-numeric values, tentative or contextual wording ("maybe 30", "5,
  sometimes 6") and vague quantities ("a few") are still dropped.
- The model never names `approximate`: it still emits `absolute` with the stated number and code
  relabels it from the quoted span, so all k samples agree. The scalar prompt changes only when the
  flag is on; with it off the prompt and every drop decision are unchanged.

## 2026-11-08 - The off-loop telemetry writer skips the per-commit fsync

- Connections opened on the off-loop telemetry writer thread use `synchronous=NORMAL` in WAL mode,
  so each observability write no longer waits for a disk flush, and the writer falls behind less
  often (a read that outruns it logs a warning, see below). A power loss can drop only the latest
  of those rows, and never corrupts the file.
- Everything else that opens a connection through `connect_telemetry_db` keeps `FULL`: the
  scheduler lease, the operations journal, pending actions, migration batches, erasure subjects,
  metric receipts and the recovery rows (merge audit, memory revisions). So does any connection
  that is not in WAL mode.
