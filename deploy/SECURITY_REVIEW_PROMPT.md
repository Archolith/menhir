# Focused-delta security review: reviewer brief

Given to an independent reviewer (an Opus subagent for releases 14-18) after
`release_flow.py prepare`. Fill the angle-bracket fields; the reviewer must not be
the release author. Expect 10-20 minutes and a report of roughly 30 KB.

---

You are the independent security reviewer for Menhir production release
`menhir-prod-0.2.0-<N>` (artifact-only, `image_provenance: inherited`; images
identical to release 13). Read-only except ONE output file:
`<release dir>\security-review-report.md` (ASCII-safe UTF-8, LF). No host contact.

Inputs:
- Review request `<release dir>\workspace\security-review-request.json`
  (authority_sha256 `<value>`; recompute it with
  `deploy/lib/menhir_schema.py release_authority_sha256` over the `release`
  object). Also `release-spec.json` and `release-notes.md` there.
- Prior authority `<prior release dir>\workspace\release.json` and its report
  `<prior release dir>\security-review-report.md`; follow that format and method.
- Menhir checkout `<path>` at HEAD `<sha>`; the prior release's repo commit is in
  the prior `release.json` under `repos.menhir`. Implementing commits: `<list>`.

What this release claims (verify, do not trust): `<one paragraph: which
artifacts change and why, what is unchanged>`.

Method (focused delta from the prior authority):
1. Diff the artifacts maps and every top-level field prior vs new; list exactly
   which digests changed, added, removed; confirm images, secret_version_ids and
   evidence digests are identical; confirm `production.env` changes only in
   `MENHIR_RELEASE_ID`/`MENHIR_RELEASE_COMMIT`.
2. Re-derive every git-sourced artifact from the checkout at the pinned commit
   and compare blob OID and sha256 with the record.
3. Review the diff of every changed artifact and of the menhir range since the
   prior release; confirm no artifact source outside the claimed set changed.
4. Fragment commits exist and are inside the candidate range.
5. `<release-specific checks>`.
6. Cover all eight scopes with one-line statements; for unchanged areas write
   "unchanged from release <N-1> authority, not reopened" with digest evidence:
   authentication-and-oauth-authority, authorization-and-client-tool-policy,
   backup-restore-and-rollback, host-privilege-and-command-wrappers,
   network-and-ingress-boundaries, runtime-hardening-and-observability,
   secret-handling, supply-chain-and-build-evidence.

Output: the report with the authority sha256, the verdict (APPROVED only with
zero unresolved critical/high; otherwise NEEDS-CHANGES with findings), counts by
severity, Findings with file:line evidence including low and informational, and
a "Review confidence" line. Reply with the verdict, the unresolved critical/high
counts, and the report path.
