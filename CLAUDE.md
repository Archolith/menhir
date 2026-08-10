# menhir

> **MCP sync** *(maintainer-only; does not apply to outside contributors)*: in the
> maintainer's workspace this project's MCP server entry is generated from a central
> registry. If the entry point, args, env, or cwd change, that registry must be updated and
> regenerated so all clients (Claude, Gemini, opencode, Qwen) stay in sync.

Read everything in [`.agent/`](.agent/) before starting work — it contains project context, architecture, data models, workflows, and maintenance rules.
