## 2026-11-12 - v0.3.10: 0.3.9 with a release image (release workflows pinned to ubuntu-24.04)

- Ships everything in v0.3.9 (#476 delete by id, the erase fence, the fence-lock hang fixes). v0.3.9
  reached PyPI but has no release image, because its image build failed on ubuntu-26.04 runners.
  Deploy 0.3.10, not 0.3.9.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.10. No dependency changes in this commit.

## 2026-11-12 - Release workflows pinned to ubuntu-24.04

- `publish-pypi.yml`, `release-image.yml`: every job runs on `ubuntu-24.04`, not `ubuntu-latest`.
  `ubuntu-latest` moved to ubuntu-26.04, where the release image's Syft SBOM no longer matches the
  candidate image ID, so the v0.3.9 image build failed after PyPI had published. v0.3.9 has no
  release image; v0.3.10 ships the same code with one. Follow-ups: image dry-run before tagging
  and the image-ID check on newer runners.

## 2026-11-12 - v0.3.9: Delete default-namespace memories by id (#476); erasure fences in-flight enrichment; fence-lock hang fixes

- `delete_memories` (operator) erases up to 100 memory, TurnEvidence or typed-assertion ids per
  call through the same ownership guard and erase path as `delete_memory`. It previews by default,
  refuses the whole call on an invalid id or more than 100, keeps going past a failing id, and
  reports ids it had no time for as `not_run` after 90 s (#476).
- `list_namespace_memories` (operator, read-only) pages a namespace's erasable ids, keyset on id.
  `default` covers every stored spelling of it; Graphiti twins, structure rows, evidence
  projections, erased records, entities and artifacts are never listed (#476).
- `delete_namespace` still refuses `default` (including `' default '`) and points to the two tools.
  `delete_memory(id, namespace='default')` no longer refuses a memory stored with `namespace=''`.
- Tool catalog version 3. A v1/v2 client policy must decide both tools or startup fails; a v3
  policy pinned below 3 withholds them. `scripts/update_client_policy.py` updates a policy and
  prints its new digest.
- The erase fence (seal, erasure markers, publish fence on Graphiti fork 0.31.2.post3) from the
  2026-11-10 to 2026-11-12 entries below ships in this release.
- The fence-lock event-loop fixes (stdio E2E hang, shutdown hang #601) from the entry below ship in
  this release.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.9. No dependency changes in this commit.

## 2026-11-12 - Fence-lock writes no longer wait on the event loop (stdio E2E hang)

- The publish fence holds a namespace's `EvidenceNamespaceFence` inside Graphiti's async publish
  transaction, which only the event loop can commit. Sync writes that take the same lock ran on the
  loop, so a same-namespace publish could freeze the backend for good: an admitted `add_memory`
  (its evidence projection) hung the release PR #599 stdio E2E.
- `queue_episode_for_enrichment` now does all of its graph writes in one `asyncio.to_thread` hop;
  enqueueing stays on the loop. `stamp_and_finalize`'s fenced writes (stamp, retention, self-tier
  link) and the preflight raw capture also run on a worker thread.
- Every sync fence-lock writer logs `FENCE_LOCK_ON_EVENT_LOOP` (ERROR, with stack) if it runs on an
  event-loop thread, and an AST census test refuses an async function that calls one directly.
- `IngestService.shutdown` cancels the workers first and then releases their leases on a worker
  thread. The release writes the in-flight episode, which an open publish locks, so releasing on
  the loop first could hang a graceful stop during enrichment (#601).

## 2026-11-12 - Graphiti fork 0.31.2.post3: publish fence inside the add_episode publish transaction

- `pyproject.toml`, `uv.lock`: `archolith-graphiti-core==0.31.2.post3`, fork commit `420bebec`. Pins, docs and release checks updated.

## 2026-11-11 - Erasure rebuild, fence foundation: an erase fences in-flight enrichment

- An erase now seals before it deletes. One transaction locks the memory's namespace, records a
  permanent `ErasureMarker` for every id it will remove, and moves a per-namespace erasure epoch.
  It then reads the closure under that lock: the queue node, its twins (linked, reserved or
  back-linked), its turns and their projections. The seal is released after the delete whatever
  the outcome, and an orphaned seal is released on replay.
- Every claim reserves the uuid of the episode it will publish, and the publish links the queue
  node to it inside Graphiti's own save transaction (needs the fork's `publish_fence` hook,
  `archolith-graphiti-core` 0.31.2.post3, not yet released or pinned). A publish whose claim was
  reaped, whose memory was erased, or whose namespace saw an erase after it entered the ingest
  gate is rolled back and requeued without burning an attempt.
- Every queue-node write, the claim, the reaper and the retry scans skip a marked node. Projection
  and self-entity creation refuse while a namespace is erasing and stamp `created_epoch`; a
  typed assertion for an erased episode is not written.
- A claim that finds its queue node already linked finishes from the link instead of publishing
  again. The by-name reconcile, which could adopt another namespace's episode, is gone.
- Only the process holding the scheduler lease runs enrichment workers.
- An erase that meets an in-flight claim from a process without the fence replies
  `erased_incomplete` with `incomplete_reasons: ["unfenced_inflight"]`; every `erased_incomplete`
  reply now lists its reasons.

## 2026-11-10 - Erasure rebuild, first steps: strict targets, scoped merge-audit scrub, sidecar physical erasure

- `delete_memory` accepts only a canonical uuid. Anything else is refused as `invalid_target`
  before any lookup or purge, so a short id like `"e"` can no longer reach the sidecar purge.
- The merge-audit scrub runs inside the caller's namespace and matches exact uuid values in each
  entry's JSON instead of substrings. An entry that names the target but cannot be parsed now
  makes the erase report `erased_incomplete`.
- Every sidecar connection runs with `secure_delete`. An erased subject goes on a permanent
  denylist, and triggers drop later content writes for it, so a late failure or usage row cannot
  bring erased text back. The journal tables are never triggered.
- The purge reads subjects by subquery, so a namespace erase with tens of thousands of members
  no longer fails with "too many SQL variables".
- A new hourly `sidecar_compaction` job runs VACUUM and a TRUNCATE checkpoint, then stamps
  `sidecar_compacted_at` on the erasures it covered. Deleted text is gone from the database
  files only after it runs.
- New counterexample tests for the full rebuild (plan v2) are in place as strict expected
  failures and turn green as each later step lands.

## 2026-11-09 - Erasure removes what a memory derived (#423, #475, #476)

- Erasing a memory now also deletes the facts and entities derived only from it. Shared ones keep
  their other sources: the erased episode is stripped, the item's text (fact, summary, content)
  is blanked at erase time, and it is flagged `rederive_pending`. Recall shows a flagged entity
  by name only and withholds a flagged fact. A new `derived_rederive` job (every 15 min) rebuilds
  each item from its remaining sources, judging a fact by subject, relation and object, or deletes
  a fact they no longer support. With no LLM it skips and the item stays blank. Items with no
  recorded provenance are left alone and the erase reports `erased_incomplete` (#423).
- Entities an erasure may delete are fenced at PREPARE, so a merge holding one fails the erase
  instead of racing it. Replay deletes only the erased nodes and re-checks sharing first.
- Merge-audit entries holding erased content are tombstoned and the survivor is marked
  `merge_irreversible`; unmerge reports `snapshot_erased` (#423).
- Journal snapshots (`graph_operations.before_snapshot_json`, `last_error`) that hold an erased
  node are purged once the operation is terminal. A new `graph_operation_subjects` index, backfilled
  on start, finds them. Terminal ENTITY_DELETE and SESSION_TTL_DELETE snapshots are pruned after
  the diagnostic window. Erasure connections use SQLite `secure_delete` and truncate the WAL (#475).
- An erase that lands while enrichment is running no longer leaves the Graphiti twin and its
  derivatives behind: the worker links and erases them, and a 10-minute sweep recovers a crashed
  worker's twin only when exactly one same-namespace twin matches, an hour after the erase (#476).
  This needed the in-flight queue node to survive as a content-less tombstone, which it never did:
  the delete cascade's later UNION branch saw its own FAILED write and deleted the node, and an
  unfenced LLM-usage stamp could overwrite the erase marker. Both are fixed, erase detection now
  uses the erase-only `erased_at` stamp, and claim, retry and `force_reset_failed_episode` never
  requeue a tombstone.
- New E2E-21 lane (`tests/e2e/test_e2e_21_erasure.py`) proves this through the installed stack
  with real Graphiti extraction, including an erase timed mid-enrichment.
- `delete_namespace` also removes the namespace's TODOs and WorkArtifacts with their unshared
  locations, sources, open questions and declarations; dry runs report `by_label` and
  `not_covered`. The default namespace is still refused (#476; wiping it is #100).
- `delete_memory` says plainly when an erase did not run (`prepare_failed`) or is incomplete.
- Review fixes (PR #595), each first reproduced by a failing test:
  - A queue node is kept as a content-less tombstone while a twin it doesn't cover could exist:
    a repeat erase or replay no longer deletes an unlinked tombstone, and a twin linked after
    the erase resolved its targets is erased right after (the sweep retries on a crash). The
    tombstone no longer keeps the memory's attached `diff`.
  - The reconcile path compensates an erase that lands before `mark_episode_ready`.
  - Derived uuids found during the graph delete are recorded before the telemetry drain, so a
    drain timeout no longer loses them from the replay's purge.
  - When the target is already gone, a pinned caller or one naming a namespace touches only
    that namespace's facts.
  - A busy WAL checkpoint is read, briefly retried, and reported as `erased_incomplete`.
  - Re-derived facts get an embedding when an embedder is available, and a fact listing the
    same episode twice is re-derived instead of failing its compare-and-set forever.
  - E2E-21 adds four criteria for these (12 in all).

## 2026-11-09 - The rerank model cache persists by default (#567 follow-up)

- The container image kept the Hugging Face cache under `HOME=/tmp`, which a read-only
  deployment usually mounts as tmpfs, so every restart downloaded the ~92 MB rerank model again.
  The image now sets `HF_HOME=/srv/menhir/cache/huggingface`, an app-owned `VOLUME` that stays
  writable under a read-only root and survives restarts and Compose recreates. A bind or named
  volume at that path replaces it, and a named volume starts owned by the app user.
- The image also sets `HF_HUB_DISABLE_TELEMETRY=1`, which stops huggingface_hub's agent-registry
  fetch.
- `MENHIR_RECALL_RERANK_CACHE_DIR` (default empty = `HF_HOME`) sets the download cache for both
  the server's background fetch and the hooks' cached-copy lookup.
- Once the model is ready, the server warns if the cache sits on tmpfs.
- `deploy/docker-compose.full.yml` mounts a named volume for the cache.

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
