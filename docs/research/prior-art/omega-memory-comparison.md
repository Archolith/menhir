# OMEGA (OmegaMax / omega-memory) vs Menhir — Prior-Art / Architecture Comparison

**Date:** 2026-11-25  
**Status:** External comparison note (Wave 1 prior-art audit); use for positioning, roadmap triage, and benchmark planning.  
**Pinned version / state:** v1.5.20 (PyPI, 2026-10-08); audited 2026-11-25  
**License:** Apache-2.0 (OMEGA Core); Proprietary / Commercial private wheel ($19/mo or $15/mo annual) for OMEGA Pro  
**Evidence tier:** `partial_public`  
**Menhir baseline:** `v0.3.15` (`S1`–`S8` in `.agent/work-sections.md`)

---

## 1. Executive Verdict

OMEGA (`omega-memory`) is a strong local-first, zero-infra coding-agent MCP memory competitor that beats Menhir v0.3.15 on packaging ergonomics (single-file SQLite + `sqlite-vec` + `FTS5` + CPU ONNX), low-latency Unix-socket IDE hook dispatch (`fast_hook.py`), and published benchmark visibility (95.4% task-averaged / 93.2% raw LongMemEval + open `memorystress` suite). However, its open-source Core is architecturally much shallower than Menhir: it relies on mutable SQLite text rows with heuristic dedup/evolution/type-weights rather than Menhir's grounded typed assertion folds, multi-oracle/warden decision layer, provenance-aware cascading erasure, deterministic code-impact graph, governance state machines, and OAuth 2.1 security surface, while gating multi-agent coordination, encrypted sync, and auditability behind a closed-source Pro tier.

## 2. Architecture Summary

OMEGA (`omega-memory/omega-memory`, Sosa Research) is a Python 3.11+ local-first MCP memory server with an open-source Apache-2.0 Core and a closed-source commercial Pro tier ($19/mo private wheel). Core runs entirely on-device over stdio (or an optional localhost HTTP daemon via `omega serve` on `127.0.0.1:8377` with static bearer token `~/.omega/mcp_api_key`), backed by a single SQLite database (`omega.db` in `src/omega/sqlite_store.py`) combining `sqlite-vec` (384-dim cosine via local ONNX `bge-small-en-v1.5` / `all-MiniLM-L6-v2` cached in `~/.cache/omega/models/`) and `FTS5` BM25. Its schema centers on `memories`, `edges`, and `coord_*` tables, exposed through ~70 MCP tools (`handlers.py`, `coord_handlers.py`, e.g., `omega_store`, `omega_query`, `omega_memory`, `omega_reflect`, `omega_checkpoint`, `context_packet`) plus a lightweight Unix-socket hook dispatcher (`fast_hook.py` -> `~/.omega/hook.sock`) for Claude Code (`SessionStart`, `UserPromptSubmit`, `Stop`, `PostToolUse`). Retrieval blends vector (70%) and BM25 (30%) scores with type-weighted boosts and tag/project contextual re-ranking; lifecycle management uses SHA256+semantic dedup (>=0.85), in-place evolution (55-95% similarity append), per-type TTLs, auto-relate edges (>=0.45 top-3), and compaction. Closed-source Pro adds multi-agent file/branch locks, task queues, PDF/doc knowledge base indexing, AES-256-GCM encrypted sync, Secure Profiles, Entity Registry Pro, Oracle Intelligence Pro, and audit trails.

## 3. Section-by-Section Comparison (`S1`–`S8`)

Ratings are relative to Menhir `v0.3.15` (`[AHEAD]` = competitor ahead of Menhir, `[PARITY]` = roughly comparable, `[BEHIND]` = competitor behind Menhir, `[N/A]` = out of competitor scope).

| Section | Assessment |
|---|---|
| **S1. Ingest & Retain** | [BEHIND] OMEGA Core delivers zero-LLM Unix-socket hook auto-capture (`fast_hook.py` on `SessionStart`/`UserPromptSubmit`/`Stop`/`PostToolUse`), SHA256+semantic dedup (>=0.85), similarity evolution, and per-type TTLs (e.g., 1-day session summaries). However, it lacks Menhir's TurnEvidence pipeline, dedicated secret scrubbing, two-tier canonical self binding, and provenance-aware erasure with cascading derived re-derivation (#423). |
| **S2. Typed State & Consolidation** | [BEHIND] OMEGA stores typed SQLite rows (`decision`, `lesson`, `preference`, `session_summary`) with heuristic in-place evolution (appending on 55-95% similarity) and clustering compaction. This trails Menhir's K-sample span-grounded scalar perception, immutable `TypedAssertion` log, and deterministic `Event->Fold->View` (`ScalarStateView`/`ScalarHistoryView`) with counting consolidation and anchored time. |
| **S3. Retrieve & Decide** | [BEHIND] OMEGA blends `sqlite-vec` cosine (70%) and `FTS5` BM25 (30%) with type-weighted boosts (e.g., 2x decisions/lessons) and tag/project contextual re-ranking at vendor-reported <50ms latency, plus `context_packet` packing. It lacks Menhir's dual-lane candidate architecture, verified Core ONNX cross-encoder rerank, 6 read-only Oracles + `LogSpaceOracleCombiner`, 4 fail-closed Wardens, and separate `ScalarAuthorityLayer` (Pro's Oracle Intelligence is closed-source). |
| **S4. Govern, Curate & Track Work** | [BEHIND] OMEGA Core offers `omega_reflect` for contradiction/evolution tracing and `omega_checkpoint`/`omega_resume_task` for session handoffs, while closed-source Pro adds multi-agent locks, task queues, and audit trails. It lacks Menhir's open CANDIDATE/PERSISTENT/PROMOTED tiers, LLM-reviewed conflict inbox, Git-reconciled `WorkArtifact` state machine, and graph-linked code TODOs. |
| **S5. Code Graph & Impact** | [N/A] OMEGA maintains memory-to-memory `edges` (auto-related at >=0.45 similarity) and triggers memory surfacing on `PostToolUse` file events, but does not build a deterministic code graph. It has no AST repo scanner, incremental code watcher, coverage-aware `blast_radius`/`affected_tests`, or `ANCHORED_TO` diff links. |
| **S6. Interfaces, Auth & Operations** | [BEHIND] OMEGA matches Menhir's broad MCP footprint (~70 tools over stdio, optional `omega serve` HTTP daemon on `127.0.0.1:8377`, Unix-socket hook daemon, CLI, and Obsidian plugin). However, its HTTP auth is limited to localhost origin checks and a static bearer token (`~/.omega/mcp_api_key`), trailing Menhir's embedded OAuth 2.1 (PKCE S256, CIMD/DCR, rotating refresh, digest-bound policy), Explorer/Labs UI, leased scheduler, and recoverable sagas. |
| **S7. Platform, Storage & Release** | [AHEAD] OMEGA Core ships as a single-file SQLite (`omega.db` + `sqlite-vec` + `FTS5`) + CPU ONNX package (`pipx install "omega-memory[server]"`, ~270MB disk, 31-337MB RAM) with zero external DB daemon, ahead of Menhir's current Neo4j 5.26+ requirement (prior to #109 FalkorDBLite). Tradeoff: OMEGA gates Pro features in closed private wheels, whereas Menhir maintains a single open feature-flag SSOT and hash-verified Docker/wheelhouse releases. |
| **S8. Research & Eval Discipline** | [AHEAD] OMEGA publishes a full 500-question LongMemEval score (95.4% task-averaged / 93.2% raw, 466/500, 75/78 KU) and an open 1,000-session longitudinal benchmark (`omega-memory/memorystress`), whereas Menhir has not yet published an LME-S full-haystack headline. Caveat: OMEGA's LME headline uses a vendor-reported GPT-4.1 research setup (+2.2pp task-averaging lift over raw) and lacks Menhir's documented evidence-gated feature retirement discipline (#399). |

## 4. Benchmarks Audited

- LongMemEval (ICLR 2025, 500 questions) — Vendor-reported 95.4% task-averaged / 93.2% raw accuracy (466/500) using GPT-4.1 reader + local `bge-small-en-v1.5` ONNX embeddings: Single-Session 99% (125/126), Preference 100% (30/30), Knowledge Updates 96% (75/78), Temporal 94% (125/133), Multi-Session 83% (111/133). Audit caveats: vendor-reported research setup using GPT-4.1 (stronger than standard GPT-4o baselines), task-averaged headline inflates raw accuracy by +2.2pp, and split/judge reproducibility requires independent verification.
- MemoryStress (`github.com/omega-memory/memorystress`) — Open-source 1,000-session longitudinal memory benchmark created by the OMEGA authors to test retrieval degradation over long horizons across OpenAI/Anthropic/Gemini and competing memory backends (OMEGA, Mem0, Zep, LangMem).
- Operational latency (vendor-reported, unverified independently) — <15ms average SessionStart hook, <3ms PostToolUse hook surfacing, <50ms local SQLite+ONNX retrieval on local hardware (e.g., M1 MacBook Pro).

## 5. Where OMEGA (OmegaMax / omega-memory) Is Ahead vs. Behind

### Ahead of Menhir
- Zero-infrastructure single-file SQLite (`sqlite-vec` + `FTS5`) + local CPU ONNX packaging (`pipx install "omega-memory[server]"`, ~270MB disk, 31-337MB RAM) requiring no external Neo4j server daemon.
- Ultra-low-latency Unix-domain-socket hook daemon (`fast_hook.py` -> `~/.omega/hook.sock`) that avoids per-hook Python cold starts and fails open if the server is offline (vendor-reported <15ms SessionStart, <3ms PostToolUse).
- Published 500-question LongMemEval headline (95.4% task-averaged / 93.2% raw, 466/500, 75/78 KU) plus an open-source 1,000-session longitudinal benchmark (`omega-memory/memorystress`).
- Dedicated session continuity primitives (`omega_checkpoint`, `omega_resume_task`, `omega_welcome`) and first-party Obsidian plugin (`omega-obsidian-plugin`).

### Behind Menhir
- No immutable append-only typed assertion log or deterministic Event->Fold->View scalar state/history architecture; in-place memory evolution (55-95% similarity append) mutates text rather than folding grounded assertions.
- No provenance-aware erasure with cascading derived re-derivation (#423) or dedicated secret-scrubbing ingest pipeline verified in Core.
- No deterministic AST code-graph indexer, incremental repo watcher, coverage-aware blast_radius/affected_tests, or ANCHORED_TO narrative/diff links.
- No OAuth 2.1 authentication (PKCE S256, CIMD/DCR, rotating refresh, digest-bound policy); HTTP daemon relies on localhost origin checks and a static file-based bearer token.
- No multi-oracle LogSpaceOracleCombiner, fail-closed Wardens, or separate non-interleaved ScalarAuthorityLayer in open-source Core (Pro Oracle Intelligence is closed and unverified).
- No open CANDIDATE/PERSISTENT/PROMOTED governance tiers, LLM-reviewed conflict resolution inbox, or Git-reconciled WorkArtifact lifecycle state machine.

## 6. What Menhir Should Borrow

- Unix-domain-socket hook dispatch (`fast_hook.py` -> `~/.omega/hook.sock`) with fail-open fallback to eliminate per-hook Python startup latency for TurnEvidence and PostToolUse surfacing.
- Single-command zero-infra embedded install ergonomics (`pipx`/`uv` + local model bootstrap) once FalkorDBLite (#109) removes the mandatory external Neo4j server requirement.
- Benchmarking Menhir against `omega-memory/memorystress` (1,000-session longitudinal harness) alongside publishing a full LME-S haystack headline with transparent raw vs. task-averaged reporting.
- Lightweight session-checkpoint/resume primitives (`omega_checkpoint`/`omega_resume_task`) and compact `context_packet` framing for coding-agent handoffs.

## 7. Novelty Claims Menhir Cannot Make

- Menhir cannot claim novelty merely for being a local-first Python MCP memory server with local ONNX embeddings, SQLite hybrid vector+FTS5 search, or ~70-tool coding-agent MCP surface (OMEGA Core already ships this under Apache-2.0).
- Menhir cannot claim novelty for Claude Code lifecycle hooks (`SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`) that auto-capture decisions/lessons and surface relevant memories on file events (OMEGA's `fast_hook.py` already implements this with a Unix-socket daemon).
- Menhir cannot claim novelty for type-weighted hybrid retrieval, automatic memory-to-memory similarity linking, or contradiction reflection tools (`omega_reflect`) without highlighting Menhir's distinct grounded Event->Fold->View, multi-oracle/warden, and provenance-erasure guarantees.

## 8. Primary Sources

- https://github.com/omega-memory/omega-memory
- https://omegamax.co
- https://pypi.org/project/omega-memory/
- https://github.com/omega-memory/memorystress
