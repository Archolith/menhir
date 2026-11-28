# GitNexus vs Menhir — Prior-Art / Architecture Comparison

**Date:** 2026-11-25  
**Status:** External comparison note (Wave 2 coding-agent & IDE incumbents audit); use for positioning, roadmap triage, and benchmark planning.  
**Pinned version / state:** v1.6.12 (released 2026-09-12; v1.6.13-rc pre-release stream active)  
**License:** PolyForm-Noncommercial-1.0.0 (source-available, noncommercial only; not OSI-approved open source)  
**Evidence tier:** `oss_primary_sources`  
**Menhir baseline:** `v0.3.15` (`S1`–`S8` in `.agent/work-sections.md`)

---

## 1. Executive Verdict

GitNexus (`v1.6.12`, PolyForm Noncommercial 1.0.0) is a specialized, zero-server structural code knowledge graph—not a general agent memory system. It is meaningfully ahead of Menhir v0.3.15 in S5 (14-language Tree-sitter parsing, Leiden clustering, precomputed execution flows, `detect_changes` diff-to-flow mapping, semantic `rename`, `PreToolUse` search augmentation) and S7 (embedded LadybugDB/KùzuDB zero-server storage with browser WASM), but has zero episodic/decision memory (S1/S2 N/A), weaker retrieval governance/auth/ops (S3/S4/S6 behind), and an unverified net-accuracy case for coding agents after Issue #3500 showed no measurable accuracy gain and higher token cost on v1.6.12.

## 2. Architecture Summary

GitNexus is a zero-server TypeScript/ESM code intelligence engine (`gitnexus` CLI/npm/Docker + browser WASM explorer) that parses repositories with Tree-sitter across 14 languages into an embedded LadybugDB (formerly KùzuDB) property graph with native vector and BM25/FTS indexes. It runs Leiden community clustering, traces multi-step execution flows ('processes'), and optionally generates local 384-dim embeddings via transformers.js (`Snowflake/snowflake-arctic-embed-xs`, AST-boundary chunking, 50k-node default cap) fused with BM25 via RRF. It exposes 8 core MCP tools (`list_repos`, `query`, `context`, `impact`, `trace`, `detect_changes`, `rename`, `cypher`), `gitnexus://repo/<name>/context` resources, a `~/.gitnexus/registry.json` multi-repo registry, `gitnexus analyze` incremental indexing/watch mode, `gitnexus wiki` LLM doc generation, and Claude Code `PreToolUse`/`PostToolUse` hooks. It has no episodic, conversational, or decision memory layer.

## 3. Section-by-Section Comparison (`S1`–`S8`)

Ratings are relative to Menhir `v0.3.15` (`[AHEAD]` = competitor ahead of Menhir, `[PARITY]` = roughly comparable, `[BEHIND]` = competitor behind Menhir, `[N/A]` = out of competitor scope).

| Section | Assessment |
|---|---|
| **S1. Ingest & Retain** | [N/A] GitNexus ingests only repository source code and git diffs; it has no episodic/conversation intake, TurnEvidence hooks, secret scrubbing, retention/decay pins, or provenance-aware erasure. |
| **S2. Typed State & Consolidation** | [N/A] GitNexus stores static AST symbols, edges, clusters, and execution flows only; it has no scalar perception, immutable TypedAssertion log, Event->Fold->View consolidation, anchored time, or experience counters. |
| **S3. Retrieve & Decide** | [BEHIND] GitNexus pairs BM25 with local transformers.js embeddings (`Snowflake/snowflake-arctic-embed-xs`, 50k-node default cap) via RRF and process-grouped `query`/`context` tools, but lacks cross-encoder reranking, multi-oracle log-space fusion, fail-closed wardens, scalar authority separation, and episodic/timeline recall. |
| **S4. Govern, Curate & Track Work** | [BEHIND] GitNexus offers LLM-generated `.gitnexus/wiki/` docs and prompt-level skills (`gitnexus-plan`/`gitnexus-work`), but lacks promotion tiers, provenance/conflict review inboxes, first-class WorkArtifact state machines, and graph-linked code TODOs. |
| **S5. Code Graph & Impact** | [AHEAD] GitNexus leads Menhir on pure code-graph breadth with 14-language Tree-sitter parsing, Leiden clustering, precomputed execution-flow tracing (`trace`/`query`), depth-grouped `impact`, live `detect_changes` diff mapping, semantic `rename`, raw `cypher`, and `PreToolUse` augmentation; Menhir still leads on coverage-aware `affected_tests`, fail-closed unindexed-path refusal, and `ANCHORED_TO` narrative/decision links. |
| **S6. Interfaces, Auth & Operations** | [BEHIND] GitNexus provides CLI, stdio/HTTP MCP (8 core tools + `gitnexus://` resources), `gitnexus serve` + browser WASM explorer, and editor hooks, but lacks embedded OAuth 2.1 (static `--auth-token` bearer only for non-loopback binds), REST/Hermes parity, SQLite-leased scheduling, and recoverable sagas. |
| **S7. Platform, Storage & Release** | [AHEAD] GitNexus embeds LadybugDB (formerly KùzuDB) with native vector + FTS indexes locally and in browser WASM via npm/Docker, avoiding Menhir's external Neo4j 5.26+ server requirement (until #109 FalkorDBLite ships), though its PolyForm-Noncommercial-1.0.0 license restricts commercial adoption. |
| **S8. Research & Eval Discipline** | [BEHIND] GitNexus lacks evidence-gated feature retirement and falsification harnesses comparable to Menhir's #399 and `archolith-bench`; external evaluation in Issue #3500 found v1.6.12 produced no measurable coding-agent accuracy gain while increasing token cost and latency. |

## 4. Benchmarks Audited

- VERIFIED ISSUE / EMPIRICAL COUNTER-EVALUATION: `abhigyanpatwari/GitNexus` Issue #3500 ('In our evaluation, GitNexus 1.6.12 showed no measurable accuracy gain for coding agents and usually increased their cost') — paired hidden-test runs on v1.6.12 reported no measurable accuracy lift and higher mean token cost and wall-clock duration vs. non-MCP baselines.
- VERIFIED REPO PERF / SCALING LOGS: GitHub issues document 30s Go scope-extraction parser timeouts, synthetic massive-graph indexing optimizations reducing runtime from timeout to ~15s, and a 50,000-node default cap on in-memory embedding generation.
- INDEPENDENT COMPARATIVE INDEXING HARNESSES: Community cold/warm/incremental wall-clock benchmarks compare GitNexus against Graphify and CodeGraph; results vary by repo size and embedding settings.
- UNVERIFIED VENDOR / THIRD-PARTY CLAIMS: Blog/marketing claims of higher DeepSWE pass rates and drastically reduced token spend are not backed by reproducible primary-source eval tables in the GitNexus repo and are directly contested by Issue #3500.

## 5. Where GitNexus Is Ahead vs. Behind

### Ahead of Menhir
- 14-language deterministic Tree-sitter AST parsing (TS, JS, Python, Java, Kotlin, C#, Go, Rust, PHP, Ruby, Swift, C, C++, Dart) vs. Menhir's Python-strongest repo scanner.
- Zero-server embedded graph database (LadybugDB / formerly KùzuDB) with native vector + BM25/FTS indexes running locally and via browser WASM, eliminating an external Neo4j 5.26+ server dependency.
- Leiden community clustering and precomputed multi-step execution-flow ('process') tracing from entry points, exposed via `query`, `context`, and `trace` (shortest path) MCP tools.
- Live `detect_changes` MCP tool mapping uncommitted `git diff` / branch diffs directly onto impacted symbols and execution flows, plus graph-aware semantic `rename` with `dry_run` preview and raw `cypher` access.
- Editor lifecycle integration via `PreToolUse` (`gitnexus augment` enriching Grep/Glob/Bash searches) and `PostToolUse` staleness detection hooks.

### Behind Menhir
- Zero episodic, conversational, or decision memory: no episode intake, TurnEvidence hooks, secret scrubbing, retention/decay pins, or provenance-aware erasure (S1).
- Zero typed scalar state or consolidation: no span-grounded scalar perception, immutable TypedAssertion log, deterministic Event->Fold->View state folding, or anchored temporal queries (S2).
- No cross-encoder reranking, multi-oracle log-space fusion, fail-closed wardens, or non-interleaved scalar authority layer; retrieval is code-only BM25 + dense RRF (S3).
- No memory promotion tiers, conflict resolution inbox, WorkArtifact lifecycle state machine, or graph-linked code TODOs (S4).
- Lacks Menhir's coverage-aware `affected_tests`, fail-closed refusal to declare 'safe' on unindexed paths (epistemic lower-bounds remain an open concern), and `ANCHORED_TO` narrative/decision links (S5).
- No OAuth 2.1 (static `--auth-token` bearer only for non-loopback binds), no REST/Hermes parity, no SQLite-leased scheduler, and no recoverable sagas (S6).
- Restrictive `PolyForm-Noncommercial-1.0.0` license prohibits commercial use without a separate license (S7).
- Lacks Menhir's evidence-gated retirement discipline; Issue #3500 empirically challenged v1.6.12 agent accuracy and token-cost claims (S8).

## 6. What Menhir Should Borrow

- Expand Menhir's deterministic code scanner beyond Python using Tree-sitter multi-language grammars, preserving Menhir's fail-closed unindexed-path refusal and coverage-aware `affected_tests`.
- Add a `detect_changes`-style MCP tool that maps uncommitted `git diff` hunks directly to Menhir's code graph, impacted execution flows, and `ANCHORED_TO` decision memories.
- Adopt precomputed entry-point execution flows and Leiden architectural clustering for compact structural summaries, while empirically gating any `PreToolUse` search-augmentation hook against token-cost regressions like GitNexus Issue #3500.
- Accelerate #109 (embedded FalkorDBLite) to match GitNexus's zero-server local onboarding ergonomics without sacrificing Menhir's permissive licensing or full episodic+typed memory stack.

## 7. Novelty Claims Menhir Cannot Make

- Menhir cannot claim novelty for MCP-exposed deterministic Tree-sitter code graphs, depth-grouped blast-radius (`impact`) queries, or git-diff-to-graph impact mapping (`detect_changes`).
- Menhir cannot claim novelty for zero-server embedded graph DBs with native vector + BM25 hybrid search for code intelligence (GitNexus ships LadybugDB/KùzuDB locally and in browser WASM).
- Menhir cannot claim novelty for `PreToolUse` / `PostToolUse` coding-agent hooks that augment Grep/Glob searches or flag index staleness after git mutations.

## 8. Primary Sources

- https://github.com/abhigyanpatwari/GitNexus
- https://github.com/abhigyanpatwari/GitNexus/releases
- https://github.com/abhigyanpatwari/GitNexus/issues/3500
- https://www.npmjs.com/package/gitnexus
