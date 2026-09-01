# Live VPS deployment playbook

This is the operator playbook for `https://memory.ctharvey.me`. Menhir's legacy
writer and its replacement run on the same VPS. The release machinery therefore
uses a Docker-authoritative same-host fence; it does not require a fictional
second source host, source-only signing key, mTLS listener, or two external
evidence workers.

## Non-negotiable client access invariant

The canonical client data-plane endpoint is
`https://memory.ctharvey.me/mcp-http`. ChatGPT, Codex, and Claude are
`operator`; OpenCode is `agent`. Every product and host variant uses its own
policy-bound OAuth client identity and OAuth 2.1 authorization code + PKCE S256;
Menhir issues and verifies a client-, audience-, scope-, and tier-bound signed
JWT. The complete identity matrix and tool boundaries are in
[`ACCESS_CONTRACT.md`](ACCESS_CONTRACT.md).

`deploy/client-policy.production.json` version 2 is the executable authority.
Release authoring and production startup refuse a missing contract, a different
primary endpoint, a role/scope/tool mismatch, or a digest mismatch. Never work
around a refusal by creating another memory endpoint, editing live OAuth rows,
or issuing a broader token.

## Determine live state; never document a release number

Release numbers, commits, image digests, and scratch-workspace paths do not belong in
this playbook. They become false as soon as the next deployment starts. Read the live
authority and runtime instead:

```bash
sudo -n /srv/menhir/production/bin/verify-artifacts
sudo -n python3 -c 'import json; v=json.load(open("/srv/menhir/production/release/release.json")); print(v["release_id"], v["repos"]["menhir"], v["images"]["menhir"])'
docker inspect -f '{{.Name}} {{.Image}} {{.State.Health.Status}}' menhir-prod-app menhir-prod-neo4j
curl -fsS https://memory.ctharvey.me/readyz
```

`/var/lib/menhir-production/release-run.json` is an unfinished maintenance
transaction when its stage is not `complete`. A normal app-only deployment must refuse
to start while such a transaction is active; the operator must reconcile or explicitly
close that maintenance operation first.

Backups are a continuously maintained host invariant, not work recreated for every
application release. Keep at least two age-encrypted generations under
`/srv/menhir/backups/encrypted`, archive verified copies on the operator desktop, and
run scheduled restore drills. A fresh release-bound cutover backup and desktop receipt
remain mandatory for writer replacement, state migration, or disaster recovery. No
remote object store, cloud backup provider, provider CLI, or provider credential is
part of this contract.

## Mandatory read-only preflight

Run this before building or reviewing a release. Every check is read-only:

```bash
sudo -n /srv/menhir/production/bin/verify-artifacts
sudo -n /srv/menhir/production/bin/backup-status
curl -fsS https://memory.ctharvey.me/readyz
```

For a writer replacement, verify the local backup wrapper in a non-mutating rehearsal
environment and prove a complete clean restore. Do not discover a missing restore key
or unwritable local archive root by stopping production. For any procedure that exercises MCP
acceptance, obtain a fresh policy-owned OAuth access token just in time; never persist a
refresh token or static operator secret as an acceptance credential.

## Deployment classes

Every release must declare exactly one deployment class. Unknown or mixed changes
fail closed into `maintenance`.

| Class | Use when | Time target | Required path |
|---|---|---:|---|
| `app-only` | Only the Menhir application image changes; data, schema, policy, OAuth, host, route, and lifecycle contracts are unchanged | 5 minutes | Verify scaffold, pull app digest, replace app only, accept, automatic image rollback |
| `security-config` | OAuth, client policy, scopes, secrets metadata, or application configuration changes without a data migration | Planned | Focused independent review plus app replacement and access-contract acceptance |
| `maintenance` | Neo4j, schema, migration/startup writes, durable inventory, backup/restore code, deployment tooling, Caddy, networking, systemd, sudoers, or host topology changes | No 5-minute promise | Full backup, rehearsal, candidate, fence, route, promotion transaction |
| `recovery` | Restoring authority after data loss or an interrupted irreversible operation | No 5-minute promise | Verified generation restore and recovery playbook |

Classification must be produced mechanically from the immutable release inputs and
included in CI evidence. A caller cannot self-assert `app-only`. In particular,
`app-only` requires unchanged Neo4j/Caddy digests, Compose and lifecycle artifacts,
durable-state inventory, policy digest, OAuth wheel/gateway, secret map, schema version,
and migration/startup-write surfaces.

## Swift app-only operator path

The normal deployment is one command and performs only these blocking gates:

1. select an explicit immutable release; never select a bundle by directory mtime;
2. verify CI evidence and the mechanically generated `app-only` classification;
3. acquire the deployment lock and refuse an active maintenance/recovery transaction;
4. verify the existing scaffold receipt, current writer census, backup freshness, desktop
   archive freshness, and most recent scheduled restore-drill result;
5. pull the exact Menhir image digest while leaving Neo4j and Caddy running;
6. replace only `menhir-prod-app` using the existing state and network authority;
7. run bounded internal and public `/readyz`, `/livez`, JWKS, OAuth identity, and MCP
   initialize/list/recall/mutation acceptance with an automatically minted short-lived
   probe token;
8. record success, or automatically restore the prior app digest if acceptance fails.

The required wrapper interface is:

```powershell
# Required contract; not implemented by the current wrapper yet.
PowerShell -File C:\Users\thron\IdeaProjects\scripts\deploy-menhir.ps1 -Mode AppOnly -Release <release-id>
```

The wrapper must enforce these default app-only admission limits from scaffold policy:

- at least two complete encrypted generations on the VPS;
- newest verified encrypted generation no older than 24 hours;
- newest verified desktop archive no older than 24 hours;
- successful clean restore drill no older than seven days;
- image pull/preparation budget of 60 seconds;
- app replacement and readiness budget of 120 seconds;
- authenticated acceptance budget of 60 seconds;
- automatic rollback budget of 60 seconds.

The total foreground budget is five minutes. A timeout fails and rolls back; it does not
escalate automatically into the maintenance transaction. If backup or restore evidence
is stale, the deploy reports the exact scheduled job that must be repaired and exits
without stopping production.

The five-minute clock starts when the reviewed release is already published. CI image
building, scans, release authoring, and human review are release preparation, not VPS
cutover work. Scheduled backup creation, restore drills, and desktop archival continue
outside the app-only critical path.

The existing `scripts/deploy-menhir.ps1` currently implements the full maintenance
transaction below. Until it has an explicit, mechanically guarded app-only mode, do not
describe it as the five-minute path and do not replace it with ad-hoc SSH commands.

## Full maintenance transaction

For `maintenance`, the desktop command performs the release-bound cutover backup,
verified desktop archive, and fixed resumable VPS transaction:

```powershell
PowerShell -File C:\Users\thron\IdeaProjects\scripts\deploy-menhir.ps1 -BundlePath <reviewed-install-bundle>
```

An explicit bundle is required; timestamp-based discovery is not release authority.
A repeated maintenance call resumes the exact release and generation from
`/var/lib/menhir-production/release-run.json`; it never starts another release silently.

The transaction reports eight named stages and stops at the first failed invariant:

```text
capture legacy writer + backup + retire writer
  -> decrypt and validate backup
  -> restore rehearsal
  -> readonly candidate
  -> candidate acceptance
  -> transactional Caddy route
  -> promotion under a second writer-census check
  -> public production acceptance
```

Errors state the missing or conflicting authority. Do not reinterpret a failed
check as success or replace the fixed command with ad-hoc Compose/SSH commands.

## Release inputs

Author from clean worktrees at the exact remote default tips:

| Release input | Canonical branch | Responsibility |
|---|---|---|
| `Archolith/menhir` | `main` | image, lifecycle scripts, policy, release workflow |
| `Archolith/archolith_oauth` | `main` | OAuth wheel embedded in the image |
| `ctharvey/yawn.deploy` | `main` | Caddy topology and route transaction |
| `ctharvey/yawn.vps` | `master` | fixed operator gateway and host wrappers |

`yawn.vps/main` is stale. The generic `vps_deploy`/`remote-deploy.sh` path is not authorized
for Menhir.

The release author verifies clean repositories, canonical GitHub origins,
committed artifact blobs, immutable image digests, rendered-file digests,
wheelhouse/provenance evidence, rollback anchors, and the fixed same-host Docker
topology. The topology authority is:

```json
{
  "topology": "same-host-docker",
  "legacy_container": "menhir-prod-app",
  "production_container": "menhir-prod-app",
  "candidate_container": "menhir-candidate-app",
  "legacy_database_container": "menhir-prod-neo4j",
  "candidate_database_container": "menhir-candidate-neo4j",
  "compose_project": "menhir-prod",
  "compose_service": "menhir"
}
```

No caller can substitute those names or selectors.

## Release review policy

All releases require normal source review and automated CI evidence. A new independent
security review bound to the complete release authority digest is mandatory for
`security-config`, `maintenance`, and any change to authentication, authorization,
secrets, privileged deployment, backup, restore, recovery, or release-authoring code:

```powershell
python deploy/release-author.py --spec <absolute-spec.json> --review-request <absolute-request.json>
python deploy/release-author.py --spec <absolute-spec.json> --security-review <absolute-review.json> --output <absolute-release.json>
```

The reviewer must be a different identity from `release_author`, record `APPROVED`,
cover every affected required scope, and leave zero unresolved critical and high
findings. An `app-only` release does not require a fresh whole-platform security review
when CI proves all sensitive surfaces are byte-identical to the last approved release.
The prior approval remains authority only for those unchanged surfaces.

## One-time host scaffold

The scaffold is the only setup-heavy step. It is not recreated for routine releases.

Install it once from the workspace root and use the read-only commands thereafter:

```powershell
PowerShell -File C:\Users\thron\IdeaProjects\scripts\menhir-scaffold.ps1 -Mode Install
PowerShell -File C:\Users\thron\IdeaProjects\scripts\menhir-scaffold.ps1 -Mode Status
PowerShell -File C:\Users\thron\IdeaProjects\scripts\menhir-scaffold.ps1 -Mode AppOnly
```

`AppOnly` is the admission gate the future fast deployment wrapper must call before
its first mutation. It validates the root-owned contract/receipt, exact runtime,
retention and freshness evidence, retained desktop generation, current clean-load
drill, absence of candidate/maintenance state, and public production readiness.

1. Install Docker/Compose, Python 3, GNU `flock`, `age`, `sqlite3`, `sha256sum`,
   and the existing Caddy stack.
2. Create `menhir-proxy` with subnet `172.30.0.0/24` and gateway
   `172.30.0.1`. Caddy is `172.30.0.2`; Menhir is `172.30.0.3`. Never publish
   Menhir port 8099.
3. Create `/srv/menhir/production`, `/srv/menhir/backups`,
   `/var/lib/menhir-production`, and `/var/log/menhir-production` as root-owned
   fixed roots.
4. Provision the secret map from `deploy/secrets-map.sh`.
5. Provision `/etc/menhir/backup-restore.agekey` as a root-owned mode `0400` or
   `0600` identity, install `/usr/local/sbin/menhir-backup-local`, and create the
   root-owned archive root. Install and test the desktop archival procedure; its
   root-owned receipt is a promotion prerequisite. Never expose secret values in the
   release bundle or logs.
6. Install every immutable path in `deploy/installed-artifacts.json`, including
   `release-run.sh`, `stage-generation.sh`, `same-host-fence.sh`, and their
   Python validators.
7. Install the release record at
   `/srv/menhir/production/release/release.json`, root-owned and not writable by
   group or other.
8. Run `/srv/menhir/production/bin/verify-artifacts`, enable the dedicated Menhir
   operations gateway, install the read-only admission-audit timer and desktop-archive
   job, and write a root-owned scaffold receipt binding the installed host contract.

Neo4j Community provides offline `dump`, not online `backup`. Therefore
`backup-generation.sh` remains an explicit maintenance operation: it quiesces the
stack and must never be installed as an unattended timer. Its completed clean-load
check supplies current backup and restore-drill evidence outside the app-only critical
path. The daily scaffold timer audits that evidence without stopping production.

Routine deploys verify that receipt and the referenced files. They do not rerun
`groupadd`, `usermod`, recursive ownership normalization, `systemctl enable`, network
creation, key provisioning, or gateway bootstrap unless the release is classified as
`maintenance` and explicitly changes that surface.

Bootstrap acceptance includes:

```bash
/srv/menhir/production/bin/verify-artifacts
docker network inspect menhir-proxy
ss -lnt | grep '172.30.0.1:8000'
! ss -lnt | grep -E '(0.0.0.0|\[::\]):8000'
! ss -lnt | grep -E '(^|:)8099[[:space:]]'
```

The operations resource remains `https://memory.ctharvey.me/ops/mcp`, with
issuer `https://memory.ctharvey.me`, audience
`https://memory.ctharvey.me/ops/mcp`, and gateway base URL
`https://memory.ctharvey.me/ops`. Caddy at `172.30.0.2` is the only admitted peer
to the gateway.

## What `release-run.sh` proves

The first stage captures the exact running legacy app and database container IDs,
image IDs, Compose project/service labels, runtime mode, restart policies, networks, host
machine identity, release ID, and release digest. While holding the shared host
lock it quiesces the stack, creates and verifies the complete encrypted local backup,
records a durable receipt, disables restart on the captured
app, and removes that exact pair. A current all-container census then refuses:

- either captured container ID or legacy name;
- any `menhir-prod` app/database Compose service;
- any other production-mode Menhir container;
- a renamed container using the captured image as a production writer.

Only the exact `menhir-candidate-app` in `candidate-readonly` mode is allowed.
Candidate acceptance and promotion each repeat the census under the host-wide
maintenance lock. Promotion cannot start a writable production container from
mere absence; it needs the root-owned release-bound fence receipt.

The backup is decrypted only from the receipt-selected local encrypted archive,
using the fixed root identity. Extraction rejects traversal, links, special
files, mixed roots, digest mismatch, and a manifest for another release or
generation. The plaintext staging archive is removed after validation.

The route transaction validates the immutable Caddy bundle and local network,
TLS, Authenticated Origin Pull, firewall/listener, and public-path contracts.
Public acceptance after promotion verifies `/livez`, production-mode `/readyz`,
OAuth discovery, MCP initialize/list/call, recall, and mutation with the fixed
short-lived acceptance credential.

It must also verify the access contract: all four products target `/mcp-http`;
their signed token resolves to the expected client identity and role; an allowed
tool succeeds; and a denied tool remains absent/refused. A Codex or Claude token
issued before an agent-to-operator promotion is expected to fail exact-policy
validation until that client reauthorizes.

## Recovery

`release-run.json` records the last completed stage. Re-run the same fixed
command after correcting the reported condition. Do not delete its state or
change `release.json` to force a resume.

| Failure point | Response |
|---|---|
| Before the verified backup | Legacy writer is retained; repair and retry. |
| After backup, before route | Writer remains fenced; repair the named stage and resume. |
| After route, before promotion | Run the fixed route rollback if the transaction did not reconcile automatically, then resume or stop. |
| After first writable mutation | Keep the fence closed. Prefer roll-forward; otherwise use the verified generation/reverse-rehearsal recovery contract. |
| Unknown/interrupted operation | Inspect persisted stage/job evidence. Never infer success from a missing process. |

Retain the previous generation, encrypted backups, rollback route, release
record, security report, fence receipt, and acceptance evidence through the
observation window.

## Routine later release

Routine app-only releases repeat only immutable CI preparation, explicit release
selection, scaffold verification, exact app-image pull, app-only replacement,
authenticated acceptance, and automatic prior-image rollback on failure. Host
topology, Neo4j, Caddy, backup identity, network, systemd, sudoers, gateway bootstrap,
full backup generation, and restore rehearsal are not recreated.
