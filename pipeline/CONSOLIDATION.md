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

**Corrected 2026-09-08 against the deployed `release.json`.** An earlier version
of this document said twelve artifacts come from `yawn_vps`, and repeated the
`ops/menhir/README.md` claim that the submit wrappers are "source-history only
and must not be installed". Both are wrong. The release lists **31** artifacts
from `yawn_vps`, and the submit wrappers are among them — they are installed,
verified artifacts, not source history. The README describes an intent the
release does not implement.

The 31 split cleanly into two groups that need opposite treatment.

### Group A — 26 artifacts: Menhir's own tooling, misfiled

Everything under `ops/menhir/`. These are Menhir operator commands that happen to
live in the yawn.vps repository; nothing about them is yawn's.

| Installed path | Source today |
|---|---|
| `bin/{backup, backup-status, candidate-accept, candidate-deploy, generation-inspect, lib.sh, logs, promote, recover, release-inspect, release-run, restore-production, restore-rehearsal, rollback, status, verify-artifacts, worker}` | `yawn_vps: ops/menhir/bin/<name>` |
| `bin/{caddy-route-apply, caddy-route-rollback}` | `yawn_vps: ops/menhir/bin/<name>` — retired by ADR 0002; drop rather than move |
| `bin/verify_python_runtime.py` | `yawn_vps: ops/menhir/bin/…` |
| `/etc/systemd/system/menhir-op@.service` | `yawn_vps: ops/menhir/systemd/…` |
| `/etc/systemd/system/menhir-caddy-reconcile.{path,service}` | `yawn_vps: ops/menhir/systemd/…` — retired by ADR 0002, but see HANDOFF §3: **do not delete before the retirement is finished** |
| `/etc/systemd/system/menhir-oauth-operations.service` | `yawn_vps: ops/menhir/systemd/…` — see Group B |
| `/etc/sudoers.d/menhir-production` | `yawn_vps: ops/menhir/etc/sudoers.d/…` |
| `/etc/tmpfiles.d/menhir-production.conf` | `yawn_vps: ops/menhir/etc/tmpfiles.d/…` |

Destination: `menhir/pipeline/bin/`, `pipeline/systemd/`, `pipeline/etc/`.

### Group B — 5 artifacts: the operations gateway, and it is dead

| Installed path |
|---|
| `/srv/yawn/projects/yawn.vps/menhir_server.py` |
| `/srv/yawn/projects/yawn.vps/vps/core.py` |
| `/srv/yawn/projects/yawn.vps/vps/menhir_capabilities.py` |
| `/srv/yawn/projects/yawn.vps/vps/menhir_tools.py` |
| `/srv/yawn/projects/yawn.vps/vps/oauth_policy.py` |

These install into yawn's tree, not Menhir's, and back
`menhir-oauth-operations.service`. Evidence gathered 2026-09-08 that nothing uses
it:

- **Not routed.** The cloudflared ingress regex admits only `mcp-http`,
  `oauth/{authorize,token,register,client-metadata/agent-smith.json}`,
  `.well-known/*`, `livez`, `readyz`. Everything else is `http_status:404`.
  `/ops/mcp` is not in the list.
- **Not reachable from outside.** It binds `172.30.0.1:8000`, the gateway address
  of the private `menhir-proxy` bridge.
- **No requests, ever.** `journalctl -u menhir-oauth-operations.service` across
  its whole history — 936 lines, earliest entry Aug 30 22:16:49, the first start
  — contains **zero** `GET`/`POST`/`HTTP/1` lines. Uvicorn logs requests at INFO
  and its INFO startup lines are present throughout, so requests would appear.
  (An earlier draft of this bullet claimed the journal held "only the startup
  banner"; that was read off a `tail -8` and was not true. The conclusion is
  unchanged and the real evidence is stronger.)
- **No client.** Nothing in the workspace `.mcp.json` or `mcp-registry.json`
  points at it. Every `/ops/mcp` reference in the workspace is either an Aug 30
  release worktree or an experiment cache.

The service is `enabled` and `active`. It has served zero requests in nine days.

### How it died: the ingress retirement orphaned it

It was not decommissioned. Its route lived in the shared Caddyfile and was
dropped when ingress moved to cloudflared, which never carried that path:

```
Caddyfile.before-cloudflared-only:216   @menhir-operations path /ops/mcp /ops/mcp/* \
Caddyfile.before-cloudflared-only:217       /.well-known/oauth-protected-resource/ops/mcp
current Caddyfile                        0 matches
cloudflared-config.yml                   0 matches
```

So this is a **third** casualty of the half-finished retirement of 2026-09-08
00:31-00:50, alongside `verify-artifacts` failing and `submit_op` being unable to
start any job. Unlike those two it is harmless, because nothing was using the
gateway even while it was routed.

Worth noting the design was already rejected once. A provenance record from an
earlier session states the owner corrected a plan to build a dedicated `/ops/mcp`
connector, "insisting on a shared API endpoint with OAuth-based permissions
instead", after which that plan was abandoned in favour of `/mcp-http`. That is
derived transcript data rather than a spec, so treat it as context, not
authority — but it fits the evidence: the endpoint was built anyway, never used,
and then silently lost its route.

Owner decision 2026-09-08: retire it; build a separate MCP surface for deploys
later if one is wanted, inside `pipeline/`, rather than preserving this coupling.

## Why this now severs yawn.vps completely

Group A moves and Group B retires, so `yawn_vps` contributes **zero** artifacts.
Combined with the ingress retirement, which removes the five `yawn_deploy`
artifacts, the release collapses from four source repositories to one:

| Repository | Artifacts today | After |
|---|---|---|
| `yawn_vps` | 31 | 0 |
| `menhir` | 27 | 53 |
| `yawn_deploy` | 5 | 0 (ingress retirement) |
| unattributed | 4 | 4 — needs its own look |

A single-repo release removes the cross-repo commit binding that made every
release record hard to reason about, and it is what makes "I don't want to deal
with the yawn stuff" actually true rather than aspirational.

## What must change alongside

1. **`release_spec.py`** — twelve `ARTIFACT_SOURCES` rows change repo and path.
2. **Repository cardinality.** Superseded: with Group B retired, `yawn_vps` drops
   out of the release entirely rather than shrinking. Any check that asserts the
   set of bound repositories, or *why* each is bound, has to be updated for a
   one-repo release — `release.json` carries `repos` and `repo_remotes`, and both
   change shape.
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
- **Retiring the gateway is a separate, riskier change than the move.** The move
  is byte-identical and reversible; removing a running `enabled`/`active` service
  is not. Do them as two releases, move first, so a gateway problem cannot be
  confused with a consolidation problem.

## What this does not fix

The PowerShell wrappers in `IdeaProjects/scripts` stay where they are — they run
on the desktop and cannot live in a repo deployed to the host. They remain a
third location by necessity. `pipeline/MAP.md` is what makes them findable.
