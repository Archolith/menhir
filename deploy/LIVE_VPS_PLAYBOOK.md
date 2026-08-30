# Live VPS deployment playbook

This is the operator playbook for `https://memory.ctharvey.me`. Menhir's legacy
writer and its replacement run on the same VPS. The release machinery therefore
uses a Docker-authoritative same-host fence; it does not require a fictional
second source host, source-only signing key, mTLS listener, or two external
evidence workers.

## Normal operator path

After the one-time bootstrap and immutable release installation, one fixed
command performs and records the release:

```bash
sudo -n /srv/menhir/production/bin/release-run.sh
```

The same operation is exposed to approved operator clients as
`menhir_release_run()`. A repeated call resumes the exact release and generation
from `/var/lib/menhir-production/release-run.json`; it never starts a different
release silently.

The command reports eight named stages and stops at the first failed invariant:

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

## Mandatory release security review

Every release still requires a new independent security review bound to the
complete release authority digest:

```powershell
python deploy/release-author.py --spec <absolute-spec.json> --review-request <absolute-request.json>
python deploy/release-author.py --spec <absolute-spec.json> --security-review <absolute-review.json> --output <absolute-release.json>
```

The reviewer must be a different identity from `release_author`, record
`APPROVED`, cover every required scope, and leave zero unresolved critical and high findings.
The review is mandatory for every release and cannot be reused after any input changes.

## One-time host bootstrap

Bootstrap is the only setup-heavy step. It is not repeated for routine releases.

1. Install Docker/Compose, Python 3, GNU `flock`, `age`, `sqlite3`, AWS CLI,
   `sha256sum`, and the existing Caddy stack.
2. Create `menhir-proxy` with subnet `172.30.0.0/24` and gateway
   `172.30.0.1`. Caddy is `172.30.0.2`; Menhir is `172.30.0.3`. Never publish
   Menhir port 8099.
3. Create `/srv/menhir/production`, `/srv/menhir/backups`,
   `/var/lib/menhir-production`, and `/var/log/menhir-production` as root-owned
   fixed roots.
4. Provision the secret map from `deploy/secrets-map.sh`.
5. Provision `/etc/menhir/backup-restore.agekey`, root-owned mode `0400` or
   `0600`, and configure its public recipient in `/etc/menhir/backup-upload.conf`.
   Back up the identity through the separate recovery channel.
6. Install every immutable path in `deploy/installed-artifacts.json`, including
   `release-run.sh`, `stage-generation.sh`, `same-host-fence.sh`, and their
   Python validators.
7. Install the release record at
   `/srv/menhir/production/release/release.json`, root-owned and not writable by
   group or other.
8. Run `/srv/menhir/production/bin/verify-artifacts` and enable the dedicated
   Menhir operations gateway.

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
lock it quiesces the stack, creates and verifies the complete backup, uploads
the encrypted archive with WORM evidence, disables restart on the captured app,
and removes that exact pair. A current all-container census then refuses:

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

Routine releases repeat only clean input preparation, immutable build/review,
exact artifact installation, and `menhir_release_run()`. Host topology, backup
identity, network, systemd, sudoers, and gateway bootstrap are not recreated.
