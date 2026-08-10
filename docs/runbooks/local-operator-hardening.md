# Local Operator Hardening (MVP)

**Purpose:** the single checklist for running Menhir safely as a **local, single-user** service —
the MVP deployment posture. It captures the local-MVP blockers from roadmap milestone **M4**
(`docs/roadmap/menhir-mvp-roadmap.md`) and folds in the two standing findings that gate a
non-local move: Neo4j transport encryption (Finding A) and the unauthenticated explorer (Finding B).

The durable auth/authz architecture lives in [`docs/security-posture.md`](../security-posture.md);
this runbook is the operator-facing "how to run it locally without exposing anything" companion.

**Scope:** local loopback single-user use. Anything that binds beyond `127.0.0.1` (Neo4j,
explorer, or the API itself) leaves MVP scope and must satisfy the "beyond localhost" rows below
first.

---

## 1. API bind — no-auth is loopback-only (enforced)

No-key/open-auth mode is only safe on a loopback bind, and this is **enforced in code**, not just
documented: `validate_no_auth_bind_safety` (`config/settings.py`) refuses to start a no-key server
on a non-loopback host unless OAuth or client tokens are enabled, or
`MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH=1` is explicitly set (unsafe; lab only). Menhir binds
`127.0.0.1` by default (`MENHIR_API_HOST`).

| Situation | Required |
|---|---|
| Local single-user (MVP) | No keys needed. Keep `MENHIR_API_HOST=127.0.0.1`. The guard permits no-auth here. |
| Bind beyond localhost | Enable auth (OAuth `MENHIR_OAUTH_ENABLED`, client tokens `MENHIR_CLIENT_TOKENS_ENABLED`, or static `MENHIR_OPERATOR_KEY`/`MENHIR_AGENT_KEY`/`MENHIR_READONLY_KEY`). The guard refuses no-auth remote binds. |

`menhir diagnostics` mirrors the guard as an offline preflight (bind host, auth mode, no-auth remote
guard, admin-key status) and never prints secret values. See security-posture §7.

## 2. Neo4j transport — plaintext bolt is loopback-only (Finding A)

The Neo4j driver connects with `GraphDatabase.driver(uri, auth=...)` and **no** `encrypted=` / TLS
trust configuration (`infrastructure/neo4j.py`). The default URI is `bolt://localhost:7687`. On a
loopback connection this is fine — the traffic never leaves the host. But the connection is
**plaintext**, so a non-loopback `NEO4J_URI` would send credentials and memory content in the clear.

| Situation | Required |
|---|---|
| Local single-user (MVP) | Keep `NEO4J_URI=bolt://localhost:7687` (loopback). Plaintext is acceptable; nothing leaves the host. |
| Neo4j on another host / network | Use an encrypted scheme (`bolt+s://` / `neo4j+s://`) with a valid server cert, **or** tunnel over SSH/WireGuard/mTLS. Do **not** point a bare `bolt://` at a remote Neo4j. |

MVP status: **local-only = fine; beyond localhost = required before exposure.** No code change is
made for MVP because the local posture is safe; the encrypted-scheme requirement is the documented
gate for any future non-local deployment.

## 3. Explorer — unauthenticated localhost tooling only (Finding B)

The graph explorer is mounted into the main app at `/explorer` on the API port (default
`127.0.0.1:8090`) and shares the runtime Neo4j pool. `BearerAuthMiddleware` gates `/explorer` and
`/explorer/candidates/*` exactly like `/api/*` (only `/explorer/static/*` is exempt). Set
`MENHIR_EXPLORER_ENABLED=false` to omit the surface entirely. The standalone `menhir-explorer`
process and port `8787` were removed.

| Situation | Required |
|---|---|
| Local single-user (MVP) | Keep the API bound to `127.0.0.1`. On a loopback bind (`AuthMode.NONE`) the explorer is open, unauthenticated localhost inspection — as before. |
| Any non-loopback need | A non-loopback bind already requires the API's bearer/OAuth/client-token credential on `/explorer` too, so the graph is not exposed unauthenticated. A reverse proxy / SSH tunnel remains good defense-in-depth; set `MENHIR_EXPLORER_ENABLED=false` if the explorer is not needed on that host. |

MVP status: **explorer inherits the API's auth posture.** Loopback = open inspection; non-loopback =
credential-gated. No separate explorer port or process to secure.

## 4. Telemetry retention / inspection

The MCP telemetry sidecar (SQLite) lives at `workspace_root()/.agent/mcp_telemetry.db` (override:
`MENHIR_MCP_TELEMETRY_DB`). Growth is bounded:

- **Revision pruning:** `prune_old_revisions(retention_days=14)` deletes `memory_revisions` older
  than the retention window; configurable via `MENHIR_REVISION_RETENTION_DAYS` (default 14).
- **Read clamps:** telemetry read paths clamp caller-supplied limits (`min(limit, 100)` /
  `min(20)` / `min(limit, 200)` in `infrastructure/telemetry/store.py`) so a large `limit` cannot
  amplify a read.

Operator practice: the DB is disposable local state. If a launch benchmark or a Hook Center smoke
run inflates it, delete the file (or point `MENHIR_MCP_TELEMETRY_DB` at a throwaway path for the
run) — no migration needed; it is recreated on next start. Non-revision tables are append-only
(low operational debt; a full retention sweep across all telemetry tables is post-MVP).

---

## 5. Verification (run against a local backend)

M4 requires verifying the operator surfaces against a real local backend. The throwaway full-scope
launcher (`scripts/dev/test_server.py`, `backend="neo4j"`) provides an isolated local backend for
this without touching the real graph.

Verified 2026-07-10 against a launcher-spun full-scope (Neo4j-backed) no-auth backend:

| Surface | Result |
|---|---|
| `GET /api/ready` | HTTP 200; reports `status` + `startup_mode` + honest `failures` list. On the throwaway (no local LLM) it correctly reports `degraded` / `degraded_reads_only` rather than failing — validates the degraded-startup path. |
| `GET /api/stats` | HTTP 200; returns `startup_mode`, `queue_depth`, `enrichment_enabled`, and the services payload. |
| MCP stdio backend-client mode | With `MENHIR_BACKEND_URL` set, `backend_client_mode_enabled()` = true and `probe_backend_health()` returns `ok=true` (probes `{backend}/api/ready`). Stdio MCP requires a running `menhir serve` and refuses to start without `MENHIR_BACKEND_URL` (fail-closed). |
| Active file-event hook -> local backend | Producer dry-run normalizes with `content_uploaded=false`; a real `POST /api/tool-events` against the local backend returns `accepted=true`. (`marked_dirty` is false only when the target file is not yet in the structure graph — expected on a fresh graph.) |

`GET /api/health` and `GET /api/ready` are the only auth-exempt routes; everything else is subject
to the resolved auth mode (security-posture §3). In local no-auth loopback mode there is no auth to
apply, which is the whole point of keeping the bind on `127.0.0.1`.

## 6. Not hardened for MVP (post-MVP, out of local scope)

- Live OAuth rollout / IdP selection / token issuance operations (resource-server code is in and
  Auth0-verified; interactive login flow is post-MVP — security-posture §11).
- Multi-user namespace ACLs.
- TLS/mTLS termination and full cloud/proxied deployment posture (reverse-proxy guards exist and are
  unit-tested; end-to-end proxied exercise is pre-exposure work — security-posture §8/§11).
- Enforced (vs. documented) explorer/Neo4j bind guards — the MVP control is loopback + this runbook;
  a code guard mirroring `validate_no_auth_bind_safety` is a reasonable post-MVP hardening.
