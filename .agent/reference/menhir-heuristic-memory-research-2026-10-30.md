---
artifact_schema: 1
artifact_uuid: fa523efe-1bf6-42ed-ac0b-8aab187f9423
artifact_type: investigation
artifact_status: COMPLETE
---

# Heuristic Memory Detection and Recall for Menhir

**Research and experimental design — 30 October 2026**  
**Status:** Proposal only; no implementation or production changes  
**Scheduling:** Post-MVP research; outside the current MVP scope and launch gates.  
**Reference use:** Retained for future post-MVP memory-detection planning and experiment design. No active implementation consumer; this document does not authorize implementation.  
**Source baseline:** [`Archolith/menhir` `main` at `0e07898361fb0d6bebf36389c7c02f9aede9cea5`](https://github.com/Archolith/menhir/tree/0e07898361fb0d6bebf36389c7c02f9aede9cea5) (29 October 2026 UTC)  
**Question:** What techniques from high-quality antivirus heuristic engines can improve Menhir's ability to notice, retain, and later retrieve consequential information?

## Executive assessment

The useful analogy is not “identify bad memories.” It is **detect consequential patterns from imperfect signals, investigate them proportionately, and take an action appropriate to the evidence and the cost of a mistake**. Antivirus engines distinguish cheap static signatures, behavioral sequences, anomaly signals, reputation, deeper investigation, and enforceable policy. An analogous memory system should distinguish *capture*, *importance detection*, *truth/admission*, *retention*, *current-state derivation*, and *query-time relevance*.

Menhir is not starting from zero. The examined revision already has deterministic TurnEvidence triage, source-linked episodes, candidate review, trust and namespace controls, conflict handling, temporal assertions and Views, a multi-source recall pipeline, and recall-usefulness telemetry. Importantly, its research notes have already established two sound principles: **probabilistic perception with deterministic folding**, and **hard trust/currentness gates rather than laundering authority through retrieval scores**. The proposal should extend those principles, not replace them.

### Highest-value opportunities

1. **Consequential transition detection:** identify *proposed → decided*, *working → broken*, *assumed → disproven*, and *open → resolved* across multiple turns; preserve the supporting chain and rationale.
2. **Behavioral sequence detection:** recognize patterns (repeated failed approach, narrowing alternatives, emerging constraint) that no single utterance explains.
3. **Retroactive significance:** re-evaluate already-authorized, retained evidence when a later decision or incident makes earlier context newly useful.
4. **Cost-of-forgetting heuristics:** prioritize misses likely to cause repeated work, broken constraints, or costly wrong answers, while keeping this signal separate from factual authority.
5. **Outcome-based evaluation:** use actual missed-answer and unnecessary-recall cases to evaluate detectors in shadow mode; do not promote memory merely because an agent rated its own retrieval “useful.”

**Recommendation:** Begin with a read-only *transition detector* and evidence packet built over existing captured episodes / TurnEvidence, plus a replayable evaluation corpus. Do not start with an automatic permanent-retention scorer, a new global ranking formula, or an always-on LLM pass over every conversation.

---

## 1. What exists in Menhir today

This inventory is grounded in the named source files at the pinned commit, not an assertion about any particular live deployment. A feature being present in code is not proof that it is active in production. Historical research documents and the current activation ledger occasionally describe different defaults; runtime settings and deployment receipts must be checked before activation.

| Area | Verified baseline | Implication for the research |
|---|---|---|
| Cheap capture signatures | Shared hook core uses deterministic phrase, number, money and date signals; adapters have parity tests. It strips known host-injected blocks before claiming `declarant=user`. [M1] | Extend this as a cheap *candidate discovery* layer, not a verdict of long-term importance. Broad triggers such as any number admit benign noise. |
| Evidence and source attribution | `TurnEvidence` is distinct from ordinary recalled memory. Hooks do **not** retain every prompt; assistant/tool turns and full transcripts are excluded by that producer contract. [M2] | A retrospective detector cannot inspect turns that were never captured. Address this honestly, without silently broadening surveillance. |
| Quarantine / review | `CANDIDATE` memories are withheld from normal recall; approval transitions to `PERSISTENT` and attempts a conflict check. [M3] | Reuse review semantics. A heuristic “flag” should *not* automatically call permanent `flag_memory`, promotion, or approval. |
| Semantic retrieval | Recall has candidate acquisition, graph/file context, score composition and currentness rules. `ScoringService` adds semantic similarity, adjacency, recency, graph prominence, conflict and type policy. [M4, M5] | Importance-at-write and relevance-at-read should remain different products. Avoid adding an uncalibrated bonus to a mixed-scale score. |
| Specialized relevance signals | Semantic, structure, scope, temporal, evidence and intent oracles exist; the evidence oracle explicitly avoids conflating provenance strength with relevance. [M6] | Reuse oracle contracts for read-side experiments; keep source/trust eligibility in policy gates. |
| Source-memory path | The activation ledger records an additive source-memory lane and measured benefits on specific LongMemEval/AMA-Bench configurations. [M7] | Preserve raw/source-backed recall as a control and fallback. A new derived state must beat this baseline, not merely an old vector-only baseline. |
| Scalar and temporal states | TurnEvidence-driven scalar consolidation, immutable typed assertions and deterministic current/history Views are implemented. Event-history paths also appear in the activation ledger. [M8, M7] | Reuse fold, lineage, bitemporal and repair machinery for *qualified* transition projections; do not invent a parallel authoritative state engine. |
| Lifecycle / retention | Session consolidation uses flagged, edge and distinctiveness signals. Irreversible decay deletion is deliberately disabled after documented scale-related data loss. [M9] | Retention changes require substantially stronger proof than harmless diagnostic annotations. No importance-based deletion in the initial program. |
| Feedback | `rate_recall` stores `useful`, `partial`, `noise`, or `unused` on an operational receipt, intentionally **without modifying heat, ranking, or promotion**. [M10] | Use feedback to build offline evaluation sets, not a direct online reinforcement loop. |
| Evaluation | Repo policy demands recorded datasets, exact code/graph state, baselines and activation decisions; prior retrieval experiments were neutral or negative. [M11] | Shadow → compare → canary → owner decision. Treat answer-level quality and false promotions as release gates. |

The largest *unproven opportunity*, rather than a verified absence, is a governed mechanism that turns **multi-turn semantic developments into evidence-backed decision/incident/resolution state** and uses later events to reconsider what earlier evidence matters. Existing institutional artifacts and Views may already provide much of the destination schema; inspect their actual writer and read contracts before adding a new node type.

## 2. The antivirus-to-memory transfer map

The 13 ideas from the brainstorming session are mapped below. “Existing” means the mechanism has a directly relevant foundation; it does **not** mean the proposed behavior is already solved end to end.

| # | Heuristic idea | Existing Menhir foothold | Research increment | Priority |
|---:|---|---|---|---|
| 1 | **Semantic signatures** | Deterministic phrase triage; typed extraction | Detect explicit decision, constraint, correction, commitment, failed approach and discovered cause with precise spans; distinguish assertion from quote, question and hypothetical | P1 |
| 2 | **Behavioral heuristics** | Episodes, TurnEvidence, temporal Views, work artifacts | Detect multi-event patterns across a bounded window; require same subject/project and coherent chronology | **P0** |
| 3 | **Anomaly detection** | Belief/supersession and temporal comparisons | Treat unexpected change as an *investigation hint*, not evidence of truth or importance by itself | P2 |
| 4 | **Cost-of-forgetting** | Type policy, recall receipts, code impact graph | Estimate *regret if unavailable* using potential consequence and recoverability; never let this override trust | P2 |
| 5 | **Quarantine** | `CANDIDATE` workflow | Introduce separate non-authoritative `heuristic_finding` / review hint; retain the current operator approval boundary | P0 (reuse) |
| 6 | **Retroactive detection** | Versioned consolidation/replay, provenance, raw episodes | On a new decisive event, revisit bounded, authorized predecessors and attach new *derived relationships* with reason receipts | **P1** |
| 7 | **False-positive suppression** | Trust/declarant checks, self-binding, conflict policy | Explicit negative cases: quotation, negation, hypothetical plans, sarcasm, restatement, agent-generated source text | P0 |
| 8 | **Importance vs. relevance** | Separate lifecycle and recall services | Define separate independent decisions for capture, retention and query-time selection; measure each stage separately | **P0** |
| 9 | **Poisoning detection** | Provenance, declarant, scope, user/agent tiers | Enforce source-authority matrix; detect source/role laundering and model-generated self-confirmation | **P0** |
| 10 | **Feedback learning** | Operational `rate_recall` receipts | Offline error taxonomy and human/outcome-adjudicated labels; no auto-rank update from self-report | P1 |
| 11 | **Memory immune system** | Producer → ingest → governance → recall services | Coordinate as a staged policy architecture rather than a single huge model or blended score | P1 |
| 12 | **Dynamic importance** | Access/decay policy, currentness, historical Views | Recompute *review priority* and *context relevance* on events; never silently revoke protected retention | P2 |
| 13 | **Consequential transitions** | Typed folds, historical views, artifacts | First-class **evidence-backed change events** preserving before/after, cause, time, status and source chain | **P0** |

The priorities reflect **experimental leverage**, not proven product benefit.

## 3. A proposed detection architecture

### 3.1 Separate the questions explicitly

For evidence item *e*, later query *q*, and time *t*, avoid one universal scalar. Maintain distinct answers:

1. **Capture eligibility:** May this source legally and operationally enter the evidence store? Is the declarant known?
2. **Signal detection:** Does the evidence suggest a decision, correction, failure, transition, or similar event?
3. **Admission and authority:** Is the assertion sufficiently grounded to become a governed fact or View? Is human approval required?
4. **Retention:** Must the source remain available, and for how long, under policy?
5. **Recall eligibility:** Is this source in scope, current for the query, and allowed to be shown?
6. **Relevance:** Among eligible candidates, which answer this particular query under the token budget?

The signal engine may compute a *review priority*. It must not use that number to make an untrusted statement authoritative. In particular, high predicted cost-of-forgetting must never rescue a provenance failure.

### 3.2 Staged processing

```text
Authorized source / TurnEvidence / episode
    ↓
Cheap signature pass (deterministic, bounded)
    ↓
Candidate finding + exact source spans
    ↓
Behavior/transition correlator (bounded same-scope window)
    ↓
Evidence check: speaker, intent, time, contradiction, independence
    ↓
       ├─ Rejected/irrelevant → reason receipt only
       ├─ Ambiguous          → provisional finding / operator review
       └─ Supported          → proposal for existing governed assertion/artifact/View path
    ↓
Deterministic fold / current and historical projection (if admitted)
    ↓
Existing gated recall + raw source fallback
    ↓
Outcome evaluation (offline, no automatic authority changes)
```

**Detection should be cheap; investigation should be selective.** Antivirus sandboxes do not need to run for every harmless file. Likewise, Menhir should reserve any LLM judgment for ambiguous multi-event clusters, not call a model on every prompt simply to assign importance.

### 3.3 Minimal new research object

Prefer an explainable, non-authoritative finding record before designing a new durable memory node:

```yaml
HeuristicFinding:
  finding_id: stable_id
  namespace: exact_scope
  subject_key: nullable
  signal_kind: decision|constraint|failure|correction|transition|...
  rule_id: versioned_detector_id
  evidence_refs: [source_id_and_exact_span]
  event_time: nullable_world_time
  observed_at: system_record_time
  proposed_before: nullable
  proposed_after: nullable
  disposition: observe|review|rejected|admitted
  disposition_reason: machine_readable_code
  proposal_hash: deterministic_replay_identity
```

This object is **a diagnostic proposal, not a new authoritative `TypedAssertion`**. A later approved projection should point to its original evidence, not rely on the finding's confidence as proof. The schema above is illustrative and should be reconciled with existing artifacts, assertion storage and erasure contracts before implementation.

## 4. The most promising detectors

### A. Consequential transition detector (first experiment)

Input: a bounded series of same-subject statements, with timestamps and evidence references.

Output: a proposed transition, e.g.:

```text
10/01  "We may replace the queue."             → PROPOSED
10/05  "The alternative cannot meet ordering." → CONSTRAINT/DISQUALIFIER
10/08  "We will keep the current queue."       → DECIDED
10/20  "The queue crashed in the new release." → INCIDENT (not a decision reversal)
```

The detector must not simply infer that the final incident invalidated the earlier architecture decision. It should distinguish a *new incident* from a *policy reversal* and maintain separate timelines. A good state change record answers **what changed, when, why, who said it, and which previous state it superseded**.

Key vetoes: hypothetical speech, different project, third-party quote, missing subject, conflicting chronology, weak speaker attribution, and unsupported causal inference.

### B. Behavioral sequence detector

Patterns to look for:

- **Converging choice:** alternatives examined → eliminated → accepted.
- **Repeated failure:** same attempt repeated without new conditions → intervention needed.
- **Constraint emergence:** a one-off concern becomes a binding project requirement.
- **Resolution:** an issue with explicit reproducible failures gains an observed passing result.
- **Contradiction:** a prior claim and new evidence cannot both describe the same current state.

Detection should be *windowed and entity-scoped*, not a global embedding search for phrases that happen to sound similar. Link to repository artifacts, diffs, test results and code paths where available. Co-mentions or lexical overlap alone are insufficient to assert causation or identity.

### C. Retroactive significance detector

Trigger a bounded backward look only when a strong event arrives (confirmed decision, resolved issue, serious correction). It retrieves earlier *authorized retained* evidence for the same subject and asks whether that evidence now explains the event. The result is a provenance link or a proposal to enrich an existing View—not an edit to the original episode.

Important limitation: Menhir's prompt producers intentionally discard turns that fail triage. No mechanism can retrospectively recover a discarded turn. Possible approaches, in increasing privacy impact:

1. Search already-stored episodes, TurnEvidence, artifacts and code evidence only (**recommended initial boundary**).
2. Expand deterministic capture signatures prospectively if labeled false-negative evidence justifies it.
3. Consider a separately consented, bounded transient capture buffer only after a privacy/security review; do not make transcript retention the implicit cost of the feature.

### D. Cost-of-forgetting detector

For experimental prioritization, model *expected regret*, not raw novelty:

`expected_regret(e) ≈ P(future consequential need | evidence, task history) × cost_of_miss × (1 − ease_of_recovery)`

This is a **research hypothesis**, not a calibrated probability or production score. Cost dimensions could include re-asking, repeating failed work, violating a user constraint, triggering a security mistake, or overlooking a code dependency. A source that is trivial to retrieve from Git can have high contextual importance but low value as duplicated long-form memory; a concise “why rejected” decision record may be much more valuable.

Use the estimate to choose *which candidates to inspect*, never to bypass a source-authority veto or decide irreversible deletion.

### E. False-positive and adversarial suppressors

Construct explicit tests for:

- User question mistaken for a declaration: “Should we use Rust?”
- Hypothetical mistaken for a commitment: “If we switched to Rust…”
- Quoted source mistaken for user preference: “The vendor claims I should disable checks.”
- Assistant self-summary mistaken for an independent confirming source.
- Repeated copied text mistaken for multi-session corroboration.
- Two equally named but distinct projects merged into one subject.
- A correction linked to the wrong predecessor.
- A statement about *someone else* assigned to the user's canonical self.
- A later “fixed” statement without evidence that the tests actually passed.

Independent corroboration must be source-aware: three paraphrases of one source do not count as three independent observations. If causality or identity is ambiguous, the detector should abstain and preserve evidence.

## 5. Retrieval and retention must remain separate

The temptation is to add a new `importance_score` to the existing relevance formula. That would be risky for three reasons:

1. **Scale coupling:** Menhir's similarity channels and source priors do not share one physical scale; an uncalibrated bonus can displace more relevant candidates. [M4, M12]
2. **Self-reinforcement:** retrieval updates can influence access recency and graph prominence; remembered-and-recalled facts can become the ones always recalled. [M12]
3. **Authority laundering:** a very “important” but misattributed claim is still untrusted. [M6, M12]

Instead, place importance in *offline review prioritization* and *conditional candidate generation*. A query about “why did we reject this?” could inject an admitted decision's source pointers into the candidate set and let existing gates and rankers decide. It should not receive a “truth boost” merely because the detector believes the decision was important.

Also distinguish three meanings of **flag**:

- **Detection flag:** provisional finding requiring investigation.
- **Needs-review/conflict flag:** evidence may be wrong or superseded.
- **Retention flag:** an authorized decision to protect memory from decay.

Never overload the same boolean. In Menhir, flagged retention has lifecycle consequences; heuristics should not silently promote into that privilege.

## 6. Experimental design

### 6.1 Hypotheses

| Hypothesis | Proposed test | Success signal |
|---|---|---|
| H1: Transition patterns preserve more useful context than isolated phrases. | Replay multi-session decision/incident histories with and without transition proposals. | Better answer correctness on “what changed/why?” and fewer unsupported state claims. |
| H2: Backward linking recovers otherwise overlooked rationale. | Replay a decisive event, then run bounded retrospective evidence association. | Higher supporting-evidence recall without a meaningful rise in false links. |
| H3: Explicit suppressors cut harmful promotions. | Adversarial set of quotes, hypotheticals, copied summaries and entity collisions. | Lower false-admission rate without excessive true-decision loss. |
| H4: Regret-driven review triage is a better use of budget than novelty. | Hold model calls/review count fixed; compare regret, novelty and random candidate queues. | More consequential future answers supported per 100 reviews. |
| H5: Feedback diagnoses errors without creating rank loops. | Compare offline incident labels with operational recall ratings. | Useful calibration and taxonomy while production ordering remains unchanged. |

### 6.2 Baselines and slices

Run against the *same frozen source corpus*, graph state, commit, embedding/model versions and retrieval settings:

- **B0:** current Menhir behavior, including source-memory lane and active policy guards.
- **B1:** cheap signatures only, observe-only.
- **B2:** signatures plus behavioral/transition grouping, observe-only.
- **B3:** B2 plus bounded retroactive linking.
- **B4:** B3 plus selective semantic review, under a fixed LLM cost budget.

Use a held-out, labeled corpus containing: project decisions, constraints, incidents, reversals, ordinary small talk, repeated quotes, false evidence, multi-project collisions, implicit changes and unresolved uncertainty. Include negative and abstention questions. LongMemEval's multi-session, knowledge-update, temporal and abstention slices are useful general checks, but an **oracle-only** run is not a substitute for end-to-end haystack ingestion or a code-agent task corpus. [M11, P2]

### 6.3 Metrics that actually matter

**Write/detection:** event-level precision, recall and F1 by signature; false *authoritative* promotion count (hard guardrail); correct speaker/subject assignment; source-span support; false transition-link rate; missed high-regret items; duplicate and repeated-source inflation; review backlog.

**Read/answer:** answer correctness with citations to evidence; evidence hit@k and rank; correct abstention; stale-before-current errors; wrong-project leakage; re-asking/rework events; tokens injected per useful answer; recall latency p50/p95.

**Cost/reliability:** model calls per 100 turns, cost per independently useful finding, replay determinism, crash/retry idempotence, namespace-scope enforcement, evidence erasure completeness, and proportion of captured evidence that is ultimately reachable. Do not train and test on the same conversations or silently count a self-produced answer as independent ground truth.

### 6.4 Evaluation protocol

1. Freeze labeled source streams and a current B0 run.
2. Run detectors in **shadow mode**; record reason-coded candidate findings without mutation to authoritative graph state.
3. Have human or independently grounded adjudication label the costly false positives and missed consequential events.
4. Replay with B1–B4 against the same source stream; compare both detection and downstream answer metrics.
5. Use paired per-question fixed/broken analysis, uncertainty intervals, and a blinded held-out set before claiming lift.
6. If positive, stage a tiny, reversible canary behind a flag and operator approval. Confirm rollback and erasure, then reevaluate against a fresh corpus.

**Release criterion:** no regression in provenance, scope, currentness or erasure invariants; no unsupported authoritative promotions; statistically credible improvement in answer-level usefulness at a documented latency/cost ceiling. A lift in “number of things remembered” alone is not a win.

## 7. Roadmap and integration plan

| Phase | Deliverable | Risk | Exit gate |
|---|---|---|---|
| **P0 — Inventory + labels** | Formal event taxonomy, authority matrix, 100–300 labeled dialogue sequences, current-source accounting, baseline metrics | Low | Agreed negative controls; no uncertain claim about live flags |
| **P1 — Shadow transition scanner** | Pure detector over retained evidence; exact source spans and reasons; same-scope bounded windows | Low–moderate | Precision and confusion matrix; zero authority writes |
| **P2 — Retroactive correlation** | Event-triggered predecessor search, provenance links and replay-safe idempotency; bounded work | Moderate | False-link rate and cost ceiling; correct current/history distinction |
| **P3 — Review and projection** | Adapt approved findings to existing assertion/artifact/View route; operator UI and repair/erasure support | High | No authority-bypass, no lost source or irreversible merge |
| **P4 — Feedback/triage tuning** | Outcome-labeled error dataset, selective deeper inspection, canary and model-comparison process | Moderate | Held-out answer-level lift, stable latency and no self-reinforcement |

The first code exploration should identify the narrowest common surface that observes accepted TurnEvidence and regular episodes **without changing the producer contract**. The second is a read-only correlator. Both should produce operator-inspectable receipts. There is no justification yet for a separate database, a new memory type, broad producer capture, a cross-session always-on LLM, or a ranking-model rewrite.

## 8. Open design questions

1. **What qualifies as a transition?** Only explicit user commitments, or also verified test/CI outcomes and signed operator artifacts? Proposed default: typed event-specific authority rules.
2. **What is the analysis window?** Per project, subject, time range and source type; how to prevent arbitrary truncation or cross-project bleed?
3. **How should a negative finding persist?** A suppressed duplicate or rejected hypothetical may need a compact reason receipt to prevent repeated costly re-detection, but should not become a recalled “fact.”
4. **Which state owner should receive a confirmed decision?** Existing WorkArtifact, TypedAssertion/View, or a new projection? Decide after inventorying current writer contracts; avoid parallel truths.
5. **What feedback is independent?** Human correction, CI result, proven source use, or task success may be stronger than agent self-rating. How can they be collected without introducing new privacy risk?
6. **How is importance calibrated?** Use future-error costs and review budgets rather than an absolute 0–100 score. Define a safety-weighted loss matrix before optimizing thresholds.
7. **Which facts should stay dormant?** Protected facts might be permanently retained but rarely injected; archival status, recall visibility and retention protection should be independently controllable.
8. **What does erasure mean for findings?** A derived signal must be removed or invalidated when its source is erased; no unaccounted shadow copies in telemetry or replay logs.

## 9. Overall recommendation

The most promising transfer from antivirus engineering is **behavioral detection plus staged investigation**, not generic anomaly scoring. Make Menhir recognize a meaningful *change in understanding* and preserve the evidence trail that explains that change. Then add *retroactive correlation* so later outcomes can make earlier evidence discoverable.

Keep the hard security and epistemic boundaries: a heuristic can suggest that something is consequential; it cannot certify the source, create authority, or override a disagreement. Evaluate whether this produces better future answers, fewer repeated mistakes, and fewer false assertions—under a frozen baseline and a bounded cost budget.

**Suggested next research experiment:** 150 multi-turn labeled sequences; one deterministic transition candidate detector; a read-only evidence-linking pass; and paired B0/B2 answer tests. The falsifiable question is simple: **Does explicit detection of consequential transitions improve source-supported answers enough to justify its capture, compute and review costs?**

---

## Sources and verification notes

All Menhir file links below point to the inspected revision, not the moving `main` branch. Source files and repository documentation are the basis for “existing” claims; proposals and priority estimates are author analysis. Some descriptions of flags in older docs conflict with the newer feature ledger or code; nothing here certifies a live deployment's exact settings.

### Menhir primary sources

- **[M1]** [Shared deterministic TurnEvidence hook, including triage and host-block stripping](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/scripts/hooks/menhir_turn_evidence_common.py).
- **[M2]** [TurnEvidence producer contract](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/docs/turn-evidence-producers.md).
- **[M3]** [`CandidateService` approval/rejection policy](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/services/candidate_service.py).
- **[M4]** [Pure candidate scoring and relevance inputs](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/services/scoring_service.py).
- **[M5]** [Recall acquisition and policy pipeline](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/services/recall_pipeline.py).
- **[M6]** [Retrieval oracle implementations and authority/relevance separation](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/services/retrieval_oracles.py).
- **[M7]** [Default-off and activated feature ledger, with dated benchmark receipts](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/.agent/default-off-features.md).
- **[M8]** [Scalar consolidation](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/services/scalar_consolidation.py) and [personal-memory readiness](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/docs/personal-memory-readiness.md).
- **[M9]** [Lifecycle session consolidation](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/services/lifecycle_consolidation.py) and [decay safety stop](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/services/lifecycle_decay.py).
- **[M10]** [`rate_recall` observational feedback contract](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/mcp/tools/ops/rate_recall.py) and [receipt implementation](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/mcp/feedback.py).
- **[M11]** [Evaluation and publication posture](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/docs/evaluation.md).
- **[M12]** [Menhir's read-time uncertainty design note](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/.agent/memory-retrieval-under-uncertainty.md), [write-time uncertainty note](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/.agent/memory-aggregation-under-uncertainty.md), and [ingest uncertainty note](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/.agent/memory-ingest-under-uncertainty.md).

### Relevant research and underlying systems

- **[P1]** Park et al., *Generative Agents: Interactive Simulacra of Human Behavior* (2023). Foundational use of memory, reflection and relevance/recency/importance signals. [Paper](https://arxiv.org/abs/2304.03442) · [Author repository](https://github.com/joonspk-research/generative_agents). This is a conceptual precursor, not evidence that its scoring formula should be copied into Menhir.
- **[P2]** Wu et al., *LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory* (ICLR 2025). Tests information extraction, multi-session reasoning, knowledge updates, temporal reasoning and abstention. [Paper](https://arxiv.org/abs/2410.10813) · [Author repository](https://github.com/xiaowu0162/LongMemEval).
- **[P3]** [Graphiti: framework for temporal knowledge graphs](https://github.com/getzep/graphiti) — underlying temporal/provenance graph framework used by Menhir; its capabilities should not be confused with Menhir's additional admission, lifecycle, and code-graph policy.

**Research limitation:** This is a bounded source and architecture review, not a full call-graph audit, live deployment inspection or executed benchmark. No new detector was built or measured for this document; every proposed improvement remains a hypothesis until the experiments above are run.
