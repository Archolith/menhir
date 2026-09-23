## 2026-09-22 - remote snapshot sync reaches an authoritative published view

- Connect explicit snapshot commit to the mode-gated receive, extraction, shadow-scan, and
  canonical graph-publication pipeline; the final chunk remains graph-inert.
- Bind uploads and promotion attempts to the authenticated principal and a server-issued project
  identity, with CAS publication, durable recovery intent, compensation, and degraded-view guards.
- Persist a server-specific local project receipt atomically and refuse conflicting or malformed
  identity receipts rather than replacing them.
- Resolve published snapshot structure through the canonical view while preserving byte-identical
  legacy responses for projects without one.
- Fix the published-view symbol query's Cypher predicate and prove that an empty promotion actor
  is refused without creating a view.
- Decode remote-simulation Docker output as UTF-8 on Windows so teardown and startup diagnostics
  cannot fail in a background reader thread.

## 2026-09-22 - Menhir serves Beacon memory evidence as a read-only provider

Beacon now owns all beacon work and defines a backend-neutral memory-provider contract (Beacon
`7a94fb0`). This is Menhir's side of it (plan Phase 3A); nothing here writes into a project.

- The project scan records the git binding it describes: indexed commit, origin URL (credentials
  stripped) and a dirty flag, read before and after the walk. It crosses the upload boundary and
  is written in the same project-node write as the scan fingerprint.
- Both fingerprint-skip paths (ingest, structure watcher) refresh only the binding when the files
  are unchanged but the commit moved, and write nothing when it is already current.
- `build_provider_evidence` reads the graph only and returns `beacon-memory-evidence-1.1`. It
  refuses an unknown or ambiguous id, a partial or in-progress index, an index taken without git
  or from a dirty checkout, and a re-index during the read. It sets no `project.status` (Menhir
  does not judge maturity).
- MCP tool `get_beacon_evidence(project_id)`: readonly tier, `menhir:read`, read-only, GLOBAL
  like the structure graph it reads. Added to the agent allow-list and every production client;
  `deploy/client-policy.production.json` digest is now
  `04abc7bdf5d59d31e497dcefb9d431c06cf6cb0f34d395469391fabd77fbb0aa` -- a release must ship it.
- `menhir beacon generate` is unchanged; Phase 3B removes it after Beacon's lane passes against
  this provider.

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

## 2026-09-17 - the extraction lease, written counterexamples first

P3 decision 4: the lease binds snapshot, owner and generation, never a project name alone. The
failures were written before the implementation -- the order that found both P2B bugs -- and it
found one here too.

- **The generation is the mechanism.** A claim is an exclusive file create (`O_CREAT|O_EXCL`) of
  `<generation>.json`, so two workers that both read "the last lease expired" both compute the same
  next generation and the filesystem lets exactly one win. A real compare-and-swap, because reading
  state and then writing it is the check-then-act the P2B quota bug was.
- **Supersession is judged before expiry.** The store is the authority on time, not the holder, so
  a worker that was paused or has a wrong clock is refused for the right reason instead of
  concluding from its own expiry that it is fine.
- **A stale holder cannot release the current holder's lease.** The dangerous one: expiry is
  obvious and everyone implements it, whereas a release keyed on the project name hands the next
  holder's lease to whoever crashed last, mid-extraction.
- **A crashed holder does not block the project.** An abandoned lease expires; there is no lock to
  be left held. That is the argument against a lock file, asserted rather than argued.
- **No process identity is recorded**, and a test asserts the field cannot be added quietly. A PID
  is reused, and a PID in another namespace is a different process -- "the holder must be dead by
  now" is an inference dressed as a fact.

The bug the counterexamples found: `release` first deleted the lease file, so the store saw no
history and the next claim reused generation 1. A generation that can be reused is not a version --
a stale lease from the previous turn would match the new one on generation, leaving only the owner
field between it and authority. Release now leaves an expired tombstone, keeping generations
monotonic for the life of the project.

## 2026-09-17 - Beacon generation from indexed project knowledge (#120)

- `src/menhir/services/beacon_generation.py`: conservative source-grounded manifest
  generation from StructureQueries evidence — intact-index/root-match/coverage/
  fingerprint gating, indexed description and canonical documents only, no synthesized
  purpose/commands/guardrails, fail closed on missing evidence.
- `src/menhir/services/beacon_compat.py`: version-pinned subprocess boundary to a
  separately installed Beacon 0.1.0 interpreter (framework dependency isolation); the
  raw manifest is parsed and validated by Beacon's own loader/validator before any
  bytes are published. No schema logic is copied into Menhir.
- `src/menhir/services/beacon_publication.py`: staged, digest-gated publication to the
  fixed sidecar `beacon.generated.yaml`; refuses overwriting initial outputs, foreign
  or hand-edited artifacts, symlinked paths, and concurrent writers (advisory lock);
  validates staged candidates before atomic replace; preserves mtime on zero diff.
- `src/menhir/cli/beacon.py` (+ registration): `menhir beacon generate PROJECT --repo
  --beacon-python [--refresh --expected-sha256]` local operator command.
- `docs/agent-usage.md`: command, sidecar/refresh policy, dependency isolation, and
  validation workflow.
- `tests/test_beacon_publication.py` (7) and `tests/test_beacon_generation.py` (10):
  publication safety, fail-closed evidence gating, real-Beacon round trips through the
  actual parser/validator, `beacon validate` CLI acceptance, deterministic refresh with
  zero semantic diff, and wrong-digest no-clobber. 17/17 pass locally; full offline and
  graph-backed CI on the exact SHA remain release gates. Live-Neo4j ingest→generate E2E-6
  and Beacon stdio tool-query acceptance are NOT RUN and stay with the MVP release lane.
- Keep the newest ten dated entries per `.agent/maintenance.md`; older entries remain in Git history.

## 2026-09-16 - local MVP tracked-write receipts and observation guidance

- `src/menhir/mcp/formatters.py`: status/watch observations direct continuation to the
  existing episode; remove duplicate-write advice and unsupported completion/retry promises.
- `src/menhir/mcp/tools/ingest/add_memory_and_track.py`: clarify that the tool queues a
  new write; preserve its accepted receipt when subsequent collection or formatting fails,
  without exposing raw exception text. Optional queue diagnostics cannot hide an observed
  episode status. Cancellation and existing write/auth arguments remain unchanged.
- `docs/agent-usage.md`, `docs/templates/AGENTS.menhir.md`: document the #118 owner decision,
  actual tool options, restricted-client behavior, and the separate TEMPORAL direct-write path.
- `tests/test_mvp_tracked_write_contract.py`: 31 focused formatter and bound-endpoint
  regression cases. Live stdio E2E-2 and exact-commit repository CI remain release gates.
- Keep the newest ten dated entries per `.agent/maintenance.md`; older entries remain in Git history.
