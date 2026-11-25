## 2026-11-24 - A merge or unmerge no longer makes a memory easier to delete (#939, part 2)

- `infrastructure/correlation_queries.py`: a merge gives the survivor the higher live scope and clears its
  `ttl_expires`, after locking both nodes so a concurrent promotion is not overwritten. A restored node
  comes back without its old `ttl_expires`; the survivor's scope is not lowered by an unmerge.
- `domain/merge_eligibility.py`: only SESSION and PERSISTENT memories merge. CANDIDATE, missing and
  unknown scopes are refused (`UNMERGEABLE_SCOPE`).
- `infrastructure/consolidation_queries.py`, `infrastructure/memory_graph_adapter.py`,
  `services/delete_coordinator.py`: the TTL delete re-checks expiry in the delete statement.
- `infrastructure/graph_text_inventory.py`: registers the transient lock key.
- Not retroactive: survivors that already carry a `ttl_expires` keep it.

## 2026-11-24 - File events stay in their project; stale receipts are ordered by instant (#964, #965)

- `infrastructure/tool_event_repository.py`: an event from an indexed project no longer marks another
  project's files. An event from an unknown project marks files only when every match belongs to one
  project. Verification receipts are compared as instants and count no later than their server
  `created_at`; the receipt list is ordered by `created_at`. Files already marked are not cleared.
- `scripts/maintenance/report_stale_verifications.py`: same receipt rule.
- `docs/hook-center-tool-events.md`: describes both rules.

## 2026-11-24 - Conflict suppressions are no longer removed by age pruning (#959)

- `infrastructure/telemetry/lifecycle_store_stats.py`: `conflict_resolutions` is out of the retention
  tiers. Pruning it at 90 days re-opened conflict pairs that were already resolved and cut short a longer
  `conflict_cooldown_days`. Rows already pruned are not restored.
- `config/settings_model.py`: comment only.
- `docs/your-data.md`: conflict suppressions are not age-pruned and erasure does not reach them.

## 2026-11-24 - Release bump PRs run stdio-e2e

- `scripts/bump.py version --pr` opens the PR from `release/X.Y.Z` instead of `chore/release-X.Y.Z`.
  `tests.yml` runs `stdio-e2e` only on `release/*` pull requests, so the 0.3.13-0.3.15 bump PRs skipped it.
  `tests/test_bump.py` checks the branch against the prefix in `tests.yml`; the release runbook says so.

## 2026-11-24 - v0.3.15: request paths with control characters are refused (#944)

- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.15. No dependency changes in this commit.

## 2026-11-24 - Request paths with control characters are refused (#944)

- `api/auth.py`: `BearerAuthMiddleware` answers 400 `invalid_path` to any HTTP or WebSocket request whose
  decoded path contains an ASCII control character (U+0000-U+001F, U+007F), in every auth mode. Route
  patterns end in `$`, which also matches before a trailing newline, so `/mcp-http%0A` was not recognised
  as protected by the auth gate but still reached the MCP route. The response does not echo the path.

## 2026-11-23 - v0.3.14: Agent Smith OAuth clients come from the client policy; conflict lifecycle and recall fixes

- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.14. No dependency changes in this commit.
- Upgrade note (#761): Agent Smith OAuth identities (`/oauth/client-metadata/agent-smith.json?client=<key>`) are served only from client-policy entries with a `registration`; the built-in table is gone. A deployment that uses Agent Smith must add those registrations to its policy in the same upgrade.

## 2026-11-22 - A memory whose enrichment fails stays recallable; extraction and lab fixes (#662, #483, #265)

- #662: the source-memory lane (`search_episode_embeddings`) now returns FAILED episodes; only
  erase tombstones and episodes with a sealed erase marker are excluded. The embedding repair
  job lists READY and FAILED episodes (`settled_only`), an oversized episode is embedded before
  it is parked, and exhausted-lease parking writes no raw-capture copy. Legacy raw captures are
  hidden from ranked recall and from recent memories. Source-search readiness
  (`source_search_eligible`) uses the same predicate as recall, and a FAILED receipt no longer
  uses up a timeline-anchor slot. The FAILED warnings on `add_memory`, the operator warning and
  `get_memory_stats` now say the text is recallable but has no entities or facts.
- #483: a combined-extraction response that is not a JSON object raises
  `CombinedExtractionShapeError`, classified as an output-parse error for manual review, instead
  of a bare `TypeError`.
- #265: the extraction lab passes the edge expiry, invalidation and preserved-attribute hooks
  that ingest uses.

## 2026-11-22 - A scalar View from a not-yet-admitted turn is recallable (#311)

- `infrastructure/typed_assertion_write_repository.py`, `projection_coverage_repository.py`,
  `scalar_view_repository.py`: when a turn-anchored assertion's TurnEvidence has no admitting
  Episodic, its `episode_uuid` is the turn id instead of `''`. The folded View's receipt is then
  that turn, it gets a TurnEvidence `MENTIONS` edge, and recall's live-provenance check passes;
  before, the View had an empty receipt and stayed hidden. The turn is used only when it is
  usable View evidence (finalized, not quarantined): a turn captured before finalization existed
  keeps the empty receipt, because the write gate would refuse it and abort the entity's rebuild.
  A finalized admitting episode becomes the receipt; the write path resolves a non-finalized one
  back to its turn. `source_episode_uuid` still reports admitted episodes only.
- Live tests cover the turn-only View, finalized and non-finalized admitters, a View rebuilt
  after admission, and a legacy turn that must not abort the rebuild.

## 2026-11-22 - Retry a timed-out source-memory embedding (#409)

- Source-memory ingestion retries one embedding timeout before finishing enrichment. A
  subsequent failure leaves the receipt available for the source-embedding repair job.
- Regression tests cover a successful second attempt and a repeated timeout.
- The episode status response reports source-search readiness separately from graph
  enrichment (`source_search_status`: READY, PENDING, UNKNOWN or UNAVAILABLE). The stored
  vector is compared with the size the configured embedder produces, so an embedder smaller
  than 1024 dimensions reads READY; when that size is not known the status is UNKNOWN.
- A PENDING status carries `source_search_reason`, and the guidance names what changes it:
  `missing` (the repair job), `missing_in_flight` (the ingest step), `size_mismatch` or
  `model_mismatch` (a re-embed with `scripts/backfill_episode_embeddings.py`).
