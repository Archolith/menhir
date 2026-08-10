# Oracle integration plan (weekend Day 3)

## Status

plan — the Day-3 capstone of `weekend-oracle-runtime-roadmap.md`. **Nothing here is built.** It sequences
the specced pieces (Day 1 interfaces, Day 2 schemas) into a build order, sketches the Context Engine
integration and the first task-level benchmark, and lists implementable issues — each tagged by its
gate. GAP items need ctharvey to sequence before building.

## What is buildable now vs gated

```text
DONE (bench, in archolith_bench/oracle):
  R4 interface + executor, R6 cheap oracles (Temporal structured), R7 E/F combiners,
  R7.5 ablation, fixture validator, SemanticScorer real-embedder seam.

BUILDABLE NOW (in-environment, bench-first, no new gates):
  - hybrid facet extractor (facet-extraction-plan.md) in archolith_bench/facet
  - more oracle fixtures; dominant-gold validator heuristic (with false-positive care)

GATED ON A REAL EMBEDDER (cannot run in remote sessions):
  - any promotion number for R1 / R2 / the oracle ladder (all ride the lexical stand-in)
  - calibration of FAMILY_ALPHA / TARGET_LAMBDA / GAMMA / role blend

GATED ON CTHARVEY SEQUENCING (the L3/L4 GAP — Program B/D):
  - the generic KnowledgeArtifact store + MemoryMutator write boundary (R9)
  - ColdStartOracle + the ColdStartBrief
  - the Context Engine
```

## Context Engine integration sketch

The Context Engine is **downstream of reasoning** and packages, it does not decide truth (SOS +
`oracle-runtime-interfaces.md`). Data flow and the menhir code surfaces it would touch:

```text
recall_service          candidate generation (R1/R2 seams already reserved)
  -> oracle_executor    bounded parallel RetrievalOracles over a query snapshot (R4)
  -> oracle_combiner    R7 OraclePacket (ranked, role-aware)
  -> cold_start_oracle  composite: OraclePacket + L3/L4 KnowledgeArtifacts -> ColdStartBrief   [GAP]
  -> context_engine     packs smallest-useful, provenance-tagged context for the target model [GAP]
  -> agent session
```

Rules carried in: oracles observe; combiner + ColdStartOracle decide; only the MemoryMutator (R9)
writes; the snapshot rule (oracles do not fetch the world); every packed item keeps its epistemic label
+ evidence + requesting oracle + risk (the context-pack provenance rules).

## First task-level benchmark sketch

The retrieval-level oracle ladder already exists. The **new** benchmark is task-level — does the brief
prepare the agent? — and is the natural artifact once the L3/L4 store exists:

```text
fixture:  task -> gold ColdStartBrief items (facts / trusted / risks / failed-approaches / context)
conditions: retrieval-only context  vs  ColdStartBrief context
metrics:  brief_completeness, decision_accuracy_per_token (CIP headline),
          stale_surfaced_rate, provenance_fidelity
```

This operationalizes the strategic reframe: benchmark whether an agent was *prepared to make the right
change*, not whether the right chunks were retrieved.

## Issue list (written, not filed)

Ordered, each tagged with its gate. (Not created as GitHub issues — surfaced here for ctharvey to file.)

```text
[ ] R0  retrieval trace (per-candidate source/prior/survived-floor) + CI for archolith-bench   buildable-now
[ ] EXT hybrid facet extractor (structural+git+LLM, confidence-tagged) in the bench             buildable-now
[ ] BENCH dominant-gold validator heuristic (guard false positives)                              buildable-now
[ ] R4  port RetrievalOracle/OracleResult/executor from bench to menhir src                       gated: R0
[ ] R6  cheap oracles in menhir (Semantic via embedder seam / Scope / Temporal / Structure)       gated: R0,R4
[ ] R7  one-pass OracleCombiner (log-space role logits) in menhir                                  gated: R6
[ ] EMB inject a real SemanticScorer; re-run ladder + R7.5 ablation; calibrate weights            gated: embedder
[ ] L4  generic KnowledgeArtifact store + MemoryMutator write boundary (R9)                        gated: sequencing
[ ] CSO ColdStartOracle producing a ColdStartBrief from OraclePacket + L3/L4                       gated: sequencing
[ ] CE  Context Engine packaging (provenance-tagged, smallest-useful)                              gated: sequencing
[ ] BR  task-level ColdStartBrief benchmark fixture + metrics                                      gated: L4,CSO
```

## Non-goals

```text
do not build from this plan — it sequences specs, it does not implement them
do not file GitHub issues from the list above without an explicit ask
do not start GAP items (L4/CSO/CE) before ctharvey sequences Program B/D
do not quote any ladder/ablation number as real until the embedder seam carries a real model
```
