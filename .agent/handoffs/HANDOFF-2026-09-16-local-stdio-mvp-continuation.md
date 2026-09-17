---
artifact_schema: 1
artifact_uuid: 26d9eafd-5ca6-4deb-a89c-39cdf6de32cd
artifact_type: handoff
artifact_status: OPEN
informs: dbde430c-9be1-4131-aa85-871a8e99e5ef
---

# Handoff: Menhir local-stdio MVP release continuation

**Date:** 2026-09-16  
**Repository:** `Archolith/menhir`  
**Primary execution authority:** `.agent/plans/menhir-local-stdio-mvp-release-2026-09-16.md`  
**Phase-A inventory:** `.agent/reviews/menhir-local-stdio-mvp-phase-a-inventory-2026-09-16.md`

## Session objective

Continue closing the approved Menhir MVP as a **local, single-operator product whose supported agent-facing interface is MCP stdio**. Do not let the release lane absorb the broader research/deployment backlog.

Required MVP capabilities are:

1. durable memory ingest + recall/context;
2. WorkArtifacts;
3. local code/repository ingest + structure queries;
4. TODOs;
5. Beacon generation/refresh for an indexed local project.

The release plan also requires an independent audit, black-box stdio E2Es, restart/adversarial E2Es, a frozen RC, and a fresh full-Oracle LongMemEval campaign.

## What was completed in the previous session

### 1. MVP release plan created and approved

Plan:

`.agent/plans/menhir-local-stdio-mvp-release-2026-09-16.md`

Artifact UUID: `dbde430c-9be1-4131-aa85-871a8e99e5ef`  
Status: `APPROVED`

Important decisions in that plan:

- local stdio is the supported MVP agent surface;
- local Neo4j/Docker and local backend processes may remain implementation details;
- memory, WorkArtifacts, local structure ingest/query, TODOs, and Beacon generation are in scope;
- remote MCP/OAuth deployment, hooks, Explorer, FalkorDBLite, remote structure snapshot transport, project rename/migration, change-provenance briefs, and default-off scalar/event/intent research are outside MVP;
- canonical benchmark and canonical E2Es must name the same clean frozen RC commit;
- LongMemEval proves memory quality only; it does not substitute for product E2Es.

Approval commit: `9e06b09e194be5fe46d8c53ffa7fbb1da5c7b987`.

### 2. Full issue + active-plan MVP inventory completed

Review:

`.agent/reviews/menhir-local-stdio-mvp-phase-a-inventory-2026-09-16.md`

Commit: `461f5fdda0f24889c4742b132da6a9102b00461f`

The open backlog was reduced to the actual release path:

- **5 blocker/release decisions**
- **5 fix-if-cheap / verify**
- **1 documented limitation**
- **20 post-MVP**
- **6 research/measurement**

Do not re-open the entire backlog. The inventory is the current MVP routing authority unless a supported-path E2E disproves a disposition.

### 3. Current blocker / decision set

#### #85 — BLOCKER

`LLM boundary hardening: unsanitized memory content into judge prompts, fragile first-token parsing, silent out-of-range fact drops`

Why it blocks MVP: ordinary stored memory content is interpolated into contradiction/identity/merge/repair LLM prompts, and those judgements can persist graph changes. This is not Explorer/research-only.

Expected bounded fix family:

- delimit/unambiguously mark memory-derived text as untrusted prompt data;
- harden verdict parsing (`CONFLICT:`, `SAME:`, etc. must not become silent `None`);
- log/report invalid fact indices rather than silently dropping them;
- add focused tests through the real bound call sites, not idealized helper-only stubs.

#### #88 — BLOCKER

`Cross-namespace session_id contamination: shared session_id collapses the second namespace enrichment`

Standing historical evidence says two namespaces with identical content and the same bare `session_id` caused the later namespace to collapse during Graphiti resolution.

A release-grade current-main regression test was added in the previous session:

`tests/test_cross_namespace_session_id_live.py`

Commit: `fe5e4c04a39e72185dbb8494d6afd661db168d69`

Run command:

```bash
pytest --run-online -m needs_llm tests/test_cross_namespace_session_id_live.py -s
```

The test deliberately requires:

- the real production composition root;
- disposable Neo4j on `:7688`;
- a real configured LLM/embedder;
- identical content in two different namespaces;
- the same `session_id` in both namespaces;
- namespace A fully READY before namespace B is queued;
- both namespaces to produce same-group `MENTIONS`/entity rows;
- zero cross-group `MENTIONS`.

**Important:** this test has NOT yet been executed with a live LLM in this session. Standard GitHub online CI explicitly runs `online and not needs_llm`, so a green normal CI run does not settle #88.

Do not claim #88 fixed or reproduced on current main until this test is actually run.

#### #118 — BLOCKER DECISION

`Make enrichment state explicit in the ordinary agent path`

MVP must make accepted/processing/failed/recallable state understandable to an agent.

Two acceptable release outcomes were recorded:

1. implement #118's bounded status hints in ordinary recall/context; or
2. explicitly make `add_memory_and_track` the canonical documented MVP path for recall-critical writes and prove that workflow in E2E-2.

Do not leave this ambiguous at RC freeze.

#### #119 — BLOCKER DECISION

`Derive retention protection from flagged episode provenance instead of propagating flags`

Current flag propagation can create sticky entity flags and entangle retention with promotion/conflict behavior.

Two acceptable release outcomes:

1. implement the reviewed provenance-derived retention design; or
2. remove/de-scope flag/unflag semantics from the supported MVP contract/docs.

Do not advertise current flagging as a reliable retention contract without settling this.

#### #120 — BLOCKER

`Generate a validated Beacon from an indexed local project for the stdio MVP`

Filed in the previous session because the capability is required by the approved MVP plan and no current Menhir generator implementation was found.

Boundary is important:

- Beacon owns its schema/build/snapshot contract;
- Menhir must not fork Beacon schema logic;
- Menhir should orchestrate/delegate through Beacon's authoritative compatibility surface;
- generated output must pass `beacon validate`, produce sensible `beacon inspect`, and start Beacon's own stdio MCP server;
- no unsafe overwrite of a hand-authored `beacon.yaml`;
- no invented project purpose/guardrails/concepts merely to fill fields.

Issue #120 contains the detailed acceptance criteria.

## #88 investigation state when the session stopped

Several hypotheses were checked statically.

### Confirmed Menhir behavior

`run_graphiti_extraction` resolves Menhir namespace to Graphiti `group_id` using `namespace_to_group_id`, then passes that `group_id` into `GraphitiClient.add_episode`. Candidate search in the pinned Graphiti node-resolution path also filters by each extracted node's `group_id`.

So the obvious "Menhir forgot to pass group_id" hypothesis is not supported by current source.

### Upstream Graphiti detour that was checked and closed

Pinned Graphiti 0.29.3 `Graphiti.add_episode()` contains code/comments suggesting a non-default `group_id` may clone the driver to a database of that name. This initially looked dangerous for Neo4j Community.

However, at the exact pinned upstream code, base `GraphDriver.clone()` returns `self`, and `Neo4jDriver` does not override `clone()`. Therefore that branch is effectively a no-op for Neo4j at this pin. Do **not** spend another session rediscovering or treating that comment as the #88 root cause without new runtime evidence.

### Still plausible #88 loci

The historical root cause remains unpinned. The useful next move is to **run the focused live regression first** and instrument only if it fails.

If it fails, inspect in this order:

1. Graphiti combined-extraction receipt before/after dedupe: extracted nodes/edges, resolved UUIDs, dropped orphan endpoints;
2. semantic candidate sets returned for namespace B — verify every candidate `group_id` equals B;
3. resolved node UUID/group pairs before `_sanitize_combined_payload` / orphan pruning;
4. episode association/name/session-derived state where `episode-{session_id}-{uuid}` is used;
5. any ContextVar/global cache keyed without `group_id` across the combined extraction/dedupe monkeypatches;
6. only then consider namespace-qualifying `session_id` at the Menhir boundary as a belt-and-suspenders fix.

The archolith-bench workaround historically namespace-qualified session IDs, but moving that into Menhir should follow evidence, not be assumed to be the root fix.

## Active-plan freeze routing

The Phase-A inventory already classified active plans. Preserve these holds during the MVP lane:

- deployment control-plane reset — HOLD / post-MVP;
- feature-flag registry implementation — HOLD; use its inventory only;
- core-promotion stack — HOLD;
- research execution ladder — HOLD;
- conflict signal/suggestion plans — HOLD;
- intent/scalar/event/typed packet plans — HOLD;
- MCP snapshot ingest — HOLD (remote transport; local `ingest_project` is MVP path);
- broad namespace-contract plan — TARGETED ONLY (#88/invariants, not full cleanup/enumeration);
- FalkorDBLite — post-MVP;
- MCP framework upgrade — freeze current pin unless cold-install E2E proves incompatibility.

WorkArtifact semantic model and reconciliation phases 0-5 are already shipped substrate. Verify them through E2E-4 rather than reimplementing them. `CurrentPlanView` and legacy Phase 6 cleanup are not MVP requirements.

## Near-term execution order

Continue in this order unless the live #88 result changes the path:

1. **Run `tests/test_cross_namespace_session_id_live.py` with real LLM + disposable Neo4j.**
   - If PASS: attach evidence to #88 and reassess whether historical defect is already gone on current pin/main.
   - If FAIL: instrument the narrow resolution path above, fix, rerun, then keep the test as a release pin.
2. **Fix #85** as one bounded LLM-boundary hardening change with focused tests.
3. **Resolve #118** product decision.
4. **Resolve #119** product decision.
5. **Design/implement #120 Beacon generation** against Beacon's own authoritative API/contract.
6. Run the independent Phase-B MVP audit.
7. Build/run the black-box stdio E2E pack.
8. Fix any supported-path blockers, rerun E2Es, then freeze RC.
9. Only after RC freeze, run fresh Oracle-500 canonical evidence and final frozen-RC E2E.

## Phase A items still not closed

The issue/plan inventory is complete, but Gate A still needs:

- supported MVP OS/platform set;
- canonical provider/model defaults for ingest and benchmark;
- exact default-on/default-off feature configuration recorded;
- #118 decision;
- #119 decision;
- #120 command/tool name and overwrite/update policy;
- a 5-item Oracle harness preflight with fresh-graph/clean-commit provenance and acceptance validator checks.

Do not start the expensive full-500 run until those are settled.

## Key release evidence rule

The final release must have one clean immutable `MVP_RC_COMMIT` such that:

- canonical LongMemEval evidence names it;
- canonical cold-install/stdin MCP E2Es name it;
- artifact/TODO/code/Beacon/restart/adversarial E2Es name it;
- any post-freeze code/default change that can affect an already-tested surface triggers the appropriate rerun.

The objective is **not zero open issues**. It is a small product surface with strong evidence that its supported local workflow works and fails honestly.
