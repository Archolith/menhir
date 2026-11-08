---
artifact_schema: 1
artifact_uuid: e04a51fd-77a2-4e81-864b-79ceebc70635
artifact_type: plan
artifact_status: IMPLEMENTED
---

# menhir -- on-demand `timeline` tool (navigate memories by time)

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Disposition: IMPLEMENTED (code: shipped as recall_timeline, PR #213; the behavior contract is [`../../plans/menhir-recall-timeline-plan.md`](../../plans/menhir-recall-timeline-plan.md)), closed with the A/B and latency PROFILE tracked in Archolith/menhir#366 (was: ACTIVE, partial)
Last verified: 2026-10-21 - DONE for code (medium); anchors checked: PR #213 merged 2026-10-01, src/menhir/mcp/tools/recall/recall_timeline.py, follow-up bug #242 closed. Steps 1-6 were not checked one by one; the section 4 A/B, section 7 PROFILE and the deploy-policy entry are open on #366.
Parent context: #172 (the appended build_context Timeline is
answer-neutral on LongMemEval and AMA; issue comments 5934663456 / 5935156692), #207 (`recall_history`).

## 1. Goal
The agent calls a read-only tool, only when it needs to, to:
- (a) list memories at a specific time or in a time window;
- (b) step next/previous from any memory along the same subject thread, or the whole namespace, without
  pulling full context;
- (c) expand one entry to full text on demand.

Entries are compact (time + one-line headline + ids); navigation is cursor-based, so a long history is
walked a page at a time instead of packed into every context.

## 2. Constraints (current code)
- **What `recall_history` does today:** semantic top-N episodes sorted oldest first, limit 1..50.
  - It has no time filter, no anchor/neighbors and no cursor
    (`src/menhir/services/recall_pipeline.py:186-240`).
  - Its wiring is the template for a new read-only tool:
    - domain `RecallHistoryResult` (`domain/recall.py:238`);
    - `backend_protocol.py:153`, `backend_client_ops.py:123`, `backend_runtime_data_ops.py:358`;
    - REST `api/routes.py:195-210` + `routes_support.py:321-330,653`;
    - MCP `mcp/tools/recall/recall_history.py`;
    - registries `access_contract.py:53`, `explorer/feature_taxonomy.py:28`, `mcp/feedback.py:35`,
      `mcp/tools/ops/get_memory_stats.py:23`;
    - `.agent/concept-ids.yaml`, `endpoints.md`, `tasks-mcp.md` (per CHANGELOG 2026-09-30).
- **Episode reads must reuse the shared predicates.**
  - `processing_state` present and not FAILED, `non_structural_memory_cypher`,
    `default_recall_visibility_cypher`, and `tenant_scope_cypher`, with the #171
    `tenant_scope_prefilter_cypher` lead when scoped (`infrastructure/memory_queries.py:382-430`).
  - The CF-127 guard (`tests/test_cf127_tenancy_scope_predicate.py`) forbids hand-written
    `.namespace` / `.group_id` comparisons in these files.
- **Indexes already exist.** RANGE on `:Episodic(valid_at)`, `:Episodic(created_at)`,
  `:Episodic(namespace)`, `:Episodic(group_id)`, and `:RELATES_TO(valid_at|invalid_at)`. Checked on
  the LME test DB with `SHOW INDEXES`. No episode-to-episode ordering edges exist.
- **Time ties are common.** All turns of an LME session share one `valid_at` (the session time), so
  ordering needs a total key: `(valid_at, created_at, uuid)`.
- **Two time bases exist.**
  - Episode `valid_at` is when it was said or recorded.
  - Fact-edge `valid_at`/`invalid_at` is the world time of a fact. On LME, invalidation can carry
    ingest wall-clock (#172 spot check).
- **Subject threads can be derived.** `(:Episodic)-[:MENTIONS]->(:Entity)` exists on both test graphs
  (#171 gold mapping used it).
- **Deployment tool allowlists must list the tool.** Those live outside this repository.

## 3. Proposed design
**Links are computed, not stored.** next/previous is a seek on the indexed total order
`(valid_at, created_at, uuid)` within the thread's filter, so there is:
- no write-path change and no migration;
- no NEXT/PREV edges to maintain on late ingest, delete, retention or supersession;
- always-correct neighbors for out-of-order ingest.

The cursor is an opaque string encoding `(valid_at, created_at, uuid, thread, direction)`.

**Tool:** `timeline` (MCP + REST `POST /api/timeline`), read-only, no access updates. Arguments:
- `namespace`: resolved exactly like `/api/recall`.
- Exactly one way to choose where to start:
  - `at` (ISO instant or date): entries around that time, half before and half after;
  - `from`/`to`: entries in the window, oldest first;
  - `around` (memory/episode uuid): that entry plus neighbors;
  - `cursor` (from a previous page): continue in its direction;
  - `query` alone: semantic seeds (the `recall_history` search) to pick the thread, then the
    thread's latest entries.
- `subject` (optional): an entity name, uuid, or a query whose top entity is used.
  - It restricts the thread to episodes that MENTION that entity.
  - Without it, the thread is the whole namespace (all visible episodes).
- `direction`: `before` | `after` | `both` (default `both` for `at`/`around`).
- `limit`: 1..50, default 10.
- `detail`: `headline` (default) | `full`.
  - `headline` = time + first ~160 chars of content (whitespace-collapsed).
  - `full` returns content up to the `recall_history` max chars.
- `facts` (bool, default false): attach that episode's fact edges (fact, valid_at, invalid_at,
  current/superseded) for world-time detail.

**Result:**
- `entries[]`: uuid, time, time_basis=`recorded`, headline or content, session_id, optional facts.
- `prev_cursor`, `next_cursor`: null at the ends.
- `thread`: `namespace` or the subject entity with name and uuid.
- `note`.

### Steps
1. **Queries** (`infrastructure/memory_queries.py`, `memory_graph_adapter.py`): `timeline_page(...)`.
   - Window or seek on the total order, with the shared predicates and optional
     `MATCH (n)-[:MENTIONS]->(:Entity {uuid:$subject})`.
   - `timeline_facts(episode_uuids)`.
   - Reuses the predicates; no hand-written tenancy.
2. **Domain** (`domain/timeline.py`): `TimelineEntry` and `TimelineResult`, cursor encode/decode
   (base64 JSON, validated, namespace-bound so a cursor cannot cross tenants), and argument
   validation (exactly one start mode).
3. **Service** (`services/recall_pipeline.py` or a new `services/timeline_service.py`):
   `run_timeline`.
   - Resolves the subject: uuid, then exact name in namespace, then top entity of a semantic query.
   - Seeds a `query`-only call via the `recall_history` search.
   - Failures raise, as `recall_history` does.
4. **Wiring:** mirror every `recall_history` surface listed in section 2 (protocol, client, runtime,
   REST models and route, MCP tool, `access_contract`, taxonomy, feedback, stats, concept ids, docs).
   Not in `always_visible`.
5. **Tests** (`tests/test_timeline_tool.py`):
   - tie ordering;
   - cursor round-trip and tamper / cross-namespace rejection;
   - `at` / window / `around` / `query` modes;
   - subject thread filtering;
   - visibility and FAILED exclusion;
   - CF-127 guard still passing;
   - REST/MCP/protocol parity (as `test_recall_history.py`).
6. **Docs:** CHANGELOG, `tasks-mcp.md`, `endpoints.md`.

## 4. Evaluation (before any default/agent-prompt change)
Agentic A/B with tool calling and the same answer model.
- Arms:
  - Arm A: `build_context` only.
  - Arm B: `build_context` + `timeline` tool available.
- Corpora:
  - AMA gold "previous"/"timeline" questions (real per-step histories);
  - LME temporal-reasoning + knowledge-update on the copy.
- Metrics: correct / stale, tool calls per question, total tokens, latency.
- Cost estimate goes to the owner before the run (flex; expected low single dollars).

## 5. Risks
- **Mitigable:**
  - Subject resolution by name is ambiguous across same-name entities. Return candidates in `note`
    and require uuid on ambiguity.
  - LME invalidation timestamps are ingest wall-clock. `facts` shows them with their basis label;
    `recorded` time stays the navigation key.
- **Mitigable:** a large namespace with no subject paging the whole graph. Seek on the indexed
  `valid_at` with LIMIT; verify with PROFILE on the LME copy.
- **Acceptable:** a cursor goes stale after a delete. Seek semantics continue from the key, not the
  node, so it still works.
- **Blocked (external):** deployment tool allowlists must be updated when this ships.

## 6. Open questions (owner)
1. Should navigation key on recorded time (episodes, the proposal) or world time (fact edges)?
   Proposal: recorded for v1, with world-time facts attachable.
2. Should it extend `recall_history` with these arguments, or be a separate `timeline` tool?
   Proposal: separate. `recall_history` stays the semantic drill-down, and keeping them apart avoids
   overloading its contract.
3. Should typed scalar/event history (supersession chains per attribute) join the thread in v1, or
   wait for the KU78 ingest?
4. Should the old appended brief timeline be removed once this ships?

## 7. Validation
- Focused tests, the CF-127 guard, and the full suite once.
- PROFILE of `timeline_page` on the LME copy: index seek, p50/p95 under 50 ms.
- Manual walk of one AMA trajectory end-to-end by cursor, matching `Step N` order exactly.
- Then the section 4 A/B.

## Coverage
- **Inspected:**
  - `services/recall_pipeline.py:186-240`;
  - `infrastructure/memory_queries.py:355-430`;
  - the `recall_history` wiring grep across `api/`, `core/`, `mcp/`, `access_contract.py`, `explorer/`;
  - `domain/brief_builder.py`;
  - `services/context_builder.py`;
  - LME and AMA test DB indexes and time fields.
- **Not inspected:**
  - typed scalar/event history read paths (question 3);
  - deployment configuration;
  - the MCP tool registration internals beyond the recall_history file list.
- **Assumptions without direct evidence:**
  - `created_at` is set on all Menhir episodes (seen on LME, not proven for every write path);
  - MENTIONS edges exist for all enriched episodes (true on both test graphs; unproven for raw-capture
    paths).
