---
artifact_schema: 1
artifact_uuid: 23dd2d11-7ed8-4f93-b978-a0c066a4d64b
artifact_type: review
artifact_status: COMPLETE
reviews: 53226b14-e147-4485-b821-f207be853561
---

# Release-container prerequisite audit, 2026-10-14

## Scope and result

Audit of main `93285d20373852bf63b0304545b1be9a1eefd04b` for #168/#123.
The unpublished audit image builds and its installed fork passes offline compatibility
checks. A scanner mount defect was reproduced and repaired. Release acceptance remains
open: the upstream base used for this diagnostic fails the Critical vulnerability policy.
This audit neither names an RC nor changes runtime defaults. No database, ingest, paid
model call, registry publication or deployment was used.

## Reproduced defect and fix

`deploy/build_release_image.py::_run_scanner` mounted the entire archive output directory
at `/candidate`. Here that directory is mode 0700, owned by the host task user, while
`docker image save` produces a mode-0600 archive which the builder already changes to
0644. With `--cap-drop ALL`, even the scanner image's root process cannot bypass the
directory's traversal permissions. The real pinned Syft scanner failed with
`open /candidate/release-image.tar: permission denied`.

Mount only the archive at `/candidate/<archive.name>`, read-only. This preserves private
directory permissions, avoids exposing sibling artifacts, and retains the existing
scanner capabilities/network restrictions. The command regression checks both scanners.
After this change the same archive scanned successfully; the host directory remained
0700 and the archive 0644. No host directory permission relaxation was required.

## Build and installed-package evidence

The locked-wheelhouse shell block from `.github/workflows/release-image.yml` ran with
Python 3.12 in an isolated builder venv. It produced 97 wheels, including the source
wheel from `git archive` of the audited commit. Registry dependencies used frozen lock
hashes; the image build used `--network none` and the production Dockerfile.

| Input/output | Identity |
| --- | --- |
| Diagnostic tag, local only | `ghcr.io/archolith/menhir-audit:0.2.3-0` |
| Upstream audit base | `python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217` |
| Source wheel SHA256 | `422699b6a80d494a9e102fc135513248f266c79082a2b8418f48761cb997a470` |
| Wheel manifest SHA256 | `48017e8e76fa8429fac649dda9f48069c2dbcc210a6ab55e6b4020e0019ee2e2` |
| Image ID | `sha256:0891c0ab0099bc8dcbf78db3faa63b5e8b06b2cce65e04f44ed6b4d9c0a7acd7` |
| Docker archive SHA256 | `6c6d6f9156093e641e2603e5c46e1eadeb0b7b62eb0da539cd128a27eb3cfa23` |

The audit base is the upstream digest referenced by `deploy/python-base.Dockerfile`,
not a built/published patched production base. The diagnostic tag is not a release
version decision. The initial build stopped at scanning before writing sealed metadata
or identity. The repaired scanner was exercised against the unchanged archive; this
does not constitute a sealed fixed-commit bundle or named-RC E2E acceptance.

An offline, read-only, capability-dropped container ran `python -m pip check` and the
existing `_INSTALLED_FORK_PROBE` from E2E-6 against the wheelhouse's public fork wheel:

- pip check: no broken requirements;
- fork `0.30.2.post7`, upstream `graphiti-core` absent;
- all 164 `graphiti_core` Python files matched the locked public wheel;
- native Graphiti parameters and Menhir resolution hooks present.

The public fork wheel needed mode 0644 for the non-root probe's individual file mount.
The probe's PostHog telemetry attempted egress and failed under `--network none`; the
compatibility probe completed successfully and no live provider was called.

## Real scanner evidence

Used the workflow's exact digest-pinned Syft 1.51.1 and Grype 0.118.0 images.
Syft validated the expected image ID and reported 192 packages. Grype initially could
not trust the session HTTPS proxy. For the diagnostic run only, the host CA bundle was
mounted read-only and selected via `SSL_CERT_FILE`; TLS verification remained enabled.
The temporary Grype database volume was removed after scanning. This session-specific
adaptation is not a production build-code change.

| Evidence | SHA256 |
| --- | --- |
| Syft JSON | `61e14aec83a106850c4fc66e2d180d74b038acfae2a2906737ead4841eaab5ee` |
| Grype JSON | `8de230c58b21f9a682377c6684f03c2e51b8a514164af2080912a430edde7eec` |

Grype counts across active/ignored matches: Critical 14, High 105, Medium 88,
Low 25, Negligible 47. The builder's actual validator refused the report with
`vulnerability policy rejected 14 critical finding(s)`.

Critical matches concern OpenSSL, glibc, Perl and Python in the upstream audit base.
In particular, CVE-2026-19445 names Python 3.12.14; the report lists 3.12.15 as fixed.
The checked-in patched-base recipe starts from this same Python digest and only updates
Debian packages, so those updates alone cannot resolve that Python finding. A release
base must be selected/refreshed and independently scanned; these counts do not describe
an operator's currently deployed image or a separately patched base that was not built.

Raw diagnostics are retained in the task workspace `/workspace/menhir-release-audit/`:
`build.log`, `release-image.tar`, `sbom-fixed.syft.json`, `scan-fixed.grype.json`, and
`container-fork-probe.txt`. They are local task outputs, not committed release evidence.

## Remaining #168/#123 gates

1. Resolve release base identity and current scan policy, including the Python finding;
   build a fresh sealed image from the reviewed commit and locked wheelhouse.
2. Complete named-RC configuration/package/image identity and exact-commit required CI;
   run E2E-6 against that sealed bundle and the frozen-candidate E2E campaign.
3. Preserve the owner hold on corpus ingest and paid qualification. Finish final
   quality/performance/cost evidence and separate per-feature decisions when authorized.
4. Resolve fresh-install versus upgrade semantics before new-install-only promotion:
   absent settings currently share code defaults. Explicit false values must survive.

The archive mount fix is a prerequisite repair, not a default-promotion or release
decision. Full registry P4-P5 and numeric/secret inventory remain outside this audit.

## Verification receipt

- Changed behavior: scanner bind mount in the release-image controller; both scanner
  commands are covered by `tests/test_build_release_image.py`.
- Structural `projects` confirms Menhir but reports a stale Windows checkout. The
  builder is absent from the index; affected-tests lookup was unavailable. Selection
  therefore uses current source, direct builder tests, workflow and wheelhouse tests.
- `.venv/bin/python -m pytest --noconftest tests/test_build_release_image.py
  tests/test_release_image_workflow.py tests/test_wheelhouse_verifier.py -q`:
  **56 passed** before and after the fix. The updated mount assertion fails with the
  old directory mount. `--noconftest` avoids an unrelated global fixture's hard-coded
  write to this environment's read-only home; these standalone helper tests use builtin
  pytest fixtures. No application tests or fixture-backed results are claimed.
- Scoped Ruff `--select F811,F821,ASYNC` and `git diff --check`: pass.
- Artifact validation: 244 records / 41 findings, versus the baseline 243 records /
  41 findings. The new review adds no findings; validation still exits nonzero for
  those pre-existing findings.
- No full local suite, live Neo4j/LLM, service startup, frozen-RC campaign, or production
  publication was run. Required CI on the fix commit remains required before merge.
