---
artifact_schema: 1
artifact_uuid: 5739b12e-217a-4c56-a7a7-248371d5f8a2
artifact_type: plan
artifact_status: IMPLEMENTING
---

# Post-v0.2.3 audit remediation

## Why

The release-to-main Astra audit found a false-green installed-wheel CI gate, two
callable deferred commands with unsafe trust boundaries, a JSON-object Graphiti
compatibility regression, and a mixed-distribution upgrade hazard. These need a
reviewable fix before naming an MVP release candidate.

## Scope and decisions

- Fail an enabled E2E campaign when the wheel cannot build. CI must also check
  runnable acceptance results, allowing only the separately evidenced image
  criterion to remain pending in an ordinary tests run.
- Run the separately installed Beacon package with isolated Python imports so
  repository files cannot shadow it. Keep the explicit Beacon command available.
- Require HTTPS for non-loopback sync targets unless the existing explicit
  insecure-backend override is set. Reject redirects for authenticated uploads.
- Restore the prior Graphiti JSON-object alias normalization at the adapter
  boundary without weakening strict-schema responses.
- Document the fresh-environment Graphiti fork migration and give a clear
  diagnostic when an overwritten upstream installation lacks fork hooks.

No release, deployment, production repair, Oracle-500 run, or RC freeze is part
of this plan. Deferred Beacon and snapshot feature acceptance remains deferred.

## Acceptance matrix

| Boundary | Positive proof | Refusal or adversarial proof |
| --- | --- | --- |
| Installed-wheel CI | Candidate wheel and required runnable criteria pass | Forced wheel-build failure and missing runnable evidence fail the job |
| Beacon child | Installed Beacon build and validate still work | Repository `beacon.py` and package cannot run through preflight or build |
| Sync token | HTTPS and loopback HTTP upload work | Non-loopback HTTP fails before send; redirect cannot forward Authorization |
| Graphiti response | Canonical strict and JSON-object responses retain edges | Alias edge is preserved; malformed or unsupported payload stays bounded |
| Upgrade | Clean installation and ordinary upgrade select the fork | Mixed/upstream overwrite produces a clear remediation message |

## Verification and closeout

Use focused regression tests and relevant neighboring tests locally. Build and
install the final wheel in a clean environment. Required GitHub CI supplies the
full offline, disposable-Neo4j, and stdio acceptance runs on the exact pushed
commit. Review the combined diff, security boundaries, workflow syntax, and
artifact metadata before requesting merge. Update the changelog and MVP plan
with the actual result, not a release approval.
