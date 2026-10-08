# Post-install setup

`pip install` makes the Menhir CLI available. A usable installation also needs configuration,
runtime dependencies, an MCP client connection, and whichever optional agent integrations the operator
has explicitly chosen.

If upgrading from Menhir v0.2.3, install into a fresh virtual environment. That version may have
installed upstream `graphiti-core`; current Menhir uses `archolith-graphiti-core==0.30.2.post3`.
Both distributions write the `graphiti_core` package, so an in-place upgrade or later upstream
reinstall can leave mixed files even when `pip check` reports no conflict. From the repository
checkout, a fresh installation is:

```bash
python -m venv .venv
.venv/bin/python -m pip install .
```

On Windows, use `.\.venv\Scripts\python.exe -m pip install .` for the second command. To repair an
existing virtual environment instead, remove both distributions before reinstalling Menhir:

```bash
python -m pip uninstall -y graphiti-core archolith-graphiti-core
python -m pip install .
```

Use that environment's Python for the repair commands. If import fails because
`GraphitiRequestTooLargeError` is missing, the installed `graphiti_core` files do not match the
required fork.

## 1. Finish the safe checkout setup

Run this from the cloned repository:

```bash
menhir setup
menhir setup --check
```

The command is idempotent. It:

- creates `.env` from `.env.example` only when `.env` is absent;
- with `--provider local|openai`, writes a consistent provider block (chat, Graphiti LLM, Graphiti
  embed) and makes sure the provider's URL/model/key lines are present without ever overwriting a
  filled-in secret or custom model. Missing or blank OpenAI models use `gpt-4o-mini` and
  `text-embedding-3-small`; local URL/chat defaults match the settings model, while the local
  embedding model still requires operator configuration. Explicit feature opt-outs are preserved.
  With `--compose-neo4j`, points `NEO4J_*` at the root `docker-compose.yml`
  instance (`neo4j/password`).
- configures `core.hooksPath=.githooks` so the repository's pre-push protection is active;
- preserves an existing `.env` and refuses to replace a different Git hooks path without
  `--force-git-hooks`;
- reports the remaining runtime, MCP, and optional capture steps.

Use `--repo PATH` to select a source checkout explicitly. A plain wheel install uses
`MENHIR_STATE_DIR` (default `~/.menhir`) for generated configuration and bundled Neo4j compose.
Git hooks and repository-managed producer scripts require a checkout.

## Configuration across upgrades and opt-outs

Setup copies `.env.example` only when `.env` is absent. A package upgrade or setup rerun does
not merge a newer template into an existing file. `--provider` explicitly selects the provider;
missing or blank model/URL defaults are filled, while custom values and secrets are retained.

For normal startup, process/service environment wins over the selected `.env`, then unset
settings use the installed version's code defaults. `ENV_FILE` selects the file; `menhir up`
otherwise selects its checkout/state `.env`. Removing a setting restores the current code
default, not the previous release's behavior. Current code has no separate feature-default
policy for fresh versus existing installs. Before any future new-install-only promotion, its
owner must choose an implementation that distinguishes those paths or explicitly preserve the
upgrade configuration. No promotion is authorized by this audit.

To opt out, set explicit `false` in the selected configuration and remove any conflicting
process/service override, then restart the backend and workers that captured the settings.
For example, the already-shipped source-memory lane uses:

```dotenv
MENHIR_FRONTIER_SOURCE_MEMORIES=false
```

This disables its recall section and new ingest-side episode-content embedding step. It does
not delete stored embeddings or undo earlier writes. Boolean switches accept `true`, `1`, or
`yes` (case-insensitive); `false`, `0`, `no`, blank and unrecognized strings all load as false.
Use explicit `false` rather than a blank or misspelled opt-out. Commented template lines do not
set values; the evidence-anchor example is a code-corpus choice, not its code default.

Scalar state, scalar/history authority, event history, Wardens and verifier sync remain opt-in.
The canonical event variables are `MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED` and
`MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED`; the shorter `MENHIR_EVENT_HISTORY_*`
names are not aliases. Disabling a feature stops its gated paths and leaves persisted data in
place; this is not a database rollback. Scalar/event processing can revisit historical evidence
when enabled or when perceiver versions change. Review namespace/version and historical-data
obligations before activation; do not run a backfill or ingest simply to upgrade the package.
Keep configuration and verified data backups for a separately approved deployment rollback.

## 1b. Or do it in one step

`menhir up [--compose-neo4j] [--provider local|openai]` runs the env step above, starts the root
compose Neo4j when asked, waits for Bolt, prints a tier report (each missing capability with the
`.env` key that fixes it), and hands off to `menhir serve`. `menhir up --check` stops after the
report and starts nothing. The steps below are the same path taken manually.

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
Both read `.env` from the current directory (or `ENV_FILE`), the same file `serve` uses.
After startup, `/api/health` confirms the process is alive and `/api/ready` confirms its dependencies:

```bash
curl -fsS http://127.0.0.1:8100/api/health
curl -fsS http://127.0.0.1:8100/api/ready
```

The public default port is `8100`. Set `MENHIR_TURNS_URL` and `MENHIR_TOOL_EVENTS_URL` explicitly if
the agent hooks must target another port.

## 3. Register the MCP client

For the supported local MVP path, keep `menhir serve` running and configure the client to launch
`python -m menhir.mcp.server` with `MENHIR_BACKEND_URL=http://127.0.0.1:8100` in the client's
environment. Use the Python installation that contains Menhir. If the backend has tier keys,
pass its `MENHIR_AGENT_KEY` to the client too. The [stdio client example](../README.md#connect-an-mcp-client)
shows both settings. The stdio bridge connects to the running backend; it does not start one.

HTTP-capable clients can also connect to `http://127.0.0.1:8100/mcp-http`. When authentication is
enabled, use a credential with the smallest tier the client needs.

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

## 5b. Preview a remote structure sync (code the server cannot see)

`ingest_project` scans its path in the server process, so it only works when the code and the
server are on the same machine. If your Menhir runs elsewhere, `menhir sync` is the path being
built for that -- a sanitized snapshot of your repository, uploaded over MCP.

The local half works today:

```powershell
menhir sync . --check
```

It reports what a sync would upload -- file count, content bytes, an archive size bound, the tree
digest, deletions, what is deliberately omitted, and anything refused -- and it makes no network
call and writes no archive. Only files git tracks are considered, taken from your working tree, so
uncommitted edits show up and ignored files never do.

Real `.env` files and private keys block the preview rather than being quietly skipped. If one is
genuinely safe to send, add `--allow-path <path>`; the override applies to that run only.

Uploading is not available yet: plain `menhir sync` refuses and says why.

## 6. Give agents the operating contract

Use [`agent-usage.md`](agent-usage.md) as the explanation and copy
[`templates/AGENTS.menhir.md`](templates/AGENTS.menhir.md) into a consumer repository's agent
instructions. Replace the placeholder workspace and project keys instead of asking agents to infer them.

Finally, ingest each code repository before trusting empty structural results:

```text
ingest_project(path="<absolute-repository-path>", name="<project-key>")
query_structure(query_type="projects")
```
