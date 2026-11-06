## 2026-11-05 - Falkor runtime integration (#109)

- Reconciled the branch with current main and repaired full-CI query capture/source guards.
  Centralized the native/Neo4j metadata provenance field, made the certification fixture safe
  under assertion-ID uniqueness, and retained historical qualification pins in the bump checks.
- Added the explicitly authorized native `fn1:` scalar repair/reconciliation IDs, preserving
  namespace identity and refusing incompatible existing receipts before activation. Mapped native
  single-memory and namespace erasure while retaining shared retirement/rebuild policy and Neo4j
  identities. Qualification covers durable receipt replay, native file reopen and late rollback.
- Added native scalar activation/readiness, View create/refresh/supersede/retire and typed-assertion
  clock/vector mappings, retaining shared folds, provenance gates and Neo4j behavior. Live checks
  cover real uniqueness, refusal, observation vectors and active fold/rebuild. Mapped the shared
  correlation path veto to the native regex function. Production enablement remains gated.
- Added native atomic project publication using the actual project writer's policy and payloads.
  Its mtime inputs and identity/scan token are revalidated before ordered writes; actual batch-edge
  counts and exact-one completion are enforced. Full/incremental/partial scan, pruning, input-race
  and late rollback cases pass on disposable storage; Neo4j retains its transaction path.
- Added native atomic document publication through the existing adapter and entity writer. The
  shared identity guard and actual MERGE execute together; create defaults and update policy remain
  shared with Neo4j. Live cases cover retained metadata, post-admission claim changes, migration
  freeze and rollback of creation/update. Atomic project publication remains open.
- Added a native atomic indexed-binding refresh through the existing adapter/writer. The shared
  identity/scan publication guard and unchanged compare-and-set execute in one command; admission
  and cleanup remain shared. Live tests cover post-admission claim changes, freeze, stale scans,
  same-name identity isolation and execution-time rollback. The actual raise/lower migration fence
  API is covered, with a native UTC freeze instant. Project/document publication remains open.
- Added controlled-model queued ingestion, Graphiti enrichment and recall on the shared Lite
  runtime. Graphiti group clones retain Menhir's physical graph. Episode clocks/claim hydration,
  source-vector reads, View receipt validation and result provenance have explicit native mappings;
  live tests reject swallowed backend errors and check tenancy, stale claims and provenance.
  This bounded profile does not enable the backend or qualify remaining atomic/scalar/merge owners.
- Added native phase-one schema/readiness: operational property/kind checks, native uniqueness,
  bounded enforcement waits and shared UTC-clock data defaults. Startup retries incomplete
  backfills and refuses duplicate identity data. Fixed Graphiti's startup index-count query and
  disabled underlying Redis transport retries on both shared clients to preserve ambiguous failures.
- Added the internal shared Lite repository/bootstrap seam and injectable Graphiti driver.
  Canonical UTC instant encoding matches Graphiti; the active TEMPORAL path now uses native
  create/list/complete queries and UTC calendar bounds. A live shared-graph test verifies
  cross-client visibility and restart persistence. Public backend selection and normal
  enrichment/recall remain unqualified; callback transactions fail before invocation.
- Reassessed the completion architecture: stop expanding isolated proof families, resolve shared
  temporal semantics, and integrate one unreleased Linux slice through existing repository/Graphiti
  seams. Retain all correctness release gates; dormant T5 cutover and native Windows work do not
  precede the active runtime path.
- Added a bounded create/supersede candidate that captures the actual View and scalar provenance
  write bodies and commits them with real projection work/receipt labels in one guarded query.
  Source/history/time fences, native postconditions, late-failure rollback, replay and competing
  publisher cases pass in the string-temporal profile. Refresh, retirement, native View temporals,
  crash recovery and the full P1 transaction gate remain open; no runtime wiring changed.

## 2026-11-04 - FalkorDBLite compatibility spike (#109)

- Fixed two shared scalar certification blockers found while qualifying the real View path:
  hash the writer's resolved evidence anchors, and allow explicit transaction budgets to honor
  View statement deadlines. Added live Neo4j history/refresh/retirement and certification-rollback
  regressions plus a Falkor string-temporal materializer diagnostic. Falkor atomic certification
  and native View temporals remain gated.
- Added a full-project compound candidate reusing the actual structural writer, with mtime/scan
  freshness, native UDF postcondition rollback, operational uniqueness constraints, full/partial
  scan parity and failure/race checks. Restart now verifies project data, temporal values and UDF
  restoration. Parameterized temporals and real View/history certification remain gated.
- Updated the agreed integration roadmap and callback-writer classification. Added an atomic
  identity-fenced document candidate reusing the actual writer's MERGE/payload, with live rollback,
  refusal, migration-drain and concurrent-writer checks. Full project/View publication remains gated.
- Compared engine-first and Menhir integration routes; documented the recommended internal backend
  with targeted Falkor fixes, ownership boundaries, and the unchanged transaction feasibility gate.
- Added a bounded source-bound prepared variant: the real assertion/provenance snapshot and evidence
  fence are revalidated inside the atomic publication/replay query. Added no-write refusal, null-safe
  comparison, overflow, rollback and competing-publisher checks; real View history integration remains gated.
- Extended the probe to real assertion/provenance reads and View history; exposed stale-admission
  and quarantined-evidence acceptance in the synthetic candidate. Corrected its slot kind to `count`
  and added a production View-kind regression. Native View temporals and full P1 remain blocked.
- `scripts/falkordblite_prepared_materialization.py`, `scripts/falkordblite_prepared_proof.py`:
  bounded numeric prepared-write candidate with atomic payload/certificate, snapshot/time fences,
  refusal, replay and worker-exit checks; offline contracts in `tests/test_falkordblite_prepared_materialization.py`.
- `scripts/falkordblite_projection_proof.py`: real Menhir materialization rejection counterexamples
  and a guarded single-query publication candidate with injected-failure and competing-writer checks;
  integrated into the isolated probe. The callback transaction gate remains unpassed.
- `.agent/reference/menhir-falkordblite-compatibility.md`: bounded transaction inventory and P1 results.
- `.agent/memory-backlog.md`: links the phased workspace roadmap and conditional LLM token budget.
- `.agent/memory-backlog.md`: future adapter gates, rollback evidence and the separate Windows fork investigation.
- `scripts/falkordblite_compatibility.py`: opt-in isolated storage probe for the checkout's Graphiti
  fork, entity/edge replay, vector/fulltext search, rollback failures and orderly restart persistence.
- `tests/test_falkordblite_compatibility.py`: offline refusal and result-reporting checks.
- `.agent/reference/menhir-falkordblite-compatibility.md`: measured transaction blockers, Windows
  limitation and remaining acceptance gates; indexed in `.agent/reference/README.md` and
  `.agent/scripts-index.md`, with the investigation boundary recorded in `.agent/architecture.md`.
  Runtime storage and dependencies remain unchanged. Older changelog entries remain in Git history.

## 2026-11-04 - `/readyz` shows build details only to callers with a valid token

- The public `/readyz` body keeps its status fields but no longer reports the version, commit or
  metadata digest. A caller sending a bearer token the MCP endpoint would accept gets the `build`
  block. Both bodies are `Cache-Control: no-store`. A bad token gets the public body, never an
  error, and does not change the readiness answer.
- `scripts/release/check_served_metadata.py` reads the token from `MENHIR_READYZ_TOKEN`, sends it
  only over HTTPS or directly to loopback, and does not follow redirects while holding it. It now
  treats any status other than 200 or 503 as an error instead of reading the body.

## 2026-11-04 - Warning lines stop suggesting a silence; candidate text is marked untrusted

- Operator warning lines no longer tell the reader how to silence them. `acknowledge_warning` is
  unchanged and still listed for operator clients.
- `review_inbox` marks the candidates section as unreviewed, untrusted text.

## 2026-11-04 - One read-only inbox of what is waiting on a decision

New MCP tool `review_inbox` (readonly tier, #437). It lists unresolved conflicts, failed
enrichments, candidates, stale TODOs and open artifact questions in one call, with the tool that
acts on each. Plans stuck in a lifecycle state and a start-session inbox line are not included yet.

- `src/menhir/mcp/tools/ops/review_inbox.py`: the tool. One failed or hung read shows as
  `unavailable (<ExceptionType>)` and the other sections still render.
- `list_candidates` gains an optional `namespace` at every backend layer. The internal backend
  dispatch now forces a pinned client's silo onto it, as it already did for the other four reads.
  Each candidate row now also reports its `namespace`. An unscoped call is unchanged.
- Agent-tier clients do not get the tool. A production client policy must decide `review_inbox`
  for every client, or startup refuses the policy.


## 2026-11-02 - Usability charter and guardrails

Documentation only. Sets who Menhir must work for and the rules that keep new work usable.

- `docs/usability/README.md`: audiences, nine principles, and which guardrails are enforced in code.
- `docs/usability/tool-design.md`: rules for MCP tool names, descriptions, arguments, output, errors,
  safety and lifecycle.
- `docs/usability/agent-facing-text.md`: rules for server instructions, tool descriptions, the AGENTS
  template and hook output, including when a change needs an agent evaluation.
- `docs/usability/first-run.md`: standards for the first-run journeys and the gaps against them
  (#429-#441).
- `docs/usability/documentation.md`: rules for docs, and which document owns each fact.
- `AGENTS.md`, `CONTRIBUTING.md`, `.github/pull_request_template.md`: point contributors and agents at
  the guides; the PR template gains a usability checklist.

## 2026-11-02 - One command prepares a version or fork bump

`scripts/bump.py` replaces the by-hand edits before a release. No runtime code changes.

- `scripts/bump.py`: `version` and `fork` subcommands update every file that states the version or
  pin, or write nothing if an expected line has moved. `uv lock --check` must pass, and `--pr` opens
  the pull request. Tagging stays in `scripts/release.py`.
- `tests/test_bump.py`: covers partial writes, restores, and CRLF files. It also fails if a new file
  names the current version or fork pin and the bump would miss it.
- `docs/runbooks/release.md`: step 2 and the fork pin procedure.

## 2026-11-02 - One tag publishes the whole release

A `v*` tag now publishes PyPI, the release image, the image's version tag and the GitHub release
in one run. Nothing is typed in by hand, and a stopped release can be finished by a re-run.

- `.github/workflows/publish-pypi.yml`: calls `release-image.yml`, verifies the attestation with
  the identity `menhirctl.ps1 upgrade` uses, tags the image `X.Y.Z`, then creates the GitHub
  release. The test gate is unchanged.
- `deploy/release_pipeline.py`, `deploy/release-python-base.txt`: derive the release inputs, skip
  finished stages, and refuse to move an image tag or upload over a different PyPI file set.
- `scripts/release.py`: wait up to three hours for the longer run.
- `docs/runbooks/release.md`: the new flow and how to finish a stopped release.

## 2026-11-02 - v0.3.2: retry replies cut off at the output limit

- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`,
  `docs/model-governance.md`, `docs/post-install.md`: version 0.3.2. No dependency changes in this
  commit.
- Includes the truncation retry and fork 0.31.2.post2 (#424), the parallel `MENTIONS` retention fix
  (#422) and the release command error report (#420).

## 2026-11-02 - Retry LLM replies cut off at the output limit; Graphiti fork 0.31.2.post2

A reply cut off at the output token limit could be parsed into a nested fragment, so the retry with
a higher limit never ran, or the fragment was accepted. The fork now raises `OutputTruncatedError`
when the provider reports `finish_reason=length`, and Menhir treats that as truncation.

- `infrastructure/graphiti_llm_adapter.py`: `_looks_like_truncation` returns true for
  `OutputTruncatedError` at any reply length. The retry limits are unchanged: 4096, 8192, 16384,
  then 16384 with a request for a smaller extraction.
- `tests/test_graphiti_truncation_retry.py`: real-client tests for the escalation.
- `pyproject.toml`, `uv.lock`: `archolith-graphiti-core==0.31.2.post2`; pins, docs and release
  checks updated.

A prose-wrapped reply that is cut off, when the provider does not report `length`, can still parse
to a fragment.
