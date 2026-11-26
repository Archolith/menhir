# Honcho (Plastic Labs) vs Menhir — Prior-Art / Architecture Comparison

**Date:** 2026-11-25  
**Status:** External comparison note (Wave 2 coding-agent & IDE incumbents audit); use for positioning, roadmap triage, and benchmark planning.  
**Pinned version / state:** v3.7.0 (v3.x release line; SDK v2.9.0+ compatibility; audited 2026-11-25)  
**License:** AGPL-3.0 (core server `plastic-labs/honcho`); MIT / Apache-2.0 for SDKs and plugins (e.g. `plastic-labs/claude-honcho` is MIT); Neuromancer reasoning model weights (XR/MR) are proprietary / Honcho Cloud only.  
**Evidence tier:** `oss_primary_sources`  
**Menhir baseline:** `v0.3.15` (`S1`–`S8` in `.agent/work-sections.md`)

---

## 1. Executive Verdict

Honcho v3 (`plastic-labs/honcho`, AGPL-3.0) is a mature, well-engineered conversational memory and social-cognition platform that outpaces Menhir v0.3.15 in storage ergonomics (PostgreSQL/`pgvector` + pluggable vector stores), agentic multi-hop retrieval (tunable Dialectic reasoning levels), and public full-haystack benchmark transparency (`honcho-benchmarks`: 90.4% LongMemEval-S, 89.9% LoCoMo). However, Honcho is purely a probabilistic conversational/user-modeling system: it lacks Menhir's deterministic span-grounded scalar state (`Event->Fold->View`), fail-closed retrieval Wardens, code-graph/blast-radius intelligence, WorkArtifact/TODO governance, secret scrubbing, and provenance-aware cascading erasure. Furthermore, Honcho's signature Neuromancer reasoning weights are proprietary/Cloud-only, creating a capability gap between Honcho Cloud and self-hosted AGPL deployments.

## 2. Architecture Summary

Honcho v3 (`plastic-labs/honcho`, AGPL-3.0) is a dual-service FastAPI + asynchronous worker memory platform built on PostgreSQL + `pgvector` (via SQLAlchemy/Alembic) with pluggable external vector stores (`turbopuffer`, `lancedb`, `qdrant`, `chromadb`) and optional Redis for caching and distributed locks. Its data model centers on Workspaces, Sessions, and unified Peers (both humans and AI agents with configurable observation rules). Ingestion splits into two phases: (1) a parallelized Deriver that extracts exhaustive explicit conclusions linked via `source_message_ids`, and (2) an asynchronous Dreamer agent that crawls historical sessions to synthesize deductive, inductive, abductive, and contradiction conclusions (linked via parent `source_ids`), session summaries, biographical Peer Cards, and auditable Dream Histories. Retrieval is served by an agentic Dialectic endpoint (`chat`) with five tunable `reasoning_level` tiers (`minimal` to `max`) and `include_evidence=True` citations. Honcho Cloud runs Plastic Labs' proprietary fine-tuned Neuromancer reasoning models (e.g., Neuromancer XR fine-tuned on Qwen3-8B, Neuromancer MR for meta-reasoning), whereas self-hosted deployments configure BYO OpenAI-compatible LLMs (OpenAI, OpenRouter, vLLM, Ollama).

## 3. Section-by-Section Comparison (`S1`–`S8`)

Ratings are relative to Menhir `v0.3.15` (`[AHEAD]` = competitor ahead of Menhir, `[PARITY]` = roughly comparable, `[BEHIND]` = competitor behind Menhir, `[N/A]` = out of competitor scope).

| Section | Assessment |
|---|---|
| **S1. Ingest & Retain** | [BEHIND] Honcho v3 pairs a parallelized Deriver for explicit fact extraction with symmetric multi-peer observation rules and an experimental bulk `/v3/workspaces/{workspace_id}/ingest` API (`v3.7.0`), but it lacks Menhir's secret scrubbing, retention-pin/decay controls, and provenance-aware cascading erasure; deleting a conclusion (`DEL /v3/workspaces/{id}/conclusions/{cid}`) can be re-derived by the Dreamer if source messages persist, and thorough self-hosted wiping often requires direct PostgreSQL purges. |
| **S2. Typed State & Consolidation** | [PARITY] Honcho's asynchronous Dreamer synthesizes typed reasoning tiers (explicit, deductive, inductive, abductive, contradiction conclusions), session summaries, biographical Peer Cards, and Dream Histories. While more expressive for psychological/social user modeling than Menhir, it is entirely LLM-driven and lacks Menhir's deterministic K-sample span-grounded `Event->Fold->View` scalar authority and immutable `TypedAssertion` log. |
| **S3. Retrieve & Decide** | [AHEAD] Honcho's agentic Dialectic endpoint dynamically runs tool-using retrieval over conclusions, summaries, and raw premises with five tunable `reasoning_level` tiers (`minimal` to `max`) and `include_evidence=True` citations, backing strong full-haystack recall (90.4% LongMemEval-S). However, it lacks Menhir's fail-closed Wardens, local ONNX cross-encoder reranking, and non-interleaved deterministic `ScalarAuthorityLayer`. |
| **S4. Govern, Curate & Track Work** | [BEHIND] Honcho tracks conclusion provenance (`source_message_ids` and parent `source_ids`), emits contradiction conclusions during dreaming, and exposes Dream Histories via CLI/Dashboard. It has no promotion-tier governance (`CANDIDATE`/`PERSISTENT`/`PROMOTED`), no human conflict-resolution inbox, no WorkArtifact lifecycle state machine, and no graph-linked code TODOs. |
| **S5. Code Graph & Impact** | [N/A] Honcho is strictly a conversational and social-cognition memory layer with no repository AST/symbol indexer, no coverage-aware `blast_radius` or `affected_tests`, and no file-event staleness advisories. |
| **S6. Interfaces, Auth & Operations** | [PARITY] Honcho ships a hosted Streamable HTTP MCP server (`mcp.honcho.dev`), official Claude Code (`plastic-labs/claude-honcho`) and Hermes Agent (`hermes-plugin-honcho`) plugins, multi-tenancy (`/v3/tenants`), named-scope API keys, Prometheus/CloudEvents telemetry, and OAuth sign-in for cloud clients. Its self-hosted auth (`JWT_SECRET`/`AUTH_USE_AUTH`) is simpler than Menhir's embedded OAuth 2.1 stack (PKCE S256, CIMD/DCR, rotating refresh), and its MCP surface is far narrower (~10 core tools vs Menhir's 68 profiled tools + 9 resources). |
| **S7. Platform, Storage & Release** | [AHEAD] Honcho runs on mainstream PostgreSQL + `pgvector` with SQLAlchemy and non-blocking Alembic migrations, pluggable vector backends (`turbopuffer`, `lancedb`, `qdrant`, `chromadb`), optional Redis caching/locking, `uv tool install honcho-cli` (`honcho start --setup`), and GHCR Docker images. This avoids Menhir's heavier Neo4j 5.26+ dependency, though Honcho Cloud's Neuromancer model weights remain proprietary. |
| **S8. Research & Eval Discipline** | [AHEAD] Plastic Labs publishes reproducible evaluation harnesses and commit-pinned run data in `plastic-labs/honcho-benchmarks` covering full-haystack LongMemEval-S (90.4%), LoCoMo (89.9% via LLM-judge), and BEAM (100K–10M tokens). Menhir has strong internal falsification (`archolith-bench`, ~90% LME-Oracle) and evidence-gated feature retirement, but lacks a published full-haystack LongMemEval-S headline. |

## 4. Benchmarks Audited

- LongMemEval-S: 90.4% accuracy published by Plastic Labs in `plastic-labs/honcho-benchmarks` (vs 62.6% baseline Claude Haiku 4.5 alone). Audited stack: Gemini 2.5 Flash Lite as Deriver, Claude Haiku 4.5 for Dialectic/Dreamer retrieval & answering, LLM-as-a-judge (GPT-4o / harness rubric). Verified as published vendor benchmark with public repo/configs; independent third-party replication not yet confirmed.
- LoCoMo: 89.9% accuracy published in `plastic-labs/honcho-benchmarks` (vs 75.6% baseline Claude Haiku 4.5 alone). Caveat: uses LLM-as-a-judge scoring rather than the original LoCoMo paper's token-F1 metric, which inflates scores relative to F1 baselines (though aligns with recent memory-system papers).
- BEAM (100K to 10M tokens): Evaluated in `plastic-labs/honcho-benchmarks` claiming Pareto dominance in accuracy, latency, and token cost over raw context stuffing. Vendor-published results; exact self-hosted parity with non-Neuromancer open-weight LLMs is unverified.
- Unverified vendor claims separated from code facts: Plastic Labs' "SOTA token efficiency" and Neuromancer XR/MR superiority claims apply to Honcho Cloud's proprietary fine-tuned weights; self-hosted AGPL deployments using generic OpenAI-compatible models do not automatically reproduce Cloud benchmark numbers or latency/cost tiers ($0.001 minimal to $0.50 max per Dialectic call).

## 5. Where Honcho (Plastic Labs) Is Ahead vs. Behind

### Ahead of Menhir
- Mainstream PostgreSQL + pgvector default storage with pluggable vector backends (turbopuffer, LanceDB, Qdrant, ChromaDB), optional Redis, and non-blocking Alembic migrations — far easier to self-host and scale than Menhir's Neo4j 5.26+ requirement.
- Published, reproducible full-haystack benchmark suite (`plastic-labs/honcho-benchmarks`) reporting 90.4% on LongMemEval-S, 89.9% on LoCoMo, and 100K–10M token BEAM scaling curves with commit-pinned run data.
- Agentic Dialectic retrieval with caller-selectable `reasoning_level` (`minimal`, `low`, `medium`, `high`, `max`) trading off latency/cost vs multi-hop synthesis depth, plus `include_evidence=True` retrieval traces.
- Native multi-peer social cognition model (humans and agents as symmetric peers with configurable observation rules) and biographical Peer Cards updated asynchronously by the Dreamer.
- Official cross-harness ecosystem plugins (`plastic-labs/claude-honcho` for Claude Code, `hermes-plugin-honcho` for Nous Hermes Agent, hosted `mcp.honcho.dev`) enabling shared user memory across different agent runtimes, plus v3.6+ `MULTI_TENANT` control-plane provisioning (`/v3/tenants`).

### Behind Menhir
- No deterministic, span-grounded typed scalar state (`Event->Fold->View`) or immutable `TypedAssertion` log; all consolidation relies on probabilistic LLM reasoning (Dreamer/Dialectic).
- No code-graph indexing, AST/symbol awareness, `blast_radius`, `affected_tests`, or file-event staleness advisories (conversational memory only).
- Weak erasure and secret hygiene: no built-in secret scrubbing or retention-pin/decay lifecycle; deleting an individual conclusion (`DEL /v3/workspaces/{id}/conclusions/{cid}`) can be re-derived by the Dreamer if source messages remain, and thorough data wiping on self-hosted deployments often falls back to direct PostgreSQL deletion.
- No governance tiers (`CANDIDATE`/`PERSISTENT`/`PROMOTED`), no human-in-the-loop conflict resolution inbox, no WorkArtifact lifecycle state machine, and no graph-linked code TODOs.
- No fail-closed retrieval Wardens or local ONNX cross-encoder reranking; retrieval quality and safety depend on live LLM tool-use in Dialectic.
- Self-hosted authentication is basic (`JWT_SECRET` / `AUTH_USE_AUTH`, named-scope API keys) compared to Menhir's embedded OAuth 2.1 server (PKCE S256, CIMD/DCR, rotating refresh, digest-bound client policy), and its MCP surface is narrow (~10 tools vs Menhir's 68 profiled tools and 9 resources).

## 6. What Menhir Should Borrow

- Tunable query-time depth/cost knob (`reasoning_level`: `minimal`/`low`/`medium`/`high`/`max`) on Menhir recall/context-building so callers can trade latency and compute for multi-hop synthesis when needed.
- `include_evidence=True` retrieval traces and an auditable consolidation diff log (analogous to Honcho's Dream Histories) showing exactly which assertions/views were added, modified, or pruned per background cycle.
- A compact, eagerly cached biographical/standing-preferences Peer Card block alongside Menhir's scalar views for zero-latency session bootstrap.
- A public, commit-pinned benchmark repository (`archolith-bench` public subset) reporting full-haystack LongMemEval-S and long-horizon scaling curves so Menhir's retrieval and scalar authority can be compared head-to-head against Honcho's 90.4% LME-S headline.
- First-class cross-harness packaging (lightweight MIT-licensed Claude Code marketplace plugin + Hermes memory provider) mirroring `plastic-labs/claude-honcho` and `hermes-plugin-honcho`.

## 7. Novelty Claims Menhir Cannot Make

- Asynchronous background 'dreaming' / consolidation workers that derive higher-order deductive, inductive, and contradiction conclusions from raw interaction logs.
- Cross-harness persistent agent memory shared between Claude Code and Nous Hermes Agent via a unified MCP/plugin backend.
- Provenance-linked memory conclusions tracing back to `source_message_ids` and parent conclusion `source_ids` with query-time evidence inspection (`include_evidence=True`).
- Symmetric multi-peer modeling where both human users and AI agents maintain evolving representations and biographical profile cards.
- Agentic query-time retrieval synthesis over memory stores with caller-controlled reasoning depth/cost budgets.

## 8. Primary Sources

- https://github.com/plastic-labs/honcho
- https://docs.honcho.dev
- https://github.com/plastic-labs/honcho-benchmarks
- https://github.com/plastic-labs/claude-honcho
- https://mcp.honcho.dev
- https://plasticlabs.ai
