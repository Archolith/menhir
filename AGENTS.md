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
<!-- beacon:guardrail id=no-secrets severity=high -->
- Keep external LLM, embedding, Neo4j, and auth configuration in environment variables. Never commit or
  record secrets.
<!-- /beacon -->
<!-- beacon:guardrail id=canonical-contracts severity=medium -->
- Extend the canonical backend/runtime and MCP contracts instead of creating parallel paths.
<!-- /beacon -->
<!-- beacon:guardrail id=incomplete-index-inconclusive severity=high -->
- Treat stale structural anchors and incomplete indexes as inconclusive until current code is checked.
<!-- /beacon -->
- Use `.agent/workflows/feature_planning.md` before a semi-large or cross-cutting change.
- Use `.agent/workflows/code_conventions.md` for code style and `.agent/maintenance.md` for changelog and
  closeout requirements.

## Verification

- Follow `.agent/workflows/run_and_test.md`; it is the authoritative risk-based test and reporting policy.
- Run the smallest credible tests for the changed behavior on the maintainer machine. Use
  `query_structure(query_type="affected_tests")` as one selection input, then confirm it from callers,
  contracts, and existing tests because an empty or stale structural mapping is not evidence that no test
  is needed. Add direct contract/regression tests for every changed deployment boundary and run matching
  lint/static checks on changed paths.
<!-- beacon:guardrail id=ci-before-publication severity=high -->
- Do not run the complete repository suite locally as a routine implementation or deployment step. Push the
  reviewed commit and let required CI run the complete suite on that exact SHA. Production publication or
  promotion must remain blocked until all required CI checks for that SHA are green.
<!-- /beacon -->
- If CI fails, reproduce the failing test plus its affected neighbors locally, fix them, and push the new SHA;
  do not repeatedly rerun the complete suite on the maintainer machine. A broad local run is exceptional and
  requires an explicit reason such as changing test collection or shared test infrastructure.
<!-- beacon:guardrail id=live-tests-opt-in severity=medium -->
- Live Neo4j/LLM tests require explicit opt-in and the documented environment.
<!-- /beacon -->
- Report commands actually run and distinguish failures from tests that were not run.

For instructions that consumer agents can copy into repositories using Menhir, see
`docs/templates/AGENTS.menhir.md` and `docs/agent-usage.md`.
