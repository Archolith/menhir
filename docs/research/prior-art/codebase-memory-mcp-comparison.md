# codebase-memory-mcp vs Menhir — Prior-Art / Architecture Comparison

**Date:** 2026-11-25  
**Status:** External comparison note (Wave 2 coding-agent & IDE incumbents audit); use for positioning, roadmap triage, and benchmark planning.  
**Pinned version / state:** v0.12.1 (2026-10-16); arXiv:2603.27277 (2026-03)  
**License:** MIT  
**Evidence tier:** `oss_primary_sources`  
**Menhir baseline:** `v0.3.15` (`S1`–`S8` in `.agent/work-sections.md`)

---

## 1. Executive Verdict

`codebase-memory-mcp` (v0.12.1) is a category-leading, zero-dependency C11/SQLite structural code-graph MCP server that clearly outpaces Menhir v0.3.15 on S5 (multi-language Tree-sitter + Hybrid LSP code graph, cross-service edges, runtime trace overlay, sub-ms Cypher/BFS queries) and S7 (single-binary embedded SQLite deployment), but it is NOT an episodic, temporal, or governed agent-memory system—leaving Menhir decisively ahead across S1–S4 and S6.

## 2. Architecture Summary

`DeusData/codebase-memory-mcp` is an MIT-licensed, local-first MCP server compiled into a single statically linked C11 binary with zero external runtime dependencies and an embedded SQLite (WAL mode) graph store under `~/.cache/codebase-memory-mcp/`. It runs a multi-phase Tree-sitter indexing pipeline (158 vendored languages) augmented by an in-binary Hybrid LSP type-resolution layer for 9 major languages, Louvain community detection, cross-service protocol edge extraction (HTTP/gRPC/GraphQL/tRPC), infrastructure/Markdown parsing, and optional runtime trace ingestion (`ingest_traces`). A background daemon runs a content-hash file/git watcher for incremental updates and serves an optional local web UI (CBM Lens, e.g., `127.0.0.1:9749`), while coding agents connect over `stdio` to ~17 deterministic tools (`index_repository`, `search_graph`, `query_graph`, `trace_path`, `get_architecture`, `detect_changes`, `compare_graphs`, `check_index_coverage`, `manage_adr`, `semantic_query`, etc.). Despite "memory" in its name, it is a structural code-intelligence and ADR graph—not an episodic, conversational, or typed-scalar memory system.

## 3. Section-by-Section Comparison (`S1`–`S8`)

Ratings are relative to Menhir `v0.3.15` (`[AHEAD]` = competitor ahead of Menhir, `[PARITY]` = roughly comparable, `[BEHIND]` = competitor behind Menhir, `[N/A]` = out of competitor scope).

| Section | Assessment |
|---|---|
| **S1. Ingest & Retain** | [BEHIND] Ingests only repository source code, infrastructure configs, Markdown ADRs, and optional runtime traces (`ingest_traces`) via content-hash incremental sync. It lacks conversational episode intake, TurnEvidence hooks, secret scrubbing, canonical self binding, retention/decay pins, and provenance-aware erasure/re-derivation. |
| **S2. Typed State & Consolidation** | [N/A] Persists a deterministic static-plus-trace code graph in SQLite rather than evolving conversational state. It has no K-sample scalar perception, immutable TypedAssertion log, Event->Fold->View projections, counting consolidation, or anchored-time experience counters. |
| **S3. Retrieve & Decide** | [BEHIND] Delivers fast zero-LLM structural retrieval (`search_graph`, openCypher-subset `query_graph`, BFS `trace_path`, `search_code`, and local-embedding `semantic_query`), which excels at code topology. However, it lacks dual-lane episodic/entity retrieval, cross-encoder reranking, read-only Oracles/LogSpaceOracleCombiner, fail-closed Wardens, ScalarAuthorityLayer, and temporal history/timeline recall. |
| **S4. Govern, Curate & Track Work** | [BEHIND] Provides `manage_adr` for Markdown Architecture Decision Record CRUD linked into the code graph, giving lightweight design-intent persistence across sessions. It lacks Menhir's CANDIDATE/PERSISTENT/PROMOTED governance tiers, provenance inspection, conflict detection/resolution inbox, WorkArtifact lifecycle state machine with Git reconciliation, and graph-linked code TODOs. |
| **S5. Code Graph & Impact** | [AHEAD] Outperforms Menhir's Python-strongest scanner and 30-min watcher with a C11 binary supporting 158 Tree-sitter languages, Hybrid LSP type resolution for 9 major languages, cross-service HTTP/gRPC/GraphQL/tRPC edges, runtime trace ingestion (`ingest_traces`), Louvain communities, `compare_graphs`, `check_index_coverage`, and continuous file/git-watcher incremental sync. Its `detect_changes` blast-radius/centrality-risk scoring and dead-code Cypher queries surpass Menhir on pure code intelligence, though Menhir retains unique `ANCHORED_TO` episodic/narrative memory links. |
| **S6. Interfaces, Auth & Operations** | [BEHIND] Exposes ~17 local `stdio` MCP tools, a background watcher daemon, and the local CBM Lens web UI (`127.0.0.1:9749`). It lacks Menhir's Streamable HTTP MCP transport, 68 profiled tools + 9 resources, REST/Hermes API, embedded OAuth 2.1 (PKCE S256, CIMD/DCR, rotating refresh, digest-bound policy), SQLite-leased scheduler, and recoverable sagas. |
| **S7. Platform, Storage & Release** | [AHEAD] Ships as a single zero-dependency C11 static binary backed by embedded SQLite (WAL mode) with cross-platform installers and npm/PyPI/Go wrappers, eliminating Menhir's external Neo4j 5.26+ requirement (pending Menhir #109 FalkorDBLite). Menhir remains ahead on hash-verified wheelhouse/Docker supply-chain discipline and centralized feature-flag SSOT. |
| **S8. Research & Eval Discipline** | [PARITY] Publishes an open arXiv preprint (`arXiv:2603.27277`, March 2026) over 31 repositories candidly reporting an accuracy-vs-token tradeoff (83% vs. 92% accuracy at ~10x fewer tokens and 2.1x fewer tool calls), though marketing headlines emphasize best-case ~99% token savings. Unlike Menhir's evidence-gated retirement ledger (#399) and LongMemEval/archolith-bench falsification, it has no memory-benchmark evaluation or feature-ablation retirement program. |

## 4. Benchmarks Audited

- PUBLISHED PREPRINT — arXiv:2603.27277 (Vogel et al., March 2026), 'Codebase-Memory: Tree-Sitter-Based Knowledge Graphs for LLM Code Exploration via MCP': evaluated across 31 real-world repositories; reported ~83% answer quality vs. ~92% for exhaustive file-exploration agents (~9 pp accuracy tradeoff) using ~10x fewer tokens and 2.1x fewer tool calls; matched/beat file explorers on graph-native hub/caller tasks in 19/31 languages.
- VENDOR / HEADLINE CLAIM (UNVERIFIED IN GENERAL WORKFLOWS) — '~99% / 99.2% token reduction' (~3,400 vs. ~412,000 tokens): reflects best-case structural queries against naive exhaustive grep-and-read loops; independent developer reports cite ~40-50% token reduction and ~2x faster sessions in day-to-day coding.
- SELF-REPORTED PERFORMANCE CLAIM — Sub-millisecond SQLite graph queries and indexing the ~28M-LOC Linux kernel in minutes: vendor/preprint throughput numbers without an independent third-party benchmark harness.
- MEMORY BENCHMARKS — None published (no LongMemEval, LoCoMo, or episodic/state-tracking evaluation; out of scope for the project).

## 5. Where codebase-memory-mcp Is Ahead vs. Behind

### Ahead of Menhir
- Zero-dependency C11 static binary + embedded SQLite WAL backend eliminates external graph DB ops (vs. Menhir's current Neo4j 5.26+ requirement prior to #109 FalkorDBLite).
- 158 vendored Tree-sitter languages plus in-binary Hybrid LSP type resolution across 9 major languages (Go, C, C++, Python, TypeScript, JavaScript, C#, Java, Rust).
- Cross-service edge extraction (HTTP routes, gRPC, GraphQL, tRPC), infrastructure/doc graph integration (Dockerfiles, K8s, HCL, Markdown), and runtime trace overlay via `ingest_traces`.
- Zero-LLM sub-ms structural graph queries including read-only openCypher subset (`query_graph`), BFS `trace_path`, Louvain community detection, `compare_graphs`, and centrality-weighted `detect_changes` risk scoring.
- Continuous event-driven file/git watcher with content-hash incremental sync (vs. Menhir's 30-min watcher interval).

### Behind Menhir
- No episodic, conversational, or temporal-biographical memory: no episode intake, TurnEvidence hooks, secret scrubbing, canonical self binding, decay/retention pins, or provenance-aware erasure/re-derivation.
- No typed scalar state layer: lacks K-sample span-grounded scalar perception, immutable TypedAssertion logs, Event->Fold->View state views, counting consolidation, or anchored-time experience tracking.
- No multi-lane episodic/hybrid retrieval decision stack: lacks cross-encoder reranking, read-only Oracles/LogSpaceOracleCombiner, fail-closed Wardens, ScalarAuthorityLayer, or `recall_history`/`recall_timeline`.
- No memory governance or work-tracking lifecycle: lacks CANDIDATE/PERSISTENT/PROMOTED tiers, conflict resolution inbox, provenance inspection, WorkArtifact state machine with Git reconciliation, and graph-linked code TODOs.
- Local-only stdio transport and minimal auth/ops: lacks Streamable HTTP MCP, embedded OAuth 2.1 (PKCE S256, CIMD/DCR, rotating refresh, digest-bound policy), REST/Hermes API, SQLite-leased scheduler, and recoverable sagas.

## 6. What Menhir Should Borrow

- Adopt an explicit `check_index_coverage` diagnostic and evidence-tier contract (requiring source-snippet verification before exhaustive dead-code or zero-impact claims) to complement Menhir's fail-closed `blast_radius` refusal on unindexed paths.
- Extract cross-service protocol edges (HTTP routes, gRPC, GraphQL) and optional runtime trace overlays (`ingest_traces`) so Menhir's `blast_radius` and `ANCHORED_TO` links span multi-service boundaries rather than stopping at single-repo ASTs.
- Add `compare_graphs` (snapshot diffing across commits) and centrality-weighted risk tiers to `blast_radius`/`detect_changes` outputs for sharper pre-edit risk triage.
- Accelerate Menhir #109 (embedded graph backend) and event-driven content-hash incremental watching to close the deployment-friction and freshness gap against single-binary SQLite code graphs.

## 7. Novelty Claims Menhir Cannot Make

- Menhir cannot claim novelty for zero-LLM sub-ms SQLite-backed code-graph MCP servers, Tree-sitter AST call/impact graphs, git-diff blast-radius (`detect_changes`) with index-coverage diagnostics (`check_index_coverage`), or cross-service HTTP/gRPC/GraphQL edge tracing.
- Menhir cannot claim novelty for linking Markdown Architecture Decision Records (ADRs) into a code knowledge graph (`manage_adr`) or running a local visual graph explorer alongside an MCP code-graph daemon (CBM Lens).
- Menhir's defensible novelty remains in combining code-graph grounding (`ANCHORED_TO`) with governed episodic/temporal memory, typed scalar Event->Fold->View state, fail-closed Oracles/Wardens, WorkArtifact lifecycles, and embedded OAuth 2.1 multi-surface operations—none of which `codebase-memory-mcp` implements.

## 8. Primary Sources

- https://github.com/DeusData/codebase-memory-mcp
- https://github.com/DeusData/codebase-memory-mcp/releases
- https://github.com/DeusData/codebase-memory-mcp/blob/main/LICENSE
- https://arxiv.org/abs/2603.27277
