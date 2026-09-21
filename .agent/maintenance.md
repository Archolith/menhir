# Maintenance

Project maintenance rules for `menhir`.

## Update Rules

- Semi-large features need a short design note before implementation. Use `workflows/feature_planning.md`.
- New module or script entrypoint -> update `architecture.md`
- New memory/edge fields, Graphiti constraints, or storage defaults -> update `data_models.md`
- New MCP tool or resource added -> update `endpoints.md`
- Creating, copying, moving, archiving, or restoring a tracked artifact (plan, review,
  handoff, implementation report, reference) -> follow `workflows/artifact_authoring.md`,
  update the destination's routing index, and run `menhir artifacts validate .` before
  committing. A move keeps the artifact UUID; a copy needs a new one. An archive move needs
  an explicit terminal lifecycle decision in the same change — the directory does not choose
  between IMPLEMENTED, SUPERSEDED, and DEFERRED.
- New workflow, test task, or runbook change -> add or update files under `workflows/`
- Backend startup / readiness / launcher behavior changes -> update `workflows/operations_runbook.md` and `workflows/backend-first-mcp.md`
- Logging layout, request-id behavior, or API error-envelope changes -> update `workflows/logging-and-troubleshooting.md`
- Any change to project intent or phase gates -> update `memory-design.md` and/or `memory-roadmap.md`

## Verification Closeout

- Follow `workflows/run_and_test.md`; do not run the complete suite locally after every change.
- During implementation, run direct regression tests and the smallest credible affected set. At
  closeout, broaden to affected callers/contracts and matching static checks.
- The structural `affected_tests` result is advisory. Confirm it from source/tests, and never treat
  an empty result as proof that no tests are needed.
- Full offline and supported graph-backed integration coverage belongs to required CI on the exact
  reviewed SHA. Local full runs are exceptional and require a recorded reason.
- Closeout notes must list changed surfaces, selection basis, exact commands/results, unrun lanes,
  exact-SHA CI state, and residual risk. Documentation-only work must state why pytest was not run.

## Changelog

- Always add a CHANGELOG entry when finishing a session with meaningful changes.
- Only log changes made to this project (`menhir`).
- Never include changes from sibling projects.
- Use the format: `## YYYY-MM-DD - <short description>` with bullet points per file changed.
- Keep only the 10 most recent dated entries in `CHANGELOG.md`; rely on git history for older detail.

## Git Hygiene

- Push to git regularly, at minimum at the end of each working session.
- Use conventional commit messages: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`
- Only commit files you worked on this session.
- Never stage or commit files you did not read or modify.
- Never `git add .` or `git add -A`.
- Run `git diff --name-only` and `git status` before staging.
- Add files explicitly by path.
- If unrelated files appear modified, do not include them in the commit.
