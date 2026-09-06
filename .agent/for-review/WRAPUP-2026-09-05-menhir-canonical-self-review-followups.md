---
artifact_schema: 1
artifact_uuid: fcba8e4f-6789-4b50-a87a-4414ebc5b94b
artifact_type: implementation_report
artifact_status: READY_FOR_REVIEW
implements: 6c32dbb8-30b6-49df-a31e-491d424051aa
---

# WRAPUP: canonical-self PR #46 review follow-ups

Date: 2026-09-05. Agent: ChatGPT. Status: repository fixes ready for review;
manual live-database/provider checks and activation remain unrun.

Base: `d1a4bb4f368d6af397dcc15ebc180b952265dc7f` on
`feat/canonical-self-authority-boundary-20260905`.
Follow-up branch: `fix/canonical-self-review-followups-20260905`.
The owner requested code remediation and a separate PR. No production configuration,
deployment, historical graph, or manual live Neo4j/LLM operation was changed or run.

## Changes

| Surface | Remediation |
|---|---|
| Combined extraction | Share whole-payload refusal selection between bounded correction and final quarantine. A marker elsewhere no longer exempts an unmarked author alias. Prune unsupported orphan aliases and corresponding index entries before candidate acquisition. |
| Node hydration | Every `enforce` call performs name embeddings only, preserving resolved state. A new receipt's empty proposal list cannot authorize summarization from previous/batched raw text. Neither free-form attributes/summaries nor direct edge-fact appending run. |
| Disposable image launcher | Translate explicit host confirmation paths into exact read-only public-file and live-directory mounts. Do not mount the parent directory or private signer. Validate configured path types. |
| Public lifecycle E2E | Seed Project Cobalt through the candidate app's ingestion/embedding path. Require one episode-linked persistent UUID with a finite populated name embedding, then require the unsigned proposal to resolve onto that exact identity. |
| Regressions and docs | Add 27 offline cases across the new review-regression module and launcher tests. Update the existing mixed-RBAC positive test to use a qualified ordinary name; document why bare mixed aliases remain ambiguous. Update architecture, data models, plan, runbook and changelog. |

## Deliberate availability tradeoffs

A bare unmarked `user` in first-person/mixed text cannot be proven to be an ordinary application
actor merely because the extraction model says so. Such ambiguous relationships stay in raw
source/refusal evidence rather than entering ordinary durable resolution. Third-person-only `user`
entities, qualified ordinary names such as `application user`, and named `user` counterparts on
individually owner-gated marker edges retain distinct identities. No stored identities are deleted.
The existing language detector remains refusal-only; no linguistic test grants identity authority.

Raw current and previous episodes do not carry assertion-level authorization for free-form
hydration. Therefore ordinary node summaries and attributes also stop updating under `enforce`.
Names, name embeddings, ordinary edge extraction/resolution, and exact verified-self-edge recall
remain available. `off` and `observe` preserve the original hydration call and behavior. Existing
stored summaries/properties are preserved, not certified or retrospectively cleaned. Re-enabling
semantic hydration requires an explicitly reviewed verified-input/provenance design.

## Verification actually run

All tests below imported the real checkout and installed Graphiti 0.29.3; model and external-I/O
boundaries were injected, not the production helper implementations. A disposable GitHub Actions
workbench exported the exact tracked source and built dependency wheels without production secrets
or a database. The local reconstructed source tree matched the base tree
`af5144357c366f99c167984023d62d976b9aabde`. Tests ran serially on Linux/Python 3.13.5 with those
wheels, not against a production image. The workbench is separate from this follow-up branch.
Menhir plugin/structural queries were unavailable, so current source and import/call-site inspection
were used instead; no index-ingestion write was attempted.

| Check | Actual outcome |
|---|---|
| Baseline focused suite before remediation | 196 passed, 1 skipped |
| Initial new authority regressions against unmodified code | 9 failed, 4 passed; failures reproduce mixed payload, orphan alias, correction and cross-turn/mode hydration gaps |
| Initial new confirmation-mount regressions against unmodified launcher | 4 failed, as expected: missing mounts and invalid configured paths not rejected |
| Focused final suite: review regressions, launcher, combined extraction, binding, resolver bypass | 223 passed, 1 skipped |
| Full serial offline suite: `python -m pytest -q` | 9,001 passed, 354 skipped, 8 warnings; 241.12 seconds |
| `ruff check --select F811 --output-format concise .` | Passed |
| `ruff check --select F821,ASYNC --output-format concise src` | Passed |
| `python -m compileall -q src tests scripts/dev/test_server.py` | Passed |
| `git diff --check` | Passed |
| Online lifecycle `--collect-only` | One test collected; no Docker, Neo4j or provider execution |
| Artifact validation including this report | 217 records, the same 22 inherited findings; none in modified files |

Commands used for the focused final suite:

```text
python -m pytest -q tests/test_canonical_self_review_regressions.py tests/test_dev_test_server.py tests/test_graphiti_combined_extraction_closure.py tests/test_self_binding.py tests/test_self_resolver_bypass.py
python -m pytest --collect-only -q tests/test_canonical_self_endpoint_e2e.py
menhir artifacts validate . --repository menhir
```

The full-suite total differs from the earlier Windows report because this run has a different
interpreter/platform and 27 added regression cases; it is not a claim that all skipped live checks
were performed. Dependency warnings remain. GitHub's normal CI results on the final commit are
separate evidence; do not substitute these local results for exact-image or live-provider parity.

## Remaining gates

Manual Docker/Neo4j lifecycle, real provider/model extraction, exact-image bind-mount behavior,
production activation, and historical-fork census/remediation remain separately owner-controlled.
Offline tests prove command construction/live-directory source identity, not that Docker executed
those mounts. The real-model seed depends on actual extraction producing the explicit named entity;
its test fails visibly rather than fabricating a substitute UUID or embedding.

This fixes the reviewed paths, not every possible semantic extraction mistake. The follow-up does
not add a signer, weaken signature checks, change the authority schema, certify historical data,
or claim that every ordinary model-extracted relationship is owner-confirmed.
