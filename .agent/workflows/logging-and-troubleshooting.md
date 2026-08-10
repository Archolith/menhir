# Logging And Troubleshooting

Reference for `menhir` logs, request ids, and API error handling.

## Logging model

`menhir` now uses centralized Python `dictConfig` logging plus project-local rotating log files.
Uvicorn access records use Uvicorn's positional-record-aware formatter; raw
`logging.Formatter` cannot populate `client_addr`, `request_line`, or `status_code`.

Code anchor:

- `src/menhir/infrastructure/logging_config.py`

## Log files

All current backend logs live under:

- `projects/menhir/logs/`

Primary files:

- `server.log`
  - main application lifecycle and info/warning output
- `server.err.log`
  - warning/error-heavy stream
- `server.access.log`
  - HTTP access log
- `launcher.log`
  - PowerShell backend launcher decisions and readiness probes

## What to check first

### Backend did not start

Check:

```powershell
Get-Content .\logs\launcher.log -Tail 50
Get-Content .\logs\server.err.log -Tail 50
```

### Backend is up but behavior is wrong

Check:

```powershell
Get-Content .\logs\server.log -Tail 80
Get-Content .\logs\server.err.log -Tail 80
```

### HTTP request behavior

Check:

```powershell
Get-Content .\logs\server.access.log -Tail 80
```

## Request ids

Every HTTP request gets a request id.

Effects:

- response header: `x-request-id`
- API error envelopes include `request_id`
- exception logging includes the same request id

Use the request id to correlate:

- client-visible error
- access log entry
- application error log entry

## Background write errors (`x-menhir-bg-warnings`)

Fire-and-forget structural writes (`ingest_project` background re-scans) report errors through a sideband header rather than raising immediately.

If a background write fails, the next MCP tool call will:

1. Receive an HTTP response with `x-menhir-bg-warnings: ["...error message..."]`
2. `BackendClient` reads the header and stores warnings in a client-side queue
3. `BaseTool.execute` drains the queue and appends `[background-error] ...` lines to the tool output

The deprecated `x-yawn-bg-warnings` spelling is emitted with the same value during
the compatibility window so older backend clients continue to surface warnings.

If you see `[background-error]` lines in a tool response, they refer to a previous background write — not the current operation. Check `server.log` for the full traceback.

## API error envelope

HTTP errors now use a unified JSON shape:

```json
{
  "error": "validation_error",
  "detail": "...",
  "code": "validation_error",
  "request_id": "..."
}
```

Common cases:

- auth failures
- `HTTPException`
- validation failures
- unhandled 500s

500 responses should no longer leak raw exception text to clients.

## Noise reduction rules

Current logging intentionally suppresses routine noise from:

- `httpx`
- `httpcore`
- `neo4j`
- `neo4j.notifications`
- `posthog`
- `urllib3`

Routine Graphiti wake/request chatter was also reduced.

Important tradeoff:

- repetitive wake chatter is lower signal now
- real backend/scheduler degradation should still surface at warning/error level

## Launcher-specific troubleshooting

`start-server.ps1` now:

- waits for `/api/ready`
- resolves the real listening PID from the socket listener
- rewrites `.server.pid` once ready
- clears `launcher.log` and `server.err.log` at fresh launch

If startup feels flaky:

```powershell
.\scripts\start-server.ps1 restart
Get-Content .\logs\launcher.log -Tail 80
```

Useful launcher log messages:

- `start launching`
- `start launched pid=...`
- `readiness ok status=200`
- `start ready pid=...`
- `wait timed_out`
- `listener lookup failed`

## Common troubleshooting patterns

### Ready endpoint is down

Check:

1. launcher status
2. `launcher.log`
3. `server.err.log`

### MCP cannot connect

Check:

1. backend readiness at `/api/ready`
2. `MENHIR_BACKEND_URL`
3. MCP client config actually loading the intended `.env`

### Queue/enrichment issue

Use MCP tools/resources first:

- `list_enrichment_queue`
- `get_episode_trace`
- `memory://system/processing-queue`

Then correlate with:

- `server.log`
- `server.err.log`

### Auth or validation failure

Capture:

- HTTP status
- JSON error envelope
- `request_id`

Then search that request id in logs.
