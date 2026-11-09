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

## 2026-11-08 - Two stores migrating one SQLite file at once no longer fail

- Schema upgrades read a table's columns, then add the missing ones. Two stores doing that on the
  same file at once both saw a column missing, and the second `ALTER TABLE ... ADD COLUMN` failed
  with "duplicate column name" (seen in CI as a failed `delete_memory`). A shared
  `add_column_if_missing` now treats that error as success, at all four sites: the telemetry
  lineage columns, `llm_usage_events`, `lifecycle_events` and the operations journal.

## 2026-11-08 - Opt-in recall query log for recall research

- New setting `MENHIR_TELEMETRY_RECORD_RECALL_QUERIES` (default off). When on, each MCP recall
  (`recall_memories`, `recall_context_memories`) and `POST /api/recall` writes one row to a new
  telemetry table, `recall_query_log`: the query text and its SHA-256, the ordered result ids and
  scores, source memory ids, namespace, preset, limit, and the recall receipt token when there is
  one. `recall_receipts` stays content-free.
- With `MENHIR_PRIVACY_REDACT` on, only the hash is stored. Rows follow the 90-day diagnostic
  retention tier. Namespace erasure scrubs the query text; rows with no namespace only age out
  (`docs/your-data.md`).
- The write goes through the #527 off-loop writer, so it never opens SQLite on the event loop.

## 2026-11-08 - A telemetry read that may miss queued writes says so

- A telemetry read waits up to `MENHIR_TELEMETRY_BUSY_TIMEOUT_S` (5 s) for off-loop writes queued
  before it. When that wait times out, the read still runs but now logs a warning naming how many
  writes are still queued, instead of returning a possibly incomplete result silently.

## 2026-11-08 - v0.3.6: profile catalog 2, telemetry off the event loop, one operation table

- Profile catalog 2 puts `get_client_context` in `core`, so agent-tier clients receive it. A
  deployment opts in with `"profile_catalog": 2`; catalog 1 policies resolve as before.
- Telemetry writes no longer block the event loop (#527), and the token endpoint logs why it
  refused a request (#541).
- What each TypedAssertion operation means now lives in one table; an operation it does not know
  carries no value anywhere.
- #519: a span-aligned scalar no longer keeps a `current_`/`latest_` prefix its quote dropped.
  Readers understand an `approximate` operation; nothing writes one yet.
- v1 and v2 client policies still load. The v0.3.5 notes said this release would drop them; that
  moves to a later release.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.6. No dependency changes in this commit.
