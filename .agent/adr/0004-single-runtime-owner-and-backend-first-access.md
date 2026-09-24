# ADR 0004 — Single Runtime Owner and Backend-First Access

- **Status:** ACCEPTED retrospectively (2026-09-21). This records the current runtime and transport
  boundary.
- **Date:** 2026-09-21
- **Deciders:** ctharvey
- **Related:** `.agent/architecture.md`, `.agent/workflows/backend-first-mcp.md`,
  `.agent/workflows/operations_runbook.md`, `src/menhir/core/backend_protocol.py`

## Context

Menhir is exposed through REST, remote MCP, and local stdio MCP. When each surface can bootstrap
services or reach directly into runtime internals, the process model becomes part of API behavior:
each bot can start a scheduler, recover the same queue, hold independent service state, and stamp
writes with process ownership rather than caller provenance.

The backend-first rewrite established one persistent owner and made other surfaces clients of a
shared protocol. The rule is implemented and documented operationally, but was not captured as an
ADR.

## Decision drivers

- Queue recovery and scheduled maintenance need one lifecycle owner.
- REST and MCP must apply the same domain operations and refusal semantics.
- Stdio clients need caller identity without inheriting a server process session.
- Remote transports require serializable operations and cannot accept in-process callbacks or
  service objects.
- A missing backend must fail visibly rather than silently creating a second runtime.

## Decision

Menhir has **one runtime owner** and **backend-first access** from every client surface.

1. **`menhir serve` is the canonical runtime owner.** It owns Neo4j/Graphiti service assembly,
   queue recovery, scheduler lifecycle, authentication, REST, and the HTTP-mounted remote MCP
   surface.
2. **Stdio MCP is client-only.** It requires `MENHIR_BACKEND_URL`, checks backend readiness during
   lifespan startup, and fails fast when the backend is unavailable. It does not bootstrap or shut
   down the memory runtime.
3. **MCP tools, MCP resources, and public REST handlers use the `MemoryBackend` contract.** The
   in-process `RuntimeProvider` and HTTP `BackendClient` implement the same async, serializable
   operation surface.
4. **The internal backend transport is not a second public API.**
   `/api/internal/backend/{operation}` exists to carry the protocol between trusted local
   components and is omitted from the public OpenAPI contract.
5. **Caller provenance is separate from process ownership.** Authenticated requests and stdio
   clients bind caller sessions explicitly; the runtime process session is reserved for maintenance
   ownership.
6. **Transport contracts remain intentionally asymmetric where required.** Stdio MCP exposes tools
   and resources; remote MCP is tool-only; REST owns the canonical health, readiness, and stats
   endpoints.
7. **Core owns the seam.** Runtime/backend code does not depend on MCP or API framework modules;
   transport packages adapt to the core contract.

## Considered alternatives

### Let every stdio MCP process own a runtime

Rejected. It multiplies schedulers, queue recovery owners, connection pools, and process-scoped
state in multi-client environments.

### Let tools and routes call `BuildArtifacts` directly

Rejected. It creates in-process-only behavior and lets REST, remote MCP, and stdio MCP drift.

### Maintain separate REST and MCP service contracts

Rejected. The same domain operation would acquire different authorization, tenancy, error, and
provenance behavior depending on the door used.

### Fall back to local bootstrap when the backend is unavailable

Rejected. An availability problem would silently become a split-brain runtime.

## Consequences

- A local MCP client depends on a running, ready backend.
- Backend operations must use transport-safe arguments and return values.
- New MCP/REST capabilities extend the shared backend protocol instead of opening a parallel path.
- Process lifecycle and caller provenance can be reasoned about independently.
- Some deliberately local behavior may remain outside the protocol, but each exception must be
  narrow, documented, and incapable of becoming a second runtime owner.

## Evidence in the repository

- `src/menhir/core/runtime.py`
- `src/menhir/core/backend_protocol.py`
- `src/menhir/core/backend_runtime.py`
- `src/menhir/core/backend_client.py`
- `src/menhir/mcp/service_access.py`
- `src/menhir/mcp/lifecycle.py`
- `src/menhir/api/server.py`
- `tests/test_backend_roundtrip.py`

## Non-goals

This ADR does not make the internal backend endpoint a public compatibility promise, require remote
MCP to expose resources, or authorize a second scheduler for redundancy.
