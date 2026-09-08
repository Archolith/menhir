# ADR 0002 — Menhir Production Ingress Ownership

- **Status:** ACCEPTED (2026-09-08). Supersedes the `Cloudflared is the sole Menhir ingress`
  position recorded as invariant I-13 in
  `.agent/reference/menhir-deployment-control-plane-architecture-spec-2026-09-08.md`.
- **Date:** 2026-09-08
- **Deciders:** ctharvey
- **Related:** `.agent/plans/menhir-deployment-control-plane-architecture-reset-2026-09-08.md`,
  `.agent/reference/menhir-deployment-control-plane-architecture-spec-2026-09-08.md`,
  `yawn.deploy@4937657` `Caddyfile`, `deploy/docker-compose.cloudflared.yml`

## Context

Two complete ingress architectures for `memory.ctharvey.me` existed in source at the same time,
both attached to the same external `menhir-proxy` Docker network, with no executed decision
retiring either.

Verified 2026-09-08:

- **Shared Caddy is the live ingress.** `yawn.deploy/Caddyfile:190` terminates TLS for
  `memory.ctharvey.me` using a manually provisioned Cloudflare Origin CA cert plus Authenticated
  Origin Pull mTLS (`require_and_verify` against the Cloudflare origin-pull CA), then reverse
  proxies `/mcp-http`, the OAuth endpoints, `/.well-known/*`, `/livez`, `/readyz` to
  `menhir-prod-app:8099`, and `/ops/mcp` to `172.30.0.1:8000` after `uri strip_prefix /ops`.
  `yawn.deploy/docker-compose.yml:82` attaches that Caddy to `menhir-proxy`.
- **`yawn.deploy` is not a product repo.** Its README describes it as centralized VPS deployment
  config for the yawn infrastructure. It serves four vhosts — `agent.yawn.rip`, `ctharvey.me`,
  `archolith.dev`, `memory.ctharvey.me`. Menhir is one tenant of a shared reverse proxy.
- **A parallel Cloudflared path is also built.** `deploy/docker-compose.cloudflared.yml` defines a
  digest-pinned `menhir-prod-cloudflared` container joining the same `menhir-proxy` network, with
  config at `/srv/menhir/production/ingress/cloudflared-config.yml`.
- **Menhir's own production compose still assumes external Caddy.** The header of
  `deploy/docker-compose.production.yml` states the reverse proxy "lives OUTSIDE this project on an
  external network named exactly `menhir-proxy`".
- **The duplicate release kernel is already retired host-side.**
  `deploy/ansible/roles/menhir_host/tasks/main.yml:77-122` removes
  `/srv/menhir/production/bin/caddy-release.sh`, `caddy-route-apply`, and `caddy-route-rollback`
  and asserts their absence. The 1948-line `caddy-release.sh` survives only in `yawn.deploy` source.

The architecture specification nonetheless recorded Cloudflared-sole-ingress as a **closed**
decision and froze a ten-row Cloudflared route table. Production disagreed with that on every point.

This mismatch is assessed as the primary generator of the deployment review cycle. Between
2026-09-07 and 2026-09-08 the branch took roughly 35 commits after closure was declared twice, with
`deploy/personal_stage_vps.py` touched 11 times and `deploy/personal_promote.ps1` 10 times. Reviews
were comparing an implementation against a specification whose foundational ingress decision had
never been executed, so each pass validly reported ingress-shaped P1 findings that no amount of
implementation work could close.

## Decision drivers

- A shared TLS terminator serving four vhosts must have exactly **one** owner, and that owner cannot
  be one of its tenants. Menhir routes living in the shared Caddyfile is correct tenancy, not
  duplicated authority.
- Cutting `memory.ctharvey.me` over to a private tunnel is a **live TLS and mTLS migration on a
  production hostname**, which is the highest-risk step available and was scheduled behind nine
  review gates that could each rediscover the unexecuted decision.
- Menhir already retired its host-side Caddy release machinery. The remaining duplication is
  source-only and can be removed by subtraction, with absence assertions as evidence.
- The decision must be **recorded durably**. Re-derivation of settled decisions by each fresh review
  is the mechanism this ADR exists to stop.

## Decision

**Menhir remains a tenant of the shared `yawn.deploy` Caddy for all `memory.ctharvey.me` ingress.**

- `yawn.deploy` is the sole owner of the `memory.ctharvey.me` vhost, its Origin CA certificate,
  its Authenticated Origin Pull configuration, and its route table. Menhir does not write, template,
  reconcile, or transact that vhost.
- Menhir owns only its application containers and their `menhir-proxy` attachment under the alias
  `menhir-prod-app`, plus the host operations gateway bound to `172.30.0.1:8000`.
- The `/ops` gateway keeps its current Caddy `strip_prefix` contract. The specification's move to a
  native ASGI mount at `/ops/mcp` is withdrawn.
- Cloudflared-sole-ingress is withdrawn as a target architecture for this cycle. It may be revisited
  as a separate, independently reviewed decision; it is not an open item blocking this work.

## Consequences

Specification (`menhir-deployment-control-plane-architecture-spec-2026-09-08.md`):

- Invariant **I-13** is replaced. The new invariant is that `yawn.deploy` owns the shared vhost and
  Menhir owns no ingress route writer, enforced by a source and host negative census.
- The frozen ten-row Cloudflared route table section is replaced by the shared-Caddy route contract
  as a **read-only expectation** Menhir verifies but does not author.
- The `Cloudflared, operations gateway, and yawn.deploy contraction` section is rewritten: gateway
  keeps `strip_prefix`; `menhir-proxy` legitimately contains shared Caddy.

Plan (`menhir-deployment-control-plane-architecture-reset-2026-09-08.md`):

- **P1 #5 is reframed and largely dissolved.** It is no longer split ownership requiring retirement
  of Caddy routes. It reduces to removing the source-only release-kernel remnants.
- **Phase 10 shrinks** from a cross-repository ingress migration to deletion of the source-only
  duplicates plus the PowerShell transport and read-only Yawn contract work.

Repository actions authorized by this ADR:

- Delete `deploy/docker-compose.cloudflared.yml` and the `cloudflared*.example` config files from
  Menhir, or mark them explicitly non-target.
- Retire from `yawn.deploy` source: `caddy-release.sh`, the `/run/lock/menhir-production.lock`
  release lock, the phase journal, `caddy-route-apply`, `caddy-route-rollback`, and their tests.
- Keep the `yawn.deploy` `memory.ctharvey.me` vhost block and its `menhir-proxy` attachment.

## Open items this ADR does not close

- Whether anything besides the `Caddyfile` block still depends on shared-Caddy behavior for Menhir
  has not been fully traced.
- `caddy-release.sh` was reviewed at header and grep level only; its full 1948 lines were not read
  before this decision.
- Whether `release.json` release authority under `/srv/menhir/production/release/` is still consumed
  by a live path after the host-side retirement is unverified.
