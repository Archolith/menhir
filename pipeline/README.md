# Menhir host-operation artifacts (`pipeline/`)

Moved byte-identical from `yawn.vps/ops/menhir/` in release 0.2.0-16 (yawn.vps
`bc4f29e`); the operations gateway (`menhir-oauth-operations.service` and its
`/srv/yawn/projects/yawn.vps` sources) retired in the same release and is not
here. `pipeline/bin/verify-artifacts` is the on-host verifier; its `required`
and `obsolete` sets are held coherent with `deploy/installed-artifacts.json`,
`deploy/release_spec.py` and `deploy/release-install.sh` by
`tests/test_artifact_authority_coherence.py`. `pipeline/systemd/` and
`pipeline/scheduled-backup.sh` belong to the scaffold (nightly backup timer).
The text below is the original yawn.vps README, kept for its safety model;
paths written as `ops/menhir/...` now read `pipeline/...`.

---

# Menhir production host-operation artifacts

This directory holds the **root-owned, root-executed host side** of Menhir
production inspection. The Python MCP gateway (`vps/menhir_tools.py`) invokes
exactly five read-only wrappers via non-interactive `sudo -n`, using direct
local argv execution with `shell=False`. It exposes no production mutation;
Menhir's canonical release/admission workflow is the sole mutation authority.

## Layout (source)

| Path | Purpose |
|------|---------|
| `bin/lib.sh` | Shared constants + status/recovery helpers. Sourced, not executed. |
| `bin/verify-artifacts` | Root-only: verifies every installed artifact (owner 0, non-symlink, non-group/other-writable, sha256) against `release.json`. |
| `bin/recover` | Root-only: verifies the lock is free, no transient unit is active, and artifacts verify; then reopens the maintenance fence. |
| `bin/{status,release-inspect,logs,backup-status,generation-inspect}` | Read-only wrappers. |
| `systemd/menhir-oauth-operations.service` | Runs the dedicated HTTP-only OAuth gateway from the reviewed `yawn.vps` checkout. |
| `etc/tmpfiles.d/menhir-production.conf` | Runtime/status directories. |
| `etc/logrotate.d/menhir-production` | Log rotation. |
| `etc/sudoers.d/menhir-production` | Narrow sudoers (exact wrappers only). |

## Fixed authority (single source of truth)

- Production root: `/srv/menhir/production`
- Compose file: `/srv/menhir/production/deploy/docker-compose.production.yml`
- Compose project: `menhir-prod`
- External network: `menhir-proxy`
- Compose services: `menhir`, `neo4j`
- Wrapper dir: `/srv/menhir/production/bin`
- Operation lock: `/run/lock/menhir-production.lock` (`flock`, kernel-held; precreated `root:menhir-operators 0660`)
- Persisted status: `/var/lib/menhir-production` (`jobs/`, `backups/`, `receipts/`, generation files, `restore-armed`, `restore-selection`, `fence`)
- Immutable release record: `/srv/menhir/production/release/release.json` (root-owned, mode <= 0444)
- OAuth operations policy: `/etc/yawn-vps/menhir-oauth-policy.json` (root-owned, release-rendered, mode 0644)
- OAuth verification key: `/etc/yawn-vps/menhir-oauth-public.pem` (root-owned public key, release-rendered, mode 0644)
- OAuth issuer: `https://memory.ctharvey.me`
- OAuth external base URL: `https://memory.ctharvey.me/ops`
- OAuth operations resource/audience: `https://memory.ctharvey.me/ops/mcp`
- OAuth gateway: `/srv/yawn/projects/yawn.vps/menhir_server.py`, HTTP on the fixed Docker bridge gateway `172.30.0.1:8000`; the only permitted network peer is Caddy at `172.30.0.2/32`; policy/key paths, bind address, port, and transport are not environment-configurable
- Logs: `/var/log/menhir-production`
- Retired gateway lane: `menhir-op@.service`, `bin/worker`, and the public
  submit wrappers are source-history only and must not be installed.

## Canonical mutation implementation scripts

The root-only Menhir implementation scripts remain installed and authoritative.
They are reached only through Menhir's canonical release/admission workflow,
never through this OAuth gateway, a public submit wrapper, or a Yawn worker.

| Script | Invocation | Contract |
|--------|------------|----------|
| `/srv/menhir/production/bin/backup-generation.sh` | Internal, root-only | Backs up the current generation and records its exact identity in durable release evidence. |
| `/srv/menhir/production/bin/restore-generation.sh` | Internal, root-only | Restores only the generation selected and authorized by the canonical maintenance transaction. |
| `/srv/menhir/production/bin/candidate-deploy.sh` | (no args) | Stages a candidate generation and writes `/var/lib/menhir-production/candidate-generation`. |
| `/srv/menhir/production/bin/promote.sh` | (no args) | Promotes candidate -> current (current -> previous), updates the generation files, and brings up production without restarting shared dependencies. |
| `/srv/menhir/production/bin/rollback.sh` | (no args) | Swaps current/previous generation files and brings up production. |
| `/srv/menhir/production/bin/release-run.sh` | Internal, root-only | Runs the maintenance transaction only after Menhir's canonical `deploy/personal_deploy.py` path binds rehearsal, owner approval, promotion attempt, operator wrapper, and this runner's digest. It is deliberately not an MCP tool, sudoers command, submit wrapper, or worker operation. |

The gateway cannot select a mutation script, path, generation, or argument.

## Safety model

- **Root-executed, read-only sudo.** The gateway may run only the five exact
  read wrappers listed in `sudoers.d/menhir-production`; `logs` alone accepts
  `--lines <int>`. `MENHIR_WRITE` authorization does not exist.
- **Local fixed argv.** The dedicated Linux gateway executes
  `sudo -n /srv/menhir/production/bin/<allowlisted-wrapper> ...` directly with
  `shell=False`. It refuses this path on Windows, bounds every call with a
  timeout, kills the POSIX process group on timeout, and redacts bounded output
  and errors. The generic PowerShell/SSH runner is never reachable from these
  tools.
- **Bridge-only listener.** The gateway binds only `172.30.0.1:8000`. The unit
  starts after Docker and `network-online.target`, applies
  `IPAddressDeny=any`, and allows only Caddy's fixed `172.30.0.2/32` peer. Caddy
  remains the external HTTPS boundary; no loopback, wildcard, or raw public
  listener is created.
- **OAuth discovery and exact resource.** `JWTVerifier` is composed inside
  FastMCP `RemoteAuthProvider`, which publishes protected-resource metadata for
  `https://memory.ctharvey.me/ops/mcp` and points clients to the Menhir OAuth
  issuer at the RFC 9728 path
  `/.well-known/oauth-protected-resource/ops/mcp`. Startup fails closed if the
  root-owned rendered policy drifts from
  the fixed issuer, `/ops` base URL, or exact MCP audience. Exact
  client/scope/tier/tool middleware checks remain authoritative.
- **Single mutation authority.** OAuth policy validation, tool registration,
  wrapper allowlisting, sudoers, the install census, and artifact verification
  all reject the former Yawn mutation lane. Canonical root-only Menhir scripts
  remain installed for the release/admission authority.
- **Persisted status.** Each operation writes a phase file to
  `jobs/<op>.status` (`enqueued` → `running` → `done`/`failed`). Read tools
  report the fence, lock holder, systemd unit/phase, persisted generation IDs,
  and current/previous candidate release from these files.
- **Immutable release record and receipts.** Canonical maintenance mutation is
  bound to root-owned release evidence and never to caller-supplied paths.
- **Artifact integrity.** `verify-artifacts` checks every root-executed
  artifact is owner 0, a regular non-symlink, not group/other-writable, and
  matches the sha256 recorded in `release.json`.
- **Backup metadata is not fabricated.** The backup record's `generation` is the
  value printed by `backup-generation.sh`, never a value the wrapper guesses.
- **Safe retrieval.** `logs` returns a bounded, validated integer of journal +
  compose log lines; `status`/`*_inspect` read only persisted state.

## Deployment (performed separately, as root)

```sh
python3 -m venv /srv/yawn/projects/yawn.vps/.venv
/srv/yawn/projects/yawn.vps/.venv/bin/python -m pip install --require-hashes -r /srv/yawn/projects/yawn.vps/requirements.menhir.txt
install -d -o root -g root -m 0755 /srv/menhir/production/bin
install -o root -g root -m 0644 bin/lib.sh        /srv/menhir/production/bin/lib.sh
install -o root -g root -m 0755 bin/verify-artifacts /srv/menhir/production/bin/verify-artifacts
install -o root -g root -m 0755 bin/recover          /srv/menhir/production/bin/recover
install -o root -g root -m 0755 bin/status bin/release-inspect bin/logs bin/backup-status bin/generation-inspect /srv/menhir/production/bin/
install -o root -g root -m 0644 etc/tmpfiles.d/menhir-production.conf /etc/tmpfiles.d/menhir-production.conf
install -o root -g root -m 0644 etc/logrotate.d/menhir-production    /etc/logrotate.d/menhir-production
install -o root -g root -m 0440 etc/sudoers.d/menhir-production      /etc/sudoers.d/menhir-production
groupadd --force menhir-operators
id -u yawn >/dev/null
usermod -a -G menhir-operators yawn
chown root:root /srv/yawn/projects/yawn.vps/menhir_server.py /srv/yawn/projects/yawn.vps/vps/oauth_policy.py /srv/yawn/projects/yawn.vps/vps/menhir_capabilities.py /srv/yawn/projects/yawn.vps/vps/menhir_tools.py
chmod 0644 /srv/yawn/projects/yawn.vps/menhir_server.py /srv/yawn/projects/yawn.vps/vps/oauth_policy.py /srv/yawn/projects/yawn.vps/vps/menhir_capabilities.py /srv/yawn/projects/yawn.vps/vps/menhir_tools.py
install -d -o root -g root -m 0755 /etc/yawn-vps /srv/menhir/production/release
install -o root -g root -m 0644 release/menhir-oauth-policy.json /etc/yawn-vps/menhir-oauth-policy.json
install -o root -g root -m 0644 release/menhir-oauth-public.pem /etc/yawn-vps/menhir-oauth-public.pem
install -o root -g root -m 0644 systemd/menhir-oauth-operations.service /etc/systemd/system/menhir-oauth-operations.service
systemctl daemon-reload
systemd-tmpfiles --create
# Create the immutable release record (root:root 0444) mapping each installed
# artifact path to its sha256, then verify:
install -o root -g root -m 0444 release/release.json /srv/menhir/production/release/release.json
/srv/menhir/production/bin/verify-artifacts
systemctl enable menhir-oauth-operations.service
systemctl start menhir-oauth-operations.service
# Install the reviewed scripts from Menhir's deploy/ directory to
# /srv/menhir/production/bin/{backup-generation.sh,restore-generation.sh,
# candidate-deploy.sh,candidate-accept.sh,promote.sh,rollback.sh,release-run.sh,
# release-lib.sh}; install its
# Dockerfile and docker-compose.production.yml under
# /srv/menhir/production/deploy/. Also install authority_digest.py,
# backup_cleanup_txn.py, validate_durable_inventory.py, menhir_schema.py,
# make_manifest.py, and mcp_acceptance_probe.py under the fixed bin root, and
# install durable-state-inventory.json plus installed-artifacts.json under the
# deploy root. Install the local encrypted-backup wrapper as
# /usr/local/sbin/menhir-backup-local. All executable
# scripts are root:root 0755; imported helpers are root:root 0644. The two
# release/menhir-oauth-* inputs above are outputs supplied to release-author as
# rendered.operations_policy_sha256 and rendered.oauth_public_key_sha256; they
# are not repository files and must contain no private key material.
```

## Unresolved assumptions

- **Gateway service account.** The dedicated unit is fixed to `User=yawn` and
  `Group=menhir-operators`; that account must exist and be a member of the
  sudoers-approved group before the unit starts. The unit intentionally omits
  `NoNewPrivileges` because it would prevent the fixed `sudo -n` wrappers from
  acquiring their narrowly approved root execution context.
- **Cross-repository installation.** The release bundle installs the exact
  Menhir census. Upgrades use Menhir's transactional installer to retire an old
  Yawn worker/template/submit-wrapper lane; do not delete those files manually.
- **Receipts and release record.** The fixed operation scripts must write the
  `backup`, `rehearsal`, `candidate`, and `previous-generation` receipts under
  `/var/lib/menhir-production/receipts/`, and the deployer must author
  `/srv/menhir/production/release/release.json` (root:root 0444) with the sha256 of each
  installed root-executed artifact. GNU `date`/`stat`/`sha256sum` and Python 3
  are assumed on the host.
- **Wrappers are regular files** (not symlinks) so sudoers exact-path matching
  is authoritative.
- **Fixed Docker bridge addressing.** The host must provide `172.30.0.1` as the
  gateway address and Caddy must use `172.30.0.2` on the production bridge.
  The service fails to bind or systemd drops traffic if that network contract
  is absent.
