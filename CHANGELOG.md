## 2026-09-16 - P2A staging receiver: the real chunk handler, off by default

The instrument the transport measurement runs against. It exercises the real
begin/chunk/status/abort path -- same middleware, auth, parser and telemetry a release would
use -- and stops at SEALED. No extraction, no manifest read, no graph; a test reads the module's
AST and fails if it so much as imports `zipfile` or anything graph-shaped.

- `menhir.snapshot.receive`: filesystem-backed staging. Ownership is bound at creation and
  re-checked on every load, and a foreign or guessed id reports NOT FOUND rather than forbidden.
  Bounds are enforced before allocation and again after decode. An exact chunk replay is a no-op;
  the same index with a different digest fails the upload, because that is not a retry -- client
  and server disagree about what is being uploaded.
- Records hold ids, counts, digests and timestamps, and nothing derived from content. The chunk
  tool overrides `call_payload` so telemetry records the upload id, index, byte count and digest
  -- the default would have recorded the base64 of a user's source file into every row.
- State is durable and never inferred from a directory: an upload resumes across a restart, and
  staged bytes with no readable record are reclaimed rather than adopted.
- Quotas are the approved pilot figures (2 per principal, 8 per project, 24h inactivity TTL, 1h
  terminal retention, disk budget), kept out of `SnapshotLimits` because that ships to clients.
  A begin is refused ahead of exhaustion, counting what the upload could add.
- Four MCP tools, operator tier, registered only while `MENHIR_SNAPSHOT_RECEIVE_MODE=staging`,
  so while off they are neither advertised nor invocable. Each endpoint re-checks the mode at
  call time as well; anything but `staging` fails closed. P2B replaces this env read with the
  registered feature flag and adds commit.

## 2026-09-16 - risk-based test workflow for a large suite

- Replaced routine local full-suite guidance with one authoritative risk-based workflow:
  direct regression tests first, affected callers/contracts next, and subsystem expansion when
  shared, destructive, security, concurrency, migration, or test-infrastructure risk requires it.
- Made `affected_tests` advisory rather than sufficient. Empty or stale structural mappings must be
  checked against callers, contracts, and existing tests before deciding that no test is needed.
- Assigned complete offline and supported graph-backed integration coverage to required CI on the
  exact reviewed SHA. Local full runs remain available for justified collection/fixture/foundational
  changes, but are no longer a per-change closeout ritual.
- Added a required verification receipt covering changed surfaces, selection basis, exact commands
  and outcomes, unrun lanes, exact-SHA CI state, and residual risk. Updated repository agent
  directions, maintenance routing, README guidance, and living work plans to use the policy.

## 2026-09-16 - snapshot provenance: a commit id is not a statement about bytes

`snapshot.json` carried `source_head` alone, which cannot answer the question that decides
whether a code memory is grounded: were these the bytes at that commit, or bytes someone was
still editing? A commit id reads as the former and is frequently the latter.

- Replaced by a `provenance` object: `base_commit`, `commit_tree` (the commit's tree OID, so a
  server holding the commit's objects can compare them against what arrived), `branch` (absent
  when detached), `dirty`, and `quality`.
- `quality` is always `self_reported` from a client and is forced back to it on parse. A client
  claiming `trusted_automation` is precisely the claim the field exists to refuse; only the
  server may raise it, from forge or CI evidence.
- `dirty` describes the source checkout, not the bundle, and is computed with
  `--untracked-files=no`: untracked files never enter the bundle, so counting them would mark
  nearly every working repository dirty for content it did not send. Staged changes do count.
  `dirty=false` still does not mean the bundle equals the commit -- excluded paths and deletions
  are declared separately and a reader must consult them.
- `tree_digest` is unchanged and stays independent of every label. It remains the only statement
  in the manifest about the bytes actually uploaded.
- `menhir sync --check` now prints the base commit, branch, and a plain-language clean/dirty line.

## 2026-09-16 - structure prunes address the identity they were authorised for (#99)

Every structure prune matched `structure_project` -- the caller-supplied display name -- while
the identity gate validated `project_id`, `root_key`, generation and host. The gate guarded the
front door and the deletes used a different address, so nothing established that the rows about
to be DETACH DELETEd were the rows the gate authorised. Two projects sharing a display name
pruned each other; a negative control against the old predicate deletes both projects' rows where
the fix deletes one.

- All prunes, plus the mtime read-back, now match on identity: rows stamped with this
  `structure_project_id`, plus rows carrying no stamp at all under this display name (written
  before CF-257). A row bearing a *different* project's id is unreachable by either arm. Two
  indexed lookups rather than one `OR`, which would plan as a label scan over every `:Entity`.
- **Renames work.** `get_file_mtimes` was name-keyed, so a renamed project read back zero mtimes,
  took the first-scan branch, and orphaned every entity under its old name while the name-keyed
  prunes matched nothing.
- **Two more instances the issue did not list**, both in `_write_symbols`: the incremental symbol
  delete, and a full-replace symbol delete carrying no path filter at all -- keyed on the name
  alone, any forced or first scan emptied every same-named project's symbols. A sweep of `src/`
  for the same pattern now finds none.
- An id-less scan keeps the old name-only behaviour. It never passed an identity gate, so there
  is no validated identity to diverge from; that is the residual, and it closes when `project_id`
  becomes mandatory on the write path.
- Consequence worth knowing: `identity_action="new"` no longer prunes the superseded silo, since
  those rows belong to an identity the scan was not authorised to touch. `adopt` remains the way
  to continue a project.

## 2026-09-16 - `menhir sync --check`: see what a remote structure sync would upload

First two phases of MCP-bundled snapshot ingest
(`.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`). Nothing uploads yet; this is the
local half.

- `menhir sync --check` reports what a sync would send from a git repository: file count,
  content bytes, an archive upper bound and chunk count, the `tree_digest`, deletions, declared
  omissions, and refusals. It makes no network call and writes no archive. Plain `menhir sync`
  refuses with an explanation -- the MCP upload tools are a later phase, gated on measuring the
  real transport.
- `menhir.snapshot.protocol`: the frozen wire contract. Canonical manifest, `tree_digest` over
  the ordered file records, path rules (relative POSIX only; absolute, `..`, drive/UNC,
  backslash, control characters and Windows-reserved names are refused, never repaired), and
  stable error codes. Rejected paths never appear in an exception message, so a server logging a
  refusal cannot thereby log a source path.
- `menhir.snapshot.policy`: include / omit / refuse. Directories the structure scanner skips are
  imported from it rather than restated, so the two cannot drift. Real `.env` files and private
  key material block a sync until overridden per run with `--allow-path`; `.env.example` and
  friends are fine.
- `menhir.snapshot.bundler`: tracked paths and file modes from the index, bytes from the working
  tree -- so uncommitted edits to tracked files are visible and the repository is never written
  to. Submodules, symlinks, oversized files and scanner-skipped paths become *declared*
  omissions: the server cannot tell "never uploaded" from "deleted upstream" by looking at the
  extracted tree, and structure writes prune.
- Archives are byte-reproducible: fixed entry order, pinned timestamp and deflate level, modes
  from the manifest.

## 2026-09-15 - test Neo4j default is 127.0.0.1, not localhost

- `tests/conftest.py` and the five test modules that repeat the default: `MENHIR_TEST_NEO4J_URI`
  now defaults to `bolt://127.0.0.1:7688`. The throwaway container publishes on IPv4 only, so
  on Windows every driver connect to `localhost` waited out a ~21s `[::1]` timeout before
  falling back -- about 20s per online test, a two-hour lane instead of three minutes.
  Explicit `MENHIR_TEST_NEO4J_URI` values are unaffected.

## 2026-09-15 - release gate script

- `scripts/release.py`: the workstation half of the release gate. Check-only by default;
  `--tag` creates and pushes the annotated tag only after all nine checks pass in the same
  run (clean tree, `main` in sync with origin, pyproject version == tag and tag unused and
  newest, CHANGELOG heading, no VCS dependencies, the exact two `ruff` commands from
  `tests.yml`, offline suite, online suite with an unreachable test Neo4j counted as failure,
  and `tests.yml` green on HEAD's SHA), then watches `Publish to PyPI` through its `test-gate`
  and confirms the version on PyPI. No flag skips a check and still allows `--tag`.
- `docs/runbooks/release.md`: the release procedure and why the gate has two halves.

## 2026-09-15 - v0.2.3 hotfix: recovery sweep NameError, release test gate, ingest denylist

**Upgrade from 0.2.1/0.2.2 promptly.** Both earlier releases shipped a `NameError`:

- **The enrichment worker died on its first idle poll in 0.2.1 and 0.2.2.**
  `fail_transient_exhausted_pending_episodes` referenced `LLM_RESET_SET` without importing
  it, so every lease-recovery sweep raised before reaching the database. The sweep runs from
  the worker's idle-timeout path with no enclosing handler, so the worker task died (it was
  restarted on the next ingest, which is why ingestion appeared to work). Stale-lease
  recovery, orphan reset, and transient-exhausted parking never ran, startup resume failed
  with a warning, and the scheduler's recovery job errored every tick. Fixed by importing the
  name; `tests/test_transient_exhausted_recovery_live.py` now exercises the real adapter path
  (repository method, startup resume, recurring sweep) against a live Neo4j.
- **Publication is now gated on green tests for the exact tagged commit.** `tests.yml` was
  red on both v0.2.1 and v0.2.2 (its `ruff --select F821` lint caught the undefined name) and
  `publish-pypi.yml` published anyway. A `test-gate` job now resolves the tag to its peeled
  commit (annotated tags resolve to a tag object first) and refuses to build unless every
  `tests` run for that SHA is completed and green.
- **Ingest denylist hardening.** `src/menhir/core/ingest_guard.py` refuses `logs/`,
  `backups/`, and `.git/` anywhere in the resolved path for every tier, including a
  configured root inside one of those trees. Other dotfiles are checked at the artifact and
  its immediate parent (operator/no-auth) or at and below the configured root (confined
  tiers), so a checkout under an incidental hidden ancestor stays ingestable.
  `docs/security-posture.md` and `.env.example` updated to match.

Version bumped to 0.2.3.

## 2026-09-15 - v0.2.2 hotfix: the v0.2.1 wrapup-review findings

The v0.2.1 wrapup review caught one shipping-broken fix and three same-class gaps:

- **#79 was not actually fixed in v0.2.1.** The transient refund query used `greatest()`,
  which Neo4j Cypher does not have — every call raised, so attempts were never refunded and
  the transient counter never moved. The refund now uses `CASE WHEN`, proven by a new
  `--run-online` test that executes the real query against a live Neo4j (the instrument gap:
  the v0.2.1 tests ran against fakes that never compile Cypher). The three refund call sites
  also route through a best-effort helper so a refund failure can never escalate a requeue
  into a failure path.
- Re-running `menhir hook install` replaced the whole settings entry when it held a menhir
  command, dropping third-party commands co-registered with it — the install-side twin of
  the #113 uninstall bug. Reinstall now refreshes only menhir's commands.
- `MemoryRequest.diff` was still unbounded while `episode` was bounded in v0.2.1; diff now
  carries the same MAX_DIFF_CHARS (50,000) bound and returns 422 above it.
- Hook uninstall's "kept N third-party hooks" count included commands that were never at
  risk; only commands rescued out of a stripped menhir entry count now.

Version bumped to 0.2.2.

## 2026-09-15 - v0.2.1 release fixes (#113, #97, #83, #87, #81, #79/#70, #78, #76)

Eight fixes for issues a fresh `pip install archolith-menhir` user can hit or that lose data:

- **#113** `menhir hook uninstall` no longer deletes co-registered third-party hooks. Removal
  is per command: a settings entry survives while any non-menhir command remains in it, the
  event key only goes away when no entries are left, and uninstall reports both counts
  ("removed N menhir hooks; kept M third-party hooks"). Same fix in the installer's
  re-registration cleanup (`_remove_managed_hook_entries`).
- **#97** The two ungated structure prunes -- the incremental mtime-diff file prune and the
  stale-directory prune -- are now gated on `not scan.partial_index` like the four existing
  destructive prunes. A scan truncated by the 2000-file cap no longer deletes every eligible
  file the cap dropped; a skip logs at INFO.
- **#83** The ingest allowed-root default is gone. The old default was the server's working
  directory, so an agent-tier credential could read the server's own tree (config, logs,
  `.env`). `agent`/`readonly` ingest now requires an explicit `MENHIR_INGEST_ALLOWED_ROOTS`
  and is refused with the setup message when it is unset; as a second layer, dotfiles and
  dot-directories (`.env*`, `.git`) and `logs/`/`backups/` paths are denied for every tier,
  operator included. Zero-config local development (no keys configured) is unchanged.
- **#87** `MemoryRequest.episode` is bounded at 48,000 chars (the default enrichment
  preflight exactly: 12000 estimated tokens at ~4 chars/token), min 1. Oversize bodies now
  return 422 at the API instead of being accepted and preflight-rejected later.
- **#81** Scheduler-bound settings are validated at startup instead of silently accepted:
  `*_INTERVAL_S` values below 1.0 and consolidation K / call budgets / ingest concurrency /
  frontier top-K below 1 raise the named env-var error (same family as the parse errors).
  `MENHIR_STRUCTURE_WATCHER_INTERVAL_S=-30` no longer busy-loops the scheduler gate;
  `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_K=0` no longer no-ops consolidation.
- **#79 / #70 item 2** Transient outages no longer burn the enrichment retry budget.
  `processing_attempts` now counts only genuine failures: a retryable provider fault, a
  circuit-open requeue, and a backpressure requeue each refund their claim's attempt and
  ride a separate `transient_retries` counter capped at 20 -- so an outage of any length
  that ends can never park an episode by itself, while a permanently dead provider still
  terminates (scheduler refuses at the cap; the pending list skips; recovery parks as
  `pending_transient_exhausted`). `manual_review` handling is unchanged.
- **#78** Candidate approval ran its contradiction check without a namespace, so it always
  searched the default one. `fetch_candidate` now projects
  `coalesce(n.namespace, n.group_id) AS namespace` and approve() carries it into the check.
- **#76** The belief scorer's sigmoid saturates instead of raising: `log_odds` is clamped to
  [-700, 700] so terminal evidence scores to 0/1 rather than `exp()` overflowing.

Version bumped to 0.2.1.

## 2026-09-15 - operator literals and the legacy identity header removed

- `yawn-neo4j` appeared in three user-facing places -- the Neo4j-unreachable message in both
  the runtime and the MCP resource, and the `--neo4j-container` default -- naming a container
  that exists on one machine. Menhir's own compose file calls it `menhir-neo4j`, which is what
  they now say.
- `_KNOWN_PORTS` shipped one operator's project names (`yawn.rip`, `yawn.dashboard`) plus
  `cth.mcp.scheduler`, an integration removed months ago, as though they were general
  knowledge; every other installation got wrong attributions for ports it happened to use. It
  is empty by default now.
- The deprecated `x-yawn-*` identity headers are no longer accepted, on either the ASGI
  middleware or the REST path. Identity should have one spelling: two meant a caller had two
  ways to say who it was and every trust gate had to cover both. No client config in this
  workspace sends them.
- The server stops emitting `x-yawn-bg-warnings`; `backend_client` still *reads* it, so a
  current client keeps working against a server that has not been redeployed yet.
- `ProviderKind.LOCAL` and the README env table said "local" where they meant any
  OpenAI-compatible endpoint, hosted gateways included. The identifier stays (it is in every
  existing `.env`); the descriptions no longer mislead.

## 2026-09-15 - the MCP path, documented from a live server

- The README's MCP section showed only a config with `Authorization: Bearer <your-key>`, a key
  `menhir setup` never writes -- the same dead end the operator-key instruction had. Verified
  against a running server: with no credentials configured Menhir binds to loopback and accepts
  MCP requests with no header at all, so that is now the documented default, with the keyed
  config beside it as the step to take for anything beyond a single-user install (and the
  command to generate one).
- Added an MCP check that needs no client: the transport is stateless Streamable HTTP, so a
  single `curl` of `tools/list` lists the tools (40 at agent tier in 0.2.0). The REST path had a
  copy-paste smoke test from the start; the MCP path had no way to prove it worked.
- Documented the stdio bridge (`python -m menhir.mcp.server`) alongside the HTTP transport.

## 2026-09-15 - cold-start run #6 fixes

- `menhir up --help` said "Bring Menhir up from a checkout", which stopped being true when the
  state-directory fallback landed. A cold-start evaluator read it as meaning `--compose-neo4j`
  needs a clone, then ran it from a plain `pip install` anyway and it worked.
- The tier report's embeddings row says when the endpoint did not list the model at
  `GET /models`, instead of printing a bare `[ok]` beside a startup log saying the opposite.
  Readiness is unchanged -- hosted gateways do serve models they omit from that list -- but the
  two health signals no longer appear to contradict each other.

## 2026-09-15 - MCP tool/resource precompute failures no longer escape undiagnosed

- `BaseTool.execute()` and `BaseJsonResource.execute()` evaluated `self.call_payload(...)`
  and `self.timeout_for(...)` as call arguments to `track_mcp_call(...)`, outside its own
  try/except. A bug in either one skipped `_diagnose_failure`, telemetry recording, and the
  tool/resource's own `error_mapper` entirely, reaching FastMCP/the MCP SDK's generic
  fallback -- the "-32603 Internal error, no way to diagnose" failure mode. Real, reachable
  case: several `timeout_for` overrides (`force_reenrich`, `get_enrichment_status`,
  `watch_enrichment`) do `int(timeout_s)` on caller-supplied input, which raises on
  `inf`/`nan`. Both computations now run through `_safe_precompute`, which logs the full
  traceback and falls back to a safe default so the call proceeds through the normal,
  protected path instead of raising raw past it. See
  `tests/mcp/test_precompute_exception_stays_diagnosable.py`.

## 2026-09-15 - published to PyPI

- `archolith-menhir` 0.2.0 is on PyPI. The quick start is now `pip install archolith-menhir`
  then `menhir up` -- no clone, no build tooling, and no Git, since both first-party
  dependencies are PyPI releases too. The clone path moves to an appendix for people working
  on Menhir rather than installing it.
- The smoke test locates `.env` through `MENHIR_STATE_DIR` (default `~/.menhir`) rather than
  assuming the working directory, which only held for a checkout.

## 2026-09-15 - setup and up work without a source checkout

- `menhir setup` and `menhir up` no longer require a cloned repository. Installed from a wheel
  they configure `MENHIR_STATE_DIR` (default `~/.menhir`): `.env` is generated there from the
  same key tables the checkout path uses, `--compose-neo4j` writes the bundled Neo4j compose
  definition next to it, and the Git-hook step is reported as skipped rather than failing.
  Until now every documented first command answered "Menhir setup requires a source checkout"
  on a pip install, which would have made a PyPI release unusable.
- `--repo PATH` still requires a real checkout: naming a path is a statement about where it is,
  so a wrong one says so instead of silently configuring somewhere else.
- Bare `menhir` outside a checkout reports readiness against the state directory instead of
  printing help.

## 2026-09-15 - no dependency resolves from git any more

- `archolith-oauth==0.3.1` replaces its `git+https` pin, published to PyPI from the tag of the
  same commit Menhir pinned. With `archolith-mcp-framework==0.2.0` already swapped, both
  first-party dependencies are ordinary PyPI releases.
- The release wheelhouse is now hash-verified end to end. `uv export` no longer excludes the
  two first-party packages (a VCS requirement cannot carry a hash; a PyPI release can), and the
  separate unhashed `pip wheel git+https://...` step is deleted -- nothing in the release job
  fetches from a repository. Verified locally: `pip wheel --require-hashes --only-binary=:all:`
  builds all 92 wheels, both first-party wheels included, with no VCS access.
- `deploy/Dockerfile`'s contract comments corrected: `--no-index --no-deps` is about keeping the
  image build hermetic, not about git URLs in metadata, which no longer exist.

## 2026-09-15 - framework installs from PyPI

- `archolith-mcp-framework` is pinned as `==0.2.0` from PyPI instead of a `git+https` URL at
  commit `0a7c300`. Same code, verified rather than assumed: the published 0.2.0 wheel and the
  git tree at that commit hash identically across all 17 modules, and the reinstalled package
  carries no `direct_url.json`, so it really comes from the index. Upgrading past 0.2.0 is a
  separate decision tracked in issue #110.
- `archolith-oauth` still installs from git: it is not on PyPI yet. Until it is, `git` remains
  a build requirement and the wheelhouse still resolves one dependency from GitHub.

## 2026-09-15 - hooks deliver context again

- `wrap_hook_response` put `additionalContext` at the top level of the hook envelope, where
  the harness parses it, reports the hook successful, and ignores it. It must be nested under
  `hookSpecificOutput` with the event's name. Evidence: a session transcript with 62
  UserPromptSubmit hook firings delivered zero Menhir context, while every other hook using
  the nested form delivered on each call. Flagged memories, TODOs, temporal reminders and
  post-compaction recall were all being dropped.
- Post-compaction recall moved from `PostCompact` to `SessionStart`. Compaction has no context
  channel at all -- the harness's union has no PostCompact variant -- but SessionStart fires
  immediately afterwards with `source="compact"`. The handler checks that source, so it still
  runs only after a compaction and not on startup/resume/clear.
- `menhir hook install` now registers the post-compaction command on SessionStart and prunes
  the old PostCompact entry; uninstall still sweeps PostCompact so pre-move installs are
  cleaned up rather than stranded.

## 2026-09-15 - smoke-test operator key, corrected

- Cold-start run #5 (first run able to execute `menhir up --compose-neo4j`: a nested Docker
  daemon, so the documented one-liner was finally reachable). It worked end to end, and
  disproved a README line added the day before: `menhir setup` writes no `MENHIR_OPERATOR_KEY`
  line at all -- `.env.example` names it only in a comment -- so "set the empty line" sent the
  reader looking for something that does not exist. The smoke test now gives the exact append
  command and reads the key back with `grep | cut`.

## 2026-09-14 - cold-start run #4 doc fixes

- Findings from cold-start run #4 (end to end in 8.8 min, smoke test matched the documented
  output; no code changes needed). README: `--compose-neo4j` now says it needs the Docker
  daemon where `menhir` runs and what to do when Menhir is itself in a container; the smoke
  test says to set `MENHIR_OPERATOR_KEY` in `.env` first and how. `deploy/README.md` no longer
  claims the release image builds from a plain clone -- `deploy/Dockerfile` installs only
  from the pre-built wheelhouse, as the root README already said.

## 2026-09-14 - one path to a running server

- `menhir setup` now ends with the same tier report `menhir up --check` prints and exactly one
  next command (`menhir up`); three cold-start evaluators read its old "configure .env, check,
  serve" hint and never tried `up`.
- Bare `menhir` (no subcommand) inside a checkout runs the readiness check instead of printing
  help; outside a checkout it still prints help.
- README quick start is a single path: prerequisites, install and start with `menhir up`,
  connect a client, REST examples, smoke test. The manual Install / Configure / Start Neo4j /
  Check-and-run steps move to a "Step by step (what `menhir up` does)" appendix.

## 2026-09-14 - smoke test that actually cleans up; minimum extraction model

- Findings from cold-start run #3. `DELETE /api/memory/{id}` removes the episode and its
  projections but keeps the entities it produced (shared knowledge, by design); the README's
  smoke test wrongly claimed a cascade. It now writes into a throwaway namespace and tears it
  down with `DELETE /api/namespace/{ns}` (operator tier, dry-run first).
- `POST /api/memory?wait=true` reports `entities_linked` on a `ready` write, counted on the
  Graphiti episode the anchor resolved to (counting on the anchor always read 0). `ready` with
  `entities_linked=0` is the "model extracted nothing" case that was invisible before.
- Minimum recommended extraction model is `gpt-4o-mini` class. Measured on OpenRouter:
  `openai/gpt-4o-mini` extracted 5/5 test sentences (one prefixed "SMOKE TEST:");
  `openai/gpt-4.1-nano` returned zero entities 6/9 on ordinary sentences. `.env.example`
  defaults and the OpenRouter example now name `gpt-4o-mini`; README examples use a
  person-plus-action sentence.

## 2026-09-14 - cold-start evaluation #2 fixes

- `POST /api/memory?wait=true` now reports the terminal processing state -- `ready`, or
  `failed` with `error` and `retry` (`retryable` / `manual_review` / `terminal`) -- and
  `timed_out` when the wait elapsed. It previously answered `queued` even when enrichment had
  already failed for good; the only trace was in the server log.
- The README's REST example is a two-entity sentence with an explicit relation, and the text
  explains that a relationless fragment is refused by design (`relationless_extraction`).
- README's `uv` route uses `uv venv --seed` so the venv has `pip`.
- README gains a "Smoke test, then clean up" sequence (write a marked memory with `wait=true`,
  recall it, delete it by `episode_id`), and `menhir up` points at it before handing off to serve.
- Graphiti's `EquivalentSchemaRuleAlreadyExists` downgrade is installed process-wide by
  `configure_logging`; the previous filter was scoped to `build_indices_and_constraints`, but
  Graphiti's constructor fires the same errors from a background task before that call.

## 2026-09-14 - point newcomers at `menhir up`

- `menhir setup` now ends by recommending `menhir up --check` / `menhir up`; its previous hint
  (configure .env, check, serve) steered a cold-start evaluator past the one-command path
  entirely. README shows the `menhir up` variant for an existing Neo4j / no Docker.

## 2026-09-14 - cold-start evaluation fixes

Findings from a fresh Sonnet agent installing Menhir on Debian from the README alone.

- `menhir check` and `menhir diagnostics` now read `.env` like `serve`; the documented
  diagnostics -> check -> serve sequence reported stale defaults after `.env` was configured.
- `POST /api/recall` defaults `include_session` to true, matching every MCP recall tool. The
  first-run check -- write one memory, read it back over REST -- returned nothing until the
  evaluator found the flag in a source comment.
- Neo4j `01N52` "property key does not exist" notifications are filtered at the
  `neo4j.notifications` logger, so Graphiti's own driver is covered on a first write, not only
  Menhir's drivers.
- README: minimal images ship no `python3`; the deploy Docker stack needs a release-built
  wheelhouse and is not a from-clone path; REST write/read curl examples; configured tier keys
  must be distinct (also in `.env.example`); `post-install.md` notes which commands read `.env`.

## 2026-09-14 - hosted OpenAI-compatible gateways (OpenRouter) work as the `local` provider

- `GET /models` is treated as authoritative only for a loopback server. Hosted gateways route
  models they do not enumerate (OpenRouter serves `openai/text-embedding-3-small` without
  listing it), so a missing name there is now a warning verified on first call instead of a
  preflight failure that forced `degraded_queue_only`. Preflight wording no longer assumes
  llama.cpp. `.env.example` shows the OpenRouter block.

## 2026-09-14 - quieter first boot, honest MCP serverInfo

- Menhir's Neo4j drivers disable the UNRECOGNIZED notification classification (Neo4j's
  ``01N52`` "property key does not exist"), which fired dozens of WARNING lines on an empty
  graph and never meant anything; every other classification stays visible.
- Graphiti's ``EquivalentSchemaRuleAlreadyExists`` errors during ``build_indices_and_constraints``
  -- its index shapes collide by (label, property) with Menhir's under different names, so
  ``IF NOT EXISTS`` cannot help -- are downgraded to DEBUG for the duration of that call only.
- MCP ``serverInfo.version`` now reports Menhir's package version; the SDK's ``FastMCP`` has no
  version parameter and was advertising the ``mcp`` package's own (``1.30.0`` for a ``0.2.0`` build).

## 2026-09-14 - Menhir reads one .env, and a refused Neo4j password is named as such

- `python-dotenv`'s path-less `load_dotenv()` walks up from the *calling file* whenever the
  entry point has a `__file__` (`python -m menhir.cli`, the installed `menhir` script), so
  `graphiti_core.helpers`' import-time call loaded whatever `.env` sat above `site-packages`
  and Menhir's own `load_dotenv(ENV_FILE or None)` loaded the one above `menhir/cli`. Keys the
  operator's `.env` left unset were silently filled from a bystander file (seen as
  `GRAPHITI_EMBED_PROVIDER=openai` in a deployment configured `local`). Menhir now resolves
  exactly one file -- `ENV_FILE`, else `./.env` -- via `menhir.env_file`, and installs a guard
  at package import that turns a dependency's bare `load_dotenv()` into a no-op.
- Preflight distinguishes a Neo4j that refused `NEO4J_USER`/`NEO4J_PASSWORD` from one that
  did not answer, with one connection instead of two; `menhir up` stops its wait immediately
  on a refused password and the tier report says which credential to fix.
