# Where should a bounded LLM reviewer exist in Menhir?

## Status

speculative

Doc kind: review / independent fresh pass — written 2026-06-29 against `menhir-frontier/src` (verified) and the
research corpus (`docs/research`, `docs/roadmap`). This is an **architectural review**, not a plan: it
builds nothing and proposes no code. It answers one question — *given a deliberately deterministic memory
system, where can a bounded LLM safely make sanity judgments without becoming the source of truth?* — and
its working bias is **restraint**: most seams should stay deterministic.

It is an independent pass: each seam is judged on its own merits first (Parts 0-4), then reconciled
against the corpus's already-settled positions (Part 5). Where the fresh pass agrees with a locked
decision, that is corroboration, not deference; where it would diverge, Part 5 says so out loud.

---

## Part 0 — The reviewer-role taxonomy (read this first)

Three different LLM roles keep getting collapsed into "use an LLM here." They have different blast radii,
and only one is the subject of this review.

```text
PROPOSER     mints candidate knowledge (Decision/Failure/Incident, L3 nodes) from code+history.
             Authority: writes CANDIDATE artifacts (never TRUSTED). Designed as Phase B-0, bench-gated
             behind Phase C. NOT a reviewer — it originates content, it does not judge a detector's output.

REVIEWER     given a DETERMINISTIC detector's "possible X", returns a bounded judgment: yes/no, or a small
             graded label (weak/strong/circular). Authority: advisory only — it annotates or routes, the
             deterministic layer still owns the action. THIS REVIEW IS ABOUT THE REVIEWER.

SYNTHESIZER  composes a task-shaped read-out (ColdStartOracle -> ColdStartBrief) over already-retrieved,
             already-classified items. Authority: read-side only; its LLM output lands as HYPOTHESIS,
             never as FACT, and it never mutates the store.
```

The architecture already has exactly one shipped LLM of any kind: `confirm_contradiction` — a **Reviewer**.
Everything else LLM-shaped in the corpus is a Proposer (designed) or a Synthesizer (designed/bench). That
is the baseline this review extends.

### The bounded-reviewer contract

A seam qualifies for an LLM reviewer only if the reviewer can satisfy **all seven**:

```text
1. advisory              it recommends/annotates; a deterministic rule still decides the action
2. deterministic fallback the seam already works (degraded) with the LLM removed
3. evidence preserved    its input and verdict are stored; it never erases the deterministic signal
4. explainable           the verdict carries a reason a human can audit
5. replayable            same inputs -> re-runnable; the decision is not a hidden side effect
6. benchmarkable         there is a fixture + metric that can prove it beats a no-LLM baseline
7. removable             deleting it does not break correctness, only (maybe) quality
```

**The falsifier (the load-bearing test):** *if removing the LLM breaks correctness, the LLM is in the
wrong place.* A reviewer that the system cannot run without has become a source of truth — exactly the
failure mode this architecture was built to avoid. Every "Should remain deterministic" / "Never" verdict
below traces back to a contract violation, usually #2 or #7.

---

## Grounding — the deterministic substrate each seam sits on

A reviewer seam is only real if a deterministic detector already emits the "possible X" candidate AND that
detector survives as the fallback. Verified build status (`menhir-frontier`, 2026-06-29):

| Subsystem | File(s) | Build status | LLM today |
|---|---|---|---|
| Oracles (Semantic/Structure/Scope/Temporal/Evidence/Intent) | `services/retrieval_oracles.py`, `domain/oracles.py` | BUILT; `oracle_ranking`+`intent_lens` ON in prod | none |
| LogSpaceOracleCombiner / OraclePacket | `domain/oracle_combiner.py` | BUILT | none |
| Wardens + WardenChain | `domain/warden.py` | BUILT; `warden_gate` OFF (opt-in), shadow ON | none |
| Retrieval trace / `OraclePacket.rationale` / AssertionShadowTrace | `domain/retrieval_trace.py`, `services/assertion_pipeline.py` | BUILT (shadow pass live) | none |
| Belief scorer + currentness bucket | `domain/belief.py` | BUILT; gated from live recall | none |
| Artifacts (Decision/Failure/Incident) + `:Evidence` node + R9-lite write boundary | `domain/artifacts.py`, `infrastructure/artifact_repository.py`, `services/artifact_service.py`, `services/memory_oracle_service.py` | BUILT; MCP/API wiring gated | none |
| Candidate tier + dedup/merge (Jaccard 0.70/0.85/0.95) | `infrastructure/candidate_repository.py`, `candidate_service.py`, `correlation_queries.py` | BUILT | none |
| Git staleness / bitemporal / durable file id | `domain/git_staleness.py`, `temporal.py`, `repo_snapshot.py` | BUILT; gated | none |
| Doc Drift Watch | `docs/roadmap/doc-drift-watch-mvp.md` | DESIGNED / bench-only | none (v0 forbids LLM-only flags) |
| ColdStartBrief v0 / ColdStartOracle | `archolith_bench/l4/brief.py`; `docs/research/cold-start-brief.md` | BENCH-only / designed | designed: LLM out -> HYPOTHESIS only |
| **LLM semantic-node proposer** | `docs/roadmap/l3l4-hybrid-sketch.md` Phase B-0 | **DESIGNED-ONLY (not in src or bench)** | would be the LLM |
| Contradiction confirmation `confirm_contradiction` | shipped `menhir` conflict pipeline | SHIPPED — the one existing LLM reviewer | yes (advisory) |

Note the dominant fact: **nearly the entire stack is BUILT and zero-LLM by construction**, much of it
gated off in production. The deterministic fallback for most seams is not hypothetical — it is the
shipping code.

---

## Part 1 — Complete review-seam inventory

Each seam: **Purpose / Inputs / Outputs / Authority / Failure mode / Fallback**, plus the subsystem it
rides on. Seams 1-10 are the ones named in the brief; 11-13 are surfaced by the architecture itself.

### Seam 1 — Artifact proposal review (merge / split / re-attach)
- **Purpose:** when capture produces overlapping Decision/Failure/Incident artifacts, decide: should two
  be merged, one be split, or evidence re-attached elsewhere?
- **Inputs:** two+ `KnowledgeArtifact` records (summary, body, type, anchors, evidence), their
  `MemoryOracleService.find` overlap.
- **Outputs:** advisory `{merge | split | reattach | leave}` + reason; routes to a human/agent review list.
- **Authority:** advisory; only the MemoryMutator / `ArtifactService` writes. LLM never merges.
- **Failure mode:** wrong merge erases a distinct lesson; wrong split fragments one lesson into noise.
- **Fallback:** the locked A-6 default — **keep-both + a conflict/`RELATES_TO` link, never silently merge.**
  No merge/split logic exists today, so the fallback is "do nothing," which is safe.
- **Subsystem:** `artifact_service.py`, `memory_oracle_service.py` (BUILT, gated); merge/split absent.

### Seam 2 — Contradiction review (is a flagged pair genuinely contradictory?)
- **Purpose:** the detector flags two memories as *possibly* contradictory; decide if they actually are,
  or are merely different context.
- **Inputs:** the two memories (name + content), similarity score, scope/temporal metadata.
- **Outputs:** `is_conflict: bool` + reason -> sets `unresolved` (surface) or `false_positive` (suppress).
- **Authority:** advisory but **consequential** — it sets a status field that the read side honors. Bounded
  by cooldown suppression and an age-based de-escalation override.
- **Failure mode:** false "not a conflict" hides a real contradiction; false "conflict" nags the user.
- **Fallback:** pair stays `pending_llm_review`; `auto_resolve_stale_conflicts` de-escalates by age
  (keep-both) after 14 days. Correctness holds without the LLM — only resolution latency grows.
- **Subsystem:** SHIPPED `confirm_contradiction`; mirrored by belief `CONFLICT_SET`. **This is the
  existing reviewer and the template for all others.**

### Seam 3 — Evidence quality review (weak / strong / circular / missing?)
- **Purpose:** judge whether evidence attached to an artifact actually *supports* the claim, beyond what
  the evidence *kind* tells you.
- **Inputs:** artifact summary + each `Evidence(kind, ref, directness, note)`; the referenced anchor.
- **Outputs:** a graded label per evidence (`strong | weak | circular | irrelevant`) + reason; advisory
  input to the human promotion decision.
- **Authority:** advisory; cannot promote. The deterministic gate (`>=1 promotable evidence`) still owns
  TRUSTED.
- **Failure mode:** grading a circular agent-inference as "strong" would, if trusted, defeat the
  evidence-first invariant.
- **Fallback:** the deterministic evidence-class policy — `{git, test}` promotable, `agent_inference`
  non-promoting, directness in `[0,1]`. The gate works today with zero LLM.
- **Subsystem:** `EvidenceOracle`, `artifacts.Evidence`, R9-lite write boundary (BUILT).

### Seam 4 — Duplicate review (duplicate / near-dup / related / different)
- **Purpose:** for two candidate memories, choose the correct relationship.
- **Inputs:** two contents, `_content_overlap_ratio` (Jaccard), embedding similarity.
- **Outputs:** `{duplicate | near | related | different}`; routes to merge vs `RELATES_TO` vs nothing.
- **Authority:** advisory at most; **the irreversible action (>0.95 merge, DETACH DELETE) must stay
  deterministic.**
- **Failure mode:** an LLM "duplicate" verdict that triggered a merge would destroy a nuance with no undo.
- **Fallback:** Jaccard thresholds (0.70 relate / 0.85 conflict / 0.95 merge) + keep-both. Fully working.
- **Subsystem:** `correlation_queries.py`, `candidate_repository.py` (BUILT).

### Seam 5 — Belief evolution review (does evidence justify superseding a trusted belief?)
- **Purpose:** when a new claim contradicts a TRUSTED belief, decide if the org belief should actually change.
- **Inputs:** old + new artifact, their evidence, `git_staleness` verdict, belief buckets.
- **Outputs:** advisory winner-pick `{replace | discard_new | keep_both}` + reason.
- **Authority:** advisory; supersession is a guarded Mutator transition (`TRUSTED -> HISTORICAL`, never
  delete). Auto-supersede is forbidden by B-4.
- **Failure mode:** a wrong "replace" would retire a still-valid belief; mitigated by reversibility.
- **Fallback:** **flag-conflict for human/agent review**; `has_conflict` already routes the contested node
  to the conflict role (CONTESTED, not asserted-current) *before* resolution. Safe without LLM.
- **Subsystem:** `belief.py` (SUPERSEDED), conflict winner-pick (BUILT, gated).

### Seam 6 — Doc drift review (probably stale vs incidentally touched?)
- **Purpose:** the deterministic matcher says a doc *may* be stale because an anchor changed; decide if it's
  genuinely stale or only touched by an unrelated refactor.
- **Inputs:** `DocReviewCandidate(reason, changed_refs, severity)`, the doc body, the `ChangeEvent`.
- **Outputs:** advisory `{stale | incidental}` + reason; can downgrade a low-severity false positive.
- **Authority:** advisory; **never the sole reason for a high-severity flag** (v0 invariant 6).
- **Failure mode:** suppressing a true stale flag lets a well-written stale doc out-rank current truth —
  the exact failure Doc Drift Watch exists to prevent.
- **Fallback:** deterministic severity rules (direct-anchor change = high; body-token = low). Works without LLM.
- **Subsystem:** Doc Drift Watch (DESIGNED), `git_staleness.py` (BUILT).

### Seam 7 — Friction clustering review (one recurring problem or several?)
- **Purpose:** decide whether N similar friction events are one root cause or several.
- **Inputs:** candidate friction records sharing a `cluster_id`, `_content_overlap_ratio`.
- **Outputs:** a (re)clustering opinion.
- **Authority:** would be advisory — **but clustering is owned by the emitter (cth.painscan), not menhir.**
  Menhir trusts the `cluster_id` it is handed (idempotent on `source+cluster_id`).
- **Failure mode:** re-clustering inside menhir would split ownership of a decision the emitter already made.
- **Fallback:** emitter-supplied `cluster_id` + Jaccard dedup. Menhir needs no judgment here.
- **Subsystem:** `candidate_repository.py` (BUILT); clustering is upstream.

### Seam 8 — Agent-performance review (model-related vs context/tooling/repo-state?)
- **Purpose:** when metrics show an agent "performed poorly," decide the cause.
- **Inputs:** `classify_enrichment_failure` output, telemetry (queue depth, failure class, budget hits).
- **Outputs:** a causal opinion (model vs context vs tooling vs repo state).
- **Authority:** advisory ops-analytics; **tangential to memory truth** — it does not touch what a memory
  asserts or how it ranks.
- **Failure mode:** low — a wrong cause label misguides an operator, it does not corrupt the store.
- **Fallback:** deterministic failure classification (`retryable | terminal | manual_review`) + telemetry.
- **Subsystem:** `enrichment_failures.py`, telemetry store (BUILT).

### Seam 9 — ColdStartBrief review (missing / contradictory / over-emphasized obsolete?)
- **Purpose:** sanity-check an assembled brief: what's obviously missing, internally contradictory, or
  over-weighted toward obsolete info?
- **Inputs:** the `ColdStartBrief` buckets (facts/trusted/hypotheses/risks/failed/stale) + provenance.
- **Outputs:** advisory edits to the brief (add open-question, demote item) as HYPOTHESIS-tagged notes.
- **Authority:** read-side **Synthesizer/Reviewer hybrid** — never mutates the store; LLM output lands as
  `likely_interpretations` (HYPOTHESIS), never `known_facts`.
- **Failure mode:** lowest of all write-adjacent seams — a bad brief note misleads one session, is visible,
  and never persists as fact.
- **Fallback:** deterministic bucketing (`_epistemic`, `_recommend`) already produces a correct,
  if blunter, brief. Invariant 8 (CANDIDATE/HISTORICAL never shown as FACT) is structural.
- **Subsystem:** ColdStartBrief v0 (BENCH), ColdStartOracle (DESIGNED).

### Seam 10 — Retrieval explanation review (does the explanation justify why this memory won?)
- **Purpose:** judge whether the stated reason a memory ranked first is actually justified.
- **Inputs:** `OraclePacket.rationale` (per-oracle, per-target strings), `role_logits`, the scored
  breakdown.
- **Outputs:** a narrative "why this won" / "this looks wrong" note.
- **Authority:** would be advisory annotation on the trace.
- **Failure mode:** the rationale is **already a mechanically faithful projection of the math** — every
  line is "+relevant semantic 0.45" derived from the actual logit contributions. An LLM restating it can
  only be *less* faithful, and risks inventing a plausible-but-wrong story the deterministic trace did not
  say.
- **Fallback:** the trace itself. It is complete and true by construction.
- **Subsystem:** `retrieval_trace.py`, `oracle_combiner.py` (BUILT).

### Seam 11 — Cross-candidate paradox (two co-admitted memories contradict)
- **Purpose:** catch the case where the stack admits two memories that contradict each other (wardens
  decide per-candidate, in isolation, so no component sees the pair).
- **Inputs:** the admitted set + their OraclePackets.
- **Outputs:** advisory "candidates 5 and 7 are mutually exclusive; one should be flagged."
- **Authority:** advisory annotation; routes into the existing conflict pipeline (Seam 2/5), does not
  remove anything itself.
- **Failure mode:** missing a paradox = status quo (no regression); false paradox = a spurious conflict flag.
- **Fallback:** none today — this is a genuine *gap*, not a detector with an LLM bolt-on. That matters for
  categorization (you cannot have a "fallback" for a detector you have not built).
- **Subsystem:** `warden.py` (per-candidate only); pairwise detection absent.

### Seam 12 — Cross-scope relevance (library in repo B relevant to repo A?)
- **Purpose:** decide whether a wrong-scope memory is nonetheless relevant (shared library used by the
  query's repo).
- **Inputs:** `query_scope`, `candidate_scope`, the dependency graph.
- **Outputs:** an opinion to admit-anyway.
- **Authority:** would override a `ScopeWarden` **REFUSE** — i.e. it would soften an authoritative gate.
- **Failure mode:** scope leakage — admitting another project's memory as in-scope is precisely what
  ScopeWarden exists to prevent; an LLM override re-opens that hole.
- **Fallback:** `scope_conflict` (deterministic) + the structural dependency graph, which can already model
  "repo A depends on lib B" *deterministically* if cross-scope relevance is wanted.
- **Subsystem:** `ScopeWarden`, `scope.py` (BUILT).

### Seam 13 — Intent x role matrix override
- **Purpose:** override a fixed `INTENT_ROLE_MATRIX` cell when it seems wrong for a specific query (e.g.
  PLAN role under VERIFY_CURRENTNESS).
- **Inputs:** query text, candidate roles, the matrix cell.
- **Outputs:** a per-query weight override.
- **Authority:** would replace a deterministic table lookup inside the ranking path.
- **Failure mode:** destroys the matrix's defining properties — determinism, unit-testability, bench
  attributability ("ranking change = this cell"). The intent-warden design chose the table *specifically*
  so no model sits in the hot ranking path.
- **Fallback:** the matrix. Extending it is "add a row/column," not "add a model."
- **Subsystem:** `intent_affinity.py` (BUILT).

---

## Part 2 — Categorization

Each seam in exactly one bucket. The verdict column cites the **first contract clause it passes or fails**.

| # | Seam | Category | Decisive reason |
|---|------|----------|-----------------|
| 2 | Contradiction review | **Safe today** | Already shipped; passes all 7; fallback = pending + age de-escalation |
| 5 | Belief winner-pick advisory | **Safe today** | Sanctioned (replace/discard_new stays human/LLM); reversible; flag-conflict fallback |
| 3 | Evidence quality | **Future research** | Passes contract but **no gold yet**; gates the TRUSTED boundary, high value, needs a bench |
| 1 | Artifact merge/split | **Future research** | Needs the L4 store live + a merge/split fixture; keep-both fallback holds until then |
| 9 | ColdStartBrief review | **Future research** | Lowest-risk write-adjacent seam (read-out only, HYPOTHESIS-tagged); needs a brief-quality bench |
| 6 | Doc drift false-positive review | **Future research** | Corpus already defers it; deterministic v0 must ship and prove precision first |
| 11 | Cross-candidate paradox | **Future research** | Real gap with **no deterministic detector yet** -> build the detector before any LLM; fails contract #2 today |
| 4 | Duplicate routing | **Should remain deterministic** | Irreversible merge; raise the 0.95 threshold before adding judgment; keep-both already safe |
| 7 | Friction clustering | **Should remain deterministic** | Owned by the emitter; menhir trusting `cluster_id` is correct boundary |
| 8 | Agent-performance | **Should remain deterministic** | Ops analytics, tangential to memory truth; low value-at-risk either way |
| 12 | Cross-scope relevance | **Should remain deterministic** | Would soften an authoritative warden; model cross-scope in the dep graph instead |
| 10 | Retrieval explanation | **Should never use an LLM** | Rationale is mechanically true; LLM can only be less faithful (fails the *point* of the trace) |
| 13 | Intent x role matrix override | **Should never use an LLM** | Destroys determinism/benchability; a model in the hot ranking path is the anti-goal |
| — | Warden verdicts / structural anchors | **Should never use an LLM** | These ARE the truth boundary; an LLM here is a source of truth by definition |

**Score:** 2 safe today, 5 future-research (one of which is "build the detector first"), 4 stay
deterministic, 3 never. Of 13 candidate seams, **at most 2 deserve an LLM now, and all the safe ones are
conflict-adjacent.**

---

## Part 3 — Cost / value analysis

Latency anchored to the real execution boundary: **read/hot-path** seams must answer in-line with recall;
**write/promotion** and **background** seams run off the hot path and tolerate LLM latency. Operational
cost is bounded by the existing rolling-window per-session LLM cap, the per-job cap, and circuit breakers —
the same governors that already protect enrichment.

| # | Seam | Frequency | Latency sensitivity | Quality gain | Failure impact | Op cost |
|---|------|-----------|---------------------|--------------|----------------|---------|
| 2 | Contradiction review | Low (only 0.85-0.95 pairs) | Off hot path (background `confirm_conflicts`) | High — kills false-positive nag | Medium — hidden real conflict | Low (capped, cooldown) |
| 5 | Belief winner-pick | Low (only on contradiction) | Off hot path (review-time) | High — correct supersession | Medium but **reversible** | Low |
| 3 | Evidence quality | Medium (per promotion) | Off hot path | High — protects TRUSTED | High **if trusted** (must stay advisory) | Medium |
| 1 | Artifact merge/split | Low-medium | Off hot path | Medium — tidier store | Medium (erase a lesson) | Medium |
| 9 | ColdStartBrief review | Per cold start | Tolerant (brief assembly already LLM-budgeted) | Medium-high — better briefs | Low (visible, non-persistent) | Medium-high |
| 6 | Doc drift review | Per change event | Background | Medium — fewer false flags | Medium (suppress true stale) | Low-medium |
| 11 | Cross-candidate paradox | Per recall (pairwise) | **Hot path if inline** | Medium | Low (annotation) | High (pairwise blowup) |
| 4 | Duplicate routing | High (every candidate) | Hot-ish | Low (Jaccard already good) | **High (irreversible merge)** | High |
| 7 | Friction clustering | Per emit | N/A | Low (not menhir's job) | Low | Wasted |
| 8 | Agent-performance | Per failure batch | Background | Low-medium | Low | Low |
| 12 | Cross-scope relevance | Per scope conflict | Hot path | Low | **High (scope leak)** | Medium |
| 10 | Retrieval explanation | Every result | **Hot path** | Negative (less faithful) | Medium (plausible lie) | High |
| 13 | Matrix override | Every ranking | **Hot path** | Negative (kills benchability) | High | High |

The pattern that falls out: **value concentrates in the low-frequency, off-hot-path, write/promotion-gating
seams (2, 5, 3, 1, 9); cost-and-risk concentrate in the high-frequency hot-path ranking seams (10, 13, 4,
12).** This is the architecture telling you where an LLM belongs: at the slow, rare, reviewable margins —
not in the fast inner loop.

---

## Part 4 — Promotion criteria (per LLM-worthy seam)

The corpus already has a promotion discipline (intent-warden's bench plan, l3l4 Phase C). Apply it
uniformly. **A seam graduates only if all four hold:**

```text
BASELINE   a structure/deterministic-only arm the LLM must beat (l3l4 C-4: non-negotiable)
PRECISION  gate on false-fact / false-verdict rate <= epsilon; track recall but precision wins (C-3)
ABLATION   shuffle the LLM's input labels -> the lift must collapse to chance (proves it's the signal,
           not topic/positional leakage)
NO-HARM    an off-target arm where the LLM must not degrade the deterministic baseline
```

Per safe / future seam:

- **#2 Contradiction review (safe today).** *Helps when:* agreement-with-human on a labeled
  contradiction fixture beats "flag everything 0.85-0.95." *Bench:* pairs fixture with gold
  conflict/not-conflict; metric = precision on false-positive suppression. *Reject if:* it suppresses a
  true contradiction (any false "not a conflict" above epsilon). Already in production behind cooldown —
  this formalizes its gate retroactively.
- **#5 Belief winner-pick (safe today).** *Helps when:* its replace/discard recommendations match a gold
  supersession set better than "always keep-both." *Bench:* superseded-belief fixture (the floor-fix story
  is a ready example). *Reject if:* it ever recommends retiring a belief whose evidence is still valid.
- **#3 Evidence quality (future).** *Helps when:* its strong/weak/circular grades beat the evidence-kind
  prior at predicting "human would promote." *Bench:* artifacts with gold evidence grades. *Reject if:* it
  grades a circular agent-inference as strong. **Blocker:** no gold corpus exists -> stays design-only
  until the L4 store accumulates real evidence.
- **#1 Artifact merge/split (future).** *Helps when:* merge/split proposals match a gold curation of a
  real artifact set, beating keep-both. *Bench:* curated artifact pairs. *Reject if:* precision of "merge"
  drops below epsilon (a wrong merge erases a lesson). **Blocker:** needs the live store + a fixture.
- **#9 ColdStartBrief review (future).** *Helps when:* `brief_completeness` / `decision_accuracy_per_token`
  improve vs the deterministic v0 brief, with `provenance_fidelity` held (no HYPOTHESIS->FACT). *Bench:*
  the planned task->gold-brief fixture. *Reject if:* it raises `stale_surfaced_rate` misses or mislabels
  epistemics.
- **#6 Doc drift review (future).** *Helps when:* it cuts `false_stale_rate` without lowering
  `stale_doc_flagged_rate`. *Bench:* `doc_drift_watch_basic.json`. *Reject if:* it ever becomes the sole
  reason for a high-severity flag (v0 invariant). **Blocker:** deterministic v0 must ship and prove
  precision first.
- **#11 Cross-candidate paradox (future).** *Blocker is upstream of the LLM:* **build the deterministic
  pairwise-contradiction detector first.** Only then is there a "possible X" for an LLM to review, and a
  fallback to fall back to. Until that exists, it fails contract clause #2 and is not a reviewer seam yet.

---

## Part 5 — Reconciliation with the existing corpus

This was an independent pass; here is where it lands against the corpus's settled positions.

**Agreements (independent pass corroborates locked decisions):**
- **Proposer is not a reviewer, and is bench-gated.** Matches l3l4 Phase B-0 gated behind Phase C, and the
  layer4 invariant "do not let an LLM mint a TRUSTED artifact or anchor." This review keeps the Proposer
  out of the reviewer inventory entirely (Part 0).
- **ColdStart LLM output is HYPOTHESIS-only.** Matches `cold-start-brief.md` ("ColdStartOracle MAY call an
  LLM... lands only as `likely_interpretations`, never `known_facts`"). Seam 9 inherits this verbatim.
- **Doc-drift LLM is deferred, never the sole high-severity reason.** Matches `doc-drift-watch-mvp.md`
  invariant 6 and open-question 5. Seam 6 is filed Future-research for exactly that reason.
- **No model in the hot ranking path.** Matches the intent-warden determination (table lookup chosen
  precisely so no LLM sits in ranking). Seams 10 and 13 are filed Never on the same logic.
- **Conflict resolution stays human/LLM, reversible, no auto-supersede.** Matches l3l4 B-4. Seams 2 and 5
  (the two "safe today") sit inside this already-sanctioned envelope.
- **No auto-promote; write-path assertions fail-closed.** Matches l3l4 B-1 and X-1. Every reviewer here is
  advisory; none promotes.

**No material disagreement.** The independent verdicts did not surface a seam the corpus marked LLM-worthy
that this review would reject, or vice versa. The one *addition* is analytical, not contradictory:
**Seam 11 (cross-candidate paradox)** is a gap the corpus has not named as a reviewer seam — and this
review explicitly declines to make it one *yet*, because the deterministic detector beneath it does not
exist. That is consistent with, not a divergence from, the corpus's build-the-deterministic-floor-first
discipline.

---

## Conclusion — the restraint verdict

Of ~13 candidate review seams:

```text
2  safe today          contradiction review + belief winner-pick — both conflict-adjacent,
                       both already sanctioned, both advisory + reversible + benchmarkable.
5  future research     evidence quality, artifact merge/split, ColdStartBrief review, doc-drift
                       false-positive review, cross-candidate paradox — each behind a bench gate,
                       and one (paradox) behind building its deterministic detector first.
4  stay deterministic  duplicate routing, friction clustering, agent-performance, cross-scope
                       relevance — the deterministic answer is already correct or already owned
                       elsewhere; an LLM adds risk, not truth.
2+ never               retrieval-explanation narration and matrix override (plus warden verdicts
                       and structural anchors) — putting an LLM here destroys the very properties
                       (faithfulness, determinism, benchability) the system is built on.
```

The headline is restraint. The deterministic stack is not a placeholder waiting for an LLM to fill it in —
it is the design. An LLM earns a seat only at the **slow, rare, off-hot-path, write/promotion-gating
margins**, only **advisory**, only with a **deterministic fallback that already works**, and only after a
**bench proves it beats no-LLM**. Everywhere it would sit in the hot ranking path, own an irreversible
action, or replace a true deterministic signal, it should be left out.

The LLM is a senior engineer reviewing the work at the margins — never the subsystem doing it.
