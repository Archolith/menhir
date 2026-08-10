# L3/L4 hybrid (C→A→B) — rung sketch + decision register

## Status

proposal / sketch — **NOT a decision, NOT a ladder rung, builds nothing.** Expands the recommended hybrid
from `l3l4-overlay-sequencing-options.md` into concrete phases and, more importantly, the **choices
inside it** for ctharvey to rule on. Once the load-bearing choices are made, this becomes a rung
breakdown for the ladder.

## The hybrid in one picture

```text
Phase C (falsify, bench)      prove LLM-proposed semantics are good enough BEFORE building a store
        │  gate: proposal beats a structure-only baseline on a real fixture
        ▼
Phase A (capture, menhir)     ship a capture-only knowledge store in parallel — real value, zero
        │                     fact-minting risk; gives the brief something TRUE to assemble
        ▼
Phase B (propose+govern)      only after C clears: layer LLM proposal + review/promotion onto the
                              working A store; the review gate stays the safety
```

A and C run in parallel; B is gated behind C's result *and* A's store existing.

## Phase rung sketch (maps to bench / menhir / existing rungs)

```text
C0  semantic/institutional bench fixture: code+history -> gold nodes + artifacts   bench
C1  proposal pipeline (LLM-proposed nodes) scored vs a structure-only baseline      bench  -> GATE
A0  KnowledgeArtifact store (schema subset) + MemoryMutator write boundary          menhir (this IS R9)
A1  capture surface (how decisions/incidents/failures get recorded, with evidence)  menhir
A2  MemoryOracle/EvidenceOracle read it; ColdStartOracle assembles a brief          menhir (oracles = R4-R7,
                                                                                    built in bench; brief = GAP consumer)
B0  LLM proposal pass mints Layer-3 nodes as CANDIDATE (never trusted)              menhir (gated by C1)
B1  review/promotion: CANDIDATE -> TRUSTED                                          menhir
B2  supersession + conflict + candidate decay                                       menhir (reuse BeliefCircuit/conflict?)
```

Everything reads through the oracle layer we already built; everything writes through the single
MemoryMutator boundary. The spine invariants from the options doc hold at every rung.

---

## The decision register (what you actually choose)

Each entry: **the fork → options → trade-off → recommended default** (a default, not a decision).

### Phase C — falsification

**C-1. What is "gold"?** Who defines the correct semantic-node / artifact set for a module?
→ options: hand-author from real menhir history (like our existing fixtures) · derive from ADRs/docs ·
skip gold, use human-judgement scoring. Trade-off: hand-authored is defensible but slow; the concept is
only as well-posed as the gold. **Default:** hand-author a small fixture from real history (proven path).

**C-2. Propose L3, L4, or both first?** capabilities/policies (L3) vs decisions/incidents (L4).
Trade-off: L4 is more concrete/verifiable (it happened); L3 is more interpretive/risky. **Default:** gate
**L4 first** — easier to define gold, lower risk — then L3.

**C-3. The gate threshold.** What bar must proposal quality clear to graduate to building B?
→ options: precision@trusted, false-fact rate, recall. Trade-off: high precision bar protects trust but
may reject a useful-but-noisy proposer. **Default:** gate on **false-fact rate ≤ small ε** (protect
trust) + proposal recall as a secondary; precision over recall.

**C-4. The baseline to beat.** **Default (non-negotiable):** a **structure-only** derivation (no LLM) —
the transparent baseline the LLM proposer must beat, or the LLM isn't earning its risk.

**C-5. Which extraction model?** **Default:** reuse the extraction-bench winners (`gpt-4.1-nano` /
`qwen3-next-80b`) behind the existing model seam.

### Phase A — capture store

**A-1. Storage backend.** New dedicated store vs extend the shipped Graphiti/Neo4j + BeliefCircuit (the
Version-D question, re-entering as a sub-choice). Trade-off: reuse is fast but risks blurring
structural-vs-semantic if the existing model can't hold *untrusted + evidence-backed + superseded*
cleanly. **Default:** **reuse if** a quick read shows the memory model can represent those three states
without a rewrite; else a dedicated store.

**A-2. Which artifact types ship first?** all six, or a subset? **Default:** **Decision / Incident /
Failure** first (highest brief value, clearest provenance); Assumption/Review/AgentDiscovery later.

**A-3. Default status of a HUMAN capture.** Does a human-recorded decision enter as `TRUSTED` (a person
authored it) or `CANDIDATE` (still needs evidence)? *This is a real fork — human capture ≠ LLM proposal.*
Trade-off: trusted-on-write is ergonomic but lets an unevidenced human claim skip the gate. **Default:**
**human capture → TRUSTED only with ≥1 evidence anchor, else CANDIDATE** (evidence-first applies to
people too, but humans don't need a review step).

**A-4. Evidence strictness at capture.** Block save without an anchor, or allow-but-flag? **Default:**
**require ≥1 anchor to reach TRUSTED**; allow anchorless saves but pin them at CANDIDATE.

**A-5. Capture surface.** MCP tool · git/PR hook · PR-template field · agent mid-task write. **Default:**
an **MCP capture tool** (agents + humans via the existing server) first; PR hooks later.

**A-6. Dedup/conflict on overlapping captures.** merge · keep-both-and-link · reject. **Default:**
keep-both + a conflict link (reuse the shipped conflict governance), never silently merge.

### Phase B — propose + govern (the riskiest)

**B-1. Promotion authority — THE choice.** Who promotes CANDIDATE → TRUSTED?
→ options: **human review** · **agent review** · **confidence threshold (auto-promote above X)** · hybrid
(auto below-risk, human for high-impact). Trade-off: human is safe but a bottleneck; auto scales but can
mint near-facts. **Default:** **human (or designated agent) review for L3; auto-promote forbidden** until
C's data justifies a threshold for a narrow class.

**B-2. Proposal trigger.** on ingest · on a schedule (background cognition) · on-demand at query time.
Trade-off: ingest is fresh but costly per episode; background batches; on-demand risks latency.
**Default:** **background** proposal (off the hot path), on the current background-execution substrate —
**not** the (deprecated) yawn.scheduler. See the locked decision below.

**B-3. Candidate-pool control.** How to avoid flooding the store with un-promoted candidates?
→ options: rate-limit · dedup · **anergy/decay on stale candidates** (reuse BeliefCircuit). **Default:**
decay un-promoted candidates (they cool down and drop), so the pool self-limits.

**B-4. Supersession policy.** New proposal contradicts a trusted node → auto-supersede vs flag-conflict.
**Default:** **flag-conflict for review** (never auto-supersede a trusted node); superseded → historical.

**B-5. Rollback.** A promoted node turns out wrong → how is it demoted? **Default:** an explicit
review-state reverse path (TRUSTED → DEPRECATED/HISTORICAL via the Mutator), never deletion.

### Cross-cutting (span all phases)

**X-1. Boundary enforcement.** How hard is "anchors never LLM-set, hypotheses never auto-trusted"?
→ a type-system guard / write-path assertion vs convention. **Default:** **write-path assertions** in the
MemoryMutator (fail closed) — the boundary is too important for convention.

**X-2. Bench↔production coherence.** **Default:** the bench fixture schema **projects from** the real
store (already the pattern for OracleMemory), so they can't drift.

**X-3. Human-review budget.** How much review effort is acceptable? This *drives* B-1's auto-promote
threshold. **Default:** treat review minutes as the scarce resource; auto-promote only where it provably
saves them without raising false-fact rate.

### Sequencing choices (the hybrid's own joints)

**S-1. A-before-C or true parallel?** **Default:** start A's capture store slightly **ahead** so C's gold
can reuse real captured artifacts as examples.

**S-2. Does captured (A) feed C's gold?** **Default:** yes — captured decisions/incidents become
gold-standard examples, tightening C cheaply.

**S-3. What exactly is the C→B gate?** **Default:** B0 (LLM proposal in production) does not start until
C1 clears its false-fact bar **on L4**; L3 proposal waits for a separate L3 gate.

---

## The load-bearing four

Most entries above have a safe default; these four **change everything downstream**, so decide them
first:

```text
1. B-1 Promotion authority      human / agent / threshold — sets how much LLM-semantics becomes trusted
2. A-3 Human-capture default    trusted-on-write vs candidate — sets whether people skip the gate
3. A-1 Store backend            new subsystem vs reuse shipped — sets the bulk of the build cost
4. B-2 Proposal trigger         ingest / background / on-demand — sets cost + freshness + latency
```

## Decisions locked (walkthrough with ctharvey)

```text
A-1 store backend  ->  REUSE-AND-EXTEND the shipped store, mostly (A), with one (B) carve-out:
                       migrate EVIDENCE to a first-class `Evidence` node.
   Rationale: the shipped model already carries per-type policies, source_confidence (human-vs-LLM
   trust), scope-promotion, conflict/supersession governance, and ANCHORED_TO anchoring. Reuse those;
   add institutional/L3 types + a clean knowledge-status/review_state field; and elevate evidence from
   edge-only (ANCHORED_TO / CREATED_FROM) to a first-class Evidence node (artifact -> Evidence ->
   structural-anchor/commit/test). Contained migration; unlocks "what is the evidence for this?" queries.

A-3 human-capture default  ->  (b) human capture is TRUSTED only with >=1 evidence node, else CANDIDATE.
   Evidence-first applies to humans too; but a human author is the authority, so no separate review STEP
   — the presence of evidence IS the gate. The human-vs-LLM distinction is therefore "needs evidence"
   (both) vs "needs evidence AND review" (LLM only, see B-1).

B-1 promotion authority  ->  HUMAN/AGENT REVIEW, no auto-promote.
   Promotion is a guarded status transition CANDIDATE -> TRUSTED done by the Mutator: evidence-gated
   (promote() refuses an unevidenced candidate, fail-closed — even on human say-so), stamped with
   review provenance (review_state=HUMAN_REVIEWED + who/when), reversible (TRUSTED -> DEPRECATED/
   HISTORICAL, never deletion), and a legal state-machine transition only. Confidence can rank the
   review list but can never BE the gate. Deterministic-evidence auto-promote (e.g. evidence is a
   passing test that exercises the capability) is reserved for a narrow class POST-C only.
   No heavyweight queue: "review" = a `status=CANDIDATE` list query + a promote verb (reuses the
   explorer / MCP / the existing scope-promotion + pending_llm_review patterns); volume is bounded by
   candidate decay (B-3) + background proposal cadence (B-2); review can be inline (surfaced in the
   explorer or a brief), not a separate inbox.
   SEPARATE FIELD: the new candidate/review status does NOT overload the shipped `user_flagged`
   (retention/promotion override) — they stay distinct concepts.

B-4 supersession  ->  REUSE the existing conflict-resolution pipeline (prior art:
   .agent/conflict-resolution-history-proposal.md + shipped conflict_status/contradicts).
   Map "new node contradicts a trusted node" onto existing conflict detection. The staleness worry
   ("trusted-but-stale lingers, injected as current") is already covered: (1) a contradicted trusted
   node carries `has_conflict` -> the oracle layer routes it to the z_conflict role -> treated as
   CONTESTED, not asserted as current, BEFORE resolution; (2) winner-picking (supersede) stays
   human/LLM via `replace`/`discard_new`, with age-based `auto_resolve_stale_conflicts` only
   DE-ESCALATING, plus resolution-history suppression + cooldown_days for drift. Conclusion: the
   conservative default is already menhir's behavior; no new auto-demotion mechanism needed. OPTIONAL
   post-C carve-out (symmetric with the auto-promote rule): extend the auto path with a
   deterministic-evidence "replace" (new node has a passing test the trusted one now fails) — later,
   not default. (Walkthrough: ctharvey flagged the prior art; checking it revised the lean from
   "push back / add auto-demotion" to "reuse; read-side has_conflict already covers it.")

B-2 proposal trigger  ->  BACKGROUND (off the hot path), NOT on the (deprecated) yawn.scheduler.
   Use the current/non-deprecated background-execution substrate — confirm against the live tree at
   build time; do NOT assume the scheduler. On-demand-at-query-time stays an optional later add for a
   brief that needs a just-in-time proposal. (Correction: earlier reuse pitch leaned on the deprecated
   scheduler; the background *trigger* is right, the specific mechanism is not the scheduler.)
```

C-2 layer order  ->  L4-FIRST, time-boxed; L3 behind a separate later gate. L4 institutional facts
   (decisions/incidents/failures) are objective -> authorable gold; L3 semantic nodes are interpretive
   -> ill-posed gold. L4-first sidesteps C-1's hardest part.

C-1 gold  ->  author L4 gold from real history now; DEFER L3 gold. When L3 arrives, prefer
   (c) TEST-GROUNDED capabilities: a capability is "correct" iff a passing test exercises it — makes
   L3 gold semi-deterministic (covers testable capabilities first), and dovetails with the
   deterministic-evidence carve-outs reserved for auto-promote (B-1) and auto-supersede (B-4).
   Fallbacks if needed: (a) human-curated per-module gold, (b) human/LLM-judge scoring.

C-3 gate metric  ->  precision over recall (gate on false-fact rate <= epsilon), but TRACK recall so
   what's rejected stays visible.

Remaining defaults locked as-is (low contention): A-2 (Decision/Incident/Failure types first),
A-4 (>=1 evidence for TRUSTED, else CANDIDATE), B-3 (decay un-promoted candidates — PRIOR ART:
ConsolidationRepository already does Entity decay), B-5 (reverse path TRUSTED->DEPRECATED/HISTORICAL via
Mutator, never delete), X-1 (write-path assertions, fail closed), X-2 (bench fixture projects from the
real store), X-3 (review minutes scarce; earns a narrow auto-promote class later), S-1 (A slightly ahead
of C), S-2 (captured feeds C's gold), S-3 (B gated on C clearing L4; L3 separate gate).

## Prior-art audit (changes the build size dramatically)

A sweep of the menhir tree found the **CANDIDATE review tier already exists**, implementing much of
Phases A/B as reuse, not new build:

```text
scope='CANDIDATE'                       a real low-trust tier: "not recalled until approved"
mcp/tools/ingest/add_candidate.py       active capture (source_confidence, evidence_strength, cluster_id, notes)
services/candidate_service.py           approve() = promote CANDIDATE->PERSISTENT + the SAME contradiction
                                        check consolidation runs; reject() deletes; list_candidates() = review list
  - deliberately AVOIDS the user_flagged auto-promote shortcut (confirms A-3/B-1)
emitters (e.g. cth.painscan)            passive background emitters that write candidates (confirms B-2 pattern)
cluster_id + _content_overlap_ratio     candidate grouping + dedup primitive (confirms A-6)
conflict pipeline (B-4)                 detect/resolve/keep_both/replace + history/cooldown
```

Net: the governance machinery is largely BUILT. Genuinely-new work shrinks to (1) artifact TYPES
(L4 institutional / L3 semantic), (2) the first-class Evidence node (A-1), (3) an LLM proposer = just
another candidate emitter, (4) the C bench gate, (5) the ColdStartOracle/brief consumer.

A-5 capture surface  ->  REUSE: `add_candidate` (active) + the emitter pattern like cth.painscan
   (passive). Both already exist; the LLM proposer is a new emitter on the same path. No new surface.

A-6 overlap          ->  REUSE: keep-both-as-CLUSTER is already the shape (`cluster_id`), with
   `_content_overlap_ratio` + the conflict pipeline for dedup/merge. No new dedup subsystem.

CONFIRMED by prior art (already the code's behavior): B-1 (checked approve/reject, not user_flagged),
A-3 (flag separate), B-2 (background emitter pattern).

STILL GENUINELY OPEN (no resolving prior art): C-1/C-3 (semantic gold + precision-over-recall) and
C-2 (L4-first vs L3-first). These remain real decisions.

## To lock the sketch into rungs

Give a ruling on the load-bearing four (the rest can take defaults), and I'll turn this into a
dependency-ordered rung breakdown for the ladder — each rung with its bench gate and scope-risk guard.
Still a proposal; it builds nothing until you say so.
