## 2026-09-26 - chronological memory reads tolerate legacy timestamp storage (#145)

- Normalize native dates and valid legacy ISO text before database ordering and limits in recent,
  flagged, scope, and type reads; invalid access falls back to creation, unknown dates sort last,
  and equal instants use a stable UUID tie-break without losing fractional precision.
- Stamp native memory timestamps in TEMPORAL, candidate, L4, TODO-reminder mirrors, and View refreshes;
  retain candidate/artifact creation receipts when storage types change.
- Use guarded timestamp conversion for decay and session age predicates; unknown ages cannot
  justify destructive decay. Document writer coverage, manual backfill precautions, and scoring limits.
- Disposable Neo4j regressions cover actual reads/touches, bounded startup selection, writer receipts,
  lifecycle protection, offsets, invalid calendar values, native/local dates, and nanosecond ties.
- Archive the oldest changelog entry to keep ten. No automatic migration or production data changes.

## 2026-09-26 - generic reads preserve completion and artifact supersession (#143)

- Preserve stored status, artifact status, and replacement identifiers through graph projections,
  recall scoring, MCP/resource serializers, REST recall, and startup context.
- Label completed obligations and historical artifacts while keeping original content searchable;
  retain labels through context budgets, clipped timelines, and pinned hook summaries.
- Preserve distinct lifecycle states during context deduplication. Ordinary records retain their
  existing presentation; dedicated reminder lists still include only open reminders.
- Regressions cover compact/full recall, startup and REST reads, resources, clipping/budgets,
  ordinary/unknown state controls, and actual completion/supersession writers on disposable Neo4j.
- Document the read policy and optional result fields. No database migration or production activation.

## 2026-09-26 - semantic recall preserves conversation admission (#148)

- `backend_runtime_data_ops.py`: forward the effective request/process session into recall.
- `backend_client.py` and `service_access.py`: carry the writer's conversation identity through
  the stdio HTTP bridge; regressions exercise actual auth middleware across operation paths.
- `recall_support.py` and `recall_pipeline.py`: share ordinary/pending SESSION admission,
  filter before waiting, recheck refreshed source ownership before pending/READY projection,
  and require explicit policy inputs at all three pending-result assembly paths.
- `episode_lifecycle.py` and `memory_graph_adapter.py`: filter pending sources before the
  bounded query limit and carry owner stamps, preserving namespace filtering and anonymous opt-in.
- Recall/MCP/graph regressions cover owners, absent stamps, disabled inclusion, all assembly paths,
  refresh changes, caller/process identity, and actual disposable-Neo4j query-to-recall behavior.
  Existing test doubles accept the extended internal signatures.
- The stdio lifecycle lane flags, consolidates, then promotes its corrected recall result before a new
  conversation is expected to retrieve it, and verifies the same UUID after restart.
- `.agent/memory-policy.md`: document conversation admission and its namespace-auth boundary.

## 2026-09-26 - startup context supporting reads retain namespace scope (#116)

- `src/menhir/mcp/tools/recall/recall_context_memories.py`: pass the effective tool namespace
  to flag inspection and stale-TODO reads, preserving unscoped behavior and client pins.
- `tests/test_cf238_bootstrap_receipt_identity.py`: exercise MCP execution and the real local
  provider through scoped, default, omitted, and conflicting/omitted client-pin cases.
- `.agent/tasks-mcp.md`: describe the supporting-read scope and the agreed MVP deferral of
  the full effective-scope receipt feature. #116 remains open.

## 2026-09-25 - decay age pre-filters follow eligible policy thresholds (#86)

- `src/menhir/services/lifecycle_models.py`: derive compression and deletion age minima
  from non-exempt policies at startup, preventing future lower thresholds from being skipped.
  Current eligible-policy minima remain 7 and 30 days; zero-day non-exempt policies participate.
- `src/menhir/services/lifecycle_decay.py`: replace the obsolete pre-LLM compression docstring
  with the helper's actual truncation behavior and the automatic sweep's LLM path.
- `tests/test_decay_logic.py`: verify both real sweep requests in fresh processes with shorter,
  zero-day, and exempt policies, without mutating shared test module imports.
- `.agent/memory-policy.md`: document the pre-filter rule, exemption choice, and restart requirement.

## 2026-09-25 - stale-verification reads avoid Neo4j 5.26-only label syntax (#69)

- `src/menhir/infrastructure/tool_event_repository.py`: match the fixed
  `StaleAnchorVerification` label in both receipt reads and remove unused label parameters.
  Preserve property parameters, tenant filtering, sorting, and post-dirty matching.
- `tests/test_stale_anchor_verifications.py`: update query contract checks and execute both
  reads against disposable Neo4j, covering filters, limits, ordering, receipt paths, and empty results.
- Verified the previous queries execute on Neo4j 5.26.31. Dynamic MATCH labels were introduced
  in 5.26; this change removes that unnecessary version dependency without claiming a 5.26 failure.

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
