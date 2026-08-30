# Live VPS deployment playbook

This is the operator sequence for moving an immutable Menhir release to the
Contabo VPS at `https://memory.ctharvey.me`. It complements the detailed
[production contract](PRODUCTION.md); it does not replace any fail-closed check
implemented by the release author, host wrappers, or Caddy supervisor.

## Current readiness: source-complete, live activation unproven

The reviewed source topology closes the two original bootstrap blockers:

1. `yawn.vps`'s Linux gateway invokes only fixed local
   `sudo -n /srv/menhir/production/bin/<wrapper>` argv without a shell. The
   generic Windows PowerShell/SSH runner remains separate and cannot execute a
   Menhir production operation.
2. The gateway binds the host side of the fixed `menhir-proxy` bridge at
   `172.30.0.1:8000`. Caddy at `172.30.0.2` is the only admitted peer and exposes
   only the release-bound `/ops/mcp` and OAuth protected-resource metadata paths.

The public operations resource is `https://memory.ctharvey.me/ops/mcp`. Its
authorization server is `https://memory.ctharvey.me`; its Caddy prefix is
`/ops`; and the prefix is stripped before the request reaches FastMCP. Port 8000
and the application port 8099 are never published directly.

This is source and test readiness, not proof of a deployed service. The first
live activation still follows every bootstrap, artifact, backup, candidate,
route, and acceptance gate below and records the immutable deployed commits.

## Authority map

Use clean, dedicated release worktrees at the exact remote default tips. Never
author a release from a working checkout or a branch head that has not merged.

| Release input | Canonical branch | Production responsibility |
|---|---|---|
| `Archolith/menhir` | `main` | App image, production compose, policy, backup/restore/candidate/promotion scripts |
| `Archolith/archolith_oauth` | `main` | OAuth wheel embedded in the immutable Menhir image |
| `ctharvey/yawn.deploy` | `main` | Caddy topology and transactional route supervisor |
| `ctharvey/yawn.vps` | `master` | Dedicated operations gateway, fixed host wrappers, systemd, sudoers, and policy enforcement |

`yawn.vps/main` is stale and is not a release source. Its remote default branch
is `master`, which contains the merged Menhir operations lane. Record the four
full commit SHAs in the release evidence.

The generic `vps_deploy`/`remote-deploy.sh` path is not authorized for Menhir or
Caddy. It must refuse this release. Day-2 operations use only the dedicated
`menhir_*` tool surface backed by root-owned fixed wrappers.

## Release flow

```text
clean source tips
  -> immutable images and evidence
  -> release.json
  -> exact root-owned installation
  -> backup and off-host receipt
  -> restore rehearsal
  -> isolated candidate
  -> candidate acceptance
  -> transactional route apply
  -> promotion
  -> public and connector acceptance
  -> observation
```

Every arrow is a stop gate. Do not skip ahead, reinterpret a failed check, or
replace a fixed wrapper with an ad-hoc SSH command.

## 1. Prepare clean release inputs

Create four clean worktrees at the remote tips, fetch tags, and record the
commits. The exact local parent directory is operator-selected.

```powershell
git -C <menhir-repo> fetch origin --prune
git -C <oauth-repo> fetch origin --prune
git -C <yawn-deploy-repo> fetch origin --prune
git -C <yawn-vps-repo> fetch origin --prune

git -C <menhir-release-worktree> rev-parse HEAD origin/main
git -C <oauth-release-worktree> rev-parse HEAD origin/main
git -C <yawn-deploy-release-worktree> rev-parse HEAD origin/main
git -C <yawn-vps-release-worktree> rev-parse HEAD origin/master

git -C <each-release-worktree> status --porcelain=v1 --untracked-files=all
```

For each repository, `HEAD` must equal the named `origin/*` tip and status must
be empty. `release-author.py` independently verifies clean repositories,
canonical GitHub origins, immutable commits, and committed artifact blobs.

Compute the policy digest from the canonical payload, not from the file bytes:

```powershell
python -c "import hashlib,json,pathlib; p=json.loads(pathlib.Path('deploy/client-policy.production.json').read_text()); declared=p.pop('canonical_digest'); actual=hashlib.sha256(json.dumps(p,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')).hexdigest(); print(actual); raise SystemExit(actual != declared)"
```

Put that exact value in the rendered `production.env` as
`MENHIR_CLIENT_POLICY_DIGEST`. The embedded policy digest, environment value,
and release `rendered.policy_sha256` authority must all agree.

## 2. Build immutable artifacts and author the release

Build in controlled CI, never on the VPS. Produce and retain:

- digest-pinned Menhir, Neo4j, Caddy, and base images;
- the OAuth wheel, offline wheelhouse, wheel manifests, SBOM, scan evidence,
  and provenance;
- rendered Menhir compose, yawn compose, Caddyfile, registry, client policy,
  yawn environment, production environment, operations policy, and OAuth public
  key;
- source-fence public material and rollback anchors;
- secret version identifiers only, never secret values.

The rendered operations policy must bind these exact external authorities:

```json
{
  "issuer": "https://memory.ctharvey.me",
  "audience": "https://memory.ctharvey.me/ops/mcp",
  "base_url": "https://memory.ctharvey.me/ops"
}
```

Its exact client entries still decide scopes, tier, and visible tools. The
gateway's OAuth protected-resource metadata must advertise the same audience
and issuer.

Create a release-author spec with the exact top-level fields enforced by
`deploy/release-author.py`. Its `artifact_sources` must exactly match every
destination in `deploy/installed-artifacts.json`. Git-backed entries identify a
repository and committed path; the three generated files identify their
rendered digest keys.

```powershell
python deploy/release-author.py --spec <absolute-release-spec.json> --output <absolute-release.json>
```

The release ID must be monotonic and match
`menhir-prod-<major>.<minor>.<patch>-<sequence>`. Validate the result in a Linux
release environment:

```bash
MENHIR_RELEASE_JSON=/absolute/path/release.json deploy/release-validate.sh
```

Stop if any image is floating, a repository is dirty/noncanonical, evidence is
missing, a rendered digest differs, or an installed destination is absent.

## 3. One-time host bootstrap

Bootstrap is a reviewed root provisioning action through the Contabo console or
the established human operator channel. Codex and the MCP gateway do not gain a
general remote shell for this step.

1. Install Docker/Compose, Python 3, GNU `flock`, `sha256sum`, `stat`, Caddy's
   existing stack, and the `yawn` account.
2. Create the external Docker network `menhir-proxy` with the release-recorded
   topology: subnet `172.30.0.0/24` and gateway `172.30.0.1`. Menhir is
   `menhir-prod-app` at `172.30.0.3`; Caddy is `172.30.0.2`. Do not publish a
   Menhir host port.
3. Create the fixed roots described in [PRODUCTION.md](PRODUCTION.md):
   `/srv/menhir/production`, `/srv/menhir/backups`,
   `/var/lib/menhir-production`, and `/var/log/menhir-production`.
4. Provision secret files with the exact ownership from
   `deploy/secrets-map.sh`. Secret values never enter Git, the release spec,
   environment output, or command arguments.
5. Check out `yawn.deploy` and `yawn.vps` at the exact commits recorded by the
   release. Build `/srv/yawn/projects/yawn.vps/.venv` from its reviewed
   `requirements.txt` without changing the checkout.
6. Install every path in `deploy/installed-artifacts.json` from the exact
   release sources. Use the modes and commands in `yawn.vps`'s
   `ops/menhir/README.md`; do not copy artifacts from an unrecorded checkout.
7. Install the immutable release record as
   `/srv/menhir/production/release/release.json`, owned by root and mode `0444`.
8. Run `/srv/menhir/production/bin/verify-artifacts`. Any owner, type, mode,
   inventory, or digest mismatch stops bootstrap.
9. On an already-running Caddy host, run the fixed supervisor's one-time
   `caddy-release.sh adopt-current`. This records the live rollback authority
   without reloading Caddy.
10. Enable the tmpfiles, reconciliation, operation, and dedicated gateway units
    described in `yawn.vps/ops/menhir/README.md` after artifact verification.

Bootstrap acceptance:

```bash
/srv/menhir/production/bin/verify-artifacts
systemctl is-active menhir-caddy-reconcile.path
systemctl is-active menhir-oauth-operations.service
ss -lnt | grep '172.30.0.1:8000'
! ss -lnt | grep -E '(0.0.0.0|\[::\]):8000'
! ss -lnt | grep -E '(^|:)8099[[:space:]]'
```

Also prove `https://memory.ctharvey.me/.well-known/oauth-protected-resource/ops/mcp`
advertises `https://memory.ctharvey.me/ops/mcp`, the approved operations endpoint
reaches only the OAuth-protected gateway, and an unauthenticated MCP request is
denied. A raw public listener or any other public `/ops` path is an acceptance
failure.

## 4. Execute a release through fixed operations

Before the first mutation, inspect the immutable authority and host fence:

1. `menhir_release_inspect()`
2. `menhir_status()`
3. `menhir_generation_inspect()`

The release commit/digests must match the reviewed record, the maintenance fence
must be open, the host lock must be free, and no previous job may require
recovery.

Run each mutation separately. A submit call returns a host job ID; it does not
mean the operation completed. Use the persisted status after a completion
signal, or a bounded backoff monitor. Never tight-loop poll the gateway.

| Order | Dedicated operation | Required evidence before continuing |
|---:|---|---|
| 1 | `menhir_backup_submit()` | `menhir_backup_status()` reports terminal success and an off-host/WORM receipt bound to the actual generation |
| 2 | `menhir_restore_rehearsal_submit()` | Terminal success and a fresh rehearsal receipt bound to that backup and release |
| 3 | `menhir_candidate_deploy()` | Isolated candidate generation is healthy; production authority remains untouched |
| 4 | `menhir_candidate_accept()` | Fresh candidate acceptance receipt proves health, OAuth, MCP initialize/list/call, read, write, and optional recall |
| 5 | `menhir_caddy_route_apply()` | Transactional Caddy validation, reload, topology/drift checks, and public probes all succeed |
| 6 | `menhir_promote()` | Candidate becomes current, prior generation is retained, and mutation marker is recorded |

After every terminal result, call `menhir_status()`. A timeout, unknown phase,
failed operation, closed fence, stale receipt, or digest mismatch is a hard stop.
Root may run the fixed `recover` wrapper only after proving the lock is free, no
transient unit is active, and installed artifacts still verify.

## 5. Acceptance after promotion

Capture command output and timestamps without capturing bearer tokens or secret
values.

- `https://memory.ctharvey.me/livez` and `/readyz` succeed.
- OAuth discovery, authorization, token refresh, and MCP Streamable HTTP work
  through the public hostname.
- A fresh ChatGPT and Claude authorization receives the current operator scope;
  old tokens are expected to fail after the policy/scope change.
- Hosted operator discovery includes `ingest_document` and `ingest_project` and
  excludes `delete_namespace`, `mint_client`, and `revoke_client`.
- Test one read, one write, one document/project ingest, and one recall using a
  disposable acceptance namespace or artifact.
- An unauthorized client, wrong scope set, wrong tier, and disallowed tool call
  are denied.
- Caddy exposes only the reviewed MCP/OAuth/metadata/health paths. Direct origin
  access and host port 8099 remain unavailable.
- `yawn.deploy/check-drift.sh` reports the exact network, image, Caddy config,
  and release authority.

Observe readiness, error rate, OAuth failures, queue health, Neo4j health,
memory/disk, and enrichment backlog for the release-defined observation window.
Record the release ID, four commits, image digests, job IDs, receipt digests,
probe results, start/end times, and operator identity.

## 6. Rollback and recovery rules

| Failure point | Allowed response |
|---|---|
| Before route apply | Stop, repair a new immutable release, and discard the isolated candidate. Production is unchanged. |
| After route apply but before production mutation | `menhir_caddy_route_rollback()`, verify public probes, then stop. |
| After promotion starts or any authoritative mutation marker exists | Do not use a blind route/image rollback. Use `menhir_rollback()` only when its verified previous-generation contract permits it; otherwise create and verify a reverse generation or use the two-factor production restore procedure. |
| Unknown/timeout/interrupted job | Leave the maintenance fence closed. Inspect status/logs and reconcile completion evidence before root runs the fixed `recover` wrapper. |
| Disaster restore | Root creates the exact generation selection and out-of-band arming file; then the operator calls `menhir_restore_production_submit(confirm=True)`. Both factors are mandatory. |

Never delete the previous generation, backup generation, rollback route, or
release evidence during the observation window.

## 7. Routine later release

After bootstrap is proven, later releases repeat sections 1, 2, 4, 5, and 6.
They still require a fresh backup and restore rehearsal. Reusing old receipts,
editing an installed artifact in place, pulling a branch tip on the VPS, or
calling the generic deployment connector invalidates the release.
