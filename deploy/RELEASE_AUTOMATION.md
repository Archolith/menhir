# Release automation

`release_flow.py` turns the maintained release controls into one resumable
desktop workflow. It prepares immutable release inputs, renders the staged
change notes, creates the independent-review request, finalizes the reviewed
authority, builds the host installer, and hands the bundle to the existing
deployment transaction.

It does not build or publish container images, invent evidence, perform the
independent review, or deploy without an explicit command. Those remain
separate trust boundaries.

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
