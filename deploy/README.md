# Menhir Docker deployment

For an immutable production release to the live VPS, start with the
[live VPS deployment playbook](LIVE_VPS_PLAYBOOK.md) and use
[PRODUCTION.md](PRODUCTION.md) as the detailed contract. The one-endpoint,
per-client OAuth identity, and ChatGPT/Codex/Claude/OpenCode role invariant is
in [ACCESS_CONTRACT.md](ACCESS_CONTRACT.md).

A containerized Menhir for **test deployments** — in particular, validating the
auth/OAuth surface (including the proxied-deployment guards **CT-001** and
**RL-001**) behind a real reverse proxy, which cannot be exercised locally.

The image builds from a single `git clone` of menhir — no sibling checkout, no
workspace layout. Menhir's two unpublished first-party dependencies
(`archolith-mcp-framework`, `archolith-oauth`) are resolved from public GitHub in the
Dockerfile's builder stage, which is the only stage carrying `git`; the runtime stage
installs the resulting wheels offline.

## Contents

| File | Purpose |
|------|---------|
| `Dockerfile` | Two-stage runtime image (context = repo root). |
| `build.sh` | Build the image (`docker build -f deploy/Dockerfile .`). |
| `docker-compose.test.yml` | Test stack. Phase 1 = auth-only (no Neo4j); Phase 2 = full + throwaway Neo4j. |
| `docker-compose.full.yml` | Full stack: menhir + an isolated throwaway Neo4j. |

## 1. Bring it up (new-user flow — from a menhir clone)

```bash
export MENHIR_OPERATOR_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose -f deploy/docker-compose.test.yml up -d --build
docker compose -f deploy/docker-compose.test.yml logs -f menhir-test
```

That is all a new user needs: clone menhir, set one bootstrap operator credential,
then run `up --build`. No PyPI package, workspace layout, or separate framework
checkout is required. Compose fails closed when `MENHIR_OPERATOR_KEY` is unset.

To build the image explicitly (e.g. to tag/push it):

```bash
deploy/build.sh                      # -> menhir:test
IMAGE=<registry>/menhir:test deploy/build.sh && docker push <registry>/menhir:test
```

> Maintainer note: the first-party dependencies are pinned by tag/commit in
> `pyproject.toml`. To pick up a framework change, bump the pin there and rebuild;
> Docker's layer cache keys on `pyproject.toml`, so the builder stage re-resolves.

Phase 1 defaults: **auth-only scope** (no Neo4j), **client-token tier** enabled,
`MENHIR_TRUSTED_PROXY=1`, state in a `/data` volume, published on
**`127.0.0.1:8099`** (loopback only — the reverse proxy reaches it, the internet
does not hit the app port directly).

Health: `curl -fsS http://127.0.0.1:8099/api/health` → `{"status": ...}`.

### First operator credential (container binds `0.0.0.0`)

The container binds `0.0.0.0`, which is a **network bind**, so the credential-free
loopback bootstrap is disabled by design (a network-bound server can't be
bootstrapped without a credential — a proxy could otherwise spoof a loopback
origin). For a container/proxied deployment you therefore provide a credential:

- **Client-token tier:** set `MENHIR_OPERATOR_KEY=<secret>` and use it to mint the
  first client token, e.g.

  ```bash
  curl -s -X POST http://127.0.0.1:8099/api/admin/clients \
       -H "authorization: Bearer $MENHIR_OPERATOR_KEY" \
       -H 'content-type: application/json' \
       -d '{"client_name":"my-agent","tier":"operator"}'
  # -> {"client_id":..., "token":"<shown once>", ...}
  ```

- **OAuth mode:** set the `MENHIR_OAUTH_*` vars instead; tokens come from the IdP.

(A no-credential `POST /api/admin/clients` correctly returns `401` on the container.)

## 2. Put it behind the reverse proxy (validates CT-001 / RL-001)

The proxied topology is exactly what CT-001/RL-001 harden against. With Caddy,
add a route for a test hostname:

```caddyfile
menhir-test.<your-domain> {
    reverse_proxy 127.0.0.1:8099
}
```

**Two layers of bootstrap protection, depending on how the app binds:**

- **Container default (`0.0.0.0` bind) — primary protection.** A network bind
  disables the credential-free loopback bootstrap outright, so *any*
  `POST /api/admin/clients` without a credential is `401`, proxy or not. This is
  the safest posture and what the compose file ships. Provide the first
  credential per §1.

- **App loopback-bound behind a same-host proxy (`MENHIR_API_HOST=127.0.0.1`) —
  CT-001 forwarding-header guard.** In this shape the app *would* accept a
  loopback bootstrap, so the extra guard matters: Caddy's `reverse_proxy` sets
  `X-Forwarded-For`, so a bootstrap arriving through the proxy carries it and is
  refused, while a direct loopback `curl` on the host (no such header) still
  bootstraps. Verify:

  ```bash
  # through the proxy -> 401 (forwarding-header guard engaged)
  curl -s -o /dev/null -w '%{http_code}\n' -X POST https://menhir-test.<domain>/api/admin/clients \
       -H 'content-type: application/json' -d '{"client_name":"x","tier":"operator"}'
  # on the host, direct to the loopback-bound app, empty store -> 200
  curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8099/api/admin/clients \
       -H 'content-type: application/json' -d '{"client_name":"x","tier":"operator"}'
  ```

- **RL-001** — with `MENHIR_TRUSTED_PROXY=1`, the AS rate limits key on the
  real client from Caddy's `X-Forwarded-For` last hop instead of collapsing
  every caller onto the proxy's address.

> Operational rule (see `docs/runbooks/client-token-tier.md`): on a proxied
> deployment, **pre-mint the first operator token (or set `MENHIR_OPERATOR_KEY`)
> before wiring the proxy**, and never revoke the last active token while the
> proxy is attached — revocation re-opens the bootstrap window.

## 3. Safety

- This is a **test** stack: isolated data volume, own port, **no shared
  database**. Never point it at a production Neo4j/Postgres.
- The app port is published on loopback only; exposure is via the reverse proxy.
- Auth is always enforced: the default is the client-token tier; the no-auth
  bind guard refuses an unauthenticated non-loopback bind, so the container
  will not start `0.0.0.0` without an auth mode configured.

## Phase 2 — full mode

Uncomment the `neo4j-test` service and the marked `menhir-test` lines in
`docker-compose.test.yml` (sets `MENHIR_STARTUP_SCOPE=full` +
`NEO4J_URI=bolt://neo4j-test:7687`). This adds an **isolated throwaway** Neo4j
so the memory backend can be exercised too — still fully separate from any
production graph.
