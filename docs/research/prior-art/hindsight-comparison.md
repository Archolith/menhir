# Hindsight (Vectorize) vs Menhir — Prior-Art / Architecture Comparison

**Date:** 2026-11-25  
**Status:** External comparison note (Wave 1 prior-art audit); use for positioning, roadmap triage, and benchmark planning.  
**Pinned version / state:** v1.2.3 (November 2026); paper arXiv:2512.12818 (Dec 2025)  
**License:** MIT  
**Evidence tier:** `oss_primary_sources`  
**Menhir baseline:** `v0.3.15` (`S1`–`S8` in `.agent/work-sections.md`)

---

## 1. Executive Verdict

Hindsight (Vectorize, MIT, v1.2.3) is the strongest open-source prior art against Menhir on general conversational memory, single-database PostgreSQL simplicity (embedded pg0 + pgvector + recursive-CTE graph), 4-way RRF/cross-encoder recall, and published full-haystack LongMemEval-S/LoCoMo benchmarks. However, Hindsight is fundamentally an LLM-synthesized reflection engine—it trails Menhir v0.3.15 on deterministic typed state (Event -> Fold -> View), fail-closed retrieval/governance wardens, automatic provenance-aware erasure re-derivation, code graph/blast-radius analysis, WorkArtifact/TODO lifecycle governance, and embedded self-hosted OAuth 2.1.

## 2. Architecture Summary

Hindsight (vectorize-io/hindsight, MIT) structures agent memory around three core primitives—retain, recall, and reflect—backed by four logical networks (World facts, Agent experiences, Consolidated observations/entity summaries, and Evolving opinions/beliefs) plus cached Mental Models (renamed Pages in v1.2.x). Storage runs on a single PostgreSQL engine (embedded pg0 by default or external PG + pgvector HNSW + tsvector GIN + JSONB), modeling facts in memory_units (with fact_type, valid/invalidated state, and source_memory_ids uuid[] lineage) and directed temporal/semantic/entity/causal edges in memory_links traversed via recursive CTEs. Retain screens inputs through Memory Defense (regex secret/PII redaction or 422 blocking in OSS) before LLM fact/entity extraction; recall runs 4-way parallel retrieval fused with RRF (k=60), cross-encoder reranking, multiplicative boosts, and token-budget trimming; reflect executes an agentic reasoning loop that consults Mental Models -> Observations -> Raw Facts while enforcing bank Directives and Disposition.

## 3. Section-by-Section Comparison (`S1`–`S8`)

Ratings are relative to Menhir `v0.3.15` (`[AHEAD]` = competitor ahead of Menhir, `[PARITY]` = roughly comparable, `[BEHIND]` = competitor behind Menhir, `[N/A]` = out of competitor scope).

| Section | Assessment |
|---|---|
| **S1. Ingest & Retain** | [PARITY] Hindsight's retain pipeline extracts structured world/experience facts into memory_units and memory_links with OSS Memory Defense secret/PII redaction or 422 blocking (44-45 regex patterns) and document/observation deletion endpoints. Menhir remains ahead on two-tier canonical self binding, retention pins, and automatic provenance-aware erasure re-derivation (#423), whereas Hindsight has required explicit clear_observations and bug fixes for orphaned observations after document deletion/re-retain. |
| **S2. Typed State & Consolidation** | [BEHIND] Hindsight separates world facts, experiences, consolidated observations (tracked via source_memory_ids uuid[]), confidence-scored opinions, and cached Mental Models/Pages updated by background LLM consolidation. Unlike Menhir's K-sample span-grounded scalar perception, immutable TypedAssertion log, and deterministic Event -> Fold -> View (ScalarStateView/ScalarHistoryView), Hindsight's consolidation and mental-model synthesis are non-deterministic LLM generations. |
| **S3. Retrieve & Decide** | [AHEAD] Hindsight's recall runs 4-way parallel search (pgvector semantic, tsvector BM25, recursive-CTE graph spreading activation, temporal) merged via RRF (k=60), cross-encoder reranking (default 300 candidates), multiplicative boosts, and token-budget trimming, paired with an agentic reflect loop prioritizing Mental Models -> Observations -> Raw Facts. While Menhir is stronger on fail-closed Wardens, 6 read-only Oracles with LogSpaceOracleCombiner, and a non-interleaved ScalarAuthorityLayer, Hindsight's 4-way RRF + token-budgeted recall and hierarchical reflect are empirically proven at full-haystack scale. |
| **S4. Govern, Curate & Track Work** | [BEHIND] Hindsight offers bank-level Directives enforced during reflect, Disposition traits, valid/invalidated memory states, and source_memory_ids lineage inspection. It lacks Menhir's CANDIDATE/PERSISTENT/PROMOTED curation tiers, conflict-scan resolution inbox, first-class WorkArtifact lifecycle state machine with Git reconciliation, and graph-linked code TODOs. |
| **S5. Code Graph & Impact** | [N/A] Hindsight provides no deterministic AST/repository code scanner, incremental watcher, coverage-aware blast_radius/affected_tests, or ANCHORED_TO narrative/diff links; its graph models conversational and document entities only. |
| **S6. Interfaces, Auth & Operations** | [PARITY] Hindsight ships a FastAPI REST API, native HTTP/stdio MCP (hindsight-local-mcp with destructiveHint: true annotations), a Control Plane UI, broad agent SDK integrations, and Helm/Gateway API charts. However, full MCP OAuth (RFC 9728 + PKCE) is restricted to Hindsight Cloud while self-hosted deployments default to open/no-auth or API-key/JWT TenantExtension, trailing Menhir's embedded self-hosted OAuth 2.1 (PKCE S256, CIMD/DCR, rotating refresh, digest-bound policy) and 68 profiled MCP tools. |
| **S7. Platform, Storage & Release** | [AHEAD] Hindsight unifies vector (pgvector HNSW), full-text (tsvector GIN), relational, and graph (recursive CTEs) storage in a single PostgreSQL database with zero-setup embedded pg0 locally, multi-tenant schema isolation, zero-downtime re-embedding (v1.2.x), separate query/passage embedders, and export/import index re-derivation. This avoids Menhir's Neo4j 5.26+ external server requirement while Menhir's FalkorDBLite migration (#109) remains in progress. |
| **S8. Research & Eval Discipline** | [AHEAD] Hindsight pairs a published paper (arXiv:2512.12818 with Virginia Tech/Washington Post) with open-source evaluation harnesses (vectorize-io/agent-memory-benchmark) reporting 91.4%/94.6% on full-haystack LongMemEval-S and 89.61%/92.0% on LoCoMo (paper vs. later vendor AMB updates). Menhir's evidence-gated feature retirement (#399) is disciplined, but it lacks a published full-haystack LME-S headline comparable to Hindsight's reproducible suite. |

## 4. Benchmarks Audited

- [Paper - arXiv:2512.12818, Dec 2025] LongMemEval: up to 91.4% accuracy with backbone scaling (authored by Vectorize, Virginia Tech, and Washington Post researchers); outperforms full-context GPT-4o baseline on multi-session, temporal, knowledge-update, and abstention tasks.
- [Paper - arXiv:2512.12818, Dec 2025] LoCoMo: up to 89.61% accuracy (reported vs. 75.78% prior open-source baseline).
- [Vendor Harness - vectorize-io/agent-memory-benchmark & benchmarks.hindsight.vectorize.io] Post-paper v0.4.19+ runs report 94.6% on LongMemEval-S and 92.0% on LoCoMo-10 using LLM-as-a-judge evaluation; harness code is open-source, but these higher headline scores are vendor-maintained updates rather than independently peer-reviewed paper figures.
- [Unverified Vendor Claim] Production recall latency of 50-500ms at millions of memories and "up to 50x" v1.2.x speedups on large banks are vendor documentation/release-note claims without an independent third-party latency benchmark audit.

## 5. Where Hindsight (Vectorize) Is Ahead vs. Behind

### Ahead of Menhir
- Single-engine PostgreSQL storage (pgvector HNSW + tsvector GIN + relational recursive-CTE graph) with zero-setup embedded pg0 for local development, avoiding an external Neo4j server dependency.
- Published, reproducible full-haystack LongMemEval-S (91.4% in arXiv:2512.12818; 94.6% in later vendor AMB harness) and LoCoMo (89.61% in paper; 92.0% in AMB) evaluations with open-source benchmark harnesses (vectorize-io/agent-memory-benchmark).
- 4-way parallel recall (semantic, BM25/tsvector, graph spreading activation, temporal) fused via RRF (k=60), cross-encoder reranking, bounded multiplicative boosts, and dynamic token-budget trimming instead of flat top-k cutoffs.
- Pre-computed, auto-refreshing Mental Models / Pages hierarchy (Mental Models -> Observations -> Raw Facts) with bank-level Directives and Disposition for zero-LLM-overhead standing answers during reflect.
- Operational storage lifecycle features in v1.2.x: zero-downtime re-embedding, separate query/passage embedders, bank export/import index re-derivation, multi-tenant Postgres schema routing, and Helm/Gateway API deployment charts.

### Behind Menhir
- No deterministic typed-state layer: consolidation into observations and Mental Models/Pages is LLM-synthesized rather than Menhir's K-sample span-grounded scalar perception, immutable TypedAssertion log, and deterministic Event -> Fold -> View (ScalarStateView/ScalarHistoryView).
- No fail-closed retrieval/governance Wardens, 6-Oracle LogSpaceOracleCombiner, or separate non-interleaved ScalarAuthorityLayer; reflect relies on prompt-enforced Directives over an agentic LLM loop.
- Weaker provenance-aware erasure: deleting or re-retaining documents historically left orphaned observations referencing missing source_memory_ids, requiring explicit clear_observations or cleanup sweeps rather than Menhir's automatic derived re-derivation (#423).
- No codebase/AST graph scanner, incremental repo watcher, coverage-aware blast_radius/affected_tests, or ANCHORED_TO code-diff links.
- No CANDIDATE/PERSISTENT/PROMOTED curation tiers, conflict-scan resolution inbox, first-class WorkArtifact lifecycle state machine with Git reconciliation, or graph-linked code TODOs.
- Self-hosted auth defaults to open/no-auth or API-key/JWT TenantExtension; full MCP OAuth (RFC 9728 + PKCE) is a Hindsight Cloud feature rather than an embedded self-hosted OAuth 2.1 server with CIMD/DCR and digest-bound client policy.

## 6. What Menhir Should Borrow

- Token-budgeted recall packing as a first-class API parameter (trimming reranked results dynamically to a token budget rather than fixed top-k).
- Standing, pre-computed cached views ("Mental Models / Pages") refreshed asynchronously on consolidation for frequent high-level queries—adapted in Menhir to combine deterministic folded state with bounded narrative summaries.
- Zero-downtime re-embedding and export/import index re-derivation workflows so embedding model upgrades do not require LLM re-extraction.
- Running and publishing Menhir v0.3.15 on the open-source vectorize-io/agent-memory-benchmark LongMemEval-S full-haystack harness to establish a direct apples-to-apples comparison against Hindsight's 91.4%/94.6% baseline.
- MCP destructiveHint: true tool annotations on erasure/mutation endpoints and optional block-vs-redact (422) policy modes for secret scrubbing on ingest.

## 7. Novelty Claims Menhir Cannot Make

- "Separating raw facts/experiences from consolidated observations and entity summaries with source_memory_ids lineage" — already shipped in Hindsight (MIT) and formalized in arXiv:2512.12818.
- "Multi-arm hybrid retrieval combining semantic vectors, BM25/keyword, temporal filtering, and graph traversal with cross-encoder reranking" — already standard in Hindsight's 4-way RRF + cross-encoder recall pipeline.
- "Pre-ingest secret/PII regex scrubbing on agent memory retain" — Hindsight ships Memory Defense with 44-45 regex patterns supporting redaction or 422 blocking in OSS.
- "Single-node embedded zero-config graph+vector memory store for local MCP coding agents" — Hindsight already delivers this via embedded pg0 + hindsight-local-mcp.

## 8. Primary Sources

- https://github.com/vectorize-io/hindsight
- https://arxiv.org/abs/2512.12818
- https://hindsight.vectorize.io/
- https://github.com/vectorize-io/agent-memory-benchmark
- https://benchmarks.hindsight.vectorize.io
