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
3. Make sure the throwaway test Neo4j is up (`docker compose` service
   `menhir-neo4j-test`, bolt://localhost:7688). The online lane is mandatory; an
   unreachable instance is a failure, not a skip.
4. Check:

       python scripts/release.py vX.Y.Z

   It prints a PASS/FAIL table and stops at the first failure. Fix, push, re-run.
   `--only lint,offline` reruns a subset while iterating; `--only` is never allowed
   with `--tag`.
5. Release:

       python scripts/release.py vX.Y.Z --tag

   Re-runs every check, creates an annotated tag whose message is the CHANGELOG heading,
   pushes it, watches `Publish to PyPI` until its `test-gate`, artifact verification,
   publish, and GitHub-release jobs all finish, and confirms the version on PyPI.

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
