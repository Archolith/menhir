## 2026-09-25 - non-test modules split under the 500-line limit (wave 1)

102 maintained non-test Python files over 500 lines were reduced by behavior-preserving
extraction: cohesive units moved into new sibling modules, with every moved public symbol
re-exported from the original module so no import site anywhere changes. 99 targets are now
at or under 500 lines (94,719 to 33,724 lines across the same 102 paths), with the moved code
in 404 new sibling modules, each also under 500 lines.

Three documented exceptions remain: `deploy/personal_stage_vps.py` is unchanged because the
single-file exact-census VPS deployment contract installs exactly one runner and the runner
self-pins its digest; `src/menhir/core/runtime.py` (954 to 679) and `src/menhir/mcp/contracts.py`
(832 to 570) keep their test monkeypatch seam units in place, since moved bodies would resolve
names in the sibling module and silently stop intercepting test patches.

Verification: `ruff check --select F811,F821,ASYNC` is clean over all 457 changed files,
`python -m compileall` is clean across src/deploy/scripts/pipeline, and `pytest --collect-only`
collects 10,690 tests with no errors. Collection must run from the repository root because one
test reads a cwd-relative source path. The full offline and graph-backed suites run in CI on
this SHA.

## 2026-09-22 - memory write bounds now apply before every supported ingest side effect

The REST memory route already rejected episode text above 48,000 characters and diffs above
50,000, but both MCP queued-write tools could bypass those request-model checks and persist an
oversized pending episode before enrichment rejected or truncated it. The existing bounds now
live at the shared ingest boundary and run before evidence reads, persistence, provenance,
worker startup, or queue insertion. The MCP `add_memory` TEMPORAL direct-write branch applies
the same text limit before its graph write. REST keeps its existing 422 behavior; backend and
MCP callers receive the existing bounded validation-error path. Boundary tests cover limit - 1,
limit, and limit + 1 for both fields and assert rejected writes leave no episode, queue, or
evidence/provenance effects.

## 2026-09-22 - conflict resolution stays inside the caller's namespace

`resolve_conflict` previously scoped its initial group lookup but dropped the namespace before
the authoritative graph mutation. A legacy mixed-namespace conflict group could therefore turn
an allowed same-namespace lookup into a cross-namespace read, mutation, and response.

- The namespace now crosses the MCP, backend, runtime, adapter, and repository boundaries.
- Every resolution action scopes its member reads, final node writes, and edge bridging;
  unscoped internal scheduler calls retain their existing global behavior.
- Scoped reads use Menhir's canonical tenant predicate, including both `default` and the legacy
  empty spelling of that same silo.
- The post-resolution readback is scoped too, so foreign members cannot appear in the response.
- Focused regressions cover all three actions on a mixed legacy group and reject a foreign UUID
  before any partial mutation. This is a containment fix only; it adds no conflict features.

## 2026-09-22 - transient refunds belong to one claim, not merely one episode

A retryable failure used to transition the episode and refund its attempt in two separate
writes. Another worker could claim between those writes, after which the stale refund decremented
the new worker's attempt count. The refund now occurs in the same Cypher mutation as the FAILED or
PENDING transition and is fenced by ENRICHING state, owner, and the claim's existing
`processing_started_at` incarnation. The incarnation fence also closes the same-service ABA case,
where multiple worker loops share one service-level owner ID.

- Removed the UUID-only `count_transient_requeue` follow-up mutation and its helper.
- Missing owners or claim incarnations fail closed; duplicate refunds are no-ops; NULL attempts
  clamp to zero.
- Deterministic regressions cover different-worker and same-worker interleavings, idempotence,
  normal retry survival, exhaustion, and the generated Cypher contract for both terminal paths.
- Real-Neo4j coverage includes successful PENDING and FAILED refunds, different-worker and
  same-worker stale calls, absent owners, duplicate calls, NULL attempts, and exhaustion.

## 2026-09-22 - a JWKS-fetch 503 now says why, and which request

Production returned one `503 Unable to fetch OAuth JWKS` on 2026-09-22 02:31:32 UTC. The app
fetches its own JWKS through the public URL (Cloudflare and back through the tunnel); that one
fetch never reached the app, and nothing recorded why: the exception was discarded and the
client's `request_id` appeared nowhere in the server log. Diagnostics only -- no auth outcome
changes:

- `_load_jwks` logs `OAuth JWKS fetch failed: kind=<exception> error=... uri=... elapsed_s=...`
  (URI reduced to scheme/host/path, and the same redaction applied inside the httpx message).
- The auth middleware logs `OAuth server_error -> 503: request_id=<id> ... cause=<exception>`
  with the same id the client receives.
- `/readyz` gains an `oauth_jwks` block (`refresh_failures`, `last_failure_at`,
  `last_failure_kind`); it never affects readiness. Counts are per process.
- `deploy/RUNBOOK.md` section 7 covers the symptom and where to look.

The fixes themselves (keep cached keys when a refresh fails; verify against local keys rather
than the public URL) are not implemented.

## 2026-09-21 - a tracked-write receipt can be traced to its enriched episode (#92)

Every write leaves two `:Episodic` nodes: Menhir's receipt (the `episode_id` a caller is handed;
carries `processing_*`) and the node Graphiti mints inside `add_episode` (carries the MENTIONS
edges). That is by construction -- Menhir cannot pass a uuid into Graphiti -- and it is not double
LLM cost. What it broke was traceability: `get_provenance` named only the Graphiti twin, so an
agent holding its receipt could not match provenance to its own write. E2E-2 reproduced it.

**The link already existed, one-way, and nobody surfaced it.** `mark_episode_ready` has recorded
the Graphiti uuid on the receipt as `resolved_episode_uuid` since the anchor design; the entity
count on `POST /api/memory` already resolved through it. So the change is exposure, not schema:

- `get_provenance` lists `episode_id` (the receipt) beside `uuid` (the twin) for every episode,
  resolved by a reverse lookup on `resolved_episode_uuid` in `fetch_node_receipts`. Null for
  episodes enriched before the anchor recorded it -- honest, not hidden.
- `get_enrichment_status` reports `enriched_episode_uuid` so the pair is reachable from the
  receipt side too. Either id gets you the other.
- No new relationship, no migration, no backfill; the index for the lookup was already declared.
- The issue's RCA attributes the twin to the evidence projection (`turn_evidence_uuid`). The
  reproduction had none; the twin on the `add_memory_and_track` path is Graphiti's own node.
- E2E-2 is re-selected in the `stdio-e2e` job and `provenance_reachable_from_receipt` now asserts
  the structured field, not a substring. An online test proves the Cypher against a real graph.

## 2026-09-21 - a backend refusal is an answer, not a crash (#132)

`delete_namespace` refused correctly past its `max_nodes` cap, but MCP callers saw
`500 Internal Server Error` instead of the tool's documented JSON error, and lost the
"pass force=true, or dry_run=true first" guidance with it. Found by the E2E-8 isolation lane
against a real stack; invisible to the unit suite.

**The tools' `except ValueError` was dead code in HTTP mode.** `/api/internal/backend` mapped
only `InvalidQueryPresetError` to a status and re-raised everything else, so a backend
`ValueError` became a 500 before the tool could catch it. The named REST routes already mapped
these; the generic dispatch the tools actually use did not -- the same per-site-fix shape CF-30
recorded on this boundary.

- `backend_invoke` now maps `PermissionError` -> 403 and `ValueError` -> 400 with the message
  as `detail`; `BackendClient._request` re-raises them as the same exception types, so a tool
  behaves identically in-process and over HTTP. Three tools wrap a backend call this way
  (`delete_namespace`, `flag_memory`, `unflag_memory`); only the first was reproduced.
- **Why 299 tests missed it, and what changed:** `httpx.ASGITransport` re-raises app
  exceptions by default, so an in-process round-trip test could never see the 500 uvicorn
  sends. The regression tests use a `raise_app_exceptions=False` transport and were confirmed
  to fail on the pre-fix code; one runs the real `DeleteNamespaceTool` against the HTTP client.
- **The named REST routes had the same hole, one layer up.** `POST /memory/{uuid}/flag` had no
  mapping at all, so the structural-node refusal hit the catch-all and became 500 "An
  unexpected server error occurred". An app-level `ValueError -> 400` handler now sits next to
  the existing `PermissionError -> 403` one, so no route can be the one that forgot; a genuine
  fault still gets the 500 and keeps its traceback, and a test pins both.
- E2E-8 is re-selected in the `stdio-e2e` CI job. Its `capped_scan` criterion now requires the
  documented JSON error rather than tolerating the 500, so a regression fails the job.

## 2026-09-18 - Beacon generation switched to Beacon-owned build (issue #120 ownership switch)

Menhir no longer maps Beacon manifest fields. The bespoke raw-manifest construction in
`beacon_generation.py` (identity block, structure concept, doc selection, guidance block) is
deleted; Menhir now supplies only what it owns and Beacon generates:

- `beacon_evidence.py` (new) - dumps the versioned `beacon-menhir-evidence` v1.0 document from
  the structure read surface (identity, documents, files, structure counts, scan fingerprint),
  fail-closed on any index defect, deterministic for a frozen graph state. This is the single
  boundary Beacon's `MenhirSourceAdapter` consumes.
- `beacon_compat.py` (rewritten) - the compatibility gate is now the supported **build
  contract** (`beacon build` + `beacon validate` present in the target interpreter), replacing
  the brittle `beacon.__version__ == "0.1.0"` equality check that refused every post-0.1
  implementation. The boundary runs the real Beacon CLI with fixed argv; generation streams
  the manifest bytes from `beacon build --out -` so Menhir keeps publication ownership
  unchanged (`beacon_publication.py`: prefix, lock, atomic replace, CAS refresh).
- `beacon_generation.py` (rewritten) - evidence dump -> `beacon build` -> guarded publication.
  Same public surface (`generate_beacon`, `GenerationOutcome`, refresh/`expected_sha256`
  semantics) plus `GenerationOutcome.git_head`. Menhir refuses rather than invents: a project
  whose scanner read no description (no `.agent/README.md` or `CLAUDE.md`) is refused instead
  of publishing the `"<stack> project"` overview placeholder as its purpose; the raw scanner
  value is now persisted on the project node as `indexed_description` for that check.
- Freshness now covers git state. `beacon build --repo` runs Beacon's git tier, so the manifest
  cites `git HEAD <sha>` and the `origin` URL; the scan fingerprint excludes `.git`. The
  evidence guard captures is-a-repo/HEAD/origin alongside the graph fence and rechecks them
  under the publication lock, so a HEAD move between capture and publish is refused like a
  scan change and an empty commit counts as a change for refresh. Only `project.status` is
  `experimental`; Beacon hard-codes `current` on concepts and canonical docs
  (`docs/agent-usage.md` says so).
- The Beacon child runs with an allowlisted environment (PATH, Windows runtime, home, temp,
  locale, `PYTHONUTF8`-class switches only): `NEO4J_PASSWORD`, API keys and auth tokens loaded
  by `load_menhir_env` no longer reach `--beacon-python` or the git it spawns.
- Scanner-indexed documents publish `document_type`/`role` `generic` instead of the stringified
  `None` (`query_documents` omits unset properties rather than stringifying them).
- `menhir beacon generate` prints expected refusals (stale index, freshness/CAS, unusable
  interpreter, Beacon failure, filesystem error) as one `beacon generate refused: ...` line
  and exits 2; unexpected errors still propagate.
- **Operational note: scanner schema 5 -> 7.** The version is part of the scan fingerprint, so
  every stored fingerprint is invalidated and the next ingest of every project performs a full
  re-scan. That re-scan adds `document` entities for the `.agent` orientation set (`README.md`,
  `architecture.md`, and the rest of the A0 list) and stamps `indexed_description`, so overview
  entity counts change once per project. Scanner-written `document` entities are now pruned on
  rescan when the file is gone (`ingest_document` documents are untouched).
- `tests/test_beacon_generation.py` rewritten for the new flow; `tests/test_beacon_evidence.py`
  (graph-side fail-closed gates) and `tests/test_beacon_e2e6.py` (full Menhir MVP E2E-6:
  Beacon-owned generation, validate/inspect, stdio overview/onboarding/concept queries,
  claims-to-evidence tracing, deterministic rebuild, changed-fact isolation, serves without
  Menhir) added. CI installs Beacon into an isolated venv pinned to the exact commit
  `1cc3352b90004f3b76f1c5ed49ee4235c606a52f` (not a moving branch) and drops the
  version-equality assert.
- Requires a Beacon whose CLI supports build+validate (PR Archolith/beacon#10, stacked on #9).

## 2026-09-17 - the shadow scan: structural parity, and the copy does not survive it

The last P3 piece, and the only one that produces a result rather than a refusal. Its correctness
question is different in kind: not "was the attack stopped" but "does scanning a snapshot remotely
give the same answer as scanning the repository locally".

**It calls the existing `ProjectScanner` rather than reimplementing one.** Parity is then true by
construction and a surviving difference is a real one -- something the bundle dropped or the
extraction changed -- instead of a disagreement between two scanners.

**Local scanning is untouched, and that is asserted rather than claimed.**
`project_scanner.py` is not modified; a test checks it against git, and the 196 pre-existing tests
covering it pass unchanged.

- The fingerprint excludes `root_path` (absolute), `file_mtime` (extraction writes new files),
  `name` (the root is named for an upload) and the identity/self-fingerprint fields. Including any
  would make every parity check fail for a reason unrelated to the snapshot -- worse than not
  checking, because it trains a reader to ignore the result.
- Every list is sorted. `os.scandir` promises no order and differs between filesystems, so an
  unsorted fingerprint would differ between two scans of identical content.
- **A test proves the exclusions did not make it blind**: dropping one source file still changes
  the fingerprint. A digest that ignores enough to always match reports parity it never checked.
- The materialised root is deleted after the report, including when the scan raises. A root that
  outlives its report is an unattributed copy of somebody's repository sitting on a disk.
- The report carries the three coverage counts and does NOT carry `partial_index` (invariant 4):
  the counts are the source of truth and the derivation belongs to whoever can see the whole
  picture.

## 2026-09-17 - extraction runs in a child process (P3 decision 1)

`extraction_writer` holds the rules; this holds the blast radius. A malformed archive that hangs or
crashes the ZIP parser is now a failed job rather than a server outage.

**The counterexample this module exists for: a killed child runs no cleanup.** The writer removes
its root in an `except` clause, and SIGKILL raises nothing -- so a child killed at its deadline
leaves a half-written root and the PARENT has to remove it. Easy to miss precisely because the
in-process cleanup is correct and already tested. Confirmed by negative control: with the parent's
cleanup removed, the half-written root really does survive.

- A hung child is killed at its deadline; a crashed one (segfault, OOM kill) becomes a stable code.
- **Exit code 0 is not a result.** A child that printed a warning instead of JSON is refused rather
  than read as success -- that is how an empty root becomes a believed snapshot.
- Failures cross as codes, never tracebacks, and a child's refusal message is dropped. The child
  was parsing attacker-chosen bytes, so its output is not a place to source an error string from.
- The hanging and crashing children are injected as a different command, not hooked into the worker
  with a test-only flag. A production module with a test branch can take that branch in production.

**Containment is not uniform across platforms, stated rather than implied.** The wall-clock
deadline works everywhere; the memory ceiling uses `RLIMIT_AS`, which POSIX has and Windows does
not. Menhir deploys on Linux so the gap is development-only, but a caller believing the ceiling is
universal would be believing something false.

## 2026-09-17 - the extraction writer: bytes land, or nothing does

The half of P3 with consequences outside the process. `archive_plan` decides what may be written
and touches no disk; `extraction_lease` decides who may write it; this decides whether the bytes
actually land. Every rule is enforced again here rather than assumed from upstream.

- **The root is a boundary checked at the last line before `open()`.** Every joined path is
  re-resolved and proven inside the root, duplicating a check `plan_archive` already does. The
  duplication is the point: a containment check that lives only upstream is one refactor, one
  second caller, or one plan built elsewhere away from not existing.
- **Bytes are counted, never believed.** `declared_size` is metadata an attacker wrote, useful for
  refusing early and worthless as a promise. The limit is enforced against what has actually
  reached the disk, in 64 KiB blocks so a lying header cannot overshoot by a buffer's worth.
- **The lease is re-checked as work proceeds**, because an extraction can outrun its own authority.
- **A failure leaves no root**, including on `KeyboardInterrupt`. A half-written extraction that
  survives is indistinguishable from a complete one to whatever finds it next.
- **An existing root is refused, not reused and not deleted.** Adopting it would merge two
  extractions; deleting it would destroy evidence this function does not own. Reclaiming it is the
  sweep's job.

Verified by negative control rather than by the tests passing: with `_safe_target` replaced by a
naive join, the forged entry really is written outside the root; with the cleanup removed, the
partial root really does survive. Both guards are load-bearing, which is the thing a green test run
does not tell you.

Still to come in P3: the subprocess wrapper this writer will run inside (decision 1), and the
shadow scan.
