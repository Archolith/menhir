# Phase 1 — retire Caddy for Menhir only

Change list. **Nothing here has been executed.** Owner decision 2026-09-10:
*"retire caddy only for menhir, yawn still uses it."*

> ## CORRECTED 2026-09-10 after independent corroboration
>
> **Group C below was wrong and has been withdrawn.** It rested on the claim
> that the mutation-lane retirement was accidental overreach and that "no
> replacement exists". Both are false:
>
> - The `obsolete` set was **not** added by `585f0ff`. `git log -S'obsolete = {'`
>   returns exactly one commit: `b1191b8` "fix(ops): make Menhir gateway
>   read-only" — a deliberate 13-file change that also removed the
>   `Cmnd_Alias MENHIR_WRITE` from sudoers, stripped 97 lines of mutation tools
>   from `vps/menhir_tools.py`, and updated three test files. `585f0ff` touched
>   `verify-artifacts` with a one-line diff removing `release-run` only.
> - `ops/menhir/README.md`, rewritten by that same commit, states the intent
>   outright: *"Retired gateway lane: menhir-op@.service, bin/worker, and the
>   public submit wrappers are source-history only and must not be installed."*
> - **The successor lane exists and is installed**: `deploy/release-run.sh`
>   calls the canonical scripts directly at `:237` (stage), `:250`
>   (candidate-deploy), `:260` (candidate-accept), `:272` (promote), all of
>   which are in `required`. Backups go via `release-install.sh:1028-1034` and
>   the `pipeline/` timer.
> - `submit_op` has **no live caller**. Its nine wrappers are in no authority.
>
> Restoring `release-run` to `required` would also arm a fence lockout:
> `submit_op` → `fence_close` (lib.sh:494) → worker rejects the unknown op
> (`return 2`) → no completion reconciler (lib.sh:692) → `recover` refuses. The
> fence stays closed with no recovery path.
>
> **The owner's decision stands for Groups A and B, which are verified safe.**
> Group C was this document's own proposal, not the owner's, and it is dropped.
>
> **Corrected target: `required` = 48 + 2 ingress = 50. `obsolete` = 14**
> (`retired_caddy_*` 5 ∪ `retired_gateway_*` 9), **or 15 including
> `bin/release-run`**, so the verifier matches the installer exactly.
>
> Two further corrections, both fatal on their own:
>
> - **A missed authority.** `menhir/deploy/release-install.sh` holds an `allowed`
>   frozenset (`:26-75`, 48 paths) enforced with `SystemExit` at `:136-137` and
>   `:186-187`, plus `retired_caddy_*` / `retired_gateway_*` arrays
>   (`:334-358`). Editing only the four files listed below produces an
>   un-installable release. It fails before the lock, so it fails safe — but if
>   `allowed` is fixed and the retirement arrays are not, the install fails at
>   `verify_obsolete_writers_retired` (`:1057`) **after** stopping the gateway
>   and taking the backup outage, then rolls back. A wasted window with a real
>   outage in it.
> - **Step 5 is deleted.** Running `backup` through the submit lane is not an
>   enqueue check: it runs immediately, stops production, has `op_timeout` 21600s,
>   and that lane has none of the restart protection `pipeline/scheduled-backup.sh`
>   provides. On failure `memory.ctharvey.me` stays down until a human notices.
>   Assert the `lib.sh` fix statically instead:
>   `grep -c menhir-caddy-reconcile ops/menhir/bin/lib.sh` → 0.
>
> **Live hazard found in passing:** `recover` gates fence reopening on
> `verify-artifacts` exiting 0 (`lib.sh:1012`), and it currently exits 1. If the
> maintenance fence closes today, root cannot reopen it. Fixing the three FAILs
> is what clears this.
>
> Six more files need editing (`release-install.sh` plus five test files), and
> `menhir/tests/test_ansible_host_state.py:466` is **already broken** on this
> branch — it does `installer.index("retire_caddy_writers\n")` for a function
> since renamed `retire_obsolete_writers`, an unconditional `ValueError`. So
> "the tests pass" is not currently a usable pre-flight signal, which matters
> because those tests are the only off-host detection for the missed authority.
>
> **There is no cross-repo check at all.** Nothing compares yawn.vps's `required`
> against menhir's `installed-artifacts.json`. Step 2 below is aspirational until
> such a check is written into a test in one of the two repos.

## Why the last three release attempts failed

`verify-artifacts` enforces a three-way equality and exits 1 on any divergence:

1. a hardcoded `required` set inside `verify-artifacts` itself,
2. `installed-artifacts.json` `destinations`,
3. the keys of `release.json` `artifacts`.

Retiring anything means editing all three in lockstep — and the verifier is
itself a release artifact, so it changes in the same release it validates.

The source is currently **incoherent with itself**. `ops/menhir/bin/verify-artifacts`
gained an `obsolete` set (commit `585f0ff`, "retire unapproved release lane")
that declares `/srv/menhir/production/bin/worker` must not exist, and fails hard
if it does. But `ops/menhir/bin/lib.sh:501-508` — same repo — still ends
`submit_op` by launching exactly that binary:

```bash
systemd-run --unit="${unit}" --collect \
    --property=Requires=menhir-caddy-reconcile.service \
    --property=After=menhir-caddy-reconcile.service \
    ...
    "${MENHIR_ROOT}/bin/worker" "${op}" "${job_id}"
```

Cutting that release gives two outcomes, neither of which looks like an ingress
problem: eight new FAILs, or the files are removed to satisfy the verifier and
`submit_op` has nothing to launch. All eight are present and live on the host
today; the deployed verifier has no `obsolete` set, which is why it currently
fails only on the three artifact-level FAILs.

## What the source drops, and what the decision does with it

Source takes `required` from 67 to 48 — 19 paths, in three groups.

### Group A — Menhir's Caddy tooling (5). RETIRE.

```
/etc/systemd/system/menhir-caddy-reconcile.path      already absent from host
/etc/systemd/system/menhir-caddy-reconcile.service   already absent from host
/srv/menhir/production/bin/caddy-release.sh          PRESENT, remove
/srv/menhir/production/bin/caddy-route-apply         PRESENT, remove
/srv/menhir/production/bin/caddy-route-rollback      PRESENT, remove
```

Drop from `required`; add to `obsolete` so the verifier enforces their absence.

### Group B — yawn's shared Caddy stack (4). STOP TRACKING, DO NOT TOUCH.

```
/srv/yawn/projects/yawn.deploy/Caddyfile
/srv/yawn/projects/yawn.deploy/check-drift.sh
/srv/yawn/projects/yawn.deploy/docker-compose.yml
/srv/yawn/projects/yawn.deploy/releases.json
```

These are yawn's and yawn still uses Caddy. Menhir stops listing them as its own
release artifacts. Drop from `required`, and **do not** add to `obsolete` — they
must keep existing, they are simply not Menhir's to verify. This also clears the
standing `FAIL … Caddyfile: digest mismatch`, which exists only because Menhir
was pinning the digest of a file yawn edits.

### Group C — the mutation lane (10). ~~KEEP~~ **WITHDRAWN — retire as source intends.**

**Everything in this subsection is superseded by the correction at the top.**
Kept for the record of what was proposed and why it was wrong.

```
/srv/menhir/production/bin/worker
/srv/menhir/production/bin/{backup,promote,rollback}
/srv/menhir/production/bin/{candidate-deploy,candidate-accept}
/srv/menhir/production/bin/{restore-production,restore-rehearsal}
/srv/menhir/production/bin/release-run
/etc/systemd/system/menhir-op@.service
```

Restore to `required`; remove from `obsolete`. These are how every operation
runs. Note `release-run` is dropped from `required` in source but is *not* in
`obsolete` — incoherent even within the retirement's own terms.

### Add (2) — the live ingress, currently unmanaged

```
/srv/menhir/production/ingress/cloudflared-config.yml
/srv/menhir/production/ingress/docker-compose.cloudflared.yml
```

Sole route to `memory.ctharvey.me`, in no release and no backup. Captured
byte-exact in `pipeline/ingress/` (commit `8f96d06`), digests
`44338a06…8697` and `8c50533f…de09b39`.

**Resulting `required`: 67 − 5 − 4 + 2 = 60. `obsolete`: 5.**

## Edits required, by file

| Repo | File | Change |
|---|---|---|
| yawn.vps | `ops/menhir/bin/verify-artifacts` | `required` → the 60; `obsolete` → the 5 Group A paths only |
| yawn.vps | `ops/menhir/bin/lib.sh` | delete the two `--property=Requires=/After=menhir-caddy-reconcile.service` lines (503-504) |
| menhir | `deploy/installed-artifacts.json` | `destinations` → identical 60 |
| menhir | `deploy/release_spec.py` | `ARTIFACT_SOURCES`: drop Group A + B rows, add the 2 ingress rows |
| host | — | remove the 3 Group A scripts; sudoers grants for `caddy-route-*` are already gone from source |

## Order

1. Edit all four files. Do not cut anything yet.
2. Verify locally that the three authorities are set-equal (60/60/60) before
   building — the failure mode is a hard `sys.exit(1)`, so catch it off-host.
3. Cut a release.
4. Install. `verify-artifacts` must reach **exit 0**.
5. Confirm `submit_op` works — run `backup` through the normal lane and check it
   enqueues, which is the acceptance test for the `lib.sh` fix.
6. Only then delete the reconcile unit sources.

## Risks

- **The verifier validates the release it ships in.** If step 2 is skipped, the
  first honest signal is a failed install on production.
- **Group C is a judgement call about intent.** The decision is to keep the
  mutation lane; if `585f0ff` was the start of a deliberate replacement, this
  reverses it. Nothing in the code says which, and no replacement exists.
- **Group B leaves yawn's Caddyfile unverified by anyone.** That is correct for
  Menhir but means Menhir no longer notices if yawn's ingress changes. It does
  not need to — it is on cloudflared.
- **Ingress files are captured from a running host**, not authored. They have
  never been installed *from* a release, so step 4 is their first round-trip.
