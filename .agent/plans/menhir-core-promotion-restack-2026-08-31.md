---
artifact_schema: 1
artifact_uuid: fe097f4e-11ee-4d76-89b2-38497d693d55
artifact_type: plan
artifact_status: APPROVED
---

# Core promotion stack restack and recovery-repair roadmap

Status: owner-approved execution authority for the stacked core-promotion PRs.

## Objective

Refresh the stacked core-promotion branches without broadening their scope, preserve one logical
tranche per PR, and repair the bounded scalar-recovery starvation defect at the layer that owns the
queue contract.

This document is deliberately committed in the bottom tranche so every higher branch receives the
same execution authority during the local history rewrite. It does not authorize merging any tranche
and it does not authorize unrelated cleanup.

## Authoritative starting snapshot

Snapshot captured after the August 31, 2026 mechanical refresh onto:

```text
main = 4f4969a9987c37343db071b766ce2499c66cde93
```

Before this roadmap was written into tranche 1, the stack was:

| PR | Branch | Old parent/base SHA | Old head SHA | Intended disposition |
|---|---|---|---|---|
| #11 | `research/core-promotion-1` | `4f4969a9987c37343db071b766ce2499c66cde93` | `155ae4fbadd5afbc6c64370fe0886913cfcdd827` | Bottom tranche; this roadmap is amended here |
| #12 | `research/core-promotion-2` | `155ae4fbadd5afbc6c64370fe0886913cfcdd827` | `e736125f694d043e4f282be28f9d18205493af0b` | Mechanical restack |
| #13 | `research/core-promotion-3` | `e736125f694d043e4f282be28f9d18205493af0b` | `e4009d2b8dc01dd77a8b9f73ecf71c325666091a` | Mechanical restack |
| #14 | `research/core-promotion-4` | `e4009d2b8dc01dd77a8b9f73ecf71c325666091a` | `0885e46079c3a594c33c4d531b475a453af46286` | Mechanical restack |
| #15 | `research/core-promotion-5` | `0885e46079c3a594c33c4d531b475a453af46286` | `f8f3e019c0ac8f2b2c21e992600e76ef76bd7348` | Restack, then repair repository queue scoping |
| #16 | `research/core-promotion-6` | `f8f3e019c0ac8f2b2c21e992600e76ef76bd7348` | `f45b036f1572cb7d2a530b0547794cd2b6a1e463` | Mechanical restack after #15 |
| #18 | `research/core-promotion-7` | `f45b036f1572cb7d2a530b0547794cd2b6a1e463` | `6b798719c7e486660a7558394ea85dab8e292c67` | Mechanical restack |
| #19 | `research/core-promotion-8` | `6b798719c7e486660a7558394ea85dab8e292c67` | `a2a5262caf97ef6e950225198c4217cf846846b4` | Mechanical restack |
| #42 | `research/core-promotion-9` | `a2a5262caf97ef6e950225198c4217cf846846b4` | `c172f65766aed65f0091037c71689afb11fccee2` | Restack, then consume the scoped queue contract |

Every listed tranche was one commit at this snapshot. Old SHAs are immutable recovery anchors even
after the branches move.

## Non-negotiable guardrails

1. Start from a clean worktree and fetch all remote refs.
2. Create local backup refs for every old head before rewriting anything.
3. Never merge one tranche into another. Rebase/cherry-pick the single logical tranche commit.
4. Use `--force-with-lease`, never an unconditional force push.
5. Keep PR bases chained exactly `main -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9`.
6. A conflict is not permission to accept an entire side. Reconstruct the tranche's own intent against
   its new parent and compare with the old one-commit diff.
7. Do not modify scalar semantics, assertion identity, authority, default-namespace storage identity,
   or the legacy entity-wide rebuild in this repair.
8. Stop if a mechanical tranche gains unrelated files, loses a previously changed file, or becomes
   more than one logical commit.
9. Keep all PRs draft until the complete stack and CI have been re-reviewed.
10. Do not merge or close PR #10 as part of this lane.

## Phase 0 — local safety refs

Run after fetching the roadmap-amended bottom branch:

```bash
git fetch origin --prune

git branch backup/core-promotion-1-pre-roadmap 155ae4fbadd5afbc6c64370fe0886913cfcdd827
git branch backup/core-promotion-2-pre-restack e736125f694d043e4f282be28f9d18205493af0b
git branch backup/core-promotion-3-pre-restack e4009d2b8dc01dd77a8b9f73ecf71c325666091a
git branch backup/core-promotion-4-pre-restack 0885e46079c3a594c33c4d531b475a453af46286
git branch backup/core-promotion-5-pre-repair f8f3e019c0ac8f2b2c21e992600e76ef76bd7348
git branch backup/core-promotion-6-pre-restack f45b036f1572cb7d2a530b0547794cd2b6a1e463
git branch backup/core-promotion-7-pre-restack 6b798719c7e486660a7558394ea85dab8e292c67
git branch backup/core-promotion-8-pre-restack a2a5262caf97ef6e950225198c4217cf846846b4
git branch backup/core-promotion-9-pre-repair c172f65766aed65f0091037c71689afb11fccee2
```

If any backup ref already exists, verify it points to the exact SHA rather than overwriting it.

Synchronize the bottom branch to the remote commit containing this file:

```bash
git switch research/core-promotion-1
git reset --hard origin/research/core-promotion-1
git merge-base --is-ancestor 4f4969a9987c37343db071b766ce2499c66cde93 HEAD
```

The last command must exit zero.

## Phase 1 — mechanically restack tranches 2–4

Use the old parent SHA as the cut point and the freshly rewritten lower branch as the new parent:

```bash
git switch research/core-promotion-2
git reset --hard backup/core-promotion-2-pre-restack
git rebase --onto research/core-promotion-1   155ae4fbadd5afbc6c64370fe0886913cfcdd827

git switch research/core-promotion-3
git reset --hard backup/core-promotion-3-pre-restack
git rebase --onto research/core-promotion-2   e736125f694d043e4f282be28f9d18205493af0b

git switch research/core-promotion-4
git reset --hard backup/core-promotion-4-pre-restack
git rebase --onto research/core-promotion-3   e4009d2b8dc01dd77a8b9f73ecf71c325666091a
```

For each tranche, require:

```bash
test "$(git rev-list --count <new-parent>..HEAD)" -eq 1
git diff --check <new-parent>...HEAD
git range-diff <old-parent>..<old-head> <new-parent>..HEAD
```

The range-diff for #12–#14 should be mechanically equivalent. Investigate any substantive `!`
instead of assuming it is harmless.

## Phase 2 — restack and repair tranche 5 / PR #15

First restack the old tranche commit:

```bash
git switch research/core-promotion-5
git reset --hard backup/core-promotion-5-pre-repair
git rebase --onto research/core-promotion-4   0885e46079c3a594c33c4d531b475a453af46286
```

Then repair `ProjectionLifecycleRepository.pending()` in:

```text
src/menhir/infrastructure/projection_lifecycle_repository.py
```

### Required repository contract

Retain the existing global bounded queue API, but add definition-scoped selection:

```python
def pending(
    self,
    *,
    limit: int = 100,
    definition_id: str | None = None,
) -> tuple[ProjectionWorkToken, ...]:
    ...
```

Contract:

- `pending(limit=N)` preserves the current global behavior.
- `pending(definition_id=X, limit=N)` filters to `X` in Cypher before ordering and limiting.
- `definition_id` must be a non-blank string when supplied.
- ordering remains deterministic by `dirty_at`, then `work_key`;
- corruption checks and shared-current definition-version checks remain fail-closed;
- no consumer may emulate scoped selection by fetching a globally limited page and filtering it
  afterward.

Prefer two constant query variants so the scoped query can use the existing
`ProjectionWorkState.definition_id` index. Do not add an interpolated caller-controlled Cypher
fragment.

### Required tranche-5 regression

Add repository coverage proving:

1. an older dirty row for another definition exists;
2. a newer dirty row for the requested definition exists;
3. `pending(definition_id=requested, limit=1)` returns the requested row;
4. the unrelated row remains pending;
5. `pending(limit=1)` still preserves the old global ordering behavior.

The essential invariant is:

> Definition scoping happens before `LIMIT`.

Amend the tranche-5 commit after the repair so PR #15 remains one logical commit:

```bash
git add src/menhir/infrastructure/projection_lifecycle_repository.py tests/
git commit --amend --no-edit
```

## Phase 3 — mechanically restack tranches 6–8

Because tranche 5 changed semantically, all upper branches must be replayed onto its new head:

```bash
git switch research/core-promotion-6
git reset --hard backup/core-promotion-6-pre-restack
git rebase --onto research/core-promotion-5   f8f3e019c0ac8f2b2c21e992600e76ef76bd7348

git switch research/core-promotion-7
git reset --hard backup/core-promotion-7-pre-restack
git rebase --onto research/core-promotion-6   f45b036f1572cb7d2a530b0547794cd2b6a1e463

git switch research/core-promotion-8
git reset --hard backup/core-promotion-8-pre-restack
git rebase --onto research/core-promotion-7   6b798719c7e486660a7558394ea85dab8e292c67
```

The #16, #18, and #19 range-diffs should remain mechanical except for import/signature adaptation that
is strictly required by the tranche-5 API change. Do not opportunistically refactor them.

## Phase 4 — restack and repair tranche 9 / PR #42

Restack the old tranche-9 commit:

```bash
git switch research/core-promotion-9
git reset --hard backup/core-promotion-9-pre-repair
git rebase --onto research/core-promotion-8   a2a5262caf97ef6e950225198c4217cf846846b4
```

Update:

```text
src/menhir/services/scalar_projection_reconciliation.py
tests/test_scalar_projection_reconciliation.py
```

### Required tranche-9 changes

1. Update `_ProjectionLifecycle.pending()` to expose the optional `definition_id`.
2. Change `ScalarProjectionReconciler.drain_pending()` to call:

   ```python
   self._lifecycle.pending(
       definition_id=SCALAR_STATE_PROJECTION.definition_id,
       limit=limit,
   )
   ```

3. Remove the post-limit Python filter as the correctness mechanism.
4. Update the fake lifecycle so it applies `definition_id` filtering before slicing to `limit`.
5. Add a regression with an older unrelated token first, a scalar token second, and `limit=1`.
   The scalar token must be committed and the unrelated token must remain pending.

Amend the tranche-9 commit after the repair:

```bash
git add src/menhir/services/scalar_projection_reconciliation.py   tests/test_scalar_projection_reconciliation.py
git commit --amend --no-edit
```

## Validation gates

### Per mechanical tranche

For #12, #13, #14, #16, #18, and #19:

- exactly one commit above the new parent;
- changed-file set matches the old tranche;
- `git diff --check` passes;
- `git range-diff` shows semantic equivalence;
- focused tests previously owned by that tranche pass.

### Tranche 5

At minimum run the projection lifecycle domain/repository/atomicity tests and the new scoped-pending
regression. Then run Ruff on touched files and `git diff --check`.

### Tranche 9

At minimum run the scalar reconciliation tests, scalar materialization tests, projection lifecycle
tests, and the new starvation regression. Then run Ruff on touched files and `git diff --check`.

### Whole stack

From `research/core-promotion-9`:

```bash
git log --graph --decorate --oneline   4f4969a9987c37343db071b766ce2499c66cde93..HEAD
```

Expected topology:

```text
roadmap-amended tranche 1
tranche 2
tranche 3
tranche 4
repaired tranche 5
tranche 6
tranche 7
tranche 8
repaired tranche 9
```

Run the repository's normal offline suite and any configured real-Neo4j/hosted validation used by
these PRs. A repository-wide failure already present on current `main` must be reproduced on
`main` before it is classified as unrelated.

## Push order and lease protection

Only after the complete local chain validates, push bottom-up. The remote tranche-1 branch already
contains the roadmap-amended bottom commit; do not rewrite or push it again unless a later reviewed
change requires that. Pin every upper-branch lease to the old remote head captured above:

```bash
git push --force-with-lease=research/core-promotion-2:e736125f694d043e4f282be28f9d18205493af0b origin research/core-promotion-2
git push --force-with-lease=research/core-promotion-3:e4009d2b8dc01dd77a8b9f73ecf71c325666091a origin research/core-promotion-3
git push --force-with-lease=research/core-promotion-4:0885e46079c3a594c33c4d531b475a453af46286 origin research/core-promotion-4
git push --force-with-lease=research/core-promotion-5:f8f3e019c0ac8f2b2c21e992600e76ef76bd7348 origin research/core-promotion-5
git push --force-with-lease=research/core-promotion-6:f45b036f1572cb7d2a530b0547794cd2b6a1e463 origin research/core-promotion-6
git push --force-with-lease=research/core-promotion-7:6b798719c7e486660a7558394ea85dab8e292c67 origin research/core-promotion-7
git push --force-with-lease=research/core-promotion-8:a2a5262caf97ef6e950225198c4217cf846846b4 origin research/core-promotion-8
git push --force-with-lease=research/core-promotion-9:c172f65766aed65f0091037c71689afb11fccee2 origin research/core-promotion-9
```

After pushing, verify every PR still targets the immediately preceding branch and remains draft.

## Stop conditions

Stop the rewrite and inspect before pushing if any of these occur:

- `main` advances again and touches a file changed by the stack;
- a lease fails because a remote branch moved;
- a mechanical range-diff contains a semantic change;
- a tranche has more than one logical commit;
- a scoped pending query applies `LIMIT` before the definition predicate;
- scalar recovery clears an assertion-level `projection_pending` marker;
- a test requires changing scalar fold semantics or physical namespace identity;
- CI fails in a way not reproduced or explained.

## Deferred review hardening

These remain useful follow-ups but are not part of the starvation repair unless a failing test proves
they are required:

- replace shape-only assertion doubles with a real `TypedAssertion` construction path;
- make missing scalar target identity fields fail closed rather than collapsing to empty strings;
- add a partial-batch recovery test where one token commits and a later token fails;
- explicitly test the authority of stored versus currently registered projection-definition versions.

Keep those changes out of the mechanical restack and out of the mandatory #15/#42 repair unless they
become necessary for correctness.

## Completion criteria

This plan is complete when:

1. the chain is linear from current `main` through tranche 9;
2. each PR retains one bounded logical tranche;
3. #15 provides definition-scoped bounded pending selection before `LIMIT`;
4. #42 consumes that scoped contract and passes the mixed-definition starvation regression;
5. all focused and stack-wide validation is green or an identical current-`main` failure is recorded;
6. the PRs remain draft for renewed review;
7. this plan is transitioned to `IMPLEMENTED` and archived after the rewritten stack is accepted.
