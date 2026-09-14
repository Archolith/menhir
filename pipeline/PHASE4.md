# Phase 4 - host debris, itemised (2026-09-14)

> **Executed 2026-09-14 00:30 UTC.** Every DELETE/PRUNE row below was
> independently corroborated (read-only, Opus) against the live host before
> removal; all 24 CONFIRMED, with three corrections adopted: A3 widened to the
> whole `production/caddy/` (the `current` symlink would otherwise dangle);
> C8 `mutations/`, D3 `/srv/menhir/staging` and D4 `.menhir-stage-upload` are
> recreated on demand by `lib.sh` / `menhir_stage_vps.py` / `personal_stage.ps1`
> rather than orphaned (still safe to delete); B5 must be followed at once by a
> backup, because the scaffold audit and `stage-generation.sh` rehash every
> archive named in the current `backup-local-receipt.json`. Result: 22,094 MB
> freed; 5 archives kept, then a fresh backup (`generation.fn2qSMhcAP`,
> rehearsed, copied off-host) made it 6; `verify-artifacts` 0; audit green;
> recoverability YES.
>
> Found by the corroborator, **not removed** (uncorroborated as deletions):
> `/home/thron/menhir-0.2.0-13.tar` (417 MB image tarball, Sep 7) and four
> Sep-7 one-off scripts there; `/srv/menhir/production/bin/__pycache__/`
> (bytecode of the retired `verify_python_runtime.py` and a stale
> `menhir_schema` pyc); `/srv/menhir/backups/.neo4j-conf.50ra9gsj/` (60 B
> mktemp leftover); `/usr/local/sbin/menhir-backup-upload` (dead installed
> code with no caller, no unit, no config, not in `installed-artifacts.json`).
> Candidates for the next pass.

Read-only enumeration of 147.93.132.141 on 2026-09-14 00:xx UTC, after release
0.2.0-17. Every row has a verdict; nothing is removed until an independent
corroborator has checked each DELETE row against the live host. Live state
this phase must not touch: `/srv/menhir/production/{bin,deploy managed files,
ingress,policy,release/{release.json,production.env},secrets,state}`,
`/srv/menhir/backups/encrypted/` beyond the rows below, `/etc/menhir/`,
`/var/backups/menhir-install/active`, the current generation
(`current-generation` = `vJBZKqtqAF`; newest backup = `jpSjHqieHz`), and
anything Yawn-owned.

Verdicts: **DELETE** (no reader in installed code, no rollback value),
**KEEP** (still referenced, or rollback/retention value), **PRUNE** (retention).

## A. `/srv/menhir/production/` - stale copies beside the managed tree

| # | Path | Size | Dated | Evidence | Verdict |
|---|---|---|---|---|---|
| A1 | `production/deploy/` - the 33 files NOT listed in `installed-artifacts.json` (`.env.deploy.example`, `PRODUCTION.md`, `README.md`, `backup-generation.sh`, `build.sh`, `candidate-*.sh`, `client-policy.production.json`, `cloudflared-config.production.yml.example`, `docker-compose.{full,test}.yml`, `docker-compose.production.yml.bak-agent-smith-refresh-20260828`, `external-evidence-*.py`, `menhir-backup-upload-contabo.sh`, `production.env.example`, `promote.sh`, `release-author.py`, `release-lib.sh`, `release-validate.sh`, `release.json.example`, `restore-generation.sh`, `rollback.sh`, `secrets-map.sh`, `source-fence-author.py`, `lib/{authority_digest,backup_cleanup_txn,make_manifest,mcp_acceptance_probe,menhir_schema,restore_authority_txn,validate_durable_inventory,verify_wheelhouse}.py`) | ~430 KB | 2026-08-28 | The Aug-28 shadow copy of the repo's `deploy/`. The four managed files in the same directory (`Dockerfile`, `docker-compose.production.yml`, `durable-state-inventory.json`, `installed-artifacts.json`) are release artifacts and stay. Installed code references `production/deploy/` only for those four (`verify-artifacts`, `backup-generation.sh`, `restore-generation.sh`, `menhir_stage_vps.py`); an older wrapper once pointed `BACKUP_SCRIPT` at the shadow `backup-generation.sh` (HANDOFF s6, fixed). | DELETE the 33; keep the 4 and the directory |
| A2 | `production/ops-source/` (26 files, 200 KB) | 200 KB | 2026-08-28 | Copy of yawn.vps `ops/menhir` from Aug 28; the sources moved to `pipeline/` in 0.2.0-16 and the retired lane's units live here. No reference from `bin/`, `scaffold/bin/`, `/usr/local/sbin`, systemd or sudoers. | DELETE |
| A3 | `production/caddy/releases/adopted-current-20260828T020407Z-4c6e91c6ed0e/` | small | 2026-08-28 | Menhir's own Caddy release adoption from before ADR 0002; Caddy retired for Menhir in 0.2.0-14; no reference from installed code, compose or ingress. Yawn's Caddy stack lives elsewhere (`/srv/yawn`), untouched. | DELETE |
| A4 | `production/backups/` (300 files, 281 MB: `bootstrap-menhir-prod-0.2.0-{5,7,8,10}`, seven `*-20260828/29-*` experiment dirs) | 281 MB | 2026-08-28..09-05 | Bootstrap/experiment generations from before the release lane existed (MAP: "debris, but harmless"). Only reader of a `production/backups` string: none in `bin/*.sh`, `lib.sh`, `release-lib.sh`. Distinct from `/srv/menhir/backups/` (the real backup root). | DELETE |
| A5 | `production/release/{client-policy.failed-release.json, offline-image-ids.env, production.env.before-bc2113eb, production.env.before-policy-digest-fix}` | 29 KB | 2026-08-29..31 | Pre-release-lane leftovers beside the two managed files (`release.json`, `production.env`). Zero references in installed code. | DELETE |

## B. `/srv/menhir/backups/` - plaintext and retention

| # | Path | Size | Evidence | Verdict |
|---|---|---|---|---|
| B1 | `backups/candidate/generation.{AHnMOX447V,G5XsaIi1vF,G9CwejBuMV,OWdCoHE1gK,cQTQX0b8vx,iJchhFotA4,tlkF4qOKqr,u0wWCFHba4,vJBZKqtqAF}` | 13 GB | Rehearsal scratch roots left by manual `restore-generation.sh` runs (the nightly wrapper now deletes its own). Plaintext copies of the graph; the encrypted archive is the authority for each. `cQTQX0b8vx` is today's manual rehearsal; none is referenced by a receipt (rehearsal-receipt binds `jpSjHqieHz`, whose scratch the wrapper already removed). | DELETE all nine |
| B2 | `backups/decrypted/` same nine generations | 3.4 GB | Decrypted staging from the same runs; `stage-generation.sh` recreates on demand from the encrypted archive. | DELETE all nine |
| B3 | `backups/generations/generation.{KYjPwpW104,SIKJg3G9Xx}` | 309 MB | 2026-09-01 plaintext generations from the bootstrap era. `menhir-backup-local` and `menhir-backup-upload` mention `backups/generations` as the plaintext root they consume *during* a backup and then remove (`plaintext_removed`); these two are not the current generation (`vJBZKqtqAF`) and no receipt names them. | DELETE |
| B4 | `backups/in-place/` (304 MB), `backups/migration-20260828`, `backups/migration-pre-index-repair-20260828`, `backups/migration-verify-20260828`, `backups/migration-verify-pre-index-repair-20260828` (~1.9 GB), `backups/pre-agent-smith-20260828T044528Z` (124 KB) | ~2.2 GB | Aug-28 migration and the retired in-place lane; no reference from any installed script. | DELETE |
| B5 | `backups/encrypted/` - 15 archives, 4.4 GB; policy `RETENTION_TARGET_GENERATIONS=2` is recorded in receipts but nothing prunes | - | Desktop `C:\Users\thron\Backups\Menhir\` holds byte-identical copies (sha256 verified 2026-09-14) of 12 of the 15. Missing off-host: `T4fz7YKk8n` (first bootstrap snapshot), `cQTQX0b8vx`, `juvSYbdOcg`. Newest: `jpSjHqieHz`, `cQTQX0b8vx`. `vJBZKqtqAF` is the live `current-generation`. | PRUNE the 10 with verified desktop copies that are neither the newest two nor the live generation: `5zzC6Fj20c, AHnMOX447V, ErDx55L1kp, G5XsaIi1vF, G9CwejBuMV, OWdCoHE1gK, YBf6rSxgwW, iJchhFotA4, tlkF4qOKqr, u0wWCFHba4` (~3.1 GB). KEEP `jpSjHqieHz, cQTQX0b8vx, juvSYbdOcg, T4fz7YKk8n, vJBZKqtqAF`. Per-generation `backup-receipts/` entries stay (history). |

## C. `/var/lib/menhir-production/` - orphan markers (MARKERS.md D4)

| # | Path | Evidence | Verdict |
|---|---|---|---|
| C1 | `release-override.json` (Sep 7), `last-incident-recovery.json` (Sep 7) | 13's hand-promote route override and recovery record; no reader in installed code (MARKERS.md). | DELETE |
| C2 | `in-place-release.json`, `in-place-rollback.env`, `in-place-runtime.env` (Aug 31) | Retired in-place lane; no reader. | DELETE |
| C3 | `operator-release.env`, `operator-recovery.env`, `recovery.env`, `client-policy.before-operator-*.json` (x2), `production.env.before-operator-*` (x2) (Aug 31) | Aug-30/31 operator recovery episode copies; no reader. | DELETE |
| C4 | `oauth.before-operator-20260830T230115Z.db` (143 KB, Aug 31) | A second copy of the OAuth SQLite authority outside its one declared home (`durable-state-inventory`); root-only; no reader. | DELETE (securely: it holds token state) |
| C5 | `recovery-63/` (43 files, 592 KB), `hotfix/` (3 files) | Episode working dirs from Sep 4/7; no reader. | DELETE |
| C6 | `ingress-retirement/{Caddyfile.before-cloudflared-only, scaffold-contract.before-cloudflared-only.json}` | Rollback copies for the half-finished retirement; 14, 16 and 17 have landed and the contract has moved on twice. | DELETE |
| C7 | `abandoned/` (4 entries), `maintenance-history/` (4 journals + markers) | Scaffold lifecycle history written by `abandon-`/`complete-maintenance`. | KEEP (history) |
| C8 | `jobs/`, `mutations/`, `receipts/`, `backups/`, `plaintext-cleanup/`, `staging/` (all empty) | Empty; `tmpfiles.d` declares `jobs/`, `backups/`, `receipts/`; `plaintext-cleanup/` and `staging/` are runtime scratch for `menhir-backup-local` and `stage-generation.sh`; `mutations/` belongs to the retired submit lane. | KEEP all except `mutations/` (DELETE, empty) |
| C9 | `/run/lock/menhir-in-place-release.lock` (Aug 31) | Only lock with no reader; `menhir-release-run.lock` stays (`release-run.sh` is a required artifact). tmpfs, gone at reboot anyway. | DELETE |

## D. Transactions and staging

| # | Path | Evidence | Verdict |
|---|---|---|---|
| D1 | `/srv/menhir/staging-transactions/release-menhir-prod-0.2.0-{14,15,16}` | Staged bundles of installed releases; the installer copies the bundle into `/var/backups/menhir-install/active` as its rollback evidence, so the staging copy has no role after commit. | DELETE 14-16; KEEP 17 until 18 is installed |
| D2 | `/var/backups/menhir-install/menhir-prod-0.2.0-{11,12,13}.*` (7 dirs) | Pre-Phase-1 installer transaction dirs (the installer's own history is `history/`, 4.9 MB, 5 entries). Rollback evidence for releases that were superseded three times. | DELETE the seven; KEEP `active` and `history/` |
| D3 | `/srv/menhir/staging/` (empty, Sep 7) | Pre-scaffold staging dir; the scaffold contract names `staging-transactions`, not this. | DELETE |
| D4 | `/home/thron/.menhir-bootstrap.IiBFR6EV4m`, `.menhir-hotfix-20260907-{13,14}`, `.menhir-release-recovery-63`, `.menhir-secret-upload` (empty), `.menhir-stage-upload` (empty) | Operator upload scratch from Sep 1-7. | DELETE |
| D5 | `/home/thron/.menhir-backup-export/` | Staging used by `scripts/menhir-backup-archive.ps1` every night. | KEEP |
| D6 | `/home/thron/marker_inventory.py` | One-off inventory script; the result is `pipeline/MARKERS.md`. | DELETE |
| D7 | `/home/thron/apply_modes.py` | Used by every release install this session (applies manifest modes after scp). Belongs in the repo, not in a home dir. | KEEP for now; move into `pipeline/` in Phase 5 |

## Not candidates

`/srv/menhir/production/state/` (1.6 GB: the live Neo4j, OAuth and telemetry
data), `secrets/`, `ingress/`, `policy/`, `bin/`, `scaffold/`, the four managed
`deploy/` files, `/etc/menhir/`, `/etc/yawn-vps/` (empty directory, left
deliberately), `/var/backups/menhir-install/active` and `history/`,
`backup-receipts/` (15 receipts), the scaffold's own receipts and history.

## Order of operations

1. Corroboration (read-only) of every DELETE/PRUNE row by an independent reviewer.
2. Delete A, C, D rows (small, reversible only via the desktop clone of the repo for A1/A2).
3. Delete B1-B4 plaintext (large; nothing references it).
4. Prune B5 archives one by one, re-verifying the desktop digest immediately before each `rm`.
5. `verify-artifacts` exit 0, scaffold audit success, `backup-status` recoverability YES.
