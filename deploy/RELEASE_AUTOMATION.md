# Release automation

Menhir has two independent automation boundaries:

- **Product release:** `prepare -> review when required -> finalize -> publish`. It builds,
  tests, scans, versions, documents, and publishes immutable packages/images and provenance.
  Success means a consumable product release exists; it does not mean any production instance was
  deployed.
- **Personal deployment:** `select published release -> stage -> approve -> promote -> observe`.
  It consumes an immutable product release without rebuilding it, proves it against this owner's
  production contract, and deploys only after one explicit owner approval.

`release_flow.py` owns product preparation and finalization. `personal_deploy.py` owns the separate
personal deployment state machine. The only shared object is the immutable finalized release
identity and its evidence; neither workflow silently invokes or mutates the other.

The personal deployment side does not build or publish container images, invent evidence, perform
the product's independent review, or deploy without an explicit command. Those remain separate
trust boundaries. Production must not be used to discover whether the coordinator,
transport, candidate topology, acceptance probe, or rollback works.

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

This contract is implemented by `personal_deploy.py`, `personal_stage.ps1`,
`personal_stage_vps.py`, and `personal_promote.ps1`. The isolated end-to-end rehearsal must pass for
the exact release before approval can be recorded. Direct execution through
`release_flow.py deploy --execute` is disabled so the old handoff cannot bypass staging or approval.

## Before starting

1. Commit and push every repository included in the release. Each checkout
   must be clean and at an exact remote-tracking tip.
2. Add one JSON change fragment under `deploy/changes/unreleased/` for every
   production-impacting change. See [changes/README.md](changes/README.md).
3. Produce the immutable image references, wheelhouse, SBOM, scan evidence,
   public OAuth key, runtime digest, current operations policy, prior release,
   prior route, and current Yawn environment digest required by
   `release-inputs.example.json`.
4. Copy `release-inputs.example.json` outside the repository and replace every
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

Inspect the current phase at any time:

```powershell
python deploy/release_flow.py status `
  --workspace C:\absolute\menhir-prod-0.2.0-9
```

## Select, stage, approve, and promote personally

Create one empty personal-deployment workspace beside the finalized product-release workspace.
Selection verifies the finalized release authority and complete bundle-tree digest; it does not
rebuild or publish anything.

```powershell
New-Item -ItemType Directory C:\absolute\personal-menhir-prod-0.2.0-9
python deploy/personal_deploy.py select `
  --release-workspace C:\absolute\menhir-prod-0.2.0-9 `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9
```

Preview the isolated staging command by omitting `--execute`, or run it as follows:

```powershell
python deploy/personal_deploy.py stage `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9 `
  --runner (Resolve-Path deploy/personal_stage.ps1) `
  --execute
python deploy/personal_deploy.py status `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9
```

Staging uploads the exact bundle into a random private VPS directory, creates disposable Neo4j,
OAuth, policy, ingress, and telemetry authority under `/srv/menhir/staging`, and uses only
non-production credentials. It verifies production-equivalent image digests, memory limits,
network shape, OAuth authorization code with PKCE, MCP discovery/list/recall, an allowed synthetic
write, an exact policy denial, restart persistence, and simulated automatic rollback. It compares
the production release and container identities before and after, removes staging, and writes a
receipt only if every check passes.

Review `release-notes.md` and `staging-receipt.json`. Then bind one owner approval to the exact
release ID and the `staging_receipt_sha256` printed by `status`:

```powershell
python deploy/personal_deploy.py approve `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9 `
  --confirm-release-id menhir-prod-0.2.0-9 `
  --confirm-staging-sha256 <64-character-staging-receipt-digest> `
  --approved-by <owner-identity>
```

Preview promotion first, then execute the same bound command:

```powershell
python deploy/personal_deploy.py promote `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9 `
  --confirm-release-id menhir-prod-0.2.0-9 `
  --confirm-staging-sha256 <64-character-staging-receipt-digest>

python deploy/personal_deploy.py promote `
  --workspace C:\absolute\personal-menhir-prod-0.2.0-9 `
  --confirm-release-id menhir-prod-0.2.0-9 `
  --confirm-staging-sha256 <64-character-staging-receipt-digest> `
  --execute
```

`personal_promote.ps1` independently rehashes the bundle, release authority, staging receipt, and
approval; rechecks all staging results and their 24-hour freshness; verifies image and deployment
class bindings; and only then invokes the existing production transaction. `app-only` selects the
bounded app replacement. `security-config` currently uses the conservative maintenance runner until
its focused production runner is implemented. `maintenance` uses the full resumable backup,
restore, candidate, fence, route, and promotion transaction.

The deployment class is mechanical. It selects `AppOnly` only when every staged fragment is
`app-only`, no sibling repository changed, and the Menhir diff contains only application source
outside protected authentication, runtime, schema, configuration, and deployment paths. A fragment
can escalate but cannot de-escalate the result.

The bundle's complete tree digest is checked locally, by the staging runner, by the promotion gate,
and again by the maintenance transaction before installation. Missing, extra, changed, linked, or
special files stop before production mutation. State is resumable: rerunning a completed stage
validates its saved evidence and does not repeat it.

For maintenance installs, the bundle installer reloads systemd definitions and
restarts the operations gateway and Caddy reconcile path only when each service
was already active. A failed activation restores the prior files, reloads the
restored definitions, and attempts to return those services to their prior
active state before failing the deployment.

## After product release or personal promotion

Retain the product-release workspace as immutable publication evidence. Retain the separate personal
deployment workspace through the observation window as staging, approval, and promotion evidence.
Move released fragments out of `changes/unreleased/` in the next release-preparation commit. The
generated Markdown is the detailed release changelog source; `CHANGELOG.md` remains the short
repository history.
