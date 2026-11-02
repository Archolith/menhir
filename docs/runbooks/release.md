# Cutting a release

Releases are tagged from `main` with `scripts/release.py`, never with a bare `git tag`.
The script and `publish-pypi.yml` are the two halves of one gate: the script refuses to
create the tag unless every check is green on the exact commit, and the workflow refuses
to publish unless `tests.yml` is green on the exact commit the tag resolves to.

## Why two halves

v0.2.1 and v0.2.2 were tagged from a workstation whose local suite was green while
`tests.yml` was red on the same SHA (`ruff --select F821` caught a `NameError` in the
lease-recovery sweep that the offline suite's fakes could not reach). The publish
workflow did not consult CI and shipped both. Both were yanked; v0.2.3 carried the fix.

## Steps

1. Land everything on `main` and push. Wait for `tests.yml` to go green on that commit.
2. Bump `version` in `pyproject.toml` and add a `## <date> - vX.Y.Z ...` heading to
   `CHANGELOG.md`. Commit and push that too -- the gate requires HEAD == `origin/main`.
3. Make sure the throwaway test Neo4j is up (`docker compose -f docker-compose.test.yml up -d`:
   service `neo4j-test`, container `menhir-neo4j-test`, bolt://127.0.0.1:7688). The online lane is mandatory; an
   unreachable instance is a failure, not a skip.
4. Check:

       python scripts/release.py vX.Y.Z

   It prints a PASS/FAIL table and stops at the first failure. Fix, push, re-run.
   `--only lint,offline` reruns a subset while iterating; `--only` is never allowed
   with `--tag`.
5. Release:

       python scripts/release.py vX.Y.Z --tag

   Re-runs every check, creates an annotated tag whose message is the CHANGELOG heading,
   pushes it, watches `Publish to PyPI` until all its jobs finish, and confirms the version
   on PyPI.

Only repository admins can create `v*` tags (the `release-tags` ruleset), and that one tag
publishes everything. In order, `publish-pypi.yml` runs the test gate, verifies the artifacts,
publishes to PyPI, builds and publishes the release image, verifies its attestation, tags it
`X.Y.Z`, and creates the GitHub release. The image version label (`X.Y.Z-1`) comes from the tag,
and the Python base comes from `deploy/release-python-base.txt` at the tagged commit. Change
that file in a normal PR to move the base.

Deploying stays a separate, manual step: `tools/menhirctl.ps1 upgrade X.Y.Z` from menhir-deploy.

## If a release stops partway

Re-run the failed jobs on the run page. If that is no longer possible, start a new run on the
tag; a run from a branch is refused:

    gh workflow run publish-pypi.yml --repo Archolith/menhir --ref vX.Y.Z -f tag=vX.Y.Z

A new run skips what is already finished. A version already on PyPI is not uploaded again, and
the GitHub release attaches the files PyPI serves. An image tag that already exists stops the
image build. An existing GitHub release is left alone. The run stops and asks for a look by hand
if PyPI holds a different set of files than the build produced, or if the image tag names another
digest.

## What the gate checks

| check     | mirrors                                   |
|-----------|-------------------------------------------|
| tree      | clean working tree                        |
| branch    | on `main`, HEAD == `origin/main`          |
| version   | pyproject == tag, tag unused, tag newest  |
| changelog | heading names the version                 |
| deps      | `publish-pypi.yml` no-VCS-dependency step |
| lint      | the two `ruff` commands in `tests.yml`    |
| offline   | `tests.yml` offline job                   |
| online    | `tests.yml` online job                    |
| ci        | `tests.yml` green on HEAD's SHA           |

Keep the `lint` command lines in `scripts/release.py` identical to `tests.yml`; that is the
one place the two definitions can drift.

## If a release is bad anyway

Yank it on PyPI (project → Manage → release → Options → Yank, with a reason); do not
delete. Fix on `main`, then cut the next patch through the same gate.
