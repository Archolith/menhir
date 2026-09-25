## 2026-09-24 - MCP clients are told when to use memory, not just what Menhir is

Every MCP client receives the server instructions, whether or not a repository pastes the
AGENTS.md template. They were one positioning sentence that never mentioned memory or recall,
and in an agent evaluation agents skipped `recall_memories` in half the runs and used Menhir as a
code index. With the new instructions and descriptions, every run recalled and most read the
source episode; answers improved at the same cost.

- `src/menhir/mcp/instructions.py` (new): `SERVER_INSTRUCTIONS`, shared by the stdio and remote
  servers. It covers when recall is worth it (recorded decisions, rejected alternatives,
  incidents, preferences, constraints; especially when the repository cannot supply the
  rationale), the identifier discipline, one focused query first, `get_provenance` for exact
  wording, checking dates, conflicts and stale anchors, and that an empty result does not prove
  there is no history.
- The instructions also carry the local-stdio MVP write path (#118): write a decision with its
  reason and the rejected alternative; `add_memory_and_track` when the write must be recallable
  in the same session, observing its episode id rather than writing again; `ingest_project`
  when the repository is missing; a new project's memory starts empty.
- `src/menhir/mcp/server.py`, `src/menhir/api/mcp_remote.py`: use it. The stdio gateway also pins
  `get_provenance`, which the instructions rely on for a recorded reason's exact wording.
- `src/menhir/mcp/contracts.py`: `registered_description()` drops a docstring's opening sentence when
  it repeats the curated description; 13 tools sent their lead sentence twice.
- `recall_memories`, `get_provenance`, `query_structure`: descriptions say what each returns and
  when to use the next tool. Recall returns summaries, not source episodes; `include_invalidated`
  keeps superseded facts on returned results and is not a history search; `compact` and `trace` are
  documented; the structure graph is not a substitute for targeted recall.
- `get_artifact`, `list_artifacts`, `list_artifact_questions`: say they return records and document
  locations, not document text (`get_artifact` claimed "in full"), and that an open question does
  not establish a decision.
- `docs/templates/AGENTS.menhir.md`, `docs/agent-usage.md`, `README.md`: the same when-to-recall and provenance
  guidance; structure-first only for structural questions; `rate_recall` only where the client
  exposes it (the readonly tier does not).
- `tests/test_mcp_agent_guidance.py` (new): both transports send the shared text, every tool it names
  exists, stdio agents see the tools it relies on, and no registered description repeats its lead
  sentence. `tests/e2e/test_e2e_01_cold_install.py`: `get_provenance` joins the pinned MVP surface.
- `CHANGELOG-archive.md`: archived the 2026-09-17 shadow-scan entry to keep ten.

## 2026-09-24 - source retention fails closed on incomplete provenance

- `backfill_legacy_retention.py`: inventory all historical flags and processed sources, then add only individually reviewed, tenant-consistent source links under a quiesced, backed-up maintenance operation; preserve ambiguous entity flags.
- `test_backfill_legacy_retention.py`: cover incomplete, cross-tenant, structural, and merged candidates, manifest drift, and graph-backed live flag/unflag/reflag behavior.
- `episode_stamping.py`: refuse a missing or wrong-tenant source or extracted semantic entity instead of silently returning fewer retention links.
- `scheduler_tasks.py`: leave a failed episode unreconciled and continue the retry sweep when provenance is incomplete.
- `test_live_source_retention.py` and `test_live_source_retention_live.py`: cover incomplete links and structural exclusions with focused unit and Neo4j tests.
- `test_e2e_08_isolation_adversarial.py`: compare original node identities during capped-delete refusal so concurrent enrichment additions do not register as deletion.
- `memory_queries.py`, `.agent/data_models.md`, and `README.md`: describe direct entity flags, live source-linked protection, and the historical cutover limit.
- `episode_stamping.py` and `correlation_queries.py`: mark enrichment-written provenance as direct so unmerge preserves a survivor link recorded after the merge.
- `test_live_source_retention.py` and `test_unmerge_coordinator_live.py`: verify the direct marker and the graph-backed merge, later write, unmerge sequence.

## 2026-09-23 - Beacon can search the docs an agent needs to get oriented

Beacon only searches the documents listed in `beacon.yaml`'s `canonical_docs`. The list named five
entry points and plans, so the architecture, workflow and procedure documents that `.agent/README.md`
routes to could not be found through Beacon at all. In a Beacon-only evaluation run, an agent with
no file tools inverted the stdio runtime decision because `backend-first-mcp.md` and
`.agent/architecture.md` were out of reach.

- `beacon.yaml`: `canonical_docs` now lists 25 documents in reading order (entry points,
  architecture and decisions, reference, workflows, then the current plans and research indexes),
  chosen by an agent with no task context. The superseded July roadmap is left out, and so is
  `deploy/RUNBOOK.md`, which is specific to one operator's deployment. Listing a document makes it
  searchable, not required reading; the project overview still shows only the first four. Checked
  locally: all listed documents are indexed (443 search chunks, up from 105) and the generated
  manifest grows by about 420 tokens.
- `CHANGELOG-archive.md`: the 2026-09-17 extraction-writer entry moved there (10-entry limit).

## 2026-09-23 - Menhir's own beacon.yaml

Menhir's beacon intent holds only what a maintainer has to say: purpose, problem, non-goals,
audiences, current focus, the entry docs and plans in reading order, and the areas an agent must
not change without review. Everything else is read at build time from the repository, git and
Menhir's evidence (workspace plan `beacon-near-zero-authoring-plan-2026-09-23.md`).

- `AGENTS.md`: five rules wrapped in `<!-- beacon:guardrail -->` markers (no secrets, extend the
  canonical contracts, incomplete indexes are inconclusive, CI before publication, live tests
  are opt-in); the wording is unchanged. Beacon cites and pins them exactly.
- `docs/roadmap/menhir-mvp-roadmap.md`: frontmatter `status: superseded` (superseded by the
  local-stdio MVP release plan), so the beacon reads the status from the document itself.
- A comprehensive hand-written version (13 concepts, 20 docs, 13 cited guardrails, project
  state) is kept at commit `dd73b1b9` as the golden reference. With Beacon P0/P1, the overlay plus
  Menhir's own files matches it on identity, purpose, non-goals, audiences, review areas and
  commands; guardrails come from `AGENTS.md` markers and `SECURITY.md` (6 of 13); concepts and most
  project state await Menhir's evidence and the forge adapter.
- `beacon validate --intent`: 0 errors. `beacon.generated.yaml` is committed once production
  Menhir can supply the bound evidence.

## 2026-09-22 - project identity lives only in the graph; Menhir writes nothing into a checkout

Menhir no longer creates, changes or deletes `.agent/project-id`, `.agent/.gitignore` or a lock
file in any project (CF-257's per-checkout identity file is retired).

- A directory resolves silently only when this host's active binding names it AND the checkout is
  the one the binding recorded: the same `origin` (`""` for none). The binding now records
  `bound_repository` on create and on every transfer.
- A legacy binding (no recorded repository) is verified once by the legacy `.agent/project-id`
  naming the same id, which is only read; the repository is then recorded and the file is never
  consulted again. Without a matching file it is a decision (`legacy_binding_unverified`).
- Everything else is a decision, as before: an unbound directory (`directory_not_bound`, the old
  file's id offered as an adopt candidate for a moved checkout), or another repository in a bound
  directory (`repository_changed`). A malformed legacy file is ignored, not fatal.
- Removed: minting, the ignore rule, the `.agent/.gitignore` publication lock, and the publication
  recovery marker functions. Transfers serialize on the graph (one statement, root constraint).
- Scanner schema 9: `.agent/project-id` is scan-invisible (existing projects re-scan once).
- With no file to read, the id comes from Menhir: `ingest_project` reports it
  (`Scanned shop (project_id=...)`, also when skipped), and `get_beacon_evidence` accepts the
  checkout's `repository` origin instead of an id, serving the one project that recorded it and
  refusing with the list when several checkouts match. The tool census and client-policy digest
  are unchanged (a parameter, not a tool). Lookup refusals are MCP error results, like
  evidence refusals.

## 2026-09-22 - Beacon provider: README descriptions; Beacon output no longer dirties the index

Found by running Beacon's build against a local Menhir (plan Phase 3A exit check).

- A repository with neither `.agent/README.md` nor `CLAUDE.md` is described by the first
  paragraph of its own `README.md`, so Beacon evidence no longer refuses a plain repository.
  Scanner schema 8 re-scans existing projects once to pick this up.
- The index's dirty flag ignores Beacon's own root artifacts (`beacon.generated.yaml` and its
  staging files), the same set the scanner never reads, so publishing a beacon does not make the
  next index unservable. Any other change, or a rename touching another path, is still dirty;
  unparseable git status is dirty.
- `get_beacon_evidence` returns a refusal as an MCP error result (`isError`) with the reason, so
  Beacon can tell it from evidence. New `McpToolRefusal` in the call tracker; every other tool's
  failure keeps its "Error: ..." text.

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
