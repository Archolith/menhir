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

- Run only tests relevant to the changed modules on the maintainer machine. Start with
  `query_structure(query_type="affected_tests")`, add direct contract/regression tests for every changed
  deployment boundary, and run the matching lint/static checks.
- Do not run the complete repository suite locally as a routine implementation or deployment step. Push the
  reviewed commit and let required CI run the complete suite on that exact SHA. Production publication or
  promotion must remain blocked until all required CI checks for that SHA are green.
- If CI fails, reproduce the failing test plus its affected neighbors locally, fix them, and push the new SHA;
  do not repeatedly rerun the complete suite on the maintainer machine. A broad local run is exceptional and
  requires an explicit reason such as changing test collection or shared test infrastructure.
- Live Neo4j/LLM tests require explicit opt-in and the documented environment.
- Report commands actually run and distinguish failures from tests that were not run.

For instructions that consumer agents can copy into repositories using Menhir, see
`docs/templates/AGENTS.menhir.md` and `docs/agent-usage.md`.
