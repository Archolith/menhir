## 2026-11-13 - Candidates can be reviewed over MCP: list_candidates and review_candidate (#431)

- #431: `add_candidate` staged claims that nothing on a production surface could list, approve
  or reject; only the explorer could, and production does not serve it. Two tools close that:
  - `list_candidates` (readonly, `curate`): candidates awaiting review, newest first, with
    their silo, evidence, content excerpt and notes, under a line marking the text untrusted.
  - `review_candidate(uuid, decision)` (operator, `curate`): `approve` or `reject`.
- A scoped caller cannot approve or reject another silo's candidate. The silo is checked in the
  write statement itself, and the candidate fetch, promote, reject and approve backend
  operations now declare `namespace`, so the internal dispatch forces a pinned client's silo
  onto them as it does for `list_candidates`. A candidate that exists in another silo is
  refused by name; a missing one reads as not found.
- Approval now embeds the candidate's name and writes `name_embedding`, the property vector
  recall reads, with the scope flip. Candidates are created without one. The embed is
  best-effort and bounded at 10 s, and the reply says when it failed. Tests cover the write,
  not a recall that returns the approved memory. A candidate with no summary gets its content
  as one. The reply says when the contradiction check merged the new memory into a
  near-duplicate, and a retry after a timed-out approval says it was already approved.
- Approve and reject lock the candidate before re-reading its scope. Without that, a reject
  queued behind an approve deleted the memory the approve had just made `PERSISTENT`.
- Tool catalog 4. A v3 client policy pinned to an older catalog withholds both tools until
  it is re-pinned (`scripts/update_client_policy.py --profile-catalog 4`). A v1 or v2 policy
  lists every tool, so it refuses at startup until the same script adds the two decisions
  (operators allowed, agent and readonly clients denied). `review_inbox` now points at these
  tools.
- The keyless local default leaves out `add_candidate` and `review_candidate`: approval turns
  unreviewed emitter text into recallable memory, and with both tools the agent that stages a
  claim could approve it itself. `MENHIR_LOOPBACK_PROFILE=operator` restores both; whether the
  default should include either is undecided.

## 2026-11-13 - A keyless local agent gets the core and curate tools, not every tool (#439, #431, #430)

- #439: with no keys configured, an agent connecting to a local install got the operator tier
  and every registered tool, including the scheduler, lease, orphan-recovery, client and
  namespace administration tools. Now a keyless agent gets the `core` and `curate` profiles.
  This applies to both ways an agent connects:
  - The stdio bridge, when it has no backend key, removes every other tool before it starts,
    so `search_tools` can't find them and `call_tool` can't reach them. It also binds the
    allowlist, so the tool gate refuses them too.
  - The backend's `/mcp` and `/mcp-http`, in auth mode none, bind the same allowlist, which
    filters `tools/list` and refuses calls.
- `MENHIR_LOOPBACK_PROFILE=operator` restores every tool. Any other value except `default`
  refuses to start. `/api/*` keeps the operator tier, so the CLI, hooks and explorer are
  unchanged. A keyed caller, and any request #560's guard refuses (a non-loopback Host, or a
  proxy forwarding header), is unaffected. This decides what an agent is offered; it is not a
  boundary against local processes, which can already read the state directory (CT-002).
- #431: the default leaves out `add_candidate`, because nothing on a production surface can
  list, approve or reject what it stages.
- #430: `recall_history`, `recall_timeline` and `get_scalar_view_items` are in `core`, so a
  keyless agent keeps them. `.agent/tasks-mcp.md` now says that a policy client gets them only
  by selecting `core`.
- The E2E harness sets `MENHIR_LOOPBACK_PROFILE=operator` for its lanes. E2E-1 runs the
  default and checks the fresh-install tool list.

## 2026-11-13 - An admission audit can't re-store an erased turn id; each verdict is its own record (#623, #622)

- #623: an admission audit lives in its own namespace, while the turn it names lives in the
  user's, and the turn's erasure locks only the user namespace's fence. An audit written just
  after the erasure committed could store the erased id again, and nothing retired it. The audit
  write now locks the turn's fence and its own fence, in sorted key order, and stores the id only
  if the turn is still live under a fence it holds. Otherwise it keeps the record without the id.
  So an audit either commits before the erasure, which then retires it and strips the id, or
  after it, without the id. This covers all three erasure paths: `purge_namespace`,
  `delete_memory_with_scalar_cascade` and `delete_namespace_with_scalar_cascade`. The fence-key
  rule is now `silo_key_cypher` in `domain/namespace.py`, also used by
  `erase_target_namespace_keys`. An audit that has to wait behind a long erasure is bounded by the
  30 s statement timeout; a timeout is not retried and is logged at WARNING, as before.
- #622: identical denials, most often "no turn_evidence_uuid provided", shared one signature, so
  repeats only refreshed a single node. Each `record_admission_audit` call now has a
  `verdict_id` (stored as `admission_verdict_id`), which is the signature, so every verdict is
  its own record. Retries of one call reuse the id. The signature no longer hashes the turn id.
  Repeated denials now add one node each.

## 2026-11-13 - The release image builds on ubuntu-26.04; a dry run gates every tag (#615, #613, #614)

- #615: on Docker 29's containerd image store, `docker image inspect` gives the manifest digest
  as the image ID, while Syft and Grype report the config digest, so the SBOM check refused every
  build on ubuntu-26.04. A report is now also accepted when it names the config of the manifest
  whose digest is the candidate's image ID, read from the sealed archive with the manifest's
  hash checked. A report naming any other image is still refused, and the refusal now names the
  expected ID, the candidate config and what the report named. Docker v2 manifests in the
  archive index are now read, not only OCI ones. A failed build logs `docker version` and the
  storage driver.
- `release-image.yml` takes a `runner` input (ubuntu-24.04 by default, ubuntu-26.04 allowed;
  any other value fails the first step) and, with `push` false, a `verify` job that reloads the
  sealed archive on a fresh runner and runs publish's pre-login check without logging in.
- #613: `release-image-dryrun.yml` runs that build and verify on both runners nightly, on PRs
  touching `deploy/`, the release workflows, `pyproject.toml` or `uv.lock`, and by hand. It never
  pushes or attests.
- #614: `scripts/release.py` has a tenth check, `image`: the newest scheduled or dispatched dry
  run on HEAD's SHA must be green with build and verify passed on both runners. With no run it
  prints the `gh workflow run` command and refuses; while one runs it waits. No flag skips it.

## 2026-11-13 - Concurrent admission denials no longer drop audit records; Windows flakes fixed (#604, #605)

- Since #600, intake runs in worker threads, so two admission denials for one subject could race
  on the shared audit view's compare-and-set, and the loser's record was dropped with a DEBUG
  log. A lost compare-and-set now raises `ViewWriteConflict` (still a `ValueError`). That covers
  the create path, the unchanged-signature refresh of a View with no contributors, and any
  zero-row write that declares no contributors, even when the diagnosis read fails. The
  provenance-refresh paths still raise a plain `ValueError`.
- The admission-audit writer retries a conflict with jittered backoff, up to 50 attempts or 10
  s. Before each retry it re-checks the denial's turn: an erasure of that turn retires audit
  views naming it and strips the id, so a retry writes the record without the erased id. Intake
  and the temporal path log a write that still fails at WARNING (session id or namespace, and
  the reason; not the user id). On a test Neo4j, four concurrent denials lost three records
  with no retry, and eight lost one in 1 of 5 runs with 5 attempts and no backoff; with this
  change all were kept. Unchanged: denials with identical fields (the same reason and turn id,
  or no turn id) still share one record, as they did when written one at a time.
- #605: none of the flakes is a product race. Sidecar compaction correctly skips an op committed
  in the same ~15.6 ms clock tick it starts in (Python 3.12 on Windows); the test now waits for
  the clock to pass the commit, and a new test pins the tie. The telemetry ordering test saw
  writes in order but a read that gave up its 5 s drain on a loaded runner, the documented
  stale-read fallback from #527; the test now allows 60 s. The erasure drain tests wait for the
  released writer before replaying. The redirect test's server reads the request body before
  answering (likely cause: Windows resets a socket closed with unread input). The lane-overlap
  test checks that each lane sees the other start, instead of a wall-clock budget.

## 2026-11-13 - The erasure fence has one node per namespace (#603, #602)

- The uniqueness constraint on `EvidenceNamespaceFence.namespace_key` was defined but never
  applied, so two first writers in a new namespace could each create a fence and lock different
  nodes: an erase seal and a publish could both proceed. On a test Neo4j, six concurrent first
  writers split the fence in 10 of 10 trials without the constraint.
- Startup now brings the four evidence-lifecycle constraints (fence, publication intent,
  tombstone, projection repair) online on every start, existing installs included, and refuses
  writers if they cannot come online. Duplicate fences are merged first: the survivor keeps the
  highest `erasure_epoch` and `generation` and every in-flight erase op, and the other copies are
  relabelled `EvidenceNamespaceFenceDuplicate` rather than deleted. Duplicate intent, tombstone or
  repair keys stop startup for operator review.
- Every sync statement that writes a fence now runs the on-event-loop guard in the Neo4j wrapper,
  so a writer can no longer miss it. The census test derives fence writers from the Cypher and
  follows them transitively instead of using a hand-written list, and publication-intent `begin`
  and `finalize_remote_outcome` now run off the loop.

## 2026-11-13 - The scalar gate keeps agreement when one sample reads a span twice (#564)

- With aligned spans, a group of overlapping claims where one sample appears twice is now split
  by claim kind, `(value_kind, unit)`, before it is given up. One sample reading
  "pre-approved for $350,000" as both a status and an amount no longer voids the other samples'
  agreement on the amount: 2/3 agreement commits instead of becoming three 1/3 vetoes.
- A kind that one sample still reads twice stays ungrouped, as before. The counting lane keeps
  its split by attribute.

## 2026-11-13 - Recall rerank uses ~90 MB less memory per process

- ONNX Runtime's Python wrapper kept its own copy of the model bytes (~90 MB) for the life of
  the session. The only reader of that copy is its silent CUDA-to-CPU fallback. The reranker now
  turns that fallback off and drops the copy. `MENHIR_RECALL_RERANK_DEVICE=cuda` already promised
  no CPU fallback, and now a CUDA failure keeps the floor for that call.
- Resident memory after scoring went from 282 MB to 194 MB per process (Windows, pinned MiniLM).
  This matters because every agent session's MCP process loads the model. Scoring speed is
  unchanged.
- The CPU memory arena stays on: turning it off saved only ~20 MB and made scoring ~20% slower.

## 2026-11-13 - Explorer is off by default

- `MENHIR_EXPLORER_ENABLED` now defaults to `false`, so `/explorer` is not mounted unless an
  operator turns it on. On a loopback bind the explorer answers without a credential, so leaving it
  on by default put an unauthenticated read and candidate-approval surface on every local install
  (config review M5 F1). Set `MENHIR_EXPLORER_ENABLED=true` to keep using it.

## 2026-11-13 - Loopback servers refuse requests addressed to other hosts (DNS rebinding)

- A web page whose hostname re-resolves to 127.0.0.1 could reach a loopback-bound server from
  the user's browser. It looked like a direct local caller: loopback peer, no forwarding header.
  That reached three credential-free grants: the explorer, the no-auth mode's operator tier on
  `/api` and `/mcp`, and the first-token bootstrap.
- All three now also require the `Host` header to be `localhost`, a loopback IP, or absent.
  Other requests fall through to normal authentication, or get a 401 in no-auth mode.
- Reverse-proxied and token-authenticated requests are unchanged. A local client that addresses
  the server by another name (for example the machine's hostname) needs a credential.

## 2026-11-12 - Saga journal tells the truth: failed merges close, stale writers are fenced, operators can resolve (#427, #404, #406)

- A saga write the server itself rolled back (`ClientError`, `TransientError`) now closes its row
  `FAILED` once a graph re-read shows the op's before-state, releasing the pair fence and no longer
  blocking a `live` boot. A dropped connection stays `PREPARED`: the write may have landed (#427).
  Erasure is excluded: it has no before-state proof.
- Every claim gets its own token (`<process token>-<32hex>`), and every journal mark requires it, so
  a stalled writer can no longer commit or fail a row that was taken over and re-claimed by its own
  process (#404 F-3, #406). New `graph_operation_claims` table and `commit_graph_work` column.
- A writer whose commit is refused after it mutated the graph re-fences the participants and moves
  the row to `NEEDS_REVIEW` when two owners may have applied it, or reports success when exactly one
  application is proven (#404). A replay refused mid-transition is `STALE` and blocks a live boot.
- `clear_needs_review` refuses `PREPARED` and stamps `operator:<name>`; attestation is bound to the
  owner token the operator inspected (#406).
- New `menhir saga list|show|attest|resolve|clear-review` operator commands; see
  `.agent/workflows/operations_runbook.md` for when attestation is true.
- Upgrade every process that shares a journal (server, workers, CLI) together. An older binary
  still makes tokenless marks, reports a stale replay as `LIVE_OWNER`, and can re-open a row to
  `PREPARED` without an owner, so the guarantees above hold only once no older process remains.

## 2026-11-12 - README: erasure known limitation matches what erasure now does

- `README.md` still said erasure leaves extracted entities and facts behind (#423) and that
  `default` cannot be cleared. The bullet now describes provenance-based cleanup and the two
  remaining gaps (unknown provenance, unverified re-derived paraphrases), and points to
  `list_namespace_memories` / `delete_memories` for `default` (#476). Wording follows
  `docs/your-data.md`.

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
