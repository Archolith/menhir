# Menhir memory for Hermes Agent

Use Menhir as Hermes Agent's long-term memory backend: relevant context is recalled automatically,
and Hermes gets a `menhir_recall` tool for more specific searches. The `menhir_remember` tool is
**off by default**. No conversation transcripts, assistant messages, tool outputs or automatic
memory-file writes are mirrored into Menhir.

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

4. Start a new Hermes chat and ask about a subject you previously discussed with Menhir.
   For a specific check, ask Hermes to call `menhir_recall` with a narrow question. If it finds
   nothing, verify the endpoint, token tier and namespace before assuming the data is missing.
   A failed explicit tool call reports an HTTP/connection error; a failed automatic prefetch logs
   a warning and lets the Hermes turn continue.

5. Optional: once read-only recall works, enable `write_enabled` in the memory setup/dashboard,
   then start a new Hermes session. The `menhir_remember` tool appears, and only an explicit
   agent tool call sends a chosen episode to Menhir. Confirm its `status`: `queued` means
   enrichment is pending, **not** that a memory is already searchable. Use `menhir_recall`
   afterwards to verify it has become available.

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
| `write_enabled` | `false` | Expose `menhir_remember`; no automatic writes |
| `max_tokens` | `800` | `/api/context` budget; allowed 100–2000 |
| `timeout_seconds` | `2.5` | Per HTTP call; allowed 0.25–10 seconds |

`HERMES_MENHIR_TOKEN` is a **secret**, scoped by Hermes to the active profile. It is sent as a
Bearer token; no credentials go in the URL or the JSON settings. Server-side pinned namespaces
still take precedence over the requested namespace. The provider never offers a per-tool
namespace override.

Every non-trivial user prompt sends up to 2,500 characters of **that prompt** to the configured
Menhir endpoint for `/api/context`. Recalled context returns to Hermes as an untrusted historical
data block (capped at 6,000 characters). Explicit searches send their queries to `/api/recall`.
If you enable writes, explicit `menhir_remember` calls send only the chosen episode to `/api/memory`
as `source=hermes-agent`, `role=agent`, with a stable idempotency key. They are **not** attributed
as directly witnessed user evidence, and no `/api/turn-evidence` requests are made. `sync_turn`,
`on_memory_write`, compression and session-end hooks remain no-ops.

Menhir outages cannot block the whole Hermes conversation; the synchronous prefetch can add up to
the configured timeout per substantive turn. Automatic prefetch failures are visible in Hermes
logs, while explicit tool calls return a structured error without server-body or token contents.
The adapter does not implement OAuth refresh, bulk import, deletion or transcript recovery.

## Local checks

From the Menhir repository root (no Hermes/Menhir server required):

```bash
python -m unittest discover -s tests -p test_hermes_memory_provider.py -v
python -m compileall -q integrations/hermes/menhir
```

The tests validate the Hermes-facing provider contract using a fake SDK and mocked HTTP responses.
They do not prove your actual server, authentication policy, or graph-backed recall works. Check
those manually with `hermes memory status` and a real `menhir_recall` after setup.
