# Maintenance

Project maintenance rules for `menhir`.

## Update Rules

- Semi-large features need a short design note before implementation. Use `workflows/feature_planning.md`.
- New module or script entrypoint -> update `architecture.md`
- New memory/edge fields, Graphiti constraints, or storage defaults -> update `data_models.md`
- New MCP tool or resource added -> update `endpoints.md`
- New workflow, test task, or runbook change -> add or update files under `workflows/`
- Backend startup / readiness / launcher behavior changes -> update `workflows/operations_runbook.md` and `workflows/backend-first-mcp.md`
- Logging layout, request-id behavior, or API error-envelope changes -> update `workflows/logging-and-troubleshooting.md`
- Any change to project intent or phase gates -> update `memory-design.md` and/or `memory-roadmap.md`

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
