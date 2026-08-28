---
artifact_schema: 1
artifact_uuid: a6b498e0-f409-424a-b516-84b96cc5703e
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir full production migration to Contabo

## Decision

Move the complete Menhir production stack to the existing Contabo VPS:

- public OAuth authorization server and resource-server metadata;
- Streamable HTTP MCP at `https://memory.ctharvey.me/mcp-http`;
- Menhir runtime, scheduler, recall, writes, enrichment, and model clients;
- the production Neo4j graph and indexes.

Use the existing Cloudflare-proxied DNS and Caddy ingress. Run Menhir and Neo4j as a dedicated
Compose project with separate networks: Caddy and Menhir share only a proxy network; Neo4j is
reachable only from Menhir on an internal network and publishes no host port. Preserve the accepted
ChatGPT DCR registration, canonical origin/resource, signing key, refresh families, internal
`chatgpt-chat` label, agent-tier cap, namespace policy, and exact MCP tool allowlist.

This revision replaces the earlier split VPS-edge/private-worker topology. There is no private
Cloudflare Tunnel, remote worker RPC, forwarded OAuth bearer token, or cross-host operation
manifest in the target architecture.

## Why and measured fit

The migration removes the failure mode that motivated this work: public memory availability no
longer depends on a Windows process, an interactive PowerShell window, a home/LAN Neo4j host, or a
private tunnel connector.

The Contabo host was observed with 11 GiB RAM, about 8.5 GiB available, low load, and 48 GB free
before the large Pokémon image cleanup. The owner has confirmed that cleanup completed; phase 0
must capture the new figures rather than assume how much space was reclaimed. The current graph is
modest (57,080 entities and 2,610 episodes in the latest operational snapshot). A prior logical
export was 8.6 MB and an older compressed raw store snapshot was about 210 MB, but neither is a
substitute for a fresh production dump and store-size measurement.

Hardware is therefore expected to be sufficient. The production risks are state consistency,
shared-host isolation, secrets, backups, single-writer cutover, and rollback—not CPU capacity.

Neo4j Community supports offline database dumps, loads, and consistency checks; online backup is an
Enterprise feature. The migration and recurring recovery design therefore use a bounded maintenance
window and verified offline dumps. See Neo4j's official
[backup and restore](https://neo4j.com/docs/operations-manual/current/backup-restore/) and
[Docker dump/load](https://neo4j.com/docs/operations-manual/current/docker/dump-load/) guidance.

## Target structure

```text
ChatGPT / Codex
       |
       v
Cloudflare proxied DNS/WAF: memory.ctharvey.me
       |
       | TLS Full (strict) + authenticated origin
       v
Contabo host firewall + existing Caddy (Menhir application ingress: 443)
       |
       | shared proxy network
       v
Menhir production container
  - OAuth AS / DCR / token / JWKS / metadata
  - OAuth resource-server validation
  - /mcp-http and client/tool/tier policy
  - full runtime, scheduler, recall, writes, enrichment
  - outbound model/embedding calls
       |
       | menhir-internal Docker network only
       v
Neo4j Community container at the exact discovered source version/digest
  - production graph, indexes, constraints
  - persistent data/log volumes
  - no host-published HTTP or Bolt ports
```

Use a separate Compose project pinned as `menhir-prod` so Menhir can deploy, restart, and roll back
without recreating Yawn services. A root-owned idempotent bootstrap creates the external Docker
network with one explicit stable name, `menhir-proxy`. Both Compose projects declare that same
network as `external: true`; the existing Caddy service keeps its current default network and joins
`menhir-proxy`, while only Menhir joins it under the unique alias `menhir-prod-app`. Neither project's
`down`, rollback, cleanup, nor prune path may remove the external network. Keep Neo4j solely on a
Compose-owned `internal: true` network. Deployment validation proves the exact project name,
network labels, attachments, alias, and absence of unintended peers before and after every change.
`yawn.deploy` owns the Caddy route and Caddy-side network declaration; Menhir owns its image,
runtime profile, Compose contract, and application-side declaration; `yawn.vps` owns bounded
deployment, status, logs, backup, and restore operations.

## Scope

In scope:

- a production runtime/route profile that starts the full backend while exposing only approved
  public OAuth/MCP/health surfaces;
- immutable-client OAuth/tool policy and durable refresh-retry idempotency;
- hardened Menhir and Neo4j containers, networks, volumes, secrets, limits, health, and monitoring;
- a rehearsed Neo4j + OAuth state migration from the private host to Contabo;
- encrypted off-host backups, restore drills, cutover, rollback, and real ChatGPT E2E proof;
- retiring the old public gateway, private worker/runtime, public tunnel route, and source writer
  only after the observation window.

Out of scope:

- Neo4j version upgrades, Enterprise clustering, or changing store format during migration;
- replacing the embedded OAuth authority with an external IdP;
- changing the accepted DCR client to CIMD;
- public app-directory or Company Knowledge submission;
- high availability across multiple VPS hosts.

## Authorities and refusal outcomes

| Invariant | Authoritative enforcement | Defense in depth | Refusal/outcome |
|---|---|---|---|
| Public ingress | Contabo/provider firewall + Docker-aware host rules | Cloudflare proxy + authenticated origin | direct-origin and non-Caddy application ports unreachable |
| Host administration | OpenSSH policy + provider console | source-IP restriction and login alerting | password/root login refused; recovery remains possible |
| Cloud control plane | Contabo/Cloudflare/registrar account policy | scoped API tokens and change alerts | MFA required; unauthorized DNS/host changes detected |
| Container privilege | Compose security contract + Docker daemon policy | image scan and runtime drift audit | privileged/host-namespace/socket-mounted workload rejected |
| OAuth validity | Menhir `BearerAuthMiddleware` | exact issuer/resource/JWKS configuration | `401` with correct Bearer challenge |
| `chatgpt-chat` identity | immutable DCR `client_id` -> local policy | token display name is informational | unknown/missing client refused |
| Tier, namespace, tools | in-process Menhir tool/domain authorities | filtered `tools/list` | `403`/MCP refusal; never upgrade |
| Graph side effects | Neo4j transactions and Menhir repository/domain guards | public route minimization | transaction/refusal; no alternate RPC |
| OAuth writes | one Menhir process + durable OAuth store | local store lock and old-host operational fence | second writer fails startup |
| Graph writes | one target Menhir runtime after quiescence | source services disabled/read-only | cutover stops if any old writer remains |
| Backup validity | complete durable-state generation manifest + clean restore | Neo4j/SQLite integrity, checksums, OAuth/graph lifecycle proof | incomplete, mixed-epoch, or mutable artifact rejected |

The VPS holds the OAuth signing key, graph, and model credentials, so compromise of its root account
is a full Menhir compromise. Container/network isolation limits accidental exposure and
cross-service blast radius but is not a defense against hostile VPS root or provider control. On
2026-08-25 the owner explicitly accepted Contabo's provider-at-rest trust boundary for this migration;
that acceptance does not weaken host hardening, secret isolation, or recovery requirements.
Encrypted off-host backups remain mandatory.

## Production invariants

1. `memory.ctharvey.me`, the exact MCP resource, issuer, DCR client record, signing key, refresh
   families, scopes, and callback remain unchanged across cutover. Mismatch blocks cutover; partial
   OAuth recovery is forbidden.
2. The production DCR `client_id`—not token `client_name`—selects the worker-local policy record for
   internal label `chatgpt-chat`, allowed scopes, agent maximum tier, namespace, and exact MCP tools.
   Missing, duplicate, empty, unknown, or drifted policy fails closed. `menhir:admin` remains absent.
3. Only Caddy binds a public application port; narrowly controlled SSH is a separate management
   path. Menhir has no direct public host bind, and Neo4j publishes no host port at all. Explorer,
   generic REST mutations, SSE `/mcp`, `/api/internal/backend/*`, and operator/admin surfaces are not
   mounted on the public production profile.
4. Exactly one OAuth authority and one graph-writing Menhir runtime are active. Source services,
   tasks, scheduler, tunnel route, and Neo4j are stopped and disabled before the final dumps; source
   state becomes read-only before target writers start.
5. OAuth retry idempotency survives restart. Refresh rotation and an encrypted, bounded,
   digest-keyed retry receipt commit atomically; raw presented refresh tokens never persist or log.
6. The Neo4j migration uses the discovered exact source version, edition, image digest, and plugin
   artifact checksums for dump, check, load, rehearsal, and first production boot. `5.26.26` is the
   currently expected version, not an assumption: any mismatch parameterizes the target to the
   source or blocks cutover. No version, store-format, APOC, index, or constraint upgrade is combined
   with data transfer.
7. Every migration artifact is encrypted in transit and at rest, checksummed, integrity-checked,
   restored in rehearsal, and reconciled for databases, counts, indexes, constraints, selected
   content digests, and queue/scheduler state before it can become production.
8. Authentication material, OAuth codes/tokens, memory content, graph values, model prompts, and
   provider keys are absent from Caddy, Docker, Menhir, Neo4j, migration, and backup logs.
9. Backend startup/readiness failure returns bounded `503 backend_unavailable` with no OAuth
   challenge. Invalid credentials remain `401`; insufficient scope remains `403`; an admitted MCP
   tool failure returns protocol-native `CallToolResult(isError=true)` rather than triggering
   reauthorization.
10. Once Contabo performs any OAuth or graph mutation, rollback requires quiescing the target and
    reverse-transferring the newest OAuth and Neo4j authority state, or explicitly accepting loss of
    post-cutover writes plus connector reauthorization. DNS-only rollback is forbidden.
11. No Menhir, Neo4j, Caddy, backup, monitoring, or deployment container is privileged, uses host
    PID/IPC/network namespaces, mounts the Docker socket, receives a device, or gains a capability
    without a documented reviewed exception. Application containers drop all capabilities, set
    `no-new-privileges`, use fixed non-root identities, and expose no host port except Caddy's
    reviewed ingress.
12. Host ingress is default-deny on IPv4 and IPv6 except for an approved shared-service matrix. SSH
    is key-only for a named non-root operator and restricted to an approved management source where
    practical. The Menhir virtual host always requires a zone/hostname-specific authenticated
    origin credential; forged forwarding headers and direct-IP/alternate-SNI requests fail closed.
    Restrict 443 to Cloudflare ranges at L3 only if every co-resident HTTPS service supports that
    rule; otherwise use a dedicated Menhir IP for that restriction or retain the matrix-required
    host sources while Caddy AOP remains Menhir's authoritative direct-origin refusal.
13. A deploy references immutable image digests and a reviewed configuration digest. A new image
    cannot become production until its provenance, vulnerability result, SBOM, signatures when
    available, health checks, database compatibility, and rollback digest are recorded. Runtime
    drift, an unexpected published port, or a changed firewall rule blocks cutover and alerts later.
14. Backup credentials on the VPS cannot delete or rewrite retained off-host generations. Backup
    encryption keys and recovery material are stored separately from the host, and a host/root
    compromise must not silently destroy the only recoverable copy.
15. A versioned durable-state manifest enumerates every writable file, SQLite database and journal
    mode, key, secret, volume, graph database, queue/scheduler record, configuration authority, and
    audit store. Each entry names its writer, consistency boundary, migration inclusion, recurring
    backup, restore order, rollback artifact, retention, and whether it is authoritative or
    disposable. Discovery of any unclassified writable path or volume blocks rehearsal, cutover,
    and backup acceptance.

## Implementation phases

### 0. Capacity, threat, and baseline gate

- Capture `df -h`, inode use, `docker system df`, memory/load, container limits, and current volume
  sizes after the Pokémon image removal. Inventory which Docker layers/volumes were removed without
  using a blanket prune.
- Cutover prerequisites: disk below 70%, at least 30 GB free before image pulls, and at least 20 GB
  free after target images plus a restored rehearsal database and two local dump generations. Alert
  at 80%; hard stop at 85%.
- Build a measured peak-memory and peak-disk budget for the whole co-resident host, not only Menhir.
  Include every currently limited Yawn service (about 6.25 GiB of declared memory limits at plan
  review), actual p95/peak RSS, Menhir, Neo4j heap/page cache/native/vector memory, Caddy, Docker,
  kernel/OS reserve, image pulls/build layers, clean restore, two local generations, backup/check
  processes, security scanners, and scheduled jobs. Define an owner-approved free-RAM/swap/OOM
  headroom threshold from the measurement; do not authorize on limits whose sum merely fits 11 GiB.
  Serialize image builds and memory-heavy checks, suspend overlapping heavy scheduled work during
  rehearsal/cutover, and reproduce the worst approved overlap under cgroup limits without OOM,
  swap thrash, readiness loss, or co-resident service regression. Failure requires smaller measured
  limits, workload separation, or a larger/dedicated host before migration.
- Capture source Neo4j version/edition, plugins, store sizes, `SHOW DATABASES`, `SHOW INDEXES`,
  `SHOW CONSTRAINTS`, node/relationship/episode/entity counts, pending/enriching queue state,
  scheduler lease/owner, and selected UUID/content-hash probes.
- Capture current public discovery documents, DCR metadata without secrets, OAuth DB schema/counts,
  signing `kid`, tool catalog, tier/namespace result, and successful `chatgpt-chat` read/write/recall.
- Record the explicit provider-at-rest decision and the immutable source/target build IDs.
- Produce the durable-state manifest before selecting migration commands. At minimum enumerate
  `menhir_oauth_as.db` and its actual journal/WAL behavior, DCR clients, authorization codes,
  refresh families, durable exact-retry receipts and response-encryption key, signing key and
  retained verification keys, consent secret/replay state, client policy/config digest, Neo4j
  `neo4j` and `system`, indexes/constraints, scheduler lease and pending/enriching queues, audit and
  telemetry stores, Caddy certificate/AOP material, and all Compose volumes/binds. Prove each
  disposable item can be omitted safely; "required state" and "other secrets" are not enumerable
  backup or rollback evidence.
- Build the manifest from two reconciled censuses: an in-repository search of every graph/OAuth/key/
  consent/retry/queue/lease/audit/telemetry/config writer and a runtime inventory of source and target
  processes, services/tasks/timers, container mounts/volumes, bind paths, open database files and
  journals, and observed filesystem writes during representative read/write/refresh/enrichment jobs.
  Resolve every path to an explicit owner and consistency boundary, version the census inputs, and
  fail when source code or runtime exposes a writer/path absent from the manifest. A manually curated
  list without this negative completeness proof cannot authorize rehearsal, backup, or cutover.

The first version of the manifest must contain at least these exact production decisions; discovery
may add entries but may not replace them with categories:

| State/path | Writer/authority and required decision |
|---|---|
| host `/srv/menhir/production/state/oauth/menhir_oauth_as.db` mounted as `/var/lib/menhir/oauth/menhir_oauth_as.db`, plus `-wal` and `-shm` when present | `AuthCodeStore`, `OAuthClientStore`, `OAuthRefreshStore`, durable spent-consent JTI state, encrypted exact-retry receipts, and the pinned `archolith_oauth` transaction; one SQLite consistency boundary, checkpointed/integrity-checked and included in every authority generation |
| host `/srv/menhir/production/secrets/oauth/oauth_signing_key.json` plus explicitly retained verification-key files | `oauth_keys.py`; authoritative key lifecycle, separately encrypted and restored before token validation |
| host `/srv/menhir/production/secrets/oauth/retry_response.key` | encryption key for retry responses whose receipt rows live inside `menhir_oauth_as.db`; restored before refresh handling and rotated under a versioned dual-read/re-encryption runbook. A separate retry SQLite database is forbidden because it cannot share the refresh transaction |
| `client_tokens.db` and journals | disabled and absent in the DCR-policy production profile; startup/census fails if created. If implementation proves it is required, pin its exact host/container path, table writers, migration/backup/restore order, and candidate fence before proceeding |
| host `/srv/menhir/production/state/telemetry/mcp_telemetry.db` mounted as `/var/lib/menhir/telemetry/mcp_telemetry.db`, plus `-wal` and `-shm` | one SQLite boundary covering `GraphOperationsJournal`, `PendingActionStore`, `MigrationBatchStore`, `MetricReceiptStore`, `SchedulerLeaseStore`, erasure state, telemetry events, lifecycle audit, recall/session registries, and LLM usage; table-level writer/authority/retention decisions are mandatory |
| host `/srv/menhir/production/config/client-policy.json` and the rendered Menhir/Compose configuration digests | reviewed read-only release authority; no runtime writer; restored from the immutable release record before startup |
| Compose volume `menhir-prod_neo4j-data` (`/data`) containing Neo4j `system` and `neo4j` plus indexes/constraints; separately classified logs/import/plugins volumes | Neo4j is sole store writer; Menhir is the only Bolt mutation caller. Both databases share the quiesced generation; plugin files are digest-pinned release inputs, not learned state |
| `/srv/menhir/production/secrets/caddy/memory-origin.crt`, `memory-origin.key`, and `cloudflare-aop-ca.pem` | root/Caddy-read-only TLS/AOP authority with serial/expiry/version inventory and separate clean-host recovery; never exposed to Menhir/Neo4j or logs |
| every remaining `/srv/menhir/production` bind/volume, provider/model/Neo4j/backup secret, release/operation-state record, backup credential, audit/alert state, and Caddy-side mount | exact writer, source of truth, backup or external recovery source, restore order, retention, and disposability decision; unclassified discovery blocks the phase |

Audit the exact installed `archolith_oauth` source commit, schema, transaction boundaries, lock behavior,
and packaging artifact before designing retry durability. Pin it in the release record. Structural tests
must fail whenever a new SQLite file/table writer, graph mutation path, bind/volume, or candidate-mode
writer lacks both a manifest row and candidate-fence classification.

- Inventory the entire shared VPS before changing it: OS/LTS version, kernel, enabled package
  sources, users/groups/sudoers, SSH policy, listening IPv4/IPv6 sockets, provider and host firewall
  rules, Docker daemon configuration, Docker group membership, containers, published ports,
  networks, volumes, mounts, secrets, timers/cron jobs, and all workloads sharing Caddy. Preserve a
  redacted baseline and identify each port/process owner; do not assume Compose describes the host.
- Turn that inventory into a blocking shared-service compatibility matrix. For every existing
  container/listener/Caddy vhost record its owner, network attachments, public and management
  ingress sources, Host/SNI, Cloudflare proxy/AOP posture, certificate issuance/renewal flow,
  health probes, outbound callbacks, required ports, preserve/change decision, before/after
  functional probe, and exact firewall/network rollback. The owner approves every change. No host
  firewall, Caddy-network, Docker-daemon, or shared-proxy promotion occurs until every co-resident
  service passes its probe; a failure rolls back the host-level change before Menhir cutover.
- Confirm the Contabo out-of-band console and recovery credentials work before touching SSH or
  firewall policy. Keep two tested administrative sessions during firewall changes and define the
  timed rollback command first, so hardening cannot strand the operator.
- Require phishing-resistant MFA/passkeys where supported for Contabo, Cloudflare, the registrar,
  image registry, backup provider, and alerting account. Store tested recovery codes offline; remove
  stale users/sessions/tokens; use scoped service tokens instead of global account keys; and enable
  account, DNS, firewall, certificate, and VPS lifecycle change notifications to an off-host channel.
- Patch the supported LTS host and Docker Engine to reviewed security levels, record pending reboot
  state, and configure daily security updates with controlled maintenance-window reboots and
  post-reboot health verification. Third-party repositories require an explicit owner and update
  policy; unattended upgrades must not silently restart the database during an online dump/cutover.

### 1. Add a production full-runtime route profile

- Add `MENHIR_STARTUP_SCOPE=production`: start the same full runtime as `full`, but assemble a
  minimal public app that mounts only `/livez`, `/readyz`, redacted capability health, OAuth
  discovery/authorization/token/DCR/JWKS, and `/mcp-http`.
- Do not expose the current full server's general API router, `/api/internal/backend/{operation}`,
  Explorer, SSE, local bootstrap, or admin/operator routes. Forbidden surfaces must be absent (`404`),
  not merely protected by credentials.
- Keep one Uvicorn worker while the embedded OAuth authority uses SQLite and any process state is not
  proven durable. Startup refuses missing HTTPS origin/resource, OAuth state/key, client policy,
  Neo4j credentials, privacy redaction, or production network assumptions.
- Add an authoritative `candidate-readonly` production mode for pre-cutover proof. It starts Neo4j
  and existing-token read/recall plus discovery/JWKS, but does not start scheduler/enrichment and
  refuses every graph mutation, authorization-code/DCR/token/refresh/consent-JTI write, retry-receipt
  write, lease acquisition, schema initialization, or write to authoritative telemetry/session/
  recall/lifecycle/audit/usage state at the final repository/store authority. Open restored SQLite
  authorities read-only and inject candidate-specific no-op or separately mounted disposable sinks
  for request tracking, session/client touches, recall events, lifecycle audit, and LLM usage. The
  candidate evidence sink is classified disposable, contains no token/content/secret data, is never
  merged into production authority, and is destroyed only after its redacted evidence is retained.
  Public mutation attempts return bounded maintenance `503`, not `401`. A structural/runtime census
  covers every graph/OAuth/scheduler/telemetry writer and proves real read/recall probes leave every
  authoritative file and graph digest unchanged. Promotion stops candidate mode, verifies expected
  state/config digests, reopens the authoritative telemetry boundary read-write, and only then starts
  the one-writer production mode; failure keeps the fence in place.
- Split liveness from mode-specific readiness. `candidate-readonly` readiness requires OAuth
  store/key, Neo4j connectivity/schema, read/recall runtime startup, and a proven active mutation
  fence; intentionally absent scheduler/enrichment workers are reported as disabled-by-mode, not
  unhealthy. Production readiness additionally requires the scheduler/enrichment/full runtime and
  sole-writer lease. Transient dependency failure must not emit an OAuth challenge.

Primary Menhir files: `src/menhir/config/settings_model.py`, `src/menhir/api/server.py`,
`src/menhir/api/server_support.py`, route assembly modules, diagnostics, `.env.example`, and tests.
If entrypoint args/env/cwd change, regenerate and validate the central MCP registry.

### 2. Bind public policy to immutable client identity

- Define one versioned production client-policy artifact keyed by immutable OAuth `client_id`:
  internal label, exact scopes, maximum tier, namespace policy, allowed MCP tools, and canonical
  digest. The token's `client_name` cannot select policy.
- Both authorization and tool presentation/invocation use the same policy authority. Unknown client,
  absent policy, duplicate key, empty allowlist, unknown tool, scope expansion, admin scope, or
  digest disagreement fails startup/request closed.
- Add a structural census test that fails when a new public MCP tool is introduced without an
  explicit policy decision. Preserve the current `chatgpt-chat` allowlist exactly at migration.
- Retain the existing in-process tier, namespace, ownership, argument, and domain guards at the
  final side effect. There is no remote backend RPC in this topology.

### 3. Make OAuth state restart-safe

- Extend the pinned `archolith_oauth` package rather than trying to coordinate around its hidden
  transaction from Menhir. In one `menhir_oauth_as.db` SQLite transaction, validate and rotate the
  refresh family and insert the exact-retry receipt. There is no second retry database and no
  sequential dual commit. Persist only a digest of the exact request tuple plus an encrypted
  successor response, expiry, successor digest, client ID, and family/version. Enforce global and
  per-client bounds; purge on successor use, revocation, or expiry; rotate the response-encryption
  key under a versioned runbook.
- Replace `oauth_authorize.py`'s process-local spent-consent-JTI dictionary with a durable table in
  `menhir_oauth_as.db`. Atomically consume an unspent JTI and issue its authorization-code record in
  the same SQLite transaction; duplicate, expired, missing, variant, or already-spent JTIs fail
  closed without issuing a code. Retain spent rows through the signed consent token's replay window,
  purge by bounded policy, include them in the OAuth backup boundary, and refuse all JTI/code writes
  in `candidate-readonly`.
- Add a same-host store lock/lease and prove crash before transaction, crash after commit/before
  response, exact refresh retry after restart, concurrent retry, variant replay, receipt expiry/
  eviction, family revocation, consent consume crash/restart, concurrent consent replay, expiry,
  purge, backup/restore, and candidate refusal. Keep one application replica. PostgreSQL/external
  IdP is a separate prerequisite for OAuth HA.

### 4. Build the isolated Contabo deployment

- Create a dedicated `menhir-prod` Compose definition with pinned Menhir and exact source-version
  Neo4j image digests, fixed non-root UID/GID, read-only root filesystem where compatible, `tmpfs` for required
  scratch paths, `cap_drop: [ALL]`, `no-new-privileges`, the default-or-stricter seccomp/AppArmor
  profile, bounded PIDs/CPU/memory/file descriptors, bounded logs, health checks, restart policies,
  and narrowly permissioned persistent volumes. Document any Neo4j/Caddy exception and prove it is
  the minimum required rather than broadly weakening the project.
- Treat existing `deploy/docker-compose.full.yml` and its `/api/health` check as test/development
  input only, not a production or rollback artifact. The new production Compose contract calls
  `/livez` for process health and the mode-aware `/readyz` for promotion/readiness, uses immutable
  image references, has no source build on the VPS, and has an independently renderable prior
  release bundle. Caddy must not declare a Compose `depends_on` relationship on the separate Menhir
  project; cross-project readiness is established by the transaction probes.
- Forbid `privileged`, host PID/IPC/network, host devices, arbitrary host path mounts, added
  capabilities, writable `/proc`/`/sys`, and Docker/Podman/containerd sockets in every service,
  including deployment, backup, and monitoring helpers. Treat membership in the host `docker`
  group and access to `/var/run/docker.sock` as root-equivalent: application users receive neither;
  bounded root-owned systemd/sudo operations provide deploy/status/backup access.
- Pin the application Compose project name to `menhir-prod`. Declare `menhir-proxy` as an external
  network with that exact name in both Menhir and `yawn.deploy`; pre-create it idempotently through a
  bounded root-owned operation. Attach Menhir with only alias `menhir-prod-app`; attach Caddy while
  retaining its existing default network and every matrix-approved attachment. Attach Neo4j only to
  the Menhir-owned `menhir-internal` network (`internal: true`). Publish no Menhir or Neo4j host
  ports. Disable inter-container communication not required by those explicit networks. Mark the
  external network non-owned by either Compose lifecycle, prohibit its removal by project teardown,
  rollback, or pruning, and test repeated create/deploy/down/rollback cycles. Add a host-wide
  project/network/alias/port/namespace/mount/capability census to deployment validation.
- Set host and provider firewalls to default-deny inbound on IPv4 and IPv6 plus only the approved
  shared-service matrix. Restrict key-only SSH to an approved source/VPN when feasible, with a
  documented break-glass path through the tested Contabo console. If the entire host can accept it,
  permit 443 only from current official Cloudflare ranges; otherwise dedicate an IP to Menhir for
  that L3 rule or keep matrix-required 443 sources while enforcing AOP on the Menhir vhost. Do not
  expose Docker's remote API. Run before/after probes for every shared service and restore the exact
  prior rules/network attachments on any regression.
- Maintain Cloudflare range updates from the official source as a staged last-known-good data set.
  Reject an empty result, parse/signature/source failure, unexpected family loss, or shrink beyond a
  reviewed threshold; never flush working rules first. Add new ranges before removing old ones,
  test IPv4/IPv6 reachability and direct denial, then commit atomically. On failure retain the last
  known-good set, alert with set age, and use a rehearsed rollback. Staleness beyond the documented
  maximum blocks deployment rather than failing open.
- Do not rely on UFW alone for containers: Docker can divert published-port traffic before UFW's
  normal chains. Keep Docker's networking rules enabled, install reviewed `DOCKER-USER` or
  equivalent nftables rules ahead of Docker acceptance, make them persistent across reboot, and
  test both IPv4 and IPv6 from an external host. Only Caddy may publish an application port; an
  unexpected publish fails deployment. See Docker's official
  [packet filtering/firewall guidance](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
  and [`DOCKER-USER` guidance](https://docs.docker.com/engine/network/firewall-iptables/).
- Configure Neo4j from a reviewed complete config, not learning defaults. Start with a measured
  budget around 1 GiB heap and 1–2 GiB page cache, reserve OS/vector-index memory, and finalize with
  `neo4j-admin server memory-recommendation --docker`. Neo4j documents that Docker's 512 MiB
  heap/page-cache defaults are intentionally limited and should be replaced for production; see
  [Docker configuration](https://neo4j.com/docs/operations-manual/current/docker/configuration/)
  and [memory recommendation](https://neo4j.com/docs/operations-manual/current/configuration/neo4j-admin-memrec/).
- Put OAuth signing/state, Neo4j auth, model/provider keys, consent, retry encryption, and backup keys
  in a root-controlled host directory, then project each secret read-only with `0400`/`0440`-style
  access for only the fixed service UID/GID that must read it. The deployment test must prove both
  intended readability and cross-service denial; do not assume Compose `uid`/`gid`/`mode` semantics
  work for bind-mounted source files. Prefer file-based secrets over environment variables visible
  through process/container inspection. Do not copy a general source `.env`; Caddy must not access
  Menhir/Neo4j secrets, and backup credentials must not grant deletion of retained remote
  generations. Validate volume/file ownership from inside each fixed-UID container and after restore.
- Replace the current floating, online `deploy/Dockerfile` build before production. Pin every base
  image by digest; lock all direct/transitive Python dependencies, first-party Git revisions, and
  wheel hashes; build a reviewed wheelhouse in controlled CI; and make the final runtime build
  consume only that immutable closure. Eliminate live `apt` and package-index resolution from the
  release build by using a reviewed immutable runtime base or a controlled, dated OS-package
  snapshot whose manifest is retained. The release record binds source commit, lockfile, wheel and
  OS-package manifests, base digests, SBOM, scan/provenance/signature evidence, and final digest.
  Set remediation deadlines by severity and rebuild from patched bases; never patch a running
  container manually. Registry credentials are pull-only on the VPS and are not mounted into apps.
- Add a release-managed `caddy-menhir-route` target in `yawn.deploy` that owns only the central
  repository revision/config record and the single `caddy` service. It must use the existing
  single-service `--no-deps` path and must not run the broad `all`/multi-service deploy path, rebuild,
  recreate, or wait on unrelated Yawn services. Before replacement run `caddy validate` against the
  rendered candidate, capture the prior Caddy image/config digests and network attachments, then
  prove every existing vhost plus the Menhir positive and negative probes. On any readiness/probe
  failure, persist a failure record and transactionally restore the exact prior config, image, and
  network attachments; a shell `set -e` exit without recorded rollback is not acceptable.
- Configure Cloudflare SSL/TLS as Full (strict), keep the DNS record proxied, and require
  zone/hostname-specific Authenticated Origin Pulls (mTLS) at Caddy. Select one lifecycle now:
  Caddy serves a hostname-matching Cloudflare Origin CA certificate and validates a dedicated
  hostname-level AOP client-certificate chain. Do not install a DNS API token or depend on public
  ACME ingress for this vhost. Caddy trusts forwarded
  client IP/proto headers only from verified Cloudflare peers, rejects alternate Host/SNI and direct
  IP requests, enforces request/body/header/time limits, and emits redacted bounded access logs.
  Cloudflare recommends blocking non-Cloudflare origin traffic and documents that per-host/zone AOP
  is stronger than its shared certificate; see [origin IP restrictions](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/),
  [Authenticated Origin Pulls](https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/explanation/),
  and [Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/).
- Scope manual Origin CA TLS and hostname AOP only to `memory.ctharvey.me`; preserve Caddy's existing
  automatic TLS/renewal behavior and public ports 80/443 for all approved co-resident vhosts.
  `http://memory.ctharvey.me` must be explicitly rejected rather than becoming an unreviewed ACME or
  redirect path. Validate and rehearse issuance/renewal/reload for every existing hostname after the
  change, not only Menhir, and restore the prior vhost configuration on any regression.
- Assign certificate ownership and inventory serials, SANs, issuers, locations, and expiry without
  logging private keys. Alert at 60/30/14/7 days. Rehearse origin and AOP rotation under the final
  firewall by trusting old and new chains during overlap, promoting the new credential at
  Cloudflare, proving positive proxied and negative direct requests, then removing the old trust.
  Any renewal or reload failure keeps the last-known-good certificate/config and pages the owner;
  expiry inside the minimum safe rotation window blocks cutover and deploys.
- Apply public abuse controls at Cloudflare and authoritative OAuth/MCP limits in Menhir: bounded
  request rate/concurrency/body size, authorization-code/token endpoint limits, expensive-tool
  budgets, and backpressure. Rate-limit keys use verified client identity or trusted restored source
  IP and never the untrusted raw forwarding header. Confirm limits do not break MCP streaming or
  OAuth refresh behavior.
- Restrict runtime egress to documented dependencies where maintainable. Menhir needs DNS, NTP,
  model/embedding providers, approved telemetry/backup endpoints, and Neo4j; Neo4j needs no Internet
  egress during normal service. Block host/link-local metadata and unrelated private ranges from
  application containers, and alert on new destinations rather than silently permitting them.
- Add `vps/menhir_tools.py` with dedicated Menhir operations rather than routing Menhir through the
  current fixed Yawn deploy directory or caller-supplied `vps_compose(args)`. Pin the only allowed
  repository root to `/srv/menhir/production`, Compose file to
  `/srv/menhir/production/deploy/docker-compose.production.yml`, project to `menhir-prod`, services
  to `menhir` and `neo4j`, and execution identity to the root-owned wrappers/systemd units created by
  the deployment. `vps/core.py` owns separate Menhir constants/validators; never add a free-form path,
  project, service, profile, Compose-file, environment, or command argument.
- Register explicit operations in `vps_server.py`: `menhir_release_inspect`, `menhir_status`,
  `menhir_logs`, `menhir_candidate_deploy`, `menhir_backup_submit`, `menhir_backup_status`,
  `menhir_generation_inspect`, `menhir_restore_rehearsal_submit`, `menhir_restore_production_submit`,
  `menhir_caddy_route_apply`, `menhir_caddy_route_rollback`, `menhir_promote`, and `menhir_rollback`.
  Backup/restore accepts only a syntax-validated immutable generation ID resolved through the release
  authority, never an arbitrary path. Unknown actions,
  traversal, extra Compose files/profiles, arbitrary environment, metacharacters, symlinks outside
  the fixed root, unrecorded digests, and concurrent mutations fail closed. Deployment preflights
  secrets/ports/firewall/disk/capacity, pulls by digest, proves the candidate, then promotes; failure
  retains the previous image/config/volume recovery anchor and never migrates state backward.
- Give the Menhir production operator principal only those dedicated tools. It must not inherit
  `vps_shell`, generic `vps_ssh`, `vps_exec`, `vps_compose`, arbitrary file/environment mutation,
  unrestricted Caddy reload, or generic Docker prune merely because those tools already exist in
  `yawn.vps`. Add `vps/tool_policy.py` with a fail-closed capability allowlist keyed by immutable
  operational OAuth client ID plus exact audience `urn:yawn-vps:menhir-production`; validate audience,
  client ID, tool name, and tier on every invocation after token verification. `OPERATOR` is necessary
  but never sufficient. The Menhir operations credential is distinct from public `chatgpt-chat` and
  from broad Yawn administration. Missing/duplicate/empty/unknown policy, audience drift, client-name
  substitution, or a registered tool absent from policy is refused. A structural census covers every
  registered tool, and invocation tests prove the Menhir client cannot call any generic Yawn tool
  even when both are `OPERATOR`. The remote execution credential is bound to root-owned fixed-argument
  wrappers and cannot open an interactive shell or run an unlisted Compose project.
- Update `vps/tier_auth.py` with an explicit matrix: `menhir_release_inspect`, `menhir_status`,
  `menhir_logs`, `menhir_backup_status`, and `menhir_generation_inspect` are read-only `AGENT` tools;
  candidate deployment, backup execution, either restore, promotion, rollback, Caddy/route changes,
  credential/certificate rotation, lifecycle mutation, and cleanup require `OPERATOR`. Candidate
  deployment starts only mutation-fenced `candidate-readonly` and cannot promote. Elevate, disable,
  or replace the existing generic destructive surfaces so an `AGENT` cannot reach production through
  `vps_shell`, `vps_ssh`, `vps_exec`, `vps_compose`, Caddy reload, file/env mutation, or prune.
- Long operations submit a background job and return an opaque job ID well inside the gateway's
  90-second request boundary. Define separate wall-clock limits for quiesce, dump/check, package,
  off-host upload, restore/load/check, and resume; persist sanitized phase progress and terminal
  state; and cap every MCP response and retained diagnostic excerpt at 8 KiB. Timeout or cancellation
  is a failed terminal state: terminate the process tree, prove descendants exited, preserve bounded
  evidence and recovery anchors, resume service only when the operation contract says it is safe,
  and release the maintenance lock only after cleanup is proven. Test timeout, cancellation,
  truncation, secret redaction, process death, and lock retention/release.
- Replace timer-listing as backup evidence with a dedicated status record for each complete authority
  generation: unique ID/epoch, start/end/result, source release/config identity, writer-quiescence
  proof, manifest and artifact hashes, local/off-host object identity, immutable-retention result,
  verification result, last clean-volume restore result/age, resume result, and bounded redacted
  failure. Missing/stale/mixed-generation status blocks deploy/cutover and alerts.

### 4a. Host operations, detection, and incident safety

- Use a named non-root administrative account with unique modern SSH keys. Disable SSH root login,
  password authentication, keyboard-interactive authentication, agent forwarding, and unused
  forwarding/features; restrict `AllowUsers`, sudo commands, and session limits. Verify a fresh key
  login and provider console before closing the old session. Add brute-force throttling/login alerts,
  but never let an automated ban block Cloudflare application ranges or the only recovery path.
- Keep the base OS minimal. Remove/disable unused daemons and sockets, deny unused inbound and
  forwarding paths, apply least-privilege file permissions, and enable the host's mandatory-access
  controls. Schedule security updates and reboots around tested backup/restart procedures. Ubuntu
  recommends least privilege, SSH, a host firewall, and automatic security updates; see its
  [security suggestions](https://ubuntu.com/server/docs/explanation/security/security_suggestions/)
  and [automatic-update guidance](https://ubuntu.com/server/docs/how-to/software/automatic-updates/).
- Send sanitized security and availability signals off-host: SSH/sudo/login changes, firewall and
  published-port drift, container/image/config digest drift, OOM/restarts, TLS/origin-auth failure,
  OAuth anomaly/rate limits, disk/inodes, Neo4j health, backup age, restore-test status, and pending
  critical patches/reboots. Alerts must be tested, actionable, rate-bounded, and must not include
  tokens, prompts, graph content, secret values, or authorization URLs containing codes.
- Keep a redacted, version-controlled desired-state manifest for users, SSH, firewall, Docker daemon,
  Compose services, networks, ports, mounts, capabilities, image/config digests, and timers. A daily
  read-only audit compares live state to it. Unexpected root user/key, socket listener, privileged
  workload, Docker socket mount, or public port is a high-severity incident—not self-healed deletion.
- Make the release record the deployment authority rather than mutable branch heads. It records
  immutable Menhir, Neo4j, and Caddy image digests; Menhir, `archolith_oauth`, `yawn.deploy`, and
  `yawn.vps` source commits plus the installed OAuth wheel hash; rendered Compose/Caddy/config
  digests; Compose project name; external-network identity,
  labels, peers, and alias; secret version identifiers without values; schema/store compatibility;
  and the exact prior release/route/recovery anchors. Extend drift detection beyond Git status to
  compare this record with live Docker images, containers, mounts, networks, published ports,
  rendered config, Caddy adaptation, and host firewall state. Empty/floating image pins, unrecorded
  live state, or a mismatch blocks deploy/cutover and alerts; no tool may silently normalize it.
  `yawn.deploy/releases.json`, `lib/registry.sh`, and `check-drift.sh` own expected-record validation;
  `yawn.vps` `menhir_release_inspect`/`menhir_status` collect the bounded read-only live comparison.
  Missing/uninspectable fields are drift, not "unknown but healthy," and each field has a negative test.
- Put Menhir deploy/lifecycle, Caddy-route changes, backup, restore, promotion, rollback, and Docker
  cleanup behind one root-owned host lock plus persisted operation-state record. Record operation ID,
  owner/principal, PID and process-start identity, phase, protected release/generation IDs, start and
  heartbeat times, deadline, cancellation state, and recovery instruction. Acquisition is bounded;
  every conflicting operation pair fails closed. Process death does not imply safe unlock: stale
  recovery inspects and terminates descendants, reconciles service/writer state, and requires an
  `OPERATOR` break-glass acknowledgement before clearing the record. Cancellation and timeout retain
  the lock until process-tree death and the phase-specific safe state are proven. Expose read-only
  incident inspection and test simultaneous submissions, crash/timeout/cancel, stale PID reuse, and
  lock behavior across gateway/service restart.
- Disable the current generic `docker system prune` command and replace it, if cleanup remains
  necessary, with an inventory-driven `OPERATOR` garbage collector under that lock. Require a dry
  run, explicit age plus ownership/disposable labels, release-authority comparison, and an empty
  unclassified-resource set. Forbid all network and volume pruning. Never remove `menhir-proxy`,
  stopped candidate/rehearsal containers, retained backup/rollback images, build inputs required for
  rollback, or any active/protected generation. Negative tests create each protected resource class
  and prove it survives; a cleanup refusal is safer than deleting recovery capacity.
- Write an incident runbook for suspected host/root compromise: preserve off-host logs, isolate
  ingress, quiesce writers, snapshot only when safe, rotate OAuth signing/retry/model/Neo4j/Cloudflare/
  registry/backup credentials from a clean machine, revoke refresh families as required, rebuild a
  clean VPS from pinned artifacts, restore verified data, and require ChatGPT reauthorization when
  token integrity cannot be established. Never restore executable host/container state from a
  compromised server as trusted production state.

### 5. Rehearse restore and E2E in isolation

- Build an isolated synthetic staging stack and fresh OAuth state for public protocol/auth testing.
  It must not reach the production graph or accept production client tokens.
- Separately take a fresh rehearsal dump of source `neo4j` and `system` databases under a bounded
  maintenance window. Transfer it encrypted, verify checksums, load it into a non-public rehearsal
  volume using the exact source edition/version image digest and plugin artifact checksums, and run
  `neo4j-admin database check` with that same admin image. Prove the dump, check, load, and first-boot
  commands all resolve to the recorded source digest; a floating tag is not evidence.
- Compare databases, counts, indexes/constraints and ONLINE state, selected content digests, artifact
  graph relationships, namespaces, pending/enriching state, and read-only recall probes. Destroy the
  rehearsal volume only after evidence is retained and the backup is confirmed off-host.
- Run auth-shape, OAuth AS, MCP initialize/list/call, immutable-client policy, route/network denial,
  direct-origin/AOP denial, SSH recovery, IPv4/IPv6 external port scan, Docker firewall-reboot,
  container-privilege/mount/socket census, restart/refresh, log-redaction, security-alert, and
  dependency-outage tests.
- Run a disposable real ChatGPT connector against synthetic staging through authorization, read,
  write, expiry, automatic refresh, process restart, and post-restart recall. Soak 24 hours.
- On cloned synthetic authority state, rehearse the post-mutation reverse path with the exact source
  recovery binaries/configuration: enable target writers, create a graph mutation and OAuth refresh
  rotation, quiesce the target, transfer the complete generation back, restore it, and prove the
  source-compatible runtime can refresh, read, recall, and continue writing. If this cannot pass,
  record the production mutation gate as an explicit roll-forward-only point of no return and obtain
  owner acceptance before cutover; do not describe source rollback as available after mutation.
- Rehearse the exact Cloudflare route change and rollback against synthetic staging, then use a
  scheduled production maintenance window for the positive candidate proof. The production
  hostname is switched from the old tunnel origin to Contabo while the target is authoritatively
  `candidate-readonly`; this preserves canonical `memory.ctharvey.me` Host/SNI and traverses the real
  proxy/WAF/Full-strict/AOP path. Save the exact prior route object and TTL/state, use a narrowly
  scoped API token, verify the observed Cloudflare configuration/version after each change, and
  provide a one-command idempotent rollback. Do not depend on Enterprise-only Origin Rule
  DNS/Host/SNI overrides and do not accept a direct host-file/SNI override as equivalent evidence.
- Independently rehearse the `caddy-menhir-route` release transaction through the actual central
  Caddy container. Validate/adapt the complete Caddy config before replacement; preserve automatic
  TLS and HTTP behavior for every existing vhost; observe the exact candidate config/image/network
  identities; and run every matrix probe plus Menhir `/livez`, mode-aware `/readyz`, OAuth discovery,
  AOP-positive, direct-origin-negative, alternate-SNI-negative, and plain-HTTP-negative checks.
  Any failed probe must write a bounded failure record and restore the exact prior config, image,
  and network attachments before the maintenance operation exits. Re-probe all vhosts after rollback.

### 6. Quiesced production migration and cutover

1. Announce the maintenance window and prepare the target images, empty volumes, secrets, Caddy
   route, host/provider firewall, AOP, out-of-band recovery, exact Cloudflare route rollback,
   rollback bundle, and checks while the target remains unable to accept production traffic. Snapshot
   desired-state, shared-service probes, and external scan evidence before enabling production
   ingress.
2. Stop public ingress to the old gateway. Stop and disable every source Menhir process, Windows
   service/task, scheduler/watchdog, tunnel route, CLI writer, and maintenance job. Wait for no
   `PENDING`/`ENRICHING` work or explicitly record/reconcile each residual.
3. Stop source Neo4j cleanly. Prove no Bolt listener or graph/OAuth writer remains and make source
   state read-only. Record that a Contabo-local lock cannot fence the old host.
4. With the source offline, dump both `neo4j` and `system` using the recorded exact source image
   digest and plugin checksums. Run consistency checks with that digest and record sizes/checksums.
5. Resolve every path from the durable-state manifest. With all writers stopped, checkpoint/truncate
   SQLite WAL safely as rehearsed, run `PRAGMA integrity_check`, and snapshot the OAuth DB, signing
   and retained verification keys, refresh families, durable retry receipts/key, consent state,
   client policy/config digest, and every other authoritative file/volume. No wildcard or
   operator-memory selection is allowed. Bind these artifacts and both Neo4j dumps into one
   encrypted cutover generation with a unique epoch, sequence, immutable build/config identifiers,
   per-file hashes, manifest hash, and restore order.
6. Transfer the complete generation over an encrypted channel. Verify the outer and per-artifact
   checksums before decrypting into owner-restricted target volumes. Load `system` and `neo4j`, run
   consistency checks, restore all manifest state, and prove schema/index/count/digest/OAuth parity
   while target mutation remains authoritatively fenced.
7. Start Menhir in `candidate-readonly`: no scheduler, enrichment, OAuth/consent-JTI writes, graph
   writes, retry receipts, leases, schema changes, or authoritative telemetry/session/recall/audit/
   usage writes. Candidate observations use only the disposable redacted sink. Prove internal
   readiness, route minimization, existing-token read/recall, client identity/tier, mutation refusal,
   and unchanged graph plus every authoritative-file digest. Internal probes validate application
   state only; a direct host-file/SNI override is forbidden ingress evidence.
8. Change the canonical Cloudflare route to Contabo while the target remains read-only. From two
   independent public networks prove canonical Host/SNI, WAF, Full-strict TLS, AOP, discovery,
   challenge, existing-token read/recall, direct-origin/alternate-SNI denial, and bounded mutation
   `503`. Compare the observed route version to the intended change. On failure run the idempotent
   prior-route restore and re-enable the immutable source authority only after its digests match.
   Reconfirm the cutover generation and source fence. Then atomically promote the target to one-writer production.
   This promotion is the explicit mutation point of no return: begin the append-only mutation audit,
   run write, automatic refresh, connector reconnect, enrichment completion, and post-write recall,
   and use only the rehearsed complete reverse generation or roll-forward after the first mutation.
9. Observe continuously for two hours and daily for seven days: readiness, auth loops, 4xx/5xx,
   latency, Neo4j memory/page cache, disk/inodes, scheduler lease, queue failures, enrichment p95,
   container restarts, and backup success. Keep source services fenced and state immutable.

### 7. Backups, rollback, and retirement

- Every recurring backup is a complete quiesced authority generation, not an independent graph dump.
  Enter maintenance write refusal, drain and stop scheduler/enrichment, stop Menhir so OAuth and
  graph writers are absent, stop Neo4j cleanly, checkpoint/truncate and integrity-check every
  authoritative SQLite store as rehearsed, capture every authoritative durable-state-manifest entry,
  and dump/check both `neo4j` and `system` with the pinned source-compatible admin image. Bind all
  artifacts to one unique epoch/sequence/build/config/checksum manifest. Resume Neo4j and Menhir only
  after the local generation validates; failure keeps writes unavailable and alerts the operator.
- Keep at least two complete local generations within the disk budget and 7 daily/4 weekly encrypted
  off-host generations. Run recurring consistency checks and a monthly clean-volume restore of the
  complete generation. Acceptance requires restored OAuth discovery, existing-client access-token
  validation, refresh-family rotation/retry behavior, graph read/write/recall, queue/scheduler
  reconciliation, and enrichment continuation. A logical graph export is supplemental, not the
  disaster-recovery authority.
- Use an off-host backend or broker under a separate failure domain that enforces unique immutable
  object generations, retention lock/WORM, and create-only credentials for the VPS. "Where
  supported" is not acceptable: before cutover, negative probes using the actual VPS credential
  must fail to overwrite or delete a dedicated sacrificial retained test generation while a new
  uniquely named generation succeeds. Never use the only meaningful recovery generation for a
  denial probe. Restore/delete credentials and client-side encryption recovery keys remain off-host;
  deletion/retention changes alert out of band. Test recovery from a credential-isolated clean
  machine with the production host unavailable.
- Before the first Contabo OAuth or graph mutation, rollback may restore the old route and immutable
  source state after deliberately re-enabling source services and rechecking the original digests.
- After any Contabo OAuth or graph mutation, prefer roll-forward remediation. If rollback is
  unavoidable and the synthetic reverse rehearsal passed, quiesce Contabo and produce/transfer the
  newest complete authority generation from the durable-state manifest, restore it with the exact
  source-compatible binaries/configuration, prove OAuth/graph parity, then switch ingress. If that
  rehearsal did not pass, the post-mutation plan is explicitly roll-forward-only. If state integrity
  is uncertain, require connector reauthorization and explicitly account for accepted data loss.
  Never run both writers, mix generations, or perform DNS-only rollback.
- After seven stable days and a successful off-host restore drill, revoke obsolete tunnel
  credentials, remove old public routes, archive the Windows launcher, and retain the source dump
  under the approved recovery retention before secure retirement.

## Cross-repository implementation and proof map

This is a four-repository release. A phase is incomplete until the named owner repository has an
immutable commit/release anchor and the corresponding cross-repository acceptance proof. New helper
filenames may change during implementation, but ownership and proof boundaries may not be omitted.

| Owner | Required implementation anchors | Required proof |
|---|---|---|
| Menhir | `deploy/Dockerfile`; a new production Compose file under `deploy/`; dependency lock/wheel and OS-package manifests; `src/menhir/config/settings_model.py`; `src/menhir/api/server.py`; `src/menhir/api/server_support.py`; route assembly; `oauth_authorize.py`; OAuth client/refresh/key adapters; diagnostics; `.env.example`; deployment/backup/restore runbooks | production config render; image dependency-closure/SBOM/scan verification; public-route absence; candidate graph/OAuth/telemetry writer census; `/livez` and both `/readyz` modes; consent-JTI and refresh retry restart/replay/concurrency tests; container/network/secret/limit tests; complete-generation restore and real MCP/ChatGPT lifecycle |
| `archolith_oauth` (`https://github.com/Archolith/archolith_oauth`, currently pinned at `586d715a9f87db17c9b2feaa652715e01afe5214`) | clone/review as its own repository; extend its SQLite schema and refresh transaction API so family rotation and encrypted retry-receipt insertion occur in one `menhir_oauth_as.db` transaction; expose the atomic consent-JTI consume plus authorization-code issue boundary if Menhir cannot own that same-connection transaction; update package metadata/changelog | package unit/integration tests for commit/rollback/crash interleavings, concurrent exact/variant retries, revocation/purge, consent replay, schema upgrade and downgrade refusal; build wheel in isolation, verify hashes/contents, install into a clean Menhir environment, pin the reviewed new commit and wheel hash in `pyproject.toml`/`uv.lock`, and retain the old commit/wheel as rollback anchor |
| `yawn.deploy` | `docker-compose.yml`; `Caddyfile`; `releases.json`; `lib/registry.sh`; `remote-deploy.sh`; `wait-for-readiness.sh`; `check-drift.sh`; new Caddy transaction/probe helpers as needed | extend `tests/registry.test.sh` and add Caddy transaction tests proving target isolation, `--no-deps`, config validation, all-vhost probes, failure recording, exact rollback, external-network survival, immutable release fields, and live-Docker/config/network drift detection |
| `yawn.vps` | `vps/core.py`; new `vps/menhir_tools.py` and `vps/tool_policy.py`; `vps/jobs.py`; `vps/compose_tools.py`; `vps/maintenance_tools.py`; `vps/logs_tools.py`; `vps/tier_auth.py`; `vps_server.py`; root-owned fixed-argument wrapper/systemd-unit definitions and operator runbook; README/CHANGELOG | extend `tests/test_vps_server.py`, `tests/test_tools_integration.py`, `tests/test_deploy_repo_target.py`, `tests/test_logs_tools.py`, and `tests/test_remediation_containment.py`; add focused Menhir operations tests for exact roots/project/files/services/actions and generation IDs, client-ID/audience/tool allowlist plus tier denial, generic-tool non-inheritance, traversal/symlink/metacharacter rejection, 90-second job submission, phase timeout/cancel/process-tree cleanup, 8 KiB output/redaction, lock concurrency/restart/stale recovery, protected-resource prune denial, immutable deploy anchors, quiesced backup, corruption/mixed-generation/off-host/WORM failures, clean-volume rehearsal, production-restore safeguards, and rollback refusal |

The final implementation report records the four source commits, package/wheel hashes, all deployed image/config digests,
the release-authority record, exact commands/results, rollback anchors, live-host evidence, and every
skipped hosted/external check. Menhir tests cannot substitute for Yawn deploy/operations tests, and
local tests cannot certify Contabo, Cloudflare, off-host retention, or real ChatGPT behavior.

## Acceptance gates

| Gate | Required evidence |
|---|---|
| Capacity | measured whole-host steady and worst-approved-overlap RAM/disk model—including current Yawn services, Menhir/Neo4j native/vector memory, OS, builds, backup/check, restore, and jobs—meets recorded headroom thresholds without OOM/swap/readiness regression |
| Public surface | only Caddy has a public application port; approved Menhir routes exist; forbidden routes are `404` |
| Shared VPS | every existing listener/vhost/network/probe/certificate flow has an approved matrix decision, passing before/after probe, and tested host-change rollback |
| Proxy network | the explicitly named external network is pre-created, declared external by both projects, survives repeated deploy/down/rollback/prune denial, retains Caddy's prior attachments, and contains only Caddy plus alias `menhir-prod-app` |
| Caddy transaction | Caddy-only release target validates complete config, changes no unrelated service, probes every vhost and Menhir denial path, records failures, and restores prior config/image/network state on injected failure |
| Host access | named non-root key-only SSH works; root/password login fails; sudo is bounded; Contabo console recovery is tested |
| Control plane | Contabo, Cloudflare, registrar, registry, backup, and alerting accounts have MFA, scoped tokens, offline recovery, and change alerts |
| Origin protection | Full (strict), hostname AOP, selected Origin CA lifecycle, Host/SNI checks, rotation rehearsal, and direct-origin probes pass; Cloudflare-only L3 ingress or its shared-host exception is recorded |
| Docker boundary | no privileged/host-namespace/device/socket workloads; fixed UIDs, dropped capabilities, seccomp/AppArmor, limits, and read-only mounts are proven |
| Firewall | provider + host + Docker-aware rules survive reboot; external scan matches the shared-service matrix; range updates prove add-before-remove, invalid-set refusal, last-known-good retention, staleness alert, and rollback |
| Neo4j isolation | no host-published 7474/7687; only Menhir reaches Bolt on the internal network |
| Neo4j identity | source edition/version, image digest, and plugin checksums exactly match dump/check/load/rehearsal/first-boot artifacts; no floating tag remains |
| Durable state | every writable path/volume is classified by writer, consistency boundary, migration, backup, restore order, rollback, retention, and authority/disposability |
| Restore parity | one complete generation restores all OAuth/key/retry/consent/config authority and both databases; integrity, counts, schema, digests, refresh, graph writes/recall, and queues reconcile |
| OAuth continuity | origin/resource/client/key/refresh state match; existing connector/token and automatic refresh work |
| Client authority | immutable DCR ID resolves to `chatgpt-chat`; agent/scope/namespace/tool caps fail closed under drift/escalation probes |
| OAuth replay durability | exact refresh retry succeeds after restart from the same rotation transaction; variant replay is refused; consent JTIs are atomically consumed with code issue and remain spent across restart; no raw token persists |
| Candidate fence | writer census proves candidate mode cannot mutate OAuth/consent, graph, retry, lease, queue, scheduler, enrichment, schema, or authoritative telemetry/session/recall/audit/usage state; attempted mutations return maintenance `503`, disposable evidence stays isolated, and all authority digests stay fixed |
| Read-only route | canonical route switches to the mutation-fenced target through WAF, Full-strict, and AOP; positive reads, negative direct-origin tests, observed route version, and idempotent prior-route rollback are proven |
| Failure semantics | dependency outage yields HTTP `503` or protocol-native `backend_unavailable` without OAuth challenge |
| Secrets/privacy | secret scan and adversarial log/exception probes find no credentials, tokens, graph content, or prompts |
| Supply chain | every image/config is digest-pinned; SBOM, scan, provenance/signature evidence, patch decision, and rollback digest are recorded |
| Release/drift | immutable four-repository release record, including the installed `archolith_oauth` wheel/commit, matches live image/config/project/network/port/mount/firewall state; branch heads, empty pins, and Git-only drift are refused |
| Detection | off-host alerts fire for login, port/firewall/image drift, restart/OOM, disk, TLS/AOP, OAuth anomaly, backup age, and critical patch state |
| Backup immutability | actual VPS credential creates a unique generation but cannot overwrite/delete retained objects; WORM retention, deletion alerts, off-host keys, and clean-machine restore pass |
| Operations | cold restart, complete quiesced backup, clean restore, key/AOP rotation, and either reverse-state rollback or an owner-accepted roll-forward-only mutation boundary are rehearsed |
| Operations boundary | immutable client-ID/audience/tool capability plus tier confines the Menhir credential to dedicated fixed-root/project/file/service/action operations; bounded redacted execution, shared maintenance lock, prune containment, hostile-input refusal, and generic-tool non-inheritance are proven |
| Compromise recovery | clean-host rebuild and credential-rotation tabletop identifies owners, ordering, token-revocation decision, and maximum acceptable loss |
| E2E | existing production connector reads, writes, enriches, recalls, refreshes, and survives restart without reauthorization |
| Observation | two-hour watch and seven-day monitoring show bounded latency, disk, memory, queue failures, and restarts |

Verification block for each invariant-changing phase:

```text
Invariant:
Authority and refusal outcome:
System boundary and every writer/caller:
In-repo census and external controls:
Enforcement point and required context:
Atomicity and interleavings tested:
Absent/default/legacy behavior:
Staging stop condition and recovery:
Source/target immutable versions:
Live positive and negative proof:
Remaining assumptions:
```

Minimum repository verification (exact additions follow implementation):

```bash
python -m pytest \
  tests/test_backend_mcp_boundaries.py \
  tests/test_mcp_chatgpt_metadata.py \
  tests/test_mcp_oauth_challenges.py \
  tests/test_oauth_as_e2e.py \
  tests/test_oauth_token.py \
  tests/test_oauth_rate_limit.py \
  tests/test_oauth_operator_preflight.py
python scripts/smoke/auth_shapes_smoke.py
python -m menhir.cli diagnostics --json
menhir artifacts validate . --repository menhir

# In yawn.deploy
bash tests/registry.test.sh
# Run the added Caddy target/transaction/drift/network tests, then render the complete Compose and
# Caddy candidates and validate them with the pinned production binaries.

# In yawn.vps
python -m pytest \
  tests/test_vps_server.py \
  tests/test_tools_integration.py \
  tests/test_deploy_repo_target.py \
  tests/test_logs_tools.py \
  tests/test_remediation_containment.py
```

Cutover additionally requires the real container/network census, Neo4j dump/check/load rehearsal,
real ChatGPT lifecycle, cold service restart, encrypted off-host restore, and source/target writer
fence evidence. Unit tests alone do not authorize migration.

## Alternatives considered

- **Split VPS edge and private worker:** minimized initial data movement but retained home/LAN power,
  tunnel, and worker availability dependencies and added bearer forwarding plus cross-host policy.
  Rejected after capacity was freed on Contabo.
- **New dedicated VPS:** improves failure-domain isolation but adds cost and another host to operate.
  The existing Contabo has sufficient measured headroom after cleanup; reconsider if shared-service
  load or security posture changes.
- **Move only Neo4j:** still leaves OAuth/MCP/runtime lifecycle split across hosts and does not remove
  the original Windows-process failure.
- **Upgrade Neo4j during migration:** combines store compatibility with host transfer and weakens
  rollback. Rejected; upgrade only after stable production and a separate rehearsal.
- **Neo4j Enterprise for online backup/HA:** operationally stronger but unnecessary for the current
  single-owner workload and not licensed in the present stack. Revisit if downtime/HA requirements
  change.

## Risks and explicit limits

- Menhir and Yawn share one physical host and provider failure domain. Separate Compose projects,
  networks, volumes, limits, and deployments prevent routine coupling but not host loss.
- Root/provider compromise exposes the memory corpus and credentials. This plan is production-ready
  only under the recorded at-rest trust decision and tested off-host recovery.
- Docker isolation is defense in depth, not a root/daemon boundary. Anyone able to control the Docker
  daemon or join the `docker` group is treated as host root; no application or broad automation gets
  that access.
- Cloudflare source-IP allowlisting alone proves only Cloudflare network origin. Per-host/zone AOP,
  strict Host/SNI validation, and host firewalling are all required to prevent direct-origin bypass;
  the owner must keep Cloudflare address lists and certificates current without fail-open updates.
- Community offline dumps impose brief maintenance windows. Health/error semantics must make those
  windows transient service outages, not authentication failures.
- Vector indexes consume OS memory outside Neo4j page cache. Memory recommendation and sustained
  observation—not container RSS alone—authorize final limits.
- Reverse migration after accepted writes is slower than DNS rollback. The source stays recoverable
  during the observation window, and roll-forward is preferred after target mutation.

## Documentation and ownership

Update Menhir's security posture, architecture, backend/full-runtime workflow, OAuth remote
checklist, VPS/SSH/firewall/Docker hardening baseline, operations/logging/alerting runbooks,
deployment README/examples, `.env.example`, diagnostics, backup/restore, credential rotation,
clean-host rebuild, and incident runbooks, and changelog.

Menhir owns application/runtime/auth semantics and the canonical plan. `archolith_oauth` owns the
single-database refresh-family/retry-receipt transaction and any shared consent-code transaction it
must expose. A dedicated Contabo deploy definition owns Menhir/Neo4j containers and volumes.
`yawn.deploy` owns Caddy/shared proxy-network configuration; `yawn.vps` owns capability-scoped bounded
operational tools. Each cross-repo change requires a reviewed commit and rollback anchor linked from
the final implementation report.
