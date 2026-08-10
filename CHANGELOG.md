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

## 2026-08-07 — docs: correct stale local-Docker Neo4j guidance in the operations runbook

- `operations_runbook.md` still described the pre-migration local-Docker `yawn-neo4j` workflow
  (`docker ps`/`docker start yawn-neo4j`, `bolt://localhost:7687`) even though `start-server.ps1`
  and `.env` (`NEO4J_URI`) have pointed at a remote host running `menhir-neo4j.service` (systemd)
  for some time — the desktop hasn't run Docker for menhir's Neo4j at all. Rewrote the startup
  "Behavior" bullets and the "Recover from local Neo4j-down startup failure" section to match
  actual current behavior: remote bolt-port probing, warn-and-continue on unreachable, and
  recovery via checking the remote host and starting `menhir-neo4j` there over SSH. Noted the
  root `docker-compose.yml` is vestigial. No code changed.
