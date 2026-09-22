# Menhir release and deployment runbook

Two processes, one hand-off. **Release** happens off-host and is rigorous: it
produces one digest-bound bundle after an independent review. **Deploy** happens
on the host and is mechanical: it checks digests and refuses, never judges. Every
command here ran for releases 0.2.0-14 through 0.2.0-18 (2026-09-13/14); nothing
below is theoretical. For rebuilt images, see the caveat at the end.

Where things live: the menhir checkout of record is
`C:\Users\thron\IdeaProjects\projects\archolith\menhir` on `main` (the deployment
branch was merged 2026-09-14); release workspaces stay at
`C:\Users\thron\Documents\Codex\2026-09-06\inve\work\releases\menhir-prod-0.2.0-N\`
(not in git; each release's `prior_release` points at the previous one); the
desktop helpers are `C:\Users\thron\IdeaProjects\scripts\`; the host is
`147.93.132.141`, operator login `thron` with `sudo -n`, no root login.

## 0. Is this a release or a scaffold change?

| Change to | Lane | Review |
|---|---|---|
| anything a release installs: `deploy/artifact-authority.json` and the files it names (`pipeline/bin/*`, `pipeline/etc/*`, `deploy/*.sh`, `deploy/lib/*.py`, compose, Dockerfile, policy, ingress) | **release** (this runbook, sections 1-4) | independent focused-delta security review, bound into the record |
| the host scaffold: `deploy/scaffold/*`, `pipeline/scheduled-backup.sh`, `pipeline/systemd/menhir-backup.*` | **scaffold** (section 5) | none by mechanism; ask for one for anything that runs as root nightly |
| a Menhir image (application code) | release with `image_provenance: rebuilt` | not exercised since 0.2.0-13; see the end |

To add, move or retire an installed artifact: edit `deploy/artifact-authority.json`,
run `python deploy/lib/artifact_authority.py --write`, commit the authority
together with the three rendered files it changed. The tests refuse anything else.

## 1. Before cutting a release

- Commit and push every change; `prepare` refuses a dirty or unpushed checkout
  of any of the three repositories (`menhir`, `archolith_oauth`, `yawn_deploy`).
- Write one fragment in `deploy/changes/unreleased/<id>.json` (schema in
  `deploy/changes/README.md`; copy a recent one from `deploy/changes/releases/`).
  It must claim at least one menhir commit newer than the prior release's
  `repos.menhir`; commits it claims must be ancestors of HEAD. Commit the fragment.
- Run the suites that touch what changed. The whole release set:
  `tests/test_artifact_authority_coherence.py tests/test_deployment_contracts.py
  tests/test_release_spec.py tests/test_release_author.py tests/test_release_flow.py
  tests/test_install_bundle_builder.py` (about 12 minutes).

## 2. Release (off-host)

Copy the previous release's `release-inputs.json` into a new
`releases\menhir-prod-0.2.0-N\` and change exactly: `release_id`, `prior_release`
(the previous workspace's `release.json`), `release_workspace_root`. Keep
`image_provenance: inherited` and the same `images` unless you rebuilt. Create the
empty `workspace\` directory.

```powershell
cd C:\Users\thron\IdeaProjects\projects\archolith\menhir
$R = 'C:\Users\thron\Documents\Codex\2026-09-06\inve\work\releases'
python deploy/release_flow.py next-id --prior-release "$R\menhir-prod-0.2.0-<N-1>\workspace\release.json"
python deploy/release_flow.py prepare --inputs "$R\menhir-prod-0.2.0-N\release-inputs.json" --workspace "$R\menhir-prod-0.2.0-N\workspace"
```

`prepare` ends at phase `review_requested` and writes
`workspace\security-review-request.json` (its `authority_sha256` is what the review
binds). Refusals name the cause; the ones seen so far: no fragment, dirty or
unpushed repo, fragment commit out of range, prior release stale.

**Review.** An independent reviewer (an Opus subagent has done every one since 14)
writes `security-review-report.md` in the release directory: recompute the
authority with `deploy/lib/menhir_schema.py release_authority_sha256`, diff the
artifacts map against the prior record, re-derive every git artifact from the
repo at the pinned commit, review the changed files, cover the eight scopes, give
a verdict. The brief is `deploy/SECURITY_REVIEW_PROMPT.md`; give the reviewer the prior
report to follow.
The release author must not write the review. Then bind it:

```python
# security-review.json next to the report; reviewer must differ from release_author
{"schema": 1, "kind": "menhir-production-security-review",
 "review_id": "menhir-prod-0.2.0-N-focused-delta-security",
 "release_author": "claude-release-operator",
 "reviewer": "claude-opus-5-focused-delta-reviewer",
 "reviewed_utc": "<now, YYYY-MM-DDTHH:MM:SSZ>",
 "authority_sha256": "<security-review-request.json authority_sha256>",
 "verdict": "APPROVED", "unresolved_findings": {"critical": 0, "high": 0},
 "scope": [the eight scopes], "report_sha256": "<sha256 of the report file>"}
```

```powershell
python deploy/release_flow.py finalize --workspace "$R\menhir-prod-0.2.0-N\workspace" --security-review "$R\menhir-prod-0.2.0-N\security-review.json"
python deploy/release_flow.py publish  --workspace "$R\menhir-prod-0.2.0-N\workspace" --confirm-release-id menhir-prod-0.2.0-N
```

`publish` archives the fragment(s) into `deploy/changes/releases/menhir-prod-0.2.0-N/`
with a publication receipt; commit that directory (its bytes are digest-bound;
`.gitattributes` keeps them `-text`). The bundle is `workspace\install-bundle\`.

## 3. Deploy (host)

```powershell
C:\Users\thron\IdeaProjects\scripts\menhir-release-install.ps1 -Workspace "$R\menhir-prod-0.2.0-N" [-RunBackup]
```

It reads every binding value from the workspace (refusing unless the flow is
`published` and the review is APPROVED for that authority), uploads the bundle,
and runs `pipeline/release-ceremony.sh` on the host: stage root-owned with
manifest modes restored (`pipeline/apply_modes.py` — scp drops modes),
`begin-maintenance`, `release-install.sh` as a oneshot unit, `verify-artifacts`,
live-digest and readyz checks, `complete-maintenance --artifact-only`, scaffold
audit. The installer takes no outage and rolls back on any failure; a refused or
rolled-back install leaves the maintenance journal open at `start` — rerun after
fixing the cause, or `menhir_scaffold.py abandon-maintenance --reason ...`.

**The audit goes red after every artifact-only release** until a rehearsal has
run under the new release, because the rehearsal receipt binds the release
manifest. The nightly backup (04:00 America/Chicago) rehearses and clears it;
`-RunBackup` does it immediately (about 3 minutes of downtime).

Afterwards: commit `deploy/changes/releases/menhir-prod-0.2.0-N/`, note the
release in `pipeline/HANDOFF.md`, and run
`C:\Users\thron\IdeaProjects\scripts\menhir-backup-archive.ps1` if a backup was
taken so the off-host copy exists (`backup-status` should say `YES`).

## 4. Checking the host at any time

```
sudo -n /srv/menhir/production/bin/verify-artifacts        # exit 0, 40 OK
sudo -n /srv/menhir/production/bin/backup-status            # recoverability + last nightly run
sudo -n /srv/menhir/scaffold/bin/menhir_scaffold.py status  # contract, evidence, maintenance stage
systemctl list-timers menhir-backup.timer menhir-scaffold-audit.timer
sudo -n cat /var/lib/menhir-production/release-run.json     # maintenance journal
```

## 5. Scaffold changes

The scaffold (contract, `menhir_scaffold.py`, audit units, the nightly backup
wrapper and its units) is installed by its own transactional installer, not by a
release. The desktop updater is `scripts\menhir-scaffold.ps1 -Mode Install
-PrivilegedSudo -SourceRoot <menhir checkout>`. It used to throw away the
installer's failure message and report only an exit code; since 2026-09-20 it
echoes the output and quotes the tail in the error, but no scaffold install has
run through it since that change. The direct path below is what every scaffold
install since 14 actually used: build the bundle (the 12 files in
`Get-ScaffoldSourceMap` plus a `bundle-manifest.json` of their sha256s), scp it
to `/home/thron/.menhir-scaffold-upload/scaffold-<32hex>`, then on the host

```
sudo -n cp -a --no-preserve=ownership /home/thron/.menhir-scaffold-upload/scaffold-<id> /srv/menhir/scaffold-transactions/scaffold-<id>
sudo -n bash /srv/menhir/scaffold-transactions/scaffold-<id>/install.sh --transaction-id <id> > /tmp/scaffold.log 2>&1; echo $?
grep -E 'committed|failed during|REFUSED' /tmp/scaffold.log
```

It refuses under a held admission, when any contract unit is not cleanly
`active`/`inactive`, and at `seed-drill` when the rehearsal receipt does not bind
the current backup (run a backup first). Rolled-back transactions are archived
under `/srv/menhir/scaffold-transactions/history/`. A contract change that drops
a unit pin must be installed **before** the release that retires the unit.

## 6. Nightly backup

`menhir-backup.timer` -> `/usr/local/sbin/menhir-scheduled-backup` at 04:00
America/Chicago: quiesced backup (about 3 minutes down), restart, then stage and
rehearse the new generation into scratch and clean up. Markers:
`/var/lib/menhir-production/scheduled-backup-last-run.json` (every run) and
`scheduled-backup-failure.json` (until a later run succeeds); `backup-status`
prints both. The desktop task copies the newest archive off-host at 00:30 local.
Nothing prunes encrypted archives; prune by hand only with a verified desktop
copy and never the newest two or the live generation (`pipeline/PHASE4.md`).

## 7. Symptom: 503 "Unable to fetch OAuth JWKS"

A client call fails with `503 Service Unavailable`, code `server_error`, detail
`Unable to fetch OAuth JWKS`, and a `request_id`. Why: the app verifies its own
tokens by fetching `MENHIR_OAUTH_JWKS_URI`, which is the *public*
`https://memory.ctharvey.me/.well-known/jwks.json`. That request leaves the host,
goes through Cloudflare and back in through `menhir-prod-cloudflared`. Keys are
cached for `oauth_jwks_cache_ttl_s` (300 s); the first request after expiry
refetches (5 s timeout) and fails outright if that one fetch fails, even though
the cached keys are still valid.

Check, from the desktop (`scripts\vps-ssh.ps1` logs in as `thron`):

```
.\scripts\vps-ssh.ps1 'docker logs --since 6h menhir-prod-app 2>&1 | grep -E "OAuth JWKS fetch failed|OAuth server_error|\" 503"'
.\scripts\vps-ssh.ps1 'docker logs --since 6h menhir-prod-app 2>&1 | grep "jwks.json HTTP"'   # self-fetches that arrived
.\scripts\vps-ssh.ps1 'docker logs --since 6h menhir-prod-cloudflared 2>&1 | grep ERR'
curl -s https://memory.ctharvey.me/readyz      # oauth_jwks.refresh_failures / last_failure_*
```

- `OAuth server_error -> 503: request_id=<id>` matches the id the client got;
  the `OAuth JWKS fetch failed: kind=...` line just before it says why (timeout,
  DNS, connect, HTTP status, malformed). Both lines, and the `/readyz`
  `oauth_jwks` block, exist only in releases built after 2026-09-22; on older
  images you have only the access log and must match the 503 by timestamp.
- No `jwks.json` GET reaching the app at that moment means the self-fetch was
  lost between the app, Cloudflare and the tunnel, not in the app.
- `/readyz` stays `ready` regardless; the counters are per process and reset on
  restart.

One isolated occurrence with healthy `/readyz` is a transient network blip; the
client can retry. Repeated occurrences mean the Cloudflare/tunnel path is
unhealthy. First observed 2026-09-22 02:31:32 UTC (one 503 in 30 h, no tunnel
error; five in-container refetches afterwards took 0.13-0.31 s). The durable
fixes (keep cached keys on refresh failure; verify against local keys instead
of the public URL) are not implemented.

## Not covered: rebuilt images

Releases 14-18 all inherited the 0.2.0-13 image. A rebuilt image needs
`image_provenance: rebuilt`, the six attestation evidence inputs, and
`validate_image_publication`; the last such release (13) was hand-installed, so
that path has not been run end to end by this machinery. `RELEASE_AUTOMATION.md`
describes it; treat it as unverified until it has been.
