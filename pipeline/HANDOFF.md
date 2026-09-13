# Menhir deployment — session handoff, 2026-09-08

Written to survive context compaction. Everything needed to resume is here or in
the two documents this one links. Read this first.

Companion documents in this directory:
- [`MAP.md`](MAP.md) — where every file and piece of state actually lives, verified against the host
- [`CONSOLIDATION.md`](CONSOLIDATION.md) — what moving the deploy code into `pipeline/` requires
- [`CENSUS.md`](CENSUS.md) — generated inventory of everything that can mutate the host

---

## 1. Start here: the single most important fact

**Menhir can be restored, from a backup taken 2026-09-13 (`generation.u0wWCFHba4`, rehearsed, off-host). Release 0.2.0-14 is installed and verified.**

Phase 0 is complete. The newest backup is `generation.YBf6rSxgwW`,
2026-09-10 02:05 UTC: `roundtrip_verified: true`, recipient
`age1v5azxda7…qdppg0k` (the same identity held on the desktop), 338 MB, plaintext
cleanup completed. Copied off-host to
`%USERPROFILE%\Backups\Menhir\generation.YBf6rSxgwW-20260910T020506Z-2578944e9011ab13.tar.gz.age`,
SHA-256 `6dbac32b75cd57d3c2467708b2b5a3e0dd9714448fdeed9f07ea32da6841a76b`,
with a `desktop-archive-receipt.json` on the host.

The live Cloudflared ingress is also captured now, byte-exact, in
`pipeline/ingress/` — previously it existed only on the host, in no release and
no backup, which meant a restore could bring the graph back with no route to it.

The previous newest, `generation.vJBZKqtqAF` of 2026-09-07 19:52, remains
rehearsed and verified.

`submit_op` is broken (section 3), so the `backup` wrapper cannot enqueue a job.
**This was previously recorded here as "Menhir cannot currently be backed up."
That was wrong.** Verified 2026-09-08: neither `bin/backup-generation.sh` nor
`bin/release-lib.sh` contains any reference to `caddy-reconcile` — zero matches.
The backup script takes the host lock itself, loads `production.env` itself, and
does not go through `systemd-run`. Running it directly as root works today,
without fixing the ingress first.

What is lost by bypassing `submit_op`: the fence bookkeeping, `reconcile_previous`,
and the phase markers `submit_op` would have written, so `status` will not show
the job. Serialization is still correct — the flock is taken by the script, not
the wrapper. `pipeline/scheduled-backup.sh` deliberately uses this direct path.

So the freshness gap is a decision, not a blocker.

## 2. Why the last three release attempts failed

Promoting a release could not have succeeded. Two independent blockers, neither
of which looks like a release problem from the outside:

1. `verify-artifacts` exits 1 (three FAILs) so the installed state does not match
   `release.json`.
2. `submit_op` cannot start any job, so `promote`, `candidate-deploy` and
   `candidate-accept` never run.

Time spent debugging the release itself was spent in the wrong place.

## 3. RESOLVED 2026-09-13 — release menhir-prod-0.2.0-14 is installed

`verify-artifacts` exit 0 (51 OK, Python runtime included). 50 artifacts,
`ingress_mode: cloudflared`. Every Caddy path, `worker`, `menhir-op@.service`
and the orphan `release-run` retired; both cloudflared ingress files managed.
`lib.sh` has zero `menhir-caddy-reconcile` references. The gateway runs under
its real `verify-artifacts` gate, `active`, zero restarts. Scaffold `static: ok`,
`maintenance_stage: complete`, audit `success`. Production healthy, `readyz=200`,
no restart during any install attempt — the installer takes no outage.

Three install attempts were needed; each failure was real and each fix is
committed: template units broke `systemctl show` (`872c5bb`); rollback tried to
stop a unit that never existed (`17df72d`); the Python runtime evidence reused
from r11 never matched this host (rebound to the live digest, third cut);
an evidence file picked up CRLF on Windows (64 bytes exactly). The maintenance
lifecycle itself had no exit for a rolled-back or artifact-only release
(`dd6f939`, `0ec8283`; see `MARKERS.md`).

What follows is the diagnosis as it stood before, kept for the record.

### The live problem as it was: a half-finished ingress retirement

On 2026-09-08 between 00:31 and 00:50 someone ran a Cloudflared-only ingress
retirement directly on the host. Rollback copies were saved first, to
`/var/lib/menhir-production/ingress-retirement/`. `menhir-caddy-reconcile.path`
and `.service` were stopped and removed at 00:33:05. The change was not finished.

Verified 2026-09-08:

```
verify-artifacts  ->  exit 1
  FAIL /etc/systemd/system/menhir-caddy-reconcile.path      missing
  FAIL /etc/systemd/system/menhir-caddy-reconcile.service   missing
  FAIL /srv/yawn/projects/yawn.deploy/Caddyfile             digest mismatch
```

Still inconsistent: `release.json` requires both removed units; the scripts
`caddy-release.sh`, `caddy-route-apply`, `caddy-route-rollback` remain installed
under `/srv/menhir/production/bin/`; `/etc/sudoers.d/menhir-production` still
grants the last two; the Caddyfile was edited without cutting a release.

**The operational break.** Deployed `/srv/menhir/production/bin/lib.sh:505-506`:

```
systemd-run --property=Requires=menhir-caddy-reconcile.service \
            --property=After=menhir-caddy-reconcile.service
```

That unit no longer exists, so by systemd semantics every `submit_op` job fails
to start: `backup`, `promote`, `rollback`, `candidate-deploy`, `candidate-accept`,
`restore-production`, `restore-rehearsal`.

Not confirmed by executing a submit — deliberately. Circumstantial fit is exact:
the last successful backup predates the 00:33 removal, so nothing has been
attempted since it broke.

**To finish coherently, as one coordinated change:** remove the three scripts and
`menhir-op@.service` from the host; drop all six paths from `release.json`
`artifacts` and `installed-artifacts.json`; remove the sudoers grants; strip the
`Requires=`/`After=` properties from `lib.sh`; cut a release; only then delete
the sources.

**Do not delete the reconcile unit sources before that.** A proposal to do so was
independently corroborated and disproved — they are the only maintained
definitions available to finish the retirement or restore the release.

## 4. Decisions made this session (owner-approved)

| Decision | Effect |
|---|---|
| Cloudflared is the sole ingress (ADR 0002, **corrected**) | Verified running. The shared Caddy vhost is dead config. |
| Cutover takes a maintenance window | Deletes the v1 bridge, handoff fence, cgroup census, caller drain, all-lock ordering |
| Deploys authorized by root ceremony, no signing key | Deletes the entire PKI: key custody, ACLs, passphrases, trust store, rotation, revocation |
| Nightly backup at 04:00 America/Chicago | Timer written, **not installed** — see section 6 |
| Publish stays rigorous; deploy does not | The organizing principle. See section 5. |

Together the first three delete roughly eight of the architecture review's
eighteen findings rather than fixing them.

## 4b. The rebuild-from-backup option (under consideration)

Owner is considering capturing the data and wiping the host tree rather than
untangling it. The sprawl that prompts this is real and measured: 777 files under
`/srv/menhir/production`, 48 covered by a release, three copies of the operator
code that disagree on `lib.sh`, `verify-artifacts` and `worker`. See
`MAP.md` → "Inside `/srv/menhir/production`".

**This is more viable than it looks, because the archive is already a rebuild
bundle, not just a data dump.** `bin/backup-generation.sh:317` does
`cp -a "${SECRETS_DIR}/." "${target}/secrets/"`, and the required-evidence list
(lines ~330-356) fails closed unless the generation also contains
`config/production.env`, `config/docker-compose.production.yml`,
`config/Dockerfile`, `config/release.json`, `config/durable-state-inventory.json`,
`config/commit.txt` and `policy/client-policy.json`. A verified generation is
therefore sufficient to reconstruct the running system.

**Two things are NOT in the archive and must be captured separately:**

1. `/srv/menhir/production/ingress/` — `docker-compose.cloudflared.yml` and
   `cloudflared-config.yml`. These are unmanaged, outside `release.json`, and
   outside the backup. They are the **only** route to `memory.ctharvey.me`.
   Wiping without them means the graph comes back and nothing can reach it.
   (`secrets/cloudflare/credentials.json` *is* in the archive, via the `cp -a`.)
2. `/etc/menhir/backup-restore.agekey` — correctly excluded, and already on the
   desktop, hash-verified. `/etc/menhir/scaffold-contract.json` is also outside
   the archive; confirm whether a rebuild needs it before relying on this.

**Sequencing that makes it safe.** Do not wipe and then rebuild. The rebuild path
is the exact thing that has never worked — three failed release attempts. Invert
it: `restore-rehearsal` already stands up a candidate stack from an archive and
has passed (`readyz`, `oauth_discovery`, `recall`, `mutation_503` all ok). So
stand the clean system up *beside* the current one, prove it serves, cut over,
and only then delete. That converts an irreversible wipe into a reversible
cutover, and it uses machinery that is known to work rather than machinery that
is known to fail.

Order: take a fresh backup by hand (see section 1) → capture `ingress/` →
rehearse → verify → cut over → delete the old tree.

## 5. The organizing insight

**Publish should be robust. Deploy should not.**

Publish answers "what code is this, and can I prove it" — a real question with a
real adversary, since the endpoint is public. Deterministic builds, digest
pinning, CI attestation, immutable content-addressed records. That half is
already built correctly and is **not** being replaced.

Deploy is moving a known-good artifact onto a box you own, as the only person
with the keys. Back up, swap, verify, roll back on failure, one lock. No PKI, no
fence, no transaction kernel.

The release record is the boundary. Publish makes it and makes it trustworthy;
deploy consumes it and asks one question — does the host match this record.

This explains the churn that preceded the session. Two seven-commit ratchets
exist in the branch history, each re-editing one file and adding exactly one more
identity field per review:

- staging identity, `deploy/personal_stage_vps.py`:
  `4cc70e4 8f7f1de 20a6587 a64f821 038aa08 8f4846a 5d212d1`
- authority, `deploy/personal_promote.ps1` + `scaffold/menhir_app_only.py`:
  `2fefd79 5b8db26 b63fe7a 4787ebb b94cc33 d3b7f2f 8489172`

The first is publish-grade verification re-derived at deploy time, one field per
review, forever. The second is deploy ceremony guarding against an attacker who
does not exist. Neither converges, because no schema declared the complete set of
facts that must be bound.

## 6. State of the work

**Census: complete.** 105 entry points, 105 classified, gate passes (exit 0).
86 preserve, 19 replace, 0 retire. Re-run with:

```
python pipeline/census.py --config pipeline/census.config.json \
  --dispositions pipeline/census.dispositions.json \
  --json-out pipeline/census.report.json --md-out pipeline/CENSUS.md \
  --require-classified
```

**`backup-status` rewritten** (`yawn.vps@6a98974`) to report real recoverability
including a coherence check across backup / rehearsal / off-host copy. Source
only, not deployed.

**Nightly backup timer written, NOT installed.** `pipeline/scheduled-backup.sh`
plus units in `pipeline/systemd/`. Three defects were found and **fixed**; all
three were mine. Verified against the host read-only, not just reasoned about:

1. **Stack detection always returned "stopped."** Diagnosed initially as a
   missing `-p menhir-prod`; that was wrong. The compose file carries a
   top-level `name:`, so the project resolves anyway. The real cause is that
   the compose file is variable-interpolated and unparseable without
   `--env-file` — `docker compose -f <file> ps` fails outright with
   `required variable NEO4J_IMAGE is missing a value`, and `production.env` is
   root-only mode 0400. With stderr discarded and piped to `grep -q .`, that
   error read as "stopped", which silently disabled the restart guarantee — the
   whole reason the wrapper exists. Now queries the daemon by compose project
   label, which needs no env file, and is tri-state: running / stopped /
   **unknown**, where unknown before the run aborts rather than guessing.
2. **`BACKUP_SCRIPT` pointed at the stale shadow copy** —
   `${MENHIR_PROD_ROOT}/deploy/backup-generation.sh`, 17502 bytes dated
   Aug 28, versus the managed `bin/` copy at 21698 bytes dated Sep 7. Now
   defaults to `bin/`, matching `lib.sh:40`.
3. **The wrapper took the lock its own child needs.** It held
   `/run/lock/menhir-production.lock` on fd 9, then invoked
   `backup-generation.sh`, which does its own `exec 9>` + `flock -n 9` on that
   same path (line 163) and exits 1 with "maintenance lock is held" when it
   cannot get it. Every scheduled run would have failed, every night, having
   never taken a backup. The wrapper now uses its own
   `/run/lock/menhir-scheduled-backup.lock`; host-wide serialization stays in
   the backup script where it already was.

Also changed: the restart path was a bare `docker compose up -d`, which would
have failed the same interpolation error as (1), and even given an env file
would have omitted the eight `MENHIR_*` runtime variables that `production_up`
in `release-lib.sh` supplies. It now sources `release-lib.sh` in a subshell and
calls `production_up`, the same path production uses, including `wait_healthy`.

**Run for real on 2026-09-10, and it found a fourth defect.** Executed as a
transient systemd unit against production. Sequence:

- Stack stopped, both dumps taken, consistency check passed, OAuth and telemetry
  authorities snapshotted, `Local generation complete and verified:
  generation.YBf6rSxgwW`.
- Then: `MENHIR_OPERATION_JOB_ID is required and invalid`, and
  `FATAL: encrypted local backup failed; stack is left stopped`.
- Wrapper logged `stack is stopped after backup (rc=1); restarting` and brought
  production back. App healthy, `readyz=200`. Outage ~3.5 minutes.

Defect 4: `/usr/local/sbin/menhir-backup-local` refuses to encrypt without a job
id matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`. `submit_op` mints it and the
worker exports it (`lib.sh` worker line 79); bypassing `submit_op` means nothing
does, so the run died *after* the stack was stopped, dumped and verified — the
most expensive place to fail. The wrapper now mints one in the same shape.

This corrects section 1's claim that the direct path "works today". It works up
to encryption; the job id is the missing piece, and it is now supplied.

**It also proved the wrapper's reason for existing.** The raw script left the
stack stopped exactly as documented. The restart guarantee — the thing defect 1
had silently disabled — fired and recovered production unattended. Had the timer
been installed before that fix, this would have been a 04:00 outage lasting until
someone noticed.

Recovery did not need a second outage: the generation was already verified, so
`menhir-backup-local` was run directly against it with a valid job id.

**Independently reviewed 2026-09-10 (opus, read-only).** Confirmed: the backup
and its receipt, the off-host copy (the reviewer went further and decrypted the
archive *header* with the desktop identity — a payload-chunk error rather than
"no identity matched" proves the key genuinely unlocks it), the ingress digests
four ways, production health, and the journal ordering. Measured outage from
container timestamps: 3 min 09 s, app down 2 min 28 s.

It also refuted two things and found eight further defects. Refuted: "host
changes: none" (see §8), and the implication that the *fixed* wrapper had been
proven — the copy that ran was the pre-fix one, so the job-id fix is verified as
code and by its consumer, never by execution.

Fixed in response:
1. **Exit code lied.** Backup succeeds, restart fails → the wrapper still
   `exit`ed `rc`=0. systemd would record success while the site was down, and
   the unit has no `OnFailure=`. Now any `restart-failed` forces rc=1.
2. **No signal trap.** The restart was straight-line code, so `systemctl stop`,
   a reboot, or an OOM kill during the ~2 minute window killed both wrapper and
   child and production stayed down — `docker compose stop` is an explicit stop,
   which `unless-stopped` deliberately does not undo. Now `trap … EXIT TERM INT`.
3. **Any-of stack detection.** Non-empty label query counted as "running" even
   if only neo4j came back. Becomes a permanent false positive once the ingress
   retirement folds cloudflared into `menhir-prod`. Now asserts both containers.
4. **Early aborts wrote no record**, making a wrapper that fails nightly
   indistinguishable from one never scheduled. Now every path records.
5. **`ExecStart` pointed at `/srv/menhir/production/pipeline/`**, which no
   release creates and which does not exist — every run would have been
   `203/EXEC`. Now `/usr/local/sbin/menhir-scheduled-backup`, with the install
   command in the unit and the script header.
6. **`restart_stack` discarded stderr** on the one path that ends in "manual
   recovery required". Now only stdout is dropped.
7. **`elapsed_seconds` excluded the restart**, understating this real incident
   by 40% (115 s recorded, 190 s actual). Now measured at finalize.
8. **Header overclaimed** that failures surface in `backup-status`. They do not:
   the deployed `backup-status` reads one job file and nothing anywhere
   references `scheduled-backup`. Claim removed pending deployment of
   `yawn.vps@6a98974`.

Still open from that review: retention says target 2 while holding 11 archives
(~3.2 G); `validate-receipt-binding` never ran for this generation because
`backup-generation.sh` exited before it, and cannot be re-run now that the
plaintext generation is deleted; and `menhir-scaffold-audit.service` has been
**failed since 2026-09-09** with "VPS backup is stale" — it is the only automated
staleness detector and it is itself down. Whether the new backup satisfies it
should be checked after its next run.

Note also that `Persistent=true` plus
`systemctl enable --now` fires the timer immediately on a never-run timer; that
happened once this session and triggered an unintended production backup attempt.

**Architecture spec:** all eight review issues closed in text, none independently
reviewed. An independent review returned `ARCHITECTURE NEEDS REVISION` with 0 P0,
10 P1, 8 P2 — report at `.agent/reviews/menhir-deployment-control-plane-architecture-review-2026-09-08.md`.
Its judgement on proportionality: ceremony disproportionate for the control
plane, warranted for the public data plane, applied uniformly to both. It
proposed ~7 gates rather than 14 phases.

## 7. Traps — things that were got wrong, so they are not repeated

Five wrong conclusions were reached this session, all the same move: look in one
plausible place, find nothing, report absence. In order:

1. **Ingress.** Read the Caddyfile, concluded shared Caddy served Menhir. It is
   Cloudflared. Config describes intent; only the running host describes state.
2. **Off-host backups.** Checked the VPS, said nothing copied backups off. Nine
   archives were already on the desktop.
3. **The key.** Said the age key existed only on the host. It is also at
   `IdeaProjects\.secrets\menhir\backup-restore.agekey`, hash-verified identical.
4. **Restore rehearsal.** Said none had run. Receipts are in
   `/var/lib/menhir-production/`, not `/srv/menhir/production/status/`, which
   does not exist.
5. **The retire proposal.** Claimed Ansible had already deleted the caddy
   scripts. That role is on an unmerged branch; the scripts are installed.

Concrete traps:

- **Two trees.** `/srv/menhir/production/bin/` is managed and verified.
  `/srv/menhir/production/deploy/` is a stale unmanaged shadow: 37 files, 33 not
  covered by `verify-artifacts`, most dated 2026-08-28. Anything resolving a path
  by guess lands on ten-day-old code.
- **`backup-status` before the rewrite** reports `(no backup job recorded)` while
  a complete backup chain exists. It read the job-runner file, not the receipts.
- **`verify-artifacts` only checks paths listed in `release.json`.** Absence from
  that list is invisible to it, which is why the shadow tree passes unnoticed.
- **Four PowerShell wrappers exist, not three.** The fourth,
  `menhir-backup-archive.ps1`, is load-bearing — it is what puts archives on the
  desktop. It also pipes base64 Python into `sudo -n python3 -`.
- **`vps-ssh.ps1` / `vps-compose.ps1`** run arbitrary commands on the production
  host. They are why the census anchors on the target host, not command verbs.

## 8. Commit record

All pushed. Branch `fix/deployment-reliability-20260907`, in sync with origin.

**menhir** (`4cdf8ab..adbc602`, 18 commits): `2a51408` preserve unaccepted spec
revision · `c89f522` ADR 0002 · `88ae245` apply ADR to spec+plan · `70e8ee6`
reconcile plan · `fc839e3` verified circling mechanism · `0c427ef` fresh
architecture review · `4589bc7` census · `3cd55e4` target-anchored census ·
`7a417c8` **correct ADR 0002** · `4c0adc8` MAP · `e455c22` backup timer ·
`c29f3c0` 04:00 America/Chicago · `d9ded95` CONSOLIDATION · `bf041e4` `a410dca`
`3edd573` `aa4215d` census batches · `adbc602` correct disproved retires

**yawn.vps**: `6a98974` backup-status rewrite — **pushed**, and the push was not
clean. The branch was `[ahead 3]` and was pushed without checking, so it also
sent `585f0ff` "retire unapproved release lane" and `b1191b8` "make Menhir
gateway read-only" — two pre-existing local commits that are not this session's
work and were not reviewed here. Four unrelated modified files
(`vps/admin_api.py`, `vps/sealed_contents_tools.py`, `vps/sealed_promo_tools.py`,
`tests/test_sealed_admin_api.py`) remain uncommitted and were not pushed.
Same error class as the timer install: acting on a repo without verifying its
prior state.

**workspace**: `1d03980` gitignore `.secrets/` and `*.agekey` — both backup keys
were untracked but not ignored in a repo that pushes to GitHub. Never committed;
history clean.

**Host changes.** This line previously read "none — everything installed during
the session was reverted; verified clean." An independent review refuted that.
Current state after Phase 0:

- `/var/lib/menhir-production/backup-local-receipt.json` — updated (new backup).
- `/var/lib/menhir-production/desktop-archive-receipt.json` — updated.
- `/var/lib/menhir-production/scheduled-backup-{failure,last-run}.json` — new.
  The failure marker is only cleared by a *successful* wrapper run, and no timer
  is installed, so it persists until then.
- `/srv/menhir/backups/encrypted/generation.YBf6rSxgwW-…age` — the new backup.
- `/home/thron/.menhir-backup-export/` — created (empty), staging used by
  `menhir-backup-archive.ps1`.
- `/tmp/menhir-scheduled-backup.sh` — a staged pre-fix copy of the wrapper was
  left executable on the host. **Removed 2026-09-10.** It was the only copy on
  the host, looked like "the backup wrapper", and re-running it would have
  reproduced the job-id failure and another real outage.

No release-managed artifact was modified.

## 9. Next actions, in order

1. ~~Finish the ingress retirement~~ **Done 2026-09-13.** See section 3.
2. **Install the timer.** The three wrapper defects are fixed (section 6); it
   has never been run end-to-end, and cannot be until item 1 lands.
3. **Deploy the rewritten `backup-status`** so the command tells the truth in an
   incident.
4. Delete the stale `/srv/menhir/production/deploy/` shadow tree.
5. Execute the 19 `replace` dispositions; consolidate per `CONSOLIDATION.md`.

Items 1 and 2 are the only ones affecting whether data can be protected today.
