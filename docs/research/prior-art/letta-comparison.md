# Letta (formerly MemGPT) + Sleep-Time Compute vs Menhir — Prior-Art / Architecture Comparison

**Date:** 2026-11-25  
**Status:** External comparison note (Wave 1 prior-art audit); use for positioning, roadmap triage, and benchmark planning.  
**Pinned version / state:** letta-ai/letta (v0.16.x / V1 server archived) & letta-ai/letta-code (v0.34.x active harness); MemGPT arXiv:2310.08560; Sleep-Time Compute arXiv:2504.13171; audited 2026-11  
**License:** Apache-2.0  
**Evidence tier:** `oss_primary_sources`  
**Menhir baseline:** `v0.3.15` (`S1`–`S8` in `.agent/work-sections.md`)

---

## 1. Executive Verdict

Letta is the canonical prior art for OS-style tiered agent memory and asynchronous background consolidation ('sleep-time compute'), and in 2026 has evolved into a highly ergonomic Git-backed memory harness (Letta Code / MemFS) with strong deployment, packaging, and evaluation infrastructure. However, Letta's memory consolidation remains fundamentally LLM-driven prose/Markdown rewriting rather than Menhir's deterministic Event->Fold->View typed state machine, leaving Letta materially behind Menhir on provenance-aware erasure/re-derivation, fail-closed retrieval governance (Oracles/Wardens/ScalarAuthorityLayer), deterministic code-graph blast radius, and structured work-artifact governance.

## 2. Architecture Summary

Letta evolved from the UC Berkeley MemGPT paper (arXiv:2310.08560) into two primary Apache-2.0 codebases: the legacy/archived Letta V1 server (letta-ai/letta) and the active memory-first coding harness (letta-ai/letta-code). Its classic architecture models LLM context as OS virtual memory across three tiers—pinned in-context Core Memory blocks (agent-editable via memory_insert/memory_replace/memory_rethink, or read_only), Recall Memory (searchable conversation history), and Archival Memory (vector-backed long-term passages via SQLite or Postgres+pgvector). Sleep-Time Compute (arXiv:2504.13171) decouples online inference from offline consolidation by pairing a primary agent with a background sleep-time agent (configured via enable_sleeptime and sleeptime_agent_frequency) that asynchronously rewrites shared memory blocks during idle periods. In 2026, Letta Code extended this into Context Repositories (MemFS): Git-backed local Markdown filesystems using progressive disclosure (root files loaded into system prompt, deeper files read via grep/find) and Git worktrees so parallel background subagents can dream, initialize (/init), and merge memory updates with Git commit history.

## 3. Section-by-Section Comparison (`S1`–`S8`)

Ratings are relative to Menhir `v0.3.15` (`[AHEAD]` = competitor ahead of Menhir, `[PARITY]` = roughly comparable, `[BEHIND]` = competitor behind Menhir, `[N/A]` = out of competitor scope).

| Section | Assessment |
|---|---|
| **S1. Ingest & Retain** | [BEHIND] Letta automatically logs recall history, inserts archival vector passages, and in Letta Code adds strong execution-time secret injection ($SECRET_NAME) with stdout/stderr redaction (NAME=<REDACTED>). However, it lacks Menhir's two-tier canonical self binding, decay/retention pins, and provenance-aware erasure with automatic derived re-derivation—deleting an archival passage via API does not deterministically purge claims already rewritten into Core blocks or MemFS Markdown. |
| **S2. Typed State & Consolidation** | [BEHIND] Letta's Sleep-Time Compute (enable_sleeptime, sleeptime_agent_frequency, memory_rethink) and MemFS dreaming perform non-deterministic LLM prose/Markdown rewriting in background agents or Git worktrees. Unlike Menhir's K-sample span-grounded scalar perception, immutable TypedAssertion log, deterministic Event->Fold->View views, and counting consolidation, Letta has no typed scalar state machine or deterministic time/count fold. |
| **S3. Retrieve & Decide** | [BEHIND] Letta relies on pinned in-context Core/root MemFS files paired with agent-invoked semantic vector search (archival_memory_search), conversation_search, and iterative CLI grep/find over MemFS. It lacks Menhir's dual-lane fusion, local ONNX cross-encoder reranking, read-only Oracles with LogSpaceOracleCombiner, fail-closed Wardens, and separate non-interleaved ScalarAuthorityLayer. |
| **S4. Govern, Curate & Track Work** | [BEHIND] Letta offers read_only Core blocks, Git-versioned MemFS commit trails, worktree merge conflict handling, and a /doctor command for bloat/drift auditing. It does not implement Menhir's CANDIDATE/PERSISTENT/PROMOTED tiers, structured conflict-resolution inbox, first-class WorkArtifact lifecycle state machine with Git reconciliation, or graph-linked code TODOs. |
| **S5. Code Graph & Impact** | [BEHIND] Letta Code uses standard filesystem search, /init repository scanning, and GitHub Actions (letta-code-action), relying on optional external MCP servers (e.g., Neo4j) for AST or dependency graph analysis. It has no native deterministic repo scanner, incremental watcher, coverage-aware blast_radius/affected_tests that refuses 'safe' on unindexed paths, or ANCHORED_TO stale-file advisories. |
| **S6. Interfaces, Auth & Operations** | [AHEAD] Letta delivers a broader operational surface than Menhir, including hosted MCP (api.letta.com/mcp), external HTTP/SSE/stdio MCP tool integration, Python/TS SDKs, ADE visual debugger, portable Agent File (.af) export/import, and CLI/web/Slack/Discord channels. Self-hosted auth is simpler than Menhir's embedded OAuth 2.1 (relying on LETTA_SERVER_PASSWORD/bearer tokens, with OAuth mainly for Cloud/BYOM/provider flows), but overall interface maturity and portability are ahead. |
| **S7. Platform, Storage & Release** | [AHEAD] Letta runs locally on lightweight SQLite (sqlite.db) or Git-backed MemFS and scales to PostgreSQL + pgvector (LETTA_PG_URI) without requiring a heavy graph database like Neo4j 5.26+, distributed via PyPI, npm (@letta-ai/letta-code), Docker, and Letta Cloud. While it lacks Menhir's hash-verified offline wheelhouse discipline and single-SSOT feature flags, its storage ergonomics and release ecosystem are markedly ahead. |
| **S8. Research & Eval Discipline** | [AHEAD] Letta established the MemGPT (arXiv:2310.08560) and Sleep-Time Compute (arXiv:2504.13171) research lineage, open-sourced letta-evals, Context-Bench, and the Letta Leaderboard, and published falsification critiques like its 74.0% LoCoMo flat-filesystem baseline. However, its sleep-time accuracy/compute headlines are demonstrated on synthetic stateful math benchmarks rather than LongMemEval-S full-haystack memory updates, leaving Menhir's evidence-gated feature retirement discipline distinct. |

## 4. Benchmarks Audited

- Sleep-Time Compute (arXiv:2504.13171, published preprint): Reports ~5x reduction in test-time compute for target accuracy and +13% (Stateful GSM-Symbolic) to +18% (Stateful AIME) accuracy gains from offline compute scaling, plus 2.5x lower amortized cost per query on Multi-Query GSM-Symbolic. Caveat: synthetic math/reasoning benchmarks with high query predictability, not longitudinal conversational memory benchmarks like LongMemEval.
- LoCoMo Filesystem Baseline (Letta blog, 'Is a Filesystem All You Need?'): Reports 74.0% accuracy using a minimalist flat filesystem + grep/file tools in an agentic loop, used by Letta to critique fragile memory benchmarks rather than claim a specialized graph/vector breakthrough.
- Terminal-Bench (Letta engineering report): Reports 42.5% overall score for Letta's open-source stateful terminal agent (ranked 4th overall / 1st open-source at publication time using Claude 4 Sonnet). Measures CLI/containerized engineering tasks, not memory accuracy.
- Context-Bench & Letta Leaderboard (letta-ai/letta-evals, leaderboard.letta.com): Open-source harness measuring agentic context engineering (filesystem chaining, entity tracing, skill loading) via Pass@1, recall, and precision; primarily evaluates LLM tool-use/memory-management capability rather than comparing memory storage engines on LongMemEval-S.
- LongMemEval: No official primary-source Letta headline score on LongMemEval-S full-haystack was found; third-party comparisons frequently cite LoCoMo instead.

## 5. Where Letta (formerly MemGPT) + Sleep-Time Compute Is Ahead vs. Behind

### Ahead of Menhir
- OS-style tiered memory UX and progressive disclosure (in-context Core/root MemFS blocks vs on-demand Recall/Archival/dormant files) with zero Neo4j dependency (SQLite, Postgres+pgvector, Git).
- Asynchronous Sleep-Time Compute decoupling foreground latency from offline memory refactoring, plus Git worktree-isolated parallel background consolidation (/init, /sleeptime) in MemFS.
- Mature multi-surface ecosystem: ADE visual debugger, portable Agent File (.af) serialization, Letta Code terminal harness with cross-harness Agent Skills, GitHub Actions (letta-code-action), and hosted MCP endpoint.
- Runtime secret placeholder injection ($SECRET_NAME) with automatic stdout/stderr redaction so plaintext credentials never enter LLM context or transcripts.
- Public evaluation ecosystem (letta-evals, Context-Bench, Letta Leaderboard) and peer-reviewed/preprint research lineage (MemGPT arXiv:2310.08560, Sleep-Time Compute arXiv:2504.13171).

### Behind Menhir
- No immutable TypedAssertion log or deterministic Event->Fold->View state machine; sleep-time consolidation relies on non-deterministic LLM prose/markdown rewriting rather than span-grounded scalar folding, counting consolidation, or anchored time algebra.
- No provenance-aware erasure with automatic derived re-derivation; deleting an archival passage or message does not deterministically purge or recompute facts already rewritten into Core blocks or MemFS Markdown.
- No multi-oracle retrieval governance, local ONNX cross-encoder reranker, fail-closed Wardens, or separate non-interleaved ScalarAuthorityLayer; retrieval relies on semantic vector search or iterative agentic grep loops.
- No native deterministic code graph, incremental repo watcher, or coverage-aware blast_radius/affected_tests (external AST/Neo4j MCP servers are optional add-ons).
- No first-class WorkArtifact lifecycle state machine with Git reconciliation, structured conflict-resolution inbox, promotion tiers (CANDIDATE/PERSISTENT/PROMOTED), or embedded OAuth 2.1 (PKCE S256/CIMD/DCR) for self-hosted servers.

## 6. What Menhir Should Borrow

- Execution-time secret placeholder substitution ($SECRET_NAME) paired with automatic tool-output redaction so plaintext credentials never enter TurnEvidence or memory transcripts.
- A /doctor diagnostic command to audit memory bloat, duplicate/drifted facts, and token budget pressure across pinned/promoted state.
- Portable single-file agent/memory state export/import (analogous to Letta's .af Agent File) for reproducible backups, migrations, and benchmark snapshots.
- Progressive disclosure tiering (compact always-on summary index with explicit signposts to dormant detail) for context packing in coding-agent sessions.
- Using Letta's flat-filesystem + grep baseline (LoCoMo 74.0%) as a mandatory sanity-check baseline in archolith-bench so complex memory machinery must beat simple grep-over-logs.

## 7. Novelty Claims Menhir Cannot Make

- OS-style tiered memory (in-context core/pinned memory vs searchable recall history vs external archival store) — established by MemGPT (arXiv:2310.08560).
- Asynchronous offline/background memory consolidation ('sleep-time compute' / 'dreaming') decoupled from foreground user turns — established by Letta's Sleep-Time Compute (arXiv:2504.13171) and enable_sleeptime architecture.
- Git-backed agent memory filesystems using worktrees for parallel background consolidation and merge-based history — shipped in Letta Code Context Repositories (MemFS).
- Runtime secret placeholder injection and tool-output scrubbing to keep credentials out of agent memory/transcripts — shipped in Letta Code Secrets.
- Agentic filesystem + grep retrieval as a competitive memory baseline — published by Letta on LoCoMo (74.0%).

## 8. Primary Sources

- https://github.com/letta-ai/letta
- https://github.com/letta-ai/letta-code
- https://docs.letta.com
- https://arxiv.org/abs/2310.08560
- https://arxiv.org/abs/2504.13171
- https://github.com/letta-ai/letta-evals
- https://leaderboard.letta.com
