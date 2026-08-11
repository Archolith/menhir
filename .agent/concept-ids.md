# Concept IDs

Compact concept-id router for `menhir`.

Do not read this file by default for every task. Use it when you need:
- the stable id for a cross-doc concept
- the canonical owner doc for a concept
- a quick bridge from a task router into the right index or reference doc

For the full registry, use `concept-ids.yaml`.

## Hot Path

Start with the smallest task router that matches the work:

- debugging or incidents -> `tasks-debugging.md`
- ingest, queueing, enrichment, stamping -> `tasks-ingest.md`
- MCP tools, resources, bootstrap reads -> `tasks-mcp.md`
- purpose and principles -> `memory-foundations.md`
- policy, scoring, lifecycle, scope -> `memory-policy.md`
- query, explainability, ingest design -> `memory-ingest-queries.md`
- future direction -> `memory-futures.md`

Use this file only after that when you need an exact id or owner.

## Core Families

### Runtime

| Id | Owner |
|----|-------|
| `runtime.overview` | `architecture.md` |
| `runtime.projections` | `architecture.md` |
| `runtime.ops` | `architecture.md` |
| `runtime.shape` | `architecture.md` |
| `runtime.dependencies` | `architecture.md` |
| `runtime.storage` | `architecture.md` |
| `runtime.turn_evidence_capture` | `architecture.md` |

### Memory

| Id | Owner |
|----|-------|
| `memory.overview` | `memory-design.md` |
| `memory.principles` | `memory-design.md` |
| `memory.stack` | `memory-foundations.md` |
| `memory.policy.graph` | `memory-design.md` |
| `memory.policy.scope` | `memory-design.md` |
| `memory.policy.lifecycle` | `memory-design.md` |
| `memory.policy.scoring` | `memory-design.md` |
| `memory.design.ingest` | `memory-design.md` |
| `memory.design.query` | `memory-design.md` |
| `memory.design.promotion` | `memory-design.md` |

### Model

| Id | Owner |
|----|-------|
| `model.entity` | `data_models.md` |
| `model.episode` | `data_models.md` |
| `model.turn_evidence` | `data_models.md` |
| `model.edge` | `data_models.md` |
| `model.conflict` | `data_models.md` |
| `model.config` | `data_models.md` |

### MCP

| Id | Owner |
|----|-------|
| `mcp.group.ingest` | `endpoints.md` |
| `mcp.group.processing` | `endpoints.md` |
| `mcp.group.recall` | `endpoints.md` |
| `mcp.group.operator` | `endpoints.md` |
| `mcp.group.context` | `endpoints.md` |
| `mcp.group.stats` | `endpoints.md` |
| `mcp.tool.get_episode_trace` | `endpoints.md` |
| `mcp.tool.list_enrichment_queue` | `endpoints.md` |
| `mcp.tool.add_memory` | `endpoints.md` |
| `mcp.tool.add_memory_and_track` | `endpoints.md` |
| `mcp.tool.build_context` | `endpoints.md` |
| `mcp.tool.get_memory_stats` | `endpoints.md` |
| `mcp.tool.recover_orphans` | `endpoints.md` |
| `mcp.tool.force_reenrich` | `endpoints.md` |
| `mcp.tool.force_release_enrichment_lease` | `endpoints.md` |
| `mcp.tool.scan_for_conflicts` | `endpoints.md` |
| `mcp.tool.run_llm_conflict_review` | `endpoints.md` |
| `mcp.tool.requeue_conflicts_for_llm_review` | `endpoints.md` |
| `mcp.resource.system.dependency_health` | `endpoints.md` |
| `mcp.resource.system.processing_queue` | `endpoints.md` |

## Reading Pattern

1. Start with `README.md`.
2. Open the matching task router or small companion doc.
3. Use this file only if you need the exact id or owner.
4. Use `concept-ids.yaml` if you need the complete registry.
