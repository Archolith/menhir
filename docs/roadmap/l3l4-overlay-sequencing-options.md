# L3/L4 overlay — implementation options to compare

## Status

proposal / decision-support — **NOT a decision, NOT a ladder rung.** This is a menu of distinct ways to
build the SOS Layer-3/Layer-4 semantic overlay (the unsequenced GAP: Program B semantic + Program D
institutional). All five build the *same* specced schemas
(`layer4-knowledge-artifacts.md`, `cold-start-brief.md`, `oracle-runtime-interfaces.md`); they differ in
**order, LLM exposure, and what ships first**. ctharvey picks one (or a hybrid); only then does it become
ladder rungs. Nothing here is built.

## Invariants every option must keep (non-negotiable)

```text
1. Structural anchors are deterministic (Layer 2), never LLM-derived.
2. Semantic/institutional artifacts start UNTRUSTED and carry provenance + confidence +
   valid-time + supersession. No LLM-minted facts.
3. Evidence-first: TRUSTED requires >= 1 evidence anchor.
4. Superseded -> historical, not deleted.
5. Only the MemoryMutator writes/promotes/expires. Oracles read.
6. Transparent baseline before heavy deps; bench-gated graduation.
```

The options trade off **how fast you get value** vs **how much LLM-authored semantics you let in** vs
**how much new subsystem you build**. None is allowed to violate the six rules above.

---

## Version A — Evidence-first capture (most conservative)

**Thesis:** record institutional knowledge that *already exists*; infer nothing with an LLM at first.

```text
build order
  1. KnowledgeArtifact store, CAPTURE-ONLY: DecisionMemory / IncidentMemory / FailureMemory /
     AssumptionMemory written explicitly by humans/agents, each with mandatory evidence anchors.
  2. MemoryOracle + EvidenceOracle read them; ColdStartBrief assembles captured artifacts.
  3. Layer-3 (capability/policy) and any LLM proposal: DEFERRED until capture + retrieval is proven.
```

- **LLM-semantics exposure:** none initially. **Builds first:** the store + capture path.
- **Pros:** lowest scope risk; everything has provenance by construction; impossible to mint a fact.
- **Cons:** slow accrual (depends on people/agents recording); no automatic semantic understanding; the
  differentiated "magic" (LLM-derived capabilities) comes last or never.
- **Kill criterion:** if captured artifacts don't measurably improve a brief, the whole overlay is
  suspect — cheap to learn that early.

## Version B — LLM-proposed, review-gated (the full SOS vision, sequenced safely)

**Thesis:** build the whole knowledge-promotion lifecycle; the human/agent *review gate* is the safety.

```text
build order
  1. Store + lifecycle state machine (observation -> candidate -> evidence_collected ->
     trusted -> superseded/historical) + review_state.
  2. LLM proposal pass mints Layer-3 nodes as CANDIDATE (never trusted).
  3. Evidence collection attaches structural anchors; review promotes CANDIDATE -> TRUSTED.
  4. Oracles read status-aware (hypothesis vs trusted); ColdStartBrief separates the two.
```

- **LLM-semantics exposure:** maximal (gated). **Builds first:** the lifecycle + proposal machinery.
- **Pros:** delivers the differentiated capability (semantic understanding of code); matches the SOS
  direction end-to-end.
- **Cons:** largest surface area; needs a real review workflow (human-in-loop); LLM-proposal quality is
  unproven and could flood the candidate pool with noise; most expensive to get wrong.
- **Kill criterion:** candidate→trusted precision after review is too low to be worth the review cost.

## Version C — Bench-first falsification (mirrors how we've worked all session)

**Thesis:** prove LLM-proposal quality on the bench *before* building any production store.

```text
build order
  1. archolith-bench fixture: code/history -> gold semantic nodes + institutional artifacts.
  2. A proposal pipeline (LLM-proposed nodes) scored for precision/recall + provenance discipline
     (does it keep hypotheses untrusted? does evidence attach correctly?).
  3. GATE: only if proposal quality beats a transparent baseline on a real fixture ->
  4. minimal menhir store -> oracles -> brief.
```

- **LLM-semantics exposure:** measured before commitment. **Builds first:** a bench eval, not a store.
- **Pros:** consistent with the repo's "bench decides graduation"; falsifies the riskiest assumption
  (LLM-authored semantics are good enough) cheaply; most reversible.
- **Cons:** slowest path to a usable product; authoring a *gold semantic-node* fixture is itself hard
  (what is the "correct" capability set for a module?).
- **Kill criterion:** the gold fixture can't be authored reliably -> the concept may be ill-posed.

## Version D — Reuse the shipped substrate (minimal new subsystem)

**Thesis:** don't build a new store; extend the shipped memory graph + BeliefCircuit.

```text
build order
  1. Add artifact `type` + status/review fields to existing Graphiti/Neo4j memory nodes.
  2. Reuse the shipped lifecycle (sharpness/freshness/decay) + conflict governance + scope as the
     promotion/supersession machinery; the existing write path becomes the MemoryMutator.
  3. Oracles read typed nodes; brief assembles.
```

- **LLM-semantics exposure:** moderate (whatever the existing ingest already does). **Builds first:**
  schema extensions on shipped infra.
- **Pros:** least new code; reuses tested infra (decay, conflict governance, scope); fastest to
  something usable end-to-end.
- **Cons:** risks blurring the structural-vs-semantic boundary if the existing memory model isn't strict
  about provenance; BeliefCircuit wasn't designed for evidence-as-first-class; may inherit limits you
  then have to unwind.
- **Kill criterion:** the existing model can't represent untrusted+evidence-backed cleanly without a
  rewrite — at which point a dedicated store (A/B) is cheaper.

## Version E — Brief-driven, outside-in (start from the consumer)

**Thesis:** define the agent-facing payoff first; let the store schema be pulled by what the brief needs.

```text
build order
  1. ColdStartBrief fixture (task -> gold brief items) + hand-seed a tiny artifact set.
  2. ColdStartOracle assembles a brief from whatever artifacts exist.
  3. Grow the store / add a proposal pass ONLY where brief quality demands it (demand-driven).
```

- **LLM-semantics exposure:** deferred; pulled in only where the brief needs it. **Builds first:** the
  consumer (brief + oracle) over a seed set.
- **Pros:** avoids over-building the store; keeps the strategic metric central ("was the agent
  prepared?"); demand-driven scope.
- **Cons:** you build the riskiest consumer (ColdStartOracle, itself in the GAP) first, over a thin
  store; risks a brief that can't generalize beyond the seed.
- **Kill criterion:** brief quality plateaus low even with hand-seeded perfect artifacts -> the brief
  format, not the store, is the problem.

---

## Comparison matrix

```text
                         A capture   B LLM-gated   C bench-first   D reuse-shipped   E brief-driven
LLM-semantics exposure   none        maximal       measured-first  moderate          deferred
builds first             store       lifecycle     bench eval      schema on graph   brief+oracle
time-to-first-value      medium      slow          slowest         fastest           fast
scope risk               low         high          low             medium            medium
reuses shipped infra     some        little        n/a (bench)     most              some
falsifiability up-front  medium      low           highest         low               high (on brief)
reversibility            high        low           highest         medium            high
delivers the "magic"     last        first         after-gate      mid               on-demand
```

## Recommendation (a recommendation, not a decision)

A **hybrid C→A→B** reads as the lowest-regret path and matches everything the bench has taught us:

```text
1. (C) Author a small semantic/institutional bench fixture + score an LLM proposal pipeline.
       Cheap, falsifies the core risk, no production commitment.
2. (A) In parallel, ship the CAPTURE-ONLY store (decisions/incidents/failures with evidence).
       Real value now, zero LLM-minting risk, and it gives the brief something true to assemble.
3. (B) Only after C clears its gate, layer the LLM-proposal + review lifecycle on the A store.
       The review gate stays the safety; proposals enter as CANDIDATE over a store that already works.
```

This delivers value early (A), proves the risky part before building it (C), and reaches the full SOS
vision (B) without a big-bang. **Sketched in depth (phases + the decisions inside it):**
`docs/roadmap/l3l4-hybrid-sketch.md`. **D** is the tempting shortcut — pick it only if a quick read of the
shipped memory model shows it can represent *untrusted + evidence-backed + superseded* without a
rewrite; otherwise its reuse savings are illusory. **E** is the best choice if the priority is the
agent-facing brief specifically rather than the knowledge substrate.

## To turn a choice into ladder rungs (what I'd need from ctharvey)

```text
- which version (or hybrid) to sequence;
- the LLM-semantics ceiling: capture-only, propose-as-candidate, or propose-and-auto-promote-with-review;
- who/what performs review (human, agent, or a confidence threshold) — the promotion gate owner;
- whether the store is a new subsystem (A/B) or an extension of the shipped graph (D).
Given those, I can draft the rung breakdown (store -> mutator -> proposal -> review -> oracle -> brief)
with the scope-risk guards attached to each rung, for your approval — still a proposal, not a build.
```
