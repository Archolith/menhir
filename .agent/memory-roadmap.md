# menhir — Roadmap

## v1 Status: Complete (2026-03-18)

All 12 milestones shipped. 722+ tests passing.

| Milestone | Scope | Completed |
|-----------|-------|-----------|
| **M0** Scope lock & metrics | v1 scope frozen, success metrics, 10 fixed queries | 2026-03-01 |
| **M0.5** Graphiti viability | OpenAIGenericClient works with Qwen via llama.cpp | 2026-03-06 |
| **M1** Schema baseline | Schema bootstrap, adapter, domain models, service stubs | 2026-03-06 |
| **M2** Ingestion MVP | Graphiti-backed ingestion, session policy stamping | 2026-03-06 |
| **M2.5** Session consolidation | SESSION→PERSISTENT promotion, contradiction detection | 2026-03-07 |
| **M2.75** Deferred enrichment | Journal-first writes, background worker, SQLite telemetry | 2026-03-08 |
| **M3** Retrieval & scoring | Two-phase retrieval, relevance presets, explainability | 2026-03-08 |
| **M3.5** MCP server | Tools + read-only resources/templates for Claude Code | 2026-03-09 |
| **M4** Lifecycle & decay | ACTIVE→COMPRESSED→GONE, LLM compression, rehydration | 2026-03-12 |
| **M5** Conflict governance | Conflict groups, resolution UX, auto-resolve, scoring signal | 2026-03-14 |
| **M6** Ops hardening | Circuit breakers, budget caps, context builder, embedding cache, sidecar expansion | 2026-03-16 |
| **M7** E2E validation | Replay harness, 71 regression tests, ops runbook, M0 baseline gate | 2026-03-18 |

## Post-v1 Work

See [post-v1-todo.md](post-v1-todo.md) for the living TODO on the shipped system,
and [research/menhir-research-execution-ladder.md](research/menhir-research-execution-ladder.md)
for the research → production build ladder (oracle pipeline, belief buckets,
retrieval tuning, control rails, cognitive replay). The conceptual phase ladder
that the rungs realize is in `../docs/research/vision/cognitive-replay-and-phasing.md`.

Key areas:
- **MemoryType OOP contract** — per-type decay/scoring policies (done 2026-03-21)
- **Git diff attachment** — episodes carry diffs for code-change reasoning (done 2026-03-21)
- **Layering fix** — telemetry moved from mcp/ to infrastructure/ (done 2026-03-21)
- **Server split** — 23 tools extracted to mcp/tools/{ingest,recall,conflict,ops}/ (done 2026-03-20)
- Construction narrative, conversation-aware version control, project structure mapping (planned)
- CI/CD pipeline (identified as top priority gap in senior systems review)

## Deferred from v1

- Confidence-weighted emotions
- Post-v1 lifecycle stages (STALE, ARCHIVED)
- Independent edge decay
- Fully automated skill/hook promotion
- Multi-tenant / cloud-hosted deployment

---

## Sage-wiki Integration (COMPLETED 2026-04-14)

Connected memory graph to workspace wiki for unified recall + documentation context.

| Feature | Status | PR/Commit |
|---------|--------|-----------|
| `document_type` property | ✅ | - |
| `query_structure("documents")` | ✅ | - |
| Episode → wiki linking | ✅ | - |
| Recall includes wiki context | ✅ | - |
| `ingest-wiki` CLI | ✅ | - |

**Wrapup:** `.agent/../.agent/for-review/WRAPUP-2026-04-14-MEMORY-SAGE-WIKI-STEPS-0-1.md`
