# WorkArtifact Corpus Reconciliation

Status: **PHASES 0–4 IMPLEMENTED — phases 5–6 remain separately owner-gated.**

Date: 2026-08-11

Implementation record:

- Merged to `main` in [PR #6](https://github.com/Archolith/menhir/pull/6), commit `93ce119`.
- PR #7's first hosted run found and drove repair of two integration omissions: feature-taxonomy
  classification for both new MCP tools, and retirement of the superseded WorkArtifact UUID plain
  index before creation of the uniqueness constraint.
- Offline verification after repair: 5,926 passed, 197 skipped; the sole failure is the pre-existing
  worktree-name assertion that accepts only directories named `menhir` or `menhir-frontier`. The 12
  focused taxonomy/schema tests pass.
- Throwaway-Neo4j acceptance after repair: all 21 phase-one bootstrap and reconciliation live tests
  passed, including a second idempotent bootstrap. The complete graph-backed CI selection also
  passed: 162 passed, 21 expected service-dependent skips.
- No production graph repair or corpus-wide legacy frontmatter mutation has run. Those remain the
  Phase 5 and Phase 6 approval gates below.

Extends:

- `.agent/plans/menhir-artifact-semantic-model.md`
- `model.embodiment_invariant` in `.agent/data_models.md`
- Hook Center file events in `docs/hook-center-tool-events.md`

## Decision

File state and semantic state need different authorities.

- The filesystem and Git determine that a document exists, changed bytes, moved, was copied, or can
  no longer be found.
- Menhir owns `WorkArtifact` identity, lifecycle, declared relationships, and provenance.
- Deterministic detectors may update observable source facts. They must not infer implementation,
  supersession, deferral, or semantic relationships from a path or from prose.
- MCP remains the explicit correction and semantic-mutation surface. Routine file moves do not
  require an MCP call.

This plan adds a read-only corpus auditor first, then a bounded reconciliation path for source
locators and hashes. The one-time graph repair is a separate, reviewable operation after the audit
ledger is approved.

## Current failure, measured

The 2026-08-11 corpus pass exposed a gap between the implemented identity model and its operating
path:

- The live graph has 54 Menhir `WorkArtifact` nodes and 54 `ArtifactSource` embodiments.
- It has 29 `artifact_type='plan'` nodes.
- With this plan added, the current plan corpus has 28 non-index Markdown records: 13 top-level
  records and 15 backlog records.
- Only 4 of those 28 records have an exact current locator in the graph; 24 are absent.
- Across all 54 Menhir artifacts, 25 source locators no longer resolve in the repository.
- Git recognizes 38 renames in commit `f441a23`; 13 of their old paths exactly identify existing
  graph sources and can be relocated without guessing.
- The Menhir artifact population currently has no artifact-to-artifact edges, declarations, open
  questions, `ABOUT` edges, or todo links. That lowers the immediate repair blast radius but does
  not justify replacing nodes or changing UUIDs.
- Of the 28 current plan records, 22 contain a `Status:`-shaped line but only 3 map to the typed plan
  lifecycle. Location repair cannot repair lifecycle truth.

The implementation gaps are concrete:

1. `WorkArtifactRepository.relocate_source` exists and preserves artifact identity, but it is not
   available through the adapter/backend/MCP chain and is not called by file-event handling.
2. Hook Center already sends `path`, `old_path`, `operation`, `after_hash`, and Git provenance for a
   rename, but `ToolEventRepository` only marks structural file entities dirty.
3. The hook recognizes named file tools such as `move` and `rename`. It cannot cover every shell,
   Git, IDE, `apply_patch`, branch-switch, or external-editor change.
4. `scripts/migrate_work_artifacts.py` scans only one directory level, creates missing nodes by
   locator, and never reconciles existing sources. Rerunning it would miss backlog records and can
   create duplicate identities after a move.
5. Migration writes the last commit touching a path into `ArtifactSource.version`. A commit SHA is
   provenance for a repository state, not the file's content hash. Git blob identity, observed
   commit, and raw-byte integrity must be stored separately.

## Required outcome

After this work:

1. One read-only command reports graph/filesystem parity for every configured artifact corpus.
2. A move preserves `artifact_uuid`, the existing `ArtifactSource`, and every relationship.
3. A same-path edit refreshes source integrity and validation state without minting a new artifact.
4. A new file in a typed corpus directory is either registered once or reported as unclassifiable.
5. A missing source is visible as unresolved; no detector hard-deletes an artifact.
6. Archive, backlog, and reference routing are queryable without parsing locator strings at read
   time.
7. Lifecycle and semantic relationships change only from explicit declarations or existing MCP
   operations.
8. The reconciler is idempotent. Re-running it against unchanged files and graph state produces no
   actions.

## Invariants

### Identity survives location changes

`WorkArtifact.artifact_uuid` is stable. `ArtifactSource` is the embodiment; its locator is mutable.
A relocation updates one source record in place. Delete-and-reimport is not a repair strategy.

### A hash is evidence, not identity

Use SHA-256 of raw file bytes as integrity evidence. Do not normalize line endings or Markdown
before hashing: a byte change is a real source change even when rendered prose looks equivalent.

Equal hashes do not prove equal artifact identity. Templates, copies, and duplicated records can be
byte-identical. Hash-based relocation is allowed only when the old source is absent and exactly one
unclaimed destination has that hash. Multiple candidates are a conflict.

### Git and filesystem evidence remain distinct

For a Git-backed source, record:

```text
integrity_algorithm  sha256
integrity            SHA-256 of current raw bytes
version_kind         git_blob_oid
version              blob OID for the observed Git version, when available
observed_commit      commit checked during reconciliation
size_bytes           current byte count
last_seen_at         latest successful observation
```

`schema_version=2` distinguishes this contract from sources whose `version` currently contains a
commit SHA. Migration computes fresh values; it does not reinterpret one 40-character value as
another.

Dirty working-tree content can have a raw-byte SHA-256 with no matching committed blob. That is a
valid observation, not an error. Modification time may be retained as a scan hint but never decides
identity or freshness.

### Corpus lane is not artifact type or lifecycle

Add a path-derived `corpus_lane` to the canonical source:

```text
active | backlog | reference | archive
```

Examples:

- `.agent/plans/x.md` -> `active`
- `.agent/plans/backlog/x.md` -> `backlog`
- `.agent/reference/x.md` -> `reference`
- `.agent/archive/plans/x.md` -> `archive`

A plan moved to reference remains historically a plan. Its routing lane changes; its type does not.
A move into archive also does not prove whether the plan was implemented, superseded, or deferred.
The auditor reports lane/lifecycle contradictions but does not resolve them.

`corpus_lane` belongs on `ArtifactSource`, not `WorkArtifact`, because a source is what has a
locator. A future multi-embodiment artifact may have a Markdown source and a PDF source in different
collections. A current-plan query asks whether the artifact has a canonical source in an executable
lane.

### Detectors fail closed on ambiguity

No title matching, prose similarity, or LLM classification participates in automatic identity
resolution. A detector may return `CONFLICT` or `UNRESOLVED`; it may not choose the nearest-looking
file.

### Observation cannot silently change semantics

The following remain explicit:

- lifecycle transitions through `transition_artifact`;
- replacement through `supersede_artifact` or a resolved `supersedes:` declaration;
- `reviews`, `implements`, `informs`, `about`, and todo relationships;
- retyping an artifact;
- hard deletion.

Structured frontmatter may be transcribed because it is authored intent. Running prose is never a
semantic mutation source.

## Reconciliation model

Add `src/menhir/domain/artifact_reconciliation.py` with pure values and classification:

```text
CorpusEntry
ArtifactSourceSnapshot
GitRename
ReconciliationAction
ReconciliationReport

ActionKind =
    NOOP
  | REFRESH_SOURCE
  | RELOCATE_SOURCE
  | REGISTER_ARTIFACT
  | MARK_SOURCE_UNRESOLVED
  | CONFLICT

MatchBasis =
    DECLARED_UUID
  | EXACT_LOCATOR
  | GIT_RENAME
  | UNIQUE_CONTENT_SHA256
  | NONE

ConflictKind =
    UUID_LOCATOR_DISAGREEMENT
  | DESTINATION_ALREADY_CLAIMED
  | DUPLICATE_DECLARED_UUID
  | DUPLICATE_CURRENT_LOCATOR
  | AMBIGUOUS_CONTENT_MATCH
  | AMBIGUOUS_GIT_RENAME
  | UNCLASSIFIED_NEW_SOURCE
  | INVALID_DECLARED_METADATA
```

These are enums and reasons, not confidence scores.

### Match order

For each discovered corpus entry:

1. Parse and validate a declared `artifact_uuid`, if present.
   - If it identifies one artifact and the destination is not claimed by another source, refresh or
     relocate that source.
   - If that artifact's source has a null or empty repository, assign the audited repository only
     when the document declares the same UUID. A path or hash match alone cannot establish
     repository ownership and remains a conflict.
   - If UUID and current locator identify different artifacts, emit a conflict.
2. Apply an explicit rename pair from a Hook Center event or Git diff.
   - The old locator must identify exactly one source.
   - The destination must not be claimed by another source unless that source has
     its own unambiguous rename in the same batch.
   - Hash equality is not required because a rename and edit can occur in one commit.
   - Contradictory rename evidence is a conflict and reserves every implicated
     entry and source from weaker match passes.
3. Match exact `(repository, medium, locator_path)` only when Git history does not
   say another source moved to that path.
   - Same hash: `NOOP` except observation timestamps/provenance.
   - Different hash: `REFRESH_SOURCE`; identity is unchanged.
4. If the old source is missing, try a unique raw-byte SHA-256 match among unclaimed new entries.
   - The old path must no longer exist.
   - Exactly one destination may match.
   - If the old path still exists, treat the destination as a copy/new source, not a move.
5. If no rule identifies an existing artifact, classify the entry for registration from a configured
   corpus route. If its type cannot be declared deterministically, report
   `UNCLASSIFIED_NEW_SOURCE`.
6. Graph sources not observed and not matched by a rename become `MARK_SOURCE_UNRESOLVED`. They are
   retained with a reason and last successful observation.

Titles may be included in conflict reports for humans. They never select a match.

### Registration routes

Replace the migration script's one-level `DIR_TYPES` scan with recursive, explicit route rules:

| Path | Type rule | Lane |
|---|---|---|
| `.agent/plans/*.md` | `plan` | `active` |
| `.agent/plans/backlog/*.md` | `plan` | `backlog` |
| `.agent/reviews/*.md` | `review` | `active` |
| `.agent/handoffs/*.md` | `handoff` | `active` |
| `.agent/for-review/*.md` | `implementation_report` | `active` |
| `.agent/archive/plans/*.md` | `plan` for a new record; preserve existing type | `archive` |
| `.agent/archive/reviews/*.md` | `review` for a new record; preserve existing type | `archive` |
| `.agent/archive/handoffs/*.md` | `handoff` for a new record; preserve existing type | `archive` |
| `.agent/reference/*` | preserve an existing type; require declared type for new records | `reference` |

Index READMEs are routing documents, not work artifacts, and remain excluded. Hidden files and
temporary editor files remain excluded. PDF is a supported medium and is hashed, but a new reference
PDF without declared type is reported rather than guessed.

Route configuration belongs in a pure, testable table. Do not spread path-prefix checks through the
repository, API route, and CLI.

## Operating model

### Read-only corpus audit

Add:

```text
menhir artifacts audit --repo <path> --repository <name> [--from-commit <sha>] [--json]
```

The command reads files, Git metadata, and graph state. It emits the proposed actions, conflicts,
counts by lane/type/status, and a deterministic `plan_digest`. It performs zero graph writes.

The digest covers:

- repository identity, observed commit, persisted reconciliation cursor, and the selected Git
  evidence base;
- every source locator and stored hash read from the graph;
- every discovered path, size, hash, declared UUID/type/status, and lane;
- every proposed action in stable order.

Tests must prove that audit mode leaves the graph unchanged.

Add a read-only `audit_artifact_corpus` MCP tool only after the CLI report contract is stable. MCP
returns the summary plus bounded conflicts; large ledgers stay in CLI JSON output.

### Apply mode

Add:

```text
menhir artifacts reconcile --repo <path> --repository <name> --apply --plan-digest <digest>
```

Dry-run remains the default. Apply re-scans files and graph state, recomputes the plan, and refuses
if the digest changed. This prevents an approved audit from being applied after another move or graph
write changed its premises.

Safe apply handles only:

- exact source refresh;
- declared-UUID relocation;
- first-source attachment when a declared UUID identifies an existing source-less artifact of the
  same type;
- exact old/new rename relocation;
- unique-hash relocation under the strict rules above;
- registration under an unambiguous typed route;
- unresolved-source marking.

Conflicts produce no mutation for the affected source. One conflict does not block unrelated safe
actions, but the result must list applied, skipped, and conflicted actions separately.

Registration and attachment are distinct ledger actions. `REGISTER_ARTIFACT` creates semantic
identity plus its first embodiment. `ATTACH_SOURCE` creates only the first `ArtifactSource` and
`EMBODIED_IN` edge for a globally existing UUID; it never rewrites the artifact title, lifecycle,
type, or relationships. Audit bulk-reads declared UUID identities and binds their type, status, and
source count into the plan digest. Type disagreement, an already-present source outside the scoped
source inventory, or an occupied destination is a conflict. Apply locks and rechecks the artifact,
so a source added after audit becomes a skipped conditional write rather than a duplicate.

Legacy sources with a null, empty, or whitespace-only `locator_repository` are read as a separate,
bounded inventory: sources at a current corpus path or owned by a UUID declared in the corpus.
`ADOPT_SOURCE_REPOSITORY` is safe only for the declared-UUID case and updates the existing source in
place. Every weaker match is `UNSCOPED_SOURCE_REPOSITORY`; it reserves the path so registration,
attachment, and relocation cannot create a second embodiment there. The manual
`menhir artifacts adopt-repository` command is the explicit escape hatch for legacy documents that
cannot declare a UUID.

### Immediate rename detector

Extend `/api/tool-events` handling after structural dirty marking:

- On `rename`, look up the old artifact locator scoped by repository/project.
- If exactly one source matches and the destination is unclaimed, relocate it and store the supplied
  `after_hash`, Git commit, and `corpus_lane`.
- On `edit` or `write`, refresh the hash only when the path already identifies one artifact source.
- On `create`, do not register from the event alone. The event contains no document metadata and must
  continue uploading no file content.
- Artifact reconciliation failure must not roll back or suppress structural dirty marking. Return an
  optional reconciliation outcome and log the conflict.

This closes the low-latency path for supported tools. It is not the coverage backstop.

### Git/startup recovery detector

Hook coverage will never be complete. Add a repository-local recovery pass that compares the last
reconciled commit with the current repository state using Git rename detection (`--name-status -M`)
and then runs the full corpus audit.

Persist one `ArtifactReconciliationCursor` per repository in the graph. Audit reads it without
writing and uses it as the Git evidence base unless an operator supplies `--from-commit`; that flag
overrides only the evidence range and does not replace the stored cursor. Apply re-reads the cursor
before any mutation and refuses a stale plan if it changed after audit. It advances the cursor to
the observed full commit only after a run with no conflicts, no skipped writes, and an available
observed commit. The compare-and-set update prevents concurrent reconcilers from silently moving the
same cursor. Both the stored cursor and selected evidence base are visible in the ledger and bound
into its digest.

Runtime mode is configured explicitly:

```text
MENHIR_ARTIFACT_RECONCILE_MODE=off | audit | safe_apply
MENHIR_ARTIFACT_RECONCILE_REPOSITORY=<graph repository name>
```

Default to `audit`. Startup reports drift but does not mutate the graph. `safe_apply` is an operator
choice after the one-time repair and fixture suite pass. A post-commit hook may run the same command;
it is an accelerator, not the only detector.

Repository identity is always explicit. A worktree basename is not a stable graph key. Apply refuses
to register a corpus when the named repository has zero sources unless an operator supplies
`--allow-new-repository`; startup `safe_apply` never supplies that override.

A missing cursor falls back to full audit. If Git cannot compare the stored cursor to the current
checkout, the read-only ledger is marked `evidence_base_valid: false` and apply refuses before any
artifact write. The operator must inspect the branch relationship and provide a valid
`--from-commit`; an empty rename list is never allowed to masquerade as successful Git evidence.

### Manual MCP escape hatch

Add an agent-tier targeted tool after repository support ships:

```text
relocate_artifact_source(
    artifact_uuid,
    old_path,
    new_path,
    expected_old_integrity="",
    observed_integrity="",
)
```

It uses the same collision checks as the reconciler. This is for resolving an audited ambiguity or
repairing a move when Git evidence is unavailable. It is not the required path for routine moves.

Bulk apply remains an operator CLI operation in v1; do not expose a broad graph-mutating MCP tool
until the digest gate and live repair have been exercised.

## Storage and repository changes

### ArtifactSource v2

Backfill a stable `source_uuid` on every existing source. This is addressability for an Owned Record,
not semantic identity.

Add:

```text
source_uuid
corpus_lane
integrity_algorithm
integrity
version_kind
version
observed_commit
size_bytes
resolution_status       resolved | unresolved
resolution_reason
last_seen_at
last_reconciled_at
last_reconcile_basis
last_reconcile_run_id
schema_version          2
```

Add uniqueness constraints for `WorkArtifact.artifact_uuid` and `ArtifactSource.source_uuid`.
Introduce a normalized current-locator key and constrain it to one source so two artifacts cannot
claim the same `(repository, medium, path)`. The relocation transaction checks the destination before
changing the key; a collision returns a structured conflict.

### Repository methods

Add or tighten:

```text
list_artifact_source_snapshots(repository=None)
list_unscoped_artifact_source_snapshots(paths, artifact_uuids)
refresh_artifact_source(source_uuid, observation, expected_integrity=None)
relocate_artifact_source(source_uuid, old_locator, new_locator, observation)
relocate_artifact_source_by_locator(repository, medium, old_path, new_path, observation)
mark_artifact_source_unresolved(source_uuid, reason, observed_commit=None)
register_work_artifact(entry)
```

The current `relocate_source(artifact_uuid, medium, locator)` updates every source of the same medium
owned by an artifact. Keep it for compatibility during migration, but route new work through the
source-specific method.

Every write is conditional on the state read by the audit action: expected source UUID, old locator,
and expected integrity where available. A stale action is refused instead of overwriting newer state.

### Backend and API

Thread read-only audit summaries and targeted relocation through:

- `MemoryGraphAdapter`;
- `MemoryBackend`;
- `RuntimeProvider`;
- `BackendClient`;
- internal backend allowlist and tier map;
- MCP tools.

Targeted relocation is agent-tier. Audit is read-only. Bulk reconciliation remains outside the
internal backend dispatch in v1 because it also needs local filesystem and Git access.

## Artifact authoring contract and `.agent` instructions

Reconciliation cannot stay correct if agents create files under informal, conflicting conventions.
Phase 0 must define the authoring contract and route every agent to it before parser or write
behavior is treated as stable.

### Canonical authoring guide

Add `.agent/workflows/artifact_authoring.md` as the one instruction surface for creating, copying,
moving, archiving, restoring, and reclassifying work artifacts. It must cover:

1. Which directory owns each corpus lane and artifact type.
2. The required metadata block and allowed lifecycle values per type.
3. How to generate a UUID for a new artifact.
4. The difference between a move, a copy, and a replacement.
5. Which fields are authored and which are detector-derived.
6. Which index must be updated for each destination.
7. The validation command to run before committing.
8. When an MCP call is required and when detectors handle the change.

The guide must state the minimum metadata for artifacts created after reconciliation support is
enabled:

```yaml
---
artifact_schema: 1
artifact_uuid: 00000000-0000-0000-0000-000000000000
artifact_type: plan
artifact_status: PROPOSED
---
```

Rules:

- `artifact_uuid` is UUIDv4, minted once when the document is created.
- A move or restore keeps the UUID.
- A copy intended to become a separate artifact gets a new UUID before it is committed.
- `artifact_type` uses the WorkArtifact vocabulary. A directory route and declared type must agree.
- `artifact_status` uses the canonical lifecycle for that type. Commentary belongs outside the
  value.
- Authors never declare `corpus_lane`, source hash, blob OID, commit, source UUID, resolution state,
  or reconciliation basis. Menhir derives those from the source and locator.
- Relationship keys such as `implements`, `reviews`, `informs`, `supersedes`, `about`, and `todos`
  remain optional explicit declarations.
- An archive move requires an explicit terminal lifecycle decision in the same reviewable change.
  The path does not choose between `IMPLEMENTED`, `SUPERSEDED`, and `DEFERRED`.
- A move to `reference/` must state why the artifact remains useful and remove executable ownership
  from the plan indexes. It does not silently retype the artifact.

Existing artifacts are grandfathered into audit as unresolved metadata until the approved backfill.
The reconciler must not reject or rewrite them merely because they predate this contract. New
artifacts created after activation are required to comply.

### `.agent` routing updates

Update these instruction surfaces when the contract lands:

| File | Required change |
|---|---|
| `.agent/README.md` | Add "creating or moving a plan/review/handoff/reference" to Quick Start and route it to `workflows/artifact_authoring.md`. |
| `.agent/file-index.md` | Index the authoring guide as the canonical artifact-creation contract. |
| `.agent/workflows/feature_planning.md` | Replace the generic "add a dedicated doc under `.agent/`" advice with the corpus lanes, required metadata, and a link to the authoring guide. |
| `.agent/maintenance.md` | Require artifact validation and the correct routing-index update whenever a tracked artifact is created, copied, moved, archived, or restored. |
| `.agent/plans/README.md` | Link the authoring guide from the maintenance rule and state that active plans require compliant metadata after activation. |
| `.agent/plans/backlog/README.md` | Apply the same rule to backlog plans; promotion to top-level keeps UUID and type. |
| `.agent/reference/README.md` | Document reference-lane metadata, preserved UUIDs on moves, and the required usefulness/consumer note. |

Do not duplicate the full schema in every index. The indexes state the local routing rule and link to
the canonical guide; `artifact_authoring.md` owns field definitions and examples.

### Validator and examples

Add a local validator used by both authors and the corpus auditor:

```text
menhir artifacts validate <path-or-repo> [--json]
```

It checks metadata syntax, UUID shape, type/status compatibility, route/type agreement, duplicate
UUIDs in the scanned corpus, H1 title presence, and required index membership. It performs no graph
writes.

The authoring guide includes copyable examples for plan, review, handoff, implementation report, and
reference-lane records. Fixtures used by validator tests are the executable examples; do not create
a second template set with a different schema.

Documentation acceptance:

- A fresh agent starting from `.agent/README.md` can find the guide without searching.
- Following one example produces a file accepted by `menhir artifacts validate`.
- The instructions make clear that routine moves do not require MCP, while status, supersession,
  retyping, and ambiguous identity resolution do.
- A move preserves UUID; a copy with the same UUID fails validation.
- Every corpus index links to the same canonical field definitions.

## Locked implementation order

### Phase 0 — pure scanner and planner

Add the reconciliation domain values, route table, raw-byte SHA-256 calculation, Git evidence
adapter, pure match planner, metadata validator, and canonical `.agent` authoring instructions. No
graph writes.

Acceptance:

- Recursive scan finds top-level plans and backlog records.
- Markdown and PDF hashes are stable over repeated reads.
- CRLF/LF differences produce different raw-byte hashes.
- A dirty working-tree file records integrity without claiming a committed blob.
- The full match matrix is covered offline.
- New artifact examples in `artifact_authoring.md` pass the validator; malformed UUID, type/status,
  route/type, and duplicate-copy fixtures fail with exact reasons.

### Phase 1 — read-only graph audit

Add source-snapshot reads and the `menhir artifacts audit` CLI with JSON output and plan digest.
Retire `migrate_work_artifacts.py` as an apply mechanism; keep a compatibility wrapper or make it
delegate to audit/reconcile so there is one corpus collector.

Acceptance:

- The command reports the measured 4 exact / 24 missing current-plan split before repair.
- Commit `f441a23` yields the 13 graph-backed deterministic relocations already observed.
- Audit mode produces no graph diff.
- Two identical audits over unchanged state produce byte-identical action ordering and digest.

**Owner gate:** review the complete audit ledger before Phase 2 graph mutations or the one-time repair.

### Phase 2 — source-v2 schema and conditional writes

Add source UUIDs, hashes, Git fields, lane, resolution state, uniqueness constraints, and precise
repository mutations. Backfill source UUIDs first, then create constraints.

Acceptance:

- Relocation updates one source in place and preserves the owning artifact UUID and relationships.
- A claimed destination is refused.
- A changed expected hash is refused as stale input.
- Missing source marking is reversible when the source reappears.
- Migration can be rerun without changing UUIDs or duplicating sources.

### Phase 3 — digest-gated apply and targeted MCP

Add `menhir artifacts reconcile --repository <name> --apply --plan-digest`, the read-only audit MCP
tool, and targeted
manual relocation MCP tool.

Acceptance:

- Apply with the wrong digest writes nothing.
- Apply with an omitted repository identity writes nothing, and first registration requires an
  explicit `--allow-new-repository` override.
- Safe actions apply independently of conflicts.
- Re-running audit immediately after apply reports no repeat actions.
- MCP and CLI use the same repository checks; neither has a weaker collision path.

### Phase 4 — Hook Center and recovery integration

Wire exact rename/edit observations into artifact source reconciliation, then add startup/post-commit
audit mode.

Acceptance:

- A supported rename event relocates one known source and still marks both structural paths dirty.
- An `apply_patch`, shell, or external move missed by the hook is found by the next Git/full audit.
- A copied file does not steal the original artifact identity.
- Hook reconciliation failure is visible and does not block the coding tool or structural stale
  detection.
- Default runtime mode is audit-only.

### Phase 5 — one-time Menhir graph repair

This is an operational step, not part of a code commit that silently mutates the live graph.

1. Export a graph backup and capture the pre-repair artifact/source inventory.
2. Run the new auditor against the committed Menhir tree.
3. Review the ledger and approve its digest.
4. Relocate the 13 `f441a23` sources with exact Git evidence.
5. Resolve older stale locators through declared UUID, Git history, or unique hash. Leave every
   ambiguous source unresolved.
6. Register the 24 missing current plan records and any other unambiguous typed corpus entries.
7. Do not derive terminal lifecycle states from archive/reference paths. Produce a separate list of
   lane/status contradictions for owner disposition.
8. Re-run audit and save the zero-repeat-action report.

Repair acceptance:

- All 28 current plan records have exactly one resolvable source locator.
- No two artifact sources claim one current locator.
- Existing artifact UUIDs are unchanged.
- Every unresolved source has an explicit reason.
- A second apply performs zero mutations.
- The graph backup and before/after counts are recorded in the operator handoff.

### Phase 6 — legacy metadata backfill

Phase 0 makes canonical metadata mandatory for newly created artifacts after feature activation.
Phase 6 addresses the older corpus:

```yaml
---
artifact_uuid: 00000000-0000-0000-0000-000000000000
artifact_type: plan
artifact_status: APPROVED
---
```

Reading this metadata strengthens relocation and clean-clone identity. Writing UUIDs and canonical
statuses into existing documents is a separate owner-approved documentation pass because it touches
every tracked artifact. The reconciler never inserts or edits frontmatter as a side effect of graph
repair.

Before any status backfill, normalize the current status vocabulary or review it one record at a
time. Only 3 of 28 current plans map today; broad alias additions would turn commentary such as
"partially implemented" into lifecycle claims the typed state machine cannot represent.

## Test matrix

### Pure planner

1. Exact path + same hash -> `NOOP`.
2. Exact path + changed hash -> `REFRESH_SOURCE`.
3. Declared UUID at a new path -> `RELOCATE_SOURCE`.
4. UUID points to one artifact while locator points to another -> conflict.
5. Git rename with changed bytes -> relocation.
6. Old path absent + one unclaimed equal hash -> relocation.
7. Old path still present + equal hash destination -> registration/copy, never relocation.
8. Two equal-hash destinations -> conflict.
9. Destination already claimed -> conflict.
10. Missing source with no match -> unresolved.
11. New backlog plan -> typed plan registration with backlog lane.
12. New reference PDF without declared type -> unclassified conflict.
13. Archive path with nonterminal status -> contradiction report, no status mutation.

### Repository and Neo4j

- Source-specific relocation updates one node and no relationship endpoints.
- Conditional integrity and locator checks refuse stale writes.
- Locator uniqueness survives concurrent relocation attempts.
- Source UUID and artifact UUID constraints reject duplicates.
- Registration is idempotent by declared UUID/current locator.
- Unscoped sources reserve their medium/path; declared UUID adoption preserves their source UUID,
  while path-only and hash-only matches remain conflicts.
- Unresolved -> resolved recovery retains identity.
- Source-v1 backfill reaches every existing embodiment before constraints activate.

### CLI, backend, and Hook Center

- Audit JSON is stable and contains no secrets or file contents.
- Plan-digest mismatch fails before the first write.
- Backend/provider/client signatures stay in parity.
- Read-only and agent tiers are enforced for new MCP operations.
- Rename events carry old/new paths and refresh the destination hash.
- Existing structural dirty/stale tests remain green.
- Unsupported tools remain fail-open; recovery audit catches their filesystem effects.

### Live acceptance

Use a throwaway Neo4j database and a temporary Git repository to exercise:

- create -> edit -> rename -> archive;
- rename plus content edit in one commit;
- copy followed by divergent edit;
- branch switch;
- deleted then restored source;
- interrupted apply followed by idempotent rerun.

The production repair runs only after this smoke passes and its dry-run ledger is approved.

## Files expected to change

New modules:

- `src/menhir/domain/artifact_reconciliation.py`
- `src/menhir/infrastructure/artifact_corpus_scanner.py`
- `src/menhir/services/artifact_reconciliation_service.py`
- focused domain, repository, CLI, and live-smoke tests

Existing seams:

- `src/menhir/domain/work_artifact.py`
- `src/menhir/infrastructure/work_artifact_repository.py`
- `src/menhir/infrastructure/schema.py`
- `src/menhir/infrastructure/memory_graph_adapter.py`
- `src/menhir/core/backend_protocol.py`
- runtime provider/client operation modules
- `src/menhir/api/routes.py` and `routes_support.py`
- `src/menhir/mcp/tools/ops/`
- `src/menhir/cli/`
- `scripts/hooks/menhir_file_event.py`
- `scripts/migrate_work_artifacts.py`
- `.agent/workflows/artifact_authoring.md`
- `.agent/README.md`, `.agent/file-index.md`, `.agent/maintenance.md`
- `.agent/workflows/feature_planning.md`
- `.agent/plans/README.md`, `.agent/plans/backlog/README.md`, `.agent/reference/README.md`
- `.agent/data_models.md`, `.agent/endpoints.md`, architecture and hook documentation

## Explicit non-goals

- No LLM file matching or lifecycle classification.
- No title-based automatic identity matching.
- No automatic relationship removal when frontmatter changes.
- No hard deletion of missing artifacts.
- No automatic lifecycle transition from directory names.
- No document-content storage in Menhir or Hook Center payloads.
- No `CurrentPlanView` implementation in this plan. Reconciliation supplies trustworthy source and
  lane data that the existing `CurrentPlanView` residual can consume later.
- No mandatory UUID-frontmatter rewrite during the first implementation or repair pass.

## Owner approvals

Implementation needs three explicit approvals at different points:

1. Approve this design and the locked read-only-first build order.
2. Approve the Phase 1 audit ledger/digest before graph-write code is used on the live corpus.
3. Approve any corpus-wide UUID/status frontmatter pass separately from graph reconciliation.
