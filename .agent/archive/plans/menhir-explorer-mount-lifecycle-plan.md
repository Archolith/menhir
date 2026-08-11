# menhir — Mount Explorer into the Main App (lifecycle unification)

> **Archived 2026-08-11.** Explorer mounting is integrated with the main application lifecycle and
> the former standalone-server ownership is closed.

Status: DRAFT — awaiting approval to implement
Owner: ctharvey
Author: Claude Code (Opus 4.8)
Date: 2026-07-12
Approach: **A — mount the explorer into the main app** (chosen over B: co-supervised
separate process, and C: `serve --with-explorer` two-server flag).

## 1. Problem / motivation

The menhir backend has a full supervised lifecycle: a Scheduled Task (logon + 1-min
poll) → `run-hidden.vbs` → `start-server.ps1 start` → Docker/Neo4j readiness →
`serve-watch` watchdog (singleton guard, crash backoff, pid tracking) → `serve`
(port/embedding preflights) → uvicorn REST+MCP on `127.0.0.1:8090`.

The **explorer is orphaned from all of it**. It is a second FastAPI process
(`menhir-explorer` → `explorer.app:run()`) on `127.0.0.1:8787` with its **own**
`Neo4jRepository` pool, its own lifespan, and:

- no watchdog / no crash-restart,
- no pid tracking,
- no Docker/Neo4j readiness gate,
- no scheduled-task / reboot auto-start (backend returns after reboot; explorer does not,
  until someone manually `Start-Process`es it per the runbook),
- a duplicate Neo4j connection pool.

Goal: collapse the explorer into the supervised backend lifecycle as a single process on
a single port, sharing one Neo4j pool, while **preserving the explorer's loopback-only,
low-friction access posture and closing a pre-existing unauthenticated-surface risk.**

## 2. Key constraints discovered (load-bearing)

1. **Auth only covers `/api/*` and `/mcp*`.** `src/menhir/api/auth.py:245-246`:
   `if not is_api and not is_mcp: await self.app(scope, receive, send); return`.
   Every other path is served with **no auth**. Today that is safe only because the
   explorer is a separate loopback-bound process. Mounting `/explorer/*` into the main
   app means: if the main app is ever bound non-loopback, the explorer's graph **reads**
   and its candidate **approve/reject writes** (`POST /explorer/candidates/{uuid}/*`)
   become an unauthenticated remote surface. **This must be closed as part of the mount.**
2. **Explorer URLs are absolute** — templates + `static/explorer.js` reference
   `/explorer/partials/...`, `/explorer/api/...`, `/explorer/static/...` in 36 places.
   Any mount mechanic that changes the served path prefix would require rewriting all of
   them. → Register the routes **onto the main app with their existing `/explorer/*`
   paths** (router-include), not as a sub-app mounted under a different prefix.
3. **Mounted sub-app lifespans do not run** under the parent by default (Starlette).
   The explorer lifespan sets up `app.state.candidate_service` (approve/reject path).
   If we sub-app-mounted, that setup would silently not run. Router-include avoids this
   because routes share the parent app; the parent lifespan must do the setup.
4. `explorer.create_app(settings=, repo=)` **already** supports an injected repo and sets
   `owned_repo=False` so it will not close a shared repo. This is the intended reuse hook.
5. The backend runtime already owns a Neo4j repo via `RuntimeContext` (`start_runtime`).
   The explorer must reuse that repo, not open a second pool.
6. `startup_scope` can be `auth-only`/`http-only`/`no-backend` (no Neo4j, no runtime). The
   explorer needs Neo4j; under a backendless scope it must degrade cleanly (route returns
   503 or the mount is skipped), not crash the app or open its own pool.

## 3. Chosen design

Register the explorer's routes and static files **directly on the combined app** under
their existing `/explorer/*` paths, backed by the **runtime's shared repo**, gated by a
setting, and covered by auth on non-loopback binds.

### 3.1 Refactor explorer route wiring (no URL/template changes)

- Convert the route bodies currently defined inside `explorer/app.create_app()` into a
  module-level `APIRouter` (paths unchanged: `/explorer`, `/explorer/features`,
  `/explorer/partials/*`, `/explorer/api/*`, `/explorer/candidates/*`). Handlers keep
  reading `request.app.state.repo` and `request.app.state.candidate_service`.
- Keep `explorer/app.create_app()` and `run()` working **standalone** (the
  `menhir-explorer` console script and port 8787 remain a supported fallback). The
  standalone `create_app()` builds its own repo + candidate_service in its lifespan (as
  today) and includes the same router. No behavior change for the standalone path.
- Provide `explorer.integration.mount_explorer(app: FastAPI)` (new small module) that:
  - includes the explorer `APIRouter` on the passed app, and
  - mounts `StaticFiles(directory=STATIC_DIR)` at `/explorer/static`.

### 3.2 Wire into the combined app (`api/server.py`)

- New setting `MENHIR_EXPLORER_ENABLED` (default **true**). When false, skip the mount
  entirely (pure backend, as before).
- In `create_app()`, after routers are registered and **before** the auth middleware wrap,
  if `explorer_enabled` and `startup_scope` is a full (backend) scope: call
  `mount_explorer(app)`.
- In `_lifespan`, after `start_runtime(settings)` yields `ctx`, set:
  - `app.state.repo = ctx.<neo4j repo>` (reuse the runtime's repo; confirm the exact
    attribute during implementation — do **not** open a new `Neo4jRepository`),
  - `app.state.candidate_service = CandidateService(graph_adapter=MemoryGraphAdapter(
    neo4j=app.state.repo), lifecycle_service=LifecycleService(graph_adapter=...,
    graphiti_client=<runtime graphiti client if available, else UnavailableGraphitiClient>))`.
    Prefer the **live** runtime Graphiti client so mounted candidate approval gets real
    contradiction checks (an improvement over the standalone explorer's
    `UnavailableGraphitiClient` no-op); fall back to `UnavailableGraphitiClient` only if
    the runtime has none.
- Under a backendless `startup_scope`, do not set `app.state.repo`; explorer routes should
  return 503 (add a small guard) rather than 500. Simplest: skip the mount when scope is
  backendless (matches "explorer needs Neo4j"). Decide in implementation; default to
  **skip-mount under backendless scope**.

### 3.3 Close the auth hole (required)

In `api/auth.py`, extend the enforced-path predicate so `/explorer` is protected exactly
like `/api`:

- Add `is_explorer = path == "/explorer" or path.startswith("/explorer/")`.
- Change the early pass-through to `if not is_api and not is_mcp and not is_explorer:`.
- Keep `/explorer/static/*` **exempt** (static assets, no data) via `_EXEMPT_PATHS`-style
  prefix check, so the UI still loads its CSS/JS.
- Net effect:
  - **Loopback bind + AuthMode.NONE** (the normal dev/local case): explorer stays
    friction-free — open the browser, no token — because loopback resolves to
    `AuthMode.NONE` and that branch already passes through with cooperative identity.
  - **Non-loopback bind**: explorer now requires the same bearer/OAuth/client-token as
    `/api/*`. No more unauthenticated remote graph reads or candidate writes.
- Add a regression test asserting `/explorer` and `POST /explorer/candidates/{uuid}/approve`
  are rejected without a token when the resolved auth mode is STATIC/OAUTH/CLIENT_TOKEN,
  and allowed under AuthMode.NONE.

### 3.4 Docs / scripts cleanup (lifecycle hygiene)

- `scripts/start-server.sh` is **dead**: it execs `cth_mcp_memory.cli serve` (module
  renamed to `menhir.cli`) and hardcodes a Windows venv path. Either fix it to
  `menhir.cli serve-watch` or delete it. Recommend **delete** (the `.ps1` path is the
  supported Windows launcher; the `.sh` has been broken since the rename). Confirm with
  owner before deleting.
- Update `.agent/architecture.md` §6 (Explorer): note it is now mounted at `/explorer` on
  the main port by default, shares the runtime Neo4j pool, and is auth-gated on non-loopback
  binds; standalone `menhir-explorer` on 8787 remains a fallback.
- Update `README.md` explorer access URL: `http://127.0.0.1:8090/explorer` (main port) as
  primary; keep 8787 documented as the standalone fallback.
- Update the operator runbook memory: the `Start-Process ... menhir-explorer` step is no
  longer needed for normal use — the explorer comes up with the backend and survives
  reboots via the existing scheduled task.
- CHANGELOG entry.

## 4. Out of scope

- Removing/deprecating the standalone `menhir-explorer` script (kept as fallback).
- Any change to the watchdog / scheduled-task mechanics (explorer inherits them for free
  once mounted — that is the whole point).
- Explorer feature work (graph queries, candidate logic) beyond the repo/service wiring.
- Adding auth *to the standalone 8787 explorer* (its loopback-only posture is unchanged;
  the audit finding SEC-H01 remains a separate, pre-existing item for that path).

## 5. Files touched (anticipated)

- `src/menhir/explorer/app.py` — extract routes into module-level `APIRouter`; keep
  `create_app()`/`run()` standalone using that router.
- `src/menhir/explorer/integration.py` — **new**: `mount_explorer(app)`.
- `src/menhir/api/server.py` — conditional `mount_explorer`, lifespan repo/service wiring.
- `src/menhir/api/auth.py` — add `/explorer` to enforced prefixes; exempt `/explorer/static`.
- `src/menhir/config/settings.py` — `MENHIR_EXPLORER_ENABLED` setting.
- `tests/test_explorer_app.py` / `tests/test_explorer_candidates.py` — keep standalone
  coverage; add mounted-app + auth-gating tests (new test module if cleaner).
- `scripts/start-server.sh` — delete (or fix) — pending owner confirm.
- `.agent/architecture.md`, `README.md`, `CHANGELOG.md` — docs.

## 6. Verification plan

1. `.\scripts\menhir.ps1 unit` (or targeted `pytest tests/test_explorer_*.py` +
   `tests/test_auth*.py`) — PASS.
2. `.\scripts\start-server.ps1 restart`; then:
   - `GET http://127.0.0.1:8090/api/ready` → 200, `startup_mode=full`.
   - `GET http://127.0.0.1:8090/explorer` → 200 (loopback, no token).
   - `GET http://127.0.0.1:8090/explorer/static/explorer.js` → 200.
   - `GET http://127.0.0.1:8090/explorer/partials/queue` → 200.
   - Confirm only **one** Neo4j pool is opened (no second connection burst; check
     `logs/server.log` for a single runtime bind, no explorer-owned repo line).
3. Auth-gating unit test proves `/explorer` is 401 without token under STATIC/OAUTH.
4. Reboot recovery (manual/asserted by design): with the scheduled task installed, the
   explorer is reachable on `:8090/explorer` after the backend auto-starts — no manual
   `Start-Process`.
5. Standalone fallback unchanged: `menhir-explorer` still serves on `:8787`.

## 7. Risks / mitigations

- **R1: repo attribute mismatch.** The exact `RuntimeContext` attribute for the Neo4j repo
  must be confirmed before wiring (do not guess). Mitigation: read `core/runtime.py` first;
  if the runtime does not expose a reusable repo, fall back to constructing one repo in the
  main lifespan and sharing it with both runtime and explorer, or accept a dedicated
  explorer repo (still one process, still supervised) as a documented compromise.
- **R2: sub-app lifespan gotcha** — avoided by router-include (not sub-app mount).
- **R3: auth change over-broad** — mitigated by exempting `/explorer/static` and by tests
  covering both loopback-NONE (allowed) and STATIC/OAUTH (blocked).
- **R4: backendless startup scope** — mitigated by skipping the mount under backendless
  scopes (explorer requires Neo4j).
- **R5: `startup_scope` full-scope set name** — confirm the exact membership of the
  "full/backend" scope vs `_backendless_scopes` in `api/server.py` when gating the mount.

## 8. Decisions (resolved by owner 2026-07-12)

- OD-1 — **DELETE** `scripts/start-server.sh` (dead: references old `cth_mcp_memory.cli`
  module, hardcoded Windows venv path). Owner default accepted.
- OD-2 — **REMOVE the standalone explorer** (`menhir-explorer` console script + port
  8787) once mounting lands. There is one explorer surface: `/explorer` on the main port.
  Scope additions from this decision:
  - `pyproject.toml` — drop the `menhir-explorer = "menhir.explorer.app:run"` script.
  - `explorer/app.py` — remove `run()` (and the standalone-only lifespan repo/candidate
    setup) once its logic is covered by `mount_explorer` + main-app lifespan. Keep
    `create_app()` only if still needed for tests; otherwise fold into the router module.
  - Remove `MENHIR_EXPLORER_HOST` / `MENHIR_EXPLORER_PORT` handling (8787 no longer exists).
  - Docs: delete 8787 references in `README.md`, `.agent/architecture.md`, runbook memory,
    and the "standalone fallback" language throughout this plan (superseded by this
    decision — the explorer is mount-only).
  - Note: this removes the ability to inspect the graph when the backend runtime will not
    start. Accepted: explorer requires Neo4j regardless, and the supported recovery path is
    fixing the backend (embedding-repair banner, etc.), not a parallel inspector process.
- OD-3 — `MENHIR_EXPLORER_ENABLED` default **true**. When false, no `/explorer` surface.

> The plan text in §3.1 / §4 that describes keeping a "standalone fallback" is
> **superseded by OD-2**: implement mount-only. Sections retained for context.

## 9. Follow-up (separate session, after this lands)

- **Lifecycle start/stop/restart ease-of-use review.** Current operator surface is
  `start-server.ps1 {start|stop|restart|status|install-task|uninstall-task}` plus the CLI
  `serve` / `serve-watch`. Evaluate a single friendly entry point (e.g. `menhir up` /
  `menhir down` / `menhir restart` / `menhir status`), clearer status output (pid,
  watchdog pid, port, neo4j state, ready-probe result in one line), and reducing the
  number of ways to start the thing. Track as its own plan; do not fold into this one.
