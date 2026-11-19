## 2026-11-18 - Retry a timed-out source-memory embedding (#409)

- Source-memory ingestion retries one embedding timeout before finishing enrichment. A
  subsequent failure leaves the receipt available for the source-embedding repair job.
- Regression tests cover a successful second attempt and a repeated timeout.

## 2026-11-17 - Report truncated startup pins (#674)

- Bootstrap reads report the matching pin total and whether any pins were omitted. MCP
  returns up to 1,000 characters of original pin text and marks shortened items.
- `flag_memory` takes an optional `pin_priority` (0-100, MCP and REST). Startup pins are ordered
  by priority, then the workspace's own pins before general ones, then recency. Unflagging
  clears the priority. Unset pins rank 0, so existing order is unchanged until one is set.
- Each MCP pin carries `pin_scope` and `pin_priority`; `scope` and `type` keep their meaning.
- A workspace read reports `workspace_pin_count`, and `workspace_notice` when the key has no
  pins (often a mistyped key) or the count could not be taken (null, never zero).
- Connected MCP clients see the new `flag_memory` argument only after they reconnect.

## 2026-11-17 - Explicit recall limits can bypass the default rerank cap (#661)

- MCP and REST honor a supplied recall limit up to 50. Omitted limits keep their existing
  defaults, with a note when reranking cuts the result list to its configured cap.
- `recall_context_memories` treats a supplied `limit` the same way and returns the note as
  `relevant_note`. A client newer than its server retries a rejected explicit limit once
  without the flag and says the server may have capped the results.

## 2026-11-17 - Preserve the receipt when enrichment scheduling fails (#663)

- A saved memory keeps its episode ID and reports pending queue recovery. A companion evidence
  scheduling failure is logged separately from the memory's receipt.

## 2026-11-16 - One MCP error shape; no silent coercion; namespace checks and notes (#669, #670, #671)

- Every tool failure is a `CallToolResult` with `isError: true` and `structuredContent`
  `{"ok": false, "tool", "error": {"code", "message", "hint"?}}`. JSON tools put that envelope in
  the text; text tools put the message (refusals unprefixed, faults as `Error: ...`). Codes:
  `invalid_argument`, `not_found`, `outside_namespace`, `refused`, `conflict`, `not_indexed`,
  `disabled`, `write_failed`, `needs_decision`, `bootstrap_required`, `access_denied`,
  `unavailable`, `timeout`, `internal`. Tools raise `McpToolRefusal(message, code=, hint=)`
  instead of returning error strings or hand-built envelopes. Resources keep their shape (#669).
- A failed write says why: `Failed to store memory: <reason>` / `Failed to queue memory:
  <reason>` (`write_failed`). `IngestResult.error` carries the reason from every FAILED path.
- `add_todo` rejects an unknown priority and `list_todos` an unknown status, naming the valid set,
  instead of storing "normal" / listing "open". `list_todos` notes a clamped limit and
  `recall_history` echoes `limit_applied`. `recall_memories(limit<1)` is an error.
  `add_memory(type="TEMPORAL")` without `valid_at` is an error, not an ordinary memory.
  `query_structure` documents `documents` (#670).
- Every write that creates a graph group (`add_memory`, `add_memory_and_track`, `add_candidate`,
  `ingest_document`, `ingest_project`) checks the namespace at call time, after the client pin;
  surrounding whitespace is named. Empty `recall_memories`, `recall_context_memories`,
  `recall_history` and `recall_timeline` results under a namespace that holds no records carry
  a `namespace_note`, with the stored spelling a case or whitespace variant meant (never other
  namespaces for a pinned client). New backend op `get_namespace_status`.
  `list_namespace_memories` defaults to `""` like every other tool (#671).

## 2026-11-16 - Tests that import changed code are selected; rescans drop stale edges (#681, #680)

- Python imports are read with `ast`. Relative imports resolve; `from pkg import sub` links
  `pkg/sub.py` and the package `__init__.py` it executes; every name in `import a, b` is linked;
  a file `ast` cannot parse falls back to a line match that joins continued lines. Stacks are
  detected per directory (a `setup.py` or `requirements.txt` inside a package is not a project),
  so a monorepo with no root `pyproject.toml` gets import and call edges, and a sub-project's
  module names resolve only for importers inside it (#681).
- `blast_radius` / `affected_tests` select a test that imports the changed code directly, or
  through test helpers only, whatever its name; a `conftest.py` (now a test anywhere) selects
  its directory. Tests reached only through other source files stay in the transitive list.
  Duplicate-basename test links prefer the source the test imports, then the mirroring
  directory. `affected_tests` emits one shell-quoted command per stack root and runner
  (`pytest`, `npx vitest run`, `npx jest`, `go test`, `cargo test`, `./gradlew :sub:test` from
  the wrapper's directory, Maven); with no selection it suggests the changed files' own suite,
  and nothing for a language with no manifest. `test_commands` lists the commands (#681).
- A rescan deletes IMPORTS and TESTS edges between scanned files that the scan no longer
  derives, keyed by project identity; edges into files a capped scan dropped, and from files it
  could not read, are kept (#680).
- The per-function call cap counts distinct callees, not call sites.
- `blast_radius` walks reverse imports one hop per query instead of matching variable-length
  paths, which enumerated every path through import cycles: one call on menhir's own graph ran
  16+ minutes; the slowest is now about half a second.
- Scanner schema 12: every stored project is rewritten once on its next scan.

## 2026-11-15 - query_structure answers for the stored path and says what it cut (#679, #683)

- New `domain/structure_paths.py` maps caller spellings (`./a.py`, `src\a.py`, absolute paths
  under the project's recorded root, Git Bash `/c/...`) to the stored repo-relative form. The
  MCP tool and every `StructureGraphWriter` query (`blast_radius`, `affected_tests`, `imports`,
  `tests`, `symbols`, `files`, `context`) use it; the tool echoes each rewritten spelling. An
  absolute path outside the root is reported as not indexed. Before, `src\a.py` passed the
  coverage check but missed the traversal and printed "Affected tests: none found (project
  fully indexed)" (#679).
- `imports`, `tests` and `symbols` for an unknown file now answer "not indexed" (probed with the
  existing `context` query, so older backends need nothing new); an empty `files`/`symbols`
  prefix no longer claims "(project fully indexed)". `affected_tests` lists unindexed inputs
  even when other inputs found tests (#683).
- `blast_radius` reports when the 5-hop import limit cut the trace, when function callers
  (30) or related memories (10) were capped, and renders the open TODOs it already computed
  (#683). MCP clients see the new output text after they reconnect.

## 2026-11-15 - First run: `menhir up` stops without a model; the quick start says what it costs (#685-#688)

- `menhir up` exits 1 when the extraction or embedding row is a MISS instead of serving a server
  that cannot enrich anything; `--allow-degraded` serves anyway. `--check` and the end of
  `menhir setup` count those rows as blocking too (#688).
- README quick start: adding the operator key for the smoke test now also adds an agent key and
  warns that the keyless client gets 401 after the restart (#685). A new "What runs by default"
  section names the ~92 MB rerank download and the default-on anchored-time, scalar and counting
  LLM lanes, and Recall / Retrieval scoring describe the cross-encoder and its 7-result cap
  (#686). The keyless `/mcp-http` tool list is `core`+`curate`, not every tool.
- `.env.example` no longer says Tier 0 works as copied, drops the removed standalone explorer
  host/port, and labels the always-on retrieval lanes correctly; the Docker test stack uses
  `gpt-4o-mini` instead of `gpt-4.1-nano` (#687).

## 2026-11-14 - Hermes reads every namespace and writes only to its own

- `integrations/hermes/menhir/`: the `namespace` setting is replaced by `write_namespace` (default
  `hermes`, validated). Context and recall requests carry no namespace; approved writes always
  carry the write namespace, which also binds pending approvals and the idempotency key.
- The provider now matches Hermes v0.23.1's contract: the tool list no longer changes after
  `initialize()` (a write-enabled profile advertised `menhir_remember` with no route), the wizard's
  yes/no answers are accepted, and `timeout_seconds` is capped at 6 to stay under Hermes' 8-second
  prefetch join.
- Requests send `X-Menhir-Client-Name` from a new `client_name` setting; `menhir_status` reports
  the write namespace, client name and whether a token is configured.
- A refused approved write (HTTP 400/401/403/404/422), a 409 key conflict and a `failed` receipt
  each get their own notice instead of "resend to retry" or "accepted". The idempotency key now
  covers the session, so the same fact approved in two sessions cannot collide.
- `menhir_recall` now returns Menhir's `source_memories` section (raw stored statements) next to
  the ranked results. Menhir ranks a session-scope memory only for the session that wrote it, so
  without this a recent write was invisible from any later Hermes conversation. Found by running
  the provider against a live Menhir 0.3.11.

## 2026-11-13 - Hermes memory writes require a user confirmation turn

- `integrations/hermes/menhir/approval.py` and provider: `menhir_remember` now creates a
  profile-local, expiring proposal; a separate user-authored approval turn must match its random
  ID, original session and optional author ID before the adapter can call `/api/memory`.
  Rejections, bot turns and unknown or expired approvals cannot write; failed approved writes
  preserve the proposal for retry. Automatic turn and transcript syncing remain off.
- `integrations/hermes/menhir/privacy.py` and provider: credential-like content is skipped on
  automatic recall and refused on explicit tools. `menhir_status` checks `/api/ready` without
  revealing credentials; per-session context has a 45-second cache and a 30-second outage breaker.
- `tests/test_hermes_memory_provider.py` and `tests/test_hermes_memory_provider_http.py` exercise
  approval decisions, expiry, namespace/author isolation, retries and real loopback HTTP requests.
- `integrations/hermes/menhir/README.md`, `docs/post-install.md`, `docs/roadmap/` and
  `.agent/architecture.md` describe the two-step write and remaining live validation.

## 2026-11-13 - Hermes Agent can use Menhir as a memory provider

- `integrations/hermes/menhir/`: new standalone Hermes `MemoryProvider` with per-profile connection
  settings, scoped bearer token, bounded automatic context recall, `menhir_recall` and a disabled-by-
  default `menhir_remember` tool. No transcripts, tool outputs or session-end hooks are mirrored.
- `tests/test_hermes_memory_provider.py`: offline registration, safety, config isolation, request,
  read/write, idempotency, failure and no-sync contract tests.
- `docs/post-install.md`, `.agent/architecture.md` and `docs/roadmap/`: install guide and
  implementation boundary. Hermes must reload its provider after setup/config changes.

## 2026-11-15 - v0.3.12: resources follow the client allowlist and pin, billing breakers recover on their own

- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.12. No dependency changes in this commit.

## 2026-11-14 - Billing refusals: the embed breaker guards every embed, recovers on its own, and is reported

- Graphiti now gets an embedder that runs every `create`/`create_batch` through the embed
  breaker (#630). Fused search, the cosine lane and ingest's entity embeddings stopped while it
  is open; search drops to BM25-only without logging the query or a traceback, including on the
  call that trips the breaker. Vectors already in the embedding cache are served before the
  breaker, so a cache hit is neither refused nor counted as a probe.
- While the embed breaker refuses, `add_episode` refuses before Graphiti's LLM extraction runs,
  so a requeued episode no longer pays for extraction on every attempt. The worker requeues it
  for the breaker's remaining cooldown.
- Each breaker tags the errors it raises. An outer breaker ignores another breaker's errors, so
  an embedder billing refusal inside `add_episode` opens the embed breaker, not the LLM one
  (#493). Only explicit `__cause__` chains count; a probe that hits another breaker's error
  restarts the normal cooldown. A billing refusal that lands while a probe runs keeps the
  billing hold and the probe slot.
- A billing hard-open probes after 5 minutes instead of an hour. A successful probe closes it;
  a probe that fails for another reason ends the billing hold and uses the normal cooldown
  (#492: no operator reset tool; recovery after a top-up takes up to 5 minutes).
- Recall's note says why semantic search is off, for example `semantic search off: embedding
  provider refused for billing (next probe in 4m); results are keyword-only` (#632).
- `/readyz` gives a token holder a `provider` block: `degraded` codes, the quota and auth
  summaries, and the breakers hard-open for billing (#631). `status` and the status code do not
  change; every deploy readiness check requires `status == "ready"`.

## 2026-11-14 - SESSION scope is a relevance scope, not access control

- Reverts the SESSION-row filter that #635 added to `memory://recent`, `scope`, `type` and
  `node`. It covered only those four resources: the context loaders, recall history and
  timeline, and the internal dispatch's memory reads return SESSION rows from any session. The
  session id is caller-chosen under static-key and loopback auth. It also hid a stdio client's
  own unconsolidated rows after a restart, because each process gets a new session id.
  `docs/your-data.md` now says SESSION is a relevance scope and that namespaces separate
  clients. The internal-dispatch restrictions from #635 stay.
- The internal dispatch counts a call to a deprecated operation as admitted only after the
  client restriction passes; a refused call is counted as refused. The admitted count gates
  removing `write_project_structure`.

## 2026-11-14 - SESSION rows stay with their session in resources; the internal dispatch refuses what it cannot scope

- `memory://recent`, `memory://scope/{scope}` and `memory://type/{type}` drop SESSION-scoped
  rows from other sessions, and `memory://node/{uuid}` reads one as not found. This is the
  rule recall applies. A caller with no session id sees no SESSION rows. The listings filter
  after the 10-row fetch, so they can return fewer than 10.
- `/api/internal/backend/{operation}`:
  - Refuses every operation to a client with a tool allowlist (client policy or
    `MENHIR_CLIENT_TOOLS`): it names backend operations, not tools, so it cannot apply one.
  - For a pinned client, keeps every operation that takes `namespace` (the pin is forced, as
    before) and the deployment-wide stats `get_memory_stats` shows. It refuses the rest, which
    address objects by uuid or read every silo; the tools that wrap them check ownership in
    the tool, which a direct call skipped.
  - Behaviour change: a pinned or allowlisted client running the stdio server against a
    remote backend loses those operations and should connect over MCP instead. Production
    does not mount this route.

## 2026-11-14 - MCP resources follow the client's tool allowlist and namespace pin

- Each MCP resource now names a governing tool, and a client may read it only if the client
  policy (or `MENHIR_CLIENT_TOOLS` without one) allows that tool. Resources also check client
  identity first, as tools do. `memory://recent`, `scope`, `search` and `type` follow
  `recall_memories`; `memory://node/{uuid}`, whose detail row carries processing state and
  session and user ids, follows `get_enrichment_status`; `system/metadata` and
  `system/dependency-health` follow `get_memory_stats` and now need operator tier, since they
  return infrastructure endpoints no tool returns; `system/lifecycle-trace` follows
  `get_episode_trace`; `system/processing-queue` follows `list_enrichment_queue`. Behaviour
  change: a client restricted to the `core` profile (including the keyless loopback default),
  or to a short `MENHIR_CLIENT_TOOLS` list, can no longer read `memory://node` or the
  `system/*` resources.
- A client pinned to a namespace is refused `system/lifecycle-trace` and
  `system/processing-queue`, which have no per-namespace filter, and gets `system/metadata`'s
  graph counts for its own namespace only. The lifecycle trace withholds tracebacks below
  operator tier, as `get_episode_trace` does.
- A production client policy that sets a non-empty `namespace` on a client now refuses to load.
  Nothing enforced that field; pinning is `MENHIR_CLIENT_NAMESPACES`.

## 2026-11-14 - Release gate: preflight in seconds, and `--tag` reuses a fresh check run (#606, #607)

- #606: `scripts/release.py` now runs `preflight` first. It checks only what the selected checks need: ruff for lint; an absolute `MENHIR_TEST_BEACON_PYTHON` that imports Beacon for offline and online (the online lane runs the Beacon E2E test); a reachable test Neo4j (3 s connect timeout) for online; `gh auth` for ci, image and `--tag`. It reports every problem at once with the command that fixes it, and the Beacon pin is read from `tests.yml`. A workstation missing them is refused in seconds, instead of after a 7-minute offline lane. Preflight checks that Beacon imports, not that it is at the pinned commit.
- #607: a full passing check-only run writes `.release-gate/<tag>.json`. `--tag` on the same tag, HEAD, interpreter, Beacon interpreter and test Neo4j URI, within two hours of the check run's start, reuses that receipt's offline and online results and reruns every other check, including tree, branch, ci and image. Any run that executes either slow lane deletes the receipt first, so a later failure is never covered by an earlier pass. No receipt is written if HEAD or the tree changed during the run. `--fresh` ignores the receipt, and a release deletes it. The tag now names the checked commit explicitly, instead of whatever HEAD is when it is created. `docs/runbooks/release.md` describes both.
- Tests: `tests/test_release_gate_preflight_receipt.py`.

## 2026-11-13 - v0.3.11: candidate review over MCP, tool catalog 4

- Ships every entry below back to v0.3.10. Deploy-relevant:
  - Tool catalog 4 (`list_candidates`, `review_candidate`, #431). A v3 policy pinned to catalog 3 withholds them until it is re-pinned. A v1 or v2 policy refuses to start until `scripts/update_client_policy.py` adds the two decisions.
  - Startup applies the evidence-lifecycle uniqueness constraints (#603). Duplicate fences are merged. A duplicate intent, tombstone or repair key refuses writer startup.
  - A keyless loopback agent gets core and curate, without `add_candidate` and `review_candidate` (#439). The explorer is off by default. Loopback servers refuse a non-loopback Host.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.11. No dependency changes in this commit.

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
