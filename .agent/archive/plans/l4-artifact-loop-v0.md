# L4 artifact loop v0 — minimal safe slice (plan)

## Status

plan — agreed with ctharvey in a planning session (2026-06-28). **No code yet.** The next menhir step:
a minimal L4 institutional-artifact loop (Decision / Failure / Incident → evidence → CANDIDATE/TRUSTED →
written only via R9-lite → read by a MemoryOracle → surfaced in a tiny ColdStartBrief v0). Bench-first.
This is the first concrete slice of the L3/L4 GAP (see `docs/roadmap/l3l4-hybrid-sketch.md`,
`docs/research/process/research-vs-shipped-inventory.md`).

## Scope guardrails

```text
DO start with L4 institutional artifacts only: Decision, Failure, Incident.
DO reuse the existing CANDIDATE/review/conflict/decay machinery.
DO keep it bench-first (archolith_bench), port to menhir after the loop is proven.
DO NOT build a full ColdStartOracle, automatic LLM proposal, or L3 capabilities/policies.
DO NOT let LLM output become trusted fact. DO NOT claim broad novelty.
```

## Reuse / extension / new (from the prior-art audit)

```text
REUSE     candidate_repository (create_candidate scope='CANDIDATE', promote_candidate, reject),
          CandidateService.approve/reject (+ contradiction check); type accepts any string,
          memory_types.get_policy falls back to SEMANTIC; scope tiers; source_confidence;
          conflict pipeline (supersession); ANCHORED_TO (deterministic anchor); belief.py RecallBucket;
          the archolith_bench/oracle harness (OracleFinding, EvidenceRef, runner, validator).
EXTENSION three new `type` values (DECISION/FAILURE/INCIDENT, no new policy in v0); artifact-namespaced
          fields; an evidence-gate on the promote path.
NEW       first-class Evidence (model now, :Evidence node at menhir-port); ArtifactMutator (R9-lite,
          the single writer — consolidates create/promote behind the gate); MemoryOracle; ColdStartBrief v0.
```

## Confirmed decisions

```text
D1 Evidence       model-first in the bench (dataclass); :Evidence NODE when ported to menhir
                  (locked A-1). Not a loose note, not an ANCHORED_TO overload.
D2 TRUSTED (v0)   reuse `scope`: CANDIDATE=untrusted, PERSISTENT=TRUSTED/recallable. Clean status enum deferred.
D3 types          DECISION/FAILURE/INCIDENT as new `type` strings; reuse SEMANTIC policy fallback (no new policy v0).
D4 location       bench-first (archolith_bench), then a separate menhir-port commit.
D5 supersession   reuse the conflict-pipeline shape; supersedes/superseded_by; superseded -> historical, not deleted.
```

## Invariants (enforced by the ArtifactMutator + brief)

```text
1. Oracles never write (evaluate() is read-only).
2. Only the ArtifactMutator creates/promotes/supersedes artifacts.
3. TRUSTED requires >= 1 Evidence (fail closed).
4. LLM-sourced artifact cannot be TRUSTED on write — CANDIDATE + review only.
5. Human-sourced -> TRUSTED only with >= 1 evidence, else CANDIDATE.
6. Structural anchors are deterministic, never LLM-set.
7. Superseded -> historical, never deleted.
8. ColdStartBrief never promotes a hypothesis to fact (CANDIDATE -> hypothesis, with provenance).
9. The bench artifact model projects to the menhir Entity/Evidence schema (no divergence).
```

## Models (bench v0)

```text
Evidence(dataclass)   kind ∈ {git,test,user,log,agent_inference}; ref (anchor: file:symbol/commit/test id);
                      directness ∈ [0,1]; note? . Structural-kind evidence anchors are deterministic.
Artifact(dataclass)   id; type ∈ {DECISION,FAILURE,INCIDENT}; summary; body; status ∈ {CANDIDATE,TRUSTED};
                      source ∈ {human,llm}; evidence: list[Evidence]; anchors: list[str]; supersedes?;
                      superseded_by?; created_at.
```

## Phase 4 — first benchmark fixture

Target: "Does an agent avoid repeating a known failed approach when the L4 Failure artifact is present?"
Operationalized without a live agent: does the brief surface the failed approach + a corrective first action?

```text
task           "Recall is dropping low-cosine candidates — tighten the similarity floor."
gold FAILURE   (TRUSTED, human) "fixed 0.15 cosine MIN_SIMILARITY_THRESHOLD dropped BM25/facet
               candidates; replaced by source-aware floor"  evidence=[git:e8da67d, test:test_scoring_service]
               anchors=[scoring_service.py]
also in corpus a CANDIDATE LLM-proposed artifact (must surface as hypothesis, never fact);
               a superseded artifact (must be flagged historical); an evidence-less artifact (un-promotable);
               a DECISION + an INCIDENT for type coverage.
conditions     without_l4 (ordinary memories only) vs with_l4
```

Metrics (task-level, deterministic in v0):

```text
failed_approach_surfaced       gold FAILURE id in brief.failed_approaches            0 -> 1
evidence_present               every TRUSTED brief artifact has >= 1 evidence        1 (invariant audit)
stale_or_conflict_flagged      superseded/contested items carry historical label     1
decision_accuracy_per_token    gold-relevant artifacts surfaced / brief tokens       rises with L4
first_action_quality           recommended_first_action references the corrective,   0 -> 1
                               not the failed approach
```

Headline: the L4 Failure artifact flips `failed_approach_surfaced` and `first_action_quality` 0->1.

## Phase 5 — build plan (small commits)

Status: commits 1-5 DONE (bench, 28 L4 tests green) and commit 6 BUILT + logic-checked
(menhir port), all on branch `claude/menhir-chain-handoff-doc-7iuat2`. The demo fixture
reproduces the predicted headline: `failed_approach_surfaced`, `first_action_quality`,
`stale_or_conflict_flagged` flip 0->1 with L4 while `evidence_present` holds at 1. Commit 6
is split into 6a-6d below; it ships logic-checked (the full menhir pytest needs
httpx/graphiti, unavailable in the sandbox) and is confirmed against live Neo4j at home,
commit by commit, per **`.agent/plans/l4-commit6-live-verification.md`**.

```text
# bench-first (archolith_bench), runs here, zero production risk
1 Evidence + Artifact models     l4/models.py + tests          round-trip; enums                 trivial   DONE
2 ArtifactMutator (R9-lite)      l4/mutator.py + tests         invariants 2-7 fail-closed         low       DONE
3 MemoryOracle (read-only)       l4/memory_oracle.py + tests   task->artifacts; never writes      low       DONE
4 ColdStartBrief v0              l4/brief.py + tests           CANDIDATE->hypothesis; stale flag  low       DONE
5 fixture + runner + metrics     l4/runner.py, fixtures/       without vs with_l4 -> metric table low       DONE
                                 l4_failure_demo.json,
                                 scripts/run_l4_bench.py

# menhir port — split 6a-6d; logic-checked in sandbox, confirmed live at home (the only graph-schema change)
6a domain/artifacts.py           types + Evidence + R9-lite     pure-fn probe (inv. 3/4/5/7)       DONE (logic-checked)
                                 trust policy (decide_status,   tests/test_artifacts_domain.py
                                 can_promote, scope_for_status)
6b artifact_repository.py        Cypher writer: MERGE :Entity   Cypher-capture stub probe          DONE (logic-checked)
                                 on artifact_id; first-class    tests/test_artifact_repository.py
                                 :Evidence + SUPPORTED_BY;      (live: §6b of verification doc)
                                 promote (EXISTS-guard, inv 3); + MemoryGraphAdapter delegates
                                 supersede (SUPERSEDES, inv 7);
                                 find/fetch
6c artifact_service.py (R9-lite  facade routing + fail-closed   fake-adapter probe                DONE (logic-checked)
                                 promote/supersede) +           tests/test_artifact_service.py
                                 memory_oracle_service.py       (live: §6c of verification doc)
                                 (read-only, bench ranking)
6d live-verification checklist   l4-commit6-live-verification   walk at home, commit by commit    DONE (doc)
                                 .md + this status
```

## Owed / explicitly later

```text
- the real :Evidence node migration verification on live graph (commit 6, §2 constraints)
- a real ColdStartOracle (composite synthesis) — NOT this slice
- automatic LLM proposal — NOT this slice (proposer is a later emitter)
- L3 capabilities/policies — NOT this slice
- a real agent eval (vs the deterministic brief-surfacing proxy)
```
