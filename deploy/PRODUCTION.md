# Menhir production deployment contract

The operator sequence is in [LIVE_VPS_PLAYBOOK.md](LIVE_VPS_PLAYBOOK.md). This
document defines the host, authority, backup, candidate, writer-fence, and
recovery invariants enforced by the fixed scripts.

Production has separate deployment classes. A routine `app-only` release must finish
within five minutes and does not rebuild host scaffolding or exercise disaster
recovery. `security-config`, `maintenance`, and `recovery` releases use the additional
gates appropriate to the changed authority. The class is mechanically derived during
release preparation; unknown changes become `maintenance`.

The client data-plane invariant is in
[ACCESS_CONTRACT.md](ACCESS_CONTRACT.md): the only production client endpoint is
`https://memory.ctharvey.me/mcp-http`; ChatGPT, Codex, and every Claude variant
are operators; OpenCode variants are agents; and each client proves its
policy-bound identity through OAuth 2.1 authorization code + PKCE S256 and a
signed JWT access token.

## Host topology

Production uses Compose project `menhir-prod` with two services: `menhir` and
`neo4j`. Caddy is outside that project on the external `menhir-proxy` network.

```bash
docker network create \
  --driver bridge \
  --subnet 172.30.0.0/24 \
  --gateway 172.30.0.1 \
  menhir-proxy
```

Caddy is `172.30.0.2`; Menhir is `172.30.0.3` with alias
`menhir-prod-app`. Menhir does not publish a host port. The dedicated operator
gateway binds only `172.30.0.1:8000`, and Caddy is its only admitted peer.

The release authority fixes the same-host topology and all container/project
names. Caller-provided names, paths, Compose projects, networks, and commands
are rejected.

## Files and ownership

The fixed roots are:

- `/srv/menhir/production`: immutable scripts/config plus active authority;
- `/srv/menhir/backups`: encrypted archives, decrypted release staging, and
  candidate scratch state;
- `/var/lib/menhir-production`: root-owned receipts and resumable state;
- `/var/log/menhir-production`: operation logs.

`deploy/secrets-map.sh` owns the complete secret permission contract.
Neo4j's auth file is `root:7474 0440`; Menhir/OAuth files are `root:10001
0440`; service directories are not cross-readable. Secrets never appear in
release JSON, Git, command arguments, or diagnostic output.

The required persistent secret set includes Neo4j auth/password, operator key, OAuth
signing key, refresh retry keyring, consent secret, and the selected provider key. An
acceptance token is minted just in time for a deployment probe, expires quickly, and is
removed automatically; it is not persistent scaffold state. The obsolete remote
source-fence bearer token is not required.

The immutable client policy is mounted read-only. Its canonical digest must
match `MENHIR_CLIENT_POLICY_DIGEST`, the rendered policy file, and
`release.json`. Policy version 2 embeds the canonical endpoint and product role
contract. Each OAuth client has its own scopes, tier, and tool allowlist;
ChatGPT, Codex, and Claude are operators, including provenance and ingest,
without namespace deletion or client-administration tools. OpenCode is an
agent with the bounded daily memory surface. Release authoring and production
startup refuse drift from that matrix.

## Immutable release authority

`release.json` binds:

- four clean canonical repository commits;
- Menhir, Neo4j, Caddy, and base image digests;
- OAuth wheel source and hash, wheelhouse manifests, SBOM, scan, and provenance;
- rendered Compose, Caddy, registry, policy, environment, operations policy,
  and OAuth public-key digests;
- rollback anchors and secret version identifiers;
- every installed privileged artifact;
- the exact same-host Docker deployment topology;
- an independent security review covering every required scope with zero
  unresolved critical/high findings.

Every mutating lifecycle script validates this authority before acting. Release
authoring, build, scan, provenance, and review happen before the VPS cutover and are
published as immutable CI evidence. The deployment host verifies that evidence; it
does not rebuild or re-review it.

## One-time scaffold and routine verification

Host users/groups, fixed directories, networks, backup identity, secret ownership,
systemd units, sudoers, Caddy topology, operations gateway, a read-only admission
audit, desktop archival, and restore evidence are scaffolded once. Successful
bootstrap writes a root-owned receipt binding that host contract.

Every deployment verifies the receipt and referenced files, permissions, service
health, and digests. Verification must be read-only and fast. It does not recreate
accounts or networks, recursively rewrite ownership, enable/restart unrelated units,
or reinstall unchanged infrastructure. Drift fails closed into a scoped scaffold
repair or `maintenance` release.

## Runtime modes

`production` admits authorized mutations and owns the active OAuth, telemetry,
and Neo4j authority.

`candidate-readonly` mounts the exact active OAuth/policy/secrets read-only,
uses the active Neo4j authority through an isolated candidate Compose project,
redirects telemetry to disposable candidate storage, and refuses OAuth and MCP
authority mutations with the explicit fenced 503 contract.

The only candidate admitted by the writer census is `menhir-candidate-app`
with Compose project `menhir-candidate`, service `menhir`, and runtime mode
`candidate-readonly`.

## Backup and restore rehearsal

Backups and restore drills are continuously maintained safety controls. They are not
created merely because an app-only image changed. The app-only preflight checks that
retention, desktop-copy freshness, and the latest scheduled restore drill satisfy the
declared RPO/RTO. A missing or stale proof refuses the deploy and requests the backup
job; it does not silently run a long disaster-recovery exercise inside the five-minute
cutover.

The default scaffold policy requires at least two complete encrypted VPS generations,
a VPS generation and verified desktop copy no older than 24 hours, and a successful
clean restore drill no older than seven days. Changing those limits is a reviewed
scaffold-policy change, not a per-deploy command-line override.

The production database is Neo4j Community, which has offline `dump` but no online
`backup` command. `backup-generation.sh` therefore remains explicit maintenance and
is never an unattended timer: it intentionally quiesces the stack and leaves it
stopped on failure. The scaffold schedules only a read-only app-only admission audit;
desktop archival remains scheduled on the operator desktop. Plan offline backup
maintenance before the 24-hour evidence window expires.

`backup-generation.sh` quiesces the stack under
`/run/lock/menhir-production.lock` and captures:

- offline Neo4j `neo4j` and `system` dumps, both loaded and checked in a clean
  store;
- WAL-safe OAuth and telemetry SQLite snapshots with integrity checks;
- the exact secrets, policy, Compose, Dockerfile, environment, release,
  durable-state inventory, and source commit;
- `SHA256SUMS`, strict `MANIFEST.json`, and a completion marker.

`menhir-backup-local.sh` protects the exact generation outside the active writer by
encrypting it with age under `/srv/menhir/backups/encrypted`, verifying a decrypt/hash
roundtrip, retaining the encrypted archive on the VPS, removing plaintext staging, and
writing a structured release-bound receipt. A new host may write one bootstrap
generation, but promotion requires a second distinct encrypted generation and
revalidates both files. The exact cutover archive must also be copied to the desktop;
promotion requires a fresh root-owned receipt bound to that verified desktop copy.
No remote object store, cloud backup provider, provider CLI, or provider credential is
part of the production backup contract.

`stage-generation.sh` selects the archive only from that receipt and decrypts
with `/etc/menhir/backup-restore.agekey` (root, mode 0400/0600). Extraction
rejects absolute/traversing paths, mixed generation roots, links, devices,
special files, and manifest/release/digest mismatch. The temporary plaintext
archive is removed.

`restore-generation.sh <generation>` rehearses into a clean scratch root,
loads/checks both Neo4j databases, verifies SQLite integrity and secret modes,
and writes a release-bound rehearsal receipt. Production restore remains a
separate disaster-recovery operation; routine releases do not rewrite active
authority merely to prove the backup.

## Same-host writer fence

The legacy and replacement writers are on one VPS. The old remote-source
protocol is not a valid authority for this topology.

During a release cutover, the backup transaction captures the running legacy
app and Neo4j containers before quiescing:

- full container and image IDs;
- exact name;
- Compose project/service labels;
- production runtime mode;
- restart policy and networks;
- host machine-id digest;
- release ID and release-file digest.

Only after the complete approved backup receipt verifies does the same locked
transaction disable restart and remove that exact app/database pair. It then
scans every Docker container, including stopped containers. The scan rejects the
captured IDs/names, either production Compose service, any production-mode Menhir,
a renamed writer using the captured app image, and any Neo4j container mounting
the captured production data root. It writes
`same-host-writer-fence.json` atomically.

Candidate acceptance and promotion revalidate the release, host identity,
receipt, and live all-container census while holding the same maintenance lock.
Mere connection failure or container absence is not evidence. A root-owned
release-bound receipt plus a clear census is mandatory.

If the process stops after backup but before receipt finalization,
`same-host-fence.sh` resumes only from the pre-mutation intent and the fresh
verified backup. It cannot invent a legacy identity after the container is gone.

## One-command deployment contracts

The routine app-only transaction performs:

1. explicit release and CI-evidence verification;
2. mechanical app-only classification and scaffold/backup-freshness checks;
3. deployment lock and current writer census;
4. exact Menhir image pull;
5. app-container-only replacement while Neo4j and Caddy remain running;
6. bounded readiness, liveness, JWKS, OAuth identity, and authenticated MCP probes;
7. durable success receipt or automatic restoration of the prior app digest.

App-only rollback never restores data because the class proves the data contract and
schema are unchanged. The previous app image/config must remain locally available
during the observation window.

The existing `release-run.sh` is the full `maintenance` transaction, not the routine
app-only path. It accepts no arguments and holds a separate orchestration lock.

`release-run.sh` accepts no arguments and holds a separate orchestration lock.
It records the exact release digest, generation, and completed stage in
`/var/lib/menhir-production/release-run.json`.

Its stages are:

1. capture, backup, and retire the legacy writer;
2. decrypt and validate the exact backup;
3. run restore rehearsal;
4. start the readonly candidate;
5. accept health, OAuth, MCP, read/recall, refusal, and authority-before/after;
6. apply the immutable Caddy transaction;
7. promote after a second writer-census validation;
8. verify public production health, OAuth discovery, MCP, recall, and mutation.

Each stage stops on nonzero status. A retry resumes the same release and
generation. A different release record does not inherit state from the old run.

## Route and public acceptance

Caddy validates the immutable candidate bundle, release digests, network
subnet/gateway, TLS and Authenticated Origin Pull files, listeners, upstreams,
and public allow/deny paths before reload. It keeps a rollback bundle and
transaction journal and reconciles interrupted reloads.

External signed workers are not a mandatory release dependency. They were
designed for a different topology and made normal releases impossible. The
fixed route transaction performs local firewall/listener/TLS/AOP checks, and the
release performs public HTTPS probes before candidate acceptance and after
promotion. Independent external scans may still be retained as optional audit
evidence.

## Rollback and recovery

For `app-only`, failed acceptance stops the new app and restarts the prior immutable
image against the unchanged data authority. This rollback is automatic and bounded.

For `maintenance`, before the first writable production mutation, route rollback and candidate
discard are reversible. After `first-mutation` exists, blind reattachment of a
stale candidate or legacy image is refused.

Post-mutation recovery prefers roll-forward. A reverse restore requires a
verified generation/rehearsal contract or explicit owner-authorized data-loss
path. Unknown or interrupted operations keep the admission fence closed until
persisted evidence reconciles their outcome.

Never delete the prior generation, encrypted backups, release/security evidence,
route rollback bundle, writer-fence receipt, or acceptance evidence during the
observation window.
