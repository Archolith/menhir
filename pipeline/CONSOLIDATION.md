# Consolidating Menhir deploy code into `menhir/pipeline/`

A map of what the move actually requires. Nothing here has been executed.

## The finding that makes this cheap

**The host does not have to change.** Installed paths stay exactly where they
are — `/srv/menhir/production/bin/status`, `/etc/sudoers.d/menhir-production`,
and so on. Only the *source* location moves.

`verify-artifacts` validates each installed file by owner, symlink status,
permissions and **SHA-256 of the installed bytes**, against `release.json`. If
the bytes do not change, the digest does not change and verification still
passes. The source path in `release.json` is metadata, not a verified property.

So this is a source-tree refactor with a metadata update, not a host migration.
The host sees nothing until the next release, and that release installs
byte-identical files.

## The hinge

`menhir/deploy/release_spec.py` holds `ARTIFACT_SOURCES`: a dict mapping each
installed path to `(repository, path-within-repository)`. It is the only place
that knows where an artifact comes from. Consolidation is, at its core, editing
the repository and path on twelve rows of that dict.

## What moves

Twelve artifacts are currently sourced from `yawn_vps`:

| Installed path | Source today |
|---|---|
| `/srv/menhir/production/bin/{backup-status, generation-inspect, lib.sh, logs, recover, release-inspect, status, verify-artifacts}` | `yawn_vps: ops/menhir/bin/<name>` |
| `/srv/menhir/production/bin/verify_python_runtime.py` | `yawn_vps: ops/menhir/bin/verify_python_runtime.py` |
| `/etc/systemd/system/menhir-oauth-operations.service` | `yawn_vps: ops/menhir/systemd/…` |
| `/etc/sudoers.d/menhir-production` | `yawn_vps: ops/menhir/etc/sudoers.d/…` |
| `/etc/tmpfiles.d/menhir-production.conf` | `yawn_vps: ops/menhir/etc/tmpfiles.d/…` |

Proposed destination: `menhir/pipeline/bin/`, `pipeline/systemd/`, `pipeline/etc/`.

`ops/menhir/` also holds files that are **not** in `ARTIFACT_SOURCES` and per its
own README are "source-history only and must not be installed": the submit
wrappers `backup`, `promote`, `rollback`, `candidate-deploy`, `candidate-accept`,
`restore-production`, `restore-rehearsal`, `caddy-route-apply`,
`caddy-route-rollback`, plus `worker` and the retired
`menhir-caddy-reconcile.{path,service}` and `menhir-op@.service`.

These need a disposition each before moving — several are for operations ADR 0002
retired, and moving dead code into the new home defeats the point. The census
(`pipeline/census.py`) already lists them as entry points awaiting classification.

## What must change alongside

1. **`release_spec.py`** — twelve `ARTIFACT_SOURCES` rows change repo and path.
2. **Repository cardinality.** The release currently binds a `yawn_vps` commit
   partly for these files. After the move `yawn_vps` is still required for
   `vps/oauth_policy.py` and the gateway, so it does not drop out — but what it
   contributes shrinks, and any check asserting *why* it is bound needs review.
3. **`ops/menhir/README.md` install procedure.** Installation is a documented
   manual sequence of `install -o root -g root …` commands, not an automated
   deployer. Those paths change. This is also the only install documentation, so
   it must move with the code rather than be left behind.
4. **yawn.vps tests** — `tests/test_menhir_operations.py` and
   `tests/test_menhir_shell_hardening.py` reference these files. They either move
   with the code or become cross-repo tests, and cross-repo tests are part of what
   made this hard to follow.
5. **`lib.sh` internal references** to `/srv/menhir/production/bin/…` — these are
   *installed* paths and stay correct. Verify rather than assume; `lib.sh` is
   sourced by every read command.

## Order

Each step is independently revertible and none touches the host until the last.

1. Classify the non-installed `ops/menhir` files (retire vs move). Retire first
   so dead code is not carried across.
2. `git mv` the twelve artifacts into `menhir/pipeline/`, bytes unchanged.
3. Update `ARTIFACT_SOURCES`; move the install documentation.
4. Move the two test files; run them.
5. Build a release and confirm `verify-artifacts` passes against the **existing**
   installed files — digests must be identical. If any digest changed, a file was
   edited during the move and step 2 was not clean.
6. Only then delete `ops/menhir/` from yawn.vps.

## Risks

- **Editing during the move.** Any whitespace or line-ending change alters a
  digest and turns a metadata refactor into a real host change. Move bytes
  first, edit in a later commit. Step 5 is the check.
- **Line endings specifically.** These files move from a repo edited on Windows
  into another repo edited on Windows; a `.gitattributes` difference between the
  two would rewrite line endings and silently change every digest.
- **Splitting rather than consolidating.** If only some files move, discovery gets
  worse, not better. Either the twelve move together or none do.
- **`yawn_vps` still supplies gateway code**, so this does not reduce the release
  from four repositories to three. It reduces what one of them contributes.

## What this does not fix

The PowerShell wrappers in `IdeaProjects/scripts` stay where they are — they run
on the desktop and cannot live in a repo deployed to the host. They remain a
third location by necessity. `pipeline/MAP.md` is what makes them findable.
