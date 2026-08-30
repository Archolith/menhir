## 2026-08-29 - close the live operations gateway source blockers

- Updated the live VPS playbook to the implemented fixed topology: local
  no-shell root-wrapper dispatch, a Docker-bridge-only gateway listener, exact
  Caddy peer admission, `/ops` TLS routing, and OAuth protected-resource
  discovery bound to the operations audience.
- Pinned the external `menhir-proxy` bootstrap command to subnet
  `172.30.0.0/24` and gateway `172.30.0.1`, matching the fixed host listener.
- Kept live activation explicitly unproven until the immutable four-repository
  release is installed and passes backup, candidate, route, authorization, and
  public negative-path acceptance on the VPS.

## 2026-08-29 - add a guarded live VPS deployment playbook

- Added the canonical ordered workflow for immutable four-repository Menhir
  releases, one-time host bootstrap, backup/rehearsal/candidate/route/promotion,
  acceptance, and phase-aware rollback.
- Documented two real first-bootstrap stop gates in the current operations
  gateway: its Linux service still dispatches through a Windows-only runner,
  and no reviewed TLS transport currently reaches its loopback listener.
- Updated the production environment example to the current digest-bound hosted
  operator policy and added contract tests to prevent workflow or digest drift.
- Removed the stale blanket rejection of admin-scoped production authorization.
  Exact client policy still controls the full scope set, so ChatGPT and Claude
  can complete their reviewed operator grant while narrower clients remain
  unable to request admin authority.

## 2026-08-29 - allow hosted operators to ingest documents and projects

- Added `ingest_document` and `ingest_project` to the exact ChatGPT and Claude operator policies.
  Hosted operators now receive 51 of 54 MCP tools; only `delete_namespace`, `mint_client`, and
  `revoke_client` remain denied.

## 2026-08-29 - promote hosted web clients to operator authority

- Promoted the separate ChatGPT and Claude OAuth clients to exact operator-tier grants with
  read, write, and admin scopes. Each receives 49 of the 54 MCP tools, including artifact,
  todo, conflict, scheduler, and scoped memory-administration operations.
- Kept namespace-wide deletion, host-filesystem ingestion, and client credential administration
  outside hosted connector authority. Bumped the consent-session schema so the scope elevation
  requires fresh operator authorization; old access and refresh tokens fail the exact scope check.

## 2026-08-29 - bind production rights and consent to each OAuth client

- Removed shared Agent Smith consent groups so every hosted and managed client requires an explicit
  approval and can carry an independent digest-bound tool policy.
- Kept hosted web clients on the reviewed memory, diagnostics, and structure surface. Narrowed
  Agent Smith clients to their documented workspace tools, including read-only `list_todos`, while
  retaining `add_memory_and_track` only for Codex because its generated config explicitly pins it.
- Rejected legacy or unknown client-policy fields, documented the authority boundary, and added
  regression coverage for different client rights and non-transitive consent. Versioned consent
  cookies invalidate the old group-capable format at deployment.

## 2026-08-29 - expose read-only project structure to production agents

- Added `query_structure` to the digest-bound production tool surface for hosted web and
  Agent Smith OAuth clients so connector sessions can inspect already-ingested repositories.
- Kept `ingest_project` denied; expanding the production connector does not grant a new graph-write
  path. Added policy assertions and documented the resulting authority boundary.

## 2026-08-29 - retire Reasonix OAuth authority

- Removed the archived Reasonix client from Agent Smith's published OAuth metadata and
  digest-bound production policy.
- Added regression coverage proving the retired client is neither published nor admitted by
  production policy while the remaining managed-client suite stays intact.

## 2026-08-28 - fix: complete Claude web refresh authorization

- OAuth protocol scopes are now separate from Menhir permission scopes, so
  `offline_access` can request a refresh token without becoming a new access tier.
- The dedicated Claude web registration alone declares `offline_access`; ChatGPT and every
  Agent Smith registration keep their existing exact scope contracts.
- Startup atomically upgrades the exact legacy Claude registration and refuses unknown or
  disabled protocol scopes before the service accepts traffic.
- Consent pages allow only the validated callback origin through CSP `form-action`, so
  Chromium can follow the authorization POST redirect instead of stranding issued codes.

## 2026-08-28 - unreleased: make View lifetime follow live evidence

- This entry describes local source behavior only. Production activation still requires additive
  schema execution, legacy evidence reconciliation, coordinated writer deployment, and observation;
  Graphiti publication recovery, replay tombstones, and the generic repair dispatcher are not yet
  runtime-enabled.

- Current FACT Views now require every declared contributor to resolve to live `:Episodic` or
  `:TurnEvidence` before the View can become current, and both evidence labels receive `MENTIONS`
  retention links in the same write transaction.
- Generic recent, flagged/bootstrap, scoped, typed, and scored recall now fail closed on candidate,
  gone, retired, superseded, internal zero-provenance, and orphaned Views. Explicit UUID inspection
  remains available for provenance and operator diagnosis.
- Ordinary memory decay no longer compresses or deletes derived Views. Explicit evidence and
  namespace erasure still wins, but atomically retires dependent current Views, scrubs erased UUIDs
  from retained versions, and resets counter/scalar/event fold watermarks before evidence removal.

## 2026-08-19 - fix: HIGH remediation wave 5 (explorer authorization, domain correctness)

- **Every explorer route now carries a tier floor.** The explorer's 36 routes mount into the same
  FastAPI app that enforces tier 23 times on its own routes, and enforced it zero times on these.
  Enforcement is a router-level dependency rather than 36 route bodies, so a route added later
  inherits it. Five mutating routes re-assert `agent` above that floor.
- **The explorer is no longer built on import.** A complete FastAPI application was constructed
  every time the package was imported - which happens on every production start - so the
  `explorer_enabled` gate could not prevent it, and a config error in a disabled subsystem could
  abort startup.
- **`timeline()` orders by date, not by byte.** The parser deliberately accepts slash dates; the
  sort compared raw strings, where `-` precedes `/`, so every slash date sorted after every dash
  date regardless of its calendar position.
- **A stated frequency count is no longer overwritten with 1.** "I read 2 books every week"
  captured no count because a noun sat between the number and "every", and the fabricated 1
  replaced the model's own extracted value. It now abstains and keeps the model's value.
- **Preflight no longer reports `embedder_ready=True` having checked nothing.** An unconfigured
  embedder reports not-ready, honestly, without gating startup for the deployments that run that
  way today.
- **A committed write is no longer reported as a failure** because a cosmetic read after it failed.
- **`transition_artifact` is namespace-scoped**, in both the legality read and the mutation.

## 2026-08-19 — merge: CF-165 end-to-end closure

- Merged `fix/cf165-e2e-closure` (35 commits): subject lineage enforced at the telemetry
  persistence boundary, dual-keyed MCP events, multi-dimensional subject keys, replay made
  fail-closed, TurnEvidence purged on the direct-namespace form, and recall-feedback prose
  scrubbed from the sidecar.
- **Telemetry payload redaction is now one implementation, and it is the stricter one.** That
  branch and wave 3 had independently built a redactor for `mcp_events.payload_preview`. An
  allowlisted key alone is no longer enough — the value must also be identifier-shaped, which
  closes a hole where a caller could smuggle prose through a structurally-named key such as
  `namespace`. Redaction happens inside `_preview_of` itself, so a future writer gets the safe
  behaviour without opting in, and the two allowlists are unioned into one 57-key list.
- Error strings are no longer persisted verbatim; the exception type is recorded instead.

## 2026-08-19 — fix: guard the remaining UUID-addressed mutation tools (ET-002)

- `unflag_memory`, `promote_memory` and `close_memory` now take a namespace and check ownership
  before mutating, closing the gap left when `delete_memory` and `flag_memory` were guarded.
  A UUID is an identifier, not proof of tenancy: a pinned caller that learns a foreign uuid
  through any global read reached these directly, because the namespace pin cannot be injected
  into an endpoint whose signature does not declare it.
- `unflag_memory` is the one that matters most — it runs at the default agent tier, so no
  operator credential is needed to strip another tenant's retention protection and return their
  data to ordinary lifecycle decay.

## 2026-08-19 — fix: the namespace pin reaches REST, and two unscoped tenant reads closed

- **The namespace pin now applies on the HTTP transport.** `MENHIR_CLIENT_NAMESPACES` binds a
  client to a namespace server-side and the guarantee is documented as absolute, but only MCP
  tools enforced it — REST never consulted it, so a credential restricted to one namespace
  reached every namespace by putting one in the request body. Nothing new was needed: the auth
  middleware already binds the session that carries the client name on that path.
- **`read_flagged_memories` no longer returns every tenant's flagged content.** Its query had no
  tenancy predicate at all, it runs at the lowest tier, and agents are told to call it at the
  start of every session. The bootstrap version fingerprint is scoped with it, so the receipt
  gate answers the same question the read does.
- **`close_stale_todos` no longer reads and closes every tenant's todos.** At the default agent
  tier it matched all open todos, returned their content, and closed them. Scoping matches the
  requested namespace exactly rather than the read path's requested-plus-default rule: a bulk
  mutation must not touch the shared bucket as a side effect.

Both unscoped reads were found by tracing the tools that CF-33 records as unreachable by the pin
down to their queries. That finding documents the coverage gap; it does not record that some of
those tools read tenant content with no predicate at all, which is the sharper problem.

## 2026-08-19 — fix: HIGH remediation wave 3 (retention, at-rest privacy, unbounded scans)

- **Every telemetry time window was silently too wide.** Rows are written with Python's isoformat
  and compared as TEXT against SQLite's `datetime('now')`, whose separator and precision differ, so
  a stored value sorted above a same-instant cutoff. A row genuinely 25 hours old read as inside a
  24-hour window. Fixed on the read side, so old and new rows are both correct with no migration.
- **Raw MCP tool arguments are no longer persisted verbatim.** The first 500 characters of every
  memory submitted through `add_memory` were written in plaintext to the sidecar, keyed to nothing.
  Redaction now happens at the single write boundary, under an allowlist, so a tool added later is
  private by default rather than leaked by default. Call shape (which arguments, which limits and
  flags) still survives for debugging.
- **`:TurnEvidence` is deleted inside the namespace cascade.** It holds raw user prompts, and two
  of the three deletion paths left it behind; the third purged it as an unjournaled step after the
  erasure saga had committed, so a crash in that window left prompts with nothing able to resume
  them. The blast-radius count and the pre-erasure subject capture were updated in the same change,
  because those three predicates must name the same set.
- **The documented revision-retention control now exists.** The setting was read nowhere, the
  pruner had no production caller, and its signature duplicated the setting's default instead of
  reading it — while the operator runbook stated the window was enforced and configurable. All
  three are closed. Retention for the other high-volume tables remains post-MVP, as that runbook
  already discloses honestly.
- **Post-ingest edge stamping no longer scans every relationship twice.** The match was untyped, so
  none of the five uuid relationship indexes could back it, and undirected, so it produced two rows
  per relationship. Now typed from `EDGE_LABELS` and directed.
- **Indexes added** for the Hook Center `structure_*` predicates (reached on every recall) and the
  `:Episodic` content prefix scan behind dirty-namespace detection.
- **`capture_changes` logs one commit on its default path**, as its docstring always claimed. Bare
  `HEAD` is a revision, not a commit — measured at 1,715 lines of output where 3 were intended, on
  a 64-commit repository, growing without bound as the repo ages.
- **The `code_ref` resolver is anchored and defined once.** `ENDS WITH 'utils.py'` also matched
  `my_utils.py`, and with `LIMIT 1` a todo got a `:REFERENCES_FILE` edge to an arbitrarily chosen
  wrong file — surfaced to users as `linked_file_path` beside a correct `locations[]` in the same
  payload.

## 2026-08-19 — fix: HIGH remediation wave 2 (tenancy semantics)

- **Settled the tenancy contract instead of re-litigating it.** The audit filed CF-199 as three
  mutually contradictory positions on whether `:Entity` is tenant-scoped. `domain/namespace.py`
  already answers it: namespace maps 1:1 onto graphiti's `group_id`, which is the load-bearing
  isolation boundary; the `namespace` property is defense-in-depth; `"default"` maps to group id
  `""`; and an unspecified namespace does not filter, because isolation is opt-in. Two of the three
  "positions" are that documented contract. The decision is recorded in
  `.agent/plans/menhir-cf199-tenancy-decision.md`.
- **Closed the one real leak in that group.** `query_blast_radius` applied its namespace to one of
  four sub-queries; the memory-preview fetch two lines above had no tenancy predicate at all and
  returned up to 10 previews from every namespace through a `readonly`-tier MCP tool.
- **Stopped comparing a group id against a namespace name.** The scalar-history read coalesced
  `group_id` to the NAME `'default'`, which cannot match the `""` group id the write path produces.
- **Let the namespace pin reach the two UUID-addressed mutating tools.** `delete_memory`
  (destructive, operator tier) and `flag_memory` (agent tier) declared no `namespace` parameter, and
  the pin is applied by signature introspection, so no pin could ever apply to them. Both now check
  ownership before mutating — while still allowing erasure of residual content for a node already
  absent from the graph, which is a supported path rather than an error.
- **Stamped `group_id` on raw-capture entities.** Of eight `:Entity` write sites, seven set the
  tenancy property and one did not, so the captures created to make a terminally-failed episode's
  text reachable by recall were invisible to exactly the scoped recall they exist for.

## 2026-08-19 — fix: HIGH remediation wave 1 (15 confirmed findings)

- **Extraction and LLM output parsing.** `_extract_first_json_payload` now strips a code fence
  wherever it appears and returns the first payload `raw_decode` accepts, instead of taking
  first-`{` to last-`}` greedily and only handling fences at position 0 — ordinary model chattiness
  was turning recoverable JSON into a parse failure. Entity names resolve once with an explicit
  `name > entity_name > entity` precedence, so two payloads differing only in key order no longer
  produce different graph identities. `description` and `summary` promoted into an edge's `fact`
  now carry the synthetic marker rather than being stored as model-asserted.
- **Combined-extraction patches.** The patched response model declares both output fields exactly
  as upstream does — required and described — so a `{}` or typo'd-key response fails validation
  instead of validating as a successful zero-extraction. The assistant self-echo policy is decided
  on role and label before the `known` membership test and drops echo edges explicitly, closing a
  bypass Menhir's own extraction prompt was inducing. The combined-extraction patch proves its
  dependency at patch time and restores Graphiti's originals when it cannot complete.
- **Event loop.** 38 synchronous SQLite telemetry writes inside `async def` bodies moved off the
  loop (~30 per ingest, measured mean 12.4 ms each), along with the two explorer routes reading
  `pending_actions` synchronously. The one write inside the circuit breaker's lock is deliberately
  left synchronous — an await there is a cancellation point that wedges the breaker.
- **SQLite contention.** `MENHIR_TELEMETRY_BUSY_TIMEOUT_S` now reaches all seven stores sharing the
  telemetry database file, not one of them. (The audit register listed five; `erasure_subjects` and
  the scheduler lease store are the sixth and seventh.)
- **Decay sweep.** Decay candidates are bounded per run and the sweep stops calling the LLM after
  three consecutive compression failures, instead of paying up to 242 s of backoff per candidate
  over an unbounded candidate set.
- **Data preservation.** Raw-capture creation for retry-exhausted episodes was shadowed by a second
  definition of the same method and never ran, so terminal failures lost their text to recall. The
  duplicate is gone, and the unbounded fetch and missing `raw_capture_for` index that restoring it
  switches back on are fixed in the same change.
- **Embedding cache.** A short upstream response no longer becomes a zero-length embedding vector;
  the cache returns the upstream result unmodified rather than synthesising gaps.
- **Security and privacy.** The ingest path guard now covers `write_project_structure` and its
  background rescan, which reached the same scanner unguarded with a caller-supplied root. The
  synchronous chat seam resolves through the scheduler and refuses to construct a client that would
  send personal-memory content to `api.openai.com` on the empty-base-url sentinel.

## 2026-08-11 — feat: add an idempotent post-install and agent onboarding path

- Added `menhir setup` to create a missing `.env`, wire repository-managed Git hooks, audit setup
  without mutation, preserve custom hook paths, and optionally install Claude-compatible lifecycle
  hooks or the Windows watchdog task.
- Added the managed pre-push hook, focused CLI/setup tests, and actionable output from `menhir check`.
- Added a complete post-install runbook and a paste-ready consumer-agent contract; replaced duplicated
  model-specific instructions with concise routing through `AGENTS.md` and `.agent/README.md`.
- Aligned TurnEvidence and file-event producer defaults with Menhir's public port `8100`.

## 2026-08-09 — fix: harden scalar identity and event recall

- Distinguished non-quantifying `around` idioms from genuinely approximate quantities so grounded
  scalar values survive ordinary discourse without weakening open-world hedge handling.
- Guarded unresolved acquisition anchors and made event recall fail closed when current authority
  cannot be resolved safely.
- Reconciled scalar subjects by exact namespace identity and asserted namespace lookup at the query
  boundary, including replay and pending-binding coverage.
- Updated the cumulative-activity validation record and event/scalar hardening documentation.

## 2026-08-09 — fix: isolate structure nodes from Graphiti semantic replacement saves

- Root-caused a vanished structural project node: it was not deleted. Graphiti semantic
  deduplication selected the structural `:Entity`, untyped attribute extraction replaced its
  attributes with `{}`, and Graphiti's `SET n = node` save stripped every Menhir-owned structure
  property while retaining the UUID and relationships.
- Filtered nodes carrying `structure_role` from Graphiti's semantic candidate collection, covering
  both search results and caller-provided overrides while retaining ordinary semantic entities.
- Hardened untyped attribute hydration to preserve a copy of existing properties instead of
  returning an empty mapping, preventing whole-map replacement from erasing externally owned
  properties even if another candidate boundary is missed.
- Added regression coverage for structural candidate exclusion, semantic candidate retention,
  `None`/empty-schema preservation, and typed-schema delegation. The expanded Graphiti/enrichment
  regression selection passes 1,088 tests with 34 expected skips. Updated the incident RCA from
  unresolved to confirmed and remedied. The local service reports ready after the change.

## 2026-08-09 — fix: restore nested-repo detection and close three structure-prune gaps

- Nested-repo detection had been dead since `cd330fc`. The directory walk tested gitignore before
  `_is_nested_repo`, and an umbrella repo gitignores its sub-repos by construction, so making
  root-anchored patterns match turned every repo boundary into an ordinary ignored directory.
  47 of 49 nested repos across the four umbrellas were invisible; `archolith` kept 11
  `CONTAINS_REPO` edges only because they predated that commit, and `workspace-meta` was down to
  the single sub-repo its gitignore does not cover. Being gitignored by the parent is evidence
  *for* separate-repo status, so the boundary test now runs first.
- Boundary and containment are separate questions. Every repo marker stops descent, but only an
  independent clone (`.git` as a directory) is recorded as containment: a worktree (`.git` as a
  file) is a second checkout of a repo the graph already holds, and both absorbing its files and
  recording it as a contained repo are wrong.
- Endpoint pruning now gates on scan completeness rather than a non-empty keep-list, so "this
  project exposes nothing" became expressible. `archolith` had held 102 endpoints belonging to
  nested repos it stopped indexing, permanently, because the old guard read a legitimate zero as
  a failed scan. Dependencies had no prune path at all, and `CONTAINS_REPO` edges are now pruned
  to the current scan — the edge only, never the child project.
- Staleness checking gained the two states it was missing. A project node with no `root_path` was
  reported as healthy rather than unverifiable, and entities whose project node is gone appeared
  in no listing at all, so no check reached them. The `projects` listing now separates STALE
  (root recorded, directory gone) from NEVER SCANNED (no root recorded) and reports entity sets
  with no project node — 3,580 across 11 names, largest `yawn.bot` at 1,255.
- `SCANNER_SCHEMA_VERSION` 4 → 5 to force re-scan. Verified live: `archolith` dropped all 102
  stale endpoints and kept its 11 containment edges, `workspace-meta` went from 1 edge to 8.
  Restoring containment also created five new name-only stubs for umbrella repos that have never
  been ingested; they are surfaced as NEVER SCANNED rather than passing silently.

## 2026-08-09 — fix: contain Graphiti node-deduplication context fan-out

- Replaced the failed-enrichment RCA's previous-episode attribution with the measured cause:
  Graphiti unions up to 15 semantic candidates for every unresolved extracted entity into one
  dedupe prompt. The affected 69- and 98-entity extractions could therefore assemble prompts with
  more than a thousand candidate records even though their episode bodies were only 2–3 KB.
- Kept the normal single-request dedupe path, then recursively bisect oversized entity batches and
  rebuild each half's candidate union. Candidate order and the 15-per-entity retrieval limit are
  unchanged, while request size is contained.
- Provider context-limit responses now enter the same adaptive path as local preflight failures
  immediately instead of retrying the identical payload three times. The fallback estimator is
  also more conservative at three characters per token with ceiling division.
- Added synthetic 69- and 98-entity regressions, a real Graphiti prompt-builder/size-guard
  integration case, prompt-size estimator coverage, and a no-retry provider-error test.
- Re-enriched the three incident episodes one at a time after the local suite passed. All reached
  `READY` with semantic writes present, and the unfiltered failed queue is now empty. Their retry
  prompts fit without splitting because re-extraction returned smaller entity sets; the adaptive
  branch is pinned by the deterministic real-prompt integration test.

## 2026-08-09 — fix: decode project-scan reads as UTF-8 instead of the platform locale codec

- `infrastructure/project_scanner.py` read every file it scans via `read_text(errors="replace")`
  with no `encoding=`, so Python fell back to `locale.getpreferredencoding()` — cp1252 on Windows.
  UTF-8 content decoded through the wrong codec, and the resulting mojibake was written into Neo4j
  Entity nodes: `query_structure("projects")` rendered the em-dash (U+2014, bytes `E2 80 94`) in
  the `cth.crypto` and `yawn.frontend` descriptions as a three-character cp1252 sequence. All 13
  call sites now read through one helper.
- Three reads feeding the symbol/import/endpoint parsers already passed `encoding="utf-8"` but not
  `utf-8-sig`, so a BOM leaked U+FEFF into the first extracted symbol; they now strip it. The same
  change fixes `json.loads` on a BOM-prefixed `package.json`, which previously raised.
- Added `infrastructure/text_io.py` as the single decode contract (`read_text_utf8`) rather than
  restating `encoding=`/`errors=` at each site. Restating it is how the defect happened: nobody
  chose the wrong codec, sixteen call sites each independently failed to choose the right one.
- Two more paths had the same defect and are now routed through the helper. `ingest_document`
  (`core/backend_runtime_data_ops.py`) read with plain `utf-8`, so a BOM'd document put U+FEFF at
  the head of `content_excerpt`/`narrative` and therefore into the episode body and document node.
  The sage-wiki ingest in `cli/__init__.py` had it too, where a leading BOM defeats the
  `content.startswith("---")` frontmatter check and silently drops a document's frontmatter.
- Added two scanner regression tests that pin the file's bytes rather than the platform default
  (both fail against the old code) plus eight helper tests. 143 passed across the scanner,
  structure-query, watcher, `ingest_document`, gateway, and CLI-hook suites; the touched files are
  ruff-clean, with 7 pre-existing errors in `cli/__init__.py` and `backend_runtime_data_ops.py`
  left untouched.
- Not retroactive: rows already in the graph stay corrupted until the affected repos are
  re-scanned with `ingest_project`.

## 2026-08-09 — docs: correct stale status on three backlog entries

- `post-v1-todo.md` Priority 2 listed the enrichment SLO metric and the scheduler lifecycle MCP
  tools as open work. Both landed in `8047112` (2026-04-01), two weeks after `26184d0` wrote the
  TODO, and the doc was never updated: `fetch_enrichment_rate()` returns `p95_duration_ms` and
  `get_memory_stats` renders it against a 120s target with an `ok`/`MISS` flag, and
  `pause_scheduler` / `resume_scheduler` ship as MCP tools under `mcp/tools/ops/`.
- `memory-backlog.md` `git_diff_attachment` claimed no diff size guard exists. Corrected to
  half-done rather than closed: `MAX_DIFF_CHARS = 50_000` bounds the composed episode body sent to
  Graphiti (`services/enrichment_steps.py:157`), but the raw diff is still written untruncated onto
  the `:Episodic` node (`infrastructure/episode_lifecycle.py:149`) with nothing clamping it
  upstream, so the entry's original "before Neo4j storage" wording is still literally accurate.
- Trimmed `CHANGELOG.md` back to the 10 most recent entries; four 2026-08-06 entries moved to
  `CHANGELOG-archive.md`.
- Docs only. No code changed.

## 2026-08-08 — fix: restore human duration displays on Scalar State Views

- Kept elapsed durations canonically stored in seconds for deterministic folding, voting, deltas,
  and signatures, while making Scalar State Views derive readable `M:SS`/`H:MM:SS` displays when
  no explicit display is supplied. This closes the remaining `lme-6a1eabeb` gap where `25:50` was
  correctly normalized to `1550 seconds` but the recall surface exposed only `1550`.
- Added regression coverage from View persistence through the structured Recall Lab packet,
  including fractional, hour-length, signed, and range-shaped duration values.
- Structured packets keep the readable display separate from the canonical value and unit, avoiding
  ambiguous phrases such as `25:50 seconds`; raw numeric state remains available as
  `canonical=1550 seconds` metadata.

## 2026-08-08 — feat: prototype a typed Recall Lab packet

- Added an inspection-only, deterministic Recall Lab packet that groups the existing live graph
  projection into authoritative current state, advisory change history, completed events, and
  general content. Each entry carries available world/learned time, derivation, grounding,
  and source identities instead of flattening every memory into an equivalent text line.
- Added compact prompt-oriented and structured packet views in a dedicated task tab. The prototype
  can toggle between categorized Pretty cards and the exact Raw LLM packet text; debug JSON remains
  separately labeled as not sent to the model. The prototype does not change production recall,
  ranking, ingestion, persistence, or benchmark answers.
- Untyped memories remain general context. The packet performs no recall-side tentative-intent
  inference; intent authority will come only from the planned ingest-owned Intent State View.
- Added a query-filtered follow-up contract and benchmark-task endpoint. Existing production recall
  selects evidence first; matching scalar/event Views then restore typed authority and provenance,
  while ranked non-authority results are bounded to four compact general memories under a hard
  6,000-character budget. History and event sections are query-intent gated, long shared quotes do
  not route unrelated scalars, and neither ranking nor graph state is changed.
- Durable retrieval/authority UUIDs are the production selection boundary; lexical identity matching
  is restricted to legacy inputs with no IDs. Added cross-domain contract coverage for governance
  policies and coding architecture decisions so packet behavior is not benchmark-domain shaped.

## 2026-08-08 — fix: make Recall Lab task sections true tabs

- Replaced scroll-position-driven task navigation with a single-panel tab interface: selecting
  Turns, Assertions, Scalar Roles, Scalar State, Scalar History, Event History, Memory, or Answer
  Path now hides every other panel instead of scrolling down the page.
- Preserved URL hashes/deep links and added tab semantics plus Left/Right/Home/End keyboard
  navigation. The no-JavaScript fallback still exposes every section.
- Rendered Turns as a conversation stream, with user evidence aligned right and assistant evidence
  aligned left using distinct bubble styling on desktop and mobile.
