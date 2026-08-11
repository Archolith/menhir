# Menhir Changelog Archive

Entries older than the 10 most recent, archived from `CHANGELOG.md`.

- 2026-07-18: 2026-04-11 .. 2026-07-13 (89 entries).
- 2026-08-07: 2026-07-13 .. 2026-08-06 (29 entries).
- 2026-08-09: 2026-08-06 .. 2026-08-06 (5 entries).
- 2026-08-09: 2026-08-07 (1 entry).
- 2026-08-10: 2026-08-07 (1 entry).
- 2026-08-11: 2026-08-07 (1 entry).

---

## 2026-08-07 — docs: correct stale local-Docker Neo4j guidance in the operations runbook

- `operations_runbook.md` still described the pre-migration local-Docker `yawn-neo4j` workflow
  (`docker ps`/`docker start yawn-neo4j`, `bolt://localhost:7687`) even though `start-server.ps1`
  and `.env` (`NEO4J_URI`) have pointed at a remote host running `menhir-neo4j.service` (systemd)
  for some time — the desktop hasn't run Docker for menhir's Neo4j at all. Rewrote the startup
  "Behavior" bullets and the "Recover from local Neo4j-down startup failure" section to match
  actual current behavior: remote bolt-port probing, warn-and-continue on unreachable, and
  recovery via checking the remote host and starting `menhir-neo4j` there over SSH. Noted the
  root `docker-compose.yml` is vestigial. No code changed.

## 2026-08-07 — fix: thread `namespace` into `query_structure`'s `blast_radius` dispatch

- `QueryStructureTool.endpoint` accepted a `namespace` parameter but its call into `_dispatch`
  dropped it, and `_dispatch`'s own signature never declared it either — yet the `blast_radius`
  branch referenced the bare name `namespace`, raising `NameError: name 'namespace' is not
  defined` on every single `blast_radius` call, unconditionally, regardless of whether a
  namespace was supplied. No test exercised this dispatch path at all, so it shipped silently.
  Threaded `namespace` through the `_dispatch` call site and signature; added a regression test
  covering the exact failure shape.

## 2026-08-07 — fix: validate `namespace` at the `add_memory` call boundary

- Added `namespace_group_id_error()` in `domain/namespace.py`, mirroring graphiti's
  `validate_group_id` character rule (`^[a-zA-Z0-9_-]+$`), and wired it into
  `AddMemoryTool.endpoint` so an invalid `namespace` (e.g. `workspace:ideaprojects`, which is
  the shape expected by the adjacent `bootstrap_scope` param, not `namespace`) fails immediately
  with an actionable message instead of queuing successfully and only surfacing as a
  `GroupIdValidationError` deep in the background enrichment worker, long after the caller is
  gone. Root-caused from two episodes stuck permanently `FAILED` with this exact error.

## 2026-08-06 — feat: persist exact provider token telemetry

- Added one idempotent SQLite `llm_usage_events` row per terminal provider-client call, including
  run/episode provenance, model and endpoint, latency, failures, raw provider usage, and exact
  input/output/total/cached/reasoning token counts when the provider supplies them.
- Instrumented async OpenAI-compatible chat, Responses, and embedding calls plus Gemini REST chat,
  synchronous scalar chat, and synchronous View embeddings without changing caller behavior.
- Added aggregate APIs and LongMemEval `ingest_llm_usage.json` export so future canonical runs retain
  independently auditable ingest totals; missing provider usage stays explicit and is never guessed.

---

## 2026-08-06 — feat: add offline scalar dependency evidence transport checkpoint

- Implemented and independently approved the immutable Phase 0 transport and Phase 1 pure
  source/proposal validator. Source-bound offsets, hashes, supported transport versions, bounded
  metadata, expected parser/model/pipeline provenance, deterministic evidence hashes, and quote-free
  fail-closed receipts are covered by 57 focused tests and 387 broader scalar tests; Ruff,
  `py_compile`, and `git diff --check` pass.
- The validator keeps bridge, rule, and composer versions separate (`rule_version` and
  `composer_version` remain `not_applied`). Bench now has a pinned converter/adapter with an
  independently smoke-tested real model (31 relevant Bench tests passed).
- Added the first absolute-only dependency rule: canonical-self `I`, unitless integer count,
  allowlisted present predicate, literal specific target, exact source order, and closed dependency
  topology. It calls `compose_scalar_identity` only after bridge validation and emits a bounded,
  quote-free rule receipt; 33 focused rule tests pass, with protected/unsafe target defenses.
  The earlier combined relevant transport/bridge/rule/composer suite had 245 passing tests; the
  current independently verified core subset has 90 passing tests, with Ruff and `py_compile`
  clean. The authoritative frozen 48-case Phase-A Bench artifact records 5/6 supported exact
  identities and provenance, 0/14 unsupported compositions, 0/28 negative false-current
  admissions, 45/45 emitted evidence with bridge provenance, and stable composer replay for 45/45
  emitted cases. Three history cases fail closed at the canonical adapter and one supported bare
  `retain` case abstains with `predicate_invalid`; adding `retain` from that holdout would be
  post-hoc tuning and could admit ambiguous non-possession senses, so any expansion requires a
  separately authored independent policy panel. Recognition, role/operation, performance, cache,
  and full replay remain unmeasured; promotion is `not_evaluable` and runtime/production use remain
  unimplemented.

---

## 2026-08-06 — feat: add strict cumulative-completion research grammar

- Added structural-v4's strict unitless-count rule for full-span present-perfect cumulative
  completion claims: `I have completed|finished|closed N <literal target> so far|to date`.
  Exact proposal values and specific literal targets are required; subset/generic targets,
  non-count kinds, and any unconsumed tail abstain.
- Extended the opt-in isolated adapter to research-adapter-isolated-v2. Mapped compositions prove
  completed/finished/closed relation, value, literal target, and cumulative-marker cues with
  deterministic source ranges while preserving the original proposal, source key, offsets, and
  target provenance.
- Safety exclusions remain explicit: no simple/past-only or modal/negated claims, questions,
  hedges, history tails, coordination, second numbers, subset modifiers, current-total ambiguity,
  measurements, money, or `up to now` (the existing protected-role boundary remains unchanged).
- Independent cumulative panel: baseline clean 12/12 and noisy 6/12; isolated clean/noisy 12/12;
  isolated gained 6 noisy compositions; false-current-state errors remained 0/0.
- Frozen 36-candidate replay: baseline composed 0/36 versus isolated 1/36, with one
  answer-bearing candidate. This remains offline research evidence only.

---

## 2026-08-06 — feat: add mapped isolated scalar research adapter

- Added an opt-in, offline adapter that uses normalized clause text only as a structural-grammar
  probe, then rebuilds any composed identity from the original canonical proposal and literal source
  target. Deterministic character-range mapping preserves original UUID, offsets, source key, and
  target provenance; unprovable mappings and protected semantic roles abstain.
- Mapped results use distinct `research_normalized_structural_grammar` provenance and bounded,
  quote-free receipts. The path has no runtime, persistence, graph, audit, scheduler, settings, or
  LLM integration.

---

## 2026-08-06 — fix: reject subset and semantically empty quantity targets

- Tightened the structural quantity composer to abstain on explicit subset modifiers such as
  `other`/`additional`/`remaining` and generic targets such as `things`/`items`/`ones`. Specific
  targets remain open-world. Bumped structural derivation provenance to `structural-v3`.
- Added a narrow fail-closed adjacent-transposition veto between a literal one-token source target
  and the candidate attribute. It can reject `boosk` versus `books`, but never autocorrects or uses
  the candidate attribute to derive identity.

---

## 2026-08-06 — feat: add pure research scalar clause isolation

- Added an offline clause/evidence isolator with exact original offsets and a separately normalized
  research view. It handles bounded informal surface variants while preserving protected semantic
  roles and rejecting ambiguous corrections, competing numbers, or conjunctions.
- Isolation receipts are frozen, bounded, hashed, and quote-free. The isolator has no runtime,
  persistence, graph, audit, settings, LLM, or NLP-library integration.

---

## 2026-08-06 — feat: add pure research scalar adapter

- Added an offline adapter that validates research candidates through `parse_scalar_row` and
  reuses structural scalar composition without routing, persistence, audit, graph, scheduler, or
  LLM side effects. Receipts are immutable, bounded, and quote-free; claimed UUID/span metadata
  is cross-checked against parser-derived grounding.

---

## 2026-08-06 — feat: compose possessive self measurements

- Added fail-closed structural rules for direct self measurements in the forms `my weight is
  <number> <kg unit>` and `my height is <number> <cm unit>`. The target comes from the grounded
  source noun; model-provided attributes remain non-authoritative.
- Bumped structural derivation provenance to `structural-v2`. Wrong target/unit pairs, unsupported
  units, ranges, temporal tails, collective or owned subjects, questions, uncertainty, history,
  and lists continue to abstain.
- This post-v1 expansion was prompted by the independent generic semantic panel, not LongMemEval
  task text. The unchanged panel moved from 11/12 to 12/12 correct positive identities with zero
  wrong identities and 12/12 negative system non-admissions; promotion remains `not_evaluable`.
---

## 2026-08-05 — feat: wire deterministic typed-scalar shadow telemetry

- Wired the pure deterministic scalar extractor beside the existing committed LLM gate behind
  `MENHIR_SCALAR_DETERMINISTIC_SHADOW` (default off). The shadow runs once after gating and cannot
  alter binding, persistence, projections, authority, recall, LLM calls, or returned results.
- Added bounded, quote-free comparison telemetry with one-to-one exact/aligned agreement,
  router-miss attribution limited to fully eligible episodes, candidate/drop receipts, and
  fail-open error reporting. Recording requires the existing consolidation-audit toggle.
- Forwarded the flag through settings, runtime, scheduled consolidation, and manual Phase 3 runs;
  added default-off, forwarding, privacy, identity, truncation, and fail-open regressions.
- This ships observation only. Deterministic routing, class promotion, and LLM-call savings remain
  unimplemented pending held-out shadow measurements and the plan's pre-registered gates.
---

## 2026-08-02 — feat: namespace as a storage invariant on :Todo

- `:Todo` was the only operational surface ignoring namespace silos. Of 231 nodes, 166 carried
  `namespace='default'` and 65 carried none; the three newest had none, because `add_todo` had no
  namespace parameter and `create_todo` dispatches through the generic `_BACKEND_METHODS` path,
  never reaching `_resolve_namespace`. The populated nodes came from a writer that no longer exists.
- **Backfilled** the 65 nulls to `default` — 41 of them open, so doing this after the read filter
  would have hidden them. Zero nulls remain.
- **Write**: `create_todo` always persists a non-null namespace (explicit -> `x-yawn-namespace`
  header -> `default`). `add_todo` gains an optional `namespace`. Deliberately *not* a required
  argument — rejecting calls that omit it would break the hooks and every current caller, and
  `_resolve_namespace` documents `None` as legitimate global scope for memories.
- **Read**: `list_todos` / `get_todo` gain an opt-in `namespace`. Omitted preserves today's
  unfiltered behavior; supplied narrows to `namespace IN [requested, 'default']` so a client
  pinned via `MENHIR_CLIENT_NAMESPACES` sees the shared bucket rather than nothing. `get_todo`
  enforces only when given and always reports the namespace.
- `group_id` (empty string on all 166 pre-existing nodes) is vestigial and left untouched.
- 11 new tests; two exhaustive-kwargs assertions updated for the added parameter. Verified against
  the live graph: scoped reads still return the 114 shared todos, a foreign-silo todo does not leak
  into another silo's listing, and a wrong-namespace `get_todo` is refused.
- Plan: `.agent/plans/menhir-todo-namespace-invariant.md`.
---

## 2026-08-02 — feat: get_todo, a single-todo read that does not truncate

- Added `get_todo(uuid)` across the stack (`TodoRepository` → `MemoryGraphAdapter` →
  runtime/client backends → `_BACKEND_METHODS` → `GetTodoTool`). The TODO read surface was
  list-only and `list_todos` truncates content at 100 chars, so the body of a long
  multi-part todo was stored in the graph but unreachable through any tool.
- The response includes the edges written at create time — `REFERENCES_FILE` (linked file),
  `CREATED_FROM` (originating episode), `CONCERNS` (entities named in the content) — plus
  age/stale, dates, and the untruncated content.
- `list_todos` now appends a pointer to `get_todo` when any row was truncated, so the
  truncation is discoverable rather than silent. Readonly tier (`get_` prefix, remainder
  rule). Not pinned to `always_visible`; reachable via `search_tools`/`call_tool`.
- 7 new tests in `tests/test_todo.py`; `.agent/endpoints.md` and `.agent/concept-ids.yaml`
  updated, as the SSOT completeness tests require.
---

## 2026-08-02 — fix: enable WAL on the MCP telemetry DB

- **`infrastructure/telemetry/store.py`**: set `PRAGMA journal_mode=WAL` once during `_ensure_ready`,
  the durable fix for "database is locked" contention on `mcp_telemetry.db`. WAL lets readers proceed
  while a writer holds the DB, so the bounded `busy_timeout` in `_connect` stops being the only defence.
  Applied at init rather than per connection because journal mode is on-disk and persistent; a pragma
  failure is logged and swallowed so it can never break store initialization.
---

## 2026-07-29 — fix: fail-closed scalar-history write-side projections

- Keep scalar-history completion retryable across View writes, contributor redraws, count mismatches,
  merge/unmerge receipts, deletion repairs, and typed-persistence markers; malformed source times
  abstain the whole slot and report offending assertion IDs.
- Persist full contributor counts separately from bounded payload/truncation metadata, redraw all
  HISTORY_ENTRY ordinals, normalize `HistoryEntry` and arbitrary mappings before destructive Cypher,
  and preserve TurnEvidence `turn_id` separately from ADMITTED_ON source Episodic UUIDs.
- Decouple advisory history discovery from scalar-state authority and add focused failure/retry,
  20-entry, provenance, deletion, four-flag, malformed-time, and exact-postcard regressions.
- Serialize the duplicate `scalar_history_ops` audit snapshot before the shared `SET n += $extra`
  write, preventing Neo4j from rejecting a map-valued node property. The explicit ephemeral
  Neo4j gate now covers the exact `lme-01493427` postcard replay, full TurnEvidence/Episodic
  provenance, history-aware deletion repair, and namespace isolation.
- Make authority-off history discovery query-dependent by reusing the assertion-embedding search;
  collect only valid matched slots, keep raw observation/state-authority/suppression injection
  authority-gated, and remove namespace-wide history enumeration from recall. Added a shared-
  namespace unrelated-slot and malformed-hit regression while preserving the four-flag matrix.
---

## 2026-07-29 — fix: purge namespace-keyed episode lifecycle residue

- **`memory_graph_adapter.py` and `memory_queries.py`**: include `:Episodic` lifecycle rows keyed
  only by `namespace` in namespace deletion counts and teardown. Failed evidence projections can
  lack Graphiti's `group_id`; leaving one behind made a clean benchmark namespace rebuild succeed
  and then fail its integrity gate on the obsolete pre-reset error.
- **Tests**: lock both the safety-count and scalar-cascade delete queries to the namespace-keyed
  lifecycle clause. The strict zero-failure benchmark gate remains unchanged.
---

## 2026-07-29 — fix: resolve shorthand extraction from adjacent transcript context

- **`graphiti_extraction_patches.py`**: when the existing one-shot relationless repair fires, load
  bounded adjacent transcript context lazily and deliver it through Graphiti's native
  `previous_episodes` channel. Require every context-assisted repair edge to retain a meaningful
  literal anchor from the current turn, preventing prior user or assistant claims from being copied
  into a non-committal response.
- **`turn_evidence_repository.py`, `memory_graph_adapter.py`, `episode_lifecycle.py`, and
  `enrichment_steps.py`**: carry the episode's linked turn-evidence id through the claim boundary,
  retrieve only preceding user/assistant turns from the same namespace and session, and expose
  context-suppression telemetry without adding work to successful first-pass extraction.
- **Tests**: cover the exact stopped LongMemEval `user + 100` failure, native context delivery,
  endpoint grounding, lazy happy-path behavior, namespace/session scoping, and the copied-context
  negative control. Isolated live extraction produced the intended `100 business cards` relation;
  the thank-you control suppressed the copied prior claim and completed empty.
- **`.agent/architecture.md`**: document the bounded repair-context data flow and current-turn
  grounding invariant.
---

## 2026-07-28 — fix: preserve context-resolved combined-extraction endpoints

- Close a combined-extraction edge endpoint when its name is grounded in the previous episodes
  Graphiti supplied to the extractor, not only when it appears literally in the current turn.
  Pronoun and role-label endpoints remain forbidden. This preserves valid antecedent resolution
  such as `Rachel -> Chicago` for "She moved to Chicago." when the model omits Rachel from its
  entity list, avoiding deterministic edge loss and orphan-pruning collapse.
- Add a Python-boundary regression for the exact prior-context/current-pronoun failure shape.
---

## 2026-07-28 — fix: format Uvicorn access records without logging errors

- Configure `uvicorn.access` with Uvicorn's `AccessFormatter`, which expands its positional request
  tuple into `client_addr`, `request_line`, and `status_code` before applying Menhir's access format.
- Add a functional formatter regression covering the internal lifecycle-poll request shape and
  document the centralized logging requirement.
---

## 2026-07-27 — fix: stop embedding vectors from overrunning the extraction context

- Strip embedding vectors in `_safe_to_prompt_json`, the serializer every Graphiti prompt
  routes through. Graphiti hydrates `:Entity` attributes from `properties(n)` and pops only
  its own keys, so menhir's 1536-float `content_embedding` (~31KB serialized) survived into
  `attributes` and was splatted into the dedupe prompt for every candidate. At 15 candidates
  per extracted entity name the assembled request reached 1-3M tokens against a 128K limit;
  enrichment 400'd, the episode was marked FAILED with zero entities, and the memory became
  permanently unrecallable while `add_memory` still reported success. Measured on a live
  failing episode: 484,171 -> 19,391 chars for one extracted name. Both a key-name rule and a
  structural rule are applied so a future vector property does not reintroduce this.
- Add `GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS` (default 100000), checked against the assembled
  request immediately before the API call, and log the estimate on every response. The existing
  `GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS` measures episode text, which was ~1% of the payload,
  so it could never catch this. Oversize now raises `GraphitiRequestTooLargeError` naming the
  largest messages, and bypasses the retry loop since a retry only grows the payload.
- Register `graphiti_core.prompts.extract_nodes_and_edges` for prompt patching; it was missing
  from `_GRAPHITI_PROMPT_MODULES` despite the combined-extraction patch routing through it.
- Not addressed here: the 211 already-FAILED episodes still need a re-enrichment pass, and
  `add_memory` still reports success for writes that later become unrecallable.
---

## 2026-07-22 — refactor: decompose large service and infrastructure modules

- Split typed-scalar deterministic rules, assertion persistence/repair, and stateful coordination
  into focused service modules; keep `typed_scalar_perception.py` as the stable public facade.
- Reduce `recall_service.py` to the public dataclass/API and split retrieval policy, reusable
  pre/post operations, and the candidate/scoring/result pipeline into dedicated modules.
- Split SQLite telemetry connection/schema ownership from event, lifecycle, and recall/client
  persistence families; split Graphiti compatibility patches into extraction, model/dedup, and
  LLM-response owners behind the existing facade.
- Split View models, generic writes, scalar authority, and read/query operations; split typed
  assertions into model/query constants, ordinary writes, reconciliation/activation, and repair
  families behind composition facades.
- Split ingest queue lifecycle, enrichment workers, and intake operations; split lifecycle
  consolidation, decay/compression, and conflict workflows behind the existing service dataclasses.
---

## 2026-07-22 — refactor: extract scalar consolidation from scheduler tasks

- Move scalar dirty-target selection, paged typed perception, cursor advancement, duplicate-counter
  retirement, and binding/deletion/merge/orphan repair into `services/scalar_consolidation.py`.
- Keep `consolidate_personal_memory` as the scheduler facade and pass its existing counting LLM
  instance into the scalar runner, preserving one call budget across counter and scalar phases.
- Add architecture coverage preventing scalar implementation details from returning to
  `scheduler_tasks.py`; feature flags, result keys, defaults, and repair ordering remain unchanged.
---

## 2026-07-22 — refactor: enforce the core/MCP boundary

- Move the complete project-ingest workflow—validation, scan/write delegation, narrative queueing,
  timeout/error handling, and structured outcomes—into `services/project_ingest.py`; the MCP tool now
  only adapts backend/session arguments and formats the result.
- Move reader-id normalization into `core/reader_identity.py`, and make core runtime modules consume
  infrastructure telemetry directly instead of importing MCP compatibility exports.
- Remove the core-only MCP framework type import and strengthen architecture tests to reject every
  `core → menhir.mcp` or `core → MCP framework` dependency.
---

## 2026-07-22 — refactor: make recall/trace domain dependencies one-way

- Move `RelevanceBreakdown` and retrieval-trace dataclasses into the neutral
  `domain/retrieval_trace_models.py` owner.
- Remove the old `domain.retrieval_trace` module and migrate all in-repo imports to the canonical
  owner; this cleanup intentionally does not retain a legacy compatibility facade.
- Add architecture and serialization-shape coverage preventing the recall/retrieval-trace cycle from
  returning without changing ranking or trace behavior.
---

## 2026-07-22 — refactor: make config/OAuth dependencies one-way

- Move `AuthMode`, OAuth precedence, `OAuthConfig`, and OAuth environment/snapshot construction into
  `menhir.config`; the config package no longer imports API modules.
- Retain `menhir.api.auth_mode` and the config-related names in `menhir.api.oauth` as compatibility
  exports while API runtime code consumes the config-owned contracts.
- Make OAuth preflight depend directly on `config.oauth` and add architecture coverage preventing a
  config/API cycle from returning.
---

## 2026-07-22 — refactor: make backend/MCP dependencies one-way

- Move request-scoped caller ContextVars and backend credential resolution into neutral `core`
  modules while retaining the existing `mcp.service_access` compatibility surface.
- Move structural-project narrative construction from the MCP tool into
  `services/project_ingest.py`; runtime and MCP now depend on the shared application helper.
- Add architecture tests that reject core imports of MCP service/tool modules and any import cycle
  spanning `menhir.core` and `menhir.mcp`.
---

## 2026-07-18 — feat(view): ScalarStateView kind + entity-anchored subject_uuid keying (A+B)

Pieces A and B of the ScalarStateView plan (`.agent/plans/menhir-scalar-state-view-*`). Offline and
additive — no behavior change to existing kinds.

- **`view_repository.py`**: new `ScalarStateKind` (`kind='scalar_state'`, `lww_register=True`) — an
  entity-linked typed scalar register for the 9 typed ValueKinds, reusing the existing LWW/sig
  supersession machinery. Key discriminator is a canonical hash of `{attribute, scope, value_kind,
  unit}` (collision-safe; readable components kept as `ss_*` props). `record()`/`_key`/
  `_write_version` gain an **optional** `subject_uuid`: when present the `view_key` anchors on the
  resolved entity UUID (with `view_subject` keeping display text); absent = byte-identical legacy
  key; blank raises. `_scalar_norm()` normalizes heterogeneous values (numbers, ranges, booleans,
  string states).
- **`schema.py`**: new `entity_view_subject_uuid_idx` index.
- **`tests/test_scalar_state_view.py`**: 13 offline unit tests. Existing view/counter/metric/
  perception/fold/signature-parity tests unaffected.
- Pieces C (perception extension) + D (recall authority) remain gated pending re-approval.
---

## 2026-07-18 - chore: use public Archolith MCP framework

- **`pyproject.toml`** and **`uv.lock`**: replace the private `cth-mcp-framework` Git dependency with the public `archolith-mcp-framework` `v0.2.0` tag. Menhir retains its legacy import through the framework's compatibility package. The unit-test workflow now resolves the public tag instead of checking out the private repository.
---

## 2026-07-13 — docs: merge/delete lifecycle runbook + data-model closeout (Phase 7, partial)

Phase 7 closeout for the parts that belong to this plan.

- **`.agent/workflows/operations_runbook.md`**: a "Merge / unmerge / delete recovery" section — the
  operator surface for `scripts/unmerge.py` (`--inventory`, `--list`, `--op`, `--legacy-absorbed`),
  the recovery tiers, journal reconciliation after a crash (and why a delete is never auto-re-run),
  the `NEEDS_REVIEW` operator-only escape, and the report-not-cascade Evidence rule. All commands
  documented as dry-run-by-default.
- **`.agent/data_models.md`**: Entity lifecycle fields (`merged_from`, `merge_audit`,
  `last_merge_op_id`, `ttl_expires`, `restored_*`), the `graph_operations` journal table (kinds,
  states, fencing), the lossless snapshot envelope, and the five recovery tiers.

Deferred (recorded in the plan's closeout note): class-specific Fact/Metric stamps and the Metric
drift audit belong to `menhir-metric-provenance-redesign.md`, which is REVISED — OWNER CONFIRMATION
REQUIRED. They are not implemented here to avoid executing an unconfirmed plan.
---

## 2026-07-13 — fix: unmerge lineage cleanup over-matched sibling audit entries

Defect found by the Phase 5–6 review. `restore_merge_snapshot` cleared the survivor's `merge_audit`
with `WHERE NOT x CONTAINS $absorbed` — a bare-substring match over each JSON entry. But an audit
entry embeds the absorbed node's relationships, each with a `peer_uuid`, so when two RELATED nodes
are absorbed into the same survivor, one entry's JSON contains the other's uuid. Unmerging A then
also stripped B's audit entry, silently corrupting B's recoverability record (and mis-classifying it
toward `LINEAGE_ONLY` in the inventory).

The review rated this negligible ("~1/16^32 collision"). That framing was wrong: the trigger is not a
uuid collision but the sibling's uuid *appearing as a peer* — which is correlated with the merge
happening at all, since related nodes are exactly what merges. Reproduced with a live test, then
fixed: the cleanup now matches the serialized `absorbed_uuid` FIELD (`"absorbed_uuid": "<uuid>"`),
which is unique to this absorption's own entry. `merged_from` was already exact and unchanged.

- **`src/menhir/infrastructure/correlation_queries.py`**: match `$absorbed_audit_marker`, not the raw
  uuid substring.
- **Tests**: `test_unmerge_preserves_other_absorptions_audit_when_nodes_were_related` — absorbs two
  related nodes into one survivor, unmerges one, and asserts the sibling's audit entry and lineage
  survive. Closes the review's noted multi-merge test gap.
---

## 2026-07-13 — feat: journaled physical-delete paths (ENTITY_DELETE, SESSION_TTL_DELETE)

Phase 6. Both delete paths were destructive with no durable before-record. The explicit operator
delete ran a bare `DETACH DELETE`. The session TTL sweep was worse: it recorded its audit BEFORE
deleting, from the candidate list — but the mutation re-filters on `scope = 'SESSION'`, so a node
promoted in the race window survived while the audit already claimed it was deleted. It logged its
intent and called it a record. The missing durable-before-delete record is exactly why ~24 nodes
destroyed by the degree-zero orphan cleanup on 2026-07-12 were unrecoverable.

- **`src/menhir/services/delete_coordinator.py`** (new): `DeleteCoordinator` runs both paths as a
  journaled saga — a complete lossless snapshot of every target is committed as PREPARED before
  anything is destroyed (invariant 3), the exact deleted-uuid list comes back FROM the mutation, and
  absence is verified before COMMITTED. `reconcile()` observes a crash-left PREPARED delete: if the
  targets are gone it commits; if any survive it routes to NEEDS_REVIEW and does NOT re-delete
  (blindly re-running a delete could destroy nodes the crash spared).
- **`src/menhir/infrastructure/consolidation_queries.py`**: `delete_entities_returning_uuids` returns
  the uuids actually deleted (not just a count), so the audit records truth rather than intent; a
  target that changed scope in the race window comes back as `skipped`. `newly_unreferenced_evidence`
  REPORTS Evidence that would fall to degree zero but never deletes it — isolation is not
  authorization, which is the exact inference that caused the 2026-07-12 incident.
- **`src/menhir/services/lifecycle_service.py`**: the TTL sweep (`_expire_demoted_session_nodes`) now
  routes through the coordinator and audits only nodes actually deleted; skipped and newly-orphaned
  Evidence are logged, not acted on.
- **Tests**: 9 live Neo4j tests including the true race (a node promoted between the read and the
  write is recorded `skipped`, never falsely deleted), snapshot-outlives-node, evidence reported not
  cascaded, and both crash-recovery directions.
---

## 2026-07-13 — feat: degraded legacy-unmerge lane + merge recoverability inventory

Completes Phase 5. Historical merges (pre-journal) have only a lossy snapshot: a 10-property
allowlist, Entity-only peers, one relationship property, stringified temporals, and no survivor state
at all. They can be PARTIALLY reversed — and the real danger is handing back a partially-restored
graph and letting an operator believe it is repaired. This lane is built to make that impossible.

- **`src/menhir/domain/legacy_snapshot.py`** (new): parses the legacy entry AND enumerates, per
  snapshot, the classes of state it structurally cannot restore (`OMITS_NODE_LABELS`,
  `OMITS_NON_ENTITY_PEERS`, `OMITS_RELATIONSHIP_PROPERTIES`, `OMITS_TYPED_TEMPORAL_VALUES`,
  `OMITS_SURVIVOR_PRE_MERGE_STATE`, …). The oldest entries also predate `survivor_episodes_before`,
  so the MENTIONS rebind cannot be inverted at all — reported as `OMITS_SURVIVOR_EPISODE_BASELINE`.
- **`src/menhir/services/legacy_unmerge_coordinator.py`** (new): `LEGACY_ENTITY_UNMERGE`, journaled
  and atomic like the exact lane, but deliberately hostile to accidents — **manifest-gated** (it
  refuses to touch anything not explicitly named; there is no "reverse everything"), requires
  `acknowledge_degraded=True`, and **every result carries `exact: False`** plus the omission list. It
  does not guess at the survivor's prior state: `survivor_properties` is empty, so the survivor is
  left as-is rather than fabricated backwards.
- **`src/menhir/services/merge_recoverability.py`** (new): read-only inventory classifying every
  absorption into `EXACT` / `LEGACY_SIDECAR` / `GRAPH_SNAPSHOT_ONLY` / `LINEAGE_ONLY` / `MALFORMED`.
  `GRAPH_SNAPSHOT_ONLY` is flagged as a **wasting asset** — that snapshot lives on the survivor and
  dies with it, so the report tells you to run `backfill_merge_audit.py` now. `LINEAGE_ONLY` is
  reported as flatly **not recoverable**; its absorbed before-state is unknown and will not be
  reconstructed from the survivor.
- **`scripts/unmerge.py`**: `--inventory` and `--legacy-absorbed <UUID> …` (the uuids *are* the
  manifest). Dry-run by default; `--execute` acknowledges the degradation.
- **Tests**: 8 live Neo4j tests. The load-bearing ones pin the honesty properties — refuses outside
  the manifest, refuses until degradation is acknowledged, never reports `exact`, leaves the survivor
  untouched, and reports lineage-only merges as unrecoverable.
---

## 2026-07-13 — feat: exact ENTITY_UNMERGE (a real inverse, at last)

`scripts/unmerge.py` restored an absorbed node in FIVE separate statements guarded by "skip if the
node already exists". A crash after the first left a bare node with no edges — and every rerun then
SKIPPED it, so it stayed permanently half-restored, silently. Its snapshot was lossy, and it could
not reverse the survivor at all (its own docstring: "those survivor-side changes CANNOT be reverted
here").

Phase 5 of the merge/delete lifecycle remediation plan. An unmerge is now an exact, journaled,
replayable inverse — or it refuses.

- **`src/menhir/domain/merge_delta.py`** (new): the merge's survivor mutation is a PURE FUNCTION of
  the two pre-merge nodes, so it can be REPLAYED from the lossless snapshot. That gives us the
  invariant-9 test we could not otherwise perform: if the survivor still holds exactly what the merge
  wrote, nothing changed since, and reversing is safe; if it holds anything else, someone edited it
  afterwards and we must NOT clobber them.
- **`src/menhir/infrastructure/correlation_queries.py`**: `restore_merge_snapshot` inverts a merge in
  ONE atomic transaction (Neo4j 5.26 dynamic labels `:$(...)` and relationship types `-[r:$(t)]->`
  keep it fully parameterized — a label or type from a snapshot cannot inject Cypher). It recreates
  the node with exact labels and typed properties, restores every relationship instance, reverses the
  survivor's merge-owned delta, deletes only the bridges and rebound MENTIONS this merge created, and
  removes the absorption from lineage SUBTRACTIVELY so later merges survive.
- **`src/menhir/services/unmerge_coordinator.py`** (new): the ENTITY_UNMERGE saga, with three
  fail-closed guards before PREPARE — the graph must still be in the merge's after-state, the survivor
  must still hold what the merge wrote, and every snapshot peer must still exist. A missing peer is
  refused, never fabricated: the default is all-or-nothing. Supports `--dry-run`, is idempotent, and
  replays correctly after a crash.
- **`scripts/unmerge.py`**: rewritten as a thin CLI that issues NO restoration queries of its own. The
  unaudited direct-write path is GONE. Legacy pre-journal merges are reported honestly as
  not-exactly-reversible rather than half-restored (`--legacy-report`).
- **Tests**: 15 delta unit tests; 7 live Neo4j tests headlined by a true round trip — merge, unmerge,
  and the graph is exactly what it was (labels, typed values, every relationship instance including
  parallel multiplicity, provenance, and the survivor's pre-merge state). Verified load-bearing by
  mutation: disabling the survivor-delta restore makes it fail.
- **Removed `tests/test_unmerge_e2e.py`**: it covered the now-deleted direct-write path, and its
  idempotency test asserted `skipped == 1` — enshrining the very skip-if-exists behavior that left
  crashed unmerges half-restored. Every assertion has a strictly stronger counterpart in the new
  suite (mapping documented in its header).

Remaining for Phase 5: the manifest-gated `LEGACY_ENTITY_UNMERGE` degraded-recovery lane for the
historical lossy snapshots. Until it exists they are inventory-only, and the CLI says so.
---

## 2026-07-13 — fix: merge saga review defects (false drift, abstention fencing, size limit)

Three defects found by an independent review of Phases 3–4, all fixed with regression tests.

- **False NEEDS_REVIEW on a SUCCESSFUL merge** (the serious one). `merge_state_fingerprint` folded in
  `last_merge_op_id`, a survivor-GLOBAL "who touched me last" stamp. When a second merge absorbed a
  different node into the same survivor, it overwrote the stamp — so the first op's after-state check
  failed and a merge that had actually succeeded was quarantined. Reproduced (crash op A before
  COMMIT → merge B into the same survivor → reconcile A → drift). The fingerprint now keys only on
  pair-specific facts (`absorbed_present` + `lineage_recorded`, i.e. `$absorbed IN
  survivor.merged_from`), which uniquely identify *this* absorption; the pair fence already prevents
  a competing op from performing it. `last_merge_op_id` remains as an idempotency aid and audit
  breadcrumb, never a correctness gate.
- **A benign mutation-gate abstention fenced the pair forever.** When the in-mutation `WHERE` failed
  closed (e.g. a node became COMPRESSED between PREPARE and MUTATE), the coordinator marked the op
  NEEDS_REVIEW — quarantining a pair over an outcome with *no graph mutation at all*. Leaving it
  PREPARED would instead retry forever. Now it uses the plan's own terminal `FAILED` state ("no graph
  mutation occurred"), which the unresolved-key index does not fence, so the pair is released. The
  coordinator re-reads the graph and only takes the abstention at its word if the state still matches
  the expected before-state; if the graph moved, it is genuine drift → NEEDS_REVIEW.
- **Invariant 15 was unimplemented.** No snapshot size ceiling existed. Added `MAX_SNAPSHOT_BYTES`
  (8 MiB), `SnapshotTooLargeError` (a `SnapshotSchemaError` subclass, so existing fail-closed handlers
  still catch it), and the `MERGE_SNAPSHOT_TOO_LARGE` abstention reason. Enforced in `dumps()` so every
  writer inherits it, and enforced by **abstaining before PREPARE — never truncating**: a truncated
  snapshot silently destroys the inverse it exists to guarantee. `snapshot_size_bytes()` added for
  observe-mode reporting of near-limit high-degree hubs.
---

## 2026-07-13 — feat: lossless merge snapshots + journaled ENTITY_MERGE saga

The legacy merge deleted the absorbed node and THEN wrote a best-effort, failure-swallowing
telemetry row (`record_merge`, wrapped in try/except so "telemetry must never break the merge").
A crash — or a sidecar failure — between those two steps destroyed the node with no durable
snapshot: unrecoverable. And the snapshot it did write was lossy (10-property allowlist,
Entity-only peers, one relationship property, stringified temporals, no survivor state), so an
unmerge could never be an exact inverse.

Phase 4 of the merge/delete lifecycle remediation plan (workspace-root
`.agent/plans/menhir-merge-delete-lifecycle-remediation-2026-07-13.md`).

- **`src/menhir/domain/merge_snapshot.py`** (new): versioned, checksummed, lossless codec. Neo4j
  temporal/spatial values round-trip to the SAME driver types (never stringified); `properties(n)`
  is captured whole (no implicit allowlist — the excluded set is named in
  `SYSTEM_MANAGED_PROPERTIES` and tested); every relationship INSTANCE is preserved, so parallel
  edges and relationship properties survive. Unsupported version or bad checksum fails closed.
- **`src/menhir/infrastructure/correlation_queries.py`**: `capture_node_state` /
  `capture_merge_snapshot` read both identities' complete state (all labels, all properties, every
  incident relationship regardless of peer label, peer uuid/labels/identity). The survivor is
  snapshotted too — reversing a merge means reversing its delta, not just recreating the absorbed
  node. `merge_entity` accepts `operation_id` and stamps `survivor.last_merge_op_id` so a replay
  recognizes its own completed work. `fetch_merge_state` provides the saga's before/after state.
- **`src/menhir/services/merge_coordinator.py`** (new): the ENTITY_MERGE saga —
  ELIGIBLE → SNAPSHOT → PREPARED → MUTATE → VERIFY → COMMITTED, with `reconcile()` replaying rows
  left PREPARED by a crash. The recovery snapshot is durable *before* the graph is touched
  (invariant 3); only an exact after-state match may COMMIT (invariant 5); drift is quarantined as
  NEEDS_REVIEW and never auto-repaired.
- **`src/menhir/infrastructure/graph_operations.py`**: `ENTITY_MERGE` added to the closed kind enum.
  Its `target_key` is the normalized (order-independent) pair key, so the existing unresolved-key
  index fences a drifted pair against competing merges. `METRIC_WRITE` preserved verbatim
  (invariant 13).
- **`src/menhir/services/correlation_service.py`**: a judge-confirmed merge now executes through
  the coordinator, not the raw repository primitive — new merges cannot use the legacy unaudited
  path. A saga abstention falls through to conflict rather than silently dropping the pair.
- **Tests**: 19 codec unit tests; 7 live Neo4j saga tests (snapshot durable after the node is gone;
  PREPARE failure mutates nothing; crash-after-mutation replays as COMMITTED; a drifted pair is
  quarantined, never committed; an unresolved op fences the pair in both directions).

Known gap (tracked for Phase 5/6): fencing is by pair key only. Plan invariant 14 also calls for
fencing each participant UUID individually, which needs a journal schema change; a merge of A+B does
not currently fence an unrelated merge of B+C.
---

## 2026-07-13 — feat: mutation-time merge-eligibility gate

`merge_entity` matched on uuid + `:Entity` label only — it enforced NO eligibility. A stale
discovery result, a direct repository caller, or a node whose freshness/scope/flag changed after
discovery could drive a destructive merge with no recheck (violating remediation invariant 7). The
service-level vetoes (promoted/ineligible/co-mention/anchor) never checked freshness, namespace
match, `user_flagged`, or conflict state at all.

Phase 3 of the merge/delete lifecycle remediation plan (workspace-root
`.agent/plans/menhir-merge-delete-lifecycle-remediation-2026-07-13.md`). "Option A" semantics
(owner-approved): pure tightening that preserves current auto-merge timing — fresh entities are
SESSION-scoped with unstamped freshness, so unstamped freshness is treated as ACTIVE and SESSION is
allowed; see the invariant-8 note in the plan.

- **`src/menhir/domain/merge_eligibility.py`** (new): one pure policy — `NodeSignals` → `evaluate()`
  → `MergeEligibility(allowed, reason_code, diagnostics)` with stable reason codes. Hard-vetoes:
  missing node, same uuid, structural/path/View role, namespace mismatch, COMPRESSED/GONE freshness,
  PROMOTED scope, `user_flagged`, protected conflict (`pending_llm_review`/`unresolved`).
- **`src/menhir/infrastructure/correlation_queries.py`**: `evaluate_merge_eligibility` gathers both
  nodes' signals in one read and maps through the policy. `merge_entity` runs it as a fail-closed
  preflight and returns a structured abstention (`{"merged": 0, "reason": <code>}`) without mutating;
  the mutation additionally repeats the mutable predicates in its `WHERE` (defense in depth against a
  concurrent change in the race window → `ELIGIBILITY_CHANGED_AT_MUTATION`).
- **`src/menhir/infrastructure/memory_graph_adapter.py`**: delegates `evaluate_merge_eligibility`.
- **Tests**: 17 policy unit tests (`tests/test_merge_eligibility.py`); 9 live Neo4j gate tests
  (`tests/test_merge_eligibility_live.py`) proving each unsafe state abstains with the right reason
  and leaves the absorbed node intact, plus an eligible-pair positive control. Two repository unit
  tests updated for the new preflight query in the sequence (stubs only; merge assertions unchanged).

---

## 2026-07-13 — fix: atomic Metric changed-version write + cardinality-aware reconciliation

A changed Metric write created the new current version and marked the prior one noncurrent
in **two separate** `execute()` calls (`_write_version`). A crash between them left BOTH
versions current, and `fetch_metric_state`'s `ORDER BY created_at DESC LIMIT 1` then returned
the new node as a clean after-state — so the saga could mark a two-current graph COMMITTED.

Phase 2 of the merge/delete lifecycle remediation plan (workspace-root
`.agent/plans/menhir-merge-delete-lifecycle-remediation-2026-07-13.md`). The working saga
(PREPARED→MUTATE→VERIFY→COMMITTED, frozen-UUID replay, stale-skip, NEEDS_REVIEW fence, the
`METRIC_WRITE` kind string) is preserved unchanged; only the split mutation and the
reconciliation fingerprint are corrected.

- **`src/menhir/infrastructure/view_repository.py`**: create-and-supersede is now ONE atomic
  Cypher statement. Two `FOREACH`-over-`CASE` guards replace `WITH n WHERE $emb IS NOT NULL`
  (a `WHERE` filters the row out of the pipeline, which for a null embedding — every Metric —
  would drop `n` before the supersede clause; `FOREACH` applies conditional writes without
  gating the row). `fetch_metric_state` now returns `current_count` (via `collect`/`size`)
  instead of `LIMIT 1`, so a duplicate-current set can no longer be read as a clean single
  current.
- **`src/menhir/services/metric_write_coordinator.py`**: `state_fingerprint` folds in
  `current_count` (defaults to 1, so existing single-current callers and fakes are unchanged);
  the expected after-state asserts exactly one current. A graph with two currents no longer
  matches the expected after-state and routes to NEEDS_REVIEW.
- **`tests/test_metric_write_coordinator.py`**: fingerprint is now current-set-cardinality
  sensitive.
- **`tests/test_metric_coordinator_live.py`** (real Neo4j): a changed write leaves exactly one
  current; a pre-existing duplicate-current graph routes the changed write to NEEDS_REVIEW
  instead of COMMITTED.

---

## 2026-07-13 — fix: decay projection carries every lifecycle-policy input

The decay candidate projection (`fetch_decay_candidates`) omitted `type`,
`rehydration_count`, and `target_date_passed`. `LifecycleService` defaults a missing
`type` to `SEMANTIC`, so the active compression sweep evaluated **every** candidate
under SEMANTIC thresholds regardless of its real type (IDENTITY, TEMPORAL, PROCEDURAL,
...), and the `REHYDRATION_EXEMPT_COUNT` gate never fired because `rehydration_count`
defaulted to `0` at the policy boundary. Both are active correctness bugs, not latent
deletion risk — compression is enabled. Physical deletion remains disabled.

Phase 1 of the merge/delete lifecycle remediation plan (workspace-root
`.agent/plans/menhir-merge-delete-lifecycle-remediation-2026-07-13.md`).

- **`src/menhir/infrastructure/consolidation_queries.py`**: `fetch_decay_candidates`
  now projects `type`, `rehydration_count`, and `target_date_passed`, reusing the
  canonical derivations from `cypher.py` (`ENTITY_METADATA_FIELDS`). `target_date_passed`
  is computed in Cypher (`date(n.target_date) < date()`), consistent with the rest of
  the system; `_run_decay` already enriches `last_accessed_days_ago`.
- **`tests/test_decay_logic.py`**: projection contract test asserting the query exposes
  every policy alias, so a future edit that drops one fails.
- **`tests/test_lifecycle_service.py`**: two `apply_decay` boundary tests — one proving
  the complete input dict (incl. the enriched and newly-projected fields) reaches
  `should_compress`, one proving a rehydrated node at `REHYDRATION_EXEMPT_COUNT` is now
  protected from compression end-to-end.

## 2026-07-13 — feat: tier-scoped `tools/list` on the remote MCP surface

`tools/list` over `/mcp-http` and `/mcp/` advertised all 44 tools to every caller
regardless of auth tier. Invocation was already gated (`contracts.py` raises
`PermissionError` when a token's tier is below a tool's `required_tier`), but the
advertised catalog was not — so a low-tier client saw, and could try, tools it could
never call. Now `readonly` sees 15 tools, `agent` 26, `operator` all 44.

Beyond defense in depth, this is a context/accuracy fix: tool schemas are re-sent every
turn, and small models degrade at tool selection as the catalog grows. A local
Qwen3.5-9B agent on the `agent` tier now loads 26 tools instead of 44.

- **`src/menhir/api/mcp_remote.py`**: new `TierFilteredFastMCP`, a `FastMCP` subclass
  overriding `list_tools()` to filter by `get_request_tier()` against each tool's
  `required_tier` (unmapped tools default to `agent`, matching the invocation gate). Used
  by both the SSE and streamable-HTTP builders. An **empty** tier — local stdio trust
  (CT-002) or no-auth loopback — is deliberately not filtered, mirroring the invocation
  gate; a test pins this.
  Note: the gateway's `always_visible` pinning in `mcp/server.py` never applied to the
  remote surface and structurally could not — the remote uses the MCP SDK's `FastMCP`
  (`mcp.server.fastmcp`, no tool-transform hook), while the gateway uses the separate
  `fastmcp` v2 package. Overriding `list_tools()` is the only seam available there.
- **`tests/test_mcp_remote.py`**: 4 new tests (readonly/agent/operator/empty-tier);
  existing construction test repointed at `TierFilteredFastMCP`.
- **`.agent/endpoints.md`**: documented tier-scoped `tools/list` under the auth section.
  (The pre-existing claim that remote MCP is "narrower than stdio by design" was untrue
  before this change; it now holds for non-operator callers.)

## 2026-07-13 — feat: Metric class separation (instrumentation out of recall)

Instrumentation counters (failure/revision telemetry, perception run-tallies) are
moved out of the `:Entity` semantic-recall layer into a dedicated `:Metric` class,
so they stop competing with real memories in recall. Of 313 view nodes in prod,
only ~4 are real user facts; the rest is instrumentation. Landed:

- **Saga sidecar** (`graph_operations`, `metric_receipts`, `migration_batches`): a
  recoverable cross-database saga journal — the durable before-mutation record whose
  absence made the 2026-07-12 orphan-cleanup deletions unrecoverable. Append-only
  receipts, SHA-anchored batches, fencing.
- **`ViewClass{FACT,METRIC}` seam**: `ViewRepository` writes `:Entity` (fact) or
  `:Metric` (instrumentation) through a closed label allowlist. Label-scoped
  supersession + current-lookup, so a fact and a metric with the same key are
  independent. Metrics carry no `name_embedding` and no `MENTIONS`, excluded from
  semantic recall by construction. `:Metric` schema constraint + indexes added.
- **`MetricWriteCoordinator`**: every Metric write is a PREPARED→MUTATE→VERIFY→
  COMMITTED saga with fingerprint drift detection, fail-closed preconditions, a
  widened fence, orphan-receipt exclusion, and crash-recovery replay. Cutoff-bound
  telemetry folds; an absolute-recompute path for the (current) no-prune case.

- **Producers routed**: both telemetry bridges fold through the coordinator's
  absolute-recompute path (embedding dropped entirely — metrics never rank), and the
  perception/correction run-tallies route through an injected `run_tally`. All new
  instrumentation writes land as `:Metric`.
- **Durable fact provenance**: fact versions now store a sorted, deduplicated
  `episode_uuids` list and an exact `supporting_event_count`. Evidence used to live
  only in `MENTIONS` edges, so reaping an episode silently drained a fact's provenance
  while its summary kept claiming the supporting events. Unchanged-value rewrites union
  the UUIDs inside one Cypher statement (no read-modify-write race); missing episodes
  stay counted and are reported, and `MENTIONS` is repaired if the episode returns.
- **Recapture tooling** (`scripts/metric_recapture.py`): `plan` (read-only classifier +
  whole-batch preflight → reviewable manifest with per-node fingerprints), `apply`
  (deletes strictly the reviewed UUID list, refusing on any drift, and requiring a
  backup), `verify` (residual scan). Scoped to the provably re-foldable telemetry
  sources only; run-tallies are left to supersede themselves out. **Not yet run against
  prod** — pending owner go/no-go. Dry-run: 288 candidates, preflight clean, zero
  overlap with the protected facts/registers.

Hardened across four adversarial review rounds; ~75 metric-specific tests (offline +
live against a dedicated test Neo4j). The one-time migration of existing instrumentation
nodes is a **recapture** (delete regenerable nodes under a backup, re-fold as `:Metric`)
rather than a reversible saga — the nodes are folds over retained telemetry, so recapture
IS the recovery path.

## 2026-07-13 — fix: remove unsafe degree-zero orphan cleanup (P0)

- **Removed `cleanup_orphan_subgraphs()`** from the decay sweep. It inferred physical-
  deletion eligibility from node isolation alone — an unbounded global `MATCH (n:Entity)`
  `DETACH DELETE` with no check on freshness, memory type, age, sharpness, or the deletion
  gate. Its first real-database execution deleted ~29 isolated production nodes (5
  recoverable from the merge sidecar, ~24 not). Deleted the function, the
  `memory_graph_adapter` passthrough, the lifecycle call site, the `orphan_cleanup_enabled`
  setting, and `MENHIR_ORPHAN_CLEANUP_ENABLED` parsing. A normal decay sweep can no longer
  delete a node for being isolated; an isolated node (e.g. a sole neighbour left by
  `bridge_and_delete`) is now benign.
- `DecayResult.orphan_subgraphs_cleaned` is retained and hard-pinned to `0` for
  output/telemetry/replay compatibility; field removal is a separate follow-up.
- Physical deletion, if ever built, will go through an explicit terminal-state reaper
  (`.agent/plans/menhir-terminal-reaper.md`); degree is never deletion authorization.
- Supersedes the interim `MENHIR_ORPHAN_CLEANUP_ENABLED=false` containment. Plan (workspace-
  root artifact): `.agent/plans/menhir-orphan-cleanup-removal.md`, part of the split
  merge/decay/delete lifecycle remediation (`.agent/plans/menhir-orphan-cleanup-rewrite.md`
  index). Source: the merge/decay/delete lifecycle review (Codex, 2026-07-12).

## 2026-07-12 — feat: rich console dashboard + memory-content privacy redaction

- **Console dashboard** (`menhir console` / `start-server.ps1 console`): a live `rich`
  operator view — server/neo4j/ready state, queue + enrichment + scheduler metrics from
  `/api/ready` + `/api/stats`, and a live tail of `logs/server.log`. Uses the `rich`
  library already installed via typer (no new dependency). `start-server.ps1 console` now
  ensures the server is up (starts it in the background if needed) then attaches the
  dashboard; quitting (`q`) leaves the server running. The dashboard sends the
  least-privileged configured API key (read-only > operator > agent) so it works against a
  STATIC/authed server. ASCII-only rendering; rich substitutes safe box chars on legacy
  Windows consoles.
- **Privacy redaction** (`MENHIR_PRIVACY_REDACT`, default off): hides memory *contents*
  (content, summary, previews, names, graph node labels) at display time — the console log
  tail and the explorer web UI — for screen-sharing/demos. Log files and Neo4j are
  untouched. Central redactor in `src/menhir/privacy.py` (single source of truth).
  - Console: `p` key toggles redaction live (badge shows PRIVACY ON/OFF).
  - Explorer: server-side redaction so content never reaches the browser; a header toggle
    button + `menhir_reveal` cookie flip it per browser, but the cookie can only *reveal*
    on a loopback bind (privacy is not cookie-defeatable on a remote bind). Structural
    fields (uuid, scope, labels, timestamps, counts, relationship types) are never redacted
    so the graph stays navigable.
- Tests: `tests/test_privacy.py` (redaction unit), + explorer redaction/reveal-cookie/graph
  -label/toggle tests. 91 affected tests green.
- Docs: operations_runbook.md (console dashboard + privacy), security-posture.md. Plan:
  `.agent/plans/menhir-console-dashboard-and-privacy-plan.md`.

## 2026-07-12 — feat: seamless server lifecycle — console (live output), logs, one-line status

- `scripts/start-server.ps1` gains two actions and sharper existing ones:
  - **`console`** — foreground launch with **live output**. Ensures Docker/Neo4j, runs
    `serve-watch` in the current console (logging already writes to console + files), and
    stops cleanly on Ctrl+C (serve-watch's SIGINT handler forwards the stop and releases
    its pid files). Refuses if a server is already running, pointing at `stop` / `logs`.
  - **`logs`** — live-tail `logs/server.log` (`Get-Content -Wait`) for the background mode.
  - **`restart`** — now waits for the previous server to release the bind port
    (`Wait-ForServerStopped`) before starting, so a fast restart no longer races the
    watchdog into a port-in-use stop.
  - **`status`** — collapsed to one line:
    `menhir  server=PID …  watchdog=PID …  bind=host:port  neo4j=up  http=ready`.
- Background `start` and the logon scheduled task are unchanged.
- Docs: `.agent/workflows/operations_runbook.md` action list + console/foreground section.
- Verified: `status` (server=PID 40752 neo4j=up http=ready), `console` refuse-guard,
  `logs` path read, and PowerShell parse-check clean. Follow-up from TODO b33fc46f.

## 2026-07-12 — feat: mount explorer into main app (unified lifecycle), remove standalone :8787

- The graph explorer is now mounted into the combined FastAPI app at `/explorer` on the
  API port (default `127.0.0.1:8090`) instead of running as a separate `menhir-explorer`
  process on `:8787`. It inherits the backend's supervised lifecycle (watchdog,
  crash-restart, pid tracking, Docker/Neo4j readiness, logon auto-start, reboot recovery)
  and shares the runtime's single Neo4j pool (`ctx.built.neo4j`).
- Mounted candidate approve/reject now uses the **live** runtime Graphiti client
  (`ctx.built.graphiti_client`) so contradiction checks run for real, versus the
  standalone explorer's `UnavailableGraphitiClient` no-op.
- Mechanic: routes extracted into a module-level `APIRouter` (`create_explorer_router()`)
  with literal `/explorer/*` paths and included on the main app — no template/JS URL
  changes, and avoids the Starlette sub-app-lifespan gotcha. New
  `explorer/integration.py:mount_explorer(app)`; wired in `api/server.py` behind
  `MENHIR_EXPLORER_ENABLED` (default true), skipped under backendless startup scopes.
- **Auth hole closed:** `BearerAuthMiddleware` previously enforced only `/api/*` and
  `/mcp*`, leaving all other paths open. `/explorer` and `/explorer/candidates/*` are now
  gated exactly like `/api/*` (only `/explorer/static/*` exempt). Loopback `AuthMode.NONE`
  stays open; non-loopback binds require the API credential — no more unauthenticated
  remote graph reads or candidate writes.
- Removed the standalone explorer: dropped the `menhir-explorer` console script, the
  `run()` entrypoint, and `MENHIR_EXPLORER_HOST`/`MENHIR_EXPLORER_PORT` handling. Deleted
  dead `scripts/start-server.sh` (referenced the old `cth_mcp_memory.cli` module and a
  hardcoded venv path). `explorer/app.create_app()` retained for tests only.
- Tests: explorer app (4) + candidates (7) refactored for the router; added 6 auth-gating
  tests (`tests/test_api_auth.py`) covering token-required vs loopback-open and
  static-exempt. Full offline unit suite green (1532 passed, 4 skipped).
- Docs: `.agent/architecture.md` §6, `README.md`, `docs/security-posture.md`,
  `docs/runbooks/local-operator-hardening.md`, and `.agent/project.toml` updated to the
  mounted, auth-gated posture. Plan:
  `.agent/plans/menhir-explorer-mount-lifecycle-plan.md`.

## 2026-07-12 — build: remove langfuse dependency, upgrade graphiti-core to 0.29.2

- Removed `langfuse` from `pyproject.toml`/`uv.lock` (single-user deployment, not
  currently needed). No code change required: `observability.py` already guards the
  import behind `except ModuleNotFoundError` and gates all usage behind
  `langfuse_enabled()`, so removal is a clean no-op fallback to plain `AsyncOpenAI`.
  Also dropped 8 langfuse-only transitive deps (OpenTelemetry exporter chain,
  protobuf, wrapt).
- Upgraded `graphiti-core` from `0.28.2` to `0.29.2` per
  `.agent/reviews/menhir-graphiti-0.29-dependency-probe-2026-07-12.md`. Updated
  `_patch_graphiti_dedupe_resolutions()` to the 0.29 `NodeDuplicate` shape
  (`duplicate_candidate_id: int`, `-1` = no duplicate) — no compatibility shim kept
  for the pre-0.29 `duplicate_name: str` shape (single-user deployment, one supported
  version). Bumped the patch module's version guard to `0.29.x`.
- Full offline suite green on the correct `.venv` interpreter: 2852 passed, 32 skipped,
  0 failed. **Remaining gap:** no live canary yet (a real `add_episode` + search against
  live Neo4j/LLM extraction in a disposable namespace) — offline tests don't exercise
  that path.
- Process note: bare `python` on this machine resolves to a separate global
  interpreter with its own stale site-packages (still had `graphiti-core==0.28.2`
  after this same session's `uv.lock` bump). `pytest.ini`'s `pythonpath = src` masks
  this for the local package but not third-party deps — always invoke tests via
  `.venv/Scripts/python.exe -m pytest` or `uv run pytest`, never bare `python`.

## 2026-07-12 — security: OAuth settings snapshot and embedded-AS hardening

- Added typed OAuth, embedded-AS, trusted-proxy, CORS, startup-scope, and instance settings to `MemorySettings`, with fail-closed bounds and HTTPS validation.
- Routed the HTTP auth surface, backend runtime, OAuth stores/signing key/rate limiters, diagnostics, and health metadata through one startup snapshot.
- Added trusted-proxy peer allowlisting and hardened consent HTML against caching, framing, referrer leakage, and content sniffing.
- Gated the production JWKS endpoint on embedded-AS enablement and pinned Starlette above GHSA-86qp-5c8j-p5mr.
- Pinned `cth-mcp-framework` to its exact GitHub commit and regenerated `uv.lock`,
  restoring frozen installs with Starlette 1.3.1.
- Refreshed all compatible lockfile dependencies, clearing the third-party
  `pip-audit` findings without crossing the Graphiti, Langfuse, or Neo4j major-version bounds.
- Upgraded the Neo4j Python driver to 6.2 while retaining the supported Neo4j
  5.26 LTS database server.
- Added focused snapshot, TLS, proxy, and consent-header regression coverage and updated OAuth operator documentation.

## 2026-07-12 — fix: remaining test-suite red (TestDecayLLMWiring, real scheduler network call, flaky heartbeat timing)

Chased the full local suite to green (1520+ passed, 0 failed) after Phase 4. Three
separate, previously-flagged-as-follow-up issues, none related to SSOT-01..13:

1. **`TestDecayLLMWiring` (4 failures)**: not an LLM-wiring bug -- `compress_calls` was
   empty, meaning the LLM was never even called. Root cause: the test's
   `_setup_compressible_candidate` fixture set `stub_graphiti.search_scored_results`
   (4 similar-node tuples, intending `sharpness = 1/(1+4) = 0.2`), but production's
   `LifecycleService._count_similar_nodes` was rewritten under the F2 "lawful sharpness"
   effort to call `count_similar_by_cosine` instead -- a different stub field
   (`count_similar_by_cosine_results`) that the fixture never set, so it silently
   defaulted to 0 similar nodes -> sharpness 1.0 -> `should_compress()` always False.
   The fixture had gone stale relative to already-shipped production behavior. Fixed by
   also setting `count_similar_by_cosine_results = 4` in the fixture.
2. **`test_returns_llm_summary` hang** (previously tracked as a separate, un-root-caused
   issue): not a hang, but a real 30-second delay. `_build_llm_adapter()`'s
   `base_url="http://127.0.0.1:8081/v1"` is one of `should_use_scheduler`'s recognized
   local-scheduler defaults, so `OpenAIStyleChatBackend._resolve_base_url` called the
   REAL `acquire_llama_url_async` (not mocked by this test) -- a genuine `httpx` POST to
   a scheduler that isn't running in the test env, which only falls back to the
   configured URL after its full 30s timeout. Added an autouse fixture in
   `TestCompressContentUnit` that patches `acquire_llama_url_async` to return the
   fallback immediately by default; `test_uses_scheduler_acquired_base_url_for_chat_client`
   (which deliberately exercises real scheduler-acquisition behavior) still overrides it
   with its own nested patch. The whole 16-test class now runs in 0.66s, down from
   30s+ per affected test.
3. **`test_maintenance_scheduler_heartbeat_keeps_lease_alive_during_long_job` flake**:
   passed standalone but flaked once during a 541s, 1500+-test serial run. Real
   wall-clock sleep windows (0.1s heartbeat / 0.3s lease / 0.6s job / 0.4s check) left
   thin margins that system load during a long run could blow through, without any
   actual scheduler bug. Doubled every window (0.1s heartbeat unchanged, 0.6s lease,
   1.2s job, 0.8s check) to give heartbeat ticks (~6 renewals per lease window instead
   of ~3) comfortable slack while preserving the exact ordering/intent the test checks.

## 2026-07-12 — fix: SSOT-10 tool/feature/endpoint/concept registries disagreed with runtime

The runtime registers 43 tools (42 at SSOT-review time, +1 from the promote_memory tool
added earlier this session) and 9 resources via `menhir.mcp.tools.ALL_TOOLS`, but every
external surface describing that registry had drifted from it independently:
`explorer/feature_taxonomy.py` omitted 8 registered tools and listed 2 phantom ones
(`memory_gateway`, `recover_memory`) that were never actually registered;
`.agent/endpoints.md` had documentation sections for only 31 of 42 tools;
`.agent/concept-ids.yaml` had `mcp.tool.*` concept-id entries for only 15 of 42, plus was
missing `model.todo` entirely; `README.md`/`.agent/architecture.md` still claimed "23
tools".

Did not build a doc-generation pipeline off the registry (a much larger feature than this
pass scoped for). Instead fixed all four surfaces to match `ALL_TOOLS` as of now, and
added regression tests that fail the moment a tool is added/removed without updating
docs -- which is the finding's actual ask ("a completeness test that fails on drift").
New `tests/test_registry_completeness.py` (regex-based, not PyYAML-based, since PyYAML
isn't a declared dependency) checks endpoints.md and concept-ids.yaml coverage in both
directions (missing AND phantom entries); `tests/test_feature_taxonomy.py` gained the
same two-directional check for the Explorer taxonomy.

## 2026-07-12 — fix: SSOT-12 duplicated symbol-path construction

`infrastructure/project_scanner.py::_sym_path` and
`infrastructure/structure_queries.py::_symbol_path` were two independent copies of
the exact same fully-qualified-symbol-path logic (one computed at scan time, the
other at query/write time) that could silently diverge if edited separately.
Extracted `domain/utils.py::symbol_structure_path(file_path, name, parent)` --
takes plain strings, not a `SymbolEntry` instance, so it stays a domain-layer
utility with no dependency on the infrastructure-layer scanner dataclass. Both
call sites now delegate to it. Added `TestSymbolStructurePath` in
`tests/test_utils.py` proving all three (the two wrappers plus the shared
function) agree on the same symbol corpus.

## 2026-07-12 — fix: SSOT-11 MEMORY_RETURN_FIELDS missing processing-detail fields

`MEMORY_RETURN_FIELDS` (`infrastructure/cypher.py`, used by fetch_recent/flagged/
by_scope/by_type/by_uuid) omitted `processing_substage`,
`processing_substage_started_at`, and the four active-LLM fields
(`processing_llm_active_task/kind/model/endpoint`) that `EPISODE_PROCESSING_FIELDS`
already carried -- the two "full projection" constants had silently drifted.
Extracted `_PROCESSING_DETAIL_FIELDS` as the shared base of processing fields common
to both; each constant now spreads it in and adds only its own genuinely different
bits. Deliberately kept `processing_attempts` as two separate expressions per view
(raw in `MEMORY_RETURN_FIELDS`, coalesced-to-int in `EPISODE_PROCESSING_FIELDS`) --
that's a pre-existing, intentional difference, not drift.
`MEMORY_RETURN_FIELDS` grew from 28 to 41 fields; all existing
`tests/test_cypher.py::TestFieldConstants` checks (min-count, alias-present,
no-duplicate-alias) still pass unmodified. Added
`test_memory_return_fields_is_processing_field_superset` to lock in the
field-superset invariant going forward.

## 2026-07-12 — fix: SSOT-13 version drift (0.2.0 vs Explorer's 0.1.0, stale title)

Three separately-hardcoded version literals had drifted: `pyproject.toml` (`0.2.0`,
canonical), `menhir/__init__.py::__version__` (`0.2.0`, a duplicate literal), and
`explorer/app.py`'s `FastAPI(version=...)` (`0.1.0`, already stale). Fixed by making
`__version__` resolve once via `importlib.metadata.version("menhir")` (falls back to
`"0.0.0-dev"` when not installed) and having `api/server.py` and `explorer/app.py` both
read `menhir.__version__` instead of hardcoding their own. Also fixed Explorer's stale
FastAPI `title="cth.mcp.memory explorer"` -> `"menhir explorer"`. Left `api/server.py`'s
`title="yawn-memory"` alone -- that's the registered MCP server name
(`tests/test_mcp_remote.py`/`test_mcp_server.py` assert on it), a separate naming
decision, not the version-drift bug this finding targets.
`tests/test_scaffold.py::test_version` updated from a literal `"0.2.0"` assertion to
"resolves to a non-empty string" -- deliberately, since pinning a duplicate literal in
the test would recreate the exact drift this fix removes.

## 2026-07-12 — fix: SSOT-07 boolean parser drift (MENHIR_CLIENT_TOKENS_ENABLED=on) + dead env-var doc sweep

Fixed the concrete, reproduced bug from the SSOT review: `MENHIR_CLIENT_TOKENS_ENABLED=on`
resolved to `True` via `api.client_token_store.client_tokens_enabled()` (its own ad hoc
parser included `"on"`) but `False` via `MemorySettings.client_tokens_enabled` (which
didn't) -- the two could silently disagree on the exact same env value. Extracted
`parse_bool_env()` in `config/settings.py` as the one canonical boolean parser
(`"true"`/`"1"`/`"yes"` -- `"on"` deliberately excluded, since no flag in this codebase
documents it as accepted) and routed all 20 `MENHIR_*_ENABLED`-style flags in
`MemorySettings.from_env()` through it. `client_token_store.client_tokens_enabled()` no
longer re-reads the env var itself; it delegates to
`MemorySettings.from_env().client_tokens_enabled`, so the two paths can never diverge
again. Regression tests added in `tests/test_settings.py` and
`tests/test_client_token_store.py`, both parametrized over `"on"` to lock in the fix.

Also swept docs/`.env.example` for dead env-var references (grepped `src/` to confirm
what's actually read vs merely documented): `.env.example`'s commented-out
`YAWN_MEMORY_MCP_TELEMETRY_DB`/`YAWN_MEMORY_MCP_TIMEOUT`/`YAWN_MEMORY_EXPLORER_HOST`/
`YAWN_MEMORY_EXPLORER_PORT` were dead (real names are `MENHIR_MCP_TELEMETRY_DB`/
`MENHIR_MCP_TIMEOUT`/`MENHIR_EXPLORER_HOST`/`MENHIR_EXPLORER_PORT`) -- fixed.
`.agent/architecture.md` and `.agent/data_models.md` advertised
`GRAPHITI_LLM_BASE_URL`/`GRAPHITI_LLM_API_KEY`/`GRAPHITI_LLM_CHAT_MODEL`/
`GRAPHITI_EMBED_BASE_URL`/`GRAPHITI_EMBED_API_KEY`/`GRAPHITI_EMBED_MODEL`/`OPENAI_BASE_URL`,
none of which are read anywhere in `src/` -- corrected (the `GRAPHITI_*` vars only ever
select a provider name; the endpoint/key/model come from that provider's own settings,
e.g. `OPENAI_*` or `LOCAL_LLM_*`). `README.md`/`.agent/data_models.md` now lead with the
canonical `LOCAL_LLM_*` names, noting `LLAMA_*` as the still-accepted legacy alias.

**Known follow-up, not fixed here**: routing OAuth/client-token modules
(`api/oauth.py`, `oauth_authorize.py`, `oauth_token.py`, `oauth_as_register.py`,
`oauth_rate_limit.py`, `auth_code_store.py`, `server.py`, `routes.py`) through a
`MemorySettings` snapshot instead of each re-reading `os.getenv` directly. On
inspection this needs ~10 new `MemorySettings` fields and touches 6 auth-critical
files -- bigger than "mechanical," scoped out of this pass rather than rushed.
`oauth_rate_limit.py`'s `MENHIR_TRUSTED_PROXY` parser has the same `"on"`-inclusive ad
hoc boolean-parsing pattern as the bug just fixed here; worth revisiting together with
that refactor. See `.agent/plans/ssot-remediation-2026-07-11.md` SSOT-07 for the full
inventory of dead vs. live env vars checked.

## 2026-07-12 — fix: episode processing status never resolved to READY/FAILED (str-enum bug)

Root-caused the long-standing suite-hang first reported in the 2026-07-11 SSOT review
("`pytest -m unit` made no progress after 68 tests before timeouts at 120 and 300
seconds") via CI diagnostic isolation and local reproduction, not guesswork: 12 test
files completed cleanly, then `tests/test_milestone_two_contract.py::
test_ingest_stamps_session_id_on_adapter` stalled for the entire timeout window.
Print-instrumented the full background-enrichment path
(`queue_episode_for_enrichment` -> `_enrichment_worker_loop` ->
`_process_pending_episode` -> `run_graphiti_extraction` -> `stamp_and_finalize`) and
confirmed the pipeline completes successfully and writes `processing_state = READY`
within ~100ms -- the row is correct, but the polling code never sees it.

Root cause: `ProcessingState` is a `(str, Enum)` mixin. `str(ProcessingState.READY)`
returns the qualified repr `"ProcessingState.READY"`, not the value `"READY"` --
so `IngestService.wait_for_episode_processing`'s `state = str(row.get(...) or "")`
followed by `if state in {ProcessingState.READY, ProcessingState.FAILED}` could
never match, regardless of the row's actual state. The method always polled for the
full `timeout_s` (60s in production; the review's 120s/300s stalls match this plus
retry/backoff windows). The identical bug existed a second time in `ingest_episode`'s
own status-mapping (`processing_state == ProcessingState.READY`), which would always
report `IngestStatus.QUEUED` even when enrichment had genuinely completed -- masked
until now because the one other test exercising this path (
`test_ingest_episode_returns_ingest_result`) monkeypatches
`wait_for_episode_processing` entirely and never hit the real comparison.

Both call sites fixed to compare the raw value directly (`row.get("processing_state")
in (ProcessingState.READY, ProcessingState.FAILED)`), which matches whether the row
holds the enum member or a plain string (e.g. a real Neo4j driver round-trip) --
str-mixin enums compare equal to their string value in both forms, so no `str()`
wrapping is needed or correct.

New regression tests: `TestZeroNegativeTimeout::test_ready_state_returns_immediately_
not_after_full_timeout` and `::test_plain_string_ready_state_also_matches` (both forms,
`test_edge_cases.py`), and `test_ingest_episode_maps_ready_to_ingested_without_
monkeypatch` (full pipeline, no monkeypatch, `test_milestone_two_contract.py`). Verified
each fails without the fix (confirmed by reverting locally: 5.05s instead of <1.0s)
and passes with it.

**Known follow-up, not fixed here**: the same `str(enum) ==`/`in {...}` comparison
pattern against `ProcessingState` recurs in `infrastructure/episode_lifecycle.py`,
`mcp/formatters.py`, `mcp/resources.py`, `mcp/tools/ops/force_release_lease.py`,
`mcp/tools/ops/get_enrichment_status.py`, and `services/recall_service.py`. These are
very likely harmless in real production (Neo4j returns plain strings, and
`str(plain_string)` is a no-op, so the comparison still works there) -- the bug only
manifests when a stub/test fixture stores the raw enum instance directly, as
`tests/conftest.py`'s `StubMemoryGraphAdapter` does. Not audited or fixed in this pass;
flagged for a dedicated sweep.

Also confirmed (2026-07-12) that the `tests/test_llm_compression.py::
TestCompressContentUnit::test_returns_llm_summary` hang (and likely the related
`TestDecayLLMWiring` failures in the same file) is a separate, unrelated bug in the LLM
chat-completion retry/backoff path -- not fixed in this pass, tracked as a follow-up.

## 2026-07-11 — SSOT remediation Phase 3 (part 4, stage 3/3): PROMOTED contradiction-queue routing

`LifecycleService._check_contradictions_batch` now looks up scope for both nodes in a
conflict-range pair before flagging; if either is `PROMOTED`, `set_conflict` is called
with `initial_status="unresolved"` instead of `"pending_llm_review"`. A claim
conflicting with verified ground truth must not be auto-adjudicated by
`confirm_pending_conflicts`' symmetric LLM voter (which scans `pending_llm_review` and
has no notion that one side is ground truth) -- it now surfaces for manual operator
review instead, reusing the existing `unresolved` `ConflictStatus` value rather than
inventing a new one. New test:
`test_conflict_against_promoted_node_routes_to_unresolved_not_pending`; also locked
down that ordinary conflicts still default to `pending_llm_review`.

**This completes SSOT-08** (all 3 stages: writer + confidence pin, merge-absorption
immunity, contradiction-queue routing). Phases 1-3 of the SSOT remediation plan are
now fully done (SSOT-01, 02, 03, 04, 05, 06, 08, 09); only Phase 4 (SSOT-07, 10, 11,
12, 13 -- mechanical doc/registry/config cleanup) remains.

## 2026-07-11 — SSOT remediation Phase 3 (part 4, stage 2/3): PROMOTED merge-absorption immunity

`CorrelationService._handle_merge_proposal` now fetches entity metadata unconditionally
(moved earlier, alongside the other three deterministic vetoes) and refuses to merge a
pair if either the survivor or absorbed node has `scope='PROMOTED'` -- checked before,
and independent of, the other vetoes and LLM judge availability, since this is a hard
identity-immutability guarantee, not a confidence signal. Verified at both the
correlation-service level and end-to-end through `LifecycleService`.

Also fixed a pre-existing latent bug this change surfaced: the shared
`StubMemoryGraphAdapter.fetch_entity_merge_metadata` test fixture had the wrong
signature (single uuid in, `dict|None` out) against the real `list[str] -> list[dict]`
interface -- previously unreachable because the old code only called it when an LLM
judge was available; now called unconditionally like the other vetoes.

## 2026-07-11 — SSOT remediation Phase 3 (part 4, stage 1/3): PROMOTED tier writer

From `.agent/reviews/menhir-ssot-review-2026-07-11.md` (SSOT-08, Med). First stage of
building a real PROMOTED tier (operator-curated, verified ground truth, distinct from
user_flagged's "important to the user"): added `MemoryQueryRepository.promote_memory`
(guarded on `scope IN ['PERSISTENT', 'PROMOTED']`, idempotent, pins `source_confidence`
to `1.0` at promotion time), threaded through `MemoryGraphAdapter`/protocol/
`RuntimeProvider`/`BackendClient`, registered as an operator-tier backend op, and added
a new `promote_memory` MCP tool. Remaining stages (merge-absorption immunity,
contradiction-queue routing for claims against a PROMOTED node) are separate commits
per the plan's guidance to treat this as its own sub-sequence rather than one commit.

## 2026-07-11 — SSOT remediation Phase 3 (part 3): route Explorer through CandidateService

From `.agent/reviews/menhir-ssot-review-2026-07-11.md` (SSOT-05, Med). Explorer's
candidate approve/reject routes ran their own local Cypher, a second, weaker-consistency
mutation path alongside the canonical `CandidateService.approve/reject` used by the
backend/MCP path. Removed Explorer's `_approve_candidate`/`_reject_candidate` writers;
the routes now call `CandidateService` directly. Explorer has no live Graphiti/LLM
client by design, so it's wired with `UnavailableGraphitiClient` for its
`LifecycleService` -- the contradiction check this enables is a safe, already-designed
best-effort no-op (both services already tolerate that failure), so approval still gets
the same promotion/consistency guarantees as the backend path even without live search.
Updated `test_explorer_candidates.py`'s stub repo to match the canonical
`CandidateRepository` query shapes.

## 2026-07-11 — SSOT remediation Phase 3 (part 2): consolidate identity-merge veto logic

From `.agent/reviews/menhir-ssot-review-2026-07-11.md` (SSOT-03, Med). Three separate
copies of the routing/veto/judge-gated-merge workflow existed: `CorrelationService`'s
canonical `_handle_merge_proposal`, its own unused `check_correlation_batch`, and
`LifecycleService._check_contradictions_batch`'s inline reimplementation -- which
checked only `co_mention_veto`/`anchor_project_veto` and silently omitted
`ineligible_node_veto`, so a structural/path-shaped node pair could merge through the
lifecycle path even though the canonical path would refuse it.

Extracted `CorrelationService.classify_pair()` as the sole owner of routing, all three
vetoes, and merge execution; `check_correlation`, `check_correlation_batch`, and
`LifecycleService._check_contradictions_batch` all now delegate to it, retaining only
their own bookkeeping (result counters / namespace-scoped search + conflict-queue
writes + telemetry suppression, respectively). Also removed the private
`graph_adapter._correlation` reach-around a 2026-07-04 review had flagged as fragile:
added public `MemoryGraphAdapter.check_ineligible_node_veto`/`check_co_mention_veto`/
`check_anchor_project_veto` delegates alongside the pre-existing merge-related ones.

New regression test: `test_ineligible_node_veto_blocks_merge_via_lifecycle_path`
proves the previously-missing veto now blocks a merge through the lifecycle
consolidation path even when a judge is available and would confirm.

## 2026-07-11 — SSOT remediation Phase 3 (part 1): conflict-scan default + user confidence drift

From `.agent/reviews/menhir-ssot-review-2026-07-11.md` (SSOT-06, SSOT-09 -- both Med,
both with design decisions worked through with the user beforehand):

- **SSOT-06**: `scan_for_conflicts` had four different defaults across the stack (100 on
  `RuntimeProvider`/`BackendClient`, 150 on the protocol/`LifecycleService`/module wrapper,
  500 on the registered MCP endpoint, plus a docstring that claimed 500 while the code said
  150). Canonicalized on `150` everywhere. `test_backend_signature_parity.py`'s parity
  check no longer needs its `scan_for_conflicts` exception; added an explicit
  default-parity test across all four call sites.
- **SSOT-09**: `source_confidence_for("user")` returned `0.9`, drifted from the documented
  canonical intent in `domain/truth/kinds.py` (`SOURCE_CONFIDENCE_USER = 1.0`) and the
  parked Trusted Memory Admission research doc, which builds its whole trust ladder on
  `user` being the apex tier. Fixed to source the constant from `kinds.py`; updated the
  pinned test in `test_utils.py` (deliberate behavior correction, not a mechanical
  refactor). Left `domain/artifacts.py`'s separate `_CONFIDENCE_TRUSTED = 0.9` (a
  different, not-clearly-wrong "TRUSTED review-state" concept) out of scope.

## 2026-07-11 — SSOT remediation Phase 2: namespace-scope recall adjacency

From `.agent/reviews/menhir-ssot-review-2026-07-11.md` (SSOT-04, Med). Candidate fetch
was already namespace-scoped, but the adjacency-scoring pass that follows it
(`RecallService._compute_adjacency` -> `MemoryGraphAdapter.fetch_adjacency_pairs`)
dropped `namespace` entirely, even though `MemoryQueryRepository.fetch_adjacency_pairs`
already had a namespace-constrained Cypher branch nothing called into. Cross-namespace
structure could influence a recall's ranking even with a namespace explicitly scoped.
Threaded `namespace` through `_compute_adjacency` and the adapter delegate. New tests in
`test_namespace_isolation.py` and `test_memory_graph_adapter_methods.py`.

## 2026-07-11 — SSOT remediation Phase 1: fix stdio recall and TEMPORAL namespace isolation

From `.agent/reviews/menhir-ssot-review-2026-07-11.md` (SSOT-01, SSOT-02 — both High):

- **SSOT-01**: `BackendClient.recall` was missing `include_invalidated`, while
  `MemoryBackend`/`RuntimeProvider` and `RecallMemoriesTool` (which always supplies it)
  all had it — any stdio/backend-client recall raised `TypeError` before making an HTTP
  request. Added the parameter and its request-payload key. New
  `tests/test_backend_signature_parity.py` asserts full keyword-signature parity across
  `MemoryBackend`/`RuntimeProvider`/`BackendClient` for every shared method, so future
  parameter drift on one surface fails CI (documents two pre-existing, tracked exceptions:
  `BackendClient.aclose` — HTTP-only lifecycle plumbing — and `scan_for_conflicts`'
  100/150/500 default split, tracked separately as SSOT-06/Phase 3).
- **SSOT-02**: `add_memory`'s TEMPORAL branch called `backend.create_temporal` without
  `namespace`, and `TemporalRepository.create_temporal` hardcoded `group_id: ''` — a
  private TEMPORAL memory silently landed in the shared/default group. Threaded
  `namespace` through the full chain (`add_memory.py` → `MemoryBackend` protocol →
  `RuntimeProvider`/`BackendClient` → `MemoryGraphAdapter` → `TemporalRepository`), which
  now stamps both `group_id` (the load-bearing graphiti partition) and `namespace` (the
  defense-in-depth property recall's candidate filter reads), matching the pattern
  documented in `domain/namespace.py`. New `tests/test_temporal_repository.py`.

Remaining findings (SSOT-03 through SSOT-13) and their resolved design decisions
(SSOT-06 default=150, SSOT-08 real PROMOTED tier spec, SSOT-09 confidence=1.0 for
source=user) are tracked in `.agent/plans/ssot-remediation-2026-07-11.md`.

## 2026-07-11 — Tolerate degenerate node-dedupe resolutions

Residual ingest failure (recurring, incl. 2026-07-11): `N validation errors for
NodeResolutions / entity_resolutions.N.id / Field required` when the dedupe LLM returned a
degenerate resolution like `{'': ''}`. `NodeResolutions(**llm_response)` fails in Pydantic at
construction — *before* Graphiti's own downstream logic (which already skips
out-of-range/missing ids) runs — so the whole episode fails. Added
`_patch_graphiti_dedupe_resolutions`: a before-validator that drops entries lacking a
usable integer `id` (an id-less resolution is meaningless) and defaults a missing
`name`/`duplicate_name` to '', so valid resolutions still apply. Wired into `GraphitiClient`;
`test_graphiti_dedupe_resolutions_patch.py` (4 cases).

Also confirmed the 17 `PatchedExtractedEntities.entity_type_id` failures (Jul 2–6) are stale —
the existing entity-extraction patch's else-branch already defaults `entity_type_id=0` for a
name-only `{'name': ...}` dict, so they recover on retry with no code change.

## 2026-07-11 — Coerce None EntityEdge fields so degenerate edges don't fail episodes

Recurring ingest failure (still hitting 2026-07-11): `N validation errors for EntityEdge`
with `uuid`, `group_id`, `name`, `fact` all `None` — the extraction LLM occasionally returns
a fully-degenerate edge during dedupe/resolve, and Graphiti builds `EntityEdge(uuid=None,
group_id=None, name=None, fact=None, ...)`. Each is a required `str`, so Pydantic rejects it
and the *entire episode's* enrichment fails (22 episodes stuck, incl. 3 today). The existing
`_patch_graphiti_none_replace` only guards the embedding step (too late — the object fails at
construction), and there was no EntityEdge equivalent of the `EntityNode.summary` coercion.

Added `_patch_graphiti_edge_none_fields` (symmetric to `_patch_graphiti_node_summary_none`):
wraps `EntityEdge.__init__` to drop an explicit `uuid=None` (so the uuid4 default_factory
runs — never an empty identity) and coerce the other required-str fields to '' so the
episode's real nodes/edges still persist. Wired into `GraphitiClient` patch application; new
`test_graphiti_edge_none_patch.py` (4 cases). The separate `EntityNode.summary` failures (41)
are all pre-patch (last 2026-06-23) — that patch already works; those are stale records.

## 2026-07-11 — Refuse to start on embedding-dimension mismatch (big warning)

Future-proofing after recovering 51 ingests lost to an April 2 embedding-repair window
(query vectors and stored vectors briefly had mismatched dimensions -> neo4j
`vector.similarity` "supplied vectors do not have the same number of dimensions"). The
existing preflight check only fired for the 6 hard-coded known models and only emitted a
soft `failures` line, so a model swap to any unlisted embedder — or a mixed-dimension graph
— produced silent, ongoing memory loss.

Added a single-source-of-truth `evaluate_embedding_compatibility(neo4j, settings)` in
`embedding_dimensions.py` that classifies the graph vs the configured embedder and blocks
**only when certain**: (a) the graph holds more than one embedding dimension (model-agnostic
`mixed` signal — an embedder was changed mid-life), or (b) the model's dimension is known and
stored vectors of a different dimension exist. An unknown model over a uniform graph is
unverifiable and deliberately does NOT block, so a legitimate unlisted embedder can never lock
the operator out. `embedding_dimension_health` now accepts an optional `expected_dim` and
reports a `mixed` flag.

`serve` runs this as a startup preflight and, on a blocking mismatch, prints an unmissable
banner (stderr + error log) and exits with a new terminal code `EXIT_EMBEDDING_MISMATCH=4`.
`serve-watch` treats code 4 like the port-in-use code: it stops instead of crash-looping,
since a restart cannot fix a dimension mismatch. Verified the live graph (uniform 1536) is
NOT falsely blocked. 21 embedding tests + 66 startup/diagnostics tests green.

## 2026-07-11 — Decay sweep skips no-content nodes (fixes fake `llm_failed`)

The D2 decay sweep was recording ~127 `llm_failed` compress failures per day that were not
LLM failures at all. Root cause: it selected name-only entity nodes as compression
candidates, built `raw = content or summary or ""` = `""`, and called `compress_content("")`
— which hits the empty-prompt guard in `_complete_single` and returns `None` **without ever
calling the LLM**. That `None` was mislabeled `failure_reason="llm_failed"` and deferred to
`pending_actions`, so the same nodes were re-selected and re-stamped every sweep (a
persistent, misleading fake-LLM-outage signal that would also mask a real `llm_failed`).

No-content is normal for entities: 52% of the entity layer (26,039 / 49,783 nodes) carry no
`content`, because entity nodes are connective concepts and the memory content lives on the
episodes that reference them. Compression only sheds body text, so it is a no-op on these.
Fix: in `_run_decay`, compute `raw` first and skip nodes with an empty body before any LLM
interaction — no LLM call, no pending action. 67 decay/lifecycle tests green.

## 2026-07-11 — Consolidate flag propagation into one helper (SSOT)

Root-cause follow-up to the flagged-episode enrichment fixes: the "propagate an
episode's `user_flagged` onto its extracted entity nodes, skipping structural nodes"
logic was copy-pasted at three sites (main extraction path, `try_reconcile_existing`,
and the scheduler retry-reconcile path). That duplication is why the guard was applied
once and missed twice. Extracted a single `propagate_user_flag(graph_adapter, node_uuids,
*, episode_uuid)` in `enrichment_steps.py` that owns the skip-structural behavior; all three
sites now call it. The single-node MCP `flag_memory` tool deliberately still *raises* for
structural nodes (user-initiated flags deserve feedback) — only the auto-propagation path
skips. Added `TestPropagateUserFlag`. Import direction verified acyclic; both call sites
resolve to the same helper object; 54 enrichment-adjacent tests green.

## 2026-07-11 — Flagged episodes no longer fail enrichment on structural nodes

`enrichment_steps.py` propagated a flagged episode's `user_flagged` to every extracted
entity node via `graph_adapter.flag_memory(...)`. Entity resolution can dedupe an extracted
node onto an existing structural graph node (project/directory/file/document), which
`flag_memory` refuses by design with `ValueError: Cannot flag structural graph node`. The
call was unguarded, so the raise aborted the whole episode's enrichment before
`mark_episode_ready` — the worker counted a failure and retried, hitting the same node each
time until the episode landed in FAILED. Net effect: **flagged, code-referencing memories
were never written to the graph** (48 episodes found in this state, roles directory/file/
document/project). The same unguarded pattern existed at **three** call sites — the main
post-extraction path (`enrichment_steps.py`), the reconcile path (`try_reconcile_existing`,
hit first for episodes with prior extraction artifacts), and the scheduler retry-reconcile
path (`scheduler_tasks.py`). Fix wraps the per-node flag in `try/except ValueError` at all
three and skips structural nodes (debug-logged), so enrichment/reconcile completes and the
semantic nodes still get flagged. 129 enrichment-adjacent tests green.

## 2026-07-11 — Accept demote/delete lifecycle telemetry (silent-drop fix)

The F5 demote-with-TTL lifecycle records actions with `action="demote"/"delete"` and
`trigger="consolidation_demote"/"demote_ttl_expiry"`, but the telemetry validator whitelist
(`_VALID_ACTIONS`/`_VALID_TRIGGERS` in `telemetry/store.py`) never learned them. Every such
write raised `ValueError`, swallowed by the `recorders` wrapper as a WARNING — silently
dropping the lifecycle telemetry, including the deletion audit records the code calls "the
only surviving evidence". Observed live as 87 `Invalid lifecycle action 'demote'` warnings
(one per demoted SESSION node); the `delete` side was latent (grace TTL not yet elapsed).

`lifecycle_actions` has no CHECK constraint, so the Python whitelist was the only gate —
widening it is sufficient, no migration. Added the two actions + two triggers, refreshed the
docstring, and added a regression test covering both F5 action/trigger pairs. Additive change;
the negative validator tests (`invalid_action`/`invalid_trigger`) still pass. 52 tests green.

## 2026-07-11 — serve-watch singleton guard + port-in-use handling (crash-loop fix)

Fix for redundant `serve-watch` watchdogs crash-looping forever. When two backend
bootstraps launched near-simultaneously, `serve_watch` had no singleton guard: both
wrote `.watchdog.pid`, both spawned `serve` children, and the loser hit
`[Errno 10048]` binding the already-owned port, which the watchdog misread as a crash
and retried with backoff indefinitely (one error stanza every 60s). The two watchdogs
also clobbered each other's `.server.pid`/`.watchdog.pid`, at one point deleting the
pid file for the *healthy* server.

Three-part fix in `cli/__init__.py`:
- **`serve` port preflight** — an exclusive bind probe before uvicorn; on port-in-use
  it exits with new sentinel `EXIT_PORT_IN_USE=3` instead of a generic crash.
- **`serve-watch` singleton guard** — if `.watchdog.pid` names a live process, exit 0
  immediately. On a same-instant race that slips past, the losing child returns
  `EXIT_PORT_IN_USE` and the watchdog stops (no retry) rather than looping.
- **Ownership-tracked pid files** — `_release_pid_file` only unlinks a pid file it still
  owns, and `.server.pid` is claimed only after a `STARTUP_GRACE_S` window, so a
  redundant watchdog can never delete the real server's pid file. New helpers
  `_pid_alive` (ctypes on Windows — `os.kill(pid,0)` is unsafe there), `_read_pid_file`,
  `_release_pid_file`. Verified: compile, helper unit checks, live 8090 probe, and an
  end-to-end redundant `serve-watch` exiting 0 with pid files intact.

## 2026-07-11 — Feature usage/effectiveness dashboard + self-reported recall usefulness

Two additions to make per-feature usage measurable in the explorer.

**Feature Usage dashboard** (`/explorer/features`): groups every MCP tool/resource by parent
(retrieve, write, structure, conflicts, processing, todos, stats, lifecycle, gateway, resources),
sortable by function or parent, over 7d/30d/all windows. Usage = calls + calls/day; effectiveness =
success rate (real, since `success=0` always carries an error), p50/p95 latency, avg result size, and
self-reported usefulness (below). Deliberately no "hit rate": `result_size` measures the whole
serialized envelope, not item count, so it cannot distinguish an empty result from a populated one
across tools. New `McpTelemetryStore.fetch_feature_stats`, `explorer/feature_taxonomy.py`,
`_feature_report`, `features.html`, JSON at `/explorer/api/features`.

**Self-reported recall usefulness** (R8 agent_inference grade — dashboard signal only, never feeds
memory heat/promotion/ranking): recall tools (`recall_memories`, `recall_context_memories`,
`read_flagged_memories`, `build_context`) now stamp a short `recall_id` into their response; the new
`rate_recall(score, recall_id?, reason?)` tool records a usefulness score (`useful`/`partial`/`noise`
/`unused` → 1.0/0.5/0.0/null). Token defaults to the last unrated recall in the session. Stored in a
new append-side `recall_receipts` table; surfaced as Usefulness + Rated% columns. New `mcp/feedback.py`,
`mcp/tools/ops/rate_recall.py`, store methods `record_recall_receipt`/`record_recall_feedback`/
`fetch_usefulness_stats`. 20 new tests; existing recall/MCP/telemetry suites green.

## 2026-07-10 — Decay sweep wired to the scheduler (D2)

`LifecycleService.apply_decay` previously had zero invokers — the decay half of the lifecycle
(edge sync → sharpness recompute → compress) never ran. Added a daily `decay_lifecycle` maintenance-
scheduler job (mirrors the F5 `consolidate_lifecycle` job): gated on `lifecycle_service` present +
`lifecycle_decay_enabled` (default on, 86400s). With F2 (lawful sharpness) and F3 (archive-first
rehydrate) landed, the sweep now recomputes sharpness on the true-cosine scale and compresses
idle low-uniqueness nodes losslessly. H3 (`should_delete`) stays `False`, so the decay sweep performs
NO deletions — compression only. Activates F6's Option-B compression in practice. Protocol gains
`apply_decay`; 2 new scheduler tests.

## 2026-07-10 — Merge-eligibility guardrail: structural & path-shaped nodes never merge

Ingested data structures must never be correlation-merge candidates. A forward guardrail that would
have prevented ~51% of the historical auto-merge damage (structural 28% + untagged path-shaped 23%,
including the single largest absorber `src/components/article` which swallowed 73 entities). Plan:
`.agent/plans/merge-eligibility-guardrail-plan.md`.

- **`CorrelationRepository.check_ineligible_node_veto`** — one DB-side Cypher check: a node is
  merge-ineligible if `structure_role IS NOT NULL` OR its name is path-shaped (contains `/`/`\` or a
  file-extension token). Regex validated against the live Neo4j engine.
- **`CorrelationService.handle_merge_proposal`** — new `ineligible_node` deterministic veto, run
  FIRST (before `co_mention`/`anchor_project`), routing to conflict (fail-safe; a false veto only
  costs recall dilution, never irreversible loss). Single choke point for both merge entry paths.
- Forward-only: historical `merged_from` receipts untouched. LLM judge, other vetoes, merge threshold
  unchanged. 48 correlation tests green (4 new).

## 2026-07-10 — Lifecycle F5: demote-with-TTL (re-enables H2 session deletion)

Replaces the H2 hotfix's `else: pass` (unpromoted SESSION nodes lingered forever) with a principled
middle rung. With F2 (lawful sharpness) landed, the "low uniqueness" that routes a node to demote is
now trustworthy, so H2 deletion is re-enabled — but only as a recoverable, grace-windowed path. H3
(decay GONE) stays disarmed. Plan: `.agent/plans/lifecycle-f5-demote-with-ttl-implementation.md`.

- **Demote:** an unflagged, low-edge, low-sharpness SESSION node gets a set-once `ttl_expires`
  (`DEMOTE_TTL_DAYS = 14`, DB-side `datetime() + duration`). Mere re-access does not extend it.
- **Rescue:** any promotion signal (flag / persistent edges / high sharpness) promotes the node and
  clears its TTL (`promote_to_persistent` sets `ttl_expires = null`).
- **Expire:** after the grace window with no corroboration, the node is deleted — the expiry sweep
  runs AFTER promotion (promotion wins), and each deletion writes a
  `record_lifecycle_action(trigger="demote_ttl_expiry")` audit record before the DETACH DELETE.
- **Cadence (P7):** a daily `consolidate_lifecycle` job on the maintenance scheduler runs
  `recover_orphans` — consolidation was previously restart-only. Resolves D3. Gated on
  `lifecycle_service` present + `lifecycle_consolidation_enabled` (default on, daily).
- New queries `set_demote_ttl` / `fetch_ttl_expired_session_uuids`; `ConsolidationResult.demoted`.
  171 tests green (incl. new `test_lifecycle_consolidation_job`).

## 2026-07-10 — Lifecycle F2: lawful sharpness recomputation (true cosine)

Sharpness (a memory's uniqueness score, the sole gate on the lifecycle arms) is now derived
from a genuine cosine similarity instead of graphiti's RRF rank-fusion score. Fixes the
cross-scale mis-application that corrupted the 0.2-0.5 sharpness band (the promote >=0.5 and
compress <0.3 gates), the H2 session-deletion damage. Plan:
`.agent/plans/lifecycle-f2-lawful-sharpness-implementation.md`.

- **`graphiti_client.count_similar_by_cosine`** — cosine-only node search with `sim_min_score`
  as a real cosine floor (applied in the vector search, strict `>`, before reranking). Counts
  DISTINCT neighbors above the floor, excludes self + Episodic, dedups by uuid; any exception
  (incl. vector-dimension mismatch) returns `-1` with no BM25 fallback.
- **`lifecycle_service`** — `_count_similar_nodes` now delegates to `count_similar_by_cosine`
  with `SHARPNESS_COSINE_FLOOR` (**PROVISIONAL 0.75**, pending the P3 calibration run against
  the live graph). `compute_sharpness` and `memory_types` gate thresholds unchanged.
- **`UnavailableGraphitiClient`** — sentinel parity for degraded startup (raises -> caller's
  `-1` advisory contract).
- **`scripts/probe/probe_sharpness_cosine_floor.py`** — read-only calibration probe scaffold
  (algorithm + selection criteria documented; body to be completed at the live calibration).
- Tests: SearchConfig-contract capture (cosine-only/no-BM25, `sim_min_score`, `limit+1`,
  `group_ids`), counting semantics, the deduped duplicate-uuid case, and a 0.2-0.5 corrupted-band
  regression flip. Full suite 168 green.
- **Not re-enabled:** H2/H3 delete arms stay disarmed (P5, sign-off-gated; H2 also needs F5).

## 2026-07-10 — Live Auth0 (SaaS-IdP) OAuth verification

Proves Menhir's OAuth resource-server mode against a real external IdP, the SaaS-IdP
counterpart to the existing local-mock-IdP coverage. Closes the resource-server half of the
"live SaaS-IdP interop" follow-up in `.agent/reviews/menhir-oauth-security-consolidated.md`.

- **`scripts/smoke/auth0_live_smoke.py`** — mints a genuine Auth0 client-credentials RS256
  token and drives a throwaway Menhir pointed at Auth0's real issuer/JWKS/audience: valid
  token passes auth (real JWKS fetch, issuer + audience check, `menhir:*` -> tier mapping),
  no token -> 401 + Bearer challenge, bogus token vs the reachable JWKS -> 401 (token error,
  not a 503 outage). 4/4. Self-skips (exit 0) when `AUTH0_*` env is unset, so it is safe in
  `run_all`.
- **`scripts/dev/test_server.py`** — the `oauth` launcher shape is parametrized for an
  external IdP via `launch(oauth={issuer, jwks_uri, audience, authorization_servers})`; the
  local dead-JWKS default path is unchanged (`auth_shapes_smoke.py` still 16/16).
- **`scripts/dev/auth0_{token_probe,provision,diagnose}.py`** — env-driven helpers (no secret
  on disk) to inspect a token, provision a clean API + client grant via the Management API,
  and introspect a tenant.
- **`scripts/smoke/run_all.py`** — includes the self-skipping `auth0_live` smoke.
- **`docs/runbooks/auth0-live-oauth.md`** — tenant setup, the invisible trailing-space
  audience gotcha (exact-match `access_denied`), and credential hygiene.

## 2026-07-10 — OAuth/auth open-items remediation (CT-001, RL-001/002, N-002/003, CT-002/003, AS-002 residual)

Closes the still-open findings consolidated in
`.agent/reviews/menhir-oauth-security-consolidated.md`. Deployment-gating items first.

- **CT-001 (deployment-gating)** — TOFU loopback bootstrap is now refused when the request
  carries any reverse-proxy forwarding header (`X-Forwarded-For` / `X-Real-IP` / `Forwarded`),
  closing the credential-free operator-mint window behind a same-host proxy where every peer
  reads as `127.0.0.1` (`src/menhir/api/auth.py`). Runbook expanded with the loopback-bind
  behind-proxy topology + operational discipline (`docs/runbooks/client-token-tier.md`).
- **RL-001 (deployment-gating)** — AS rate-limit keys resolve the real client from the last
  `X-Forwarded-For` hop only when `MENHIR_TRUSTED_PROXY=1`; peer address remains the default
  (`src/menhir/api/oauth_rate_limit.py`). New operator-diagnostics warn when the AS is enabled
  on a loopback bind without trusted-proxy resolution (`src/menhir/operator_diagnostics.py`).
- **RL-002** — the fixed-window limiter now hard-caps tracked keys (`_MAX_TRACKED_KEYS`) with
  FIFO eviction, bounding memory and per-call cost under a distinct-key flood
  (`src/menhir/api/oauth_rate_limit.py`).
- **N-003** — OAuth `server_error` (JWKS/IdP outage or misconfiguration) now renders `503`
  with no Bearer challenge, instead of a `401` that sends clients into a re-auth loop
  (`src/menhir/api/auth.py`).
- **N-002** — CORS preflight (`OPTIONS` + `Origin`, no `Authorization`) is exempted in
  `BearerAuthMiddleware` so the inner `CORSMiddleware` can answer it on protected routes
  (`src/menhir/api/auth.py`).
- **CT-002** — the stdio MCP process binds operator tier explicitly
  (`bind_stdio_local_trust`, `src/menhir/mcp/service_access.py`, called from
  `src/menhir/mcp/server.py`), making the local-trust decision visible instead of relying on
  the implicit empty-tier bypass; the `auth.py` provenance comment is corrected to name the
  real boundary (filesystem access to `client_tokens.db`).
- **CT-003** — bootstrap mint is atomic on the empty-store precondition
  (`ClientTokenStore.mint_bootstrap`, `INSERT ... WHERE NOT EXISTS`), so two concurrent
  loopback bootstraps cannot both mint; the route returns `409` to the loser
  (`src/menhir/api/client_token_store.py`, `src/menhir/api/routes.py`).
- **AS-002 residual** — DCR now reaps never-exchanged clients older than
  `MENHIR_OAUTH_AS_STALE_CLIENT_MAX_AGE_S` (default 24h) before enforcing the cap, and logs a
  nearing-cap warning at 80%. Added `last_exchanged` tracking (`mark_exchanged` stamped from
  the token endpoint; `reap_stale`) with an additive schema migration
  (`src/menhir/api/oauth_client_store.py`, `oauth_token.py`, `oauth_as_register.py`).
- Regression tests added across `tests/test_api_auth.py`, `test_client_token_store.py`,
  `test_client_token_tier_auth.py`, `test_oauth_rate_limit.py`, `test_oauth_client_store.py`,
  `test_operator_diagnostics.py`, and new `test_stdio_local_trust.py`.

## 2026-07-09 — Embedded OAuth AS security remediation: AS-005/006/007 low cleanups

- **AS-005 (Low)** — stop advertising an unimplemented `refresh_token` grant:
  `/.well-known/oauth-authorization-server` `grant_types_supported` and the DCR
  `/oauth/register` response `grant_types` now list only `["authorization_code"]`
  (`src/menhir/api/oauth_as_metadata.py`, `src/menhir/api/oauth_as_register.py`). The
  request-side `_SUPPORTED_GRANT_TYPES` stays tolerant. Metadata/register tests updated to
  the corrected list (sanctioned).
- **AS-006 (Low)** — documented that the `0o600` signing-key file mode is enforced only on
  the Linux VPS (the sole supported production target); Windows use of the embedded AS is
  dev-only (`src/menhir/api/oauth_keys.py` docstring).
- **AS-007 (Low/info)** — added a comment at the token `client_name` claim assembly
  (`src/menhir/api/oauth_token.py`) noting it is attacker-chosen DCR display metadata;
  verified no security/attribution decision keys on `client_name` — all identity keys on the
  AS-issued `client_id`.

## 2026-07-09 — Embedded OAuth AS security remediation: AS-003 stable consent secret across workers

- **AS-003 (Medium)** (`src/menhir/api/oauth_authorize.py`): when
  `MENHIR_OAUTH_AS_CONSENT_SECRET` is unset, the consent-integrity + one-click session HMAC
  secret was a per-process random value, so under a multi-worker server the GET that rendered
  a consent/session token and the POST that verified it could land on different workers and
  fail non-deterministically. The unset case now derives a stable secret from the persisted
  signing-key file — `sha256(b"menhir-as-consent-v1\0" + key_bytes)`, domain-separated and no
  weaker than the signing key every worker already loads — cached per process. If the key
  file is not readable yet it falls back to the per-process random (uncached, so a later call
  picks up the disk secret once the key exists). An explicit env secret still wins.
- **Operator preflight** (`src/menhir/operator_diagnostics.py`): new `oauth_as_consent_secret`
  check — when the AS is enabled and `MENHIR_OAUTH_AS_CONSENT_SECRET` is unset it warns that
  the disk-derived default covers same-host workers but multi-host scaling needs an explicit
  shared secret; passes when set; absent when the AS is disabled.
- **Tests**: `tests/test_oauth_as_consent_secret.py` (disk-derived determinism independent of
  the per-process random, explicit-env precedence, uncached missing-key fallback, and the
  three preflight states).

## 2026-07-09 — Embedded OAuth AS security remediation: AS-002 + AS-004 abuse/brute-force hardening

- **AS-002 (Medium)** — unauthenticated open DCR now has a per-IP fixed-window rate limit
  and a hard total-client ceiling (`src/menhir/api/oauth_rate_limit.py` new;
  `src/menhir/api/oauth_as_register.py`): `/oauth/register` returns 429
  `temporarily_unavailable` past `MENHIR_OAUTH_AS_REGISTER_RATE` (default 20/600s per IP) or
  once `MENHIR_OAUTH_AS_MAX_CLIENTS` (default 1000) rows exist. Added
  `OAuthClientStore.count()` for the cheap ceiling check.
- **AS-004 (Medium)** — the admin-secret POST is no longer an unlimited brute-force oracle
  (`src/menhir/api/oauth_authorize.py`): (a) failed/approve POSTs are per-IP rate-limited
  (`MENHIR_OAUTH_AS_APPROVE_RATE`, default 10/300s) *before* the secret is compared; (b) the
  consent token is now single-use — each carries a random `jti`, redeemed jtis are recorded
  in a TTL-pruned spent-set, and any replay is rejected, so one consent token can no longer
  be reused to guess the secret for its full 300s lifetime.
- **Limiter design**: dependency-free `FixedWindowLimiter` (thread-safe, self-pruning),
  keyed on `request.client.host` — deliberately **not** `X-Forwarded-For` (caller-spoofable).
  In-process only (single-host VPS target); a distributed limiter remains out of scope.
- **Tests**: `tests/test_oauth_rate_limit.py` (limiter unit tests, DCR throttle + ceiling,
  approve throttle, single-use consent replay). `tests/conftest.py` gains an autouse fixture
  that resets the module-level limiters + spent-jti set per test (their counters would
  otherwise accumulate across the process under the constant TestClient peer host).

## 2026-07-09 — Embedded OAuth AS security remediation: AS-001 client-scoped one-click + SameSite=Strict

- **AS-001 (High, release blocker)** (`src/menhir/api/oauth_authorize.py`): the Phase 8
  one-click consent session was client-agnostic (its cookie recorded only *that* an admin
  approved *something*, not *which* client) and `SameSite=Lax`, so a live session could be
  CSRF'd into minting an operator-tier code for an attacker-registered client. Fixed by
  binding the signed session to the explicitly-approved `client_id` set: `_sign_session`
  now carries `clients`, `_verify_session` returns `(sub, approved_clients)`, and the GET
  one-click branch fires **only** when the request's `client_id` is in that set — any other
  (incl. attacker-registered) client falls through to the consent page. Approvals accumulate
  across the session so a returning known client stays one-click. The `menhir_as_session`
  cookie is now `SameSite=Strict` (first-party-only; blocks the cross-site top-level-GET send).
- **Tests** (`tests/test_oauth_consent_session.py`): existing one-click tests updated to the
  new client-bound session contract; added `test_one_click_denied_for_unapproved_client`
  (the AS-001 proof), `test_session_cookie_is_samesite_strict`,
  `test_approve_then_reconnect_same_client_one_clicks`, `test_approve_accumulates_two_clients`.

## 2026-07-09 — Embedded OAuth AS Phase 9: resource-server self-wiring + full E2E (flag ON)

- **Self-wiring** (`src/menhir/api/oauth.py`, `build_oauth_config`): when
  `MENHIR_OAUTH_AS_ENABLED` is set and `MENHIR_PUBLIC_BASE_URL` is known, the resource-server
  verifier now defaults its trust anchors to *this* host — `issuer` = the public base URL,
  `jwks_uri` = `{base}/.well-known/jwks.json` (the AS's own signing-key JWKS from Phase 1),
  `authorization_servers` = `(base,)` — and token validation is turned on
  (`enabled = MENHIR_OAUTH_ENABLED or MENHIR_OAUTH_AS_ENABLED`). The embedded AS is thus its
  own IdP with zero extra config: the tokens `/oauth/token` mints (`iss` = base, `kid` from the
  local key) are exactly what the existing `OAuthTokenVerifier` consumes.
- **Precedence & fail-closed**: explicit `MENHIR_OAUTH_ISSUER` / `MENHIR_OAUTH_JWKS_URI` /
  `MENHIR_AUTHORIZATION_SERVERS` always win over the self-default (external-IdP deployments are
  untouched); only still-empty fields are filled. With the AS flag on but no public base URL,
  nothing is defaulted — the verifier rejects on its missing-issuer/JWKS check rather than
  trusting a guess (no unauthenticated access is ever opened).
- **This is where the AS flag goes ON.** The full one-codebase path is now proven end to end:
  DCR `/oauth/register` → `/oauth/authorize` (consent + admin approve) → `/oauth/token`
  (code + PKCE → RS256 JWT) → `GET /mcp/*` with that JWT accepted by `BearerAuthMiddleware`
  at the correct tier (admin-grant → operator, read-only grant → readonly). Sign-in-`/oauth/token`
  and verify-in-middleware crypto is real; only the verifier's outbound JWKS fetch is stubbed
  to the local public JWKS (as in the Phase 1 verifier suite).
- **Tests**: `tests/test_oauth_as_e2e.py` (5 — full flow at operator tier, no-token/garbage-token
  rejection, single-use across the flow, read-only tier) + `tests/test_oauth_as_self_wiring.py`
  (6 — self-default, explicit override, no-base fail-closed, AS/RS-off, RS-alone unchanged,
  partial-override). Full OAuth suite **278 passed / 1 skipped**.

## 2026-07-09 — Embedded OAuth AS Phase 8: consent-session cookie (true one-click)

- **Consent session** (`src/menhir/api/oauth_authorize.py`): a successful admin approval now
  sets a signed, `HttpOnly`, `SameSite=Lax`, short-TTL cookie (`menhir_as_session`,
  `MENHIR_OAUTH_AS_SESSION_TTL_S`=600s, `Secure` when the public base URL is https, scoped to
  `/oauth/authorize`). On a later `GET /oauth/authorize` a valid session cookie skips the
  consent page and issues the code directly — the standard IdP "already signed in" one-click.
- **Safety**: param validation (client exists, exact `redirect_uri`, PKCE, scope subset) always
  runs *before* the cookie is consulted, so a stale cookie can never bypass the open-redirect /
  PKCE / scope checks; the issued code is still PKCE-bound + single-use. One-click also requires
  a still-configured operator key (removing the admin secret disables it even with a live
  cookie). The cookie is HMAC-signed with the same secret source as the Phase 6 integrity token
  but domain-separated via a `kind:"session"` tag so neither token can be replayed as the
  other. 7 new tests; full OAuth suite 267 passed / 1 skipped.

## 2026-07-09 — Embedded OAuth AS Phase 7: `/oauth/token` (code → signed JWT)

- **Token endpoint** (`src/menhir/api/oauth_token.py`): `POST /oauth/token` exchanges a
  single-use authorization code (Phase 6) for a signed **RS256** access token. Public-client
  `authorization_code` grant + PKCE; gated by `MENHIR_OAUTH_AS_ENABLED` (404 when off); wired
  in `server.py`.
- **Flow**: `AuthCodeStore.redeem(code, client_id, redirect_uri)` (atomic single-use — the code
  is burned on first presentation even if PKCE then fails) → `verify_pkce(code_verifier,
  record.code_challenge)` → mint. Failures return OAuth token errors
  (`unsupported_grant_type` / `invalid_request` / `invalid_grant`) as JSON 400 with
  `Cache-Control: no-store`.
- **Token shape** (findings §5): `iss` = `MENHIR_PUBLIC_BASE_URL`, `aud` = `{base}/mcp-http`,
  `sub` = approving admin (`menhir-admin`), `client_id`/`client_name` from the registered
  client (provenance), `scope` from the code, `iat`/`exp` (`MENHIR_OAUTH_AS_ACCESS_TTL_S`,
  default 3600s), `kid` from the Phase 1 signing key, RS256. The `kid` is read through the
  JOSE seam's serialized JWK — the key handle is never introspected. Verified in-test to decode
  through the same `jose_provider` path the resource-server verifier uses. No refresh token
  (deferred). 9 new tests; full OAuth suite 260 passed / 1 skipped.

## 2026-07-09 — Embedded OAuth AS Phase 6: `/oauth/authorize` + admin-gated consent + PKCE

- **Authorization endpoint** (`src/menhir/api/oauth_authorize.py`): `GET /oauth/authorize`
  validates the OAuth 2.1 authorization-code + PKCE request and renders a single-admin consent
  page; `POST /oauth/authorize` verifies the admin secret, issues a single-use code via the
  Phase 5 `AuthCodeStore`, and 302s back to the registered `redirect_uri?code=...&state=...`.
  Gated by `MENHIR_OAUTH_AS_ENABLED` (404 when off); wired in `server.py`.
- **Open-redirect safe error dichotomy**: an unknown `client_id` or a `redirect_uri` that is
  not an exact match to the client's registered set returns a direct 400 and never redirects;
  all other protocol errors (`unsupported_response_type` / `invalid_request` / `invalid_scope`)
  302 back to the proven redirect_uri with `error`/`error_description`/`state`.
- **PKCE required, S256 only**; `plain` and missing challenges are rejected. Requested scope is
  validated ⊆ the client's granted scopes (defaults to the full grant when omitted).
- **Admin consent**: approval requires the operator secret (`MENHIR_OPERATOR_KEY`) via a
  constant-time compare; an unconfigured operator key cannot approve (403). A stateless
  HMAC-SHA256 integrity token (`MENHIR_OAUTH_AS_CONSENT_SECRET`, TTL
  `MENHIR_OAUTH_AS_CONSENT_TTL_S`=300s) binds the approval to the exact params shown, closing a
  display-vs-submit redirect_uri swap. All HTML output is escaped (the page carries the admin
  secret). Codes carry `subject="menhir-admin"`. 16 new tests; full OAuth suite 251 passed / 1
  skipped.

## 2026-07-09 — Embedded OAuth AS Phase 4: Dynamic Client Registration (RFC 7591)

- **DCR endpoint** (`src/menhir/api/oauth_as_register.py`): `POST /oauth/register` lets MCP
  connectors (ChatGPT, claude.ai web, Claude Code) self-register and receive a `client_id`
  with no operator pre-provisioning, persisting to the Phase 2 `OAuthClientStore`. Gated by
  `MENHIR_OAUTH_AS_ENABLED` (404 when off); wired in `server.py`.
- **Public + PKCE profile only**: issues no `client_secret`; rejects any
  `token_endpoint_auth_method` other than `"none"`. Validates redirect URIs (https, or http
  to a loopback host), caps at 5, narrows requested scope to the supported set, and checks
  grant/response types. RFC 7591-shaped success (201) and error (400 `invalid_client_metadata`
  / `invalid_redirect_uri`) bodies. No outbound I/O.
- **CIMD accept-path deferred** to a follow-on (Phase 4b) because it adds an SSRF-exposed
  outbound client-metadata fetch that warrants its own guards/audit; DCR alone unblocks
  one-click on both connectors.

## 2026-07-09 — Embedded OAuth AS Phase 5: single-use authorization-code store

- **Auth-code store** (`src/menhir/api/auth_code_store.py`): `AuthCodeStore` persists OAuth
  authorization codes (PKCE challenge, exact redirect URI, client binding, scope, resource,
  subject, hard expiry) as sha256 hashes in the shared `menhir_oauth_as.db` (`oauth_codes`
  table). `/authorize` (Phase 6) issues; `/token` (Phase 7) redeems.
- **DB-enforced single use**: `redeem` claims a code via one atomic `UPDATE ... WHERE
  redeemed_at IS NULL AND expires_at > ?`, so two concurrent redemptions cannot both win
  (verified by a threaded test) — not application locking. Returns `None` indistinguishably
  for unknown / expired / already-redeemed / wrong-client_id / wrong-redirect_uri.
- **PKCE S256 only**: `issue` rejects any non-`S256` method; `verify_pkce` (pure function,
  RFC 7636 S256, constant-time via `hmac.compare_digest`) is unit-tested against the RFC
  Appendix B vector. TTL default 120s (`MENHIR_OAUTH_AS_CODE_TTL_S`); raw codes never
  persisted or logged. No HTTP surface this phase (inert until Phase 6/7 import it).

## 2026-07-09 — Embedded OAuth AS Phase 3: authorization-server metadata (RFC 8414)

- **AS discovery endpoint** (`src/menhir/api/oauth_as_metadata.py`): serves
  `GET /.well-known/oauth-authorization-server` (RFC 8414) so MCP connectors (ChatGPT,
  claude.ai web, Claude Code) can discover the embedded AS. Static/instant (no I/O), issuer
  and `/oauth/{authorize,token,register}` + `jwks_uri` all derived from `MENHIR_PUBLIC_BASE_URL`;
  advertises `code` / PKCE `S256` / public clients (`token_endpoint_auth_methods: ["none"]`).
- **Gated by a new, independent flag** `MENHIR_OAUTH_AS_ENABLED` (default false; distinct from
  the resource-server `MENHIR_OAUTH_ENABLED`). Route 404s while disabled; the flag stays off
  until the `/oauth/*` endpoints are wired (Phase 9). Unauthenticated by design (well-known
  path stays outside `BearerAuthMiddleware`). Wired in `server.py` beside the RS metadata router.

## 2026-07-09 — JOSE provider seam: isolate the crypto library behind an interface

- **Library-neutral seam** (`src/menhir/api/jose_provider.py`): all JWT/JWK/JWKS operations
  now go through a narrow provider interface (`parse_jwks`, `jwks_has_kid`, `verify_jwt`,
  `generate_signing_key`, `serialize_key`, `load_key`, `sign_jwt`) that raises a
  provider-neutral `JoseError`. The concrete library (joserfc) is confined to this one
  auditable module; the OAuth verifier (`api/oauth.py`) and signing-key code
  (`api/oauth_keys.py`) no longer import any JOSE library directly.
- **Why**: de-risks the JOSE library choice — swapping joserfc for another implementation
  (e.g. PyJWT) becomes a single new provider file with no changes to the verifier or its
  seams, and collapses the security-critical JOSE surface into one module for the Phase 10
  audit. Behavior unchanged; full OAuth suite 200 passed / 1 skipped, plus 8 new provider
  contract tests (round-trip, error normalization, algorithm-allowlist enforcement).

## 2026-07-09 — S-009: migrate JOSE from deprecated authlib.jose to joserfc

- **JOSE library migration**: the OAuth resource-server verifier (`api/oauth.py`) and the
  embedded-AS signing key (`api/oauth_keys.py`) now use `joserfc` instead of the deprecated
  `authlib.jose`. Behavior is preserved: RS256 alg allowlist enforced at
  `jwt.decode(..., algorithms=...)`, time claims validated via `JWTClaimsRegistry(leeway=)`,
  JWKS via `KeySet.import_key_set`, `kid` lookup via `get_by_kid`. Signing keys now pin the
  `kid` explicitly with `ensure_kid()` (joserfc does not auto-assign one).
- **Dependency**: `pyproject.toml` swaps `authlib>=1.6,<2` for `joserfc>=1.0,<2`.
- **Tests**: the JOSE-specific helpers in `test_oauth_jwt_verifier.py` and `test_oauth_keys.py`
  were updated to mint tokens/keys with joserfc (structural library migration only — all
  assertions unchanged). Full OAuth suite: 200 passed, 1 skipped.
- Resolves the S-009 maintenance item; unblocks the embedded-AS `/token` endpoint (Phase 7)
  to sign on the same library as the verifier.

## 2026-07-09 — Embedded OAuth AS Phase 2: persistent registered-client store

- **Registered-client store** (`src/menhir/api/oauth_client_store.py`): SQLite-backed
  `OAuthClientStore` holding OAuth clients that register via Dynamic Client Registration
  (Phase 4 writes to it). Records carry `client_id`/`client_name`/`redirect_uris`/`scopes`/
  `token_endpoint_auth_method` plus a `client_secret_hash` — provenance for tokens minted in
  Phase 7. `register`/`get`/`all`/`verify_secret`; duplicate `client_id` raises `ValueError`.
- **Secrets never stored plaintext**: only sha256 hashes are persisted; `verify_secret` uses
  `hmac.compare_digest` (constant time). Public PKCE-only clients (`token_endpoint_auth_method
  = "none"`) store an empty hash and never authenticate by secret.
- **Shared AS database**: bound to `oauth_as_db_path()/menhir_oauth_as.db` — the embedded-AS
  state DB that Phase 5's auth-code store will also use (as a separate table), distinct from
  the per-client-token `client_tokens.db` subsystem. Storage layer only; no HTTP this phase.

## 2026-07-09 — Embedded OAuth AS Phase 1: local signing key + JWKS endpoint

- **Local RSA signing key** (`src/menhir/api/oauth_keys.py`): a 2048-bit RSA key is
  generated on first use, persisted as a private JWK to `oauth_as_db_path()/oauth_signing_key.json`
  with `0o600` file mode, and reloaded thereafter. The key carries a stable RFC 7638
  thumbprint `kid`, so caching clients survive server restarts. `get_signing_key()` is a
  lazy module singleton.
- **JWKS endpoint**: `GET /.well-known/jwks.json` serves the public key set
  (`{"keys": [...]}`, no private material) unauthenticated, matching the existing
  `.well-known` discovery routes. Foundation for the embedded AS `/token` (Phase 7) and
  resource-server self-wiring (Phase 9). Uses Authlib JOSE (consistent with the existing
  verifier; joserfc migration deferred to S-009).

## 2026-07-09 — Per-client token tier: list clients + MCP tools

- **List clients**: `GET /api/admin/clients` (operator-gated) and MCP tool `list_clients`
  return registered non-revoked clients as `client_id`/`client_name`/`tier`/`created_at` —
  never any token material.
- **MCP management tools**: `mint_client` / `revoke_client` / `list_clients` (all operator
  tier) manage per-client tokens from within an MCP session via a shared store accessor
  (`get_client_token_store()`), complementing the REST `/api/admin/*` endpoints.
- **Admin gate hardening**: bootstrap is now POST-mint only — the `is_mint` check is
  method-aware, so a `GET /api/admin/clients` (list) always requires a real operator
  credential and is never reachable via the loopback bootstrap path.

## 2026-07-09 — Enforced per-client token tier (tamper-proof provenance)

- **Enforced tier** (`MENHIR_CLIENT_TOKENS_ENABLED=1`): each bearer token resolves via a
  hashed registry to a registered `client_id`/`client_name`/`tier`, bound with
  `trust_identity_headers=False` so a caller cannot relabel itself via `x-yawn-*` headers
  (tamper-proof). Unknown / revoked / missing tokens are rejected 401. When enabled the tier
  owns protected auth (like OAuth mode).
- **Admin gate for `/api/admin/*`**: the operator key OR an operator-tier minted token
  authorizes any admin action. An unauthenticated loopback caller may ONLY mint, and only
  while no active token exists (trust on first use); revoking the last active token re-opens
  bootstrap. Loopback-origin admin is trusted only when the server is loopback-bound (a
  network bind behind a same-host reverse proxy does not get loopback admin).
- **REST endpoints**: `POST /api/admin/clients` (mint — returns the raw token once) and
  `POST /api/admin/clients/{client_id}/revoke`.
- **Storage**: `ClientTokenStore` at `oauth_as_db_path()/client_tokens.db` persists sha256
  token hashes only; the raw token is shown once at mint time. Enabling the tier counts as an
  authenticated mode for the no-auth bind-safety guard.
- Commits: `a9fe29a` (storage), `35e90f8` (verification core), `749da06` (wiring),
  `fa0c764` (loopback mint-only), `563f353` (trust-on-first-use bootstrap). See
  `docs/runbooks/client-token-tier.md`.

## 2026-07-09 — Loopback multi-client provenance (no-auth mode)

- **Per-client provenance in loopback no-auth mode**: in loopback no-auth mode (no static keys,
  OAuth disabled), the middleware previously short-circuited before binding any identity, so all
  local clients were anonymous. It now derives self-declared client identity from
  `x-yawn-client-name` / `?client_name=` and binds it via `bind_request_session` for
  telemetry/provenance, without changing access/authorization behavior (no tier bound).
- **Stable `client_id` from `client_name`**: `_request_session_headers` derives a stable
  `sha256(client_name)[:16]` id when a real (non-default) label is supplied and no `client_id` or
  api_key is present. Gated on `trust_identity_headers`, so the OAuth path is unaffected (an empty
  principal `client_id` stays empty — byte-for-byte unchanged).
- Trust model: labels are **cooperative, not enforced** (safe on loopback; enforced identity is the
  future per-client-token tier). New tests in `tests/test_loopback_multiclient_provenance.py`.

## 2026-07-09 — OAuth hardening: bind guard, gated JWKS refresh, stable client identity (S-001..S-010)

- **OAuth-aware bind safety guard** (S-001a/b/c): `validate_no_auth_bind_safety` now accepts
  `oauth_enabled=True` so OAuth-only remote deployments (no static keys) can bind to non-loopback
  hosts without raising `ValueError`. `__post_init__` passes `build_oauth_config(self).enabled`
  via a deferred local import to avoid circular dependency. Diagnostics
  (`no_auth_remote_bind_guard` check + `no_auth_remote_bind_allowed` safety field) also respect
  OAuth mode.
- **Gated JWKS force-refresh** (S-002a/b + S-008): `_decode_with_cached_jwks` only issues a
  forced JWKS refetch when the token's `kid` is absent from the cached key set (genuine key-rotation
  signal). Malformed / expired / wrong-audience tokens never trigger an outbound fetch. A 30s
  rate-limit prevents attacker-driven JWKS load against the IdP.
- **Stable client identity for missing `sub`** (S-003): `_derive_subject` returns `client:<client_id>`
  for tokens without `sub` (common in client-credentials flows) instead of merging all such callers
  into `"oauth-user"`. Tokens lacking both `sub` and `client_id`/`azp` are rejected.
- **`x-yawn-client-name` ignored in OAuth mode** (S-004): the caller-supplied header is no longer
  trusted when OAuth is enabled; the token-derived `client_name` claim wins instead.
- **Pinned JWT algorithm allowlist** (S-006): `OAuthTokenVerifier` now constructs a
  `JsonWebToken(list(...))` pinned to `RS256` (configurable via `MENHIR_OAUTH_ALLOWED_ALGORITHMS`).
  Tokens with unexpected `alg` values are rejected before key resolution.
- **Closed wildcard CORS default** (S-007): `CORSMiddleware` is only added when
  `MENHIR_CORS_ORIGINS` is explicitly set; no default `"*"`.

## 2026-07-09 - Stale Verification Diagnostics v1 — verification coverage report

- **`scripts/maintenance/report_stale_verifications.py`** (new): readonly CLI tool that fetches
  stale file-anchored memories and their verification receipts, classifies each receipt against
  its anchor (memory_uuid/project/path/timestamp), and produces a diagnostics report showing
  which anchors have valid post-dirty same-path receipts, which receipts are ignored (and why),
  and the latest valid outcome per anchor. Human-readable summary and `--json` mode. Environent
  fallbacks: `MENHIR_URL`, `MENHIR_TOOL_EVENTS_URL`, `MENHIR_READONLY_KEY`, `MENHIR_AGENT_KEY`.
  Report-only: never reads file contents, never clears dirty flags, never writes receipts.
- **Receipt classification**: pure functions `classify_receipt()` and `build_report_items()` in
  the CLI script, fully testable without HTTP/Neo4j. Valid receipts must match memory_uuid,
  project, path, and have `verified_at >= dirty_at` with a parseable timestamp. Ignored receipts
  carry a deterministic reason: `wrong_memory_uuid`, `wrong_project`, `wrong_path`, `pre_dirty`,
  or `timestamp_error`. Malformed timestamps are handled conservatively — never crash, never
  treated as valid.
- **Output status values**: `no_receipt`, `only_ignored_receipts`, `valid_still_valid`,
  `valid_outdated`, `valid_unclear`. Simple and deterministic.
- **Core invariant preserved**: a wrong-path, wrong-project, pre-dirty, or malformed-timestamp
  receipt cannot look valid. The report never marks a stale memory fresh.
- Tests: `tests/test_report_stale_verifications.py` (35) — classification, report building,
  counts, JSON roundtrip, human output, network failure exit, no file-content safety assertion.
- Docs: `docs/runbooks/stale-verification-diagnostics.md` — purpose, CLI usage, JSON mode,
  environment variables, status values, receipt validity rules, safety notes, known limitations.
- No Phase 3 changes, no TurnEvidence changes, no file content capture, no transcript capture.
- No lifecycle mutation: no auto-refresh, no dirty clearing, no deletion/expiration, no
  supersession, no ranking changes, no review-task creation.

## 2026-07-08 - Real DB Smoke Receipt Pack v1 — Hook Center stale anchor lane

- **New smoke harness** `scripts/smoke/hook_center_stale_lane_smoke.py`: proves the full
  stale-file-anchor lane against a **real** Neo4j — file event -> dirty -> stale detection
  -> recall label -> formatter/context warning -> verification-receipt enrichment (path-aware,
  post-dirty only). 12 checks, all PASS on a throwaway backend.
- HTTP endpoints run against a self-served throwaway Menhir (real router over a real Neo4j
  adapter; no full runtime, no embedder). Recall/formatter/context and receipt-matching run
  in-process through the **real** services against real Cypher; only the embedding-dependent
  graphiti vector search is seeded.
- Proves the core invariant *a wrong current-state view is worse than a miss*: wrong-path,
  pre-dirty, and malformed (HTTP 400) receipts never reassure; an `outdated` receipt yields
  `do_not_rely_update_or_supersede` and mutates no lifecycle state.
- Tests: `tests/test_hook_center_stale_lane_smoke.py` (10 tests, mocked HTTP + backend, no
  Neo4j) — JSON-only output, exit codes, honest skip/fail distinction, no file-content upload.
- Docs: `docs/smoke/2026-07-08-hook-center-stale-lane.md` (receipt),
  `docs/runbooks/hook-center-stale-lane-smoke.md` (runbook), pointer in
  `docs/hook-center-tool-events.md`.
- Validation slice only: no lifecycle behavior, no Phase 3 changes, no TurnEvidence changes,
  no file content or transcript capture.

## 2026-07-08 - Stale Recall Advisory Pack v1 — LLM-facing advisory on stale recall items

- **Stale-action advisory** on stale recall output: stale items now include `stale_action`
  (`"verify_current_file_before_relying"`) and `stale_advisory` (prose telling the LLM to
  inspect the current file before relying). Only emitted when `stale_anchor=true`.
- **No stale_action/stale_advisory** on `stale_anchor=false` items or unlabeled items.
- **Advisory-only**: no auto-refresh, no dirty clearing, no filtering, no down-ranking,
  no deletion or expiration.
- Tests: 7 new formatter tests added to `tests/test_recall_stale_labels.py` — stale_action,
  stale_advisory content, stale_false omits, unlabeled omits, compact mode, full mode.
- Docs: `docs/hook-center-tool-events.md` updated with advisory output shape.
- No Phase 3 changes, no TurnEvidence changes, no file content capture, no transcript
  capture.

## 2026-07-08 - Context Builder Stale Advisory Pack v1 — stale advisory in context paths

- **ContextBuilderService.build_context()** now emits an inline stale advisory line
  (`⚠️ Stale file anchor: <path> changed after...`) for stale file-anchored memories.
- **`_compact_memory_item`** formatter now passes through `stale_anchor`, `stale_action`,
  and `stale_advisory` when the row dict contains `stale_anchor_info`. Used by
  `recall_context_memories` tool for `relevant` items.
- **`recall_context_memories`** tool includes `stale_anchor_info` from recall results in
  the dict passed to `_compact_memory_item`.
- **`STALE_ACTION`/`STALE_ADVISORY`** moved from `menhir.mcp.formatters` to
  `menhir.services.stale_labeling` so both formatters and context builder can import
  them without creating bad dependency direction.
- Tests: 8 new tests in `tests/test_recall_stale_labels.py` — `_compact_memory_item`
  stale handling (advisory, non-stale omits, unlabeled omits, advisory content) +
  ContextBuilderService stale advisory (stale warning, non-stale omits, unlabeled omits).
- Docs: `docs/hook-center-tool-events.md` updated with context path coverage.
- No Phase 3 changes, no TurnEvidence changes, no file content capture, no transcript
  capture. Advisory-only throughout.

## 2026-07-09 - Stale Anchor Verification Receipts v1 — durable audit receipts

- **StaleAnchorVerification** storage in `ToolEventRepository`: `record_stale_anchor_verification()`,
  `list_stale_anchor_verifications()`, `latest_stale_anchor_verifications()`. Nodes created as
  `(:StaleAnchorVerification)` with full receipt properties. Outcome validated against allowed set.
- **API endpoints**: `POST /api/tool-events/stale-verifications` (agent tier) and
  `GET /api/tool-events/stale-verifications` (readonly tier) with `memory_uuid`, `project`, `path`,
  `limit` filters.
- **Adapter delegation**: `MemoryGraphAdapter` exposes `record_stale_anchor_verification`,
  `list_stale_anchor_verifications`, `latest_stale_anchor_verifications`.
- **Recall enrichment**: `RecallService.recall()` fetches latest post-dirty verification for stale
  items and attaches `stale_verification` to `stale_anchor_info`. Best-effort — failure logs and
  continues without verifications.
- **Advisory resolution**: `_resolve_stale_advisory()` in `formatters.py` adjusts action/advisory
  based on verification outcome. `still_valid` → verified advisory; `outdated` →
  `do_not_rely_update_or_supersede` action + stronger advisory.
- **Context builder** updated for verification-adjusted inline warnings.
- **New verification constants** in `stale_labeling.py`: `STALE_ACTION_OUTDATED`,
  `STALE_ADVISORY_OUTDATED`, `STALE_ADVISORY_STILL_VALID`, `ALLOWED_VERIFICATION_OUTCOMES`.
- **CLI script** `scripts/maintenance/record_stale_anchor_verification.py` — supports
  `--dry-run`, `--json`, `MENHIR_AGENT_KEY`.
- Tests: `tests/test_stale_anchor_verifications.py` (39) — repository validation, API routes,
  verification enrichment, formatter output, CLI.
- Docs: `docs/hook-center-tool-events.md` added Stale Anchor Verification Receipts v1 section.
- No Phase 3 changes, no TurnEvidence changes, no file content capture, no transcript capture.
- Receipt/audit-only: no auto-refresh, no dirty clearing, no filtering, no down-ranking,
  no deletion/expiration, no auto-supersession.

## 2026-07-08 - Recall Stale Label Pack v1 — stale-anchor labeling on recall output

- **Stale-anchor labeling** on recall output: each `ScoredMemory` gains an optional
  `stale_anchor_info` dict. Stale items get `stale_anchor=true, stale_reason, dirty_at,
  anchored_at, path`. Non-stale items get `stale_anchor=false`. Label-only: no filtering,
  no deletion, no expiration, no down-ranking.
- **`ScoredMemory.stale_anchor_info`** — new optional field on the frozen dataclass.
- **`src/menhir/services/stale_labeling.py`** — pure helper `label_stale_anchors(items,
  stale_anchors)` for dict-level enrichment with uuid and name fallback matching.
- **`_compact_scored_item`** formatter includes stale fields when present; shown in both
  compact and full modes.
- **Wiring** inside `RecallService.recall()` — calls `adapter.stale_anchored_memories()`
  best-effort after temporal enrichment. Failure logs and continues without stale labels.
- Tests: `tests/test_recall_stale_labels.py` (18) — pure helper coverage (match by uuid,
  non-match, stale fields, multiple items, no removal, no rank change, empty anchors,
  name fallback, uuid preserved) + service integration (stale labels applied, failure
  resilience, no results removed, no rank change) + formatter output (stale fields
  included, stale=false, omitted when None, compact mode).
- Docs: `docs/hook-center-tool-events.md` updated with Recall Stale Labeling v1 section.
- No Phase 3 changes, no TurnEvidence changes, no file content capture, no transcript
  capture.

## 2026-07-08 - Hook Center Actionability Pack v1 — stale endpoint, dirty report, policy guard

- **Stale-anchor diagnostic endpoint** `GET /api/tool-events/stale` (readonly) — returns stale
  anchored memories directly via `adapter.stale_anchored_memories()`. Supports `project` and `limit`
  query params. No write behavior.
- **Dirty-file report script** `scripts/maintenance/report_dirty_files.py` — CLI tool that fetches
  `GET /api/tool-events/dirty` and prints a human-readable summary or `--json` output. Respects
  `MENHIR_TOOL_EVENTS_URL`, `MENHIR_URL`, and `MENHIR_AGENT_KEY`. Report-only: never clears dirty
  flags or refreshes structure.
- **Policy Guard** `scripts/hooks/menhir_policy_guard.py` — optional `PreToolUse` hook that reads
  `.menhir/policy.json` and can `watch`, `warn`, or `block` edits to protected files. Uses
  `fnmatch` path matching. Handles rename (checks both paths). Never calls an LLM, never uploads
  file contents. Fail-open: no policy file / non-file tool / malformed input / missing path → exit 0.
- Tests: `tests/test_hook_center_actionability.py` (20) — stale endpoint (returns anchors, project/limit
  params, empty), report script (URL resolution, auth header, JSON mode, summary mode, network failure),
  policy guard (no policy, malformed, non-file, frozen warn, frozen block, branch scope, rename,
  backslash normalization, no content in output, watch mode). Gate: `pytest tests -q -k "hook_center or
  tool_event or dirty or stale or policy_guard"`.
- Docs: `docs/hook-center-tool-events.md` updated with Actionability Pack v1 sections; `scripts/hooks/README.md`
  updated with Policy Guard install/config.
- No Phase 3 changes, no TurnEvidence changes, no file content capture, no transcript capture.

## 2026-07-08 - Hook Center Live Smoke v1 — end-to-end smoke harness

- **Live smoke script** `scripts/smoke/hook_center_live_smoke.py` — validates Hook Center
  components end-to-end against a throwaway Menhir instance: checks server readiness, POSTs a
  `file_changed` event, queries dirty/stale endpoints, runs `report_dirty_files.py`, and tests
  `menhir_policy_guard.py` warn/block modes. Supports `--url`, `--project`, `--path`,
  `--require-stale`, `--skip-policy`, and `--json`. Result states: `PASS`,
  `PASS_WITH_UNSCANNED_FILE`, `FAIL`. Never uploads file content or captures transcripts.
- **Runbook** `docs/runbooks/hook-center-live-smoke.md` — purpose, prerequisites (throwaway
  Menhir), usage, expected output, troubleshooting, safety notes.
- Tests: `tests/test_hook_center_live_smoke.py` (10) — URL resolution, no content in payload,
  accepted=false fail, accepted+unscanned → PASS_WITH_UNSCANNED_FILE, report script failure,
  policy guard warn/block, require-stale, skip-policy.
- No runtime changes. No Phase 3 changes. No TurnEvidence changes. No file content capture.
- Docs: `docs/hook-center-tool-events.md` and `scripts/hooks/README.md` linked to the runbook.

## 2026-07-08 - Hook Center / tool-event capture v0 (stale file-reference prevention) — +PR-review fixes

- New deterministic event layer so file edits mark memory/context stale WITHOUT trusting the LLM to call a memory tool. A hook observes a file edit/write/delete/rename and POSTs a normalized `file_changed` event; menhir marks the affected structure-file node dirty and lets recall/diagnostics detect memories anchored to a file that changed after they were anchored. Reuses the EXISTING structural code graph (`:Entity{structure_role:'file'}` + `ANCHORED_TO{created_at}`) — no new node architecture, no schema change. Plan: `.agent/plans/menhir-hook-center-tool-events-v0.md`; docs: `docs/hook-center-tool-events.md`.
- `infrastructure/tool_event_repository.py` (new): `ToolEventRepository` — `record_file_event()` sets `structure_dirty`/`dirty_at`/`last_event_op`/optional `after_hash`+`mtime` on the matching file `:Entity` (rename marks both paths; a file not yet scanned is accepted, marks nothing, never errors; stores NO content); `stale_anchored_memories()` returns `(sem)-[a:ANCHORED_TO]->(f)` where `f.structure_dirty AND f.dirty_at > a.created_at`; `list_dirty_files()`/`clear_file_dirty()`/`dirty_stats()`.
- `api/routes.py`: `POST /api/tool-events` (agent) — forward-compatible name; v0 handles `file_changed`, accepts-and-ignores other `event_type`s; derives `structure_project` from `project_root` basename; no content/transcript required. `GET /api/tool-events/dirty` (readonly) — dirty files + stale anchors diagnostic. `memory_graph_adapter`: additive delegators.
- `scripts/hooks/menhir_file_event.py` (new): stdlib hook mapping a Claude/Codex `PostToolUse` payload (Edit/Write/MultiEdit/NotebookEdit/create/delete/move) to the normalized event; local sha256 of the file (HASH only, never content; skipped on delete); fail-open (menhir down / unreadable file / malformed input -> log + exit 0, never blocks the tool, never prints into agent context); `--dry-run`. Reuses the producer shared-core helpers. OpenCode has no clean file-event hook surface -> documented limitation. `scripts/hooks/file-event-hooks.example.json` gives the Claude/Codex registration.
- **PR review fixes:**
  - Hook paths are normalized to repo-relative structure paths when possible (absolute paths under `project_root` → `src/foo.py`; original path stored in `metadata.original_path`).
  - Project-scoped dirty marking falls back to path-only marking when `structure_project` does not match, avoiding false negatives.
  - Unsupported non-file event types can be accepted-and-ignored without requiring a path in the request body.
- Tests: `tests/test_hook_center_tool_events.py` (35) — event schema/minimal event, optional metadata non-fatal, Claude+Codex normalization, hook fail-open, dirty marking, anchored-memory stale detection, delete/rename don't crash, NO content sent, path normalization (absolute/relative/outside-repo/original-path metadata), project-scope fallback (match/mismatch/no-path/rename), endpoint accept-ignore-no-path, file_changed-no-path 400. Gate (risk-based, NOT full suite — additive new subsystem, no schema/runtime/dep change): targeted selector 73 passed. Producer TurnEvidence + Phase 3 consumer behavior unchanged.

## 2026-07-08 - Phase 3 cross-check-quality pack v1: deterministic SUM arithmetic grounding (promoted ON)

- Precision-preserving cross-check adjustment: "arithmetic is not a belief." When a fold-SUM's amounts are each an EXPLICIT price literally present in their source span (distinct tokens, summing to the value), the arithmetic is proven DETERMINISTICALLY from source text — strictly stronger than the blind holistic re-derivation (Lever B), which for that case is pure false-abstention noise. `perception.py`: `_sum_arithmetic_grounded()` + `_price_token_count()` (boundary-guarded standalone-price match: `$50`/`50`/`50.00`/`50 dollars`, rejects `150`/`2.50`/`50.5`, allows a trailing sentence period). When grounded, the gate SKIPS the holistic veto-4 (`triangulated=True`) and continues to the sharper veto-5 verifier, which still audits item MEMBERSHIP/double-count — so the wrong-write envelope is unchanged. SUM-only, and behind `enable_sum_grounding`.
- Safety by construction: a hallucinated price (not in any span), an in-span double-count (two `$40` events, one `$40` token), or a mis-sum all fail grounding -> fall through to the unchanged holistic veto. Cross-episode re-narration double-counts are caught upstream (Lever C2 dedup + veto-2b unresolved-coreference), before veto-4.
- Instrumentation (items 1-2): `GateDecision.abstained_value` + `cross_margin` on a cross-check veto (the value under test + |value - holistic|, since `value` is None on an abstention), and `sum_grounded` on the decision + a `view_audit_sum_grounded` audit stamp on grounded commits.
- Config: `MENHIR_PERSONAL_MEMORY_SUM_GROUNDING` (settings `personal_memory_consolidation_sum_grounding`), threaded /api/phase3/run + scheduler -> consolidate_personal_memory -> perceive_and_fold. **Default PROMOTED to True** after live characterization (below); set `=0` to disable. Gate/perceive library defaults stay False (opt-in for direct callers/tests).
- **Live characterization (throwaway :8099, gpt-4o-mini, real :8090 untouched), OFF vs ON, 5 SUM phrasing variants, N=5 (ON 2x):** cross-check-dominated variants jumped `two-episode` 40%->100% and `one-sentence` 40%->90%; `worded`/`sequential` stayed ~100% (grounding just makes them deterministic/cheaper: `llm_calls` 7->6, holistic skipped); `list` is high-variance on count_floor/verification (a separate extraction gap grounding doesn't touch). **`wrong_view_writes=0` in EVERY cell across OFF + 2x ON** — item 5's promotion gate met. Mechanism confirmed via `llm_calls` (grounded commits skip the holistic call and rescue the OFF holistic-veto abstentions).
- Tests: `tests/test_cross_check_quality_pack.py` (20) — `_price_token_count` boundary cases; grounding SAFETY (hallucinated/double-count/mis-sum NOT grounded; clean case grounded); gate skips holistic only when grounded, KEEPS the verifier, opt-in default off at the gate, SUM-only. `tests/test_consolidate_personal_memory.py` +2 (settings default True + env disable; threads to perceive). Full suite 2108 passed, 31 skipped.

## 2026-07-08 - Wire verify_retries to config (opt-in, default 0) + live characterization finding

- Follow-up to the consumer-quality pack: the `verify_retries` gate param is now reachable from config as `MENHIR_PERSONAL_MEMORY_VERIFY_RETRIES` (settings `personal_memory_consolidation_verify_retries`, default 0 = behaviour unchanged), threaded through `/api/phase3/run` and the maintenance scheduler into `consolidate_personal_memory` -> `perceive_and_fold`. `config/settings.py` (field + env read), `services/scheduler_tasks.py` (param + pass-through), `api/routes.py` (route reads the setting), `services/maintenance_scheduler.py` (field + call), `core/runtime.py` (wiring).
- **Live characterization (throwaway menhir :8099, gpt-4o-mini, real :8090 untouched):** a focused 2-purchase fold-SUM probe (N=10 each) commits 5/10 at BOTH retries=0 and retries=1 (0 wrong, 0 duplicate writes). Every abstention fired on `perception_abstained_cross_check` (Lever B holistic) with `llm_calls=4` — the pipeline stops at the cross-check gate, BEFORE the verifier (Lever C4) runs, so `verify_retries` is structurally unable to rescue it. Decision: **default stays 0** (the knob is opt-in infra for future tuning, NOT enabled). The `count_vs_spend_partial` receipt and the `verify_votes`/`verify_k`/`verify_attempts` receipt-clarity fields were confirmed firing live and are the durable value. Full evidence: archolith-bench `benchmarks/menhir-phase3-view-consolidation-2026-07-07.md`.
- Tests: `tests/test_consolidate_personal_memory.py` +2 (settings reads the env var; task threads `verify_retries` to `perceive_and_fold`, default 0). No consumer LOGIC changed; all guards still pinned.

## 2026-07-08 - Phase 3 consumer-quality pack v1 (count-vs-spend receipt, verify retry/clarity, corrections)

- Consumer-side pack governed by the invariant **a wrong current-state View is worse than a miss** — no change loosens precision. Plan: `.agent/plans/menhir-phase3-consumer-quality-pack-v1.md`. Item 1 scoped SAFETY-ONLY (co-extraction stays the extractor's stochastic job; count-vs-spend stays a characterization case, not a gate).
- `services/perception.py`: `count_spend_compound(text)` — deterministic detector for a "bought N <plural-noun> for $M [total]" clause (N>=2). Used ONLY to record a `count_vs_spend_partial` observability receipt when the compound is detected but the run committed only one of {count, spend} for that noun — the fail-closed is now legible, never a silent miss. It emits no Event and writes no View; gated behind `record_abstentions`, so existing behavior/tests are unchanged.
- `services/perception.py`: verifier receipt clarity — `verify_candidate_detailed` returns `(ok, votes, k)`; `GateDecision` gains `verify_votes`/`verify_k`/`verify_attempts`; a fail-closed SUM now records HOW CLOSE the audit was. The gate accepts a bool OR the `(ok, votes, k)` tuple, so existing bool verifiers are unaffected (vote detail stays None).
- `services/perception.py`: opt-in `verify_retries` (default 0 = behavior identical) on `gate`/`perceive_and_fold` — re-runs the FULL k-sample verifier vote up to `1+retries` times, committing as soon as one attempt clears. Each attempt keeps the SAME unanimity bar, so a flaky-but-correct SUM gets more chances without lowering per-attempt precision; an always-failing verifier is never rescued.
- `services/correction_resolver.py`: three new precision-first correction connectives — arrow `X -> Y` (ASCII arrow, `-+>`/`=>`), reverse `to NEW from OLD`, and `NEW replaces/replacing OLD` / `OLD replaced by NEW`. Each requires an explicit connective and is protected by the existing unique-value-match safety net (can only re-value a View that already holds `old`). ASCII-only (no unicode arrow).
- Tests: `tests/test_consumer_quality_pack.py` (26) — new correction phrasings + safety (value-match/no-target abstain), `count_spend_compound` detector + partial receipt (and no-receipt without a compound), `verify_candidate_detailed` votes, `verify_retries` rescues a flaky SUM / never rescues a rejected one / bool verifier still works. All existing perception + correction suites unchanged (101 pass). Full suite: <pending>. Verified against archolith-bench offline phase3 smoke (6 scenarios, gate PASS, invariants clean). Stochastic effectiveness of count-vs-spend co-extraction + verify_retries recovery NOT measured live (no :8099 this session) — characterization pending a live 2x run.

## 2026-07-08 - TurnEvidence Producer Pack v1: Codex producer + shared core + dry-run/health

- Producer-side only pack: the hose now has THREE faucets (Claude, OpenCode, Codex) feeding the SAME `/api/turn-evidence` contract, with **zero server change** and no change to triage semantics, Phase 3, View logic, or any consumer behavior. Full guide: `docs/turn-evidence-producers.md`.
- `scripts/hooks/menhir_turn_evidence_common.py` (new): single source of truth for all producers -- the ONE triage table + `triage_user_prompt`, `prompt_hash`, `git_probe`, `collect_provenance`, `build_payload`, `post_evidence`, `log_failure`, dry-run/health formatters, and a shared `run_cli` (handles `--dry-run`/`--health` and the `MENHIR_TURN_EVIDENCE_ENABLED`/`MENHIR_TURN_EVIDENCE_DRY_RUN` env toggles). `git_fn` is injected so each producer keeps a monkeypatchable module-level `_git` seam.
- `scripts/hooks/menhir_turn_evidence.py` + `menhir_opencode_turn_evidence.py`: refactored to THIN ADAPTERS over the shared core (identity constants + one-line `build_evidence_payload`/`main` delegations; re-export the seams the existing suites reference). **Capture behavior byte-for-byte unchanged** -- both existing suites pass unmodified.
- `scripts/hooks/menhir_codex_turn_evidence.py` (new): third producer, `source_client="codex"`, `source_kind="codex_hook"`, `triage_version="codex-hook-v1"`. Codex exposes a Claude-compatible `UserPromptSubmit` `hooks.json` event, so it registers exactly like the Claude hook (event JSON on stdin); a `normalize()` maps Codex's `user_prompt`/`workspace_root` aliases. `scripts/hooks/codex-hooks.example.json` gives the registration.
- Ergonomics on every producer: `--dry-run` prints `would_capture`/`triage_reasons`/`source_client` and NEVER POSTs; `--health` prints local config (url, api_key_configured yes/no, versions, git_available, cwd) and NEVER POSTs or prints the API key or prompt text.
- Docs: new `docs/turn-evidence-producers.md` (system guide) + `scripts/hooks/README.md` (install/dry-run/health/env for all three clients); OpenCode plugin README + ADR 0001 updated to the three-producer + shared-core model.
- Tests: `tests/test_codex_turn_evidence.py` (junk drop, durable accept, alias normalisation, source_client labels, git/offline/malformed-stdin fail-open) + `tests/test_producer_pack.py` (3-way triage parity behavioral + structural [producers share the SAME triage objects], distinct source_client labels, `--dry-run`/`--health`/`DRY_RUN`-env/`ENABLED`-env never POST, health never leaks the key). `test_turn_evidence.py` + `test_opencode_turn_evidence.py` unchanged and green. Full suite: 2058 passed, 31 skipped.

## 2026-07-08 - Second TurnEvidence producer: OpenCode (same contract, no server change)

- The producer hose widened by exactly one faucet. **OpenCode** now feeds the SAME `/api/turn-evidence` contract as the Claude hook, with **zero server change** and no change to triage, Phase 3, View logic, or any consumer behavior. `source_client`/`source_kind`/`hook_version`/`triage_version` are all client-agnostic on the server, so a second producer needed only client-side files.
- `scripts/hooks/menhir_opencode_turn_evidence.py` (new): stdlib-only Python producer mirroring the Claude hook. Reads a JSON envelope on stdin (`prompt`, `session_id`, `cwd`), runs the SAME deterministic, LLM-free triage, and POSTs only candidates with `source_client="opencode"`, `source_kind="opencode_hook"`, `triage_version="opencode-hook-v1"`, `hook_version="menhir-opencode-turn-evidence-hook-v1"`. Same fail-open posture (Menhir down / git absent / malformed stdin -> exit 0), same prompt-redacting failure log, same git provenance envelope. Assistant/tool turns and transcript mode remain OUT of scope.
- `scripts/opencode-plugin/menhir-turn-evidence.js` (new) + `README.md`: thin OpenCode plugin (WIRING only, like the Claude `.claude/settings.local.json` registration). On every `chat.message` it extracts the user's text parts and pipes a JSON envelope to the Python producer fire-and-forget, swallowing every error so it can never block a chat turn. No triage, no network in JS. Not auto-installed into the live config.
- `scripts/hooks/menhir_turn_evidence.py`: **unchanged** (byte-identical) -- the Claude producer's behavior and identity labels are untouched.
- `tests/test_opencode_turn_evidence.py` (new, 13): junk drop, durable-example accept, `source_client="opencode"` payload + labels, prompt_hash/provenance present, git fail-open, junk-still-dropped-with-provenance, blank/missing prompt, offline non-blocking (log carries `source_client`, never the text), malformed-stdin `main()` fail-open, a Claude-labels-unchanged assertion, and a **cross-client triage parity test** asserting the OpenCode triage is byte-for-byte equivalent to the Claude triage (behavioral + structural: same verdict/reasons per prompt AND identical rule tables/regex patterns). ADR 0001 updated with the second-producer note.

## 2026-07-07 - TurnEvidence provenance metadata (additive, capture scope unchanged)

- Better labels on the evidence envelope WITHOUT widening what gets captured. No triage, Phase 3, or capture-category changes. `src/menhir/api/routes.py` + `turn_evidence_repository.py`: new optional `TurnEvidenceRequest` fields `source_client` + `hook_version`, forwarded through the route and stored as node properties. Omitting them (old payload shape) stores nulls -- no client breaks. `prompt_hash` is derived SERVER-SIDE (`derive_prompt_hash` = sha256 of text) so every node carries a deterministic, client-independent content fingerprint distinct from `turn_key` (which folds source/session/cwd for idempotency). Free-form `metadata` (project_root/git_branch/git_commit/...) stored verbatim. Neo4j is schemaless for properties -> no `_SCHEMA_V` bump, no index change.
- `scripts/hooks/menhir_turn_evidence.py`: hook emits `source_client="claude_code"`, `hook_version`, and a metadata envelope with `project_root` + `git_branch` + `git_commit` (cheap, CLIENT-SIDE, fail-open via a guarded `_git` helper with a 1.5s timeout; a non-repo dir costs one probe, not three) plus `prompt_hash`. Git runs in the hook, NEVER in Menhir, so the server keeps no git dependency; any git failure -> None and capture proceeds. Triage semantics unchanged; junk still short-circuits before provenance is gathered.
- `tests/test_turn_evidence.py` (+6) + `tests/test_api_routes.py` (`TestTurnEvidence` +3): old + new payload shapes accepted and forwarded; provenance stored; `prompt_hash` deterministic + content-sensitive; missing optional metadata non-fatal; hook carries provenance; git fail-open; junk drop unchanged. Full suite 2019 passed, 31 skipped. Validated live against a throwaway Menhir (direct Neo4j read confirmed the properties persisted) + archolith-bench `menhir-phase3` offline + live 2x (invariants clean). Deferred (not cheap for a stateless fail-open hook): `dropped_count_by_session`.

## 2026-07-07 - Phase 3 View-consolidation HTTP surface (archolith-bench black box)

- Narrow black-box endpoints so `archolith-bench menhir-phase3` can exercise the `:TurnEvidence` -> Phase 3 consolidation -> View pipeline over HTTP against a throwaway instance, without importing menhir or issuing Cypher. `src/menhir/api/routes.py`: `POST /api/phase3/run` (agent) runs `consolidate_personal_memory` over one explicit namespace (chat/embed built from settings like the scheduler job) and returns `{phase3_selected, dirty_after, views_written, abstained, corrections_applied, llm_calls, ...}`; `GET /api/phase3/status` (readonly) returns `{dirty, turn_evidence}`; `GET /api/views` (readonly) returns current counter Views each with `history`/`superseded` plus `subject='perception'` abstention receipts, split; `POST /api/phase3/reset` (agent) tears down a throwaway namespace.
- Reset gap fix: `:TurnEvidence` is keyed by `t.namespace` (not `group_id`), so the `group_id`-based `delete_namespace` never removed it. Added `TurnEvidenceRepository.purge_namespace` + `count_namespace` and `memory_graph_adapter` delegates (`purge_turn_evidence`/`count_turn_evidence`); `/api/phase3/reset` = guarded `delete_namespace` (partition + watermark) + `purge_turn_evidence`, leaving zero residue for re-runnable evals.
- `tests/test_api_routes.py`: `TestPhase3` (+7) — run invokes consolidation & reports metrics, 503 without a chat provider, namespace required (422), status dirty+evidence, `/api/views` splits user Views from perception receipts (with superseded/expired history), reset purges partition + TurnEvidence, reset refuses `default` (400). 21 passed.

## 2026-07-07 - Selective TurnEvidence capture (rename + deterministic triage)

- Correction to the same-day Turn-capture MVP: capture is now SELECTIVE, not transcript logging. Renamed `:Turn` -> `:TurnEvidence`, `/api/turns` -> `/api/turn-evidence`; the hook observes every user prompt but stores only prompts that pass deterministic, LLM-free triage. ADR 0001 updated with the Claude-MVP clarification.
- `scripts/hooks/menhir_turn_evidence.py` (renamed): `triage_user_prompt` — regex/substring signals (number, money, date, i_have/i_bought/i_use/preference/cessation/remember/change/decision/correction). Non-candidates ("rewrite this", "continue") are dropped; candidates POST `/api/turn-evidence` with `triage_reason[]` + `triage_version`. No LLM. Non-blocking, silent, prompt-redacting failure log.
- `src/menhir/infrastructure/turn_evidence_repository.py` (renamed): `TurnEvidenceRepository` — `record_turn_evidence` (stores triage_reason/version/prompt_length; MERGE on turn_key), `list_dirty_evidence_namespaces` / `load_user_evidence` (role=user), `evidence_exists`, `evidence_stats` (incl triage_reason/version counts). `:TurnEvidence` is never an `:Entity`/`:Episodic` label, so it can't surface in normal recall.
- `schema.py`: `_turn_evidence_index_queries()` — `:TurnEvidence` unique constraint + indexes. `memory_graph_adapter.py`: prefers user evidence for Phase 3, `record_turn_evidence`/`turn_evidence_stats` delegators. `routes.py`: `POST /api/turn-evidence` with triage fields. `perception_report.py`: "TurnEvidence Capture" section with triage counts.
- `tests/test_turn_evidence.py` (renamed, 18) + report tests: triage drops non-candidates / stores number+preference+cessation+decision, triage is deterministic & LLM-free, hook maps candidate -> payload, offline non-blocking, repository/adapter/report. 57 passed across evidence + canonicalization + consolidation + api + schema suites.

## 2026-07-07 - Turn capture MVP: Claude-first :Turn producer (ADR 0001)

- First real `:Turn` producer, capturing Claude Code **user prompts** as raw evidence so Phase 3 has real user-authored input. Plan: `.agent/plans/turn-capture-claude-hook.md`.
- `src/menhir/infrastructure/turn_repository.py` (new): `TurnRepository` — `record_turn` (MERGE on a `turn_key` idempotency hash; metadata JSON-serialized; role/text required), `list_dirty_turn_namespaces` / `load_user_turns` (filter `role='user' AND declarant='user'`), `turns_exist`, `turn_stats`. `Turn ≠ Episodic`; raw turns never enter recall.
- `src/menhir/infrastructure/schema.py`: `_turn_index_queries()` — `turn_key` unique constraint + namespace/role/recorded_at/session indexes; added to phase-1 bootstrap (not the readiness-gated set).
- `src/menhir/infrastructure/memory_graph_adapter.py`: adapter owns a `TurnRepository`; `list_dirty_namespaces`/`load_user_episodes` PREFER user Turns when any exist, else fall back to the legacy `user:`-prefix Episodic path. New `record_turn` + `turn_stats` delegators.
- `src/menhir/services/scheduler_tasks.py`: consolidation prefix-strip made tolerant (Turn text has no `user:` prefix).
- `src/menhir/api/routes.py`: `POST /api/turns` (agent tier) -> `graph_adapter.record_turn` via `asyncio.to_thread`; 400 on missing role/text.
- `src/menhir/services/perception_report.py`: report adds a Turn Capture section (table_exists, totals by role/source_kind, latest, phase3 user turns) and flips its conclusion when user Turns exist.
- `scripts/hooks/menhir_record_turn.py` (new): stdlib-only Claude `UserPromptSubmit` hook adapter — maps hook JSON to `POST /api/turns`, non-blocking (Menhir down => log + exit 0), silent stdout, failure log redacts the prompt. Opt-in via project-local `.claude/settings.local.json` (not committed global config).
- `tests/test_turn_capture.py` (new, 12) + `tests/test_perception_canonicalization.py` (+1): repository create/idempotency, role/text required, user-only query filters, Phase 3 prefers Turns / falls back, hook mapping + missing-prompt + offline-non-blocking, report turn stats. 33 passed with the report/consolidation suites.

## 2026-07-07 - Phase 3: dedicated consolidation extractor model

- Extractor matrix ({gpt-4.1-nano, gpt-4o-mini} x {k=3, k=5} x 3 scenarios x 3 trials, deterministic gate) showed **gpt-4o-mini @ k=3** recovers both known-good cases 3/3 (derived SUM=125 + stated=25) with 0 garbage; current prod `gpt-4.1-nano` gets 0/3 on the derived case (mis-keys as `grocery_spend=40`) and k=5 makes it worse. Results: `.agent/plans/phase3-extractor-matrix-results.md`.
- `src/menhir/config/settings.py`: new optional `personal_memory_consolidation_chat_model` (env `MENHIR_PERSONAL_MEMORY_CHAT_MODEL`, default empty = global chat model).
- `src/menhir/infrastructure/sync_llm.py`: `make_sync_chat(..., model=...)` overrides only the model name (same provider client), so one job can run on a stronger model without touching Graphiti enrichment.
- `src/menhir/core/runtime.py`: wires the setting into the consolidation job's sync chat. `.env` (untracked) sets `MENHIR_PERSONAL_MEMORY_CHAT_MODEL=gpt-4o-mini`. Live-verified: env -> settings -> override -> real call returns OK.
- `tests/test_consolidate_personal_memory.py`: +2 tests (model override honored + falls back to global; settings reads the env). 9 passed.

## 2026-07-07 - Phase 3 measure-key canonicalization + stated-span guard + debug report

- **Verified finding first:** Phase 3 (`consolidate_personal_memory`) cannot fold Views on real data — the dirty detector needs `Episodic` content starting `user:` and 0/1718 live episodes have it (all `source` values are agent/tool ids; no role/speaker/source_kind fields exist). Even with correctly-shaped user turns, prod `gpt-4.1-nano` k=3 abstained on a clean bike-spend case purely from measure-key scatter. Two-track split agreed: this patch is the narrow, unblocked track (NOT conversational-turn capture, NOT global STATEMENT-only). Plan: `.agent/plans/phase3-canonicalization-guards.md`.
- `src/menhir/services/perception.py`: **measure-key canonicalization before the consistency gate.** `canonicalize_measure_key` (small alias table seeded ONLY from observed scatter — `cycling_*`→`cycling_spend`, watch-list→`watchlist_item_count`; `_number`→`_count` cleanup; idempotent) + `canonicalize_samples` (rewrites each group's measure and merges within-sample collisions by UNION + provenance dedup, so an overlapping sub-measure never double-counts). Applied in `perceive_and_fold` before `gate`; raw→canonical map exposed on `PerceptionResult`. Existing measures (`bike_spend`, `playlists`, `bikes`, `tanks`, ...) map to themselves — no-op for current tests/Views.
- `src/menhir/services/perception.py`: **stated-value span guard** (`VETO_UNSUPPORTED_STATED`, opt-in via `enable_stated_span_guard`). A `reducer="stated"` measure whose numeric value isn't present in a linked source span is quarantined; fold-derived sum/count/distinct are EXEMPT. `consolidate_personal_memory` pins it on; default off keeps existing behavior/tests.
- `src/menhir/services/perception_report.py` (new): `build_phase3_report` / `format_phase3_report` / `probe_capture_metadata` — re-runs extract→canonicalize→gate for observability (raw candidates, raw→canonical collapse, gate accepts/rejects by veto, quarantines, fold-derived preserved) and states outright when user-turn capture metadata is absent.
- `tests/test_perception_canonicalization.py` (new, 11 tests): scatter collapse pre-gate, stated-total recovery under scatter, within-sample union without double-count, iPhone-count-1 not promoted, span guard quarantine + opt-in + grounded-admit, bike-spend SUM=125 still folds end-to-end, guard exempts fold-derived SUM, report flags missing capture metadata + collapse table. Suite: perception + fold + windowed + consolidate = 93 passed.
- Live prod-LLM re-check: canonicalization collapsed the known `cycling_*` names so `cycling_spend` now concentrates to 125 at 2/3 (was full scatter); residual abstention is the model inventing fresh names + strict unanimity threshold — deferred to track-2 (extractor/k/threshold) and the future `Conversation Turn Capture Surface` ADR.

## 2026-07-06 - R2 facet: Phase-4 active wiring PARKED (shadow-only); test/symbol fix

- **Decision: keep FACET shadow-only; Phase-4 active wiring (`enable_facet_candidates`) parked, not implemented.** A three-gate investigation found the reranker's net-new over menhir's existing stack (real-embedding recall + `ScopeWarden` scope discipline) marginal — the seam stays reserved and unwired. Full write-up: `.agent/plans/r2-facet-production-integration.md` (gates a/b/c) + `archolith-bench .agent/benchmark-notes/facet-r2-gate-b-anchor-noise.md`.
  - Gate (a): FACET is a re-ranker, not a generator (generator floods the scope-filtered corpus — redundant with `ScopeWarden`).
  - Gate (b): reranker is robust to real anchor noise (~75% spurious, mean 9 anchors/mem measured on the live-menhir clone) — graduates even at total structural-anchor loss — because the win is scope/belief (metadata), not structural convergence. Anchor quality is not a blocker; it also isn't the value.
  - Gate (c): against a real OpenAI embedding baseline, FACET's net-new recall is ~1 gold / 23 (~4%), degraded further by anchor noise on the real graph.
- `src/menhir/domain/facet_derivation.py` (fix `593fdce`): test-named DEFINES symbols now route to the `test` facet regardless of origin (graph anchors OR prose), so a query's `test=` meets the candidate's `test=` — the "test_* lands in symbol" mismatch that silently broke test-name convergence. No meet-point weight change. `tests/test_facet_derivation.py`: +1 regression test (10 total).
- Observe-only facet stack (Phases 1-3) unchanged and retained: `enable_facet_shadow` stays default-OFF; the live shadow keeps measuring in case a larger/different corpus or a repurposing use-case reopens the question.
- Measurement tooling (read-only, uncommitted-to-hot-path): `scripts/_measure_facet_generator.py`, `scripts/_measure_anchor_quality.py` (menhir-frontier); anchor-noise regime + sweeps in archolith-bench.

## 2026-07-02 - D0 retrieval entropy into menhir (view-reachability probe)

- `src/menhir/services/view_entropy.py` (new): `probe_view_reachability` — for each current View, recall its own canonical surface in-namespace and record the rank + footprint (memories, ~chars/4 tokens) of the greedy rank walk to it. The View registry is the deterministic sufficiency source (the D0 handoff's option (a)): no labels, no LLM. Probes are pure reads (`update_access=False`). Also `walk_to_target` + `estimate_footprint_tokens` (intentionally not tiktoken — regression-metric units must be environment-stable).
- `src/menhir/services/recall_service.py`: `recall(..., update_access=True)` — `False` skips `_post_recall_updates` entirely (no `last_accessed` touches, no edge reinforcement, no rehydration), so a measurement never reinforces the nodes it measures; default path byte-for-byte unchanged. Trace assembly now computes `view_reachability` (first current View in the shipped results; superseded versions never count).
- `src/menhir/domain/retrieval_trace.py`: new frozen `ViewReachability` (uuid, view_kind, 1-based rank, tokens_to_view) + optional `RetrievalTrace.view_reachability`.
- `src/menhir/infrastructure/view_repository.py`: `list_views` rows now also carry `uuid` + `namespace` (group_id) — additive, needed to locate a View in ranked results and probe inside its silo.
- `src/menhir/core/backend_protocol.py` + `core/backend_impl.py` + `api/routes.py`: `view_entropy` op on the protocol, RuntimeProvider (wires recall_service + graph_adapter into the probe), HTTP BackendClient, and the `backend_invoke` whitelist.
- `src/menhir/mcp/tools/ops/view_entropy.py` (new) + `ops/__init__.py`: `view_entropy` MCP tool (readonly tier), registered in `OPS_TOOLS`.
- `tests/test_view_entropy.py` (new, 11 tests): walk/footprint math, unreached censoring, namespace threading, the pure-read contract, `update_access` gate on/off, and trace view-reachability incl. the superseded-View exclusion. Full suite: 1755 passed; the 6 failures are pre-existing on HEAD (verified via stash), none related.
- `.agent/endpoints.md`: documented `mcp.tool.view_entropy`.

## 2026-07-02 - Productionize the View primitives (proven -> always-on)

- WS1 `src/menhir/services/scheduler_tasks.py` + `services/maintenance_scheduler.py`: new `sync_experience_counters` maintenance job (hourly; gated by `experience_counter_enabled`; disabled with the rest of the scheduler under `MENHIR_BENCHMARK_MODE=1`, so benchmark A/B is unaffected) folds telemetry into supersedable experience-counter Views — `failure_events -> '<op>_<err>_failed'`, `memory_revisions -> '<field>_revised'`. Telemetry store sourced from the module `telemetry_store` singleton (no constructor widening); bridges called synchronously like `observe_queue_health`. Registered across the 5 scheduler sites mirroring `retry_failed_enrichments`.
- WS2 `src/menhir/infrastructure/view_embedder.py` (new) + `services/failure_counter_bridge.py` + `services/instability_counter_bridge.py` + `core/runtime.py`: optional `embed=` gives each counter a cosine `name_embedding` on its retrieval surface (`ViewRepository.retrieval_text`, the exact text stored as the node `name`). `make_view_embedder(settings)` builds a synchronous `openai.OpenAI` embedder from the Graphiti embed provider (base_url/api_key/embed_model), lazily; returns `None` when no OpenAI-compatible provider is configured (counters stay BM25-only) and never raises, so a failed embed degrades surfacing but never drops the write. Threaded as `experience_embed` through `runtime._start_scheduler`. (`quantstate_consolidator` already accepts an injectable embed and has no scheduled call site — nothing to wire there.)
- WS3 `src/menhir/infrastructure/schema.py`: `entity_view_key_idx` / `entity_view_kind_idx` / `entity_view_current_idx` via `_view_index_queries()`, added to the phase-1 bootstrap and `PHASE_ONE_REQUIRED_INDEXES` (existing install reports `schema_not_ready` until they build, then green). No `_SCHEMA_V` bump — indexes only, View nodes already carry the props.
- `tests/`: `test_experience_counters_task.py`, `test_view_counter_embedding.py`. `test_milestone_three_contract.py` aligned to the `ScoredMemory.is_superseded_view` field (pre-existing contract drift from `1c1f673`).

## 2026-06-21 - CANDIDATE review tier (harvester intake door for cth.painscan)

- `src/menhir/domain/models.py`: Added `NodeScope.CANDIDATE` (low-trust human-review tier).
- `src/menhir/infrastructure/candidate_repository.py`: New `CandidateRepository` - direct Cypher write (bypasses Graphiti queue, like TEMPORAL), idempotent MERGE on `(source, candidate_cluster_id)`, `scope='CANDIDATE'`, `user_flagged=false`; `create`/`list`/`fetch` plus `promote_candidate` (-> PERSISTENT) and `reject_candidate` (DETACH DELETE), both guarded on `scope='CANDIDATE'`.
- `src/menhir/infrastructure/memory_graph_adapter.py`: Candidate delegate methods.
- `src/menhir/core/backend_protocol.py`, `core/backend_impl.py`: Candidate ops on the protocol, LocalBackend (off-loop) and HTTP BackendClient; `approve_candidate` runs the service path.
- `src/menhir/api/routes.py`: Exposed candidate ops via the `backend_invoke` whitelist (HTTP intake).
- `src/menhir/mcp/tools/ingest/add_candidate.py`: New `add_candidate` MCP tool (registered in `INGEST_TOOLS`).
- `src/menhir/services/candidate_service.py`: New `CandidateService.approve` (promote + contradiction check, best-effort) / `reject`; wired into `BuildArtifacts`/`build_memory_services`.
- `src/menhir/services/recall_service.py`: Recall now excludes `scope == CANDIDATE` unconditionally (load-bearing staged-review guarantee).
- `src/menhir/explorer/app.py` + `templates/_candidates.html` + `templates/index.html` + `static/explorer.js`: Candidate review surface with approve/reject (explorer's first write endpoints).
- `tests/`: `test_candidate_repository.py`, `test_candidate_service.py`, `test_add_candidate_tool.py`, `test_explorer_candidates.py`, and CANDIDATE-exclusion cases in `test_recall_service.py`.
- `.agent/memory-design.md`: Documented the CANDIDATE review tier and its transitions.

## 2026-05-13 - validate recall presets across backend and MCP context builder

- `src/cth_mcp_memory/domain/recall.py`: Added shared preset parsing helpers and a stable `InvalidQueryPresetError` for unsupported recall/context presets.
- `src/cth_mcp_memory/core/backend_impl.py`: Validates presets through the shared parser in `recall()` and `build_context()`, and translates backend `422` invalid-preset responses back into `InvalidQueryPresetError` for client callers.
- `src/cth_mcp_memory/api/routes.py`: Returns `422 Unprocessable Entity` with the preset error detail instead of leaking invalid preset failures as backend `500`s.
- `src/cth_mcp_memory/mcp/tools/recall/build_context.py`: Corrected the documented preset list and added a friendly invalid-preset message path.
- `tests/test_api_routes.py`: Added regression coverage for invalid preset handling on `/api/recall` and `/api/context`.
- `tests/test_backend_roundtrip.py`: Added regression coverage proving invalid `build_context` presets fail before service execution and that the backend client still reuses its owned HTTP client.

## 2026-05-11 - E2E audit remediation (Steps 1, 2, 3-partial, 4, 6, 7, 8)

- `src/cth_mcp_memory/mcp/formatters.py`: Fixed score breakdown serialization — `_bd()` helper handles both dict and dataclass breakdowns; added `relevance` tier labels (high/medium/low) to `_compact_scored_item` (Steps 1+2)
- `src/cth_mcp_memory/services/scoring_service.py`: Added `MIN_SIMILARITY_THRESHOLD = 0.15` confidence floor and `min_similarity` param on `score_candidates()` (Step 2)
- `src/cth_mcp_memory/services/recall_service.py`: `note` field set when all candidates below floor; added recall latency timing instrumentation with per-phase breakdown logging (Steps 2+3)
- `src/cth_mcp_memory/domain/recall.py`: `RecallResult` gained `note: str | None = None` field (Step 2)
- `src/cth_mcp_memory/mcp/tools/recall/recall_memories.py`: Propagates `note` to payload output (Step 2)
- `src/cth_mcp_memory/infrastructure/memory_queries.py`: `flag_memory` structural guard rejects Entity/Episodic/Session nodes; filtered `fetch_flagged_memories`/`fetch_flagged_memory_bootstrap_version`; added `unflag_structural_nodes()` cleanup (Step 4)
- `src/cth_mcp_memory/infrastructure/memory_graph_adapter.py`: Exposes `unflag_structural_nodes()` (Step 4); exposes `close_stale_todos()` (Step 6); added `CorrelationRepository` import, `self._correlation` init, 4 delegate methods (Step 8)
- `src/cth_mcp_memory/mcp/tools/ingest/flag_memory.py`: Catches `ValueError` from structural node rejection (Step 4)
- `src/cth_mcp_memory/infrastructure/todo_repository.py`: `list_todos()` returns `age_days`+`stale`; `close_stale_todos()` method (Step 6)
- `src/cth_mcp_memory/mcp/tools/ops/list_todos.py`: Stale warning count, per-todo markers, age display (Step 6)
- `src/cth_mcp_memory/mcp/tools/ops/close_stale_todos.py`: New MCP tool for bulk closing stale todos (Step 6)
- `src/cth_mcp_memory/mcp/tools/ops/__init__.py`: Registers `CloseStaleTodosTool` (Step 6)
- `src/cth_mcp_memory/mcp/tools/recall/recall_context_memories.py`: Stale todo warning in bootstrap output (Step 6)
- `src/cth_mcp_memory/core/backend_impl.py`: `close_stale_todos` on RuntimeProvider and BackendClient (Step 6)
- `src/cth_mcp_memory/core/backend_protocol.py`: `close_stale_todos` on BackendProtocol (Step 6)
- `src/cth_mcp_memory/api/routes.py`: Registered `close_stale_todos` in backend method allowlist (Step 6)
- `src/cth_mcp_memory/services/lifecycle_service.py`: `confirm_pending_conflicts` accepts `status` param; 3-way routing in `_check_contradictions_batch` (RELATES_TO edge 0.70–0.85, conflict 0.85–0.95, merge ≥0.95); Python 3.12 `similar` variable fix (Steps 7+8)
- `src/cth_mcp_memory/services/scheduler_tasks.py`: New `review_unresolved_conflicts()` weekly task (Step 7)
- `src/cth_mcp_memory/services/maintenance_scheduler.py`: `review_unresolved_conflicts` job with weekly interval (Step 7)
- `src/cth_mcp_memory/services/scheduler_protocols.py`: `status` param on `confirm_pending_conflicts` protocol methods (Step 7)
- `src/cth_mcp_memory/infrastructure/correlation_queries.py`: NEW — `CorrelationRepository` with `create_related_to_edge`, `merge_entity`, `correlation_exists`, `fetch_entity_merge_metadata` (Step 8)
- `src/cth_mcp_memory/services/correlation_service.py`: NEW — `CorrelationService` with `check_correlation`, `check_correlation_batch`, `_route`; constants `CORRELATION_RELATED_THRESHOLD=0.70`, `CORRELATION_CONFLICT_THRESHOLD=0.85`, `CORRELATION_MERGE_THRESHOLD=0.95` (Step 8)
- `src/cth_mcp_memory/services/enrichment_steps.py`: Correlation check block after structural anchoring (Step 8)
- `scripts/profile_recall.py`: NEW — standalone Neo4j profiling script for metadata fetch, adjacency, touch, edge increment benchmarks (Step 3 partial)
- `tests/test_mcp_formatters.py`: 3 new tests (dict breakdown, none breakdown, relevance tiers) (Steps 1+2)
- `tests/test_scoring_service.py`: 3 new tests (threshold filter, customizable threshold, all-below-empty) (Step 2)
- `tests/test_correlation_service.py`: NEW — 33 tests (routing, single-node, batch, repository, thresholds) (Step 8)
- `tests/test_lifecycle_service.py`: 3 new tests (merge routing, related edge, adjusted conflict threshold) (Step 8)

## 2026-05-01 - add flagged param to add_memory

- `src/cth_mcp_memory/mcp/tools/ingest/add_memory.py`: Added `flagged: bool = False` parameter; output includes `flagged=true` note when set.
- `src/cth_mcp_memory/core/backend_protocol.py`: Added `flagged` to `queue_episode()` and `create_temporal()` protocol signatures.
- `src/cth_mcp_memory/core/backend_impl.py`: Threaded `flagged` through `RuntimeProvider.queue_episode()` and `create_temporal()`.
- `src/cth_mcp_memory/services/ingest_service.py`: Added `flagged` to `queue_episode_for_enrichment()`, passes as `user_flagged` to `create_pending_episode()`.
- `src/cth_mcp_memory/infrastructure/episode_lifecycle.py`: `create_pending_episode()` writes `$user_flagged` via Cypher; `claim_pending_episode()` returns `user_flagged` field.
- `src/cth_mcp_memory/infrastructure/episode_stamping.py`: Changed Episodic stamp from `n.user_flagged = false` to `coalesce(n.user_flagged, false)` to preserve pre-set intent.
- `src/cth_mcp_memory/infrastructure/temporal_repository.py`: `create_temporal()` accepts and writes `user_flagged` directly in CREATE Cypher.
- `src/cth_mcp_memory/infrastructure/memory_graph_adapter.py`: Updated delegations for `create_pending_episode()` and `create_temporal()`.
- `src/cth_mcp_memory/infrastructure/cypher.py`: Added `user_flagged` to `EPISODE_RETRY_FIELDS`.
- `src/cth_mcp_memory/services/enrichment_steps.py`: After stamping, calls `flag_memory()` for each extracted Entity node when `user_flagged=true`. Both normal and reconcile paths.
- `src/cth_mcp_memory/services/scheduler_tasks.py`: Same flag propagation in failed-episode reconciliation path.

## 2026-04-12 - improve memory gateway validation errors

- `src/yawn_memory/mcp/gateway.py`: Improved invalid JSON, non-object payload, missing-key, boolean coercion, and unknown-action errors with concrete `payload_json` examples and help-action hints.
- `src/yawn_memory/mcp/gateway.py`: Added structured JSON gateway help and `help:<action>` support for generated clients that need one action schema at a time.
- `tests/test_mcp_gateway.py`: Updated gateway validation assertions for the more actionable error messages.

## 2026-04-11 - harden embedding compatibility checks

- `src/yawn_memory/infrastructure/embedding_dimensions.py`: added helpers to infer expected embedding dimensions and summarize stored Neo4j embedding dimension health
- `src/yawn_memory/core/runtime_preflight.py`: added startup diagnostics for stored graph embeddings whose dimensions do not match the configured Graphiti embedder
- `src/yawn_memory/infrastructure/graphiti_client.py`: configured known OpenAI-compatible embedding dimensions and added a BM25-only fallback when vector search fails on mixed embedding dimensions
- `scripts/repair_embedding_dimensions.py`: added an operator repair script that snapshots, clears, and rebuilds wrong-dimension graph embeddings
- `src/yawn_memory/core/runtime.py`: avoided starting the internal scheduler when Graphiti is not using the scheduler-managed local endpoint

## 2026-04-11 - polish structural query output

- `src/yawn_memory/mcp/tools/recall/query_structure.py`: exposed `symbols` and `context` formatting and included file descriptions, hot counts, and function-level callers in query output
- `src/yawn_memory/infrastructure/structure_queries.py`: made linked-memory ordering aggregate `last_accessed` safely across structural anchors
- `src/yawn_memory/services/scheduler_tasks.py`: ensured the hook project index writer creates its parent directory before writing
- `.agent/verified-current-findings.md`: removed the resolved hook project index finding
