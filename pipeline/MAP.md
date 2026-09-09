# Where Menhir deployment things actually live

Every fact here was verified against the running host on 2026-09-08, not read
from a config file. Where source and reality disagree, that is called out.

This document exists because deployment tooling and state are spread across four
repositories and six storage locations with no cross-references. In one session
an agent with full access concluded, wrongly and in this order, that: Menhir was
served by shared Caddy (it is served by Cloudflared), no backups were copied off
the host (nine are on the desktop), the decryption key existed only on the VPS
(it is also local), and no restore had been rehearsed (one had, successfully).
Each error was the same move — look in one plausible place, find nothing,
conclude absence. A map is cheaper than repeating that.

## The one question that matters

**If the VPS died right now, could the graph be recovered?**

As of 2026-09-08: **yes, from what already exists.** `generation.vJBZKqtqAF` is
backed up, rehearsed with `neo4j_check: ok` and `sqlite_integrity: ok`, verified
as a running candidate stack (`readyz`, `oauth_discovery`, `recall`,
`mutation_503` all ok), and copied to the desktop with a matching decryption key.

**But no NEW backup can currently be taken** - see the ingress retirement issue
below, which broke `submit_op`. The newest backup is from 2026-09-07 19:52 and
is ageing against a 24-hour policy. Restoring is fine; backing up is not.

## Live issue: the ingress retirement is half finished

Someone ran a Cloudflared-only ingress retirement directly on the host on
2026-09-08 between 00:31 and 00:50. Rollback copies were saved to
`/var/lib/menhir-production/ingress-retirement/`
(`Caddyfile.before-cloudflared-only`, `scaffold-contract.before-cloudflared-only.json`),
and `menhir-caddy-reconcile.path` / `.service` were stopped and removed at 00:33:05.

It was not completed, and the host is degraded as a result. Verified 2026-09-08:

```
verify-artifacts  ->  exit 1
  FAIL /etc/systemd/system/menhir-caddy-reconcile.path      missing
  FAIL /etc/systemd/system/menhir-caddy-reconcile.service   missing
  FAIL /srv/yawn/projects/yawn.deploy/Caddyfile             digest mismatch
```

What is still inconsistent:

- `release.json` still lists both removed units as required artifacts.
- `caddy-release.sh`, `caddy-route-apply` and `caddy-route-rollback` are still
  installed under `/srv/menhir/production/bin/` (2026-09-07 19:50).
- `/etc/sudoers.d/menhir-production` still grants `caddy-route-apply` and
  `caddy-route-rollback`.
- The Caddyfile was edited without cutting a release, hence the digest mismatch.

**The operationally serious one:** deployed `/srv/menhir/production/bin/lib.sh`
lines 505-506 launch every operation with

```
systemd-run --property=Requires=menhir-caddy-reconcile.service \
            --property=After=menhir-caddy-reconcile.service
```

That unit no longer exists. By systemd semantics every `submit_op` job -
`backup`, `promote`, `rollback`, `candidate-deploy`, `candidate-accept`,
`restore-production`, `restore-rehearsal` - cannot start. This has not been
confirmed by executing a submit, deliberately. The circumstantial fit is exact:
the last successful backup was 2026-09-07 19:52, before the 00:33 removal, so
nothing has been attempted since it broke.

**Do not delete the reconcile units' sources.** They are the tail of this
retirement, not dead code, and are the only maintained definitions available to
either finish it or restore the release to passing. An independent corroboration
disproved a proposal to delete them; the census records them as
`preserve / BLOCKED-ingress-retirement`.

To finish coherently, in one change: remove the scripts and `menhir-op@.service`
from the host, drop all six paths from `release.json` `artifacts` and
`installed-artifacts.json`, remove the sudoers grants, strip the `Requires=` /
`After=` properties from `lib.sh`, cut a release, and only then delete the
sources.

## Host state

| Path | Holds | Notes |
|---|---|---|
| `/var/lib/menhir-production/` | **All receipts and status markers.** `backup-local-receipt.json`, `rehearsal-receipt.json`, `desktop-archive-receipt.json`, `candidate-accept-receipt.json`, `scaffold-restore-drill-receipt.json`, `current-generation`, `mutation-history/`, `last-incident-recovery.json` | This is the real status directory. It is **not** under `/srv/menhir`. Looking for status under `/srv/menhir/production/status/` finds nothing; that path does not exist. |
| `/srv/menhir/production/` | Deployed code and state. Broken out below. | 777 files; 48 of them covered by `release.json`. Secrets subdirs: `cloudflare`, `menhir`, `neo4j`, `oauth`. The age key is **not** here. |
| `/srv/menhir/backups/` | `encrypted/` (10 `.tar.gz.age`, ~2.9G), `generations/` (plaintext), `decrypted/`, `candidate/`, `in-place/`, `migration-*` | `encrypted/` is the authoritative archive set. |
| `/etc/menhir/` | `backup-restore.agekey` (the age identity), `scaffold-contract.json` | 189 bytes. Without it every archive is noise. |

## Inside `/srv/menhir/production`

777 files. `release.json` lists 67 artifacts, 48 of them under this tree, so
**729 files on the host are outside release coverage.** Most of that is
legitimate — `state/` and `backups/` are runtime data and are supposed to be
unmanaged. The rest is not.

| Subtree | Files | Unmanaged | What it is |
|---|---|---|---|
| `state/` | 346 | 346 | Live runtime data. Correctly unmanaged. |
| `backups/` | 300 | 300 | 12 bootstrap/experiment generations, Aug 28 – Sep 5. Debris, but harmless. |
| `bin/` | 42 | 1 | The managed tree. The one exception is a stale `__pycache__/menhir_schema.cpython-312.pyc`. |
| `deploy/` | 33 | 33 | **Stale shadow tree**, mostly Aug 28. See below. |
| `ops-source/` | 26 | 26 | **A fourth copy of the yawn.vps ops tree, on the host, drifted.** See below. |
| `secrets/` | 10 | 10 | Correctly unmanaged. |
| `caddy/` | 6 | 6 | `releases/adopted-current-20260828T…` — dead Caddy config from the retired ingress path. |
| `release/` | 6 | 5 | `release.json` (the authority, cannot list itself) plus 4 rollback leftovers: `client-policy.failed-release.json`, `offline-image-ids.env`, `production.env.before-bc2113eb`, `production.env.before-policy-digest-fix`. |
| `ingress/` | 2 | 2 | **The live Cloudflared ingress. Unmanaged.** See below. |

### Three copies of the operator code, and they disagree

`bin/` is the managed tree. `deploy/` and `ops-source/` are unmanaged copies of
overlapping material. `ops-source/` is not a stale duplicate of `bin/` — it has
**drifted on the three most load-bearing files**:

```
lib.sh             bin=33713036   ops-source/bin=a610ae1b   DIFFERS
verify-artifacts   bin=4ad73f1f   ops-source/bin=9de2d267   DIFFERS
worker             bin=87148347   ops-source/bin=60df3d22   DIFFERS
backup-status      bin=b16c3ac9   ops-source/bin=b16c3ac9   same
status             bin=41e5957f   ops-source/bin=41e5957f   same
```

`lib.sh` owns `submit_op`. Anything that resolves its path by guess, or falls
back to `SCRIPT_DIR` the way `backup-generation.sh` does, can load a different
`lib.sh` than the one actually running.

### The live ingress is not in any release

Verified against the running container, not from config:

```
menhir-prod-cloudflared
  compose project : menhir-ingress          (NOT menhir-prod)
  config file     : /srv/menhir/production/ingress/docker-compose.cloudflared.yml
  mount           : /srv/menhir/production/ingress/cloudflared-config.yml
                      -> /etc/cloudflared/config.yml
  mount           : /srv/menhir/production/secrets/cloudflare/credentials.json
```

Both `ingress/` files are outside `release.json`. The sole route to
`memory.ctharvey.me` is therefore hand-placed, unverified by `verify-artifacts`,
and not reproducible from a release. The retirement moved ingress **from** a
managed artifact (the Caddyfile, still listed in `release.json`) **to** an
unmanaged one — which is a large part of why the retirement reads as
half-finished.

Note also that ingress is a separate compose project, so a backup that stops
`menhir-prod` leaves cloudflared up and serving errors. That is expected; the
outage is the documented cost of an offline Neo4j dump.

### Sudo is not restricted

`sudoers.d/menhir-production` is widely described, including in earlier versions
of this document, as granting five read-only commands. That is wrong twice:

- The grant itself includes mutating verbs — `backup`, `promote`, `rollback`,
  `restore-production --confirm`, `candidate-deploy`, `candidate-accept`,
  `caddy-route-apply`, `caddy-route-rollback`.
- It does not matter anyway, because `thron` also holds
  `(ALL : ALL) ALL` and `(ALL) NOPASSWD: ALL`.

The named grant is documentation of intent, not a control. Treat it that way
when reasoning about blast radius.

## Desktop state

| Path | Holds |
|---|---|
| `%USERPROFILE%\Backups\Menhir\` | 9 of the 10 encrypted archives, ~2.59G, including the newest. Written by `scripts/menhir-backup-archive.ps1`. |
| `IdeaProjects\.secrets\menhir\backup-restore.agekey` | The age identity. SHA-256 verified identical to the host copy. `.secrets/` is gitignored as of workspace commit `1d03980`. |

## Tooling, by repository

| Repository | Path | Contains |
|---|---|---|
| `menhir` | `deploy/` | Release compilers, staging, ansible, `backup-generation.sh`, `restore-generation.sh`, `scaffold/`, `personal_*` deploy scripts |
| `menhir` | `pipeline/` | The rebuilt pipeline. `census.py` and this map. New work goes here. |
| `yawn.vps` | `ops/menhir/bin/` | The operator read commands and `lib.sh`, which owns `submit_op` and the transaction verbs |
| `yawn.vps` | `ops/menhir/systemd/`, `etc/` | Unit definitions, `sudoers.d/menhir-production` (read commands only), tmpfiles |
| `IdeaProjects/scripts` | root | `deploy-menhir.ps1`, `deploy-menhir-app-only.ps1`, `menhir-scaffold.ps1`, `menhir-backup-archive.ps1`, and the generic `vps-ssh.ps1` / `vps-scp.ps1` / `vps-compose.ps1` |
| `yawn.deploy` | root | **Dead Menhir config only.** The `memory.ctharvey.me` vhost, `caddy-release.sh` and the Menhir lock are retired-in-place; see ADR 0002. |

`vps-ssh.ps1` runs any command on the production host and `vps-compose.ps1` runs
any docker compose args. Both are unbounded by construction. They are the reason
the census anchors on the target host rather than on command verbs.

## Which command answers which question

| Question | Command | Reality |
|---|---|---|
| Can I recover the graph? | `backup-status` | Rewritten 2026-09-08 to read the receipts. The previous version read only the job-runner file and reported "(no backup job recorded)" while a complete chain existed. |
| What is deployed? | `status` | Fence, lock, release record, generation IDs |
| Which generation is live? | `release-inspect` or `generation-inspect` | These overlap almost entirely and should be merged |
| Are installed artifacts intact? | `verify-artifacts` | Root only, not in sudoers |

Only these five are granted in `sudoers.d/menhir-production`, all read-only.

## Ingress

Cloudflared, verified running. `menhir-prod-cloudflared` holds tunnel
`c1e621a0-...` routing `memory.ctharvey.me` to `menhir-prod-app:8099`, everything
else 404. The `menhir-proxy` network contains only cloudflared and the app; the
shared Caddy container cannot resolve `menhir-prod-app`.

Two live facts contradict every version of the architecture spec:

- `/ops/mcp` is **not routed at all**. Nothing reaches the operations gateway at
  `172.30.0.1:8000`.
- `oauth/client-metadata/agent-smith.json` **is** routed and appears in no spec
  route table.

## Known gaps

- **No backup schedule.** No timer, no cron. Backups are deploy-triggered: six on
  Sep 1, one on Sep 5, three on Sep 7. A quiet week means no backup.
- **One archive not mirrored.** `generation.T4fz7YKk8n` (Aug 31) is on the host
  but not the desktop.
- **Tooling spans four repositories.** Consolidating into `menhir/pipeline/` is
  the intent; nothing has moved yet.
