# Menhir deployment — session handoff, 2026-09-08

Written to survive context compaction. Everything needed to resume is here or in
the two documents this one links. Read this first.

Companion documents in this directory:
- [`MAP.md`](MAP.md) — where every file and piece of state actually lives, verified against the host
- [`CONSOLIDATION.md`](CONSOLIDATION.md) — what moving the deploy code into `pipeline/` requires
- [`CENSUS.md`](CENSUS.md) — generated inventory of everything that can mutate the host

---

## 1. Start here: the single most important fact

**Menhir can be restored. Menhir cannot currently be backed up.**

The newest backup is `generation.vJBZKqtqAF`, 2026-09-07 19:52 UTC. It is
rehearsed, verified, and copied to the desktop with a matching key. Restoring
from it works.

But `submit_op` — the path every operation runs through — is broken, so no new
backup can be taken. The backup is ageing against a 24-hour freshness policy.
See section 3.

## 2. Why the last three release attempts failed

Promoting a release could not have succeeded. Two independent blockers, neither
of which looks like a release problem from the outside:

1. `verify-artifacts` exits 1 (three FAILs) so the installed state does not match
   `release.json`.
2. `submit_op` cannot start any job, so `promote`, `candidate-deploy` and
   `candidate-accept` never run.

Time spent debugging the release itself was spent in the wrong place.

## 3. The live problem: a half-finished ingress retirement

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
plus units in `pipeline/systemd/`. It has **two known bugs**, both mine, both
unfixed:
1. `stack_running()` omits `-p menhir-prod`, so it misreports the stack as
   stopped and its restart guarantee would not fire.
2. `BACKUP_SCRIPT` defaults to `${MENHIR_PROD_ROOT}/deploy/backup-generation.sh`
   — the stale shadow copy. The real one is `bin/backup-generation.sh`.

Do not install it until both are fixed. Note also that `Persistent=true` plus
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

**yawn.vps**: `6a98974` backup-status rewrite (local, unpushed at time of writing)

**workspace**: `1d03980` gitignore `.secrets/` and `*.agekey` — both backup keys
were untracked but not ignored in a repo that pushes to GitHub. Never committed;
history clean.

**Host changes: none.** Everything installed during the session was reverted;
verified clean.

## 9. Next actions, in order

1. **Finish the ingress retirement** (section 3). Fixes both the failing release
   and the broken backup path. Coordinated change across two repos and the host —
   have the plan corroborated before running it.
2. **Fix the two bugs in `scheduled-backup.sh`**, then install the timer.
3. **Deploy the rewritten `backup-status`** so the command tells the truth in an
   incident.
4. Delete the stale `/srv/menhir/production/deploy/` shadow tree.
5. Execute the 19 `replace` dispositions; consolidate per `CONSOLIDATION.md`.

Items 1 and 2 are the only ones affecting whether data can be protected today.
