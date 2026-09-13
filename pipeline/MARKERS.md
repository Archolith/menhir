# Durable control-plane state on the host — the v1 marker inventory

The architecture spec's protocol registry is exhaustive for **v2**, which is not
built. This inventories **v1**, which is what runs: every entry under
`/var/lib/menhir-production/` and every Menhir lock under `/run/lock/`, with its
writer, its clearer, its readers, and its state on 2026-09-13.

The registry has producer, consumer, digest and retention columns. It has no
clearer column, and it declares markers non-semantic. Both of those are why the
gaps below survived the design reviews: **a fact with a writer and no clearer is
a bug by definition**, and the registry cannot express that.

Method: enumerated the directory on the host, then traced each name through the
installed code (`/srv/menhir/production/bin`, `/srv/menhir/scaffold/bin`,
`/usr/local/sbin/menhir-backup-local`, the staged `release-install.sh`).
Heuristic line classification, then hand-verified for every row marked defect.

## Verdicts

| Class | Meaning |
|---|---|
| **DEFECT** | Written, never cleared, and something refuses on it |
| stale-after-success | Cleared on abandon, not on complete; accumulates but nothing refuses on it |
| latest-wins | Overwritten each cycle; no clearer needed |
| pointer | Maintained forward and back by its owner |
| history | Append-only by design |
| **orphan** | No reader anywhere in installed code |
| scratch | Empty working directory |

## Transaction markers

| Marker | Writer | Clearer | Readers | On host | Verdict |
|---|---|---|---|---|---|
| `first-mutation` | `release-lib.sh` `record_first_mutation`, called from `promote.sh` — the point of no return | **none** | `promote.sh` (must match generation), `rollback.sh` (refuses post-mutation rollback), `backup-generation.sh:203` (must match `current-generation`), scaffold `abandon-maintenance` (refuses if present) | Sep 7 20:15, `generation.vJBZKqtqAF` — 13's hand promote | **DEFECT.** After the first successful promote on a host, `abandon-maintenance` is impossible forever. It is blocking 14's rolled-back ceremony right now. |
| `release-run.json` | scaffold `hold-maintenance` (via `begin-maintenance`); stage advanced by `release-run.sh` / installer | scaffold `_archive_completed_maintenance` → `maintenance-history/` (only when a *next* `begin-maintenance` finds it `complete`); `abandon-maintenance` → `abandoned/` | `lib.sh:681`, `release-run.sh`, `release-install.sh`, scaffold `assert/complete`, `menhir_app_only.py` | 14, stage `start`, bound to a `release.json` that must change | Transactional. **Gap:** a rolled-back install leaves it neither completable (no success) nor abandonable (see above). |
| `candidate-generation` | `candidate-deploy.sh:82` | `abandon-maintenance` only | `promote.sh`, `lib.sh:253,656,664`, `release-run.sh` | Sep 7 (13's) | stale-after-success |
| `candidate-accepted` | `candidate-accept.sh:250` | `abandon-maintenance` only | `promote.sh:42`, `lib.sh:665`, `release-run.sh:144` | Sep 7 | stale-after-success |
| `candidate-prestart-authority.json` | `candidate-deploy.sh:51` | `abandon-maintenance` only | `candidate-accept.sh:83` | Sep 7 | stale-after-success |
| `candidate-accept-receipt.json` | `candidate-accept.sh:204` | `abandon-maintenance` only | `lib.sh:666`, `release-run.sh:143` | Sep 7 | stale-after-success |
| `restore-selection` | `stage-generation.sh:64` | `abandon-maintenance` only | `candidate-deploy.sh:10`, `lib.sh:33`, `release-run.sh:117`, worker | Sep 13, `u0wWCFHba4` | stale-after-success |
| `same-host-writer-fence-intent.json`, `same-host-writer-fence.json` | `same_host_fence.py` from `candidate-deploy.sh` / `promote.sh` | `abandon-maintenance` only | `candidate-deploy.sh:25`, `promote.sh:19`, `release-run.sh` | Sep 7 | stale-after-success |
| `fence` | `lib.sh` `fence_close` | `lib.sh` `fence_open` from `backup-generation.sh`, `promote.sh` | `backup-generation.sh`, `candidate-deploy.sh`, `promote.sh`, `release-run.sh`, `same-host-fence.sh` | Sep 5, 5 bytes | pointer — maintained |
| `current-generation` | `promote.sh`, `restore-generation.sh:482` | `restore-generation.sh:487-490` restores prior or removes on failure | `lib.sh`, `release-run.sh`, `restore-generation.sh:360`, `backup-generation.sh` | `vJBZKqtqAF` | pointer — maintained |

## Receipts

| Marker | Writer | Readers | Verdict |
|---|---|---|---|
| `backup-local-receipt.json` | `menhir-backup-local` | `lib.sh:295,636`, `stage-generation.sh`, `worker:153`, scaffold status (24 h freshness policy) | latest-wins |
| `backup-receipts/` (12) | `menhir-backup-local`, one per generation | `menhir-backup-local --resume-cleanup` | history |
| `rehearsal-receipt.json` | `restore-generation.sh:295,339` | `candidate-deploy.sh:17`, `lib.sh:296,644`, `release-run.sh:128`, scaffold status (must bind current backup generation) | latest-wins |
| `desktop-archive-receipt.json` | `menhir-backup-archive.ps1` on the desktop | `promote.sh:56`, `worker:149`, `release-run.sh:88`, scaffold | latest-wins |
| `scaffold-receipt.json` | scaffold `capture` | scaffold `verify`/`status` | latest-wins |
| `scaffold-restore-drill-receipt.json` | scaffold `seed-drill` / `record-backup-drill` | scaffold `status` | latest-wins |
| `durable-live-census.json` | `backup-generation.sh:128` via `validate_durable_inventory.py --live` | (binding checked at backup time) | latest-wins |
| `scheduled-backup-last-run.json`, `scheduled-backup-failure.json` | `/usr/local/sbin/menhir-scheduled-backup` (scaffold-installed 2026-09-13, nightly via `menhir-backup.timer`) | `backup-status` (release 0.2.0-15, `yawn.vps@bc4f29e`) | latest-wins; failure marker is `rm`'d on success |
| `maintenance-history/` (1) | scaffold `_archive_completed_maintenance` | — | history |
| `mutation-history/` (1) | `backup-generation.sh:204-215` — archives the *content* of `first-mutation` per generation, but never removes the marker | — | history |
| `abandoned/` (16) | scaffold `abandon-maintenance` | — | history |

## Orphans — no reader in any installed code

| Marker | Dated | What it appears to be |
|---|---|---|
| `release-override.json` | Sep 7 | 13's route override (postmortem: "the route override retained the already-serving route") |
| `last-incident-recovery.json` | Sep 7 | Recovery episode record |
| `in-place-release.json`, `in-place-rollback.env`, `in-place-runtime.env` | Aug 31 | The pre-release "in-place" lane |
| `operator-release.env`, `operator-recovery.env`, `recovery.env` | Aug 31 | Operator recovery episode |
| `client-policy.before-operator-*.json` (×2), `production.env.before-operator-*` (×2) | Aug 31 | Pre-change copies from that episode |
| `oauth.before-operator-20260830T230115Z.db` | Aug 31 | **A 143 KB copy of the OAuth SQLite authority** sitting in the status directory. Root-only mode, but it is a second copy of an authority the durable-state inventory says has exactly one home. |
| `recovery-63/` (43 files), `hotfix/` (3), `ingress-retirement/` (2 rollback copies) | Sep 7 / Sep 4 / Sep 8 | Episode directories. The ingress-retirement copies are the only rollback for the half-finished retirement and must stay until 14 lands. |
| `jobs/`, `mutations/`, `receipts/`, `backups/`, `plaintext-cleanup/`, `staging/` | — | scratch; `jobs/` belongs to the retired submit lane |

## Locks (`/run/lock/`)

| Lock | Owner | Verdict |
|---|---|---|
| `menhir-production.lock` | mutation lock; `backup-generation.sh`, `release-install.sh`, `restore-generation.sh` | maintained |
| `menhir-production-admission.lock` | scaffold admission holder unit | maintained |
| `menhir-release-run.lock` | `release-run.sh` | maintained |
| `menhir-scheduled-backup.lock` | `/usr/local/sbin/menhir-scheduled-backup` | maintained |
| `menhir-in-place-release.lock` | Aug 31 | **orphan** — the in-place lane no longer exists |

## The defects, and what closes them

**D1 — `first-mutation` has no clearer.** The marker records the point of no
return for *one* maintenance. It should be scoped to that maintenance: archived
with the journal when the maintenance completes, and moved with the journal on
abandon. Today it outlives every cycle, so the first completed promote on a host
permanently disables `abandon-maintenance`. Fix in `menhir_scaffold.py`:
`_archive_completed_maintenance` and `abandon_maintenance` both move the marker
into the archive next to the journal, and `abandon` refuses only when the marker
is *newer than the current journal* — i.e. this maintenance mutated — rather than
whenever it exists.

**D2 — a rolled-back install has no exit.** With D1 fixed, `abandon` becomes the
exit: the journal is at `start`, no mutation belongs to it. Without D1 there is
no legitimate exit at all, which is the state the host is in now.

**D3 — six markers are cleared on abandon but not on complete.** Every
successful cycle leaves its candidate, selection and fence markers behind for
the next cycle to find. Nothing refuses on them today because readers compare
them to the current generation, but that is luck, not design. Fix: `complete`
archives the same eight-marker set `abandon` does.

**D4 — fourteen orphans and one orphan lock**, including a second copy of the
OAuth database. None has a reader. Retention is a decision, not a default; the
`ingress-retirement/` copies stay until release 14 lands.

## The host repair this session needs

`first-mutation` on the host belongs to 13's cycle, which completed by hand and
whose journal was archived tonight into `maintenance-history/` by the first
`begin-maintenance`. Moving the marker into that same archive is what the
completion should have done. That is a hand edit of durable production state
and is not made without the owner's approval.
