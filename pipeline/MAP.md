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

As of 2026-09-08: **yes.** `generation.vJBZKqtqAF` is backed up, rehearsed with
`neo4j_check: ok` and `sqlite_integrity: ok`, verified as a running candidate
stack (`readyz`, `oauth_discovery`, `recall`, `mutation_503` all ok), and copied
to the desktop with a matching decryption key. `pipeline/backup-status` reports
this chain; nothing else does.

## Host state

| Path | Holds | Notes |
|---|---|---|
| `/var/lib/menhir-production/` | **All receipts and status markers.** `backup-local-receipt.json`, `rehearsal-receipt.json`, `desktop-archive-receipt.json`, `candidate-accept-receipt.json`, `scaffold-restore-drill-receipt.json`, `current-generation`, `mutation-history/`, `last-incident-recovery.json` | This is the real status directory. It is **not** under `/srv/menhir`. Looking for status under `/srv/menhir/production/status/` finds nothing; that path does not exist. |
| `/srv/menhir/production/` | Deployed code, `bin/` wrappers, `release/release.json`, `secrets/`, `deploy/` | Secrets subdirs: `cloudflare`, `menhir`, `neo4j`, `oauth`. The age key is **not** here. |
| `/srv/menhir/backups/` | `encrypted/` (10 `.tar.gz.age`, ~2.9G), `generations/` (plaintext), `decrypted/`, `candidate/`, `in-place/`, `migration-*` | `encrypted/` is the authoritative archive set. |
| `/etc/menhir/` | `backup-restore.agekey` (the age identity), `scaffold-contract.json` | 189 bytes. Without it every archive is noise. |

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
