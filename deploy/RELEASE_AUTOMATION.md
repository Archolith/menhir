# Release automation

Menhir has two independent automation boundaries:

- **Product release:** `prepare -> review when required -> finalize -> publish`. It builds,
  tests, scans, versions, documents, and publishes immutable packages/images and provenance.
  Success means a consumable product release exists; it does not mean any production instance was
  deployed.
- **Personal deployment:** `select published release -> stage -> approve -> promote -> observe`.
  It consumes an immutable product release without rebuilding it, proves it against this owner's
  production contract, and deploys only after one explicit owner approval.

`release_flow.py` currently coordinates release preparation and the existing deployment handoff.
The target implementation must expose the two workflows as separate commands and state machines.
The only shared object is the immutable published release identity and its evidence; neither
workflow may silently invoke or mutate the other.

The personal deployment side does not build or publish container images, invent evidence, perform
the product's independent review, or deploy without an explicit command. Those remain separate
trust boundaries. Production must not be used to discover whether the coordinator,
transport, candidate topology, acceptance probe, or rollback works.

## Target staging and promotion contract

Before personal production promotion is enabled, deployment automation must:

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

This contract is not satisfied merely by the current `prepare`, `finalize`, and `deploy` commands.
Until a separate personal-deployment `stage` produces and `promote` verifies the required receipt,
follow the transition gate in `LIVE_VPS_PLAYBOOK.md` and do not start another personal production
deployment. Product packaging and publication may continue independently.

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

## Preview and execute deployment

The following commands describe the existing coordinator interface. Under the target contract,
`--execute` must fail closed unless the exact finalized release has a successful staging receipt
and the owner approval is bound to it.

Without `--execute`, deployment prints the exact existing wrapper command and
does not change production:

```powershell
python deploy/release_flow.py deploy `
  --workspace C:\absolute\menhir-prod-0.2.0-9 `
  --confirm-release-id menhir-prod-0.2.0-9
```

After reviewing that command and obtaining production approval, repeat it with
`--execute`. The confirmation must exactly match the reviewed release ID.

```powershell
python deploy/release_flow.py deploy `
  --workspace C:\absolute\menhir-prod-0.2.0-9 `
  --confirm-release-id menhir-prod-0.2.0-9 `
  --execute
```

The coordinator selects `AppOnly` only when every staged fragment is
`app-only`, no sibling repository changed, and the Menhir diff contains only
application source outside the protected authentication, runtime, schema, and
configuration paths. A fragment can escalate that result but cannot de-escalate
it. Any security or host-impacting change selects `Maintenance`; the external
app-only classifier repeats the source check before mutation.
The deployment wrapper and server-side transaction remain authoritative for
preflight, backup, fencing, candidate acceptance, routing, promotion,
acceptance, rollback, and recovery.

The coordinator also passes the staged bundle's complete file-tree SHA-256 to
the fixed desktop wrapper. Maintenance deployment recomputes that digest before
upload and again on the VPS before running `install.sh`; a missing digest, an
extra or changed file, a symlink, or any other mismatch stops before production
mutation.

For maintenance installs, the bundle installer reloads systemd definitions and
restarts the operations gateway and Caddy reconcile path only when each service
was already active. A failed activation restores the prior files, reloads the
restored definitions, and attempts to return those services to their prior
active state before failing the deployment.

## After release

Retain the complete release workspace with the deployment evidence. Move the
released fragments out of `changes/unreleased/` in the release commit or
replace them with the next release's fragments. The generated Markdown is the
release changelog source; `CHANGELOG.md` remains the short repository history.
