## 2026-09-21 - established architecture decisions become first-class ADRs

- Added ADRs 0003–0010 for the shipped Event → Fold → View boundary, single runtime owner and
  backend-first access, core-enforced namespace isolation, recoverable cross-store sagas,
  evidence-gated default-off activation, identity/embodiment/locator separation, source-bound
  admission authority, and deterministic canonical self identity.
- The records distinguish implementation evidence from decision scope: namespace pins remain
  defense-in-depth rather than hostile multitenancy, empirical feature gates do not delay known
  safety/correctness fixes, projection kinds are not forced into one physical node shape, and
  admission rollout, authority vocabularies, canonical-self activation, and historical fork
  consolidation remain explicit owner decisions.
- Added `.agent/adr/README.md`, routed it from `.agent/README.md`, and linked each decision from its
  live architecture/data-model/activation owner document; no runtime behavior changed.

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

## 2026-09-16 - `menhir sync` actually uploads

The command refused every unqualified run with "the upload path is not implemented yet, and its
chunk and quota limits have to be measured first". Both halves of that became false earlier today,
so the refusal was the stalest thing in the CLI.

`menhir sync` now builds the plan, writes the deterministic bundle to a temporary file, and sends
it through `SnapshotUploader`. `--check` is unchanged and still local-only.

- **A refusal stops a send structurally.** The blocked check runs before the upload branch rather
  than inside it, so while a secret-risk path stands there is no code path that reaches the
  network. Asserted without `--check`, against a fully configured remote, by making any attempt to
  construct an uploader fail the test outright -- every other refusal test runs in `--check`, where
  nothing could be sent anyway and the assertion proves less than it appears to.
- **Missing configuration names the setting**, not the symptom. No `MENHIR_BACKEND_URL` says so and
  offers `--check`; a non-operator key says the tools are operator-tier, because the server's own
  refusal talks about permissions and sends the caller looking for a broken server instead of a
  wrong key.
- The bundle goes to a temp file rather than memory: the pilot quota admits 64 MiB compressed and
  the uploader streams a chunk at a time, so the client is the only place bundle size would matter.
- A SEALED upload prints that **nothing was extracted or written to the graph**. A user who reads
  "uploaded" and assumes it was indexed has been misled about what this phase does.
- An upload that ends in any other state exits non-zero. Every chunk being accepted while the
  upload is not whole is not a transport failure, and must not read like success -- the server
  holds a partial upload until the inactivity TTL.

Driven end to end by `tests/remote_sim`: the real command, reading the environment a user sets,
against a server that cannot see the repository it is receiving.
