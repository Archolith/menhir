---
artifact_schema: 1
artifact_uuid: 7e14721a-2902-4eab-85ce-32381dd4d522
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir production release — canonical self and accumulated candidate

## Decision

Release the complete Menhir candidate through the existing production release machinery, with
canonical-self endpoint binding enabled in `enforce` mode and chat plus Graphiti extraction routed
through OpenRouter to `openai/gpt-5.6-luna`. Keep embeddings on OpenAI
`text-embedding-3-small`. Treat the release as the whole delta
from the immutable deployed Menhir commit to the final merged candidate—not as a canonical-self-only
patch.

Use the risk profile of the actual installation: one owner, no external users, one Menhir writer,
one Neo4j store, and an owner who can immediately intervene. Prove the exact candidate locally with
Docker Desktop and the provider/model selected for this release, run normal CI and the mechanically
selected release class, deploy once, and exercise one disposable synthetic canary through the public
API. Do not add an observe-first stage, representative-user cohort, multi-day soak, or separate
shadow stack unless the exact-model test or canary exposes a concrete need.

This plan does not authorize production mutation. Preparation stops for one owner check-in after
the exact candidate, evidence, release class, rollback bundle, and remaining risks are known. One
approval may then cover immutable artifact publication, the fixed deployment transaction, the
synthetic canary write, and its bounded cleanup.

## Current anchors

These are observations, not permanent release identifiers. Re-read them at execution time.

| Item | Observed 2026-09-04 | Release requirement |
|---|---|---|
| Installed maintenance authority | `menhir-prod-0.2.0-8`; Menhir commit `d8bcd6f58aaeb48a801be4a7656d8fcae50b8d54`; image `sha256:af970091…` | This is the immutable `release.json` and maintenance-journal base. Read it again before preparing the bundle. |
| Running application override | `menhir-prod-hotfix-agent-todos-20260904`; source commit `613ff7866c75b44cb3703233f301ce4f90336fbc`; image `sha256:c406c621…` | This is the actual application behavior base. The durable override is outside the older maintenance manifest, so both lineages must remain in the candidate. |
| Live chat/provider configuration | OpenAI for chat, Graphiti LLM, and embeddings; `OPENAI_CHAT_MODEL=gpt-4.1-nano` | This is the rollback baseline. The candidate deliberately changes chat and Graphiti extraction to OpenRouter Luna while retaining OpenAI embeddings. |
| Candidate chat/provider configuration | `local` OpenAI-compatible provider for chat and Graphiti LLM; `LOCAL_LLM_BASE_URL=https://openrouter.ai/api/v1`; `LOCAL_LLM_CHAT_MODEL=openai/gpt-5.6-luna`; OpenAI embeddings | Any optional real-model/image probe must use these values. Production requires a non-empty `local-llm-api-key` secret before deployment. |
| Live canonical-self mode | unset, therefore `off` | Candidate production environment must explicitly set `MENHIR_CANONICAL_SELF_BINDING_MODE=enforce`. |
| Phase 1 candidate | branch `feat/canonical-self-subject-endpoint-20260904`; production-lineage merge `4fdb27a787a79b89e53d7fed4c87a9f22c35960d` | Contains current `origin/main`, the running hotfix lineage, and the installed maintenance-authority lineage. The release binds the later final Phase 4 commit. |
| Phase 1 release delta | Installed authority → candidate: 41 commits and 82 files. Running app source → candidate: 60 commits and 95 files. | Preserve both comparisons: one protects deployment authority; the other describes application behavior changing from what is actually running. Recompute after every release fix. |
| Expected class | `maintenance` | The classifier is authoritative. `deploy/`, configuration, dependency lock, and startup surfaces already rule out `app-only`. |

The endpoint E2E remains useful development evidence and has been exercised with OpenRouter Luna
and the packaged image. Under this self-hosted, single-owner risk profile it is supporting evidence,
not a deployment gate; the deployable authority is still the published image digest.

### Phase 1 checkpoint — 2026-09-04

The first production read exposed two divergent authorities rather than one deployed commit. The
maintenance manifest/journal is complete for `d8bcd6f5`, while a durable Compose override selects
the later TODO-policy application image at `613ff786`. The feature branch originally contained the
hotfix lineage but omitted 14 non-merge release fixes from the installed maintenance lineage.
Commit `4fdb27a7` merged that installed lineage into the separate candidate branch. The only conflict
was additive test coverage in `tests/test_production_runtime_surface.py`; both test groups were kept.
The affected deployment-contract set passed with 145 tests and 3 skips.

The 82-file installed-authority delta is assigned as follows, with no unowned path:

| Owner surface | Files |
|---|---:|
| Release authority, production configuration, dependencies, and startup | 11 |
| Canonical-self authority and tests | 22 |
| Turn evidence, episode admission, ingest, and lifecycle | 13 |
| Todo domain/repository/MCP work | 9 |
| Scheduler tracing | 2 |
| API, MCP, backend protocol, and recall | 8 |
| Architecture, plans, reports, and changelogs | 11 |
| Shared adapter/CLI/taxonomy and cross-surface tests | 6 |

The production-lineage merge changed only deployment scripts/contracts and their tests; the reviewed
canonical-self source and tests are byte-identical to the pre-merge candidate. Production remained
healthy and was not mutated. GitHub PR state could not be read because the workstation's `gh` CLI
is not authenticated; final PR creation and CI remain a Phase 4 gate.

## Scope

In scope:

- the complete deployed-to-candidate change set, including canonical self, turn evidence and
  episode admission, todo operations, scheduler tracing, API/MCP changes, production policy,
  production Compose/configuration, dependencies, and documentation;
- retaining an optional, production-class E2E while closing the required production environment wiring;
- building, inspecting, and digest-pinning the exact image later referenced by `release.json`;
- normal PR CI, mechanical release classification, release authoring, independent security review,
  backup/restore admission, the fixed maintenance transaction, public acceptance, and rollback;
- prevention of new canonical-self forks after activation;
- one disposable production canary and bounded post-deploy inspection.

Out of scope:

- consolidating or deleting the existing exact-name/self-like graph population;
- treating a node's spelling, pronouns, or turn authorship as proof of node identity;
- introducing a production shadow extractor or a prolonged observation program;
- changing the embedding provider/model;
- ad-hoc SSH, Compose, database, policy, or release-state edits that bypass the fixed scripts;
- unrelated feature work discovered during release preparation. A release-blocking defect may be
  fixed narrowly; other work is recorded separately.

## Release invariants

Canonical-self invariant:

> Every trusted current-author relationship projected from byte-identical claimed turn evidence
> while canonical-self mode is `enforce` must bind only the explicitly declared subject endpoint to
> the namespace's deterministic canonical-self UUID.

Authority and refusal outcome:

- authority: atomically claimed turn evidence, the task-local opaque subject endpoint, current
  episode identity, relationship endpoint, assertion grounding, and the final pre-write validator;
- refusal: missing, duplicated, replayed, colliding, quoted, negated, questioned, unsupported, or
  undeclared authority produces no canonical binding and no partially persisted graph write;
- reserved endpoint text is transport-only and must never persist in node or relationship
  properties;
- the canonical node is ineligible for merge in either survivor/absorbed direction at the final
  mutation boundary;
- `off` remains the exact pre-change path; `observe` records decisions but grants no authority.

Release invariants:

- the image built from the final commit, recorded in the release authority, pulled by the VPS, and
  reported by the running container is one immutable registry digest;
- the final release delta and CI evidence cover every changed file, not only canonical-self files;
- exactly one production Menhir writer and one production Neo4j authority exist at promotion;
- OAuth/client policy, secrets, backup evidence, writer fence, route authority, and rollback
  receipts remain valid;
- no existing graph remediation runs during deployment;
- all production changes use `scripts/deploy-menhir.ps1` and its root-owned fixed operations.

## Release surfaces and acceptance ownership

| Surface in the accumulated delta | Required evidence before release |
|---|---|
| Canonical-self extraction, declaration, binding, dedup isolation, and merge refusal | Focused unit/integration corpus; optional real-model/image E2E as supporting evidence. |
| Turn evidence, admission, lifecycle, and background projection | Claim/replay/mismatch tests plus the required post-deploy synthetic canary. |
| Todo repository, domain, API, and MCP operations | Existing todo suite and gateway/tool-census tests pass without policy drift. |
| API routes, backend protocol/runtime operations, and recall integration | Full suite, production runtime-surface tests, authenticated release acceptance. |
| Scheduler tracing and worker lifecycle | Scheduler trace/lease tests and one completed asynchronous projection cycle. |
| Client policy and production configuration | Policy digest/startup tests, rendered Compose review, security review, per-product OAuth/tool matrix, and a bounded TODO lifecycle smoke. |
| Dependency and lockfile changes | Clean install/build from the lock, SBOM, vulnerability scan, provenance, and immutable image identity. |
| Deployment documentation | Artifact validation and agreement among this plan, the canonical-self runbook, and the live VPS playbook. |

## Phase 1 — Freeze and review the real candidate

1. Merge or otherwise select the final candidate commit on the canonical release branch. Record the
   full commit and require a clean worktree.
2. Read the live release authority through the VPS SSH wrapper and record its release ID, Menhir
   commit, image digest, production provider/model values, canonical-self mode, container IDs and
   health, and any unfinished `release-run.json` transaction. This step is read-only.
3. Produce the exact `live Menhir commit..candidate commit` name/status diff. Group every file under
   the surfaces above and assign either an existing test/review or an explicit new gate. An
   unclassified changed file blocks release.
4. Confirm the candidate contains the reviewed canonical-self commits and no later change weakens
   the endpoint declaration, final validator, ordinary-node exclusion, marker scrubbing, physical
   group check, or merge refusal.
5. Rebase/merge main only through normal Git review. Any resulting code change invalidates the
   prior test evidence and returns to Phase 2.

Exit evidence:

- immutable live base commit and candidate commit;
- complete categorized diff with no unowned file;
- clean candidate worktree;
- explicit list of release fixes still required.

## Phase 2 — Close the known production-parity gaps

### 2.1 Keep the optional E2E production-class

Remove the hard-coded `gpt-4o-mini` assignment from
`tests/test_canonical_self_endpoint_e2e.py`. When invoked as release evidence, the durable E2E must accept an explicit release-test
model and report the model used. In release mode it must refuse to run when the model is absent;
developer runs may keep a documented default only when they are not presented as release evidence.

When this optional probe is used for this release, its invocation must set `MENHIR_RELEASE_TEST=1`, name
`MENHIR_RELEASE_TEST_MODEL=openai/gpt-5.6-luna`, select the OpenAI-compatible `local` provider for
chat and Graphiti extraction, use `https://openrouter.ai/api/v1`, and retain the OpenAI embedding
provider with `text-embedding-3-small`. Missing explicit release provider/model input is a refusal,
not a skip or fallback.

Exact-image evidence, when collected, must additionally set `MENHIR_RELEASE_TEST_IMAGE` and
`MENHIR_RELEASE_TEST_COMMIT`. The launcher resolves the image tag once, refuses a revision-label
mismatch, and runs the immutable local image ID with `--pull=never`; its throwaway secrets are
mounted as files and are not copied into the container environment. This is a packaged full-surface
E2E, not a production-ingress claim: the production route surface intentionally excludes the two
internal evidence/admission REST endpoints used to assemble the chain.

### 2.2 Wire the feature mode into the production authority

Add `MENHIR_CANONICAL_SELF_BINDING_MODE` to the Menhir service environment in
`deploy/docker-compose.production.yml`, document it in `deploy/production.env.example`, and bind
the reviewed candidate `production.env` to `enforce`. Add/adjust production runtime tests so the
rendered container receives exactly the release value and an unknown value still falls back to
`off` without enabling writes.

Extend release authoring and release validation to require exactly one of `off|observe|enforce` in
the rendered production environment and bind the value through the production-environment digest
into the release authority. Acceptance must read the effective value from the running container;
the presence of a line in an input file is not proof that Compose delivered it.

Because this changes production configuration and the environment key set, it remains a protected
release surface. Do not widen the app-only environment allowlist to force a cheaper class.

### 2.3 Add one fixed production canary verifier

Make the canonical canary reproducible rather than an improvised database session. The turn-evidence
and episode-admission HTTP routes are intentionally absent from public ingress, so the verifier must
not claim that the whole chain is public. Define this exact split:

1. a root-owned helper mints a short-lived, policy-owned `menhir-release-canary` JWT in memory and
   posts turn evidence to the non-public loopback/internal API;
2. the same canary identity calls public `/mcp-http` and writes the memory with `add_memory`, carrying
   the returned `turn_evidence_uuid`;
3. after the MCP result returns the episode UUID, the helper posts the pair to the non-public
   episode-admission API;
4. it waits for projection and performs read-only graph assertions from the trusted host side.

The policy identity must have only the scopes/tier/tools needed for this sequence, have no stored
refresh token or reusable credential, and remain distinct from product identities. The verifier
accepts a release ID, generates one unique disposable namespace and turn/episode IDs, and emits a
redacted JSON receipt containing only identifiers, decisions, counts, and pass/fail results. It must
not log memory text, credentials, or provider prompts. Cleanup must target only the generated
namespace and run only after the receipt is durable. If safe scoped cleanup is not available, retain
the clearly named canary data and record that outcome rather than using a broad deletion.

### 2.4 Bind the deployment class and source delta

The maintenance requirement currently exists in prose, while the trusted machine classifier only
proves the narrower `app-only` case. Add release-authority fields for deployment class and
commit-addressed changed-path classification evidence. Include them in the authority hash, schema,
security-review request, and installed bundle. Require `release-run.sh` to verify `maintenance` and
the evidence digest before it can start. For this release, protected paths force `maintenance`; no
caller override may downgrade it.

### 2.5 Make changed policy acceptance executable

The candidate changes `client-policy.production.json` and TODO tool grants. Extend fixed acceptance
to mint short-lived tokens for the policy-bound ChatGPT, Codex, Claude, and OpenCode identities and
compare each `tools/list` result, role, scopes, and namespace to the immutable policy. Exercise one
allowed tool and one denied authority-boundary tool per product role. In the synthetic release
namespace, run one bounded TODO lifecycle covering the newly granted read/link/resolve/reopen/
supersede surface and leave it terminal. Do not use the read-only `menhir-deploy-probe` as evidence
for write permissions it cannot exercise.

Exit evidence:

- optional E2E, when run, accepts and reports the explicit target model;
- production Compose and environment render `enforce` into the container;
- a fixed, scope-bounded canary verifier and receipt schema exist;
- release authority binds the maintenance class, changed-path evidence, and canonical mode;
- fixed acceptance proves the product policy matrix and TODO lifecycle;
- focused tests for these changes pass.

## Phase 3 — Verify and package the exact candidate locally

Use Docker Desktop and the test Menhir setup. The VPS is not a test environment; it is used here
only to read the live configuration and later to execute the approved deployment.

1. Build the candidate image once from the final clean commit and record its immutable local image
   ID and source revision. Publication later supplies the immutable registry digest used by release
   authority.
2. If running the optional exact-image E2E, start that image with disposable local Neo4j and test
   state, the candidate provider selections, OpenRouter Luna for chat/extraction, OpenAI
   `text-embedding-3-small`, and canonical-self mode `enforce`. Inject credentials through the
   existing secret mechanism; never copy production state or expose keys in command output.
3. Run the full repository test suite from a clean environment. The prior run with one concurrency
   timing failure followed by an isolated pass is diagnostic evidence, not a green release gate;
   the final candidate must produce a clean required CI result.
4. Run the focused canonical, evidence, admission, merge, todo, scheduler, MCP, and production
   runtime suites.
5. Optionally drive the exact image through the public-path E2E as an additional confidence probe:
   - affirmative first-person subject statements that should bind;
   - first-person object/non-subject use that must not bind;
   - a question and an explicit negation;
   - quoted and reported first-person speech;
   - multiline/code-fence content and an unterminated quote;
   - third-person `user` and RBAC-role uses;
   - mixed self and third-party entities;
   - replay and evidence/body mismatch.
6. If the optional E2E is run, require for the positive case `READY`, the deterministic canonical
   UUID, a relationship tied to the current Graphiti episode, `MENTIONS` provenance, zero
   endpoint-marker text across every persisted node/relationship property, and an idempotent replay.
   Require canonical merge refusal in both directions. For every negative case require no canonical
   authority; ordinary entities may still be extracted normally.
7. Treat an optional production-model failure as diagnostic evidence. Fix a demonstrated
   parser/prompt/validation defect before relying on that behavior, then rebuild and rerun the
   affected checks. The optional probe does not itself authorize or block deployment; required CI,
   release packaging, and the post-deploy canary retain their own pass/fail authority.

Exit evidence:

- clean full suite and focused suite tied to the final commit;
- optional E2E receipt, if collected, naming the image ID, provider/model values, corpus revision, and results;
- no skipped required CI or release-authority check;
- local image ID and source revision; the registry digest is recorded after publication and before
  release authority is finalized.

## Phase 4 — CI, classification, and release package preparation

1. Open/update the pull request so `.github/workflows/tests.yml` runs its pull-request jobs. Require
   every applicable check to pass on the final commit. Local success cannot replace absent PR CI.
2. Mechanically classify the complete immutable release input. The protected changed paths require
   `maintenance`; preserve the commit-addressed classifier output in release authority and CI
   evidence, and stop if the bundle, source diff, runner, or rendered environment disagrees.
3. Build the SBOM, vulnerability scan, provenance, source-history bundle, rendered production
   Compose/environment/policy, and rollback anchors required by `release-author.py`. Keep the image
   and package local until the owner gate; record the content digest that publication must preserve.
4. Confirm the rendered production environment routes chat and Graphiti extraction through
   OpenRouter Luna, keeps embeddings on OpenAI, and explicitly sets canonical-self `enforce`.
   If an optional local E2E receipt exists, confirm its provider/model values and image identity do
   not disagree with the rendered environment.
5. Generate the independent security-review request from the exact release spec. A different
   reviewer must approve the complete proposed authority digest with no unresolved critical/high
   finding. Publication may not change any reviewed byte or digest.
6. Prepare two local release packages from the same code/image evidence, ready for final authoring
   and validation immediately after approval:
   - primary bundle: canonical-self mode `enforce`;
   - feature rollback bundle: same candidate image and configuration, canonical-self mode `off`,
     with its own release ID/digest and classification.

The off bundle avoids rebuilding code during an incident. It does not replace the maintenance
transaction's prior-release recovery path for failures unrelated to canonical self.

Exit evidence:

- green PR CI at the final commit;
- mechanical classification result;
- exact local image content digest, SBOM, scan, provenance, rendered config, and independent review;
- enforce and off package specifications with expected authority digests;
- prior production bundle/image still available.

## Phase 5 — Read-only production preflight and owner gate

Run the fixed read-only preflight through `C:\Users\thron\IdeaProjects\scripts\vps-ssh.ps1`:

```text
sudo -n /srv/menhir/production/bin/verify-artifacts
sudo -n /srv/menhir/production/bin/backup-status
curl -fsS https://memory.ctharvey.me/readyz
```

Also verify:

- no unfinished or conflicting maintenance/recovery transaction;
- current release/container/image identity still matches the chosen diff base;
- the required encrypted generations, desktop copy, and clean restore drill are fresh;
- disk/memory headroom satisfies the existing maintenance policy;
- the candidate release's pre-fence gate will reject any Compose `config_files` path under `/tmp`.

The running hotfix overlay is already durable at
`/var/lib/menhir-production/hotfix/agent-todos-20260904/override.yml`; the active Compose
`config_files` list contains no `/tmp` entry. The new release must absorb that override's image,
policy, and OAuth-scope effects into the immutable maintenance authority rather than continue the
out-of-band layer. Add the no-`/tmp` check to the fixed runner before its writer fence;
documentation alone is not enforcement.

Present one go/no-go packet to the owner:

- live and candidate immutable identities;
- complete delta summary and release class;
- CI, scan, provenance, and security-review results, plus optional E2E evidence if collected;
- enforce and off package specifications and expected authority digests;
- backup/restore and writer-fence readiness;
- exact production mutations, expected interruption, canary namespace scheme, and rollback actions;
- any unresolved risk.

Stop here and obtain explicit approval. Do not publish the image or release bundles, promote the
temporary overlay, stop/restart containers, change configuration, or write the production canary
before that approval.

## Phase 6 — Deploy once through the maintenance transaction

After approval:

1. Publish the exact locally tested image and require its registry digest to match the reviewed
   content digest. If any byte, commit, config, evidence, or digest changes, stop and return to the
   owner gate.
2. Finish authoring and validating the enforce and off bundles against that immutable image. Verify
   their authority digests match the approved package specifications. Retain the prior production
   bundle/image.
3. Replace the durable hotfix override with equivalent values inside the approved maintenance
   authority; do not carry the overlay forward as a third configuration source.
4. Use the existing desktop wrapper with the reviewed primary bundle:

```powershell
PowerShell -File C:\Users\thron\IdeaProjects\scripts\deploy-menhir.ps1 `
  -BundlePath <reviewed-enforce-install-bundle>
```

Do not replace it with manual SSH or Compose commands. The mechanically selected maintenance path
must complete its fixed stages: capture/backup/fence, decrypt/validate, restore rehearsal,
read-only candidate, candidate acceptance, transactional route, second writer census and
promotion, then public production acceptance.

During the transaction:

- promote/hash the temporary scope overlay before the first restart and prove no active
  `config_files` entry points into `/tmp`;
- verify the candidate container reports the exact release ID, image digest,
  `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_CHAT_MODEL`, provider values, and
  `MENHIR_CANONICAL_SELF_BINDING_MODE=enforce`;
- preserve the single-writer fence and do not start a second writable Menhir or Neo4j;
- stop at the first failed stage and follow `release-run.json`; do not erase the journal or force a
  different release over it.

## Phase 7 — One production canary and bounded acceptance

Immediately after public acceptance, run the fixed verifier once with a namespace derived from the
release ID. This is the only feature-specific production write required by the plan.

Require:

- normal OAuth identity and allowed-tool behavior through `https://memory.ctharvey.me/mcp-http`;
- successful internal turn-evidence staging, public MCP memory write, internal episode admission,
  and asynchronous projection to `READY`;
- exactly one deterministic canonical-self node for the namespace;
- a current-episode relationship and `MENTIONS` provenance for the declared subject;
- no persisted endpoint marker in any node or relationship property;
- replay idempotence;
- merge refusal in both directions;
- one negative quoted/reported-speech case that does not gain canonical authority;
- no rise in projection failures attributable to binding during the canary's complete async cycle.

As part of the same bounded acceptance window, execute the policy-bound product identity/tool
matrix and synthetic TODO lifecycle prepared in Phase 2.5. These are acceptance for other changes
shipping in the release, not extra canonical-self rollout stages.

Persist the redacted receipt with the release evidence. Clean only the exact canary namespace when
the fixed scoped cleanup is available and included in the approval; otherwise retain it under its
obvious synthetic name. There is no arbitrary multi-day wait. Acceptance ends when the canary's
async work completes and the checks above are resolved.

## Phase 8 — Completion or rollback

Declare the release complete only when:

- `verify-artifacts` and public `/readyz` pass after promotion;
- the running release/image/config match the approved authority;
- the public OAuth/MCP access contract passes;
- the canonical canary receipt passes every assertion;
- no unresolved binding-related projection failure remains;
- the release transaction, backups, rollback artifacts, and receipts are durable.

Rollback triggers:

- a false canonical bind, especially quoted/reported speech or an undeclared node;
- failure to bind the declared positive canary;
- marker leakage, wrong physical group, non-idempotent replay, or successful canonical merge;
- repeated binding-attributable projection failure;
- image/config/release identity mismatch, OAuth scope regression, writer-fence failure, or broader
  public acceptance failure.

Response:

- before promotion, let the fixed maintenance transaction retain/fence the correct authority and
  resume only the same release after repairing the named stage;
- after promotion, for a canonical-only failure deploy the pre-authored off bundle through the
  fixed classified path, preserving the new application code while disabling binding;
- for a broader release failure follow the persisted maintenance recovery stage and verified prior
  generation/rollback authority. Never delete `release-run.json`, reattach a stale writer, or infer
  recovery from process absence;
- do not run existing-fork remediation as a rollback or cleanup action.

## Known work required before go/no-go

| Item | Current state | Blocking condition |
|---|---|---|
| Explicit release-model E2E | Durable test accepts and reports OpenRouter Luna with OpenAI embeddings | Supporting evidence only; not a deployment blocker. |
| OpenRouter production secret | Workstation key exists; VPS `local-llm-api-key` is absent | Block production deployment until the fixed secret path contains the key with the enforced owner/mode and verification passes. |
| Exact-image E2E | Packaged-image probe exists and has passed against disposable Docker Neo4j | Supporting evidence only; a later packaging-neutral change does not require rerunning it for deployment. |
| Production mode wiring | Compose now passes `MENHIR_CANONICAL_SELF_BINDING_MODE` and the example sets `enforce` | Block until the rendered release environment proves the intended value before deployment. |
| Release authority for mode | Release authoring now validates and digest-binds the canonical mode enum | Block until the finalized bundle and running-container acceptance prove the same effective value. |
| Maintenance class authority | The app-only classifier is trusted, but `release.json`/maintenance runner do not yet bind a deployment class or changed-path evidence | Block until the immutable authority and runner verify both. |
| Fixed production canary | Turn-evidence/admission routes are private and no receipt-producing canary exists | Block until the scoped verifier exists; its required execution is the post-deploy synthetic canary, not a local image E2E. |
| Changed client-policy acceptance | Current deployment probe is read-only and cannot prove the new TODO grants or product identities | Block until the product/tool matrix and bounded TODO lifecycle are fixed acceptance checks. |
| Final PR CI | No final pull-request result for this candidate | Block until all required jobs are green on the final commit. |
| Full release review | Pre-plan delta spans 78 files and protected surfaces | Block until every final changed file has test/review ownership. |
| Mechanical release bundle | Not yet prepared for the final merge commit | Block owner approval until classification, scan, provenance, and security review pass; block deployment until post-approval publication and bundle validation preserve the approved digests. |
| Out-of-band hotfix overlay | The live overlay is durable and not under `/tmp`, but its image/policy/scope authority is outside the older maintenance manifest; the no-`/tmp` prohibition is still not enforced by release scripts | Block restart until the candidate absorbs the hotfix effects, drops the extra overlay, and the runner hard-refuses any `/tmp` config reference before fencing. |

The existing self-like/fork population is not a release blocker because this release performs no
historical consolidation and does not claim to repair existing data. It remains separate owner
work under the canonical-self remediation runbook.

## Execution record

Fill this section during implementation; do not replace evidence with prose.

```text
Live release / Menhir commit / image: authority menhir-prod-0.2.0-8 / d8bcd6f5 / sha256:af970091…; running override menhir-prod-hotfix-agent-todos-20260904 / 613ff786 / sha256:c406c621…
Final candidate commit / image digest: Phase 1 lineage merge 4fdb27a7; final Phase 4 commit and image TBD
Complete diff reviewed: 82-file authority census assigned with zero unowned paths; semantic acceptance remains in Phases 2–4
Live providers / chat model / embedding model: openai/openai/openai; gpt-4.1-nano; text-embedding-3-small
Candidate providers / chat model / embedding model: local/local/openai; openai/gpt-5.6-luna via https://openrouter.ai/api/v1; text-embedding-3-small
Canonical mode in rendered candidate:
Full suite / focused suite:
Optional exact-model/image E2E receipt:
PR CI run:
Mechanical release class:
SBOM / scan / provenance:
Security review digest and reviewer:
Enforce bundle digest:
Off rollback bundle digest:
Read-only VPS preflight:
Owner approval:
Maintenance transaction receipt:
Public acceptance receipt:
Canonical canary namespace / receipt / cleanup outcome:
Final running release / image / mode:
Remaining assumptions:
```

## Follow-up: remediate the existing 66-user population

After this release passes its production canary, open the historical-fork remediation as the next
separate work item. Its authorities are:

- [Canonical-self operations runbook](../workflows/canonical-self-migration-runbook.md), especially
  **Remediating existing data**;
- workspace plan
  `C:\Users\thron\IdeaProjects\.agent\plans\menhir-canonical-self-remediation-plan.md`;
- workspace corrections
  `C:\Users\thron\IdeaProjects\.agent\plans\menhir-canonical-self-observation-correction-2026-09-04.md`,
  which supersede the earlier name-, arity-, and grammar-based authority assumptions.

The previously measured 66 nodes are a planning baseline, not an immutable target list. Start with
a fresh read-only production recount, classify each candidate from episode and relationship
provenance, and obtain owner approval of the exact manifest. Apply only through a journaled,
reversible migration whose inverse bundle and restore/reapply behavior have been proven on a restored
production copy. Ambiguous nodes remain quarantined; no node is selected or merged by the name
`user` alone.

This deployment plan neither executes nor authorizes that migration. It establishes prevention
first so the historical population does not keep growing while remediation is prepared.
