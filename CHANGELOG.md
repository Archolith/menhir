## 2026-09-27 - Prepare public Graphiti fork package for Menhir MVP

- Require `archolith-graphiti-core==0.30.2.post1` so normal installs can use a
  public wheel rather than a VCS checkout while retaining the native fork hooks.
- Lock the published PyPI wheel and source hashes from fork tag `v0.30.2.post1`;
  verify a fresh Menhir wheel install resolves the public fork without upstream Graphiti.
- Simplify the release image to consume the locked, hashed fork wheel directly.
- Make the fork package and its cold-install compatibility an explicit MVP gate;
  defer Beacon generation and consumption from this release.

## 2026-09-26 - Reconcile native Graphiti fork with current Menhir

- `pyproject.toml`, `uv.lock`: pin the public Archolith Graphiti 0.30.2 maintenance
  commit `6b907b93fed32cb979093327608a4fd897b39751` and preserve locked registry dependencies.
- Recover the unfinished Phase F migration onto published main: replace all 17 runtime
  patch installers with native fork hooks and Menhir-owned extraction, resolution and
  LLM policy adapters. Keep subsequent ingestion, retention and recall fixes.
- `graphiti_client.py`, `graphiti_resolution_policy.py`: flush telemetry inside its
  request task on success, failure and cancellation; count empty candidate searches.
- `graphiti_llm_adapter.py`: preserve namespace and operation metadata across retries.
- Release wheelhouse: derive the immutable fork requirement from package metadata,
  pin/hash the additional build backend closure, and build without isolated dependency
  resolution. Docker remains an offline wheel consumer.
- Migrated contract tests and added dependency, request-context, task-boundary and
  immutable-build-pin regressions. Feature defaults and deployed configuration stay as-is.
- Fork baseline is now 0.30.2. Default-on readiness (#169 and siblings) still requires
  outstanding source-grounding fixes and current graph/model quality evidence; this
  integration does not itself qualify a feature for default enablement.

## 2026-09-26 - remaining MVP audit fixes for recall and decay (#154, #144)

- `recall_pipeline.py`: acquire applicable independent sources before deciding recall is empty;
  assemble standalone edge candidates before fallback, retain pending results and search-failure attribution,
  and avoid metadata/adjacency round trips for an empty node pool.
- `consolidation_queries.py`, `memory_graph_adapter.py`, `lifecycle_decay.py`: rotate bounded decay
  batches by persistent least-recent selection, mark selected rows before processing, and log selection
  separately from successful work. Preserve access/age/retention/policy gates and deletion disarm.
- `test_recall_service.py`: empty/filtered/failed/pending semantic pools, file visibility/session guards,
  enabled observation-only and standalone edge lanes, plus actual scoped file-to-recall Neo4j acquisition.
- `test_lifecycle_service.py`: actual 501-record skipped-batch/restart regression, marker order for both
  phases, unchanged access/freshness, direct/source retention, and marker-failure refusal.
- `.agent/memory-policy.md`, `.agent/data_models.md`: define independent acquisition and the scheduling-only
  selection marker, finite-set fairness and restart/rollout limits. No production writes or migration.
- `CHANGELOG-archive.md`: move the oldest entry to keep ten.

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
