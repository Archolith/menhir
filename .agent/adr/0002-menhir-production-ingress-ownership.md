# ADR 0002 — Menhir Production Ingress Ownership

- **Status:** CORRECTED 2026-09-08. The decision recorded in the first version of this ADR rested on
  a factual error and is withdrawn. The corrected decision is below.
- **Date:** 2026-09-08
- **Deciders:** ctharvey
- **Related:** `.agent/plans/menhir-deployment-control-plane-architecture-reset-2026-09-08.md`,
  `.agent/reference/menhir-deployment-control-plane-architecture-spec-2026-09-08.md`

## The error, recorded because it is instructive

The first version of this ADR decided that Menhir would **remain a tenant of the shared
`yawn.deploy` Caddy**, and withdrew `Cloudflared is the sole Menhir ingress` (spec invariant I-13).

That was backwards. It was derived entirely from source files:

- `yawn.deploy/Caddyfile` contains a `memory.ctharvey.me` vhost with an Origin CA certificate and
  Authenticated Origin Pull `client_auth`;
- `yawn.deploy/docker-compose.yml` declares the Caddy service on the `menhir-proxy` network;
- `deploy/docker-compose.cloudflared.yml` existed in the Menhir repository and appeared unadopted.

All three statements are true of the source and none of them is true of the running system. A fresh
independent review reached the same wrong conclusion by the same route, reporting that it had
"verified ADR 0002 against the live Caddyfile" — but a Caddyfile in a git checkout is not live state.

Live verification on 2026-09-08 established the opposite:

| Check | Result |
|---|---|
| `docker ps` | `menhir-prod-cloudflared` up 11 days, alongside `menhir-prod-app` and `menhir-prod-neo4j` |
| `/srv/menhir/production/ingress/cloudflared-config.yml` | Real tunnel `c1e621a0-...` routing `memory.ctharvey.me` to `http://menhir-prod-app:8099`, everything else `http_status:404` |
| `docker logs menhir-prod-cloudflared` | Live requests from Cloudflare edge IPs to `memory.ctharvey.me/mcp-http` |
| `docker network inspect menhir-proxy` | Contains only `menhir-prod-cloudflared` and `menhir-prod-app` |
| `nslookup menhir-prod-app` from inside `yawndeploy-caddy-1` | `SERVFAIL` — shared Caddy cannot resolve or reach the app |
| `curl https://memory.ctharvey.me/livez` | `200` |

The shared Caddy `memory.ctharvey.me` vhost is dead configuration. It reads as live and serves
nothing.

The lesson is narrow and worth keeping: **source describes intent, only the running system describes
state.** An ingress claim must be verified against the host, never against a checked-in config file.

## Decision

**Cloudflared is the sole Menhir public ingress. Spec invariant I-13 stands as originally written.**

- `menhir-prod-cloudflared` owns all public routing for `memory.ctharvey.me`. It is already the live
  ingress; this decision changes no runtime behaviour.
- The shared `yawn.deploy` Caddy has no Menhir role. Its `memory.ctharvey.me` vhost, its Menhir
  certificate mounts, and its declared `menhir-proxy` attachment are dead configuration and are
  retired from `yawn.deploy` source.
- `deploy/docker-compose.cloudflared.yml` is the live ingress definition and is **kept**.
- The `menhir-proxy` network contains only the Cloudflared and Menhir app roles.

## Observed route table

This is the running configuration, not a target. It is materially simpler than the ten-row table the
specification previously froze, and it does not expose the operations gateway at all.

| Match | Upstream |
|---|---|
| `memory.ctharvey.me` where path matches `^/(?:mcp-http(?:/.*)?\|oauth/(?:authorize\|token\|register\|client-metadata/agent-smith\.json)\|\.well-known/(?:jwks\.json\|oauth-authorization-server(?:/.*)?\|oauth-protected-resource(?:/.*)?)\|livez\|readyz)$` | `http://menhir-prod-app:8099` |
| `memory.ctharvey.me`, any other path | `http_status:404` |
| Any other hostname | `http_status:404` |

Two facts here contradict the specification and are recorded rather than corrected, because changing
them is a separate decision:

1. **`/ops/mcp` is not publicly routed.** There is no route to the host operations gateway at
   `172.30.0.1:8000`. The specification's operations-surface design describes something that is not
   deployed.
2. **`oauth/client-metadata/agent-smith.json` is routed** and appears in no version of the
   specification's route table.

## Consequences

- Spec I-13, the system boundary table, and the ingress section revert to Cloudflared ownership, with
  the route table replaced by the observed configuration above.
- Plan P1 #5 remains closed, but the retirement work is the reverse of the first version: delete the
  dead `memory.ctharvey.me` vhost, certificate mounts, and `menhir-proxy` attachment from
  `yawn.deploy`, and keep the Cloudflared compose file in Menhir.
- The separate decisions to take a maintenance window at cutover and to authorize deploys by root
  ceremony rather than a signing key are unaffected. Those concern deploy ceremony, not ingress.

## Open, not closed by this ADR

- Whether `/ops/mcp` should be exposed at all. It is currently unreachable from the public internet.
- `menhir-prod-cloudflared` logs recurring `stream canceled by remote` errors against
  `/mcp-http`. Not investigated.
