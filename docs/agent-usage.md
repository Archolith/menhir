# Agent operating contract

Menhir is most useful when an agent treats it as two related systems: durable semantic memory and a
structural code graph. The client must supply stable workspace, namespace, project, reader, and file
context instead of asking Menhir to guess them.

## Recommended session flow

1. Bootstrap in two phases. Call `read_flagged_memories` first, then
   `recall_context_memories`, using the same stable `reader_id` and registered `workspace` key.
2. Before exploring code, call `query_structure(query_type="projects")`. If the target is absent, call
   `ingest_project` before trusting an empty structural answer.
3. Use `query_structure` for files, imports, symbols, endpoints, tests, context, and dependencies. Before
   editing a file, call `blast_radius` once for that file; use `affected_tests` to choose focused checks.
4. Use targeted `recall_memories` when a decision, failure, preference, or prior implementation fact could
   change the work. Pass `file_context` and `file_context_project` for code-related questions.
5. Verify stale anchors against the current file. An incomplete or stale project index makes an empty result
   inconclusive, not proof that no dependency exists.
6. After using recall output, call `rate_recall` honestly. This records retrieval quality; it does not alter
   memory ranking.
7. At the end of meaningful work, store durable lessons with `add_memory`. Attach a bounded Git diff when
   code paths matter. Record remaining work as a todo with a repository-relative code reference.

## Write discipline

- Store durable facts, decisions, corrections, verified failures, and reusable operator knowledge.
- Do not store secrets, raw credentials, transient progress narration, or facts that have not been checked.
- `add_memory` returning `PENDING` means the write was accepted and enrichment continues asynchronously.
  Do not submit the same memory again. Use `add_memory_and_track` only when the current task truly needs to
  wait for enrichment.
- Use `TEMPORAL` with `valid_at` for time-bound reminders. Use workspace/namespace fields explicitly; a
  workspace bootstrap key, a semantic namespace, and a structural project key are different identifiers.
- Destructive and administrative tools require stronger authority than recall. Use the least-privileged
  client tier that can perform the task.

## Failure behavior

- If Menhir is unavailable, do not pretend its history or structural graph was checked. Continue only when
  the task can safely rely on current local evidence, and state the limitation.
- If a project is missing from `query_structure(query_type="projects")`, ingest or re-ingest it before
  relying on project-scoped queries.
- If recall marks a file anchor stale, inspect the current file before acting on the memory.
- If tool discovery is incomplete, use the MCP client's tool discovery mechanism instead of inventing a
  tool name or argument.

## Extension foundation status

Menhir does not yet expose a stable extension-author API, generic assertion write endpoint, dynamic
plugin loader, or production generic projection scheduler. Do not import private infrastructure and
describe it as a supported extension seam, and do not infer that a plan or proposed ADR represents
available runtime behavior.

The implementation target is routed by the
[foundation completion plan](../.agent/plans/menhir-foundation-completion-2026-08-30.md). Its proposed
[generic assertion/currentness ADR](../.agent/adr/0002-generic-assertion-currentness-and-journal.md)
defines the identity and journal decisions still awaiting owner acceptance. Until the phases land,
consumer agents should use the existing public MCP/REST operations and treat scalar/event admission,
currentness, scheduling, and feature flags exactly as documented by the current runtime.

## Ready-to-copy default

The maintained, paste-ready instruction block is
[`templates/AGENTS.menhir.md`](templates/AGENTS.menhir.md). Keep it short in consumer repositories and link
back here for explanation instead of duplicating the full Menhir reference documentation.
