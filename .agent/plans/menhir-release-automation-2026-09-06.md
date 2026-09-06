---
artifact_schema: 1
artifact_uuid: 35d57efd-8fd5-4b9a-9fd5-582ebfb134f7
artifact_type: plan
artifact_status: IMPLEMENTED
---

# Menhir release staging and automation

## Why

- Release preparation currently depends on one-off scripts and hand-assembled workspaces.
- That makes repeat releases slow, makes changelog reconstruction error-prone, and leaves useful
  release logic outside the maintained repository.
- The existing immutable release author, independent review, and deployment transaction are sound;
  the missing piece is a maintained coordinator around them.

## Scope

- Add small, merge-friendly release-note fragments with strict validation and deterministic rendering.
- Add maintained install-bundle construction from the existing release authority and spec.
- Add a resumable desktop release coordinator that owns preparation, review-request generation,
  finalization, bundle construction, status reporting, and an exact-confirmation deployment handoff.
- Keep all production mutation behind the existing reviewed bundle and `deploy-menhir.ps1` boundary.

Out of scope:

- Removing independent security review or production approval.
- Storing secret values in release state, fragments, logs, or bundles.
- Replacing the server-side backup, fence, candidate, route, promotion, or recovery transaction.

## Proposed Design

1. Store one JSON fragment per production-impacting change under
   `deploy/changes/unreleased/`. Validate exact keys, safe identifiers, bounded text, deployment
   class, operator impact, security scopes, and commit reachability.
2. Render deterministic Markdown and JSON release notes from the fragment set. Bind the rendered
   notes to the exact candidate commit range in coordinator state.
3. Move the previously one-off install-bundle builder into `deploy/`, make it reject unsafe paths,
   symlinks, extra keys, digest drift, release/spec disagreement, and existing output.
4. Add a stateful release coordinator with explicit persisted phases:
   `review_requested -> bundled -> deployed`. Preparation and finalization publish only after their
   outputs validate; interrupted work remains retryable. The coordinator may resume completed
   phases, but may never skip review or exact release-id confirmation.
5. Reuse `release-author.py` and the existing desktop deployment wrapper as authoritative
   boundaries instead of reproducing their policy.

## Alternatives Considered

- A single `CHANGELOG.md` Unreleased section is simpler, but creates merge conflicts and cannot
  prove which changes belong to a multi-commit candidate.
- A GitHub Actions-only release is convenient, but the current process depends on local reviewed
  artifacts and a desktop-to-VPS trust boundary. Moving that authority would be a separate design.
- Rewriting the server-side transaction would increase risk without reducing preparation toil.

## Risks

- A coordinator could accidentally become a bypass. Phase transitions therefore fail closed and
  delegate final authoring and deployment to the existing validators.
- Release fragments can drift from commits. Preparation verifies candidate ancestry and records
  exact repository heads.
- Paths can escape the release workspace. Every user-supplied path is resolved, symlinks are
  refused where authority requires regular files, and generated paths stay below one explicit root.
- Retrying after interruption can repeat side effects. State records immutable input digests and
  only the existing deployment transaction performs production mutation.

## Invariants

- Every finalized release must carry an independent review bound to the exact release authority.
- Every deployment must use an immutable reviewed install bundle and an exact release-id
  confirmation.
- Coordinator state and release notes must contain no secret values.
- A failed or incomplete phase must never be reported as a later completed phase.
- Production remains unchanged during staging, preparation, review, and bundle construction.

## Validation

- Unit tests for fragment schema, deterministic ordering, commit-range coverage, path traversal,
  duplicate identifiers, malformed JSON, and bounded values.
- Unit tests for coordinator transition refusals, immutable resume behavior, command construction,
  redaction, and exact deployment confirmation.
- Unit tests for bundle contents, modes, digests, symlinks, unexpected files, and release/spec
  disagreement.
- Existing release-author, schema, deployment-contract, and live-playbook tests.
- Full offline test suite, lint, diff check, artifact validation, and hosted CI after push.

## Docs To Update

- `deploy/LIVE_VPS_PLAYBOOK.md`
- `deploy/README.md` or a focused release-automation runbook
- `.agent/scripts-index.md`
- `CHANGELOG.md`
