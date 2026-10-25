## 2026-10-25 - View freshness gate and fact statement time, both on by default

o500 (#168): a View kept leading with a stale value when the newer statement was a same-slot occurrence the fold ignores, and fact lines carried only event time, so a later statement about an earlier event read as older. Recall-only o500 A/B (one run per arm): both off 0.712, said time 0.738 (+0.026, paired 95% CI +0.002 to +0.048), both 0.746. That run's gate also demoted on newer statements in other slots of the same subject; E2E-17 showed this is wrong (a newer book count demoted the coin count), and on o500 only 1 of its 15 demotions changed an answer (a win, from a same-slot occurrence), so the shipped gate keeps only the same-slot rule. The narrowed gate is not re-measured.

- `MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_FRESHNESS_GATE` (new, on; requires scalar view authority): `domain/scalar_view_freshness.py` (new) finds occurrences in the verdict's exact slot with a later `valid_at`. `services/recall_pipeline.py`: a current (not counted) View or expiry verdict with one is advisory instead of leading; `has_foundation` is unchanged and the audit records `stale_by`. Hits kept out of the ranked list still count; occurrences reach recall only with the counting lane on.
- `MENHIR_FRONTIER_FACT_SAID_TIME` (new, on): fact queries (`infrastructure/cypher.py`, `infrastructure/memory_queries.py`) read the edge `reference_time` as `said_at`; with the flag on, `TemporalFact.said_at`, the API item (`api/routes_support.py`, `mcp/formatters.py`) and the context line (`services/context_builder.py`: `<happened> | said <date> | <fact>`) carry it.
- `domain/retrieval_tuning.py`, `config/settings_model.py`, `config/feature_flags.py`, `.env.example`: the two settings; the shipped settings default on, the `RetrievalTuningConfig` engine defaults stay off.
- `docs/agreement-settings.md`, `tests/test_shipped_agreement_profile.py`: the freshness gate in the shipped profile.
- Tests: new `tests/test_scalar_view_freshness.py`, `tests/test_fact_said_time.py`.
- `CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).

## 2026-10-25 - Scalar verdict gate on by default; observation recall gated by default

On o500 the scalar lane lost to lanes-off (0.618 vs 0.686): off-topic verdicts and raw observation quotes took the slots of answer-bearing memories (#168). With both changes, counted-rw scores 0.694 (lanes-off 0.686; parity, not yet a significant gain).

- `config/settings_model.py`: `MENHIR_PERSONAL_MEMORY_SCALAR_VERDICT_MIN_SIMILARITY` defaults to 0.675 (raw cosine 0.35, #300's bench value), was 0 (off).
- `MENHIR_PERSONAL_MEMORY_SCALAR_OBSERVATION_RECALL` (new; `gated` default, `all`, `off`): which raw observations enter the ranked list. `gated` keeps those at or above the verdict gate; `all` is the old behavior; `off` keeps none. Slots still feed the authority and history lanes, and a skipped observation still resolves the query subject (G17).
- `domain/retrieval_tuning.py`, `services/recall_pipeline.py`: the mode, validated to the three values.
- `.env.example`, `docs/agreement-settings.md`, `tests/test_shipped_agreement_profile.py`: the new default and setting. `tests/test_scalar_verdict_min_similarity.py`: default assertion. `tests/test_scalar_observation_recall.py` (new).
- `CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).

## 2026-10-24 - Agent episodes cannot supersede user-stated facts; Graphiti fork 0.31.2.post1

For agent-written memories (#168, plan `menhir-agent-memory-writer-plan-2026-10-23` P1).

- `pyproject.toml`, `uv.lock`: `archolith-graphiti-core==0.31.2.post1` (upstream Graphiti 0.31.2 plus the fork's hooks, including the new `EdgeInvalidationHook`); pins, docs and release checks updated.
- `infrastructure/graphiti_edge_invalidation.py` (new), `infrastructure/graphiti_client.py`: `MenhirEdgeInvalidationHook` is always installed. Information whose source is not in `GATE_APPROVED_HUMAN_SOURCES` never expires or truncates a stored edge whose stored `r.source` is human; the authority is the cause edge's source (its stored source, else this call's `source_description`, bound per `add_episode` call in a ContextVar). A human cause may still supersede anything. Lookup errors keep the edge (fail closed). Startup refuses a fork without the seam.
- `r.source` now survives duplicate resolution: `source` is always in `preserved_edge_attributes`. Before, a paraphrased restatement cleared the stored edge's attributes and the full-replace save dropped `r.source`, so stamping gave the edge to the restating source.
- A kept contradiction is written after a successful `add_episode` as an `:EdgeInvalidationConflict` record (`status: 'unresolved'`, one per kept/cause edge pair; a repeat bumps `seen_count` and never reopens a resolved record). Entity nodes and the entity conflict groups are untouched. These records are not yet listed by `list_conflicts`.
- Known gap (fork): an edge whose own window is inverted or empty is expired by its own end without asking the hook.
- Tests (new): `tests/test_edge_invalidation_guard.py`, `tests/test_edge_invalidation_guard_online.py`, `tests/test_edge_source_preserved_live.py`. Changed for 0.31.2: test doubles that now receive `query_vector` / `identity_properties` / `max_tokens`, the preserved-attributes expectation in `tests/test_anchored_time_restatement.py`, the restated-attributes assertion in `tests/test_anchored_time_expiry_live.py`, the timeout allow-list, and the `ScoredMemory` field set (adds `provenance`, missed in #375).
- `CHANGELOG-archive.md`: the oldest former current entry moved there (10-entry limit).

## 2026-10-23 - Recall results say who said them; scalar view authority defaults on

For agent-written memories (#168, plan `menhir-agent-memory-writer-plan-2026-10-23` P1b and P2).

- `domain/recall_provenance.py` (new), `domain/recall.py`, `infrastructure/memory_queries.py`, `infrastructure/memory_graph_adapter.py`, `services/recall_pipeline.py`: each Entity, fact-edge and pending-episode result carries `provenance` (`said_by` user / agent / mixed / unknown, `sources`, `first_seen_at`, up to 5 `turn_evidence_ids`). One batched, namespace-scoped query per recall (`fetch_result_provenance`) classifies every contributing episode by its source against `GATE_APPROVED_HUMAN_SOURCES`; best-effort, so a failure leaves it unset. View, history and observation items, nodes with no contributing episode, and verb-facet facts appended later carry none.
- `mcp/formatters.py`, `services/context_builder.py`, `api/routes.py`, `api/routes_support.py`: non-user items get a label (`agent-recorded`, `user + agent`, `provenance unknown`) and the MCP `provenance` key; output for user-stated items and items without provenance is unchanged. The same rule applies to REST `/recall`, `recall_context_memories` and `memory://search`. The receipt source wins over Graphiti's placeholder `message` source; hub-entity turn ids are bounded to the earliest 5 episodes with a 10s query timeout.
- Tests (new): `tests/test_recall_provenance.py`, `tests/test_recall_provenance_pipeline.py`, `tests/test_recall_provenance_rendering.py`, `tests/test_result_provenance_online.py` (online).

- `config/settings_model.py`, `config/feature_flags.py`, `.env.example`: `personal_memory_scalar_view_authority_enabled` defaults to `true` (owner decision 2026-10-23, #168). `config/authority_gates.py` still applies it only while `personal_memory_scalar_state_enabled` is on, and the lane stays off by default, so a default install behaves as before; a deployment that turns the lane on now gets View authority without a second flag. Collapse, echo-drop and observation-dedup stay off. Opt out with `MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED=false`.
- `docs/agreement-settings.md`, `docs/personal-memory-readiness.md`, `src/menhir/domain/retrieval_tuning.py`, `.agent/default-off-features.md`, `.agent/plans/menhir-scalar-default-enablement-plan.md`: defaults and notes updated. `RecallService`'s own dataclass default stays `False`; bootstrap passes the effective gate.
- Tests: new upgrade test in `tests/test_release_config_upgrade.py` (unset versus pinned) and a default-wiring test in `tests/test_authority_requires_lane.py`. Changed: `DEFAULT_ON` in `tests/test_release_config_upgrade.py` and the shipped value in `tests/test_shipped_agreement_profile.py`.
- `CHANGELOG-archive.md`: the oldest former current entry moved there (10-entry limit).

## 2026-10-22 - Idempotent episode submit; enrichment leases renew up to a 3-hour ceiling

For resumable LME ingest (#168). A client that retries a submit after a timeout no longer creates a duplicate episode, and long enrichment no longer loses its lease at 900 s.

- Idempotent submit (`domain/ingest.py`, `services/ingest_intake.py`, `infrastructure/episode_lifecycle.py`, `infrastructure/schema.py`, `api/routes.py`, `api/routes_support.py`, `core/backend_*`):
  - `POST /memory` accepts an optional `idempotency_key` (1-256 chars), scoped to the normalized namespace.
  - The episode stores a digest of the key plus a request digest: content, diff, source, flagged, bootstrap scope, occurred_at, role, turn evidence and user statement. Session and user are excluded because the route mints a fresh session when the caller names none.
  - New uniqueness constraint `episodic_ingest_key_unique`.
  - Same key and same request: returns the first episode with `idempotent_replay=true` and its current `processing_state`, and writes nothing.
  - Same key with a different request: returns 409, or 400 over the internal `/backend` dispatch.
  - A lost create race re-reads the winner.
  - Without a key, behavior is unchanged.
- Lease renewal (`services/ingest_worker.py`, `infrastructure/episode_stamping.py`, `services/ingest_service.py`):
  - The 5 s processing heartbeat now extends `processing_lease_expires_at` by the lease length while the claim is younger than `max_enrichment_seconds` (10800).
  - Renewal is fenced on ENRICHING, the owner and the claim instant.
  - The heartbeat loop stops when the fence reports the attempt lost, and survives transient driver errors.
  - The worker bounds the enrichment body with the same ceiling. Exceeding it raises `EnrichmentDeadlineExceeded` (`enrichment_deadline_exceeded`, manual review).
- Ownership fences (`infrastructure/episode_lifecycle.py`, `services/enrichment_steps.py`): `mark_episode_ready` and `mark_episode_failed` also match the claim instant when it is given, so a stale attempt from the same process cannot finish a newer claim.
- `infrastructure/memory_queries.py`: deleting an unfinished episode also removes its ingest key and request digest.
- Tests:
  - New: `tests/test_idempotent_episode_submit.py`, `tests/test_heartbeat_lease_renewal.py` and `tests/test_attempt_ownership_fences.py`, each with a `_live` variant.
  - Changed test stubs (signature only, adding `claim_started_at`): `tests/conftest.py`, `tests/test_services_pipeline.py`, `tests/test_verbatim_skip_worker.py`.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-21 - Lazy scalar and event schema activation survives concurrent first use

In the LME pilot, 3 of 30 concurrent first `POST /api/phase3/run` calls on a fresh graph returned 500 with `Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists`. Neo4j 5 raises it when two transactions create the same rule at once, even with `IF NOT EXISTS`.

- `infrastructure/schema_ddl.py` (new):
  - `execute_schema_statement` treats only `EquivalentSchemaRuleAlreadyExists` as success. Name or schema conflicts and every other error still raise.
  - `await_schema_online` waits, bounded at 60 s per rule, for the named rules to come ONLINE. A timeout propagates.
- `infrastructure/typed_assertion_reconciliation.py` (`activate_scalar_state`), `infrastructure/typed_event_repository.py` (`activate`):
  - Run their DDL through the helper.
  - Then wait only for the rules they created. Otherwise a caller that lost the race could see the winner's index still POPULATING and fail the scalar ONLINE check.
  - Unrelated indexes are not awaited.
- Tests: new `tests/infrastructure/test_lazy_schema_activation_race.py`.
  - Offline: the fake-driver race, the codes that are re-raised, and the await order and timeout.
  - Online: 16 concurrent `ensure_scalar_state_activated` plus 16 event activations on a fresh schema, 3 rounds. It fails without the fix.
- Changed test: `test_activate_runs_idempotent_ddl` in `tests/test_typed_event_repository.py` now separates the `CALL db.awaitIndex` waits from the DDL statements it checks.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-21 - POST /api/phase3/run reports lanes that failed

In the LME pilot a counting namespace failed inside the pass (a provider 500), yet the request returned 200 with nothing marking it failed, so the bench recorded it as done.

- `api/routes_support.py`, `api/routes_handlers.py`: `Phase3RunResponse` gains `ok` (false when any lane failed closed for the namespace) and `failed_lanes` (lane, namespace, failed count). The status stays 200 and the existing counters are unchanged.
- `services/lane_outcomes.py`: new `failed_lanes()` reads each lane's failed counter; the scalar lane now has one.
- `services/scalar_consolidation.py`: when scalar-state activation is refused, `scalar_namespaces_failed` counts the scalar targets. Before, the lane was skipped with only a log warning, the same way the event lane already reports its activation refusal.
- Tests: new `tests/test_phase3_run_lane_failure.py`.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-21 - E2E work root is always absolute; dev group installs build

- `tests/e2e/conftest.py`, `tests/e2e/_harness/config.py`: `MENHIR_E2E_WORK_ROOT` is resolved to an absolute path. A relative root broke E2E-20's upgrade step, where pip runs from another cwd and could not find the candidate wheel.
- `pyproject.toml`, `uv.lock`: the `dev` group adds `build` (the wheel frontend E2E-19/20 use; CI already installs it), so `uv sync` is enough for a local E2E run. The relock also adds platform markers to `secretstorage`'s dependencies.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-21 - Default the inferred self tier to enforce

- `config/settings_model.py`, `config/feature_flags.py`, `.env.example`: `inferred_self_mode` defaults to `enforce` (owner decision for #343). Installs that leave it unset, fresh or upgraded, bind what agent-written memories say about the user to the inferred self (`user-inferred`) instead of a new "user" node, and recall reads both self tiers; pin `off` to opt out. An unrecognized value still falls back to `off`.
- `cli/bootstrap.py`: the hook CLI's `RecallService` now receives the mode, so hook-injected context reads the same tiers as backend recall (it ignored the inferred tier before).
- Existing "user" forks created before the upgrade stay where they are; `scripts/report_self_forks.py` lists them and consolidation stays blocked.
- E2E-19 runs `two_tier_self` on the shipped defaults; its opt-out step also pins `MENHIR_INFERRED_SELF_MODE=off`.
- `.agent/default-off-features.md`: the mode moves to a "graduated to default-on" section.
- Tests: new upgrade test in `tests/test_release_config_upgrade.py` for unset versus pinned modes. Changed: `tests/e2e/test_e2e_19_fresh_install.py` (default-on expectations and opt-out pin).
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-14 - Scan release archives from private output directories

- `deploy/build_release_image.py`: bind only the public image archive into each scanner, keeping private parent directories and sibling artifacts inaccessible.
- `tests/test_build_release_image.py`: require the archive-only read-only bind for both Syft and Grype.
- `.agent/reviews/menhir-release-container-audit-2026-10-14.md`, `.agent/plans/menhir-168-phase0-baseline-contract.md`: record the real unpublished image/fork checks, scanner reproduction, upstream-base scan refusal and remaining #168/#123 gates. Runtime defaults are unchanged.

