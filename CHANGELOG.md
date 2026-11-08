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

## 2026-11-08 - v0.3.4: MCP initialize names the build; releases publish what they serve; policy refusals are MCP errors

- `serverInfo.version` carries the build commit as SemVer build metadata, and each GitHub release
  attaches `served-metadata.json` for a deploy to compare with `/readyz` (#498).
- A tool refused by the client policy or `MENHIR_CLIENT_TOOLS` returns an MCP error result (#503).
- A transient 402 stays transient after it is stored, so the retry poller requeues it.
- Compact recall over the stdio bridge leaves the source-memory limit to the server, and keeps
  the closest five source memories.
- A release re-run attaches `served-metadata.json` to an existing release that lacks it.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.4. No dependency changes in this commit.

## 2026-11-08 - v0.3.4 audit fixes: bridge recall keeps the closest sources; release record on re-runs

- Compact `recall_memories` over the stdio bridge trimmed the server's source memories to the first
  five, which are the oldest, since the server sends them oldest-first. It now keeps the five with
  the highest cosine, still in the server's order. (Regression from the bridge-limit change.)
- A release run that finds the GitHub release already there attaches `served-metadata.json` when it
  is missing, and fails if it cannot, instead of finishing green without the record a deploy checks.

## 2026-11-08 - MCP `initialize` names the build; releases publish what they serve

- A release image reports `serverInfo.version` as the version plus the first 12 characters of
  `MENHIR_RELEASE_COMMIT`, as SemVer build metadata (`0.3.3+43e693f6b821`), so a long-lived
  connection shows which build it was opened against (#498). It still parses as the plain version.
  Without a valid commit the version is unchanged.
- `docs/agent-usage.md`: compare it with `/readyz`'s `build.commit` to spot a pre-deploy connection.
- The release workflow runs the sealed image with no network to compute `served-metadata.json`
  (version, commit, and the MCP metadata digest for every snapshot mode), checks it against the tag,
  and attaches it to the GitHub release. `python -m menhir.mcp.served_metadata` prints the record.
  A deploy compares `/readyz` with it; the GitHub release is not created without it.

## 2026-11-08 - The served-metadata check reaches a Cloudflare-fronted server

- `scripts/release/check_served_metadata.py` sends `User-Agent: menhir-release-check`. Cloudflare
  answered 403 to urllib's default `Python-urllib/x.y`, so the check could not read production's
  `/readyz`.

## 2026-11-08 - Policy refusals are MCP errors; v0.3.3 audit follow-ups

- A tool refused by the client policy or `MENHIR_CLIENT_TOOLS` now returns an MCP error result
  (`isError=true`) with the same text as before, so a client reading only `isError` no longer
  takes it for a result (#503). Other gate refusals (tier, identity, query-string auth) are unchanged.
- A transient 402 is stored with a `provider_payment_throttled:` prefix, so the retry poller,
  which sees only the stored text, requeues it as the worker did. Before, a 402 whose only sign
  was a Retry-After hint was parked for manual review.
- Compact `recall_memories` over the stdio bridge no longer sends a source-memory limit read from
  the bridge's own environment. The server applies its own default, and the bridge trims to 5.
- Upgrade note for v0.3.3: startup requires the production client policy to decide
  `review_inbox` and `acknowledge_warning` for every client, and refuses a policy that does not,
  so update the policy first. A deploy check that matched the old refusal text ("restricted by
  MENHIR_CLIENT_TOOLS allowlist") must also accept "restricted by the server's client policy",
  or, from this change, `isError=true`.

## 2026-11-07 - v0.3.3: review inbox, token-gated /readyz build details, transient-402 backoff

- New read-only MCP tool `review_inbox`: what is waiting on a decision, in one call.
- `/readyz` shows the build block only to callers with a valid token; the release check script
  reads it with `MENHIR_READYZ_TOKEN` and refuses served text that is not plain.
- A 402 with a retry hint (OpenRouter's in-flight budget) backs off like a 429 instead of
  hard-opening the LLM breaker for an hour.
- Recall: `relevance_basis` names the tier as a rank score; source memories can be filtered by
  ingest source. Conflict detection drops dissimilar pairs before review.
- Tool refusals name the control that refused; `uv.lock` refreshed for Python 3.15 wheels.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`,
  `docs/model-governance.md`: version 0.3.3. No dependency changes in this commit.

## 2026-11-07 - Release gaps: refusals name their control, review_inbox labels, readyz check output

- A refused tool call names the control that refused it: the server's client policy, or
  `MENHIR_CLIENT_TOOLS`. A bound client policy now decides alone, and an empty one denies every
  tool in both the invocation gate and `tools/list` (the policy loader already rejects an empty
  one) (#503).
- `review_inbox` over the stdio bridge no longer heads an unscoped read "all namespaces": a pin
  there exists only server-side (#507). Its candidates line says the explorer is not served by
  production (#504).
- `scripts/release/check_served_metadata.py` refuses a served build field that is not plain text,
  with exit 2 and without printing it, and reports an output encoding error as exit 2 (#501).
- New test pins the auth middleware's 403 `client_policy_denied` response (#508).
- `docs/usability/README.md`: the usage row is done (#480); the served-metadata row points at #498 (#505).
- Still open: a denied call returns a text result with `isError=False` (#503). Changing that
  changes an existing test's assertion, so it waits for a decision.

## 2026-11-07 - A 402 with a retry hint backs off instead of opening the breaker for an hour

- Every HTTP 402 counted as billing (#482), so OpenRouter's `in_flight_budget_exhausted` 402, which
  carries a Retry-After and clears in seconds, hard-opened the LLM breaker for an hour and parked
  the enrichment. A 402 with a Retry-After (HTTP header, or OpenRouter's `metadata.headers`) or
  that reason is now throttling: backed off like a 429, counted toward the normal breaker
  threshold, and requeued. A bare 402, or one with a billing code, is still billing.
- `retry_after_seconds` also reads the hint from the error body, so the backoff waits as asked.
- No operator reset for a hard-opened breaker yet (#492): it needs a new operator tool.
