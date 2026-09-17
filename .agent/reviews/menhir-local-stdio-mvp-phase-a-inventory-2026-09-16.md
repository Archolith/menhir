---
artifact_schema: 1
artifact_uuid: 490d95e3-dee2-49f7-8484-88e1dafb20d4
artifact_type: review
artifact_status: COMPLETE
---

# Menhir local-stdio MVP — Phase A issue and plan inventory

**Date:** 2026-09-16  
**Reviewed execution authority:** `.agent/plans/menhir-local-stdio-mvp-release-2026-09-16.md` (`dbde430c-9be1-4131-aa85-871a8e99e5ef`)  
**Purpose:** classify every open GitHub issue and every plan currently routed as active/partial/owner-decision against the approved local-stdio MVP contract.

## Result

The open backlog is much larger than the actual MVP critical path. After applying the approved product boundary, the current open issue census resolves to:

| Disposition | Count | Meaning for the MVP lane |
|---|---:|---|
| BLOCKER / release decision | 5 | Must be fixed, disproven, or explicitly de-scoped before RC freeze. |
| FIX IF CHEAP / verify | 5 | Bounded work or verification before freeze; escalate only on reproduced supported-path impact. |
| DOCUMENTED LIMITATION | 1 | Accepted only because the affected behavior is explicitly outside the supported MVP path. |
| POST-MVP | 20 | Do not spend MVP implementation budget unless a release E2E proves the issue is reachable from a supported path. |
| RESEARCH / measurement | 6 | Evidence work, not default implementation work for this release. |

This review opened **#120** because Beacon generation is in the approved MVP contract and no current Menhir generator implementation was found in `src/`.

## Release blockers / decisions

### #85 — LLM boundary hardening — BLOCKER

Stored memory content is interpolated into contradiction/identity/merge/repair prompts and those judgements can persist graph changes. This is on an ordinary durable-memory correctness boundary, not merely a research UI. The prompt-delimiting and response-parsing family must be repaired and covered before freeze. The out-of-range-index logging sub-item is lower severity but should land in the same bounded change.

### #88 — shared `session_id` cross-namespace contamination — BLOCKER

The standing reproducer shows one namespace can collapse enrichment in another when session ids collide. Namespace isolation is explicitly an MVP acceptance requirement. First action is the cheap regression reproducer on current main; if it still fails, fix the root cause and keep the reproducer as the E2E/graph-backed pin.

### #118 — enrichment state in the ordinary agent path — BLOCKER DECISION

The approved MVP contract requires an agent to distinguish accepted, processing, failed, and recallable memory. Menhir already has `add_memory_and_track`, so there are two valid release resolutions:

1. implement #118 so ordinary recall/context explains recent processing/failure; or
2. make the tracked-write path the documented canonical MVP path for recall-critical writes and prove it in E2E-2.

The RC cannot leave this ambiguous.

### #119 — derived retention protection / flag propagation — BLOCKER DECISION

`add_memory` exposes `flagged`, while current propagation can leave sticky entity flags and can affect automatic promotion/conflict behavior. Before RC either:

1. implement the reviewed provenance-derived protection design; or
2. explicitly remove/de-scope flag/unflag semantics from the supported MVP surface and docs.

Do not ship the current behavior as a claimed reliable retention contract merely because the default is `flagged=false`.

### #120 — generate a validated Beacon from an indexed local project — BLOCKER

This is missing MVP implementation work. Menhir must expose a local generation/refresh feature while Beacon remains authority for Beacon schema/build/validation. The generated artifact must pass Beacon validation and serve successfully through Beacon's own stdio MCP in E2E-6.

## Open issue disposition matrix

| Issue | MVP disposition | Reason / escalation trigger |
|---|---|---|
| #39 change provenance briefs | POST-MVP | New composition feature explicitly excluded from MVP. |
| #68 extraction-lab global monkey-patch race | POST-MVP | Explorer/lab surface excluded. |
| #69 dynamic Neo4j label in stale verification queries | FIX IF CHEAP / VERIFY | Run the exact queries on the RC Neo4j version. If they fail on the supported local stack, replace the dynamic label with the literal before RC. |
| #70 ingest-path residuals | FIX IF CHEAP / VERIFY | Heartbeat cleanup and loss visibility touch ordinary ingest. Fix bounded residuals that affect default ingest; event-fold-only behavior may stay post-MVP while default-off. |
| #71 Explorer event-loop stalls | POST-MVP | Explorer excluded. |
| #72 staged research/lab modules in `src` | POST-MVP | Packaging/maintenance cleanup; not required for supported behavior. |
| #73 god-file decomposition | POST-MVP | Architectural debt; broad churn is specifically undesirable before RC. |
| #77 default-silo scalar fallback | POST-MVP | Typed scalar authority remains default-off for MVP. |
| #80 bench-run Explorer robustness | POST-MVP | Explorer excluded. |
| #82 Explorer history re-redaction | POST-MVP | Explorer excluded. |
| #84 recall/performance cluster | RESEARCH / MEASURE | Single-operator MVP has no throughput claim. Retain measurement lane; escalate unbounded-queue behavior only if cold-install/E2E stress demonstrates a supported-path failure. |
| #85 LLM boundary hardening | **BLOCKER** | Durable memory content can influence persistent lifecycle/judgement decisions through an untrusted prompt boundary. |
| #86 decay/compress constant drift | FIX IF CHEAP | Currently dormant because lower policies are decay-exempt. Small defensive repair is welcome; not a release blocker absent a changed policy. |
| #88 cross-namespace shared-session contamination | **BLOCKER** | Direct namespace-isolation violation / enrichment loss. |
| #89 scalar `stated_span` semantic grounding | POST-MVP | Typed scalar authority default-off. |
| #91 dedupe prompt cache/cost | RESEARCH | Cost optimization; canonical run records spend but does not require this optimization. |
| #92 duplicate Episodic/evidence-projection nodes | FIX IF CHEAP / AUDIT | Decide whether twin MENTIONS are intentional. Escalate if Phase B/E2E shows user-visible provenance is ambiguous or incorrect. |
| #93 Graphiti top-15/cosine dedupe gate | RESEARCH | Fork payload / scale-quality research. Benchmark may expose severity; do not merge fork work for MVP by default. |
| #95 scalar perceiver legacy prefix | POST-MVP | Default-off scalar lane. |
| #98 deprecated remote structure payload CAS | POST-MVP | Remote/raw payload path excluded; local scan path carries the real claim generation. |
| #99 name-keyed structure prune / project rename | DOCUMENTED LIMITATION | Project rename/migration is explicitly unsupported for MVP. E2E must still prove ordinary same-name local re-ingest is safe. |
| #100 historical erasure-residue decisions | POST-MVP | Existing operator-corpus cleanup; erasure administration is not in the MVP capability contract. |
| #101 user entity extracted on assistant turns — rate unknown | RESEARCH | Measure on the pinned provider/model as part of quality characterization; promote only if the rate materially affects supported memory. |
| #102 `ADMITTED_ON` historical production follow-through | POST-MVP | Historical production-corpus evaluation/backfill; fresh local MVP graphs are tested independently. |
| #103 recall-lab query redaction | POST-MVP | Explorer/lab excluded. |
| #104 remote server cannot stat workstation roots | POST-MVP | Remote topology excluded. Local E2E must still verify local root freshness/watch behavior. |
| #105 audit-method reference issue | RESEARCH / REFERENCE | Durable method knowledge, not an actionable release defect. Use its test-trap rules during Phase B/C. |
| #106 LLM budget residuals | RESEARCH / MONITOR | Enforcement remains measurement-only. Canonical ingest telemetry will show real call distribution; do not turn enforcement on immediately before RC without new calibration. |
| #109 FalkorDBLite backend | POST-MVP | Embedded graph explicitly excluded; Neo4j/Docker accepted for MVP. |
| #110 MCP framework 0.3 upgrade | POST-MVP | Freeze current 0.2 pin unless E2E-1 proves current `tools/list` schemas fail in a stock MCP client. |
| #111 remote hooks vs production graph | POST-MVP | Remote hooks excluded. |
| #112 package hook installation | POST-MVP | Hooks optional and not an MVP install requirement. |
| #116 effective scope receipts | FIX IF CHEAP | Helpful UX/diagnostic protection. Escalate if E2E isolation shows a successful response can materially mislead an agent about the searched scope. |
| #117 bundled startup bootstrap | POST-MVP | Convenience/ergonomics; existing two-step path remains valid. |
| #118 enrichment state visibility | **BLOCKER DECISION** | Implement or make tracked writes the canonical documented MVP write path. |
| #119 flag/retention provenance | **BLOCKER DECISION** | Fix semantics or de-scope flagging from supported MVP. |
| #120 Beacon generation | **BLOCKER** | Required capability does not exist yet. |

## Active-plan routing under the MVP freeze

The current plan index contains legitimate execution authorities for work broader than this release. The MVP overlay below does **not** invalidate those plans; it prevents unrelated work from moving underneath the RC while release evidence is being collected.

| Current plan | MVP routing | Rationale |
|---|---|---|
| `menhir-local-stdio-mvp-release-2026-09-16.md` | **CONTINUE — release authority** | Owns the lane. |
| `menhir-deployment-control-plane-architecture-reset-2026-09-08.md` | HOLD / POST-MVP | Remote/deployment control plane excluded. |
| `menhir-feature-flag-registry.md` | HOLD implementation; USE inventory | The manual flag census is useful for freezing defaults, but building a new registry now is unnecessary runtime churn. |
| `menhir-core-promotion-restack-2026-08-31.md` | HOLD / POST-MVP | Research/core-promotion branch work is not needed by the local MVP; no stack merges during RC lane. |
| `menhir-research-execution-ladder.md` | HOLD / POST-MVP | Default-off research lane. |
| `menhir-work-artifact-reconciliation-2026-08-11.md` | **VERIFY REQUIRED SURFACE** | Phases 0-5 are already implemented and supply required WorkArtifact identity/reconciliation. Phase 6 legacy cleanup stays owner-gated/post-MVP. E2E-4 validates the shipped path. |
| `menhir-conflict-detection-signal-2026-08-09.md` | HOLD / POST-MVP | Advisory conflict-detection quality work; corrections/update behavior is tested black-box instead of taking on the whole redesign. |
| `menhir-conflict-suggestion-remediation-2026-08-09.md` | HOLD / POST-MVP | Conflict-review ergonomics not required by the MVP contract. |
| `menhir-intent-state-view-2026-08-08.md` | HOLD / POST-MVP | New/default-off semantic authority. |
| `menhir-namespace-contract-2026-08-09.md` | TARGETED ONLY | Do not implement the broad enumeration/cleanup plan. Pull the release invariant needed now: #88 isolation and supported namespace syntax in E2E. |
| `menhir-projection-realization-coverage-implementation.md` | HOLD / POST-MVP | Projection/default-off research reliability lane. |
| `menhir-unbounded-graph-writes-2026-08-09.md` | RECONCILE / MOSTLY POST-MVP | The diff-input gap was subsequently fixed by closed #115. Re-measure the remaining full-graph recount only if local E2E/startup shows a real MVP problem; do not implement batching speculatively. |
| `menhir-view-evidence-lifecycle-2026-08-28.md` | HOLD implementation; VERIFY DEFAULT PATH | Broad View lifecycle work is not an MVP expansion. Phase B must confirm default-off View lanes cannot leak stale/internal authority into required recall. |
| `menhir-mcp-snapshot-ingest-2026-09-16.md` | HOLD / POST-MVP | Explicitly remote snapshot transport. Local `ingest_project` is the supported MVP code-ingest path. |
| `menhir-artifact-semantic-model.md` | **VERIFY REQUIRED SURFACE** | WorkArtifact model/MCP surface is shipped. Remaining `CurrentPlanView` is not required for MVP. |
| `menhir-compositional-scalar-identity-2026-08-05.md` | HOLD / POST-MVP | Default-off scalar research. |
| `menhir-deterministic-first-event-scalar-2026-07-30.md` | HOLD / POST-MVP | Default-off event/scalar research. |
| `typed-recall-packet-prototype.md` | HOLD / POST-MVP | Prototype depends on admitted intent state. |
| `menhir-context-composition-production-integration.md` | HOLD / POST-MVP DECISION | Stage 1 was negative; no reason to resolve Stages 2-4 during MVP closure. |

## Phase A decisions now established

- Supported product scope remains: local single operator; MCP stdio agent surface; memory; WorkArtifacts; local code ingest/structure; TODOs; Beacon generation.
- Remote MCP/deployment, remote project snapshot ingest, hooks, Explorer, FalkorDBLite, project rename/migration, change briefs, and default-off scalar/event/intent authority are outside the MVP.
- `ingest_project` is the supported MVP code-ingest path; remote snapshot work is not a prerequisite.
- WorkArtifact core model and reconciliation phases 0-5 are treated as shipped substrate to verify, not new implementation scope.
- Project rename is a documented limitation; ordinary local re-ingest/idempotency remains required.
- Beacon generation is a new blocker tracked by #120.
- The current MCP framework pin remains frozen unless the cold-install stdio E2E proves it incompatible.
- Feature-flag defaults must be frozen and recorded before RC, but the new feature-flag registry implementation is not itself a release gate.

## Still open in Phase A

The inventory/classification step is complete, but Gate A is **not** complete. Remaining preflight decisions/work are:

1. settle #118: implement ordinary-path enrichment hints vs make `add_memory_and_track` the canonical recall-critical write path;
2. settle #119: fix retention protection vs explicitly de-scope flagging from the MVP;
3. choose and record the supported MVP platform set;
4. choose and freeze canonical provider/model defaults for ingest and benchmark evidence;
5. capture the exact default-off/default-on feature configuration used by E2E and LME;
6. define #120's command/MCP tool name and Beacon overwrite/update policy;
7. run the 5-item Oracle harness preflight and provenance/validator checks before any canonical full-500 run.

## Recommended execution order from this inventory

1. Build the #88 reproducer immediately; fix if still live.
2. Fix #85 as one bounded LLM-boundary hardening change.
3. Resolve the #118 and #119 product decisions before adding more wire contracts.
4. Design/implement #120 against Beacon's authoritative contract.
5. Run Phase B independent audit on the now-bounded surface.
6. Run the black-box pre-freeze E2E pack.
7. Freeze RC, then spend on canonical Oracle-500 evidence.
