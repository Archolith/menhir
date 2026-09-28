---
artifact_schema: 1
artifact_uuid: dbde430c-9be1-4131-aa85-871a8e99e5ef
artifact_type: plan
artifact_status: APPROVED
---

# Menhir local-stdio MVP release plan

**Date:** 2026-09-16  
**Owner:** Menhir  
**Purpose:** Define, audit, test, benchmark, and close a bounded local MVP without absorbing the entire Menhir backlog.

**Current gate:** Gate B audit COMPLETE and pre-freeze Gate C acceptance evidenced on merged `main` `0f8deac52628b29e91ab3bee66256d0a4a1789fb`. Phase D blocker review and named RC freeze are next. No RC, deployment, #119 repair, or final release is authorized by this status.

## 1. MVP contract

The MVP is a **local, single-operator coding-agent service exposed to the agent through MCP stdio**.
Loopback HTTP, Neo4j, workers, schedulers, and other local processes may remain implementation details; a normal MVP consumer must not need to use a remote MCP or public HTTP interface directly.

### Required product capabilities

1. **Memory**
   - ingest durable memory;
   - observe/track enrichment sufficiently to distinguish accepted, processing, failed, and recallable states;
   - recall memory and build context;
   - preserve namespace, provenance, currentness, and retention semantics claimed by the shipped surface.

2. **WorkArtifacts**
   - read/list tracked artifacts;
   - inspect questions and relationships;
   - apply supported lifecycle transitions;
   - declare supported artifact relationships and supersession;
   - preserve stable identity and Git-backed source reconciliation.

3. **Local code ingest and structure**
   - ingest a local repository from an absolute path;
   - query files, symbols, imports, callers, tests, endpoints/dependencies where supported;
   - produce coverage-qualified context / blast-radius / affected-test answers;
   - persist the structure graph across restart.

4. **TODOs**
   - add, list/read, and close TODOs;
   - preserve repository-relative location/context where supported;
   - persist TODO lifecycle across restart.

5. **Archolith Graphiti fork dependency**
   - ship the native Graphiti extension hooks used by Menhir as a public, versioned wheel;
   - install the fork from a hashed package-registry dependency on the cold-install and container paths;
   - keep the upstream `graphiti-core` distribution out of the same environment to avoid competing `graphiti_core` packages.

### Explicitly outside MVP

Unless a release-blocking dependency is discovered, the following are post-MVP:

- remote MCP and hosted deployment;
- OAuth/remote connector rollout as a product requirement;
- remote structure scanning or raw remote structure payloads;
- Claude/Codex/OpenCode hook packaging as a required install path;
- Explorer as a supported MVP surface;
- FalkorDBLite / embedded graph backend;
- large-haystack LongMemEval `_s` / `_m` variants;
- project rename/migration support unless a local E2E proves it is already safe;
- change-provenance briefs and other new composition features;
- default-off research/scalar/event authority features unless separately promoted before the RC freeze.
- Beacon generation and Beacon MCP consumption (deferred in #120; may be shown separately, but are not release gates).

## 2. Release rule

The MVP ships only from a **named, clean release-candidate commit**. Canonical benchmark and E2E evidence must name the exact same Menhir commit and configuration.

Any code/config-default change after the RC freeze invalidates the canonical evidence for the affected surface. A release-changing fix requires rerunning the affected E2E lane and, when it can alter ingest/recall behavior, the canonical LongMemEval campaign.

No release decision is based solely on the size of the open-issue backlog. Every open item is classified against the MVP contract as:

- **BLOCKER** — violates a required capability, safety/correctness invariant, install path, or release-evidence requirement;
- **FIX IF CHEAP** — non-blocking defect with low-risk bounded repair before freeze;
- **DOCUMENTED LIMITATION** — acceptable for MVP only if the unsupported behavior is explicit and does not corrupt supported flows;
- **POST-MVP** — outside the contract;
- **RESEARCH** — requires evidence before product work.

## 3. Evidence products

This plan must produce separate durable artifacts rather than hiding results in this checklist.

1. **Independent release audit**
   - path: `.agent/reviews/menhir-local-stdio-mvp-release-audit-2026-09-27.md`
   - artifact type: `review`
   - records scope, findings, severity, reproduction, disposition, and release blockers.

2. **MVP E2E evidence report**
   - path: `.agent/for-review/WRAPUP-2026-09-XX-menhir-local-stdio-mvp-e2e.md`
   - artifact type: `implementation_report`
   - records environment, exact commit, test corpus hashes, commands, results, restart evidence, and unresolved limitations.

3. **LongMemEval release evidence**
   - canonical benchmark artifacts live in `Archolith/archolith-bench/benchmarks/` and must record Menhir commit, bench commit, dirty state, graph identity/freshness, dataset variant, provider/model configuration, counts, per-type results, and caveats.

4. **Final release checklist / closeout**
   - appended to this plan or a successor closeout artifact;
   - contains issue dispositions and links to all canonical evidence.

## 4. Phase A — scope freeze and preflight

### Approved MVP platform and candidate configuration (2026-09-27)

The owner approved this supported-path decision after the default stdio E2E pack
passed on Windows and Ubuntu. It defines the candidate contract; the exact RC
commit, installed dependency set, environment values, and benchmark graph are
frozen later under Gate D. Neither platform preflight is a frozen-RC run.

- **Platforms:** Windows 11 AMD64 and Ubuntu Linux AMD64, with Python 3.12 or
  newer as required by the package. The measured paths use Python 3.12.10 on
  Windows and Python 3.12 in Ubuntu CI; other Python versions and OS variants
  have not received this MVP E2E acceptance.
- **Agent interface and topology:** one local operator uses MCP stdio. Its
  bridge calls one long-lived Menhir backend over loopback HTTP; the backend
  uses Neo4j 5 with APOC. The bundled Docker Compose graph is an optional
  way to start Neo4j locally, not a second agent-facing interface.
- **Normal provider defaults:** chat and Graphiti extraction default to
  `local`; Graphiti embedding inherits that provider when unset. The local
  endpoint defaults to `http://127.0.0.1:8081/v1` and the chat model name to
  `qwen3.5-35b-a3b`. The local embedding model defaults to an empty string:
  the operator must configure a compatible model and live endpoint. The
  `local` provider denotes an OpenAI-compatible endpoint and can point to a
  hosted gateway; it does not promise local inference. An explicit `openai`
  provider selection remains available.
- **Canonical benchmark configuration:** separate from the normal install,
  the planned Oracle path uses OpenAI `gpt-4o-mini` for extraction,
  `text-embedding-3-small` for embeddings, `gpt-4o` for answers, and
  `gpt-4o-mini` for judging, with adaptive segmentation. The five-item
  preflight and canonical quality run must verify and record their actual
  model and provider settings before they count as evidence.
- **Dependency identity:** `archolith-graphiti-core==0.30.2.post1` from PyPI,
  with locked wheel SHA-256
  `a748f98e0b09d64ab1eb29bd3449f52b552250a31663e86c4d99dc51ddf991f0`.
  The same environment must not install upstream `graphiti-core`.
  `archolith-mcp-framework==0.2.0` and `archolith-oauth==0.3.1` remain the
  direct package pins; the RC records the complete resolved set.
- **Feature and product limits:** scalar-state, event-history, frontier
  retrieval/brief, and canonical-self activation flags remain at their
  shipped default-off settings; no research lane is promoted for MVP.
  Project rename, Beacon integration, and remote MCP remain outside this
  release contract.

The Windows local run on merged `4750b1c8` and the Ubuntu [post-merge CI
run](https://github.com/Archolith/menhir/actions/runs/36368518264) each reported
11 default stdio E2E passes and one declared release-container skip. The
candidate-wheel fork path passed; release-container validation, the five-item
Oracle preflight, exact RC configuration, and frozen-RC evidence remain open.

### A1. Freeze supported behavior

- [x] Confirm the required capability list in §1.
- [x] Confirm stdio is the only supported agent-facing transport for MVP.
- [x] Confirm the local backend/Neo4j topology used by stdio.
- [x] Confirm the supported primary OS/platform set for the MVP package.
- [x] Confirm provider/model defaults and planned canonical benchmark models; verify actual settings in benchmark preflight.
- [x] Confirm which currently default-off features remain off.
- [x] Confirm project rename is unsupported unless explicitly promoted after E2E proof.
- [x] Confirm the exact Archolith Graphiti distribution/version and public wheel hash for supported candidate-wheel installs; release-container validation remains open.

### A2. Reconcile current work in flight

- [ ] Inventory all open Menhir issues and active plans against the MVP contract.
- [ ] Mark each BLOCKER / FIX IF CHEAP / DOCUMENTED LIMITATION / POST-MVP / RESEARCH.
- [ ] Do not merge unrelated architecture cleanup into the MVP lane.
- [ ] Keep already-approved correctness work that directly affects a required surface.
- [ ] Record all release-changing feature flags/defaults.

### A3. Benchmark preflight

- [ ] Verify `archolith-bench` LongMemEval harness against current Menhir APIs.
- [ ] Verify a throwaway 5-item Oracle graph can build from the intended candidate branch.
- [ ] Verify provenance/manifest records clean commit and fresh graph correctly.
- [ ] Verify zero failed episodes is enforced by the acceptance validator.
- [ ] Verify temporal-date repair/backfill behavior is current and automatic or explicitly executed.
- [ ] Verify per-question-type stratification; never use a bare grouped `--limit` as representative evidence.

**Gate A:** no expensive canonical run until scope, harness, and evidence contracts are known-good.

## 5. Phase B — independent MVP audit

The audit is a code-and-contract review, not an E2E run. It asks whether the implementation can violate the supported contract even if the happy path passes.

### B1. Stdio/MCP boundary

- [ ] Tool registration and discovery match the MVP surface.
- [ ] Local stdio trust is explicit and does not accidentally depend on remote auth state.
- [ ] Tool schemas match callable backend signatures.
- [ ] Error responses remain MCP-safe and actionable.
- [ ] Hidden/remote-only tools do not become accidental MVP dependencies.
- [ ] Startup/shutdown cannot corrupt queue or graph state.

### B2. Memory ingest/recall

- [ ] Queue acceptance vs enrichment completion is unambiguous enough for a normal agent.
- [ ] Failed enrichment is observable and repair/retry behavior is bounded.
- [ ] Namespace boundaries apply to ingest, dedupe, enrichment, and recall.
- [ ] Reproduce or close #88 shared-`session_id` cross-namespace contamination.
- [ ] Review #70 remaining ingest-path failure/observability residuals against the release contract.
- [ ] Review #118 enrichment-state visibility against ordinary stdio UX.
- [ ] Review #119 flag/retention semantics if flagging remains supported in MVP.
- [ ] Confirm default-off typed-scalar/event lanes cannot silently affect ordinary MVP recall.

### B3. Code ingest / structure

- [ ] Local scan path uses the real identity/CAS path; deprecated remote payload behavior is not an MVP dependency.
- [ ] Partial/capped scans cannot silently delete valid indexed structure.
- [ ] Negative/empty structure answers carry coverage caveats.
- [ ] Local watcher behavior is safe for local roots.
- [ ] Concurrent local scan/write behavior cannot produce an accepted lost-update path under supported use.
- [ ] #99 project-name prune behavior is either fixed or explicitly bounded by the no-project-rename MVP limitation.
- [ ] #98 remote/deprecated payload path is classified outside MVP unless reachable from the local path.
- [ ] #104 remote-server local-path staleness behavior is classified outside MVP unless reproduced locally.

### B4. WorkArtifacts

- [ ] MCP tool argument ordering matches backend/repository ordering.
- [ ] Lifecycle transitions enforce the declared type/state machine.
- [ ] `supersede_artifact` moves status and relationship atomically and in the correct direction.
- [ ] source identity survives moves/reconciliation.
- [ ] artifact validation/audit stays read-only where documented.
- [ ] malformed/duplicate UUIDs fail safely.

### B5. TODOs

- [ ] add/list/get/close semantics are internally consistent.
- [ ] namespace/project/location fields cannot silently attach work to the wrong project.
- [ ] closing a TODO is idempotent or fails clearly.
- [ ] persistence and ordering are deterministic enough for agent use.

### B6. Graphiti fork packaging and compatibility

- [x] Publish the Archolith Graphiti fork wheel under its distinct distribution name, with native hooks present (`v0.30.2.post1` at `dbb0e33`, wheel SHA-256 `a748f98e0b09d64ab1eb29bd3449f52b552250a31663e86c4d99dc51ddf991f0`).
- [x] Lock Menhir to the public wheel with an exact version and registry hash, without a VCS dependency.
- [x] Confirm a fresh package install and no-publish container build resolve the same fork version without also installing upstream `graphiti-core` (pre-freeze evidence on `0f8deac5`; final RC repeat remains).
- [x] Exercise Menhir's fork-hook contract and representative memory ingest/recall against that build (pre-freeze evidence on `0f8deac5`; final RC repeat remains).

### B7. Persistence, failure and resource safety

- [ ] restart with READY memories preserves recall.
- [ ] restart with PENDING/PROCESSING work has a defined safe outcome.
- [ ] graph/schema bootstrap is idempotent on a fresh database.
- [ ] a failed provider call cannot create a false successful memory.
- [ ] bounded input limits exist for user-controlled payloads on required tools.
- [ ] logs/telemetry do not leak secrets or produce misleading healthy states.

**Gate B:** every HIGH/critical correctness or data-loss finding on a supported path is fixed or explicitly proven unreachable under the MVP contract. The audit artifact is COMPLETE before RC freeze.

**Gate B result:** COMPLETE on merged `0f8deac5`; see the [audit](../reviews/menhir-local-stdio-mvp-release-audit-2026-09-27.md) and its exact-main CI and no-publish image receipts.

## 6. Phase C — pre-freeze black-box E2E campaign

These tests must launch Menhir the way a real local MCP client does and exercise **stdio**, not call repositories/services directly.

Use a disposable state directory, fresh graph, and fixture repositories. Capture all MCP requests/responses plus server logs as test artifacts.

### E2E-1 — cold install and MCP protocol

- [ ] Create a clean Python 3.12+ environment.
- [ ] Install from the candidate package/build artifact, not an editable checkout.
- [ ] Provision/start the documented local graph/backend prerequisites.
- [ ] Launch the stdio MCP command exactly as user documentation specifies.
- [ ] `initialize` succeeds.
- [ ] `tools/list` exposes the intended MVP tools.
- [ ] Each required tool schema parses in a stock MCP client.
- [ ] graceful shutdown leaves no corrupt state.

### E2E-2 — memory lifecycle

- [ ] add a durable memory;
- [ ] observe accepted/processing/READY behavior;
- [ ] recall it by direct wording and paraphrase;
- [ ] build context from it;
- [ ] add a correction/update and verify current vs historical behavior claimed by the default runtime;
- [ ] verify provenance points back to the correct source episode/evidence;
- [ ] restart Menhir and recall the same memory again.

### E2E-3 — integrated coding workflow

Fixture repo must contain imports, callers, tests, an endpoint/dependency, and a small Git history.

- [ ] ingest the fixture project through MCP;
- [ ] query project/files/symbols/context;
- [ ] query blast radius / affected tests for a known changed file;
- [ ] assert expected caller/import/test relationships;
- [ ] assert an intentionally unindexed/unknown path returns a coverage caveat rather than a false-safe empty answer;
- [ ] add a memory with a Git diff touching the fixture;
- [ ] verify the memory is code-anchored and appears in code-context recall where supported;
- [ ] restart and repeat key structure queries.

### E2E-4 — WorkArtifact lifecycle

Use a fixture artifact corpus committed to Git.

- [ ] validate the corpus;
- [ ] read/list an artifact through MCP;
- [ ] link two artifacts with a supported relationship;
- [ ] transition one artifact through a legal state;
- [ ] reject an illegal state transition;
- [ ] supersede an old artifact with a new one and verify direction/status;
- [ ] move an artifact file with stable UUID, run audit/reconcile as required, and verify identity survives;
- [ ] restart and verify artifact relationships/status/source location.

### E2E-5 — TODO lifecycle

- [ ] add a repository-relative TODO;
- [ ] list/read it;
- [ ] verify location/project metadata;
- [ ] close it;
- [ ] verify closed/open filtering semantics;
- [ ] repeat close or invalid close and verify safe behavior;
- [ ] restart and verify persistence.

### E2E-6 — Graphiti fork cold-install compatibility

Use the same clean environment and fixture repository as E2E-1.

- [ ] install Menhir from the named candidate wheel in a clean environment and verify the exact public Archolith Graphiti wheel/version/hash;
- [ ] verify the upstream `graphiti-core` distribution is absent;
- [ ] start Menhir with the fork hooks wired, ingest one fixture episode, and recall it;
- [ ] build and start the release container from the same locked dependency set;
- [ ] verify both install paths expose the same fork version and hook behavior.

### E2E-7 — restart and interrupted work

- [ ] terminate/restart during or immediately after a queued enrichment;
- [ ] verify no false READY state;
- [ ] verify eventual READY, explicit FAILED, or documented recoverable state;
- [ ] restart with existing structure/artifacts/TODOs and verify all required surfaces recover;
- [ ] run the same fixture twice and verify idempotency where promised.

### E2E-8 — isolation/adversarial cases

- [ ] two namespaces with the same `session_id` and similar/identical content remain isolated;
- [ ] two projects with same filenames do not cross-link incorrectly;
- [ ] malformed artifact metadata fails without graph corruption;
- [ ] oversized memory/diff is refused/truncated exactly as documented;
- [ ] partial/capped code scan does not authorize destructive pruning of omitted files;
- [ ] stale or unknown code coverage cannot produce an unqualified “safe” result;
- [ ] an incompatible or missing Graphiti fork fails explicitly during startup or install;
- [ ] provider failure does not silently pass the E2E.

**Gate C:** all required happy paths pass; every negative test either passes or creates a release-blocking issue with a reproduced failure.

**Pre-freeze Gate C result:** PASS on the reviewed source tree: a clean Windows campaign recorded 13/13 tests and 71/71 criteria, while exact-merged Ubuntu stdio CI recorded 69/69 runnable criteria and the same-commit no-publish image verifier recorded the remaining 2/2 container criteria. The named frozen-RC rerun remains Phase F work.

## 7. Phase D — blocker fixes and RC freeze

- [ ] Fix all Gate B/C blockers.
- [ ] Re-run focused regression tests for every fix.
- [ ] Re-run the complete pre-freeze E2E campaign.
- [ ] Run the normal unit/integration/graph-backed suite and required CI checks.
- [ ] Ensure working tree is clean.
- [ ] Record exact Menhir commit as `MVP_RC_COMMIT`.
- [ ] Record exact package artifact/wheel identity/hash.
- [ ] Record the Archolith Graphiti fork wheel identity/hash and verify that the Menhir lock resolves it from the registry.
- [ ] Freeze relevant configuration/defaults.
- [ ] No unrelated merges until canonical evidence is complete.

**Gate D:** clean immutable RC candidate.

## 8. Phase E — canonical full Oracle LongMemEval campaign

LongMemEval and product E2Es answer different questions. LME measures memory quality; it does not certify artifacts, TODOs, code ingest, fork packaging, installation, or restart behavior.

### E1. Fresh build

- [ ] use a new isolated Neo4j container/volume;
- [ ] use the frozen RC commit and clean tree;
- [ ] ingest the full Oracle corpus (n=500);
- [ ] enforce strict zero failed-episode acceptance;
- [ ] run the harness acceptance validator;
- [ ] preserve manifest, provenance, telemetry, model/provider names, token counts, and graph identity.

### E2. Full deterministic retrieval gate

- [ ] run full n=500 Oracle IR gate;
- [ ] report Hit@3 support, MRR@10, explainability, and per-question-type results;
- [ ] compare against the same vector-only Graphiti baseline on the same graph;
- [ ] do not reuse the July 2026 result as current release evidence.

### E3. Full answer-quality / Mode-B evidence

The prior July full-500 result was retrieval-only. The benchmark inventory still distinguishes that from end-to-end memory-QA answer accuracy.

- [ ] verify the current Mode-B answer path is production-capable before canonical execution;
- [ ] run the full Oracle corpus through ingest/recall/answer scoring if the harness supports a valid canonical n=500 run;
- [ ] record answer model, judge/scorer, token usage, failures, and per-type accuracy;
- [ ] if the Mode-B harness cannot yet support a valid full canonical run, record that as an explicit release-evidence gap and decide before release whether it is a blocker; do not relabel the IR gate as answer accuracy.

### E4. Failure analysis

Every miss/failure is bucketed as:

- ingest/extraction;
- wrong subject/entity/namespace;
- temporal/update/currentness;
- graph/projection;
- retrieval/ranking;
- answer-generation/scoring;
- provider/operational;
- harness/matching-method artifact.

- [ ] publish aggregate and per-type buckets;
- [ ] open issues only for reproduced product/harness defects, not every benchmark miss;
- [ ] no tuning directly against individual held-out answers after the canonical run without declaring a new candidate/run.

**Gate E:** canonical results are tracked, reproducible, and honestly scoped. A FAIL triggers an owner decision or another RC cycle; it is never silently recalibrated after seeing the result.

## 9. Phase F — canonical E2E on the frozen RC

Re-run the complete E2E campaign from Phase C using the exact RC commit/package named by the benchmark evidence.

- [ ] E2E-1 cold install/protocol PASS;
- [ ] E2E-2 memory PASS;
- [ ] E2E-3 code ingest/structure PASS;
- [ ] E2E-4 WorkArtifacts PASS;
- [ ] E2E-5 TODOs PASS;
- [ ] E2E-6 Graphiti fork cold-install compatibility PASS;
- [ ] E2E-7 restart/interruption PASS;
- [ ] E2E-8 isolation/adversarial PASS.

**Gate F:** one frozen build passes the benchmark and the complete product acceptance pack.

## 10. Phase G — final issue sweep and release closeout

### Required decisions

- [ ] Review every open issue modified or discovered during this program.
- [ ] Record disposition against the MVP contract.
- [ ] No unresolved BLOCKER remains.
- [ ] Every DOCUMENTED LIMITATION appears in user-facing MVP docs.
- [ ] Post-MVP items remain open without delaying release.
- [ ] Update evaluation docs with new canonical benchmark evidence.
- [ ] Update MVP/README install and stdio instructions to match the tested path exactly.
- [ ] Document the Graphiti fork distribution, version, and supported upgrade path.
- [ ] Record exact release commit and evidence links.
- [ ] Transition this plan to IMPLEMENTED only after all mandatory gates pass.

## 11. Initial issue disposition hypotheses

These are starting hypotheses only; the audit/E2Es own the final ruling.

| Issue | Initial MVP treatment |
|---|---|
| #70 ingest residuals | AUDIT / likely fix-if-cheap; promote to blocker if E2E can lose required data silently |
| #88 cross-namespace shared session contamination | BLOCKER until reproduced closed or fixed |
| #92 duplicate Episodic/projection provenance | AUDIT; blocker only if it makes supported provenance materially wrong/ambiguous |
| #95 scalar perceiver legacy selector | POST-MVP if affected scalar authority stays default-off; blocker if required runtime depends on it |
| #98 deprecated remote payload CAS | POST-MVP if unreachable from supported local scan |
| #99 name-keyed structure prune / rename | DOCUMENTED LIMITATION or fix; project rename excluded for MVP |
| #100 historical erasure residue | Existing-operator-corpus decision; not a fresh local-MVP blocker unless package behavior is affected |
| #103 Explorer recall-lab redaction | POST-MVP (Explorer excluded) |
| #104 remote server stale-path behavior | POST-MVP if not reproducible in local topology |
| #106 LLM budget residual | RESEARCH / monitor during canonical ingest; blocker only if release run proves unsafe/unbounded behavior |
| #109 FalkorDBLite | POST-MVP |
| #110 MCP framework upgrade | POST-MVP unless current pin blocks stdio correctness |
| #111 remote hooks | POST-MVP |
| #112 hook packaging | POST-MVP |
| #116 effective scope receipt | FIX IF CHEAP / UX; blocker only if E2E shows scope ambiguity can mislead required flows |
| #117 bundled startup bootstrap | POST-MVP or UX enhancement |
| #118 enrichment state visibility | AUDIT; likely MVP UX/correctness requirement for ordinary add→recall flow |
| #119 derived retention protection | AUDIT; blocker if current flag/unflag behavior violates supported retention semantics |
| #39 change provenance brief | POST-MVP |

## 12. Release checklist — one-page view

### Scope
- [ ] local single operator
- [ ] MCP stdio supported interface
- [ ] memory
- [ ] WorkArtifacts
- [ ] local code ingest / structure
- [ ] TODOs
- [ ] Archolith Graphiti fork dependency
- [ ] explicit post-MVP exclusions documented

### Quality
- [x] independent release audit COMPLETE (Gate B, merged `0f8deac5`)
- [x] pre-freeze Gate B/C blockers fixed or disproven; frozen-RC review remains later
- [x] full normal test/CI suite green on merged `0f8deac5`
- [x] cold-package install E2E green on merged `0f8deac5`
- [x] stdio MCP E2E green on merged `0f8deac5`
- [x] restart/interruption E2E green on merged `0f8deac5`
- [x] isolation/adversarial E2E green on merged `0f8deac5`
- [x] Graphiti fork installs from a hashed public wheel on supported paths

### Benchmark
- [ ] fresh graph
- [ ] clean frozen Menhir commit
- [ ] full Oracle n=500 ingest
- [ ] zero failed episodes
- [ ] full Oracle IR gate recorded
- [ ] per-type breakdown recorded
- [ ] Mode-B answer-quality evidence recorded or explicit owner disposition recorded
- [ ] manifests/provenance/telemetry retained

### Release
- [ ] benchmark and E2Es name the same RC commit
- [ ] issue sweep complete
- [ ] limitations documented
- [ ] README/install path matches tested path
- [ ] Graphiti fork install docs match tested path
- [ ] evidence artifacts linked
- [ ] release tag/version cut only after all mandatory gates pass

## 13. Stop conditions

Stop and return to a new RC cycle if any of the following occurs:

- a supported operation can silently lose/cross-link user memory, code structure, artifact identity, or TODO state;
- namespaces cross-contaminate;
- a fresh install cannot reach the supported stdio surface using documented steps;
- restart produces false success or unrecoverable corruption on an ordinary supported flow;
- the canonical benchmark graph contains failed episodes or mixed source commits;
- canonical evidence was produced from a dirty tree or reused pre-existing graph without an explicit certified provenance contract;
- a post-freeze code/default change can affect the evidence already collected.

The goal is not zero open issues. The goal is a small, named product surface with strong evidence that the supported local workflow works end-to-end and fails honestly outside its bounds.
