# Post-install setup

`pip install` makes the Menhir CLI available. A usable installation also needs a configured checkout,
runtime dependencies, an MCP client connection, and whichever optional agent integrations the operator
has explicitly chosen.

## 1. Finish the safe checkout setup

Run this from the cloned repository:

```bash
menhir setup
menhir setup --check
```

The command is idempotent. It:

- creates `.env` from `.env.example` only when `.env` is absent;
- configures `core.hooksPath=.githooks` so the repository's pre-push protection is active;
- preserves an existing `.env` and refuses to replace a different Git hooks path without
  `--force-git-hooks`;
- reports the remaining runtime, MCP, and optional capture steps.

Use `--repo PATH` when running outside the checkout. The command needs a source checkout because the
Git hooks, service launcher, and optional producer scripts are repository-managed assets.

## 2. Configure and verify the runtime

Edit `.env` for one Neo4j 5 + APOC instance and one supported LLM/embedding configuration. Never put
real credentials in `.env.example` or an agent instruction file.

Then run:

```bash
menhir diagnostics
menhir check
menhir serve
```

`diagnostics` is an offline, redacted configuration snapshot. `check` verifies live dependencies.
After startup, `/api/health` confirms the process is alive and `/api/ready` confirms its dependencies:

```bash
curl -fsS http://127.0.0.1:8100/api/health
curl -fsS http://127.0.0.1:8100/api/ready
```

The public default port is `8100`. Set `MENHIR_TURNS_URL` and `MENHIR_TOOL_EVENTS_URL` explicitly if
the agent hooks must target another port.

## 3. Register the MCP client

Point HTTP-capable clients at `http://127.0.0.1:8100/mcp-http`. When authentication is enabled, use a
credential with the smallest tier the client needs. Stdio clients must set `MENHIR_BACKEND_URL` and
connect through `python -m menhir.mcp.server`; the stdio bridge does not start another runtime.

Client schemas and credential stores differ, so `menhir setup` does not rewrite MCP client config.
Validate the connection by listing tools, calling `query_structure` with `query_type="projects"`, and
using a read-only health or recall operation before enabling writes.

## 4. Install agent lifecycle hooks deliberately

Menhir has two separate hook families.

### Recall and checkpoint hooks

These package-native hooks inject bootstrap recall, post-compaction recall, and end-of-turn save nudges
into a Claude-compatible hook host:

```bash
menhir setup --install-claude-hooks --hook-location project --workspace <registered-workspace-key>
```

For a user-wide install, omit the workspace because user hooks are intentionally general-only:

```bash
menhir setup --install-claude-hooks --hook-location user
```

The standalone equivalent is `menhir hook install`. Re-running either command replaces only Menhir's
own entries and preserves unrelated hooks. Malformed JSON is rejected rather than overwritten.

### Evidence and file-event producers

TurnEvidence, memory-admission, file-event, and policy-guard producers are privacy- or policy-relevant
and are not silently enabled by setup. Review and install only the integrations you want:

- Claude Code and Codex: [`scripts/hooks/README.md`](../scripts/hooks/README.md)
- OpenCode: [`scripts/opencode-plugin/README.md`](../scripts/opencode-plugin/README.md)
- event and privacy contracts: [`turn-evidence-producers.md`](turn-evidence-producers.md) and
  [`hook-center-tool-events.md`](hook-center-tool-events.md)

Run each producer's `--health` and `--dry-run` checks before live use. The producers fail open, do not
call an LLM, and must never block the host agent.

## 5. Optional Windows service persistence

To start and monitor Menhir after login:

```powershell
menhir setup --install-watchdog
```

This installs the `menhir-watchdog` scheduled task through `scripts/start-server.ps1`. It is opt-in
because it changes login-time behavior. Inspect it with:

```powershell
.\scripts\start-server.ps1 status
```

Remove it with `.\scripts\start-server.ps1 uninstall-task`.

## 6. Give agents the operating contract

Use [`agent-usage.md`](agent-usage.md) as the explanation and copy
[`templates/AGENTS.menhir.md`](templates/AGENTS.menhir.md) into a consumer repository's agent
instructions. Replace the placeholder workspace and project keys instead of asking agents to infer them.

Finally, ingest each code repository before trusting empty structural results:

```text
ingest_project(path="<absolute-repository-path>", name="<project-key>")
query_structure(query_type="projects")
```
