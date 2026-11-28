# Supermemory vs Menhir — Prior-Art / Architecture Comparison

**Date:** 2026-11-25  
**Status:** External comparison note (Wave 1 prior-art audit); use for positioning, roadmap triage, and benchmark planning.  
**Pinned version / state:** API v5 (Oct 2026; v3/v4 sunset Dec 31, 2026) / supermemory-server Lite (Oct 2026); audited 2026-11-25  
**License:** Mixed: MIT for public monorepo, SDKs, plugins, and memorybench; proprietary closed-source for cloud backend and compiled supermemory-server binary (Lite license capped at 10,000 documents; Scale/Enterprise for unconstrained self-hosting)  
**Evidence tier:** `partial_public`  
**Menhir baseline:** `v0.3.15` (`S1`–`S8` in `.agent/work-sections.md`)

---

## 1. Executive Verdict

Supermemory is a strong commercial-first memory platform that leads Menhir v0.3.15 in deployment ergonomics (single-binary local server with embedded PGlite/WASM embeddings plus Cloudflare edge SaaS), turnkey SaaS connectors, always-on static/dynamic User Profiles, and published full-haystack LongMemEval-S benchmarking via its open MemoryBench harness. However, because its core extraction, graph, decay, and retrieval engine is closed-source (and its local binary is Lite-capped at 10,000 docs), Supermemory falls behind Menhir on auditability, deterministic typed scalar state (Event->Fold->View), fail-closed Oracle/Warden retrieval governance, conflict-inbox curation, WorkArtifact tracking, multi-source re-derivation on erasure, and deterministic AST code-graph impact analysis.

## 2. Architecture Summary

Verified OSS/API contracts: Supermemory API v5 (Oct 2026; v3/v4 sunset Dec 31, 2026) exposes namespace-scoped REST endpoints (/ns/{namespace}/search, /ns/{namespace}/profile, /ns/{namespace}/memories, /ns/{namespace}/memories/semantic, DELETE /ns/{namespace}) with default hybrid (vector + lexical) search across memories and raw chunks, optional rewriteQuery, cross-encoder rerank (none/order/aggregate), dual temporal fields (documentDate, eventDate), isLatest versioning, forgetAfter ISO expiry, and cascading forget of derived memories when a source document is deleted. Hosted MCP (https://mcp.supermemory.ai/mcp) uses OAuth with space scoping and a compact tool set (whoAmI, addMemory/memory, searchMemories/recall). Public MIT repos cover SDKs, coding-agent plugins (claude-supermemory, @supermemory/tools), graph UI (@supermemory/memory-graph), and supermemoryai/memorybench. Local deployment ships as a proprietary compiled Bun binary (supermemory-server, Lite license capped at 10,000 docs) bundling PGlite/SQLite, rivet-engine, and local WASM BGE embeddings (port 6767). Published benchmarks: LongMemEval-S end-to-end QA with GPT-4o judge (81.6% GPT-4o, 84.6% GPT-5, 85.2% Gemini 3 Pro) plus separate higher ~94%-97% Recall@20/ASMR figures and LoCoMo/ConvoMem leaderboard claims. Unverified closed-backend claims: Core extraction prompts, updates/extends/derives graph mechanics, smart-forgetting decay formulas, background dreaming inference, and Cloudflare Workers/D1/KV cloud internals are proprietary and cannot be audited from source.

## 3. Section-by-Section Comparison (`S1`–`S8`)

Ratings are relative to Menhir `v0.3.15` (`[AHEAD]` = competitor ahead of Menhir, `[PARITY]` = roughly comparable, `[BEHIND]` = competitor behind Menhir, `[N/A]` = out of competitor scope).

| Section | Assessment |
|---|---|
| **S1. Ingest & Retain** | [PARITY] Supermemory ships broader external SaaS connectors (Google Drive, Gmail, Notion, OneDrive, GitHub, Granola, web crawlers), explicit ISO expiry (forgetAfter), soft/semantic erasure (DELETE /ns/{namespace}/memories[/semantic] with dryRun), and automatic cascading forget of derived memories on document deletion. However, it lacks Menhir's secret-scrubbing ingest hooks, two-tier canonical self binding, and provenance-aware multi-source re-derivation (#423), and its extraction/decay engine is closed-source. |
| **S2. Typed State & Consolidation** | [BEHIND] Supermemory models evolving atomic facts via updates/extends/derives edges, isLatest versioning, dual timestamps (documentDate/eventDate), and static/dynamic User Profiles, which covers basic supersession and profiling. Unlike Menhir, it relies on opaque LLM extraction with no span-grounded K-sample scalar perception, immutable TypedAssertion log, deterministic Event->Fold->View scalar state/history views, or counting consolidation. |
| **S3. Retrieve & Decide** | [PARITY] API v5 (POST /ns/{namespace}/search) provides default hybrid vector+lexical retrieval over both memories and raw chunks, optional rewriteQuery, cross-encoder rerank (none/order/aggregate), and a ~50ms /ns/{namespace}/profile lane, alongside experimental multi-agent ASMR retrieval. It lacks Menhir's 6 read-only Oracles, LogSpaceOracleCombiner, 4 fail-closed Wardens, separate non-interleaved ScalarAuthorityLayer, and deterministic timeline/history recall APIs. |
| **S4. Govern, Curate & Track Work** | [BEHIND] Supermemory offers namespace isolation, isLatest/parentMemoryId audit trails, and interactive graph visualization (@supermemory/memory-graph), but has no governance tiers (CANDIDATE/PERSISTENT/PROMOTED), conflict-resolution inbox with LLM review, WorkArtifact lifecycle state machine with Git reconciliation, or graph-linked code TODOs. |
| **S5. Code Graph & Impact** | [BEHIND] While claude-supermemory provides lifecycle hooks and repo-scoped text memory containers for coding agents, Supermemory has no deterministic AST code-graph scanner, incremental file watcher, coverage-aware blast_radius/affected_tests, or ANCHORED_TO diff/narrative links. |
| **S6. Interfaces, Auth & Operations** | [PARITY] Supermemory delivers a clean namespace-scoped REST v5 API, hosted OAuth MCP (https://mcp.supermemory.ai/mcp) with space-level authorization, MIT SDKs/agent plugins, and SOC 2/HIPAA/GDPR enterprise compliance. Its MCP surface is minimal (whoAmI, addMemory/memory, searchMemories/recall) compared to Menhir's 68 profiled tools, 9 resources, embedded OAuth 2.1 (PKCE S256, CIMD/DCR, digest-bound policy), SQLite-leased scheduler, and recoverable sagas. |
| **S7. Platform, Storage & Release** | [AHEAD] Supermemory ships both an edge-native Cloudflare (Workers/D1/KV) cloud and a zero-config single static Bun binary (supermemory-server with embedded PGlite/SQLite, rivet-engine, and local WASM BGE embeddings) plus Docker, beating Menhir's current Neo4j 5.26+ requirement—though the server binary is closed-source and capped at 10,000 docs under the Lite license. |
| **S8. Research & Eval Discipline** | [AHEAD] Supermemory open-sourced supermemoryai/memorybench (checkpointed INGEST->SEARCH->ANSWER->EVALUATE->REPORT harness) and publishes full-haystack LongMemEval-S scores (81.6% GPT-4o, 84.6% GPT-5, 85.2% Gemini 3 Pro; GPT-4o judge), whereas Menhir lacks a published LME-S full-haystack headline; however, Supermemory's higher ~94%-97% ASMR/Recall@20 claims and closed-backend ablations are not independently reproducible from source. |

## 4. Benchmarks Audited

- LongMemEval-S (published end-to-end QA, GPT-4o judge): 81.6% (GPT-4o answer model), 84.6% (GPT-5), 85.2% (Gemini 3 Pro) — reported by Supermemory/MemoryBench; core backend is closed-source so internal pipeline ablations cannot be independently verified from code.
- LongMemEval-S / ASMR & Recall@20 headlines (~94% end-to-end / ~97% Recall@20): reflect experimental multi-agent ASMR or retrieval-only/multi-variant configurations rather than the standard single-pass production API baseline (81.6%-85.2%).
- LoCoMo & ConvoMem (via supermemoryai/memorybench): Supermemory claims #1 positioning with ~700-token context budgets vs full-context baselines; public harness is MIT and checkpointed (INGEST->SEARCH->ANSWER->EVALUATE->REPORT), but exact per-split LoCoMo/ConvoMem numbers vary by harness version/model and independent audits note ~99 label/date-math errors in LoCoMo ground truth.

## 5. Where Supermemory Is Ahead vs. Behind

### Ahead of Menhir
- Zero-config local distribution via compiled single binary (supermemory-server with embedded PGlite/SQLite + local WASM Xenova/bge-base-en-v1.5 embeddings) and Docker without requiring Neo4j.
- Turnkey external SaaS sync connectors (Google Drive, Gmail, Notion, OneDrive, GitHub, Granola, web crawlers) with per-provider connection lifecycle APIs.
- Always-on static/dynamic User Profile endpoint (POST /ns/{namespace}/profile, ~50ms) decoupled from vector search so non-lexical user traits are reliably injected.
- Semantic erasure endpoint with dryRun preview (DELETE /ns/{namespace}/memories/semantic) and explicit ISO expiry (forgetAfter).
- Published full-haystack LongMemEval-S end-to-end scores (81.6% GPT-4o, 84.6% GPT-5, 85.2% Gemini 3 Pro) and an open-source multi-provider benchmark harness (supermemoryai/memorybench).
- Enterprise compliance posture (SOC 2, HIPAA BAA, GDPR DPA, 30-day purge / 90-day backup retention SLAs).

### Behind Menhir
- Closed-source core backend and Lite-capped (10k docs) local binary prevent source-level auditability, reproducibility, and unconstrained self-hosting.
- No deterministic typed scalar state machine (no span-grounded K-sample perception, immutable TypedAssertion log, or deterministic Event->Fold->View counting consolidation).
- No fail-closed retrieval/decision guardrails (no read-only Oracles, LogSpaceOracleCombiner, Wardens, or separate non-interleaved ScalarAuthorityLayer).
- No curation/governance or work-tracking layer (no CANDIDATE/PERSISTENT/PROMOTED tiers, conflict-resolution inbox, WorkArtifact lifecycle + Git reconciliation, or graph-linked TODOs).
- No deterministic AST code graph, incremental repo watcher, coverage-aware blast_radius/affected_tests, or ANCHORED_TO diff/narrative links.
- Coarse erasure semantics: deleting a document cascades to forget derived memories rather than re-deriving multi-source claims (#423), and no built-in secret scrubbing at ingest.

## 6. What Menhir Should Borrow

- Add a fast, non-lexical always-on static/dynamic profile summary endpoint (analogous to /ns/{namespace}/profile) that injects stable user/workspace facts without relying on per-turn vector overlap.
- Support semantic natural-language erasure with a dryRun preview mode (analogous to DELETE /ns/{namespace}/memories/semantic) layered on top of Menhir's provenance-aware re-derivation (#423).
- Add explicit ISO expiration metadata (forgetAfter) for transient episodic memories alongside decay/pin policies.
- Run and publish Menhir on full-haystack LongMemEval-S (using supermemoryai/memorybench or archolith-bench with standard GPT-4o judge) to close the public benchmark comparability gap.
- Prioritize the embedded graph backend (#109 FalkorDBLite) to approach Supermemory's zero-config single-binary local ergonomics while preserving Menhir's open-source auditability.

## 7. Novelty Claims Menhir Cannot Make

- Cannot claim novelty for separating raw immutable documents from evolving atomic facts with updates/extends/derives edges and isLatest supersession.
- Cannot claim novelty for dual temporal grounding (documentDate vs eventDate) or ISO-based automatic memory expiration (forgetAfter).
- Cannot claim novelty for hybrid vector + lexical search across both extracted memories and raw chunks with query rewriting and cross-encoder reranking.
- Cannot claim novelty for always-on static/dynamic user profile synthesis decoupled from per-turn vector search.
- Cannot claim novelty for semantic natural-language erasure with dryRun preview or cascading forget of derived memories upon source document deletion.
- Cannot claim novelty for coding-agent lifecycle hooks that auto-inject repo-scoped memory into Claude Code / Cursor / OpenCode via MCP + OAuth.

## 8. Primary Sources

- https://github.com/supermemoryai/supermemory
- https://github.com/supermemoryai/memorybench
- https://github.com/supermemoryai/supermemory-mcp
- https://github.com/supermemoryai/claude-supermemory
- https://supermemory.ai/docs
- https://supermemory.ai/research
