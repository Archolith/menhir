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

### Local verification and CI ownership

Local verification is deliberately change-scoped. Run the affected tests for each changed module,
its direct integration contracts, and the matching lint/static checks. Do not run the complete
repository suite on the maintainer machine during routine implementation or deployment.

After the reviewed commits are pushed, required CI owns the complete suite and records its result
against the exact source SHA. Product publication and personal production promotion remain blocked
until every required CI check for that SHA is green. If CI reports a failure, reproduce that failing
test and its affected neighbors locally, fix them, and push a new SHA; CI then performs the next
complete run. Do not compensate by repeatedly running the full suite locally.

Independent full-system audits are not a routine release step and must not start automatically or
repeat as a remediation loop. Run one only when the owner explicitly requests it for an exceptional
system-wide risk. Normal review remains risk-scaled: ordinary source review plus CI for application
changes, and the security review described below for sensitive release surfaces.

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

`personal_stage.ps1` therefore resolves the digest-pinned image locally to one exact image ID,
applies a nonce-scoped temporary export tag to that ID, saves that tag, removes it, hashes the
archive, and transfers the archive and install bundle over the existing authenticated SSH channel.
It never exports a long-lived mutable candidate tag. It does not upload Python or execute uploaded
Python as root. Sudo may execute only
`/usr/bin/python3 /srv/menhir/scaffold/bin/menhir_stage_vps.py`. That fixed root-owned runner first
verifies its path, owner, mode, and runtime `__file__` digest against the caller's expected digest,
then copies each hostile user-owned upload through no-follow descriptors into a new root-owned
transaction directory and revalidates the copied bytes before Docker uses them. It accepts the
transferred image only when the archive digest, image configuration, layer set, loaded revision,
Menhir wheel-manifest, and OAuth-wheel identity match the same CI publication consumed by release
authoring. Labels remain a backstop, not the publication-to-staging authority. The staging receipt
binds the verified installed-runner digest and finalized registry manifest digest.

This distinction matters because `docker save`/`docker load` preserves the image configuration and
tag but does not reproduce the registry's `tag@digest` lookup metadata on another Docker host.
Treating a failed post-load `docker image inspect tag@digest` as proof that the archive is wrong
causes an unnecessary registry pull and fails for a private package. The correct checks are:

1. select the local source image by the finalized digest-pinned reference and record its exact ID;
2. attach a nonce-scoped temporary tag to that ID solely for `docker save`, then remove the tag;
3. transfer the sealed archive over the authenticated transport;
4. compare its digest, configuration and layers with the CI publication identity; and
5. retain the finalized registry manifest digest in release and staging evidence.

The CI-published image ID is the digest of the immutable image configuration and must match after an
exact registry-digest pull and archive load. The canonical configuration digest provides a second
explicit comparison, while the loaded RootFS list proves the transferred layers. Container IDs,
local tag metadata, and registry lookup aliases are not portable authorities and are never used as
substitutes for that CI binding.

Image label inputs are generated by `build_release_image.py`, not transcribed by an operator. In
particular, the Docker label
`org.archolith.menhir.wheel-manifest.sha256` binds
`dockerfile_wheel_manifest_sha256` (the exact `SHA256SUMS` file copied into the image), while
`wheel_manifest_sha256` is the release provenance digest for the wheel records. The build command
derives the Git commit, hashes `deploy/wheelhouse/SHA256SUMS`, verifies every listed wheel, extracts
the single OAuth wheel digest, supplies all Docker build arguments, verifies the resulting image
labels, generates and validates a Syft SBOM plus Grype report for the exact sealed archive, and
writes machine-readable metadata. Publication is a separate reuse-only operation.

```powershell
python deploy/build_release_image.py `
  --mode build `
  --version 0.2.0-14 `
  --image ghcr.io/archolith/menhir `
  --python-base ghcr.io/archolith/menhir-python-base:<tag>@sha256:<digest> `
  --syft-image docker.io/anchore/syft@sha256:<reviewed-digest> `
  --grype-image docker.io/anchore/grype@sha256:<reviewed-digest> `
  --output C:\absolute\release-image-metadata.json `
  --identity C:\absolute\release-image-identity.json `
  --image-archive C:\absolute\release-image.tar
```

Use `--mode publish` with those exact three outputs and the identity-file SHA-256. It pushes a
collision-resistant `candidate-<full-archive-sha256>` tag, verifies the registry copy, and emits
only a digest-qualified `image_ref`; it never moves a normal version tag. Release authoring consumes
the publication document together with the exact validation metadata, identity, archive, SBOM and
Grype report named by that document, and revalidates the complete chain before it writes release
authority. Do not copy an image digest and independently choose scanner files, and do not reconstruct
the registry digest from Docker's local image ID; a registry manifest digest and a local image
configuration ID are different authorities.

Offline GitHub attestation verification treats the publication trusted root as evidence, not as its
own authority. Release authoring accepts that root only when the SHA-256 of its exact raw bytes
matches the repository-reviewed pin in `release_spec.py`, before writing verification inputs or
invoking `gh`. A Sigstore or GitHub trusted-root rotation requires acquiring the replacement root
independently of the publication bundle and reviewing an update to that repository pin.

A staging failure writes no passing receipt and grants no promotion authority. Before using a
changed staging or scaffold runner, converge the reviewed host scaffold from the exact checkout and
verify the installed scaffold. This is a separate host-infrastructure operation, not product
promotion and not a reason to rebuild the application release. Then rerun staging against the same
selected immutable product release and an empty receipt path. Every staging attempt snapshots
production authority and exact Cloudflared container, image, Compose-label, and network-attachment
identity and must leave them unchanged. Future cold-cache tests must remove or avoid the candidate
app image so this path is not accidentally validated only by a previously cached image.

The root staging transaction retains the sealed preflight and authoritative receipt after the
operator upload and disposable staging resources are removed. Once the root-owned input copy is
created, every later success or failure deletes its `inputs/` tree, including the large image
archive and bundle copy. A failure while creating that copy removes the partial transaction.
Retained root evidence must not become a second payload archive.

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
Operator wrappers may parse a root receipt for validation, but the receipt file consumed by the
Python coordinator must be written from the validated raw JSON string. Piping the parsed object
back through `ConvertTo-Json` can silently rewrite UTC timestamps into the workstation's local
offset after production has already changed.

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
coordinator. Owner approval and the promotion receipt bind the release, bundle, staging receipt,
promotion wrapper digest, selected operator-wrapper digest, root-runner digest, deployment class,
ingress mode, attempt identity, and timing. These executables have fixed paths: environment or CLI
substitution of the promotion wrapper, operator wrapper, or root runner is forbidden. App-only has
a root-runner digest argument that the fixed privileged executable verifies against its own bytes
before taking the transaction lock; security-config enforces the same pre-mutation gate, and
maintenance verifies the trusted bundle copy before installing or invoking its release runner.
Interrupted app-only and security-config recovery rechecks the current root runner against the
approved digest retained in the root-owned active transaction before rollback or rollforward.
An already `complete` transaction is finalized without replaying replacement or rollback. If the
desktop receipt was lost after root completion, each operator wrapper first adopts the matching
root receipt. Maintenance adoption additionally requires the immutable root transaction start and
completion timestamps plus the initiating approval and promotion-attempt identity; it rejects an
older direct maintenance run rather than manufacturing current chronology. No adoption path
reinstalls or repeats production mutation.
A valid receipt repeats the same runner binding as completion evidence. App-only has
a 300-second foreground budget and security-config has a 600-second foreground budget. Maintenance
is resumable and has no short foreground budget. A wrapper that exits zero without writing the
exact receipt is a failed promotion.

Security configuration is a distinct mode throughout selection, staging, approval, and promotion.
It is never converted to maintenance. The repository-owned `personal_security_config.ps1` wrapper
and root-owned `menhir_security_config.py` transaction install only the bounded auth/config set and
fail closed on database, ingress, host, secret-rotation, or other maintenance changes. No
environment variable or command-line option may substitute another security-config wrapper.

### Converge reviewed host infrastructure

When any staging or scaffold runner changes, perform the scaffold install/convergence separately
from product promotion. Start in the exact reviewed checkout so the scaffold bundle and installed
runner digest come from those bytes, then verify the resulting host contract:

```powershell
Push-Location C:\absolute\reviewed-menhir-checkout
PowerShell -File C:\Users\thron\IdeaProjects\scripts\menhir-scaffold.ps1 `
  -Mode Install -SourceRoot (Resolve-Path .) -BootstrapHost root@reviewed-host-alias
PowerShell -File C:\Users\thron\IdeaProjects\scripts\menhir-scaffold.ps1 -Mode Status
Pop-Location
```

`SourceRoot` is always the reviewed repository root. The source map deliberately takes
`personal_stage_vps.py` from `deploy/` and the remaining scaffold files from
`deploy/scaffold/`; passing `deploy/scaffold` itself is invalid. `BootstrapHost` is a
separately authorized root SSH endpoint because a clean host has no Menhir sudo policy and an old
policy cannot authorize its own replacement.

`Install` validates the exact scaffold-bundle manifest, copies its user-owned upload into
`/srv/menhir/scaffold-transactions`, independently compares every trusted-copy file with the
desktop-approved digest, prevalidates sudoers, and transactionally installs root-owned executables
and units under both production locks. An interruption leaves one explicit
`active-install` transaction; rerun with `-Mode Recover -SourceRoot (Resolve-Path .)
-BootstrapHost root@reviewed-host-alias` before retrying. A failure restores prior files and exact
supported systemd state before archiving rollback evidence. It then captures the host contract and
finishes with verification. `Status` provides the separate read-only operator check.
`InstallArchiveTask` is a distinct desktop-backup phase and is not implied by host convergence.
Do not fold convergence or archive scheduling into `prepare`, `finalize`, `publish`, staging,
or promotion.

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
   must be clean and at an exact remote-tracking tip. Wait for the required CI suite on each exact
   source SHA; do not continue to release preparation while any required check is missing or red.
3. Add one JSON change fragment under `deploy/changes/unreleased/` for every
   production-impacting change. See [changes/README.md](changes/README.md).
4. Produce the immutable image references and the complete CI publication set:
   publication document, validation metadata, validation identity, sealed archive, Syft SBOM, and
   Grype report. Also produce the wheelhouse, public OAuth key, runtime digest, current operations
   policy, prior release, prior route, and current Yawn environment digest required by
   `release-inputs.example.json`. Do not mix artifacts from different workflow runs.
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
  --workspace C:\absolute\menhir-prod-0.2.0-14
```

Preparation validates all four repository identities and commit tips, checks
the complete installed-file map, revalidates the publication-to-image-to-scanner evidence chain,
verifies the wheelhouse and policy bindings,
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
preflight; only then does it upload the exact bundle and image archive into a random private VPS
directory. Sudo invokes only the fixed installed runner, which verifies itself, root-copies and
revalidates those inputs, and creates disposable Neo4j, OAuth, policy, ingress, and telemetry
authority under `/srv/menhir/staging` using only non-production credentials. It verifies
production-equivalent image digests, memory limits, network shape, OAuth authorization code with
PKCE, MCP discovery/list/recall, an allowed synthetic write, an exact policy denial, restart
persistence, and simulated automatic rollback. It compares production and Cloudflared identity
before and after cleanup and writes a receipt only if every check passes. The transaction retains
only the root receipt and sealed evidence, not its copied bundle or image archive.

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
and deployment-class bindings; and only then invokes the fixed selected production transaction.
`app-only` selects the bounded app replacement. `security-config` selects the dedicated bounded
config/application runner. `maintenance` uses the full resumable backup, restore, candidate, fence,
route, and promotion transaction.

Immediately before first execution, the coordinator persists `phase: promoting`, a random
`promotion_attempt_id`, and `promotion_started_utc`. If the coordinator or shell crashes, rerun the
same `promote --execute` command. The retry reuses that persisted attempt rather than creating a new
one; if the matching root transaction receipt already exists, the promotion gate validates and
adopts it, then writes the outer promotion receipt without rerunning production mutation.

The deployment class is mechanical and is part of the reviewed immutable release authority. It
selects `app-only` only when every staged fragment is `app-only`, no sibling repository changed, and
every changed Menhir source path is on the explicit explorer allowlist:
`src/menhir/explorer/static/[^/]+` or `src/menhir/explorer/templates/[^/]+`. Any unknown or
unclassified source path selects `maintenance`; absence from a protected-path list is not app-only
authority. A fragment can escalate but cannot de-escalate the result.

The bundle's complete tree digest is checked locally, by the staging runner, by the promotion gate,
and again by the maintenance transaction before installation. Missing, extra, changed, linked, or
special files stop before production mutation. State is resumable: rerunning a completed stage
validates its saved evidence and does not repeat it.

Maintenance and scaffold wrappers treat their VPS upload directories as hostile transport only.
Before root execution, each copies the exact allowlisted upload into a fresh root-owned transaction
directory (`/srv/menhir/install-transactions` for maintenance and
`/srv/menhir/scaffold-transactions` for scaffold convergence), then verifies the copied manifest,
file census, digests, modes, and release or contract bindings. Root code never executes from a
user-owned upload path.

Before a maintenance installer can change production files, the wrapper creates the root-owned
maintenance transaction and acquires the same host-wide admission fence used by app-only and
security-config. That fence remains owned through installation, backup, cutover and final
acceptance; every lane refuses a conflicting owner or incomplete transaction.

The wrapper invokes the verified root installer immediately after beginning maintenance; it does
not count encrypted archives, overlay live backup helpers, or run the first-backup generator.
After validating the exact maintenance binding, acquiring the mutation lock, fsyncing the exact
prior-state snapshot, and durably arming its journal, the installer stops the read-only operations
gateway, refuses active legacy workers, and enters its journaled `bootstrap-backup` phase. An
interrupted cleanup is resumed with the fixed verified helper overlay before retained archives are
recounted. If none remains, the installer creates the bootstrap backup under inherited lock FD 9
before installing release, environment, or deploy authority.

For maintenance installs, the bundle installer reloads systemd definitions and
restarts the read-only operations gateway only when it was already active. An
upgrade from an older host retires Yawn's alternate mutation lane inside this
same durable transaction: after the canonical admission and mutation locks are
held, the gateway is stopped and any active `menhir-op-*` transient worker
causes a fail-closed refusal. The installer snapshots, disables, and removes
`menhir-op@.service`, `worker`, and the public `candidate-deploy`,
`candidate-accept`, `backup`, `restore-rehearsal`, `restore-production`,
`promote`, and `rollback` wrappers. Verification requires their absence. On
rollback or crash recovery, their exact prior files and template unit-file/
activity state are restored before the former gateway state is resumed. Never
delete these artifacts outside the install journal.

Clean-host bundles do not contain that obsolete lane. The dedicated OAuth MCP
exposes only `menhir_release_inspect`, `menhir_status`, `menhir_logs`,
`menhir_backup_status`, and `menhir_generation_inspect`; its sudoers destination
contains read authorization only. The root-only `*.sh` implementation scripts,
including `release-run.sh`, remain installed for Menhir's canonical authority.

## After product release or personal promotion

Retain the product-release workspace as immutable publication evidence. Retain the separate personal
deployment workspace through the observation window as staging, approval, and promotion evidence.
The `publish` transaction archives the exact prepared fragments under
`deploy/changes/releases/<release-id>/`; do not move them manually. The generated Markdown is the
detailed release changelog source; `CHANGELOG.md` remains the short repository history.
