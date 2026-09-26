## 2026-09-25 - ingest cleanup and fallback failure visibility (#70)

- `src/menhir/services/ingest_worker.py`: include heartbeat, usage callback, and context setup
  in the cleanup boundary; setup errors and cancellation stop the heartbeat and restore request context.
- `src/menhir/services/enrichment_steps.py`: warn when oversized-episode raw capture fails;
  preserve the original episode's terminal failure handling.
- `tests/test_services_pipeline.py`: cover failures before and after callback installation,
  setup cancellation, and a visible capture warning with the original content retained.
- `src/menhir/services/event_fold.py`: warn when counter or timeline embedding fails while
  preserving the derived write and keyword-only fallback.
- `tests/test_windowed_fold.py`: verify both result shapes survive embedding failure and warnings
  appear only on failure, not successful or intentionally omitted embedding.
- `.agent/workflows/logging-and-troubleshooting.md`: explain capture and event-fold warnings.

## 2026-09-25 - orphan recovery preview covers every execution phase (#149)

- `recover_orphans` uses one backend contract in local and HTTP modes; execution preserves the
  `demoted` counter and skips the unused pre-read.
- The read-only preview scans all sessions and separately reports consolidation candidates,
  expired demotion TTL nodes, and eligible empty episodes. The age argument applies to
  consolidation; existing TTL and seven-day empty-episode safeguards remain in force.
- Focused unit, HTTP round-trip, and disposable Neo4j regressions cover the preview and actual
  cleanup, including flagged and content-bearing survivors.

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
