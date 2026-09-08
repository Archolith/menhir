# Release automation

Menhir has two independent automation boundaries:

- **Product release:** `prepare -> review when required -> finalize -> publish`. It builds,
  tests, scans, versions, documents, and publishes immutable packages/images and provenance.
  Success means a consumable product release exists; it does not mean any production instance was
  deployed.
- **Personal deployment:** `rehearse (select + stage) -> approve -> promote -> observe`.
  It consumes an immutable product release without rebuilding it, proves it against this owner's
  production contract, and deploys only after one explicit owner approval.

`release_flow.py` owns product preparation and finalization. `personal_deploy.py` owns the separate
personal deployment state machine. The only shared object is the immutable finalized release
identity and its evidence; neither workflow silently invokes or mutates the other.

The personal deployment side does not build or publish container images, invent evidence, perform
the product's independent review, or deploy without an explicit command. Those remain separate
trust boundaries. Production must not be used to discover whether the coordinator,
transport, candidate topology, acceptance probe, or rollback works.

The release 11-13 incident review is
[`../.agent/reviews/menhir-release-0.2.0-13-postmortem.md`](../.agent/reviews/menhir-release-0.2.0-13-postmortem.md).
Its controlling rule is that application delivery and release-engineering changes use separate
release trains. A deployment-tool, host, sibling-repository, database, or ingress change correctly
selects `maintenance`; do not combine it with an otherwise routine application update and then
expect the five-minute app-only path.

Before an owner is asked to approve promotion, product publication must archive the current release's exact
fragments, bind deployment class/changelog/source/image identity, complete exact-image staging with
cold-cache transfer, pass a class-specific read-only live preflight, and render one preview naming
the components that may change and the enforced time budget. A failed rehearsal is repaired and
rerun against the same immutable product release. It does not create a new product version unless
product bytes, release authority, or reviewed evidence changed.

## Personal staging and promotion contract

Before personal production promotion is enabled, deployment automation:

1. select an already-published immutable image and release manifest, then deploy them into isolated
   disposable staging without rebuilding or changing them;
2. reproduce production memory limits, Compose networking, OAuth policy shape, and ingress
   request handling without using production secrets or data;
3. run the complete OAuth/MCP/read/write/deny/restart/rollback suite as one job;
4. retain a digest-bound receipt containing the release ID, image digest, test identities,
   start/completion times, and pass/fail result;
5. accept one owner approval bound to that receipt and release ID; and
6. run a bounded production replacement and read-only canary, with automatic prior-image
   rollback on failure.

The production step consumes evidence; it does not create release evidence. Routine promotion
must not run a full database hash, create a fresh backup, rehearse restoration, launch an LLM
review, or debug failed staging logic. Scheduled backup/restore evidence is checked for freshness.
Only a mechanically classified maintenance or recovery release may invoke the full state-protection
transaction.

### Lessons from the first live personal-deployment run

The first release exercised through this path exposed two assumptions that a warm VPS had hidden:

- A newly published Menhir image is not present in the VPS Docker cache. Inspecting it before it is
  obtained fails even though the image was built and pushed correctly.
- The Menhir GHCR package is private, and the VPS deliberately has no standing GitHub registry
  credential. Routine deployment must not copy a developer token to the host merely to make a
  pull work.

`personal_stage.ps1` therefore resolves the digest-pinned image locally, exports the selected tag,
hashes the archive, transfers it over the existing authenticated SSH channel, and loads it into
isolated staging. `personal_stage_vps.py` accepts the transferred tag only when the archive digest
matches and the loaded revision, Menhir wheel-manifest, and OAuth-wheel labels match the finalized
release authority. The staging receipt continues to bind the registry manifest digest from the
finalized release.

This distinction matters because `docker save`/`docker load` preserves the image configuration and
tag but does not reproduce the registry's `tag@digest` lookup metadata on another Docker host.
Treating a failed post-load `docker image inspect tag@digest` as proof that the archive is wrong
causes an unnecessary registry pull and fails for a private package. The correct checks are:

1. select the local source image by the finalized digest-pinned reference;
2. transfer the tagged archive over the authenticated transport;
3. compare the transferred archive digest and release-bound labels; and
4. retain the finalized registry manifest digest in release and staging evidence.

Docker image configuration IDs are not a portable cross-engine authority: Docker Desktop and the
Linux engine can assign different configuration IDs while loading the same archive with identical
layers, labels, creation timestamp, and runtime content. Do not compare those IDs across hosts.
Bind the archive bytes before transfer and use the loaded engine's image ID only to verify which
local image its staging containers actually ran.

Image label inputs are generated by `build_release_image.py`, not transcribed by an operator. In
particular, the Docker label
`org.archolith.menhir.wheel-manifest.sha256` binds
`dockerfile_wheel_manifest_sha256` (the exact `SHA256SUMS` file copied into the image), while
`wheel_manifest_sha256` is the release provenance digest for the wheel records. The build command
derives the Git commit, hashes `deploy/wheelhouse/SHA256SUMS`, verifies every listed wheel, extracts
the single OAuth wheel digest, supplies all Docker build arguments, verifies the resulting image
labels, optionally publishes the image, and writes machine-readable metadata.

```powershell
python deploy/build_release_image.py `
  --version 0.2.0-14 `
  --image ghcr.io/archolith/menhir `
  --python-base ghcr.io/archolith/menhir-python-base:<tag>@sha256:<digest> `
  --output C:\absolute\menhir-image.json `
  --push
```

Copy `image_ref` and `registry_digest` from that output into release inputs. Do not reconstruct
either from Docker's local image ID; a registry manifest digest and a local image configuration ID
are different authorities. Isolated staging repeats label verification as a fail-closed backstop.

A staging failure writes no passing receipt and grants no promotion authority. After correcting a
personal-deployment runner, rerun `stage` against the same selected immutable product release and
empty receipt path; do not rebuild the application release unless application artifacts changed.
Every staging attempt snapshots production authority and container identity and must leave both
unchanged. Future cold-cache tests must remove or avoid the candidate app image so this path is not
accidentally validated only by a previously cached image.

Windows operator wrappers must not assume profile-loaded PowerShell hashing commands or Unix mode
preservation. Use an embedded .NET SHA-256 implementation, verify the complete uploaded bundle by
content, and only then restore every payload mode from the verified `bundle-manifest.json` plus
`install.sh` mode `0755`. Recursive SCP from Windows preserves the reviewed bytes but not the Unix
modes; treating transferred modes as authoritative makes an otherwise valid bundle fail before
installation. Never normalize modes broadly or before the content digest passes.

The production wrapper's transient private-registry credential lookup must also avoid piping the
registry key directly from Windows PowerShell 5 into `docker-credential-desktop`. That shell can
encode native-pipeline input in a form the helper misreads, returning a non-JSON error even though
the credential exists. Invoke the helper with redirected .NET process streams, keep the secret out
of command arguments and logs, upload only the restricted temporary Docker config, and remove it
from both desktop and VPS immediately after the digest-pinned pulls.

On operator workstations with PowerShell 7, `personal_deploy.py` selects `pwsh.exe` for its staging
and promotion wrappers and falls back to Windows PowerShell only when necessary. This keeps native
process I/O and modern cmdlet behavior consistent with the shell in which deployment preflights are
tested; the lower-level wrapper still uses portable .NET hashing and redirected credential-helper
streams so the fallback remains supported.

PowerShell 7 can deserialize an ISO-8601 JSON string directly into a UTC `DateTime`, while Windows
PowerShell 5 leaves the same value as a string. Timestamp gates must accept both representations:
require `Utc` kind for a deserialized `DateTime`, or require an explicit `Z`, successful invariant
round-trip parse, and zero offset for a string. Tests must execute the promotion gate under both
shells using a Python-generated timestamp such as `2026-09-07T15:51:00.882184Z`.

Neo4j `SHOW INDEXES` includes volatile usage statistics. In particular, `lastRead` and `readCount`
change during the mandatory read-only recall probe. They are observations, not schema authority,
and including them in the before/after authority digest makes production acceptance fail by
construction while leaving graph content unchanged. The digest must select only index definition
and health fields. The production incident that exposed this showed six indexes read during the
probe and no admitted graph mutation.

The authority digest's Compose override requests 4 GiB, but an override supplied to `compose exec`
cannot resize an already-running 2 GiB container. On the production-sized graph this caused the
post-probe digest to page tens of gigabytes and stretched a minutes-long check past twenty minutes.
Create the isolated candidate with the reviewed 4 GiB digest allowance; production remains at its
normal 2 GiB limit. Future work should make the digest bounded-memory and emit progress so the
temporary allowance is no longer necessary.

Release 13 exposed three shared-Caddy assumptions after Menhir itself had passed candidate
acceptance. `release-run.sh` expected `/srv/yawn/releases/menhir-route-candidate` to exist, but no
step created it. Once constructed from the reviewed installed files, the Caddy validation
container tried to claim the live proxy's fixed IP. The four configured TLS source paths were also
directories created by Docker because the expected certificate files had never been provisioned.

The live host disproved the handbook's single-Caddy ingress diagram. The `menhir-proxy` network
assigns `172.30.0.2` to `menhir-prod-cloudflared`, which proxies the approved public allowlist
directly to `menhir-prod-app:8099`; Menhir remains `172.30.0.3`. Cloudflared is now the sole
supported Menhir ingress mode. Current release authorities must declare `ingress_mode:
cloudflared`; staging checks the actual peer's Compose service label, and all deployment classes
retain that route unchanged. The inactive Menhir virtual host was removed from shared Yawn Caddy,
and the `menhir-caddy-reconcile` path/service units were retired. A root-only pre-change Caddyfile
is retained at `/var/lib/menhir-production/ingress-retirement/` for incident rollback.

Do not take a healthy Menhir application back down to debug an unchanged shared route. If the
public host, upstream, Caddy image, and route files are unchanged, the personal app-only lane must
retain the existing route and run the release-owned production OAuth/MCP acceptance against the
public URL. If an owner explicitly overrides a failed route-only gate, preserve a root-owned
receipt containing the release digest, reason, exact image identity, candidate result, and public
checks. Before a release that really changes proxy authority, create the candidate directory from
reviewed files, provision those TLS paths as regular root-owned files, and validate Caddy in a
network-isolated container that cannot claim the live proxy address.

Release completion includes operational health, not only application health. It must require an
active `menhir-scaffold-audit.timer`, absent retired Caddy-reconcile units, no failed Menhir
units, no unfinished release/route journal, and a passing app-only readiness audit whose backup and
restore evidence is bound to the current generation. A completed maintenance rehearsal is valid
restore evidence only when its strict receipt schema, generation, release digest, and freshness
all match.

This is the intended split: packaged-product release automation may carry legacy reviewed
deployment artifacts, but personal deployment never mutates shared Caddy. Durable-state or host
operation changes use the maintenance lane; an application change uses app-only, and an OAuth or
client-policy change uses security-config.

This contract is implemented by `personal_deploy.py`, `personal_stage.ps1`,
`personal_stage_vps.py`, and `personal_promote.ps1`. The isolated end-to-end rehearsal must pass for
the exact release before approval can be recorded. Direct execution through
`release_flow.py deploy --execute` is disabled so the old handoff cannot bypass staging or approval.

Promotion success is reported by the executing PowerShell gate, not synthesized by the Python
coordinator. Its receipt binds the release, bundle, staging receipt, owner approval, deployment
class, ingress mode, both wrapper digests, start/completion times, and elapsed time. App-only is
limited to 300 seconds; security configuration and maintenance are limited to 600 seconds. A
wrapper that exits zero without writing the exact receipt is a failed promotion.

Security configuration is a distinct mode throughout selection, staging, approval, and promotion.
It is never converted to maintenance. The repository-owned `personal_security_config.ps1` wrapper
and root-owned `menhir_security_config.py` transaction install only the bounded auth/config set and
fail closed on database, ingress, host, secret-rotation, or other maintenance changes. An optional
`MENHIR_SECURITY_CONFIG_DEPLOY_WRAPPER` may name a reviewed equivalent; it may not point at the
maintenance wrapper.

## Before starting

1. Generate the next label from the installed prior release instead of inventing it or incrementing
   it by hand:

   ```powershell
   python deploy/release_flow.py next-id `
     --prior-release C:\absolute\prior-release.json
   ```

   Pass `--version <major>.<minor>.<patch>` only for an intentional semantic-version change; its
   sequence starts at 1. Run this only when product bytes or reviewed evidence will change. A failed
   staging or production rehearsal reuses the same immutable label. `prepare` independently verifies
   that this is the generated next label and refuses skipped sequences or version regressions.
2. Commit and push every repository included in the release. Each checkout
   must be clean and at an exact remote-tracking tip.
3. Add one JSON change fragment under `deploy/changes/unreleased/` for every
   production-impacting change. See [changes/README.md](changes/README.md).
4. Produce the immutable image references, wheelhouse, SBOM, scan evidence,
   public OAuth key, runtime digest, current operations policy, prior release,
   prior route, and current Yawn environment digest required by
   `release-inputs.example.json`.
5. Copy `release-inputs.example.json` outside the repository and replace every
   example value. Never put secret values in this file. Secret entries are
   version identifiers only.

The `client-policy` secret version must be `sha256-` followed by the canonical
digest in `client-policy.production.json`. Image references must include the
same immutable digest declared beside them.

## Prepare and request review

Create a new empty directory for one release. The inputs file must name that
same absolute directory as `release_workspace_root`.

```powershell
python deploy/release_flow.py prepare `
  --inputs C:\absolute\release-inputs.json `
  --workspace C:\absolute\menhir-prod-0.2.0-9
```

Preparation validates all four repository identities and commit tips, checks
the complete installed-file map, verifies the wheelhouse and policy bindings,
renders `release-notes.md` and `release-notes.json`, and writes
`security-review-request.json`. The state file binds every output by digest.

Send the review request and retained evidence to an independent reviewer. The
reviewer produces the strict shape documented in
`security-review.json.example`, including the exact authority digest and a
separate retained report digest.

## Finalize the reviewed bundle

```powershell
python deploy/release_flow.py finalize `
  --workspace C:\absolute\menhir-prod-0.2.0-9 `
  --security-review C:\absolute\approved-security-review.json
```

Finalization refuses a mismatched review. It creates `release.json` and
`install-bundle/` in private staging first, then publishes them only after the
entire bundle validates. Repeating the command with the same review is safe;
changed inputs or artifacts are rejected.

Publish the packaged product after finalization. This revalidates every frozen artifact, moves only
the fragments that were bound during `prepare`, and writes a nonce-bound publication receipt. It
does not contact production or publish a container image:

```powershell
python deploy/release_flow.py publish `
  --workspace C:\absolute\menhir-prod-0.2.0-9 `
  --confirm-release-id menhir-prod-0.2.0-9
```

The command is resumable after interruption. A pre-existing unowned archive, changed fragment, or
changed bundle is refused rather than adopted. Later unreleased fragments remain untouched.

Inspect the current phase at any time:

```powershell
python deploy/release_flow.py status `
  --workspace C:\absolute\menhir-prod-0.2.0-9
```

## Select, stage, approve, and promote personally

Create one empty personal-deployment workspace beside the finalized product-release workspace.
The `rehearse` command selects the exact product release and immediately runs its isolated staging
rehearsal. Selection verifies the release authority, immutable deployment class, generated
changelog digests, and complete bundle-tree digest; it does not rebuild or publish anything.

```powershell
New-Item -ItemType Directory C:\absolute\personal-menhir-prod-0.2.0-9
python deploy/personal_deploy.py rehearse `
  --release-workspace C:\absolute\menhir-prod-0.2.0-9 `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9 `
  --runner (Resolve-Path deploy/personal_stage.ps1) `
  --execute
python deploy/personal_deploy.py status `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9
```

Omit `--execute` to preview the exact staging command. Repeating `rehearse` resumes the same
digest-bound state and does not repeat a completed stage. Staging first runs a read-only production
preflight; only then does it upload the exact bundle into a random private VPS directory, create disposable Neo4j,
OAuth, policy, ingress, and telemetry authority under `/srv/menhir/staging`, and uses only
non-production credentials. It verifies production-equivalent image digests, memory limits,
network shape, OAuth authorization code with PKCE, MCP discovery/list/recall, an allowed synthetic
write, an exact policy denial, restart persistence, and simulated automatic rollback. It compares
the production release and container identities before and after, removes staging, and writes a
receipt only if every check passes.

Review `release-notes.md` and `staging-receipt.json`. Then bind one owner approval. The coordinator
derives the already-validated release ID and staging digest; the operator supplies only an identity:

```powershell
python deploy/personal_deploy.py approve `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9 `
  --approved-by <owner-identity>
```

Preview promotion first, then execute the same bound command:

```powershell
python deploy/personal_deploy.py promote `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9

python deploy/personal_deploy.py promote `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9 `
  --execute
```

`personal_promote.ps1` independently rehashes the bundle, release authority, staging receipt, and
approval; recomputes the sealed production preflight; rechecks all staging results and their 24-hour
freshness; derives the only permitted promotion mode from the immutable release class; verifies image
and deployment-class bindings; and only then invokes the existing production transaction. `app-only` selects the
bounded app replacement. `security-config` selects the dedicated bounded config/application runner.
`maintenance` uses the full resumable backup,
restore, candidate, fence, route, and promotion transaction.

The deployment class is mechanical and is part of the reviewed immutable release authority. It
selects `AppOnly` only when every staged fragment is
`app-only`, no sibling repository changed, and the Menhir diff contains only application source
outside protected authentication, runtime, schema, configuration, and deployment paths. A fragment
can escalate but cannot de-escalate the result.

The bundle's complete tree digest is checked locally, by the staging runner, by the promotion gate,
and again by the maintenance transaction before installation. Missing, extra, changed, linked, or
special files stop before production mutation. State is resumable: rerunning a completed stage
validates its saved evidence and does not repeat it.

For maintenance installs, the bundle installer reloads systemd definitions and
restarts the operations gateway only when it was already active. A failed activation restores the prior files, reloads the
restored definitions, and attempts to return those services to their prior
active state before failing the deployment.

## After product release or personal promotion

Retain the product-release workspace as immutable publication evidence. Retain the separate personal
deployment workspace through the observation window as staging, approval, and promotion evidence.
The `publish` transaction archives the exact prepared fragments under
`deploy/changes/releases/<release-id>/`; do not move them manually. The generated Markdown is the
detailed release changelog source; `CHANGELOG.md` remains the short repository history.
