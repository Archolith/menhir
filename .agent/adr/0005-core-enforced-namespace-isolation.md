# ADR 0005 — Core-Enforced Namespace Isolation

- **Status:** ACCEPTED retrospectively (2026-09-21). This records the namespace boundary enforced
  by the current backend.
- **Date:** 2026-09-21
- **Deciders:** ctharvey
- **Related:** `.agent/architecture.md`, `.agent/data_models.md`,
  `src/menhir/core/tenancy.py`, `docs/security-posture.md`

## Context

Menhir added namespace checks incrementally at MCP resources, named REST routes, MCP tools, and the
internal backend dispatch. Each local fix was valid, but another transport or UUID-addressed path
could omit the same check. The repeated failure mode was drawing the boundary at entry points
instead of below all of them.

Menhir remains a single-operator service. Namespaces isolate configured clients and datasets inside
one trusted deployment; they are not a claim that one process is a hostile multi-tenant security
boundary.

## Decision drivers

- Switching REST/MCP surfaces must not change namespace authority.
- A caller must not select another silo by changing a request argument or client label.
- UUID-addressed operations need ownership checks even when no namespace argument exists.
- A refused mutation must not be silently retargeted at the caller's own data.
- Erasure must still remove sidecar content when the graph object is already absent.

## Decision

Namespace isolation is resolved and enforced **below transport-specific code**, at the core/backend
operation boundary.

1. **Server-side configuration pins verified client identity to a namespace.** The pin comes from
   request context and `MENHIR_CLIENT_NAMESPACES`; a caller-supplied `client_name` or namespace does
   not grant authority.
2. **`group_id` is the load-bearing engine partition.** Core ownership checks and stamped namespace
   properties provide defense in depth and consistent refusals above that storage boundary.
3. **Filters force; mutation targets refuse.** For a read filter, a configured pin silently narrows
   the query to the caller's silo. For an operation whose namespace is the object being mutated, an
   explicit conflicting target raises `PermissionError`; omission resolves to the pin.
4. **UUID ownership uses two lookups.** First look in the caller's namespace. If absent there, look
   globally only to decide whether the object demonstrably belongs to another silo. An object absent
   everywhere is not treated as foreign.
5. **All transports converge on the same backend checks.** REST, internal backend dispatch, MCP
   tools, and MCP resources may adapt errors differently, but they do not define independent
   ownership policy.
6. **Unpinned deployments preserve unscoped behavior.** Namespace pinning is opt-in and does not
   retroactively claim full multi-tenant isolation.

## Why filters force but targets refuse

Narrowing a read from “namespace B” to the caller's namespace A returns an authorized answer to a
smaller question. Rewriting `delete namespace=B` to `delete namespace=A` destroys the caller's own
data while reporting success against the wrong target. Mutation intent therefore cannot be fixed by
silent substitution.

## Considered alternatives

### Enforce the pin independently in every route and tool

Rejected. It had already produced a sequence of correct but incomplete fixes, and every new surface
became another opportunity to omit the guard.

### Trust the namespace argument supplied by an authenticated caller

Rejected. Authentication identifies the caller; it does not make arbitrary target selection safe.

### Silently force both reads and writes to the pin

Rejected for target mutations because it changes the object of the request and can cause
self-inflicted deletion.

### Treat “not found in my namespace” as proof of foreign ownership

Rejected. The object may not exist anywhere, including the valid post-merge state where erasure
must still purge sidecar content.

### Rely only on Neo4j `group_id`

Rejected as the only guard. Service-level checks provide consistent UUID ownership and mutation
semantics before storage calls, while `group_id` remains the final partition.

## Consequences

- New backend operations must classify namespace inputs as filters or mutation targets.
- UUID-addressed operations must enforce ownership at load/use time, not only during list queries.
- Error mapping must preserve `PermissionError` through internal HTTP so client mode matches the
  in-process provider.
- Core checks may require an extra lookup and still have a narrow time-of-check/time-of-use window;
  they are defense in depth, not a replacement for engine partitioning.
- Hostile tenants or operators from different trust domains require separate Menhir instances.

## Evidence in the repository

- `src/menhir/core/tenancy.py`
- `src/menhir/core/backend_runtime_data_ops.py`
- `src/menhir/core/backend_runtime_admin_ops.py`
- `src/menhir/mcp/contracts.py`
- `tests/test_client_namespace_pin.py`
- `tests/test_high_wave2_tenancy.py`
- `tests/test_object_ownership_at_load.py`
- `tests/test_two_tenant_e2e.py`

## Non-goals

This ADR does not claim adversarial multi-tenancy, remove the need for per-operation authorization,
or turn namespace strings into authenticated identity.
