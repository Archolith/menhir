# .agent/

Core bot-facing docs for `menhir`.

> **New chain / swapping in a fresh LLM?** For current local-MVP sequencing, start with
> [`../docs/roadmap/menhir-mvp-roadmap.md`](../docs/roadmap/menhir-mvp-roadmap.md) - read its
> **RECONCILED STATUS (2026-07-10)** banner for the live MVP board. The code-verified finding
> ledger is [`verified-current-findings-main-2026-07-10.md`](verified-current-findings-main-2026-07-10.md).
> Historical frontier/oracle context is archived at
> [`archive/plans/chain-handoff.md`](archive/plans/chain-handoff.md); it is not the current MVP roadmap.

## Quick Start

Do not preload the large reference docs.

Start with one file:

- install, post-install setup, Git/client hooks -> `../docs/post-install.md`
- writing default instructions for agents that use Menhir -> `../docs/agent-usage.md`
- debugging, incidents, queue problems -> `tasks-debugging.md`
- memory-review program: open fixes, hotfix status, decisions -> `memory-review-tracker.md`
- ingest, enrichment, stamping -> `tasks-ingest.md`
- **about to write a script?** -> `scripts-index.md` — READ THIS FIRST. Every durable instrument in
  BOTH menhir and `archolith-bench`, indexed by the question it answers. Two sessions in a row
  re-derived results an existing instrument already produced.
- measuring whether the scalar/View path worked (4-stage coverage matrix, live authority A/Bs)
  -> `workflows/scalar_state_measurement.md` — the instruments live in `archolith-bench`
- **creating or moving a plan/review/handoff/reference?** -> `workflows/artifact_authoring.md`
  — required metadata, which directory owns which type, what a move keeps, and the validation
  command to run before committing
- MCP tool or resource selection -> `tasks-mcp.md`
- backend startup, queue ops, and operator checks -> `workflows/operations_runbook.md`
- stdio/remote MCP connection setup -> `workflows/backend-first-mcp.md`
- logging, request ids, and API errors -> `workflows/logging-and-troubleshooting.md`
- who "the user" is, self-entity identity, or self forks -> `workflows/canonical-self-migration-runbook.md`
- purpose and principles -> `memory-foundations.md`
- the governance constitution (admission/assertion/authority/accountability/reversibility) -> `memory-governance.md`
- current STATE of artifacts, provenance, and governance (what's wired/dark/unwired, with live measurements) -> `artifacts-provenance-governance-status.md`
- policy, scope, lifecycle, scoring -> `memory-policy.md`
- retrieval tuning profiles (code-workspace vs anecdotal) -> `retrieval-profiles.md`
- query and ingest design -> `memory-ingest-queries.md`
- long-term vision, CIP positioning -> `memory-futures.md`
- open design questions, weak spots, roadmap-ish ideas -> `memory-backlog.md`
- live TODO on the shipped system (bugs, ops, deferred features) -> `post-v1-todo.md`
- known edge cases and testing gaps, by severity -> `edge-case-testing.md`
- shipped-but-default-off features (what's built but not live) -> `default-off-features.md`
- conflict-resolution history and suppression-recording semantics -> `conflict-resolution-history-proposal.md`
- exploring new View shapes (design exploration, not settled) -> `memory-view-kinds-frontier-transfer.md`
- current local-MVP roadmap -> `../docs/roadmap/menhir-mvp-roadmap.md`
- current executable plans and dependency order -> `plans/README.md`
- useful non-executable design, research, and negative evidence -> `reference/README.md`

Use `concept-ids.md` only when you need an exact concept id or owner doc.
Use `concept-ids.yaml` only when you need the full registry.
Use `concept-tree-design.md` only when editing the tree/document structure itself.
Use `maintenance.md` for maintenance, changelog, and git policy.
Use `verified-current-findings-main-2026-07-10.md` (reconciled against `main`) for the current
verified bug and hardening list. The older `verified-current-findings.md` is superseded
(frontier-baselined) and carries a banner pointing to the reconciled ledger.
Use `file-index.md` if you need the full doc inventory.
Use `workflows/feature_planning.md` before any semi-large feature or architectural expansion.

The `user/` folder is human-readable fallback material. Bots should stay in the main `.agent`
docs unless the core docs are still unclear.

## Entry Docs

Fast routing:

- Operator path -> `workflows/operations_runbook.md`, then `workflows/logging-and-troubleshooting.md`
- Feature path -> `workflows/feature_planning.md`, then `architecture.md`
- Debugging path -> `tasks-debugging.md`, then `workflows/troubleshoot_enrichment_stalls.md` if the problem is queue/LLM related

Read these before any large reference file:

- `tasks-debugging.md`
- `tasks-ingest.md`
- `tasks-mcp.md`
- `memory-foundations.md`
- `memory-policy.md`
- `memory-ingest-queries.md`
- `memory-futures.md`
- `glossary.md`

Only then open targeted sections from:

- `architecture.md`
- `data_models.md`
- `endpoints.md`
- `memory-design.md`

## Benchmarking Doc Cost

Use the local benchmark when you want to compare the token cost of the core `.agent` path against
the human-readable `user/` path for the same task.

Example:

```bash
python .agent/tools/benchmark_doc_tokens.py
```

Prompt-aware table:

```bash
python .agent/tools/benchmark_doc_tokens.py --show-prompts
```

Warm-path comparison:

```bash
python .agent/tools/benchmark_doc_tokens.py --warm
```

Preferred mode:

- install `tiktoken` if you want model-oriented counts
- default encoding is `o200k_base`
- the benchmark creates and uses a local `.agent/test_tmp/tiktoken` temp directory before initializing `tiktoken`
- if `tiktoken` is not installed, the tool falls back to a rough `chars / 4` estimate
- `--warm` excludes the common entry docs so you can compare incremental per-task doc cost
- scenarios include explicit task prompts so the benchmark reflects real asks, not only scenario ids

## Structural Code Graph

Before grepping or globbing to explore code, use `query_structure` — it queries the pre-indexed graph and is faster and cheaper than file searches.

```
query_structure("projects")                                          # list all ingested projects
query_structure("overview", project="menhir")                  # entity/edge counts, stack
query_structure("files", project="menhir", path="src/...")     # files in a directory (with module docstring summaries)
query_structure("imports", project="menhir", path="<file>")    # import graph for a file
query_structure("endpoints", project="menhir")                 # all MCP tools + HTTP routes
query_structure("tests", project="menhir")                     # test → source mappings
query_structure("blast_radius", project="menhir", path="...")  # downstream impact of a change
query_structure("affected_tests", project="menhir", path="...") # minimal test set for changed files
query_structure("symbols", project="menhir", path="<file>")    # classes/functions/methods in a file or dir
query_structure("context", project="menhir", path="<file>")    # summary + symbols + imports for a file
```

Required workflow:
- start with `query_structure("projects")` and confirm the repo is listed before trusting project-scoped structure queries
- if the repo is missing, run `ingest_project(path="<absolute repo path>", name="<project-name>")` first
- treat `No files found ...` or `No test mappings found ...` as potentially "not ingested yet" until you confirm the project appears in `projects`

Fall back to Grep/Read only when you need actual file content. Use `ingest_project(path)` to re-scan after significant structural changes (new files, renamed modules). The structure watcher re-scans automatically every 30 minutes for already-ingested projects; it does not auto-discover new repos.

## Project-Level Constraints

- This project is a long-lived graph memory service and is expected to run with a Neo4j backend.
- The codebase is a **complete v1 service** (graph/MCP/REST/runtime, OAuth, Hook Center, Phase 3
  consolidation). As of 2026-07-10 the local MVP is near-complete — only the M1 fresh-Neo4j benchmark
  remains (see `../docs/roadmap/menhir-mvp-roadmap.md`).
- Keep external service usage (Graphiti + LLM endpoints) behind environment variables in `.env` only.
