# SSOT remediation plan (from menhir-ssot-review-2026-07-11)

> **Archived 2026-08-11.** All four remediation phases are complete; the deliberately separated
> OAuth settings follow-up was completed and archived independently.

Source review: `.agent/reviews/menhir-ssot-review-2026-07-11.md` (+ coverage CSV).
Independently spot-verified against current code before this plan was written:
SSOT-01, SSOT-02, SSOT-04, SSOT-06, SSOT-09, SSOT-13 all confirmed exactly as
described (exact line numbers, exact divergent values). Remaining findings
(SSOT-03, 05, 07, 08, 10, 11, 12) were not independently re-derived line-by-line
but share the same deterministic-scan + manual-trace methodology and are treated
as trustworthy; verify each at the start of its own phase before editing.

Status of each finding: **unfixed** unless noted. (Confirmed distinct from the
unrelated `2b2a657` "single source of truth for episode flag propagation"
refactor already committed 2026-07-11 — that commit fixes episode-flag
duplication, not any of the 13 findings below.)

## Phase 1 — Correctness bugs (ship first, small blast radius) — DONE 2026-07-11

1. **SSOT-01 (High): `BackendClient.recall` missing `include_invalidated`. FIXED.**
   - Add `include_invalidated: bool = False` to `BackendClient.recall`
     (`core/backend_impl.py:1150`) and its request payload dict.
   - Add a signature-parity test (or extend the existing 67-method-name
     equality check) asserting `MemoryBackend`, `RuntimeProvider`, and
     `BackendClient` share identical parameter names/defaults per method, not
     just identical method names.
   - Regression test: instantiate `BackendClient` (fake transport) and call
     `recall(..., include_invalidated=False)`; must not raise `TypeError`.

2. **SSOT-02 (High): TEMPORAL ingestion drops namespace. FIXED.**
   - Thread `namespace: str | None = None` through the full chain:
     `add_memory.py:70` call site → `MemoryBackend.create_temporal` protocol →
     `RuntimeProvider`/`BackendClient` implementations → `MemoryGraphAdapter.create_temporal`
     → `TemporalRepository.create_temporal` → Cypher `group_id`.
   - `TemporalRepository.create_temporal` currently hardcodes `group_id: ''`
     (`infrastructure/temporal_repository.py:61`) — replace with the passed
     namespace/stamped group id, defaulting to `''` only when none given.
   - Add a round-trip test: TEMPORAL created with `namespace="private-ns"` is
     retrievable via namespace-scoped recall and absent from default-scope
     recall (mirror whatever namespace test already exists for semantic/TODO
     ingestion).

## Phase 2 — Namespace isolation completeness — DONE 2026-07-11

3. **SSOT-04 (Med): Recall adjacency scoring ignores namespace. FIXED.**
   - Add `namespace: str | None` to `RecallService._compute_adjacency`
     (`services/recall_service.py:455`) and `MemoryGraphAdapter.fetch_adjacency_pairs`
     (`infrastructure/memory_graph_adapter.py:414`), passing through to
     `MemoryQueryRepository.fetch_adjacency_pairs` which already accepts it.
   - Add a two-namespace graph fixture test: create linked nodes in namespace
     A and B; recall scoped to A must not have its ranking affected by B's
     structure.

## Phase 3 — Ownership consolidation (larger, one PR per bullet)

4. **SSOT-03 (Med): Duplicated identity-merge veto logic. FIXED.**
   - Extracted `CorrelationService.classify_pair(source_uuid, target_uuid,
     similarity)` as the sole owner of routing, all three deterministic vetoes,
     and judge-gated merge execution. `check_correlation` and
     `check_correlation_batch` (previously two more independent copies of the
     same routing/merge logic) now both delegate to it too.
   - `LifecycleService._check_contradictions_batch` now calls
     `classify_pair` instead of reimplementing routing/vetoes/judge inline
     (its old inline copy checked only `co_mention_veto`/`anchor_project_veto`,
     silently omitting `ineligible_node_veto`). It retains only lifecycle
     bookkeeping: namespace-scoped search, `pending_llm_review` conflict-queue
     writes, telemetry suppression/cooldown checks, one-conflict-per-node capping.
   - Removed the private `graph_adapter._correlation` reach-around (flagged
     fragile by a 2026-07-04 review): added public `MemoryGraphAdapter`
     delegates for `check_ineligible_node_veto`/`check_co_mention_veto`/
     `check_anchor_project_veto` alongside the pre-existing
     `create_related_to_edge`/`merge_entity`/`fetch_entity_merge_metadata`
     delegates, so `CorrelationService` now only depends on the adapter's
     public surface.
   - Test: `test_ineligible_node_veto_blocks_merge_via_lifecycle_path` in
     `test_lifecycle_service.py` — a structural/path-shaped node pair with a
     merge-confirming judge available is still blocked from merging via the
     lifecycle consolidation path (previously it would not have been).

5. **SSOT-05 (Med): Explorer bypasses `CandidateService`. FIXED.**
   - Explorer's local approval/rejection Cypher (`_approve_candidate`/
     `_reject_candidate`, old `explorer/app.py:229-252,672-675`) is removed.
     The `/explorer/candidates/{uuid}/approve|reject` routes now call
     `CandidateService.approve`/`.reject`.
   - Explorer has no live Graphiti/LLM client by design (small, mostly-read
     UI) — wired a `LifecycleService` for it using `UnavailableGraphitiClient`,
     which both `LifecycleService` and `CandidateService` already treat the
     contradiction-check failure from as best-effort/non-fatal. So approval
     still gets the same promotion/consistency guarantees as the backend path;
     the contradiction check itself is a safe no-op until Explorer has a live
     search client (not attempted here — out of scope).
   - `tests/test_explorer_candidates.py`'s stub repo updated to match the
     canonical `CandidateRepository` query shapes (`fetch_candidate` /
     `promote_candidate` / `reject_candidate`) instead of Explorer's old
     ad-hoc queries — a deliberate test update reflecting the architecture
     change, not a mechanical refactor-preserves-behavior change.

6. **SSOT-06 (Med): Conflict-scan default fragmentation (100/150/500). DECIDED: 150. FIXED.**
   - Canonical default is `150` (user-confirmed 2026-07-11). Make
     `RuntimeProvider`/`BackendClient` (currently `100`, `core/backend_impl.py:616,1342`)
     and the registered endpoint (currently `500`, `mcp/tools/conflict/scan_conflicts.py:34`)
     inherit it rather than hardcoding their own default. Fix the module-level
     `scan_for_conflicts` docstring at `scan_conflicts.py:17` which claims
     "default 500" while the actual default there is 150 — align docstring
     to the real (now singular) value.
   - Add a default-parity test across protocol/impl/wrapper/endpoint.

7. **SSOT-08 (Med): `NodeScope.PROMOTED` vs `PERSISTENT + user_flagged`. DECIDED: build a real third tier. FIXED (all 3 stages done).**
   - **Stage 1 (writer + confidence pin) — DONE.** `MemoryQueryRepository.promote_memory`
     (guarded on `scope IN ['PERSISTENT', 'PROMOTED']`, idempotent), full
     `MemoryGraphAdapter`/protocol/`RuntimeProvider`/`BackendClient` chain,
     registered in `_BACKEND_METHODS`/`_OP_TIER_OPERATOR`, new
     `promote_memory` MCP tool (`mcp/tools/ingest/promote_memory.py`,
     `required_tier="operator"`). Sets `source_confidence = 1.0` at
     promotion time. Tests in `tests/test_promote_memory.py`.
   - **Stage 2 (absorption immunity) — DONE.** `CorrelationService._handle_merge_proposal`
     fetches entity metadata (moved earlier so it runs unconditionally, like
     the other three vetoes) and refuses to merge if either survivor or
     absorbed node has `scope='PROMOTED'` — checked before, and independent
     of, the other vetoes and judge availability. Tests:
     `test_promoted_survivor_veto_blocks_merge`,
     `test_promoted_absorbed_veto_blocks_merge` (correlation-service level),
     `test_promoted_node_veto_blocks_merge_via_lifecycle_path` (end-to-end
     through `LifecycleService`). Also fixed a pre-existing latent bug in the
     shared `StubMemoryGraphAdapter.fetch_entity_merge_metadata` test fixture
     (wrong signature: took a single uuid, returned `dict|None`, never
     actually matching the real `list[str] -> list[dict]` interface — dead
     code path until this change made it reachable unconditionally).
   - **Stage 3 (contradiction-queue routing) — DONE.** `LifecycleService._check_contradictions_batch`
     looks up scope for both nodes in a conflict-range pair before flagging;
     if either is `PROMOTED`, `set_conflict` is called with
     `initial_status="unresolved"` instead of `"pending_llm_review"` — this
     reuses an existing `ConflictStatus` value (LLM-confirmed genuine
     contradiction) per the plan's "don't invent a new status" guidance, and
     is not auto-adjudicated by `confirm_pending_conflicts`'s symmetric LLM
     voter (which defaults to scanning `pending_llm_review` only and has no
     notion that one side is ground truth) — it surfaces for manual operator
     review instead. Test: `test_conflict_against_promoted_node_routes_to_unresolved_not_pending`
     (plus a same-file assertion locking down that ordinary conflicts still
     default to `pending_llm_review`).

   All 3 stages verified together: 323 targeted tests passing.
   PROMOTED is not a renamed `user_flagged` — it is a distinct, stronger,
   operator-curated "verified ground truth, cannot be false" tier, separate
   from `user_flagged`'s "important to the user" semantics. Spec (user-confirmed
   2026-07-11):
   - **Writer**: only a new operator-tier tool (e.g. `promote_memory`) sets
     `scope='PROMOTED'`. Not auto-propagated like `user_flagged`; a deliberate
     curation action, gated at `required_tier="operator"` same as
     `resolve_conflict`/`scan_for_conflicts`.
   - **Immune to correlation/merge absorption**: `CorrelationService`
     (post-SSOT-03 consolidation) and any absorb/merge path must refuse to
     absorb a `PROMOTED` node into another node's identity — it is never a
     merge target or source.
   - **Confidence pinned at 1.0, never adjusted**: exempt from the
     `correlation_queries.py:174-177` confidence-drift-on-absorb math
     (`survivor.source_confidence = ... + 0.1` clamp) — a PROMOTED node's
     `source_confidence` is always read/written as `1.0`.
   - **Excluded from symmetric contradiction/conflict scanning**: a new claim
     that would conflict with a PROMOTED node must not become a normal
     two-sided `pending_llm_review` conflict pair. Instead queue it for
     manual review with the PROMOTED node treated as ground truth (exact
     queueing mechanism to be designed during implementation — reuse the
     conflict pipeline's existing review states rather than inventing a new one).
   - **Deletion**: stays immune to decay/GONE cleanup (already true today via
     `consolidation_queries.py:254-261,768-772`); removable only through the
     existing `allow_promoted_removal` operator override on `resolve_conflict`.
   - Implementation note: this is the largest single item in Phase 3 — treat
     it as its own sub-plan/PR sequence (writer tool → absorption guard →
     confidence pin → contradiction-queue routing → tests for each), not one
     commit.

8. **SSOT-09 (Med): Truth confidence fragmented (`1.0` vs `0.9` for source=user). DECIDED: 1.0 is canonical; runtime has drifted. FIXED (scoped).**
   Fixed `source_confidence_for("user")` to source `SOURCE_CONFIDENCE_USER` from
   `domain/truth/kinds.py` (now `1.0`); updated the pinned test. Left
   `domain/artifacts.py`'s separate `_CONFIDENCE_TRUSTED = 0.9` untouched --
   it maps to a "TRUSTED/evidence-backed" review-state concept analogous to
   `STRUCTURAL` (0.9), not `USER`, and isn't clearly wrong; changing it would
   be a second, undecided judgment call outside what was scoped with the user.
   Per `docs/research/privacy/trusted-memory-admission.md`, `kinds.py`'s
   `SOURCE_CONFIDENCE_USER=1.0` is the documented canonical intent (the
   Trusted Memory Admission design's whole T3–T5 ladder assumes `user` is the
   apex trust tier at 1.0) — `domain/utils.py:source_confidence_for("user")
   == 0.9` is a drift bug, not an alternate valid design, and even that
   research note is unaware the drift happened.
   - Fix `domain/utils.py:source_confidence_for` so `"user"` maps to `1.0`
     (matching `kinds.py`), sourcing the value from `kinds.py`'s constant
     rather than a re-hardcoded literal (this also resolves the "centralize
     in domain/truth/kinds.py" part of the original finding).
   - Update the pinned assertion at `tests/test_utils.py:19` from `0.9` to
     `1.0` — call out in the commit message that this is a deliberate,
     user-authorized behavior correction (drift fix backed by the research
     doc), not a mechanical refactor-preserves-behavior change.
   - Low blast-radius: `attestation.py:review_state_from_confidence` buckets
     `>=0.9` into `HUMAN_REVIEWED` already, so this does not change
     `ReviewState` classification — it only changes the raw stored float
     (affects `CorrelationService` absorption-clamp math and any consumer
     reading the raw confidence number).
   - **Explicitly out of scope**: capability separation so agents cannot
     self-declare `source="user"` to claim the tier. Per the same research
     note this is a known, accepted risk at current single-user scale,
     parked until a hosted/multi-user deployment or signing surface exists.
     Do not build an admission firewall as part of this fix.

## Phase 4 — Documentation/registry generation (mechanical, low risk)

9. **SSOT-10 (Med): Tool/feature/endpoint/concept registries disagree. FIXED 2026-07-12.**
   (43 actual tools as of this fix -- 42 at review time, +1 from the SSOT-08
   `promote_memory` tool added earlier this session -- vs README's stale 23;
   Explorer taxonomy had 8 omissions + 2 phantom tools `memory_gateway`/
   `recover_memory`; `.agent/endpoints.md` had sections for only 31/42;
   `.agent/concept-ids.yaml` had `mcp.tool.*` entries for only 15/42 plus a
   missing `model.todo`.)
   - Did not build a doc-generation pipeline (out of scope for this pass --
     would be a much larger feature). Instead: fixed all four surfaces to
     match `menhir.mcp.tools.ALL_TOOLS` (the actual runtime registry) as of
     now, and added regression tests that fail on future drift, which
     satisfies the finding's actual ask ("a completeness test that fails on
     drift") without inventing a generator this codebase doesn't have yet.
   - `explorer/feature_taxonomy.py`: added the 8 missing tools to their
     natural parent groups, removed the `memory_gateway`/`recover_memory`
     phantom entries and the now-empty `gateway` group. Added
     `test_every_registered_tool_is_classified` and
     `test_no_phantom_tools_in_taxonomy` in `tests/test_feature_taxonomy.py`.
   - `.agent/endpoints.md`: added the 12 missing tool sections (`add_candidate`,
     `close_memory`, `close_stale_todos`, `get_provenance`, `list_clients`,
     `mint_client`, `pause_scheduler`, `promote_memory`, `rate_recall`,
     `resume_scheduler`, `revoke_client`, `unflag_memory`), each written from
     the tool's own docstring/endpoint implementation for accuracy, placed
     next to their closest topical neighbor rather than reordering the file.
   - `.agent/concept-ids.yaml`: added the 28 missing `mcp.tool.*` entries plus
     `model.todo` (the persistent `:Todo` node schema, already documented in
     `data_models.md` but never registered as a concept id).
   - New `tests/test_registry_completeness.py` (deliberately regex-based, not
     PyYAML-based, since PyYAML is not a declared project dependency):
     `test_endpoints_md_documents_every_registered_tool`,
     `test_concept_ids_yaml_declares_every_registered_tool`,
     `test_concept_ids_yaml_has_no_phantom_tool_entries`,
     `test_concept_ids_yaml_declares_model_todo`. All four fail loudly the
     next time a tool is added/removed without updating docs.
   - Also corrected the hardcoded "23 tools" count in `README.md`'s
     architecture diagram and `.agent/architecture.md`'s package-layout line
     to the current 43 (a plain literal, not test-enforced — left as a
     lower-value follow-up if this needs to stay perfectly in sync; the
     doc-completeness tests above are the load-bearing guard for this
     finding).

10. **SSOT-11 (Med): `MEMORY_RETURN_FIELDS` omits active processing fields. FIXED 2026-07-12.**
    - Extracted `_PROCESSING_DETAIL_FIELDS` in `infrastructure/cypher.py` as
      the shared base of processing fields common to both projections
      (state, stage, substage, substage_started_at, progress, steps
      total/completed, LLM tasks attempt/total/last-task-at, the 4 active-LLM
      fields, queued_at, reference_time, owner, lease/heartbeat/started/completed
      timestamps, processing_error). `MEMORY_RETURN_FIELDS` and
      `EPISODE_PROCESSING_FIELDS` both now spread it in, each adding only its
      own genuinely-different bits (identity fields, `processing_attempts` --
      kept as two intentionally different expressions, raw vs.
      coalesced-to-int-default-0, since that's an existing per-view choice,
      not drift -- and `EPISODE_PROCESSING_FIELDS`'s `namespace`).
      `MEMORY_RETURN_FIELDS` grew from 28 to 41 fields (all existing
      `TestFieldConstants` min-count/membership/no-duplicate-alias checks in
      `tests/test_cypher.py` still pass unmodified).
    - Added `test_memory_return_fields_is_processing_field_superset` in
      `tests/test_cypher.py::TestFieldConstants` — every `processing_*` field
      alias present in `EPISODE_PROCESSING_FIELDS` must also appear in
      `MEMORY_RETURN_FIELDS` (the field-superset invariant the finding asked
      for). `namespace` is exempt (not a processing field; a separate,
      pre-existing design choice that `MEMORY_RETURN_FIELDS` doesn't project
      it, unrelated to this finding's drift).

11. **SSOT-12 (Med): Symbol-path construction duplicated. FIXED 2026-07-12.**
    - Confirmed the two implementations were byte-identical logic (both
      `f"{file_path}::{parent}.{name}"` vs. `f"{file_path}::{name}"`).
      Extracted `domain/utils.py::symbol_structure_path(file_path, name,
      parent)` -- takes plain strings rather than a `SymbolEntry` instance so
      it stays a domain-layer utility with no dependency on the
      infrastructure-layer scanner dataclass. `project_scanner._sym_path` and
      `structure_queries._symbol_path` now both delegate to it (kept as thin
      wrappers at their existing call sites rather than renaming every call
      site, to minimize the diff).
    - Added `TestSymbolStructurePath` in `tests/test_utils.py`, including
      `test_scanner_and_query_writer_agree_on_the_same_corpus` — exercises
      `_sym_path`/`_symbol_path`/`symbol_structure_path` directly against the
      same `SymbolEntry` corpus (class, method, top-level function) and
      asserts all three agree, so the two call sites can never independently
      drift again.

12. **SSOT-07 (Med): Config loader/env-name drift + boolean parser disagreement.
    PARTIALLY FIXED 2026-07-12 (concrete bug + doc sweep done; OAuth-snapshot
    routing scoped out, see below).**
    - **Boolean parser unification — DONE.** Extracted `parse_bool_env()` in
      `config/settings.py` (single canonical truthy set `("true", "1",
      "yes")` — deliberately excludes `"on"`, which no flag in this codebase
      documents as accepted) and routed all 20 `MENHIR_*_ENABLED`-style flags
      in `MemorySettings.from_env()` through it (mechanical regex
      substitution, verified same call sites). `api.client_token_store.client_tokens_enabled()`
      no longer has its own ad hoc parser (which included `"on"`) — it now
      delegates to `MemorySettings.from_env().client_tokens_enabled`, so the
      two can never disagree again. Regression tests:
      `tests/test_settings.py::test_client_tokens_enabled_from_env` and
      `tests/test_client_token_store.py::test_client_tokens_enabled_agrees_with_memory_settings`,
      both parametrized including `"on"` explicitly proving it's `False`
      everywhere now (previously `True` via the client-token-store path only).
    - **Doc/`.env.example` sweep — DONE.** Confirmed which advertised env
      vars are actually read (`grep` across `src/`) vs dead:
      - Dead, removed from `.env.example`: `YAWN_MEMORY_MCP_TELEMETRY_DB`,
        `YAWN_MEMORY_MCP_TIMEOUT`, `YAWN_MEMORY_EXPLORER_HOST`,
        `YAWN_MEMORY_EXPLORER_PORT` — replaced with the real names
        (`MENHIR_MCP_TELEMETRY_DB`, `MENHIR_MCP_TIMEOUT`,
        `MENHIR_EXPLORER_HOST`, `MENHIR_EXPLORER_PORT`).
      - Dead, never read anywhere in `src/`: `GRAPHITI_LLM_BASE_URL`,
        `GRAPHITI_LLM_API_KEY`, `GRAPHITI_LLM_CHAT_MODEL`,
        `GRAPHITI_EMBED_BASE_URL`, `GRAPHITI_EMBED_API_KEY`,
        `GRAPHITI_EMBED_MODEL`, `OPENAI_BASE_URL` — corrected in
        `.agent/architecture.md` and `.agent/data_models.md` (endpoint/key/model
        for Graphiti's selected provider come from that provider's own
        settings, e.g. `OPENAI_*` or `LOCAL_LLM_*`/legacy `LLAMA_*`; the
        `GRAPHITI_*` vars only ever select a provider name, never an
        endpoint).
      - `README.md` and `.agent/data_models.md` updated to lead with the
        canonical `LOCAL_LLM_*` names (`LOCAL_LLM_BASE_URL` etc.), noting
        `LLAMA_*` as the still-accepted legacy alias rather than presenting
        it as primary.
      - Left untouched (real, actually read, not drift): `GRAPHITI_LLM_PROVIDER`/
        `GRAPHITI_PROVIDER`/`MEMORY_GRAPHITI_PROVIDER` (provider selection
        aliases), `GRAPHITI_EMBED_PROVIDER`, `GRAPHITI_RERANKER_PROVIDER`,
        `GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS`,
        `GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS`,
        `GRAPHITI_REQUEST_STALL_TIMEOUT_SECONDS` (all genuinely read via
        `_getenv` alias chains in `settings.py`).
    - **OAuth/client-token modules routed through `MemorySettings` snapshot —
      NOT DONE, scoped out.** On inspection this is materially bigger than
      "mechanical": none of the ~10 env vars involved
      (`MENHIR_OAUTH_AS_CODE_TTL_S`, `MENHIR_OAUTH_AS_CONSENT_SECRET`,
      `MENHIR_OAUTH_AS_CONSENT_TTL_S`, `MENHIR_OAUTH_AS_SESSION_TTL_S`,
      `MENHIR_OAUTH_AS_ACCESS_TTL_S`, `MENHIR_TRUSTED_PROXY`,
      `MENHIR_CORS_ORIGINS`, `MENHIR_STARTUP_SCOPE`, `MENHIR_INSTANCE_ID`,
      plus one more `oauth_as_register.py`/`oauth_rate_limit.py` each) exist
      on `MemorySettings` today — doing this properly means expanding the
      settings dataclass by ~10 fields and touching 6 auth-critical files
      (`oauth.py`, `oauth_authorize.py`, `oauth_token.py`,
      `oauth_as_register.py`, `oauth_rate_limit.py`, `auth_code_store.py`,
      `server.py`, `routes.py`), each with TTL/type-coercion/default-preservation
      risk. Also worth noting: `oauth_rate_limit.py:70`'s
      `MENHIR_TRUSTED_PROXY` parser has the exact same "on"-inclusive ad hoc
      boolean-parsing pattern as the fixed `client_tokens_enabled()` bug —
      worth revisiting together with this refactor, not separately. Left as
      a follow-up; not attempted in this pass to avoid rushing changes into
      authentication-critical code.
      **Sub-plan written 2026-07-12**:
      [`.agent/archive/plans/menhir-oauth-settings-snapshot-routing-2026-07-12.md`](menhir-oauth-settings-snapshot-routing-2026-07-12.md).
      Investigation for that plan found the gap is bigger than originally
      estimated here: `oauth.py`'s `_get_setting()` already looks settings-first
      for 16 `oauth_*` attributes, but none of them exist on `MemorySettings`
      today, so every one of those 16 silently falls through to env
      unconditionally — not just the ~10 stray call sites listed above. Not
      started; see the sub-plan for the full file-by-file inventory and
      suggested execution order.

13. **SSOT-13 (Low): Version drift (`0.2.0` vs Explorer's `0.1.0`, stale title). FIXED 2026-07-12.**
    - `menhir/__init__.py`'s `__version__` now resolves once via
      `importlib.metadata.version("menhir")` (falls back to `"0.0.0-dev"` via
      `PackageNotFoundError` when running from an uninstalled checkout)
      instead of a third hardcoded literal that had already drifted from
      `pyproject.toml`'s `0.2.0`.
    - `api/server.py`'s `FastAPI(version=...)` and `explorer/app.py`'s
      `FastAPI(version=...)` both now read `menhir.__version__` instead of
      their own separately-hardcoded `"0.2.0"`/`"0.1.0"` literals.
    - Fixed Explorer's stale title `"cth.mcp.memory explorer"` -> `"menhir
      explorer"` (`explorer/app.py`). Left `api/server.py`'s `"yawn-memory"`
      title untouched -- out of scope for this finding (that's the app's
      `title=` string, not a version; `tests/test_mcp_remote.py` and
      `tests/test_mcp_server.py` assert on `"yawn-memory"` as the registered
      MCP server name, a naming decision distinct from the version-drift bug
      this finding is about).
    - `tests/test_scaffold.py::test_version` updated from asserting the
      literal `"0.2.0"` to asserting `__version__` resolves to a non-empty
      string -- deliberate, not mechanical: pinning a duplicate literal in
      the test would recreate the exact drift-prone pattern this fix removes.

## Open decisions — RESOLVED 2026-07-11

All three flagged decisions were worked through with the user and are now
settled (see SSOT-06, SSOT-08, SSOT-09 above for the full specs):

- **SSOT-06**: `150` is the canonical conflict-scan default.
- **SSOT-08**: build a real third tier — PROMOTED is operator-curated,
  merge-immune, confidence-pinned, contradiction-scan-excluded ground truth;
  distinct from `user_flagged`.
- **SSOT-09**: `1.0` is canonical for `source="user"` (matches `kinds.py` and
  the Trusted Memory Admission research doc); the runtime's `0.9` was drift,
  now corrected. Capability separation (agents can't self-claim the tier)
  stays explicitly out of scope/parked.

No open decisions remain — Phase 3 can proceed without further check-ins.

## Suggested execution order

Phase 1 → Phase 2 → Phase 3 → Phase 4.
Phases 1–2 are safe to do back-to-back in one session (small, well-localized,
each with a clear regression test). Phase 3 items are one PR each per the
review's own module boundaries; SSOT-08 (PROMOTED) is the largest and should
be its own sub-sequence of commits. Phase 4 is mechanical and can be delegated.

## Status — 2026-07-11

All four phases are now DONE (2026-07-12). SSOT-01 through SSOT-13 are all
fixed and committed except the deliberately-scoped-out OAuth-snapshot-routing
follow-up within SSOT-07 (see that section for why). All 13 findings from the
2026-07-11 SSOT review have been addressed or explicitly disposed with a
documented, user-visible rationale.

## CI suite-hang — RESOLVED 2026-07-12

The GitHub Actions CI hang (and the original SSOT review's "pytest -m unit
made no progress after 68 tests" report) is now root-caused and fixed, not
just timeout-contained. Root cause: `ProcessingState` is a `(str, Enum)`
mixin; `str(ProcessingState.READY)` returns the qualified repr
`"ProcessingState.READY"`, not the value `"READY"`. Two comparison sites in
`services/ingest_service.py` (`wait_for_episode_processing` and
`ingest_episode`'s status mapping) wrapped the enum in `str()` before
comparing against `{ProcessingState.READY, ProcessingState.FAILED}`, so the
membership check could never match — `wait_for_episode_processing` always
burned its full `timeout_s` regardless of actual state, and under xdist a
worker hitting this got amplified into an indefinite whole-run hang (xdist's
own separate bug: a stalled/crashed worker hangs the run instead of failing
fast). Fixed both sites to compare the raw value directly (works whether the
row holds the enum member or a plain string, since str-mixin enums compare
equal to their string value either way). Two regression tests added
(`tests/test_edge_cases.py`, `tests/test_milestone_two_contract.py`) proving
READY resolves on the first poll instead of stalling for the full timeout.

Verified end-to-end on commit `376fe20` (`gh run list --repo Archolith/menhir`):
full unit suite (1513 selected) now completes in **50s** (job total 1m43s),
down from indefinite hangs killed only by the 15-minute job timeout. Only
remaining failures are the already-known, separate
`tests/test_llm_compression.py::TestDecayLLMWiring` LLM-wiring issue (4
tests, `assert 0 == 1`) — untouched by this fix, tracked as its own
follow-up. `-n auto`/xdist parallelism stays off in CI for now (its
crash-then-hang failure mode is a separate, unfixed issue); serial `pytest -m
unit -v` is fast enough on its own that this isn't currently a priority.

CI can now be trusted as a real signal for Phase 4 work.

## Remaining test-suite red — RESOLVED 2026-07-12

After Phase 4 landed, a full local `pytest -m unit` run (1520+ tests, 541s
serial) surfaced the previously-known `TestDecayLLMWiring` failures plus one
new-looking flake. Chased all of it to green:

- **`TestDecayLLMWiring` (4 failures)**: root cause was not LLM wiring at all
  -- the test's `_setup_compressible_candidate` fixture was stale relative to
  the F2 "lawful sharpness" rewrite, which switched
  `LifecycleService._count_similar_nodes` from a `search_scored_results`-based
  count to `count_similar_by_cosine`. The fixture kept setting the old field;
  the new one silently defaulted to 0 similar nodes, so sharpness always came
  out 1.0 and the node never qualified for compression -- the LLM was never
  even called. Fixed by also setting `count_similar_by_cosine_results = 4`.
- **`test_returns_llm_summary`** (tracked separately all session as an
  unexplained standalone hang): actually a real 30-second delay, not a hang.
  `_build_llm_adapter()`'s base URL matches `should_use_scheduler`'s
  recognized local-scheduler defaults, so the unmocked `acquire_llama_url_async`
  made a real `httpx` POST to a scheduler that isn't running, only falling
  back after its full 30s timeout. Added an autouse fixture patching it to
  fall back immediately by default in `TestCompressContentUnit`.
- **`test_maintenance_scheduler_heartbeat_keeps_lease_alive_during_long_job`**:
  a genuine one-off flake under a 541s heavy serial run, not a regression --
  passed standalone. Real wall-clock sleep windows were thin enough that
  system load could delay a heartbeat tick past lease expiry. Doubled every
  window to give comfortable slack without changing the test's intent.

Full local `pytest -m unit` is now green end to end.
