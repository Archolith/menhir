---
description: Python style and project conventions
---

## Workspace Context

yawn.* is a Pokemon card market tracking workspace. Related projects include:

| Project | Purpose |
|---|---|
| yawn.rip | Spring Boot REST API |
| yawn.market | Spring Boot scraper/worker |
| yawn.seed | Spring Boot CLI catalog/bootstrap service |
| yawn.dashboard | React/TypeScript frontend |
| menhir | Python MCP memory server |
| yawn.delegate | Python MCP delegate server |
| yawn.bot | Python Discord bot |
| yawn.vps | Python MCP VPS management server |
| yawn.scheduler | Python task scheduler |

## Python Conventions

- Python 3.12+ baseline.
- Use builtin generics (`list[str]`, `dict[str, Any]`, `str | None`).
- Indent 4 spaces; max line length around 120.
- Use `%s` logging placeholders (no log f-strings).
- Prefer explicit return annotations for public functions.
- Keep modules small and composable; avoid overlong single-purpose files.

## Async and I/O

- Keep I/O-bound and external calls async when possible.
- Avoid `asyncio.run()` inside async paths.
- Use `asyncio.to_thread()` for any unavoidable blocking call.

## Logging

```python
logger = logging.getLogger(__name__)
logger.info("Connecting to neo4j at %s", uri)
```

## Error Handling

- Validate user/environment input before long-running operations.
- Keep failures explicit and actionable; avoid silent catches.
- Fail fast for hard dependency failures (missing env, no DB connectivity) when startup requires them.

## Testing Conventions

- Pytest is configured in `pytest.ini`.
- Markers:
  - `unit` for fast local checks
  - `online` for tests that need live Neo4j/LLM
  - `smoke` for fast baseline checks
- Use `pytest -m unit` for routine local loops.

## File Layout Preference

- Keep import order: stdlib, third-party, local.
- Keep `.env` keys in `.env.example` aligned with code.
- Use conventional commits (`feat:`, `fix:`, `refactor:`, `chore:`, `docs:`).
- Stage files explicitly by path; do not use `git add -A`.
