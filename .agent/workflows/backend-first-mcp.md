# Backend-First MCP

How `menhir` MCP connects after the backend-first runtime rewrite.

## Core rule

Stdio MCP is no longer a second runtime owner.

It now behaves as a client and requires a running backend server:

- backend owner: `menhir serve`
- stdio MCP: client-only
- remote MCP: HTTP-mounted, tools only

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
