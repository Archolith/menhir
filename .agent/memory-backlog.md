# Memory Backlog

Compact backlog companion for [memory-design.md](memory-design.md).

Use this file first when the task is about future work, weak spots, open questions, or roadmap-ish
design ideas rather than current runtime behavior.

## Scope

This file covers:

- open design questions
- weak spots and feature ideas
- community-derived backlog
- external project evaluation
- code graph companion ideas
- progressive retrieval, hierarchy, and cache planning

## Sections

### `memory.backlog.open_questions`
- source sections: `Open Questions`
- use for:
  - unresolved design choices still shaping the system

### `memory.backlog.weak_spots`
- source sections: `Weak Spots and Feature Ideas Backlog`
- use for:
  - known gaps in reliability, retrieval quality, and product surface
- follow-up:
  - replace the current missing-edge-`fact` compatibility fallback with a targeted `edge_fact_repair` pass. Scope it to Graphiti edge payloads that already have valid `source_entity_name`, `target_entity_name`, and `relation_type` but fail validation because `fact` is missing. Preferred later-milestone design: run a small LLM repair step against the episode text and invalid edge stubs, record whether the edge was `original`, `llm_repaired`, or `synthetic_fallback`, and keep the current mechanical synthesis only as the last safety net.
  - add a doc-drift/contradiction pass that can compare root docs, project docs, and durable memories, then surface likely conflicts during agent bootstrap.
  - add a workspace bootstrap preset that returns service ownership, ports, startup order, and known discrepancies in one compact context block for "orient me" requests.
  - attach freshness metadata to structural summaries and recalled facts so agents can see when a fact was last verified instead of treating all recall as equally current.
  - normalize UTF-8 BOM and related encoding noise during doc ingest and structure indexing so project descriptions do not leak artifacts like `ï»¿`.

### `memory.backlog.progressive_retrieval`
- source sections: `Scoring`, `Query Construction`, `Code Graph Companion MVP`, `Temporal Awareness (post-v1)`
- use for:
  - practical retrieval improvements before speculative research implementations
  - deciding where caches belong
  - reducing hot-path LLM and vector-search cost

Design stance:
- Menhir does not need to implement Hopfield networks, tensor networks, HDC/VSA, or other frontier research architectures to benefit from them.
- The near-term direction is a pragmatic pipeline: exact lookup -> cached summaries -> multi-hierarchy narrowing -> semantic candidate retrieval -> coherence ranking -> LLM synthesis.
- Hierarchy is the cheap filter; coherence scoring is the later ranking layer.
- Expensive reasoning should produce durable artifacts that make the next retrieval cheaper.

Backlog candidates:
- **Multi-hierarchy references**: store each memory once, then attach lightweight references from project, repo, directory, file, symbol, session, time, intent, git commit, test, and incident hierarchies. Do not force one canonical tree.
- **Category summary nodes**: maintain cached summaries and embeddings for useful category nodes such as repo, directory, file, symbol, session, and recent commit range.
- **Candidate-pool cache**: cache common retrieval bundles such as `file -> related memories`, `symbol -> prior decisions`, `test -> likely causes`, and `commit range -> changed symbols/tests`.
- **Coherence ranking pass**: after narrowing, rank candidate memories by temporal consistency, shared symbols, shared Git history, contradiction state, confidence, and prior successful retrievals. Treat this as an energy-style intuition, not a Hopfield implementation requirement.
- **Retrieval artifact ledger**: record what expensive retrieval or synthesis produced, what inputs invalidated it, and which files/symbols/commits it was attached to.
- **Cache invalidation rules**: invalidate or refresh retrieval artifacts when related facts, file fingerprints, symbol outlines, contradiction state, or Git ranges change.
- **Background digestion loop**: refresh summaries, candidate pools, contradictions, and blast-radius snapshots outside the interactive agent path.
- **Hot-path cost budget**: add metrics for which tier answered a query and how often retrieval escalated to vector search, coherence ranking, or LLM synthesis.

Implementation order suggestion:
1. Add low-risk cache artifacts for file/symbol/session summaries.
2. Add multi-hierarchy references as index/display hints, not ontology changes.
3. Add candidate-pool caching for file/symbol/test access paths.
4. Add coherence ranking over already-narrowed candidates.
5. Only revisit frontier research implementations if real scale or quality data proves the current pipeline insufficient.

### `memory.backlog.community`
- source sections: `Community-Derived Priority Backlog (Reddit Scan)`
- use for:
  - external pressure / adoption signals influencing priorities

### `memory.backlog.external_eval`
- source sections: `External Project Evaluation (Adopt / Experiment / Skip)`
- use for:
  - which outside ideas are worth borrowing or rejecting
- current watch list: see `post-v1-todo.md` -> `External Evaluation (Watch List)`
- notable entries (2026-05-06):
  - **regent-vcs/re_gent** — ships our "Conversational git" concept as a standalone Go VCS; `rgt blame` (per-line prompt attribution) has no analog in our system -> Episode-level line attribution follow-up in `git_diff_attachment`
  - **zhangfengcdt/memoir** — semantic path taxonomy on memory nodes; browsability idea -> `canonical_path` property follow-up in `git_diff_attachment`

### `memory.backlog.code_graph`
- source sections: `Code Graph Companion MVP`
- use for:
  - the MVP's original purpose/framing only
  - for phase-by-phase status (done vs. remaining), go to `post-v1-todo.md` -> "Code graph companion
    MVP" / "Code graph Phase 1" — that is the current source of truth, not this section

### `memory.backlog.git_diff_attachment`
- status: **done** (merged 2026-03-21)
- implemented:
  - optional `diff` parameter on `add_memory` and `add_memory_and_track` MCP tools
  - diff stored on episode node, appended to episode body during enrichment via `compose_episode_body()`
  - Graphiti can reason about code changes in context during entity/edge extraction
- remaining work:
  - diff size guard is **half done** (verified 2026-08-09). The *enrichment* half landed in
    `99c9743` (2026-03-21): `MAX_DIFF_CHARS = 50_000` in `services/enrichment_steps.py:157` bounds
    the composed episode body sent to Graphiti, so a huge diff can no longer blow the LLM context
    or inflate token usage. The *storage* half is still open: the raw untruncated `diff` is written
    straight onto the `:Episodic` node at `infrastructure/episode_lifecycle.py:149`, and nothing
    upstream clamps it (`core/backend_runtime_data_ops.py:41` passes it through verbatim). So the
    original wording — "before Neo4j storage" — is still literally accurate. Remaining: bound or
    auto-summarize the persisted property, not just the enrichment body.
  - link diff hunks to file-entity nodes in the code graph
- follow-up ideas:
  - **construction narrative**: treat the sequence of diffs + conversation as a "construction story" — not just *what* the code is, but *how and why* it was built in that order. This gives the memory system a temporal narrative of the project's evolution that plain snapshots miss.
  - **conversation-aware version control**: combine git history with conversational context so the system can answer questions like "why did we change the auth flow last Tuesday?" by linking the diff to the discussion that motivated it.
  - **project structure mapping**: use diffs over time to build and maintain a live map of project structure, hot spots, and ownership patterns in the memory graph.
  - **AI context checkpointing**: save a snapshot of `(conversation messages, graph timestamp, git hash)` at a named point so the AI can be restored to that exact state. "AI state" is really the union of all its inputs — model weights are fixed, so checkpointing means checkpointing what the AI can *see*. The graph layer is already time-travelable via `created_at`/`closed_at` — a checkpoint is just a timestamp filter. The missing piece is raw conversation history serialization (menhir currently distills episodes, not the raw message array). Restore = inject saved messages + scope all recall to `<= checkpoint_timestamp` + `git checkout <hash>`. Branching (try approach A vs B from the same checkpoint) is the most compelling developer use case. Product fit: strong niche for AI coding tools and agentic workflows; weak for consumer chat. Best as an embedded feature ("git for what the AI knew, not just the files it touched") rather than a standalone product. *Externally validated by regent-vcs/re_gent (2026-05-06) — they've shipped a close analog as a standalone Go CLI.*
  - **Episode-level line attribution** *(derived from regent-vcs/re_gent `rgt blame`, 2026-05-06)*: when an episode carries a diff, record which specific hunks were agent-initiated and link them to the episode (and thus the prompt text) that caused them. Graph model: add a `LineAttribution` edge type from `Episode -> File` carrying `hunk_start`, `hunk_end`, `hunk_summary`. Query: "which episode caused this region of this file to change?" This is our graph-native analog of `rgt blame`. Prerequisite: diff hunks must be parsed (not just stored as raw text) and File nodes must already exist in the code graph (done in Phase 0/1). Follow-on: surface attribution in `blast_radius` output so agent can see "this file was last changed by episode X, which was caused by prompt Y."
  - **`canonical_path` property on Entity nodes** *(derived from zhangfengcdt/memoir, 2026-05-06)*: memoir uses dot-notation paths (`profile.professional.skills.python`) as the primary memory key — human-readable, hierarchical, and O(log n) traversable. Our graph is more expressive but less browsable. Consider adding an optional `canonical_path` string property to Entity nodes (e.g., `project.yawn.market.endpoint.price_matrix`) as a human-readable alias alongside the UUID. This enables path-prefix queries ("show me all entities under `project.yawn.market`") and makes the graph inspectable without running Cypher. Low implementation cost — just a computed property from existing label + name + parent edges. Do not change the graph model; treat it as an index/display hint only.

### `memory.backlog.event_history_rollout`
- status: **Phases 1–5 implemented as a default-off production-capable
  path at Menhir `370eff1`** (see `.agent/archive/plans/menhir-event-history-implementation-2026-08-07.md`);
  **not enabled by default**
- done (implemented, flag-dormant until an operator enables them):
  - immutable `TypedEventAssertion`/`EventLane` contract + pure latest/predecessor selector (`domain/event_history.py`)
  - durable append/audit repository with stable source/assertion identities and binding safety (`infrastructure/typed_event_repository.py`)
  - predicate/domain event-lane timeline Views + exact `EVENT_HISTORY_ENTRY` edges on the existing `TimelineKind`; deterministic exact-lane rebuild (`services/event_history_service.py`); `MemoryGraphAdapter` delegates
  - **Phase 3 — perception/admission**: flag-gated shadow event perception with a small generic predicate registry (`purchased/acquired`, then measured additions); preserves exact quotes, distinguishes completed events from intent/hypotheticals; shadow-first, recording abstentions and disagreements; ordering/folding/selection stay deterministic
  - **Phase 4 — recall authority**: detects latest/predecessor temporal queries independently of scalar current-state intent; resolves subject/predicate/domain; applies evidence, time, uniqueness, and foundation gates; serializes a separate event verdict without changing scalar verdict contracts
  - **Phase 5 — transport/lifecycle closeout**: runtime scheduling/manual Phase 3 integration; API/backend/MCP/context transport; bounded metrics; deterministic lane rebuild; namespace cleanup/shared-head lifecycle safety
  - **Recall Lab inspection**: task pages separate Current Scalar, Change Scalar, and Event Scalar roles; show absolute/delta/event derivation; and render grounded event assertions plus ordered occurrence Views
- pending:
  - operator enablement only — the path ships default-off; no default enablement until an operator explicitly turns it on
  - **post-plan rollout hardening**: add event-specific durable repair receipts; render the selected authority verdict alongside the now-visible evidence → event assertion → timeline path; then run a broader stratified temporal-categorical set before any default enablement
- constraints: `valid_at` is the only ordering/selection time (`learned_at` is audit/ingest time only — its sole permitted use is choosing a deterministic representative inside an already-proven exact replay group); exact replay dedups and distinct same-world-time winners fail closed as ambiguous; event siblings never supersede by recency; projection is disposable, durable assertion + evidence is source of truth; no benchmark IDs/answers in production docs beyond the approved plan's acceptance section; no canonical-run gains claimed from infrastructure alone

## Read Next

- Need current runtime behavior -> [architecture.md](architecture.md)
- Need current policy behavior -> [memory-policy.md](memory-policy.md)
- Need the full long-form design context -> [memory-design.md](memory-design.md)
- Need compact future direction -> [memory-futures.md](memory-futures.md)
