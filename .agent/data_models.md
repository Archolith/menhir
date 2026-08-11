# Data Models Reference

Do not preload this entire file by default. Start with `README.md` and `concept-ids.md`, then open only the
model section you need.

## Quick Index

- Need episode processing fields: read `model.episode`
- Need durable memory node fields: read `model.entity`
- Need TODO / task state fields: read `model.todo`
- Need the general modeling rules before designing a feature: read `model.primitives`
- Need the owned-subordinate modeling pattern: read `model.owned_record`
- Need the declaration-to-identity pattern: read `model.declarative_resolution`
- Need the operational/semantic boundary: read `model.operational_vs_semantic`
- Need the identity/embodiment/locator rule: read `model.embodiment_invariant`
- Need edge / conflict fields: read `model.edge` and `model.conflict`
- Need Python return types: read `model.domain.session` and `model.domain.ingest_result`
- Need env / payload contract: read `model.config`
- Need terminology help first: read `glossary.md`

## Modeling primitives

Concept id: `model.primitives`

Four general rules, extracted from the todo redesign and the artifact model but **not
specific to either**. New features — beacons, orientations, workflows — should be checked
against these before inventing their own vocabulary.

| Primitive | Rule |
|---|---|
| `model.operational_vs_semantic` | Operational objects do not infer semantics; semantic objects describe them |
| `model.owned_record` | A subordinate with no independent semantic identity, destroyed with its owner |
| `model.declarative_resolution` | An author declaration is stored verbatim, then resolved to a durable identity |
| `model.embodiment_invariant` | Identity, embodiment and locator are three things and never collapse |

## Canonical Graph Model

This document tracks the contract for v1 memory graph objects used by policy and scoring jobs.

### Pattern: Declarative Resolution

Concept id: `model.declarative_resolution`

> **An author's declaration is stored verbatim, resolved separately to a durable identity,
> and retained even when resolution fails.**

```
raw declaration  ->  normalization  ->  resolution  ->  durable identity
   (kept)                                (may fail)
```

The wrong shape, which this exists to prevent:

```
raw string  ->  relationship
```

Because then a rename silently deletes the relationship, and a declaration that cannot be
resolved today is simply lost.

Instances:

| Declaration | Resolves to |
|---|---|
| `code_ref` on a `:Todo` | `:TodoLocation` -> structural file `:Entity` |
| artifact frontmatter target (`implements: [oauth-plan]`) | a `:WorkArtifact` uuid |
| artifact `about:` | an existing semantic `:Entity` |

Three properties make it work, all proven in Phase A:

1. **The raw text survives normalization.** Re-migration under changed rules stays possible.
2. **Resolution is per-record.** One declaration may resolve while its sibling does not;
   both are kept.
3. **Failure is a state, not a discard.** `resolution_status='unresolved'` with a reason,
   never a fabricated target. "No matching entity" and "could not understand the
   declaration" are different states and stay distinguishable.

This is what makes renames safe: identity is resolved, not spelled.

### Pattern: Owned Record

Concept id: `model.owned_record`

A recurring modeling primitive, alongside Semantic Object and View. Named because it kept
being re-derived behaviorally; `:TodoLocation` is its first instance, not its definition.

> **An Owned Record has no independent semantic identity, exists only through its owner,
> may resolve external references independently, and is destroyed with its owner.**

Concretely, an Owned Record:

| Property | Why it matters |
|---|---|
| Carries its own label, never `:Entity`/`:Episodic` | It cannot surface in semantic recall — the containment `:TurnEvidence` also uses |
| Holds no namespace of its own | Visibility is inherited through the owner, so there is no second copy to drift |
| Resolves or fails per record | One declaration may normalize cleanly while a sibling stays unresolved; both are retained |
| Retains the raw declaration | The author's original text survives normalization, so re-migration is possible |
| Carries an ordinal | The author's ordering is data, not incidental |
| Is deleted with its owner | It is a component, not a peer |

Instances and candidates:

```
Todo        -> TodoLocation      (implemented)
Artifact    -> ArtifactLocation  (candidate: plans, reviews, reports, handoffs)
Review      -> ReviewFinding     (candidate)
Orientation -> OrientationSection(candidate)
```

An Owned Record is **not** a Semantic Object. That distinction is the point: it is an owned
structural component, so it never competes in recall, never accrues meaning, and never
needs its own lifecycle.

**Addressability rule** — stated explicitly, because the presence of a uuid will otherwise
be read as "this is a semantic object":

> **An Owned Record may be addressable by UUID without becoming a Semantic Object.**

Some Owned Records must be referenced individually — answering "which review answered
question 3?" requires pointing at one `:OpenQuestion`. Giving it a stable uuid is correct
and changes nothing else: it still never surfaces in recall, still carries no meaning
alone, and still dies with its owner.

Being **referenceable** is not the same as having **semantic identity**. A uuid is an
address, not a promotion.

### Invariant: identity, embodiment, locator

Concept id: `model.embodiment_invariant`

Three concepts that must never collapse into one:

```
Identity     what the thing IS          stable; survives everything
Embodiment   a manifestation of it      carries the bytes
Locator      how to reach it right now  mutable
```

Worked through `:WorkArtifact`: the artifact has identity, an `:ArtifactSource` is one
embodiment (markdown, PDF, HTML), and repository/branch/path are merely locators for that
embodiment. Renaming, moving or archiving a file changes the **locator** only — identity
and embodiment are untouched, and every relationship pointing at the artifact survives.

The failure mode this prevents is keying identity on a path. It looks correct until the
first rename, then silently orphans everything that referenced the object.

**A medium-neutral source contract.** Git is one implementation, not the model:

| Leg | Git | Wiki | Filesystem |
|---|---|---|---|
| `medium` | `markdown` | `wiki` | `file` |
| locator | repository, branch, path | page_id | uri |
| version | git_sha | revision_id | mtime |
| integrity | *(git_sha serves both)* | *(often none)* | content hash |

`integrity` is optional and medium-dependent: for a wiki there may be no integrity value at
all, and inventing one to fill the column is worse than leaving it empty.

**Git is the exception to its own row (`ArtifactSource` schema_version 2).** The v1 table above
treated a Git handle as serving both legs. It does not, and collapsing them cost the corpus its
locators. Three separate facts:

| Field | Holds | Answers |
|---|---|---|
| `integrity` (+`integrity_algorithm`) | SHA-256 of the current **raw bytes** | did this file's content change? |
| `version` (+`version_kind`) | `git_blob_oid`, when the file is committed | which committed object is this? |
| `observed_commit` | the commit checked during reconciliation | when did we look? |

Raw bytes are hashed without normalizing line endings or Markdown: a CRLF change is a real source
change even when the rendered prose is identical. A dirty working-tree file has an integrity value
and no blob OID, which is a valid observation rather than an error. `version_kind` exists because
the v1 migration wrote a *commit* SHA into `version` — two forty-character hex strings meaning
different things cannot share a field with no discriminator, so legacy values are relabelled
`legacy_commit_sha` rather than reinterpreted.

**A source's routing lane is not its type or its lifecycle.** `corpus_lane` (`active` | `backlog` |
`reference` | `archive`) is derived from the locator and lives on `:ArtifactSource`, because a source
is the thing that has a locator; a future artifact with a Markdown source and a PDF source in
different collections needs one lane each. A plan moved to reference is still a plan, and a plan
moved to archive has not thereby been implemented, superseded, or deferred — the auditor reports
that contradiction and refuses to resolve it.

**A missing source is unresolved, never deleted.** `resolution_status` / `resolution_reason` record
that a locator could not be found while leaving the locator, the artifact, and every relationship
intact, so the state reverses itself the moment the file reappears. `source_uuid` makes each
embodiment addressable — Owned Record addressability, not semantic identity — and
`current_locator_key` (`repository|medium|path`) is uniquely constrained so two artifacts can never
claim one current path.

A source-less `WorkArtifact` remains a valid semantic identity. When a corpus document explicitly
declares that UUID, reconciliation may attach its first `ArtifactSource` only when the graph and
document types agree and the locator is unclaimed. This is embodiment repair, not registration:
the existing artifact's title, status, declarations, and relationships remain authoritative and
unchanged.

`version` is the embodiment's current revision handle — **one value, never a history**.
History belongs to the versioning system that owns it. Menhir stores semantic identity;
Git stores revisions. Do not duplicate version control.

### Principle: operational vs semantic identity

Concept id: `model.operational_vs_semantic`

Two invariants the todo redesign converged on. They generalize past todos.

> **Operational objects do not infer semantics. Semantic objects explicitly describe
> operational objects.** The semantic graph explains the operational graph, never the
> reverse.

`:Todo` is operational. Memories, decisions, findings and observations are semantic, and
they point *inward* at the todo — a todo never accumulates semantic behavior until it
becomes a second, competing kind of memory node.

> **Prefer explicit, typed relationships over inferred ones unless inference enables a
> capability that cannot reasonably be expressed by explicit author intent.**

This is why `CONCERNS` was removed: once `HAS_LOCATION`, `MENTIONS_TODO`,
`ADDRESSES_TODO`, `RESOLVES_TODO` and `REOPENS_TODO` existed, the one remaining inferred
edge was guessing intent from prose, and answered no question the declared links could not
answer deterministically.

A related boundary, worth keeping distinct: **structural objects explain where something
lives; semantic objects explain what something means or decides.** A file does not
"address" a todo, which is why link eligibility excludes `structure_role` nodes.

### Node: Memory Entity

Concept id: `model.entity`

Each node represents a memory unit stored in Neo4j. Labels: `Entity` or `Episodic`.

| Field | Type | Notes |
|-------|------|-------|
| `id` / `uuid` | string | Primary identifier (stable UUID) |
| `type` | string | `EPISODIC`, `SEMANTIC`, `PROCEDURAL`, `PREFERENCE`, `IDENTITY`, `TEMPORAL`, `SPATIAL` — each type has a `MemoryTypePolicy` in `domain/memory_types.py` that defines its decay thresholds, scoring parameters, and lifecycle rules |
| `scope` | string | `SESSION`, `PERSISTENT`, `PROMOTED` |
| `freshness` | string | `ACTIVE`, `COMPRESSED`, optionally `GONE`; post-v1 adds `STALE`, `ARCHIVED`. Not set on `SESSION` nodes |
| `source` | string | `claude-code`, `discord-bot`, `manual`, `system-inferred`. On a merged node this is the single contributor that carries the authority — the LOWEST-tier label in `sources`, ties broken on contributor order — or the placeholder `merged` when the node has no contributors at all. It is not an append log; the pre-`01a10e4` comma-joined form (`'claude-code,project-scan'`) is legacy data that `source_confidence_for` cannot parse. |
| `sources` | array[string] | Ordered, de-duplicated contributor labels. Written by entity merge (`domain/merge_delta.derive_merged_provenance`): survivor contributors keep their positions, the absorbed node's new ones append. Absent on nodes written before the property existed — readers fall back to splitting the legacy comma-joined `source`, but an explicitly EMPTY list means "no contributors" and must NOT fall back. The placeholder `merged` is never a member. Projected by `MEMORY_RETURN_FIELDS` and read by BOTH halves of structural recognition (`structural_memory.legacy_structural_memory_cypher` and `infer_legacy_structure_role`): a merged legacy structure row's `source` no longer says `project-scan`, so a reader consulting only `source` would surface it in recall as an ordinary memory. |
| `corroboration` | int | How many INDEPENDENT writers assert this node: distinct source FAMILIES in `sources`, not distinct labels (`claude-code` and `claude-chat` are one writer; see `utils.source_family`). Deliberately separate from `source_confidence` — authority is who said it, corroboration is how much independent observation agrees, and neither is a function of merge count. |
| `source_confidence` | float | Trust level: user-confirmed (1.0) > structural/project-scan (0.9) > agent-reviewed (0.7) > LLM-inferred (0.5). These tiers are formalised as `ReviewState` (HUMAN_REVIEWED / AGENT_REVIEWED / UNREVIEWED) in `domain/truth/`; use `review_state_from_confidence()` to convert. Constants: `SOURCE_CONFIDENCE_USER` (1.0), `SOURCE_CONFIDENCE_STRUCTURAL` (0.9, HUMAN_REVIEWED threshold), `SOURCE_CONFIDENCE_AGENT_REVIEWED` (0.7, AGENT_REVIEWED threshold), `SOURCE_CONFIDENCE_AGENT` (0.5, default fallback). **This is the node's EFFECTIVE authority, which the label tier only bounds.** `source_confidence_for(label)` returns a *nominal ceiling*; a writer may deliberately stamp lower (`structure_queries` writes an inferred import target as `source='project-scan'` at agent tier, not the structural tier the label permits). So `source_confidence <= source_confidence_for(source)` always holds, but equality does not — never "repair" a node by recomputing this from its label. Merge takes `min(effective authority of each input, ceiling of the merged contributor set)`, so a merge can never raise trust and never discards an explicit downgrade (`domain/utils.effective_authority`). The valid domain is `0.0 .. SOURCE_CONFIDENCE_USER` inclusive; a stored value outside it is corruption rather than a downgrade and is answered by the label ceiling, the same path a missing or non-numeric value takes — it is never propagated. |
| `user_flagged` | boolean | Explicit v1 user override for retention/promotion review |
| `bootstrap_scope` | string or null | Startup injection selector, independent of retention: `general`, `workspace:<normalized-key>`, or null for retention-only. Structural nodes must remain null. |
| `created_at` | timestamp | Creation time |
| `last_accessed` | timestamp | Used by recency bonus |
| `sharpness` | float | Cached relevance/protection score |
| `content` | string | Primary memory text |
| `original_content` | string | Retained for future revision/recovery |
| `user_id` | string | User/group context key |
| `session_id` | string | Active session correlation |
| `edge_count` | int | Cached incoming edge count proxy for prominence (synced by `sync_edge_counts()`) |
| `promoted_at` | timestamp | Set when a SESSION node is promoted to PERSISTENT; null for nodes that were always PERSISTENT |
| `rehydration_count` | int | Counter for compress→rehydrate cycles; nodes exempt from further compression after 3 cycles |
| `emotions` | array[object] | Optional v1 emotion metadata list |
| `merged_from` | array[string] | UUIDs this survivor has absorbed via merge. Cleared subtractively on unmerge (only the reversed absorption is removed). |
| `merge_audit` | array[string] | One JSON blob per absorption (absorbed node's pre-merge snapshot, relationships, episode provenance). Matched by the `"absorbed_uuid"` field, never a bare substring — see the merge/delete lifecycle section. |
| `last_merge_op_id` | string | Op id of the most recent journaled merge into this survivor. Idempotency breadcrumb for saga replay; NOT a correctness gate. |
| `ttl_expires` | datetime | Set once (coalesced) when a SESSION node is demoted; the TTL sweep deletes it after this passes. Cleared by promotion. |
| `restored_from_merge` / `restored_by_op` / `restored_at` | string / string / datetime | Stamped on a node recreated by an unmerge (survivor uuid, unmerge op id, time). |

### Node: Episode

Concept id: `model.episode`

Episode nodes are provenance anchors created by each ingestion call. Label: `Episodic`.

| Field | Type | Notes |
|-------|------|-------|
| `uuid` | string | Stable identifier for the episode anchor |
| `session_id` | string | Session that produced the ingest |
| `user_id` | string | Owner of the ingest |
| `source` | string | Origin of the episode text |
| `scope` | string | Usually `SESSION` at ingest time |
| `source_confidence` | float | Provenance trust level |
| `user_flagged` | boolean | Defaults to `false` |
| `bootstrap_scope` | string or null | Requested startup selector carried through enrichment only when `user_flagged=true`; null means retention-only. |
| `created_at` | timestamp | When the episode node was written |
| `content` | string | Original episode text |
| `processing_state` | string | `PENDING`, `ENRICHING`, `READY`, `FAILED` |
| `processing_stage` | string | Coarse stage label (`queued`, `graphiti_extracting`, `stamping`, `rehydrating`, `finalizing`, etc.) |
| `processing_substage` | string | Finer-grained live marker inside a coarse stage (`lease_acquired`, `awaiting_graphiti_response`, `llm_activity_observed`, `graphiti_response_received`, etc.) |
| `processing_substage_started_at` | timestamp | When the current substage was first observed |
| `processing_progress` | float | Coarse percent progress (0.0-100.0) |
| `processing_steps_total` | int | Total coarse steps planned for the enrichment pipeline |
| `processing_steps_completed` | int | Completed coarse steps so far |
| `processing_llm_tasks_attempt` | int | LLM endpoint calls consumed by the current enrichment attempt |
| `processing_llm_tasks_total` | int | LLM endpoint calls consumed across all attempts for this episode |
| `processing_llm_last_task_at` | timestamp | Timestamp of the most recent LLM task call for this episode |
| `processing_llm_active_task` | string | Current logical LLM task label attached by the service layer |
| `processing_llm_active_kind` | string | Current LLM activity kind (`chat`, `embedding`, `response`) when known |
| `processing_llm_active_model` | string | Most recent active LLM model observed for this episode |
| `processing_llm_active_endpoint` | string | Most recent active OpenAI-compatible endpoint observed for this episode |
| `processing_heartbeat_at` | timestamp | Last worker heartbeat while the episode is being processed |
| `processing_attempts` | int | Number of claim attempts made by enrichment workers |
| `processing_owner` | string | Worker identity holding the current enrichment lease (`null` when not owned) |
| `processing_lease_expires_at` | timestamp | Lease expiry timestamp for the current enrichment claim |
| `processing_error` | string | Last terminal error when state is `FAILED` |
| `processing_started_at` | timestamp | When the episode claim transitioned to ENRICHING |
| `processing_completed_at` | timestamp | When enrichment finished (READY or FAILED) |
| `queued_at` | timestamp | When the episode was initially created as PENDING |
| `enrichment_priority` | string | Priority tier for processing (defaults to `P1`) |
| `enriched_nodes_touched` | int | Count of entity nodes created/modified during enrichment |
| `enriched_edges_touched` | int | Count of edges created/modified during enrichment |
| `resolved_episode_uuid` | string | Canonical Graphiti episode UUID after successful enrichment (set on every READY episode, not only merges) |
| `diff` | string or null | Optional git diff attached at ingest time; appended to episode body during enrichment so Graphiti can reason about code changes |

Episode nodes are included in graph traversal and provenance queries but excluded from default memory recall/ranking.

### Node: Structural Entity

Structural graph records are also stored as `:Entity` nodes, but they are deterministic scan artifacts rather than Graphiti-extracted semantic memories.

Common distinguishing fields:

| Field | Type | Notes |
|-------|------|-------|
| `structure_project` | string | Project name owning the structural node |
| `structure_path` | string | Repo-relative path or synthetic structural path such as `.` / `dep:<name>` / `endpoint:<name>` |
| `structure_role` | string | `project`, `directory`, `file`, `entrypoint`, `config`, `test`, `dependency`, `endpoint`, `document` |
| `stack` | string | Present on project nodes |
| `root_path` | string | Present on project nodes; also present on `document` nodes (stores the absolute file path) |
| `scan_fingerprint` | string | Present on project nodes for skip detection |
| `file_mtime` | float | Unix mtime at last scan time; present on file/entrypoint/config/test nodes. Used for incremental diff — only files whose mtime changed are rewritten on re-scan |
| `hot_count` | int | Change frequency counter; incremented each time the file appears in `changed_paths` during an incremental write. Surfaces as `[hot:N]` in `query_structure("files")` when non-zero |
| `symbols_truncated` | bool | Set to `true` on file nodes when the 200-symbol-per-file cap was reached during AST extraction |

Structural entities use the same `Entity` label and several shared fields (`uuid`, `scope`, `source`, `created_at`, `last_accessed`) but are not treated as normal semantic recall results.

Legacy compatibility: a small pre-`structure_role` corpus still exists in production. A row is
treated as structural on bootstrap read paths when its source contains `project-scan` and its
trimmed content begins with the deterministic `Directory:`, `File:`, or `Project:` scan shape.
Project-scan rows with ordinary semantic content remain eligible for recall. The one-time
`scripts/migrate_flagged_bootstrap_scope.py` cutover assigns those rows their canonical role and
clears invalid flag/bootstrap state in the same reviewed manifest used to classify semantic
bootstrap pins. It fingerprints but never writes `namespace` or `group_id`.

### Node: Todo

Concept id: `model.todo`

TODO nodes are direct Neo4j work items and bypass Graphiti enrichment entirely.

| Field | Type | Notes |
|-------|------|-------|
| `uuid` | string | Stable identifier |
| `content` | string | Operator-authored task text |
| `code_ref` | string or null | Optional file path and optional line suffix, e.g. `src/foo.py:42` |
| `priority` | string | `low`, `normal`, `high` |
| `status` | string | `open` or `closed` |
| `source` | string | Usually caller/tool provenance |
| `created_at` | timestamp | Creation time |
| `closed_at` | timestamp or null | Set when closed |
| `namespace` | string | **Never null.** Silo the TODO belongs to; defaults to `default`. "Unscoped" is not representable — see below |

TODO nodes are durable operator state, not lifecycle-managed memories. They do not decay, compress, or participate in semantic scoring.

**Namespace is a storage invariant** (2026-08-02). `create_todo` always persists a non-null
`namespace`, resolved as explicit argument -> `x-menhir-namespace` header -> deprecated
`x-yawn-namespace` alias -> `'default'`. It is
*not* a required argument: omitting it yields the shared `default` silo, mirroring how
`_resolve_namespace` treats memories, so no existing caller breaks.

Reads are opt-in. `list_todos` / `get_todo` without a `namespace` behave as before and span
every silo. Supplying one narrows to `namespace IN [requested, 'default']` — the requested silo
plus the shared bucket — so a client pinned via `MENHIR_CLIENT_NAMESPACES` sees shared todos
rather than nothing. `get_todo` is a direct uuid lookup, so it enforces the filter only when one
is given and always reports the TODO's namespace.

`group_id` is empty string on pre-2026-08-02 nodes and is vestigial — not written, not read.

**Hook injection**: open TODOs (up to 5, sorted by priority then age) are injected into every hook bootstrap output under `### TODOs (N open)`. Agents see them automatically without calling `list_todos`.

**TODO vs memory decision guide**:
- Use `add_todo` when: the item is an explicit task with open/closed lifecycle, optionally tied to a file location, and needs to survive until deliberately closed.
- Use `add_memory` when: the item is a fact, decision, preference, or observation — something to recall and reason about rather than track and close.
- Rule of thumb: if you would write it on a task board → TODO. If you would write it in a notebook → memory.

**Multi-repo scoping**: always pass `structure_project` when creating a TODO with a `code_ref` in a multi-repo workspace. Without it, `REFERENCES_FILE` edge linking uses suffix-only path matching which can bind to the wrong project's file.

### Node: TurnEvidence

Concept id: `model.turn_evidence`

Selective raw conversation evidence (ADR 0001). Label: `TurnEvidence` — a distinct label, **never**
`:Entity` or `:Episodic`, so raw turns cannot surface in normal recall. Written by a host lifecycle
producer (the Claude Code `UserPromptSubmit` hook) after deterministic, LLM-free triage: only prompts
that look like durable memory evidence (a number, money, a possession/preference/decision/correction)
are stored; boring prompts are dropped and never reach Menhir.

| Field | Type | Notes |
|-------|------|-------|
| `turn_id` | string | Generated uuid |
| `turn_key` | string | Idempotency merge key (unique constraint) = `sha256(source_kind + session_id + text + cwd)`; a double-fired hook merges onto one node |
| `role` | string | `user` \| `assistant` \| `tool` \| `agent`. Only `user` is consumed by Phase 3 in the MVP |
| `declarant` | string | Captured at write time, never inferred from prose (defaults to `role`) |
| `text` | string | The raw prompt span (truncated to 8000 chars) |
| `namespace` | string | Silo; from the producer (hook infers it from cwd basename) |
| `session_id` | string | Conversation/session id |
| `source_kind` | string | e.g. `claude_code_hook` |
| `source_id` | string | session id or transcript path |
| `cwd`, `transcript_path` | string | Producer provenance |
| `triage_reason` | list<string> | Which deterministic signals fired (`number`, `i_have`, `decision`, ...) |
| `triage_version` | string | Triage ruleset version, e.g. `claude-hook-v1` |
| `prompt_length` | int | Character length of the captured prompt |
| `occurred_at` | timestamp or null | Optional source/world time supplied by replay and import producers. Live hooks omit it. The fold uses this as `valid_at` when present |
| `recorded_at` | timestamp | Server receive time. Always present and used for dirty discovery and consolidation cursor ordering; it is the `valid_at` fallback when `occurred_at` is absent |
| `metadata` | string | JSON-serialized producer metadata (Neo4j can't store nested maps) |

Written via `POST /api/turn-evidence` (agent tier) -> `MemoryGraphAdapter.record_turn_evidence` ->
`TurnEvidenceRepository` (`infrastructure/turn_evidence_repository.py`). Phase 3 consumption:
`list_dirty_evidence_namespaces` / `load_user_evidence` (both filter `role='user' AND
declarant='user'`); the graph adapter prefers `:TurnEvidence` over the legacy `user:`-prefix
`Episodic` path whenever any user evidence exists. Schema: `turn_evidence_key_unique` constraint +
namespace/role/recorded_at/session indexes (`schema._turn_evidence_index_queries`). Source time and
receive time are deliberately separate: replaying old evidence must preserve historical ordering
without moving the monotonic processing cursor backwards.

### Nodes: TypedAssertion and ScalarStateView

Concept id: `model.scalar_authority`

`TypedAssertion` is the immutable scalar evidence log. Each assertion identifies a subject,
namespace, slot, normalized value, validity time, and provenance foundation. A future-dated write
sets `activation_pending=true`; present-time authority folds exclude it until the scheduler claims
it at or after `valid_at`. `ScalarStateView` is a disposable materialized projection of that log,
not an independent source of truth.

`ScalarHistoryView` is a second projection kind (`view_kind="scalar_history"`) for the same slot.
It preserves every delta/absolute/correction/expiry assertion in source-time order without computing
an absolute current value. Key prefix is `sh_` (vs `ss_` for state). The View payload is a JSON
array of `{assertion_id, operation, value, valid_at, stated_span}` entries stored as `view_payload`,
with `HISTORY_ENTRY` edges to contributing assertions. History Views are `lww_register=False`
(append-only). Feature flag: `MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED` (default off).
Advisory invariant: history never enters the scalar authority lane, never suppresses raw evidence,
never computes an absolute total from deltas, and never treats the latest delta as an absolute
current value.

`CompositionalScalarIdentity` is a non-persisted shadow sidecar over a grounded
`TypedScalarProposal`; it is not a Neo4j node, durable assertion identity, slot key, or View schema.
It separates a small closed relation type from an open, source-grounded target/scope and carries
typed value/unit/operation/effective-time semantics plus provenance. `semantic_key` hashes semantic
fields only; `claim_key` additionally binds the source locator. Structural composition can abstain
with a versioned receipt. Consolidation shadow schema v2 compares these sidecars in memory and emits
only hashes plus closed diagnostic enums in its new compositional section; the existing raw shadow
summary remains intact for backward comparison.

### TypedEventAssertion node, EventLane value object, and event timeline projection

The Event History Phase 1–5 substrate: an immutable, evidence-grounded record of categorical
occurrences (`TypedEventAssertion`), a fold/selection scope (`EventLane`), and a disposable timeline
projection, wired as a **default-off production-capable** path at `370eff1`. Every event settings/flag
defaults off and flag-off behavior is byte-compatible; scalar assertion/state/history/authority and
wire contracts are unchanged. There is no dedicated event endpoint and no default enablement. Labels:
`TypedEventAssertion` (one occurrence version) under a `TypedEventAssertionHead` (the binding-stable
source claim).

Identity is three-level, mirroring the scalar `TypedAssertion`:

- `source_key` — BINDING-STABLE locator: `episode_uuid + span_start + span_end + claim_ordinal`
  (reuses `build_source_key`). Contains no extracted semantics and no `subject_uuid`, so a newer
  perceiver can correct predicate/object on the same source span and still supersede via the same
  head, and a merge re-bind does not fork it.
- `assertion_key` — fully-interpreted identity: `source_key + perceiver_version + namespace +
  predicate + object_key + domain + valid_at + time_basis`. Exact replay of the same interpreted
  occurrence yields the same key (dedup); a corrected world time on the same span is a NEW assertion.
  Excludes `learned_at` and `subject_uuid`/`object_uuid` so UUID rebinding does not fork identity.
- `lane` (`EventLane`) — fold/selection scope: `(namespace, subject_uuid, predicate, domain)`
  normalized. `select_event_assertion` filters strictly to exactly one lane.

Canonical assertion fields: `subject_uuid`/`subject_display` (resolved identity + display surface),
`predicate` (canonical), `object_key`/`object_display`/`object_uuid` (resolved object),
`valid_at` (world/source time; the ONLY ordering/selection authority), `learned_at` (ingest time;
audit only — it never orders distinct occurrences, resolves an authority tie, or falls back for an
invalid `valid_at`; inside an already-proven exact replay group the selector may use it only to
choose a deterministic representative), `stated_span` + `span_start`/`span_end`/`claim_ordinal`
(source grounding), `episode_uuid`/`turn_evidence_uuid` (provenance), `time_basis` (one of
`TIME_BASES`), `evidence_tier` (one of `EVIDENCE_TIERS`), `perceiver_version`, and `metadata`.

Provenance/graph-edge contracts:

- `:TypedEventAssertionHead` is DB-unique on `source_key`; a claim durably bound to subject A that a
  record re-presents as subject B fails closed with `binding_mismatch` and never moves `CURRENT`.
- `TypedEventAssertion` nodes are DB-unique on `assertion_key`/`assertion_id`. Supersession is
  strict-rank (`perceiver_rank`); a higher-version assertion replaces `CURRENT`, lower/same-version
  disagreement stays non-current audit. Exact replay is idempotent and may monotonically upgrade the
  evidence tier, never downgrade.
- `HAS_VERSION` (head → assertion), `CURRENT` (head → current occurrence), `SUPERSEDES`
  (newer → superseded prior), `GROUNDS`/`FOUNDS` (episodic/turn evidence → assertion),
  `HAS_EVENT_ASSERTION` (subject entity → assertion), `EVENT_OBJECT` (assertion → object entity).
- Raw source stamps are retained verbatim (`valid_at_raw`/`learned_at_raw`) with a nullable parsed
  Neo4j temporal on the canonical props; an unparseable source stamp stays durable for audit but
  cannot win a selection or enter the View.

Temporal / selection rules:

- `valid_at` is the only ordering/selection time. `learned_at` and input order never order distinct
  occurrences and never break an authority tie; inside an already-proven exact replay group the
  selector may use `learned_at` only to choose a deterministic representative.
- `select_event_assertion` supports `LATEST` (greatest eligible `valid_at` at/before optional
  `as_of`) and `PREDECESSOR` (greatest `valid_at` strictly before a required anchor). Exact replay
  dedups deterministically; distinct candidates tied at the winning world time fail closed as
  `AMBIGUOUS`; malformed/missing time can never win. Event siblings are occurrences and never
  supersede merely because one is newer.

Timeline projection (Phase 2): the existing `TimelineKind` (`kind="timeline"`) now has two modes
sharing the same kind and node shape. The legacy subject-only mode (no `predicate`) keeps the exact
`{when, what, episode_uuid}` payload, key `timeline`, surface, and parse shape. The EVENT-LANE mode
(`predicate` nonblank, optional `domain`) stores the fixed query-sufficient event-entry schema under
a collision-safe `timeline:event:` lane discriminator; each View carries `view_predicate`/`view_domain`
lane stamps and exact `EVENT_HISTORY_ENTRY` contributor edges with `ordinal`, redrawn atomically by
`draw_event_timeline_entries`. The projection is disposable/rebuildable; the durable
`TypedEventAssertion` + evidence is the source of truth. `EventHistoryService.rebuild_lane(s)` rebuilds
exactly one lane and reports `complete` only after a successful View write, exact edge proof, and
exact-lane reconciliation. The rebuild orders entries by world time then `assertion_key` and uses a
`learned_at`-free replay representative.

Phase 3 (perception + admission): `event_history_perception.py` is the generic, offline LLM
extraction/admission seam — a single completed-acquisition predicate registry (`acquired`) with exact
quote/unique-span grounding and completed-vs-intent/hypothetical/negation discrimination. LLM output
is perception only; ordering, folding, and selection remain deterministic. `event_consolidation.py`
backfills grounded occurrences from canonical user `:TurnEvidence` into durable assertions and rebuilds
affected lanes, advancing an **independent** `:EventConsolidationWatermark` cursor keyed by namespace
in `group_id` (never disturbing the scalar/counter cursors) under a fail-closed page spine that emits
bounded, generic metrics. Only self-subject events are admitted (canonicalized to `user`; the LLM's
free-text domain is ignored this lane to avoid fragmentation).

Phase 4 (recall + authority): `event_history_recall.py` classifies a conservative first-person
latest/predecessor `did I` query into an `EventQueryRoute` and selects a deterministic candidate
(`select_event_recall`); `event_history_authority.py` produces a structured `EventAuthorityVerdict`
(`status` `leads`/`advisory`) only when the route, scope, uniqueness, evidence, and foundation gates
pass. When `event_history_authority_enabled` (default off) and a namespace are present, recall reads
assertions via `event_assertions_for_subject_predicate` and attaches `RecallResult.event_authority_layer` —
a separate structured verdict never interleaved with or reranked among observations, and never changing
the scalar verdict contract.

Phase 5 (transport + lifecycle closeout): the event authority layer is carried through REST `/api/recall`
(`event_authority_layer`), the MCP `recall_memories` tool, the `ContextBuilderService` context block,
and the backend round-trip. The scheduled personal-memory job and manual `POST /api/phase3/run` drive
event consolidation when enabled and return bounded Phase-3 event metrics. Namespace cleanup is
event-aware: `delete_namespace_with_scalar_cascade` deletes the namespace-keyed event log and the
independent `:EventConsolidationWatermark`, and preserves a shared `:TypedEventAssertionHead` that
still `HAS_VERSION` to a surviving assertion in another namespace (shared-head safety; its deleted
CURRENT is repaired by a later idempotent write).

### Node: ScalarProjectionRepair

Concept id: `model.scalar_projection_repair`

| Field | Type | Notes |
|-------|------|-------|
| `repair_key` | string | Unique idempotency key for one operation/subject/namespace repair |
| `operation_id` | string | Correlates all partitions affected by one destructive or activation operation |
| `subject_key` | string | Scalar subject whose projection must be rebuilt |
| `namespace` | string | Authority partition; repairs never cross namespaces |
| `operation_kind` | string | `MEMORY_DELETE`, `NAMESPACE_DELETE`, or `TIME_ACTIVATION` |
| `status` | string | `pending` until rebuild succeeds, then `complete` |
| `started_at`, `completed_at` | datetime | Receipt lifecycle timestamps |

Memory/episode/namespace deletion creates the receipt in the same Neo4j transaction as assertion
removal. The scheduler rebuilds the affected projection at one concrete `as_of` time and only then
marks the receipt complete. A process crash therefore leaves pending repair work instead of a
silently stale view.

### Recall scalar authority payload

When view authority is enabled, `RecallResult.authority_layer` is separate from ranked observation
results. It contains current, expired, or as-of verdicts with slot, normalized value, `valid_at`,
view UUID, foundation, and status (`leads` or `advisory`). Inline contributors are bounded and carry
their graph relation (`CURRENT_ANCHOR`, `CONTRIBUTED_TO`, `SUPERSEDED_ANCHOR`, or `EXPIRY_INPUT`),
with totals, truncation, and `next_offset`. The HTTP expansion endpoint provides explicit paging.
When the feature is off, the field remains absent from wire output for compatibility.

When event-history authority is enabled (default off), `RecallResult` also carries a separate
`event_authority_layer`: a structured `EventAuthorityVerdict` (`status` `leads`/`advisory`, plus
predicate, selected object, `valid_at`, exact `stated_span` quote, assertion/episode/turn-evidence
identity, `gate`, and `reason`) for a conservative first-person latest/predecessor `did I` query. It
is independent of the scalar authority layer, never interleaved with or reranked among ranked
observations, and never changes scalar verdict or wire contracts; when off it is absent from output.

When `scalar_history_enabled` is true, the recall pipeline runs a dedicated advisory lane between
observation and authority. For slots with history data, it injects a `SCALAR_HISTORY` candidate
(`similarity=0.85`, `is_scalar_authority=False`) and adds a `ScalarAuthorityVerdict(kind="history",
status="advisory")` with `HISTORY_ENTRY` contributor IDs. The history lane activates for
`PREVIOUS_VALUE`/`COMPARISON` query intents; for `CURRENT_STATE` queries it activates only when no
`scalar_state` View exists (bounded support for unanchored slots). When `scalar_history_enabled` is
false, stored `scalar_history` Views are excluded during metadata filtering (generic exclusion).

### Edge: Relation

Concept id: `model.edge`

Edges connect memory nodes and carry behavioral metadata.

| Field | Type | Notes |
|-------|------|-------|
| `type` | string | `belongs-to`, `led-to`, `caused-by`, `related-to`, `contradicts`, etc. |
| `weight` | float | Traversal/persistence strength (starts 1.0, capped 5.0) |
| `created_at` | timestamp | Creation time |
| `last_traversed` | timestamp | Used for decay |
| `source` | string | `llm-inferred`, `user-stated`, `system-derived` |
| `scope` | string | `SESSION` for newly created ingest edges, can differ per relation |

Additional edge families now in active use:

| Edge | Notes |
|------|-------|
| `ANCHORED_TO` | Links semantic entities to structural entities for file-aware recall and code-memory fusion; excluded from lifecycle promotion/bridging logic |
| `CONTAINS` / `DEPENDS_ON` / `TESTS` / `IMPORTS` / `EXPOSES` / `CALLS` | Deterministic structural graph edges written by `ingest_project` |
| `REFERENCES_FILE` | Links `:Todo` to a structural file entity |
| `CREATED_FROM` | Links `:Todo` to the episodic memory that motivated it |
| `HAS_LOCATION` | Links `:Todo` to its owned `:TodoLocation` records (normalized `code_ref`) |
| `MENTIONS_TODO` / `ADDRESSES_TODO` | A durable semantic entity references a `:Todo` |
| `RESOLVES_TODO` / `REOPENS_TODO` | Lifecycle evidence; created only by `resolve_todo`/`reopen_todo` |
| `HAS_VERSION` / `CURRENT` / `SUPERSEDES` | Event-history head → assertion versioning; `SUPERSEDES` is drawn from a newer to a superseded occurrence |
| `GROUNDS` / `FOUNDS` | Source evidence (episodic / turn evidence) grounding a `:TypedEventAssertion` |
| `HAS_EVENT_ASSERTION` | Subject entity → `:TypedEventAssertion` |
| `EVENT_OBJECT` | `:TypedEventAssertion` → resolved object entity |
| `EVENT_HISTORY_ENTRY` | Event-lane timeline View → contributing `:TypedEventAssertion` nodes, carrying `ordinal` (see the event-history section above) |

### Event history namespace watermark

The event-history backfill runs on an **independent** cursor node `:EventConsolidationWatermark`
(unique on `group_id`, keyed by the namespace string), deliberately distinct from
`:ScalarConsolidationWatermark`/`:ConsolidationWatermark` so event consolidation never disturbs the
scalar/counter cursors. It advances by the monotonic `recorded_at`/`turn_id` (`cursor_at`/`cursor_uuid`,
stamping `perceiver_version`); world time (`valid_at` = `occurred_at` or `recorded_at`) never moves the
cursor. Only `role=user`/`declarant=user` nonempty `:TurnEvidence` participates. A cursor stamped by a
different perceiver_version is ignored (reset), so bumping it revisits history. A page advances the
cursor only when the whole page succeeded (fail-closed).

### Conflict Tracking

Concept id: `model.conflict`

| Field | Type | Notes |
|-------|------|-------|
| `conflict_group_id` | string | Groups mutually conflicting node pairs |
| `conflict_status` | string | `pending_llm_review`, `unresolved`, `false_positive`, `resolved`, `auto-resolved` |
| `conflict_created_at` | timestamp | First conflict detection time |

## Recall Lab experiment history

Concept id: `model.recall_lab_runs`

Recall Lab persists each completed query to the telemetry sidecar's `recall_lab_runs` table. The
stored `result_json` is the same privacy-filtered payload displayed to the operator; unredacted text
used only inside a blinded judge prompt is not persisted when the Explorer is in hidden mode.

| Column | Notes |
|--------|-------|
| `id` / `recorded_at` | Monotonic run id and UTC creation time. |
| `query` / `preset` / `namespace` | Shared experiment inputs for history filtering and display. |
| `judge_*` / `winner_id` / `tied_ids_json` | Blinded LLM verdict summary. |
| `arms_json` | Compact arm labels, health, hit counts, and candidate counts. |
| `request_json` | Exact arm configuration and shared controls. |
| `result_json` | Complete displayed result and judgment payload for reopening the run. |

## Provider token usage telemetry

Concept id: `model.llm_usage_events`

Every instrumented provider-client call writes at most one terminal row to the telemetry
sidecar's `llm_usage_events` table. `call_id` is generated immediately before the provider call and
makes terminal persistence idempotent. A failed invocation is retained even though it has no token
counts. A completed invocation whose provider omitted usage is retained with null counts instead of
being estimated.

| Column | Notes |
|--------|-------|
| `call_id` | Primary-key correlation id shared by the in-process start and terminal events. |
| `recorded_at` / `duration_ms` | UTC terminal timestamp and observed provider-call latency. |
| `run_id` / `episode_uuid` | Optional benchmark-run and ingest-episode provenance. |
| `operation` / `kind` / `model` / `endpoint` | Logical caller plus physical provider surface. |
| `status` / `error` | `completed` or `failed`; failures preserve the provider error text. |
| `input_tokens` / `output_tokens` / `total_tokens` | Exact normalized provider-reported counts. |
| `cached_input_tokens` / `reasoning_output_tokens` | Exact optional usage details when supplied. |
| `provider_usage_json` | Raw provider usage payload for audit and future normalization. |

`McpTelemetryStore.fetch_llm_usage_summary(run_id=...)` reports totals, missing-usage calls, and
model/endpoint groups. Cost is deliberately not stored because it depends on an external pricing
schedule, not on the immutable invocation evidence.

## Merge / delete lifecycle (recoverable saga)

Concept id: `model.graph_operations`

SQLite and Neo4j cannot share a transaction, so every destructive graph mutation (merge, unmerge,
delete) runs as a recoverable saga journaled in the telemetry sidecar. The missing
durable-before-delete record is exactly why ~24 nodes destroyed by the degree-zero orphan cleanup on
2026-07-12 were unrecoverable. See `.agent/plans/menhir-merge-delete-lifecycle-remediation-2026-07-13.md`.

### Table: `graph_operations` (telemetry sidecar SQLite)

The durable record of intent for every graph mutation. The row is the source of intent; the graph is
the source of truth for "done". `op_id` and `request_json` are immutable once PREPARED.

| Column | Notes |
|--------|-------|
| `op_id` | Primary key. |
| `operation_kind` | Closed enum: `METRIC_WRITE`, `METRIC_MIGRATE`, `METRIC_REVERSE`, `ENTITY_MERGE`, `ENTITY_UNMERGE`, `LEGACY_ENTITY_UNMERGE`, `ENTITY_DELETE`, `SESSION_TTL_DELETE`. |
| `target_uuid` / `target_key` | `target_key` is the order-independent pair key for merge/unmerge; the partial unique index fences it while `PREPARED`/`NEEDS_REVIEW`. Deletes pass `target_key=NULL`. |
| `before_snapshot_json` | The complete lossless recovery snapshot, committed BEFORE any destruction (invariant 3). |
| `expected_after_sha256` | Postcondition fingerprint frozen at PREPARE; only an exact match may COMMIT (invariant 5). |
| `state` | `PREPARED` → `COMMITTED` \| `NEEDS_REVIEW` \| `FAILED` \| `REVERSED`. `FAILED` = terminal, no graph mutation occurred (releases the fence). `NEEDS_REVIEW` = drift, operator-only escape. |

### Lossless snapshot envelope (`domain/merge_snapshot.py`)

`{schema_version, checksum, body}`; unsupported version or bad checksum fails closed. Node bodies
capture ALL labels, `properties(n)` with Neo4j temporal/spatial types preserved (never stringified),
and EVERY incident relationship instance (parallel edges distinct, relationship properties intact,
non-Entity peers included). A snapshot over `MAX_SNAPSHOT_BYTES` (8 MiB) abstains — never truncated.

### Merge-owned survivor delta (`domain/merge_delta.py`)

An exact unmerge reverses what the merge wrote to the SURVIVOR, so
`MERGE_OWNED_SURVIVOR_PROPERTIES` must list every property the merge's SET clause touches:
`summary`, `content`, `source`, `source_confidence`, `sources`, `corroboration`. Three surfaces have
to agree on that set or exact unmerge silently stops working:

* `merge_entity` writes them (provenance via the single `derive_merged_provenance`);
* `fetch_survivor_properties` reads them back — a property it omits reads as `None`, never matches
  the replay, and Guard 2 refuses the unmerge as `SURVIVOR_CHANGED_SINCE_MERGE` on a graph that is
  in fact intact;
* `replay_survivor_merge` recomputes them from the pre-merge snapshot for that comparison.

A merge's Phase 1 read and Phase 2 write are separate Neo4j statements, so every provenance value
Phase 1 reads is re-checked in the Phase 2 `WHERE` (`GUARDED_PROVENANCE`) before the derived values
land. The comparison is exact and null-safe — never collapsed onto a sentinel, which would make
absent indistinguishable from sentinel-valued and would leave a malformed stored value permanently
unmergeable. `absorbed.corroboration` is guarded too: it is not a derivation input, but it is
captured into the audit entry, and a stale entry would describe a state the node never held.

The degraded reader (`domain/legacy_snapshot.LEGACY_PROPERTY_ALLOWLIST`) must list every property the
audit entry can carry, `sources` and `corroboration` included — it also serves NEW entries whenever
the journal row is gone (tier `GRAPH_SNAPSHOT_ONLY`), and an unlisted field is dropped at restore
time with the data in hand.

**No formula versioning, deliberately.** Merges recorded 2026-07-12..28 were produced by the earlier
rule (`source` comma-appended, `source_confidence += 0.1`), so the replay yields a different value
and Guard 2 refuses them. That window is unrecoverable-by-unmerge, not corrupt. Do not loosen the
guard to make an old snapshot pass.

### Recovery tiers (`services/merge_recoverability.py`)

| Tier | Meaning |
|------|---------|
| `EXACT` | Journaled `ENTITY_MERGE` with a complete snapshot → fully reversible via `UnmergeCoordinator`. |
| `LEGACY_SIDECAR` | Lossy pre-journal snapshot in the sidecar → partial, manifest-gated `LEGACY_ENTITY_UNMERGE`; never exact. |
| `GRAPH_SNAPSHOT_ONLY` | Snapshot lives only on `survivor.merge_audit` → wasting asset (dies with the survivor); run `backfill_merge_audit.py`. |
| `LINEAGE_ONLY` | `merged_from` records the absorption but no snapshot exists anywhere → NOT recoverable. |
| `MALFORMED` | A snapshot exists but cannot parse. |

## Domain Types (Python)

### `MemorySession`

Concept id: `model.domain.session`

Immutable session context carried through ingest operations.

| Field | Type | Notes |
|-------|------|-------|
| `session_id` | string | UUID, auto-generated or test-injected |
| `user_id` | string | Required |
| `started_at` | datetime | UTC timestamp |

Factory: `new_session(user_id, *, session_id=None, started_at=None)`

### `IngestResult`

Concept id: `model.domain.ingest_result`

Lean v1 ingestion result returned by `IngestService.ingest_episode()`.

| Field | Type | Notes |
|-------|------|-------|
| `episode_id` | string | UUID of the persisted Episode anchor |
| `status` | `IngestStatus` | `INGESTED`, `SKIPPED`, or `FAILED` |
| `nodes_touched` | int | Count of nodes stamped with policy metadata |
| `edges_touched` | int | Count of edges stamped with policy metadata |

## API / Integration Payload Contracts

### Configuration Contract

Concept id: `model.config`

All runtime settings are loaded from `.env` and passed to runtime clients:

```
NEO4J_URI
NEO4J_USER
NEO4J_PASSWORD
LOCAL_LLM_BASE_URL (legacy alias: LLAMA_BASE_URL)
LOCAL_LLM_API_KEY (legacy alias: LLAMA_API_KEY)
LOCAL_LLM_CHAT_MODEL (legacy alias: LLAMA_CHAT_MODEL)
LOCAL_LLM_EMBED_MODEL (legacy alias: LLAMA_EMBED_MODEL)
OPENAI_API_KEY
OPENAI_CHAT_MODEL
OPENAI_EMBED_MODEL
SCHEDULER_URL (optional, defaults to http://localhost:8082 for scheduler-backed llama acquisition)
GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS (optional rough preflight limit for episode text; set 0 to disable)
```

Note: there is no `OPENAI_BASE_URL` -- it is not read anywhere in the codebase (SSOT-07).

### Validated Test Paths

Concept id: `model.tests`

| Command | Scope |
|---------|-------|
| `pytest -m unit tests -q` | All unit tests (no external deps) |
| `pytest -m online tests/test_phase_one_bootstrap_live.py` | Live schema bootstrap against Neo4j |
| `pytest -m online tests/test_ingest_live.py` | Live Graphiti ingestion + policy stamping |

## Relationship to Design Docs

- Canonical domain behavior is defined in `memory-design.md`.
- Milestone-level acceptance and sequencing is in `memory-roadmap.md`.
