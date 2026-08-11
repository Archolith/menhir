# Menhir agent instructions

These instructions apply to the entire repository.

## Start here

1. Read `.agent/README.md`. It is a router; do not preload all of `.agent/`.
2. Follow the task-specific route it names. Read large architecture, data-model, endpoint, or memory-design
   references only when the task requires them.
3. Before code exploration, confirm Menhir appears in `query_structure(query_type="projects")`. Use
   structural queries before filesystem search, and check `blast_radius` once per file before editing.
4. Verify the repository root, current branch, and worktree status. Preserve unrelated changes and stage
   files explicitly.

## Implementation rules

- Python 3.12+, builtin generics, explicit public return types, and small composable modules.
- Keep external LLM, embedding, Neo4j, and auth configuration in environment variables. Never commit or
  record secrets.
- Extend the canonical backend/runtime and MCP contracts instead of creating parallel paths.
- Treat stale structural anchors and incomplete indexes as inconclusive until current code is checked.
- Use `.agent/workflows/feature_planning.md` before a semi-large or cross-cutting change.
- Use `.agent/workflows/code_conventions.md` for code style and `.agent/maintenance.md` for changelog and
  closeout requirements.

## Verification

- Prefer focused tests from `query_structure(query_type="affected_tests")`.
- Routine safe suite: `pytest tests/ -m unit -q` (serial; do not use `-n auto` on the maintainer machine).
- Live Neo4j/LLM tests require explicit opt-in and the documented environment.
- Report commands actually run and distinguish failures from tests that were not run.

For instructions that consumer agents can copy into repositories using Menhir, see
`docs/templates/AGENTS.menhir.md` and `docs/agent-usage.md`.
