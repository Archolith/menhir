# Menhir production deployment contract

The operator sequence is in [LIVE_VPS_PLAYBOOK.md](LIVE_VPS_PLAYBOOK.md). This
document defines the host, authority, backup, candidate, writer-fence, and
recovery invariants enforced by the fixed scripts.

Production has separate deployment classes. A routine `app-only` release must finish
within five minutes and does not rebuild host scaffolding or exercise disaster
recovery. `security-config`, `maintenance`, and `recovery` releases use the additional
gates appropriate to the changed authority. The class is mechanically derived during
release preparation; unknown changes become `maintenance`.

Production is only a promotion target. Before another release, the exact finalized image must
complete a production-equivalent staging deployment with production memory/network/ingress
constraints, isolated disposable data, non-production credentials, OAuth and MCP behavior,
restart handling, and automatic rollback. The resulting immutable receipt plus one owner approval
is required for promotion. The production cutover consumes that evidence and runs only a bounded
read-only public canary; it does not debug automation or recreate CI evidence.

Routine `app-only` releases do not create a fresh backup, rehearse a restore, traverse the complete
production database, or start the full maintenance candidate transaction. They verify scheduled
backup/restore freshness and automatically restore the prior application on failed acceptance.
The same bounded model is implemented for non-migrating `security-config` releases by a dedicated
config/application transaction. It atomically replaces only the reviewed authority files, restarts
the gateway and app, proves Neo4j and Cloudflared identities unchanged, and restores the prior set
on failure. Full state protection remains mandatory for
mechanically classified maintenance and recovery work.

Product release and personal deployment are separate trust boundaries. The product workflow may
publish packages, images, provenance, and changelogs without access to this host. This deployment
contract may select and promote one published digest, but it may not rebuild, modify, tag, or
publish product artifacts. A successful product release does not imply deployment, and a successful
personal deployment does not create a new product release.

The normal personal sequence is `rehearse`, `approve`, then `promote`. Rehearsal performs selection,
the read-only production preflight, and isolated production-equivalent staging in one resumable
operation. Approval is the sole required human gate; the coordinator derives the exact release and
receipt digests instead of asking the operator to copy them between commands.

The client data-plane invariant is in
[ACCESS_CONTRACT.md](ACCESS_CONTRACT.md): the only production client endpoint is
`https://memory.ctharvey.me/mcp-http`; ChatGPT, Codex, and every Claude variant
are operators; OpenCode variants are agents; and each client proves its
policy-bound identity through OAuth 2.1 authorization code + PKCE S256 and a
signed JWT access token.

## Host topology

Production uses Compose project `menhir-prod` with two services: `menhir` and
`neo4j`. The application joins the external `menhir-proxy` network for ingress.

```bash
docker network create \
  --driver bridge \
  --subnet 172.30.0.0/24 \
  --gateway 172.30.0.1 \
  menhir-proxy
```

The current live inventory assigns Cloudflared `172.30.0.2` and Menhir
`172.30.0.3` with alias `menhir-prod-app`. Cloudflared proxies the approved public path allowlist
directly to Menhir. Menhir does not publish a host port. The dedicated operator gateway binds only
`172.30.0.1:8000`.

The shared Yawn Caddy container is on `yawndeploy_default`, not `menhir-proxy`. Its inactive Menhir
virtual host and the Menhir Caddy reconciliation units were retired on 2026-09-07. Cloudflared is
the sole supported Menhir ingress authority: release authority declares `cloudflared`, staging
proves that the peer at `.2` is the running Cloudflared Compose service, and every deployment class
retains ingress unchanged. The pre-change Caddyfile is retained root-only under
`/var/lib/menhir-production/ingress-retirement/` for incident rollback.

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
- the mechanically derived deployment class and both generated release-note digests;
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
systemd units, read-only sudoers, Cloudflared topology, inspection gateway, a read-only admission
audit, desktop archival, and restore evidence are scaffolded once. Successful
bootstrap writes a root-owned receipt binding that host contract.

The dedicated OAuth gateway is inspection-only: release inspect, status, logs,
backup status, and generation inspect. It has no mutation tools or write sudo
authorization. Production mutation remains solely under the canonical Menhir
release/admission authority; the authoritative root-only implementation scripts
remain installed and are not callable through the gateway.

Scaffold convergence starts from the exact reviewed repository root. The operator passes that root
as `SourceRoot`; it must not pass `deploy/scaffold`, because the fixed source map also includes
`deploy/personal_stage_vps.py`. Initial installation and old-policy replacement use a separately
authorized root SSH bootstrap endpoint, acquire the shared admission lock and then the production
mutation lock, and leave one durable `active-install` recovery target if interrupted. Desktop
archive scheduling is a separate backup phase, not a side effect of convergence.
Old hosts are upgraded only through the release installer's durable transaction.
After both canonical locks are held it stops the gateway, refuses if any active
`menhir-op-*` transient worker remains, snapshots and removes the obsolete unit
template/worker/public submit wrappers, verifies absence, and restores exact prior
files and template state on rollback or recovery.

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

This production candidate mode is maintenance-only. Routine staging candidates use isolated
disposable authority and may exercise synthetic writes safely. A production maintenance candidate
must be prevented from committing authority changes by a storage-level read-only boundary or must
operate on an isolated restored copy; an application-mode flag alone is not sufficient future
authority. Once that boundary exists, a cheap transaction/bookmark check may prove no commits
occurred. Full content hashing is reserved for backup/restore validation and exceptional maintenance,
not routine deployment acceptance.

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
5. app-container-only replacement while Neo4j and Cloudflared remain running;
6. bounded readiness, liveness, JWKS, OAuth identity, and authenticated MCP probes;
7. durable success receipt or automatic restoration of the prior app digest.

App-only rollback never restores data because the class proves the data contract and
schema are unchanged. The previous app image/config must remain locally available
during the observation window.

The existing `release-run.sh` is the full `maintenance` transaction, not the routine
app-only path. The owner-approved wrapper creates its root-owned journal and acquires the shared
cross-lane admission fence before the installer can mutate production. Maintenance retains that
ownership through installation, backup, cutover and acceptance; app-only and security-config
consult the same authority and refuse while it is active.

`release-run.sh` accepts no arguments. Its journal records the exact release digest, generation,
completed stage, immutable root start/completion timestamps, and initiating approval and promotion
attempt in `/var/lib/menhir-production/release-run.json`. Recovery and adoption preserve those
values; they never synthesize a new chronology for earlier work.

Its stages are:

1. capture, backup, and retire the legacy writer;
2. decrypt and validate the exact backup;
3. run restore rehearsal;
4. start the readonly candidate;
5. accept health, OAuth, MCP, read/recall, refusal, and authority-before/after;
6. retain and verify the immutable Cloudflared ingress;
7. promote after a second writer-census validation;
8. verify public production health, OAuth discovery, MCP, recall, and mutation.

Each stage stops on nonzero status. A retry resumes the same release and
generation. A different release record does not inherit state from the old run.

## Ingress and public acceptance

Deployments do not rewrite ingress. Preflight proves the sole running Cloudflared Compose peer on
the production network; completion receipts prove its container identity is unchanged; and public
acceptance verifies readiness, OAuth discovery, authentication boundaries, and MCP behavior through
the canonical URL.

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
