# Menhir memory for Hermes Agent

Use Menhir as Hermes Agent's long-term memory backend: relevant context is recalled automatically,
and Hermes gets `menhir_recall` and `menhir_status` tools for targeted searches and connectivity
checks. The `menhir_remember` proposal tool is **off by default**. No conversation transcripts,
assistant messages, tool outputs or automatic memory-file writes are mirrored into Menhir.

This directory is a standalone **Hermes directory memory provider**. It does not change Menhir's
server or require a Hermes fork. Requires a Hermes version supporting the
[`MemoryProvider` plugin interface](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin).
The plugin has no third-party Python dependencies.

## Install

1. Make sure a Menhir REST server is running. For local installs, `menhir check` verifies
   dependencies, and `menhir serve` starts the service (the default REST root is
   `http://127.0.0.1:8100/api`). The server must be running during Hermes use.

2. From the **Menhir repository root**, copy the provider into your active Hermes profile:

   ```bash
   mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
   cp -R integrations/hermes/menhir "${HERMES_HOME:-$HOME/.hermes}/plugins/menhir"
   hermes plugins list
   ```

   `menhir` should appear as an available memory provider. If you use a named Hermes profile,
   point `HERMES_HOME` at that profile's home **for both the copy and the Hermes commands**.

3. Configure and select it with Hermes' wizard:

   ```bash
   hermes memory setup
   hermes memory status
   ```

   Select **menhir**. Enter the REST endpoint, optional namespace (blank for the server default),
   and an agent-tier bearer key if your Menhir server requires authentication. The wizard stores
   the key as `HERMES_MENHIR_TOKEN` in Hermes' profile-scoped secret environment, not in this
   provider's JSON. For a hosted Menhir instance, use **HTTPS**, not plain HTTP.

   Alternatively select the provider without the wizard using
   `hermes config set memory.provider menhir` and use the dashboard Memory settings to configure
   the connection. Restart existing Hermes sessions after changing provider/settings.

4. Start a new Hermes chat and ask Hermes to call `menhir_status`, then discuss a subject
   you previously discussed with Menhir. For a specific check, ask `menhir_recall`. If it finds
   nothing, verify the endpoint, token tier and namespace before assuming the data is missing.
   A failed explicit tool call reports an HTTP/connection error; a failed automatic prefetch logs
   a warning and lets the Hermes turn continue.

5. Optional: once read-only recall works, enable `write_enabled` in the memory setup/dashboard,
   then start a new Hermes session. Ask Hermes to remember one specific fact. The
   `menhir_remember` tool **stages** it locally and returns a random approval ID; it does not
   contact Menhir. To save it, send the exact message shown by the tool in your **next user turn**:

   ```text
   Approve Menhir memory <approval_id>
   ```

   Or send `Reject Menhir memory <approval_id>` to discard it. Approval only works from the
   original user's session (and matching author identity, where Hermes supplies one), with a
   non-bot user-authored message. IDs expire after 24 hours. A failed server write retains the
   proposal; repeat the approval message after fixing connectivity. After approval, check the
   write `status`: `queued` means enrichment is pending, **not** that the memory is searchable.
   Use `menhir_recall` later to verify the completed result.

## Using Menhir instead of Hermes' two built-in memory files

Hermes normally retains `MEMORY.md` and `USER.md` alongside an external provider. To make Menhir
the only **persistent memory tool**, disable those built-in stores in the same profile:

```bash
hermes config set memory.memory_enabled false
hermes config set memory.user_profile_enabled false
```

Hermes' external memory provider is unaffected by these two flags. Do **not** put `memory` into
`agent.disabled_toolsets`, which hides external provider tools as well. Hermes' procedural skills
remain separate and are not migrated. Existing Hermes memory-file contents are **not** imported.

## Configuration and data boundary

Non-secret settings are stored per profile in `$HERMES_HOME/menhir/config.json`, managed by the
setup wizard and dashboard. They are validated when loaded:

| Setting | Default | Effect |
|---|---|---|
| `base_url` | `http://127.0.0.1:8100/api` | Menhir REST endpoint; HTTPS required off loopback |
| `namespace` | empty | Use Menhir server default, or choose an existing scope |
| `write_enabled` | `false` | Expose `menhir_remember` proposals; user message required for every write |
| `max_tokens` | `800` | `/api/context` budget; allowed 100–2000 |
| `timeout_seconds` | `2.5` | Per HTTP call; allowed 0.25–10 seconds |

`HERMES_MENHIR_TOKEN` is a **secret**, scoped by Hermes to the active profile. It is sent as a
Bearer token; no credentials go in the URL or the JSON settings. Server-side pinned namespaces
still take precedence over the requested namespace. The provider never offers a per-tool
namespace override.

Every non-trivial user prompt sends up to 2,500 characters of **that prompt** to the configured
Menhir endpoint for `/api/context`, unless a local heuristic spots an obvious credential.
This is a defense-in-depth check, **not** a complete privacy/DLP guarantee; Hermes also redacts
secrets at its provider boundary. Recalled context returns to Hermes as an untrusted historical
data block (capped at 6,000 characters). Explicit searches send their queries to `/api/recall`,
but refuse queries with obvious credentials. Repeated identical context queries are cached in
memory for up to 45 seconds per session. After three consecutive failures, automatic recall
pauses for 30 seconds to avoid hammering an offline server; explicit tools remain usable.

When you enable writes, `menhir_remember` stores the proposed episode in the profile-local,
owner-only `$HERMES_HOME/menhir/pending/` directory. Its random ID is required in the separate
user approval message; bots and different sessions cannot approve it. Only after approval does
the adapter send the episode to `/api/memory` as `source=hermes-agent`, `role=agent`, with a stable
idempotency key. The episode is **not** attributed as directly witnessed user evidence, and no
`/api/turn-evidence` requests are made. `sync_turn`, `on_memory_write`, compression and
session-end hooks remain no-ops. Delete old pending proposal files after they're no longer needed;
expired proposals are rejected and removed when someone tries to approve them.

Menhir outages cannot block the whole Hermes conversation; the synchronous prefetch can add up to
the configured timeout per substantive turn. Automatic prefetch failures are visible in Hermes
logs, while explicit tool calls return a structured error without server-body or token contents.
The adapter does not implement asynchronous first-turn recall, OAuth refresh, bulk import, deletion
or transcript recovery.

## Local checks

From the Menhir repository root (no Hermes/Menhir server required):

```bash
python -m unittest discover -s tests -p 'test_hermes_memory_provider*.py' -v
python -m compileall -q integrations/hermes/menhir
```

The unit tests validate the Hermes-facing provider contract using a fake SDK and mock responses;
the HTTP test makes real loopback requests to a small Menhir-shaped test server, including
authentication, namespace and the two-step write. They do **not** prove your real Hermes app,
Menhir authentication policy, or graph-backed recall works. Check those manually with
`hermes memory status`, `menhir_status` and a real `menhir_recall` after setup.
