# Backend-First MCP

How `menhir` MCP connects after the backend-first runtime rewrite.

## Core rule

Stdio MCP is no longer a second runtime owner.

It now behaves as a client and requires a running backend server:

- backend owner: `menhir serve`
- stdio MCP: client-only
- remote MCP: HTTP-mounted, tools only

## Planned generic projection boundary (not implemented)

Backend-first ownership will also govern any future generic projection runtime, but that foundation
is planned and unimplemented. The generic projection host, ordered-journal consumer, temporal
wakeups, runtime manifest digest, typed corruption states, writer census, and cutover receipts have
no current operator commands or readiness fields. The proposed
[master plan](../plans/menhir-foundation-completion-2026-08-30.md),
[Phase 2 runtime plan](../plans/menhir-foundation-phase-2-runtime-orchestration-2026-08-30.md),
[Phase 4 cutover plan](../plans/menhir-foundation-phase-4-developer-surface-and-cutover-2026-08-30.md),
and [ADR 0002](../adr/0002-generic-assertion-currentness-and-journal.md) describe the future target;
ADR 0002 is **PROPOSED**, not accepted. Do not configure stdio MCP, infer a generic scheduler, or
present a public extension surface from those plans. Current scalar- and event-specific
repositories, writers, and behavior remain authoritative.

When implemented, readiness for the projection host must live on the backend and fail closed on
runtime-manifest or adapter-digest drift and on a missing active definition. Backend diagnostics must
report per-definition work, freshness, and corruption, together with durable journal cursor and
census-watermark state. Stdio MCP remains a client and must never own or independently report that
runtime readiness.

Future production activation must follow **Expand → read-only Backfill → Drain** (atomic authority
flip, then post-flip materialization) **→ Verify → Enforce → Contract**, with separate continuous
attested windows of 7 days for Drain, 7 days for Verify, and 14 days for Contract. Old-image rollback
is allowed only before the first durable production mutation; afterward the recovery path is a roll
forward to a certified fence-aware release or a verified reverse generation with an atomic authority
flip.

## Required backend

The backend must be reachable before stdio MCP starts:

```powershell
cd C:\Users\you\IdeaProjects\projects\menhir
.\scripts\start-server.ps1 start
Invoke-WebRequest http://127.0.0.1:8090/api/ready
```

## Required env

Minimum stdio MCP requirement:

- `MENHIR_BACKEND_URL`

Typical local value:

```text
MENHIR_BACKEND_URL=http://127.0.0.1:8090
```

Useful related settings:

- `MENHIR_MCP_CLIENT_USER_ID`
- `MENHIR_CLIENT_ID`
- `MENHIR_CLIENT_NAME`

These define caller provenance for stdio MCP instead of reusing runtime ownership state.

## Connection model

### Stdio MCP

- requires `MENHIR_BACKEND_URL`
- probes `/api/ready` during lifespan startup
- exposes tools and resources locally
- forwards operations through the backend protocol

### Remote MCP

- mounted on the backend HTTP server
- tool-only by design
- shares the same runtime as REST

### REST

- canonical external surface for:
  - `/api/health`
  - `/api/ready`
  - `/api/stats`
  - public memory endpoints

## Failure mode to expect

If the backend is not running, stdio MCP should fail fast with a backend/readiness error rather than silently booting a second runtime.

That is expected behavior now.

## Quick troubleshooting

### MCP fails during initialize

Check in order:

1. backend running:

```powershell
.\scripts\start-server.ps1 status
```

2. readiness:

```powershell
Invoke-WebRequest http://127.0.0.1:8090/api/ready
```

3. env:

- confirm `MENHIR_BACKEND_URL`
- confirm the MCP launcher actually loads `.env` if that is how it is configured

### Requests stamped to the wrong session

Check caller-session config:

- `MENHIR_MCP_CLIENT_USER_ID`
- `MENHIR_CLIENT_ID`
- `MENHIR_CLIENT_NAME`

Authenticated HTTP/MCP requests can also bind request-scoped caller sessions through headers/context.

## Operational expectation

For local development and operator workflows:

1. start backend first
2. confirm `/api/ready`
3. then start/reload MCP clients
