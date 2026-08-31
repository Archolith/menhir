# Operations Runbook

Operational runbook for the backend-first `menhir` service.

For an immutable deployment or promotion on the live VPS, use the canonical
[live VPS deployment playbook](../../deploy/LIVE_VPS_PLAYBOOK.md). The local
Windows startup instructions below are not a production deployment path.

For MCP connection details, see [backend-first-mcp.md](backend-first-mcp.md).
For log files, request ids, and error envelopes, see [logging-and-troubleshooting.md](logging-and-troubleshooting.md).
For enrichment-specific stall diagnosis, see [troubleshoot_enrichment_stalls.md](troubleshoot_enrichment_stalls.md).

## Runtime model

`menhir` now has one canonical runtime owner:

- `menhir serve`
  - owns Neo4j/Graphiti services
  - owns queue recovery and maintenance scheduler
  - exposes REST and remote MCP over HTTP

Client surfaces:

- stdio MCP (`menhir.mcp.server`)
  - requires `MENHIR_BACKEND_URL`
  - does not own runtime bootstrap
- remote MCP (`/mcp/*`)
  - tool-only HTTP-mounted MCP surface
- REST (`/api/*`)
  - canonical health/readiness/stats and memory endpoints

### Planned generic projection boundary (not implemented)

The generic projection foundation is planned only. The generic projection host, ordered-journal
consumer, temporal wakeups, runtime manifest digest, typed corruption states, writer census, and
cutover receipts are not implemented operator surfaces and have no current commands or readiness
fields. The proposed [master plan](../plans/menhir-foundation-completion-2026-08-30.md),
[Phase 2 runtime plan](../plans/menhir-foundation-phase-2-runtime-orchestration-2026-08-30.md),
[Phase 4 cutover plan](../plans/menhir-foundation-phase-4-developer-surface-and-cutover-2026-08-30.md),
and [ADR 0002](../adr/0002-generic-assertion-currentness-and-journal.md) describe a future target;
ADR 0002 is **PROPOSED**, not accepted. Operators must not infer or enable a generic scheduler or
public extension surface from those documents. Current scalar- and event-specific repositories,
writers, and behavior remain authoritative; the existing scheduler and readiness fields documented
below do not imply generic projection orchestration.

When implemented, projection readiness belongs to the backend runtime owner (`menhir serve`), never
to stdio MCP. Backend readiness must fail closed on runtime-manifest or adapter-digest drift and on a
missing active definition. Diagnostics must expose per-definition work, freshness, and corruption,
plus durable journal cursor and census-watermark state.

Future production activation must follow **Expand → read-only Backfill → Drain** (including the
atomic authority flip and post-flip materialization) **→ Verify → Enforce → Contract**. Drain and
Verify each require separate 7-day continuous attested windows; Contract requires a 14-day
continuous attested window. Old-image rollback is allowed only before the first durable production
mutation. After that boundary, recovery must roll forward to a certified fence-aware release or use
a verified reverse generation with an atomic authority flip.

## Startup

### Windows

Preferred launcher:

```powershell
cd C:\Users\you\IdeaProjects\projects\archolith\menhir
.\scripts\start-server.ps1 start
```

Supported actions:

```powershell
.\scripts\start-server.ps1 start      # enable watchdog task, start in background, wait for /api/ready
.\scripts\start-server.ps1 stop       # disable watchdog task, stop watchdog + server, release pid files
.\scripts\start-server.ps1 restart    # disable/stop, wait for port release, then enable/start
.\scripts\start-server.ps1 status     # one-line: server / watchdog / watchdog task / bind / neo4j / http
.\scripts\start-server.ps1 console    # live rich dashboard (starts server if down)
.\scripts\start-server.ps1 logs       # live-tail logs/server.log (Ctrl+C to stop)
```

Behavior:

- reads `MENHIR_API_HOST` / `MENHIR_API_PORT` from `.env`
- probes wildcard binds through loopback (`0.0.0.0` -> `127.0.0.1`) for readiness reporting
- uses project-local logs under `projects/menhir/logs/`
- probes the remote Neo4j bolt port (host:port parsed from `NEO4J_URI` in `.env`) before launch
  and warns if unreachable, but starts anyway so the server's own preflight reports the
  connectivity error clearly. Neo4j is **not** local Docker -- it runs as `menhir-neo4j.service`
  (systemd) on the remote host named by `NEO4J_URI`; the desktop no longer runs Docker for
  menhir's Neo4j at all, and the root `docker-compose.yml` is vestigial.
- waits for `/api/ready` before reporting success (`start`)
- rewrites `.server.pid` to the actual listening backend PID once ready
- `start` re-enables the installed `menhir-watchdog` scheduled task before launching
- `stop` disables the scheduled task before stopping the watchdog process and server, so the
  one-minute trigger cannot bring the service back up
- `restart` waits for the previous server to release the bind port before starting,
  then re-enables the scheduled task and launches a fresh watchdog

`status` prints a single line, e.g.:

```text
menhir  server=PID 40752  watchdog=PID 45340  watchdog_task=enabled/Ready  bind=127.0.0.1:8090  neo4j=up  http=ready
```

If startup reports "launched but did not report ready within timeout", check the log files before retrying. A common failure is the remote Neo4j host being unreachable (network down, host powered off, or the `menhir-neo4j` service not running there), which shows up as `neo4j_ready: false` and `"Neo4j connectivity check failed."` in `/api/ready`'s `failures` list instead of a clean ready state. See [Recover from remote Neo4j-unreachable startup failure](#recover-from-remote-neo4j-unreachable-startup-failure) below.

### Console dashboard (live operator view)

`console` launches a live `rich` dashboard. It ensures the server is up (starting it in the
background if needed), then attaches a monitor showing server/neo4j/ready state, queue +
enrichment + scheduler metrics (`/api/ready` + `/api/stats`), and a live tail of
`logs/server.log`. Quitting the dashboard (`q` / Ctrl+C) leaves the server running.

```powershell
.\scripts\start-server.ps1 console
# or, against an already-running server, directly:
.\scripts\start-server.ps1 status   # confirm it's up, then:
$env:PYTHONPATH='...\src'; $env:ENV_FILE='...\.env'
& '...\.venv\Scripts\python.exe' -m menhir.cli console
```

Keys in the dashboard: `p` toggles **privacy redaction** (masks memory content in the log
tail), `q` quits. The dashboard sends the least-privileged configured API key
(read-only > operator > agent), so it works when the server runs in STATIC/authed mode.

**Privacy:** set `MENHIR_PRIVACY_REDACT=true` to start the dashboard (and the explorer web
UI) with memory contents hidden by default; toggle live in the dashboard with `p`.
The explorer UI is masked field-exactly; the dashboard's **log tail is best-effort only** — it can
mask only quoted spans in a rendered log line, so unquoted or short content survives (CF-96). See
`docs/security-posture.md` before relying on it while screen-sharing.

Raw fallback (foreground server, bypasses the remote-Neo4j readiness probe and the watchdog —
direct process output only):

```powershell
$env:PYTHONPATH='C:\Users\you\IdeaProjects\projects\archolith\menhir\src'
$env:ENV_FILE='C:\Users\you\IdeaProjects\projects\archolith\menhir\.env'
& 'C:\Users\you\IdeaProjects\projects\archolith\menhir\.venv\Scripts\python.exe' -m menhir.cli serve
```

## Health and readiness

### Canonical readiness check

```powershell
Invoke-WebRequest http://127.0.0.1:8090/api/ready
```

Expected response shape:

- `status`
- `startup_mode`
- `capabilities`

Important capability flags:

- `neo4j_ready`
- `embedder_ready`
- `llm_ready`
- `scheduler_ready`
- `reads_ready`
- `queue_writes_ready`
- `enrichment_ready`

### Stats endpoint

Use `/api/stats` for the broader runtime view:

```powershell
Invoke-WebRequest http://127.0.0.1:8090/api/stats
```

Use it to check:

- queue depth and processing counts
- startup mode
- capability state
- scheduler snapshot
- enrichment enabled/disabled state

### Recall Lab history

Recall Lab saves every completed query to `<workspace_root>/.agent/mcp_telemetry.db`. Recent summaries
are available at `GET /explorer/api/recall-lab/history`; the complete stored request and result are
available at `GET /explorer/api/recall-lab/history/{run_id}`.

## Startup modes

OAuth startup validation is fail-closed:

- embedded-AS mode requires `MENHIR_PUBLIC_BASE_URL`
- non-loopback public URLs must use HTTPS
- OAuth TTL/rate/window settings must be positive
- `MENHIR_TRUSTED_PROXY=1` requires `MENHIR_TRUSTED_PROXY_PEERS`; forwarded addresses are trusted only when the direct socket peer is in that list

### `full`

- Neo4j + embedder + LLM + scheduler available
- recall and enrichment both enabled

### `degraded_reads_only`

- Neo4j + embedder available
- recall works
- enrichment disabled

### `degraded_queue_only`

- Neo4j available
- writes persist
- Graphiti-backed recall/enrichment unavailable

## Queue and recovery operations

### List queue state

Use MCP:

```text
Tool: list_enrichment_queue
```

Or use the processing-queue resource:

```text
Resource: memory://system/processing-queue
```

### Inspect one episode

```text
Tool: get_episode_trace
Args: episode_uuid="<uuid>"
```

### Repair stale enrichment rows

```text
Tool: repair_stale_enrichment
Args: dry_run=true, limit=100
```

Apply after review:

```text
Tool: repair_stale_enrichment
Args: dry_run=false, limit=100
```

### Force release a stuck lease

```text
Tool: force_release_enrichment_lease
Args: episode_uuid="<uuid>"
```

### Re-enrich a failed episode

```text
Tool: force_reenrich
Args: episode_uuid="<uuid>"
```

### Recover stale SESSION nodes / orphans

Startup now runs orphan recovery in the background and does not block `/api/ready`.

Manual operator run:

```text
Tool: recover_orphans
Args: max_age_hours=4.0, dry_run=false
```

## Scheduler operations

### Check scheduler health

Use `/api/ready` or `/api/stats`, or inspect MCP/system resources.

### Force scheduler lease takeover

```text
Tool: force_scheduler_takeover
```

Use this when a stale or incorrect owner is blocking scheduler work.

## Common operational checks

### Verify backend is up

```powershell
.\scripts\start-server.ps1 status
Invoke-WebRequest http://127.0.0.1:8090/api/ready
```

### Recover from remote Neo4j-unreachable startup failure

Neo4j is **not** local Docker. It runs as `menhir-neo4j.service` (systemd) on the remote host
named by `NEO4J_URI` in `.env`. The desktop no longer runs Docker Desktop or a local Neo4j
container for menhir at all -- the root `docker-compose.yml` describing a local `yawn-neo4j`
container is vestigial and does not reflect the current deployment.

When `server.err.log` shows `Neo4j connectivity failed: ...` (a socket timeout or connection
refused against the `NEO4J_URI` host:port) and `/api/ready` reports `neo4j_ready: false` with
`"Neo4j connectivity check failed."` in `failures`, the backend starts anyway (`start-server.ps1`
warns and continues rather than blocking) but stays degraded until the remote graph is reachable.

Confirm the remote host is reachable at the network level:

```powershell
Test-NetConnection -ComputerName <neo4j-host-from-.env> -Port 7687
```

If both ping and the port test fail, the host itself is off the network (powered down, network
cable/Wi-Fi dropped, or still booting) -- this is not a Neo4j-specific symptom at that point;
check the host directly before touching Neo4j.

If the host is reachable but the port isn't, the `menhir-neo4j` service isn't running there.
Start it on the remote host (requires an interactive password if sudo isn't passwordless for
this command -- it commonly isn't):

```bash
ssh <neo4j-host>
sudo systemctl start menhir-neo4j
systemctl status menhir-neo4j --no-pager
```

Then retry the backend launcher (or just wait -- the watchdog retries the connection on its own):

```powershell
.\scripts\start-server.ps1 restart
Invoke-WebRequest http://127.0.0.1:8090/api/ready
```

### Verify MCP can connect

- stdio MCP must have `MENHIR_BACKEND_URL` set
- backend must already be reachable at `/api/ready`

See [backend-first-mcp.md](backend-first-mcp.md) for exact setup.

### Verify logs are healthy

```powershell
Get-Content .\logs\server.log -Tail 40
Get-Content .\logs\server.err.log -Tail 40
Get-Content .\logs\launcher.log -Tail 40
```

## Merge / unmerge / delete recovery

Every destructive graph mutation is journaled in the telemetry sidecar as a recoverable saga (see
`data_models.md` → "Merge / delete lifecycle"). All operator commands below are DRY-RUN by default and
require `--execute` to mutate. Every refusal is explicit — the tools never silently half-restore.

### Inventory what can actually be recovered

```
python scripts/unmerge.py --inventory
```

Read-only. Classifies every recorded absorption into `EXACT` / `LEGACY_SIDECAR` /
`GRAPH_SNAPSHOT_ONLY` / `LINEAGE_ONLY` / `MALFORMED`. Run this FIRST during a recovery incident so
expectations match reality — most historical merges are not exactly reversible, and lineage-only
absorptions cannot be reversed by any tool. If it reports `GRAPH_SNAPSHOT_ONLY` rows, preserve them
before they are lost with their survivor:

```
python scripts/backfill_merge_audit.py --dry-run  # preview
python scripts/backfill_merge_audit.py            # idempotent; copies graph snapshots to the sidecar
```

### Reverse a journaled merge exactly

```
python scripts/unmerge.py --list                  # merges reversible exactly
python scripts/unmerge.py --op <MERGE_OP_ID>      # dry run: shows exactly what would be restored
python scripts/unmerge.py --op <MERGE_OP_ID> --execute
```

Restores the absorbed node byte-for-byte (labels, typed properties, every relationship instance) and
reverses the survivor's merge delta, in one atomic transaction. It REFUSES (restores nothing) if the
graph is no longer in the merge's after-state, if the survivor was edited after the merge (invariant
9 — newer state is preserved, not clobbered), or if any snapshot peer is missing.

### Degraded recovery of a pre-journal (legacy) merge

```
python scripts/unmerge.py --legacy-absorbed <UUID> [<UUID> ...]            # dry run + degradation list
python scripts/unmerge.py --legacy-absorbed <UUID> [<UUID> ...] --execute
```

The listed uuids ARE the manifest — nothing outside the list is touched. This is a PARTIAL recovery
from a lossy snapshot: it always reports `exact: False` and the classes of state it cannot restore
(labels, non-Entity peers, relationship properties, typed temporals, the survivor's entire pre-merge
state), and it never fabricates the survivor's prior values.

### After a crash: reconcile the journal

A crash can leave a saga row `PREPARED`. Reconciliation observes the graph and records the truth:

- Merge/unmerge: replays a completed mutation to `COMMITTED`; genuine drift → `NEEDS_REVIEW`.
- Delete: if the targets are gone the delete completed (→ `COMMITTED`); if any survive → `NEEDS_REVIEW`.
  A delete is NEVER re-run automatically — blindly re-deleting could destroy nodes the crash spared.

`NEEDS_REVIEW` is an operator-only state; no background job clears it. Inspect the row's
`before_snapshot_json`, `expected_after_sha256`, and `last_error`, then adjudicate.

### Physical deletes

Explicit deletes and the SESSION TTL sweep both snapshot every target before destruction and audit
only the nodes ACTUALLY deleted (a node that changed scope in the race window is reported `skipped`,
never claimed deleted). Evidence left unreferenced is REPORTED, never cascade-deleted — isolation is
not authorization.

## Important current limitations

- `POST /api/memory?wait=true` still waits against the in-process backend runtime after queueing through the backend seam
- stdio MCP is now a client surface only; if the backend is down, stdio MCP should fail instead of booting its own runtime
- orphan recovery no longer times out during background startup, so it can create real background load even though startup stays fast
