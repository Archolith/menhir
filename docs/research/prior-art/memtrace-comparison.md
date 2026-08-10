# MemTrace vs Menhir — Prior-Art / Positioning Comparison

**Date:** 2026-07-09  
**Status:** External comparison note; use for positioning, roadmap triage, and benchmark planning.  
**Compared project:** [`syncable-dev/memtrace-public`](https://github.com/syncable-dev/memtrace-public)  
**Compared project state:** not pinned to a commit/release at review time (curation audit,
2026-08-07) — re-verify claims below against the current repo before relying on them for a
decision; this is a known staleness risk for all comparisons in this cluster.  
**Primary question:** Does MemTrace collapse Menhir's structural-code-memory lane, or does it validate one subsystem while leaving Menhir's broader cognitive-memory lane intact?

---

## 1. Executive verdict

MemTrace is the strongest direct prior art for **structural code memory for AI coding agents**.

It is not just another memory app. It explicitly claims:

- structural memory
- codebase knowledge graph
- function/class/call/import edges
- bi-temporal version history
- time-travel queries
- blast radius / impact analysis
- replay-aware refactors
- MCP tools
- multi-agent session continuity
- local indexing with no LLM calls
- compact source reads and token-savings accounting

This means Menhir should **not** claim "code graph + temporal code history + MCP + blast radius" as a standalone moat.

That lane is already occupied.

Menhir remains differentiated if it centers on:

> governed semantic memory, evidence-backed beliefs, user/agent provenance, contradiction handling, supersession, and why-context across sessions.

MemTrace is a **deterministic structural code-memory engine**. Menhir should be the **agent cognitive-memory and evidence substrate** that can use structural code memory as one projection, not its whole identity.

---

## 2. What MemTrace is claiming

MemTrace presents itself as:

> structural memory for AI coding agents

The public docs/README describe a local system that:

- indexes repositories with Rust + Tree-sitter
- stores symbols and relationships in an embedded graph/vector/full-text database (`MemDB`)
- exposes MCP tools for code search, symbol context, relationships, impact, temporal evolution, API topology, and replay
- supports file watching and incremental re-indexing
- uses local embeddings / reranking rather than LLM extraction
- carries `valid_from` / `valid_to` timestamps for bi-temporal symbol history
- supports multiple agents sharing a workspace owner

Its strongest product claim is speed and deterministic structure:

> parse the codebase once, then let agents query precise structural facts instead of repeatedly grepping and reading files.

This overlaps heavily with Menhir's code-graph companion direction.

---

## 3. Where MemTrace is ahead

### 3.1 Deterministic structural code indexing

MemTrace is far ahead on deterministic code indexing depth.

It claims:

- Rust runtime
- Tree-sitter parsers
- 20+ languages in newer releases
- file/function/class/interface/type/endpoint nodes
- `CALLS`, `IMPLEMENTS`, `IMPORTS`, `EXPORTS`, `CONTAINS` edges
- API topology across services
- framework-aware scanners
- local ONNX embeddings
- BM25 / vector / RRF / cross-encoder rerank
- HNSW vector index
- local embedded storage

Menhir has a structural graph, but it is currently a secondary layer beside semantic memory. MemTrace's structural layer is its product center.

### 3.2 Bi-temporal code history

MemTrace stamps symbols with `valid_from` / `valid_to` tied to git commits or working-tree save episodes.

It exposes:

- `get_evolution`
- `get_timeline`
- `get_changes_since`
- `detect_changes`
- `get_episode_replay`
- `replay_history`

This directly overlaps with Menhir's temporal code-memory ambitions.

Important distinction:

- MemTrace appears to version structural code entities.
- Menhir should version beliefs, evidence, decisions, experience records, and their anchors to code/Git/test artifacts.

### 3.3 Benchmarks and public proof

MemTrace has a public benchmark suite claiming results for:

- exact-symbol lookup
- token economy
- natural-language intent retrieval
- graph query recall against pyright ground truth
- incremental freshness
- PR code review F1
- memory footprint

It also includes honest losses, which makes the benchmark story more credible.

Menhir needs a benchmark story for its own core value. We should not benchmark Menhir against MemTrace on exact-symbol lookup unless Menhir deliberately enters that lane.

### 3.4 MCP surface and agent skills

MemTrace exposes many MCP tools and ships agent skills/workflows, including:

- search/discovery
- relationships
- impact analysis
- code quality
- temporal analysis
- graph algorithms
- API topology
- indexing/watch
- session continuity
- incident investigation
- refactoring guide

This is stronger product packaging than Menhir's current tool surface.

The key product lesson: agents need **workflow-shaped affordances**, not just low-level graph operations.

### 3.5 LeanCTX / context-economy layer

MemTrace's LeanCTX Native is especially relevant.

It adds:

- compressed source reads (`raw`, `lightweight`, `aggressive`, `map`)
- directory-tree maps in a single bounded call
- server-side token-savings ledger
- adaptive mode selection using a Thompson-sampling bandit
- per-call byte counters when explicitly requested

This is directly useful prior art for Menhir's progressive retrieval / hot-path budget direction.

Menhir should borrow the principle:

> every expensive context operation should leave behind a measurable, reusable artifact.

---

## 4. Where Menhir remains different

### 4.1 Menhir is semantic episode memory, not only code structure

Menhir's core loop is:

> episode text -> queue -> LLM extraction -> graph merge -> policy metadata -> structural anchoring

That means Menhir stores what happened in agent/user sessions, not just what exists in the repo.

MemTrace can answer:

> What calls this symbol? What changed in this file? What is the blast radius?

Menhir should answer:

> What did the agent believe when it changed this file? What user correction shaped that belief? What evidence supported it? What later superseded it?

### 4.2 Menhir has governed memory lifecycle

Menhir has memory lifecycle semantics:

- session memories
- persistent memories
- active memories
- compressed memories
- gone/deleted memories
- access-based sharpening
- decay
- conflict detection

MemTrace has temporal structural records, but Menhir is designed to manage durable knowledge and forgetting.

This distinction should be central to positioning.

### 4.3 Menhir has stronger trust and evidence policy

Menhir's artifact/trust direction is deeper than structural history:

- human artifacts are trusted only with evidence
- LLM artifacts are candidate by default
- promotion requires promotable evidence
- LLM self-inference cannot justify trust
- supersession marks artifacts historical rather than deleting them

This is not code indexing. This is belief governance.

MemTrace may know a symbol changed. Menhir should know whether the reason we remember for that change is trusted, candidate, contradicted, or historical.

### 4.4 Menhir can integrate friction and failed attempts

Menhir can ingest and connect:

- agent failures
- user corrections
- command hangs
- repeated tool mistakes
- failed tests
- local runtime issues
- model/provider quirks
- session state
- Git state
- code graph state

MemTrace focuses on structural code memory. Menhir can become experience memory for agents.

This is where PainScan / friction digestion belongs.

---

## 5. Dangerous overlap

### 5.1 "Structural memory" language

MemTrace already owns the phrase "structural memory" publicly.

Menhir should not depend on that phrase as a differentiator.

Better Menhir language:

- evidence-backed agent memory
- cognitive infrastructure
- governed memory substrate
- belief/provenance graph
- agent experience memory
- why-context memory

### 5.2 Temporal code memory

MemTrace already has bi-temporal symbol history and time-travel query language.

Menhir's temporal claim must be broader:

- valid-time of facts and beliefs
- learned-time of memories
- supersession time
- evidence time
- Git/code time
- user-correction time
- agent-action time
- retrieval/use time

The important distinction is not that Menhir has time. It is that Menhir reconciles **multiple kinds of time** across belief, evidence, code, and agent experience.

### 5.3 Blast radius

MemTrace has `get_impact` / blast-radius style tools.

Menhir's version should not stop at structural dependents.

Menhir-shaped blast radius:

> If this file/symbol/test changes, what memories, decisions, known failures, unresolved TODOs, user constraints, prior agent mistakes, and superseded beliefs are impacted?

That is broader than code impact.

### 5.4 Replay

MemTrace has episode replay for code graph state.

Menhir's replay should mean:

- repo state
- graph timestamp
- memory view timestamp
- episode/prompt context
- evidence set
- believed constraints
- tool failures and test outputs
- what the agent could see at that time

Menhir's replay should be "what the agent knew," not only "what the graph looked like."

---

## 6. Borrow list

### 6.1 Compressed source/context reads

Borrow the LeanCTX idea.

Menhir should expose context retrieval modes such as:

- `raw`
- `summary`
- `outline`
- `evidence_only`
- `decision_brief`
- `failure_brief`
- `map`

Each should return size/cost metadata and say what was omitted.

### 6.2 Token/context value ledger

Menhir should add a value ledger, but track Menhir-shaped wins:

- context tokens avoided
- repeated mistake avoided
- stale belief suppressed
- prior decision recovered
- contradiction surfaced
- evidence-backed artifact promoted
- candidate belief withheld from trusted context
- file-context recall found memory vector search missed

This turns memory value into something visible.

### 6.3 Session continuity skill

MemTrace's session-continuity workflow is simple and powerful:

- persist a last-session timestamp
- call `get_changes_since`
- decide whether changes matter
- store the new anchor

Menhir should add a richer version:

- what changed in code
- what changed in memory
- what beliefs were superseded
- what new conflicts emerged
- what failed attempts happened
- what TODOs/constraints are now relevant

### 6.4 Task-shaped MCP tools

Menhir should add higher-level tools that compress multi-step workflows:

- `catch_up_project_context`
- `explain_why_changed`
- `replay_agent_state`
- `find_related_failures`
- `audit_memory_claim`
- `show_belief_lineage`
- `build_decision_brief`
- `surface_stale_context`

### 6.5 Benchmark style

MemTrace's benchmark suite has a useful shape:

- task-specific datasets
- per-query JSONL
- rollup markdown
- fair baselines
- explicit primary axis per benchmark
- honest losses

Menhir should copy the benchmark discipline, not the benchmark target.

---

## 7. Do not copy

### 7.1 Do not rebuild MemTrace's deterministic code index as Menhir's core

Menhir can improve structural parsing, but it should not become a Rust Tree-sitter code graph product unless the strategic goal changes.

If Menhir needs a stronger deterministic code index later, options include:

- integrate with external code-intelligence engines
- build a narrow deterministic parser only for Menhir's anchors
- consume MemTrace/Repowise-style outputs as evidence
- keep Menhir's ontology centered on memory/evidence rather than code nodes

### 7.2 Do not make structural graph accuracy the only success metric

Symbol lookup, call recall, and indexing latency are MemTrace's home field.

Menhir's home field should be:

- correct memory recall across sessions
- prior decision recovery
- contradiction detection
- supersession handling
- evidence-backed trust ranking
- prevention of repeated agent mistakes
- recovery of why-context after time passes

### 7.3 Do not blur semantic memory and structural memory

Structural facts are not the same as semantic beliefs.

Example:

- Structural fact: `auth.py::validate_token` calls `decode_jwt`.
- Semantic belief: "We intentionally kept auth stateless because mobile clients rotate sessions poorly."
- Evidence: user decision, issue link, commit, test, incident log.
- Supersession: "This changed after refresh-token support landed."

MemTrace is strong at the first. Menhir should own the rest.

---

## 8. Recommended Menhir positioning update

### Weak positioning

> Menhir is a graph memory system with code structure, Git awareness, and blast-radius queries.

This is too close to MemTrace.

### Stronger positioning

> Menhir is an evidence-backed memory substrate for agents. It records episodes, decisions, failures, user corrections, code/Git/test anchors, belief lineage, contradictions, and supersession so future agents can recover why work happened and avoid repeating known mistakes.

### Short form

> MemTrace remembers the code structure. Menhir remembers the agent's evolving understanding of it.

### Product contrast

> Git remembers what changed. MemTrace remembers what depends on it. Menhir remembers why the agent believed the change was right.

---

## 9. Near-term roadmap implications

### Immediate

- Add MemTrace to the external-eval watch list.
- Avoid using "structural memory" as the main public differentiator.
- Define Menhir's `why-context` surface concretely.
- Create a benchmark fixture around superseded decision recall and prior failed attempt avoidance.

### Soon

- Add task-shaped MCP tools for decision/failure/evidence context.
- Add compressed/context-mode retrieval for Menhir memory outputs.
- Add context value ledger for memory-specific wins.
- Attach freshness/evidence status to recalled memories.
- Add file/symbol/test-linked failure recall.

### Later

- Consider consuming deterministic code graph outputs from tools like MemTrace/Repowise rather than recreating everything.
- Add replay of "what the agent knew" at a checkpoint: messages, memory timestamp, evidence set, git hash, code graph anchors, and constraints.
- Add background digestion that refreshes summaries, candidate pools, contradictions, and blast-radius memory caches.

---

## 10. Proposed benchmark target

A Menhir benchmark should avoid MemTrace's home field. Suggested benchmark:

### `why_recall_v0`

Fixture structure:

1. Episode A: user makes a design decision with evidence.
2. Commit A changes a file based on that decision.
3. Episode B: later test/incident contradicts or supersedes the decision.
4. Commit B changes related code.
5. Future task asks an agent to modify the original area.

Measure:

- Does the agent retrieve the current decision instead of the superseded one?
- Does it cite the supporting evidence?
- Does it warn about the historical/superseded belief?
- Does it recall prior failed attempts touching the same file/symbol/test?
- Does it produce a safer plan than a baseline agent with only code search?

This would validate Menhir's actual purpose.

---

## 11. Bottom line

MemTrace makes it clear that **structural code memory is no longer a unique idea**.

That is not bad news. It validates that agents need durable code context. But it means Menhir must move up the stack:

- from code graph to belief graph
- from blast radius to memory/evidence blast radius
- from time-travel code to time-travel cognition
- from symbol context to why-context
- from indexing structure to governing what agents know

Durable Menhir claim:

> Agents do not only need to know what the code looks like. They need governed memory of what happened, what was believed, why it was believed, what evidence supported it, what changed, and which lessons should survive into the next session.
