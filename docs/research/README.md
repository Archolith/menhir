# menhir research index

## Status

canonical

## Purpose

This index prevents research-note sprawl. It names the durable docs, marks which ideas are active versus parked, and defines when a concept may become part of menhir.

The rule:

```text
A concept is not part of menhir until it has at least one of:
1. a code surface,
2. an archolith-bench fixture,
3. a metric,
4. a named failure mode it prevents.

Otherwise it remains a parked research note or issue comment.
```

> **2026-07-11 — active-direction correction (read before the cluster / reading order below).**
> This corpus is organized around the **read-side retrieval pipeline** (candidate -> oracle -> combine
> -> rails) and labels it the "active pipeline." That is now **historical framing**: the oracle/warden
> read-side stack was **built and benched neutral-to-negative on LongMemEval** (node-only 0.400 > full
> stack 0.333) and **ships default-off** (`config/settings_model.py`, every `frontier_*` boolean gate False). The **current
> active direction is write-time consolidation** — "aggregation is a consolidation problem, not a
> retrieval one": D0 retrieval-entropy, D1 QuantState, Event -> Fold -> View, and agent-experiential
> counters, all BUILT. Those docs live under `.agent/plans/`
> (`aggregation-as-consolidation.md`, `quantstate-agent-counter.md`, `event-fold-view-architecture.md`),
> not this corpus — the latter two briefly relocated into `docs/research/direction/` on 2026-07-11
> and were moved back on 2026-08-07 (curator audit) once "shipped and realized in code" made the
> research-corpus placement inaccurate; all three moved out of `plans/backlog/` on 2026-08-10 once
> the arc was sequenced as Track W in the execution ladder. See the execution ladder's "Bench verdicts — reconciliation"
> for why the read-side pivoted. The mechanism-ownership tables below remain accurate; only the
> "what to build next" framing changed.
>
> **2026-08-09 — the pivot now has a number.** The write-side arc scored **0.910 (71/78)** on the
> 78-item LongMemEval knowledge-update oracle fixture against a `no_memory` arm of 6/78 — a +0.833
> delta, and up from the 0.872 canonical baseline. Same fixture hash as the read-side campaign, so
> it compares directly against node-only 0.400. Run `scalar-event-activity-ku78-v6-20260809`
> (Menhir `1fa57955`, Bench `d5e97cc4`, both clean); the authority is archolith-bench
> `results/lme-ku-buildout/LEDGER.md`, with the same acceptance record tracked in this repo at
> `.agent/plans/menhir-cumulative-activity-scalars-2026-08-08.md`. This is benchmark evidence on
> one subset, not a launch headline.

## Cluster layout

The corpus is organized into themed subdirectories; each has its own `README.md`:

```text
docs/research/
  direction/        cluster 0 — architectural synthesis (read first)
  process/          cluster 1 — research workflow + eval division of labor
  positioning/      cluster 2 — product/category positioning
  retrieval/        cluster 3 — candidate -> oracle -> combine -> rails (BUILT; benched
                    neutral-to-negative on LME, default-off; see 2026-07-11 note above)
  schemas/          cluster 3 — L3/L4 knowledge-artifact data structures (spec-only)
  belief-temporal/  cluster 4 — belief, temporal, structure substrates
  vision/           cluster 5 — future direction
  privacy/          cluster 6 — encrypted memory + provenance-gated admission (trust)
  prior-art/        cluster 7 — external system/paper comparisons (see its own README)
  archive/          superseded docs (kept as pointers, never deleted)
```

Two top-level docs (not clustered): `adaptive-activation-topology-plan.md` (speculative —
task-local activation trace + bounded routing, no code surface yet) and `building-on-menhir.md`
(speculative — positioning/vision catalog of downstream applications, not a build proposal).

## Where this fits (corpus map)

```text
docs/research/        forward research notes (this corpus): positioning, retrieval
                      pipeline, belief/temporal, future/vision. Owns mechanisms +
                      promotion conditions.
.agent/               operational docs for the SHIPPED system: architecture,
                      data_models, endpoints, memory-design/roadmap, workflows.
                      Token-optimized router system (see .agent/file-index.md).
.agent/research/menhir-research-execution-ladder.md
                      the bridge: dependency-ordered build order taking this
                      corpus into code + bench. Read it for "what to build next".
```

## Reading order (clusters)

```text
0. Architectural direction (read first for the big picture):
   direction/semantic-operating-system.md, direction/oracle-architecture.md,
   direction/llm-reviewer-seams.md
   (four-layer knowledge model + oracle/combiner/context-engine/mutator stack;
    the synthesis the rest of the corpus implements; plus where a bounded LLM
    reviewer belongs in that stack)

1. Process / eval (read first):
   process/research-process.md, process/archolith-bench-operational-model.md,
   process/research-vs-shipped-inventory.md

2. Positioning (one canonical doc):
   positioning/positioning.md   (CIP category + 3 lenses; supersedes the 3 framing stubs)

3. Retrieval pipeline (candidate -> oracle -> combine -> write):
   retrieval/retrieval-tuning-stack.md, retrieval/facet-retrieval.md,
   retrieval/facet-extraction-plan.md, retrieval/oracle-amplified-retrieval.md,
   retrieval/oracle-runtime-interfaces.md, retrieval/oracle-execution-and-performance.md,
   retrieval/retrieval-control-rails.md, retrieval/intent-warden.md
   Schemas (Program D/E, the L3/L4 GAP — spec only):
   schemas/layer4-knowledge-artifacts.md, schemas/cold-start-brief.md
   Build sequencing (roadmap): docs/roadmap/README.md ->
   docs/roadmap/weekend-oracle-runtime-roadmap.md, docs/roadmap/oracle-integration-plan.md

4. Belief / temporal / structure:
   belief-temporal/belief-layer.md, belief-temporal/connected-data-substrates.md,
   belief-temporal/tracehead-braidtrace.md

5. Future / vision:
   vision/cognitive-replay-and-phasing.md   (phase ladder, Cognitive Replay, epistemic law)

6. Privacy / trust (PARKED — future-need shower thoughts, not active work):
   privacy/sealed-recall.md   (confidentiality: encrypted content + local embeddings +
   selective decrypt) and
   privacy/trusted-memory-admission.md   (integrity/provenance: source_type + trust_tier +
   admission firewall deciding what becomes durable user memory)
   Both presume a hosted/multi-user scale menhir does not have. If anything revives, start
   with admission's trust-tier -> belief-layer assertion-confidence seam.

7. Prior art (external comparisons — positioning/roadmap input, not menhir mechanism docs):
   prior-art/memtrace-comparison.md, prior-art/repowise-comparison.md,
   prior-art/fluxmem-connectivity-prior-art.md
   None pin the external project to a commit/release; treat all three as due for
   re-verification before citing in a decision (see the cluster's own README).

Build order across these lives in the execution ladder (see corpus map above).
```

## Canonical docs

| Doc | Status | Owns | Code surface | Bench surface | Notes |
|---|---|---|---|---|---|
| `process/research-process.md` | canonical | research workflow | none | all research lanes | Defines sources, claims, baselines, evals, and publication ladder. |
| `process/research-vs-shipped-inventory.md` | canonical (snapshot) | EXISTS / PARTIAL / NEW reconciliation of the whole corpus vs `src/menhir` (reconciled 2026-07-11) | maps every concept to its code surface | n/a | Read this BEFORE planning a build — it shows what to reuse, what to wire, and the net-new (now 4 clusters incl. write-side consolidation). Drifts; re-audit (checklist at the bottom). |
| `process/archolith-bench-operational-model.md` | canonical | eval harness responsibilities | none | `archolith-bench` lifecycle/results | Keeps `menhir = proposes/implements` and `archolith-bench = proves/falsifies`. |
| `direction/semantic-operating-system.md` | active (direction) | Four-layer knowledge model (Source/Structural/Semantic/Institutional), structural-vs-semantic truth boundary, evidence-as-first-class, knowledge-promotion lifecycle, temporal semantics, Unison-as-optional-backend, Cold Start Brief, Programs A–E + 6-phase build | structural substrate exists (`infrastructure/structural_anchoring.py`, `structure_queries.py`, `ingest_project`); L3/L4 semantic+institutional layer not yet built | L3/L4 fixtures owed | Top-level synthesis. Re-frames the oracle pipeline (owned by oracle-*.md) and adds the L3/L4 semantic-overlay direction. Reconciled with the execution ladder — see its "SOS direction reconciliation" section. |
| `direction/oracle-architecture.md` | active (direction) | The runtime stack statement: Layers store → Oracles reason → Combiner synthesizes → Context Engine packages → Mutators write; cold-start pipeline | mechanisms owned per-layer (oracle-*.md, belief-layer.md) | per-rung | Concise architectural stack companion to semantic-operating-system.md; does not own mechanism detail (links to the per-layer docs). |
| `belief-temporal/belief-layer.md` | active | BeliefCircuit, breaker decisions, anergy/apoptosis split | `src/menhir/domain/belief.py`, recall/scoring/lifecycle integration | stale assertion, belief drift, Git-aware invalidation fixtures | Single owner for probabilistic belief/breaker concepts. |
| `positioning/positioning.md` | active | Canonical product/category positioning: CIP category + alternatives, the three lenses (CIP / Agent Experience Substrate / cognitive artifacts), system hierarchy, cognitive primitives, decision-quality-per-token thesis + CIP bench metrics | none (positioning) | CIP metrics are archolith-bench candidates (Context Compression Ratio, Decision Accuracy per Retrieved Token, ...) | Single owner for positioning. Consolidates four prior artifacts; see Superseded docs. |

## Speculative research notes

| Doc | Status | Owns | Promotion condition |
|---|---|---|---|
| `retrieval/retrieval-tuning-stack.md` | speculative | EmbeddingDimensionSweep, HybridAlphaSearch, CrossEncoderRerankOracle, ProjectionCalibrationLayer | Promote when candidate-generation/tuning knobs are implemented or archolith-bench shows dimension/alpha/reranker settings improve quality, latency, cost, or stale/wrong-scope suppression. |
| `retrieval/intent-warden.md` | supported-by-eval | QueryIntent, ArtifactRole, IntentAffinity, IntentOracle (RELEVANCE family); task-intent-aware ranking + the oracle-vs-warden pairing rule | Bench graduated embedder-invariantly (`archolith_bench/intent/`, bench `1bf31fa`/`d3811a2`); shipped in `default_oracles()` (menhir `c979ca4`, `dcf795e`). Owner of the IntentOracle determination. |
| `direction/llm-reviewer-seams.md` | speculative | Where a bounded LLM reviewer belongs in menhir — the oracle/mutator-boundary review seam | Promote when a menhir spike defines a bounded reviewer seam or archolith-bench shows a reviewer pass improves a measured outcome. Independent fresh-pass review doc; structural-architecture synthesis (clustered in `direction/`). |
| `retrieval/facet-retrieval.md` | supported-by-spike | MemoryFacetIndex, MeetPointReranker, ExpansionDriftBreaker, RetrievalTransferFixture | Promotion condition #2 met: benchmark-local `MemoryFacetIndex`/`MeetPointReranker` + A–F ladder built in `archolith-bench` (`archolith_bench/facet/`) with a DEMO fixture comparison. → `supported-by-eval` when the real 50/20 fixture run clears the promotion gate. |
| `retrieval/facet-extraction-plan.md` | supported-by-spike | The extractor-improvement path for R2's "extracted facets fail": deterministic-from-structure vs git-inferred vs LLM-interpreted facets, the vague-query guard, per-source confidence, the hybrid extractor | Weekend Priority 6. Hybrid extractor built in the facet bench (`hybrid` mode): closes F recall 0.28→0.83 (gold 0.85) on the DRAFT fixture, re-graduates the gate. → `supported-by-eval` with real Layer-2/Git deterministic facets + a real extraction model + a real embedder. |
| `retrieval/oracle-amplified-retrieval.md` | supported-by-spike | RetrievalOracle, OracleResult, OracleExecutor, OracleCombiner, OracleAmplifiedRetrieval, MeasurementBudgetGate | Oracle bench prototype built (`archolith_bench/oracle/`, 38 tests) + combiner wired into recall on frontier (`30c58d0`, `3bac9b5`). R11 iterative amplification remains bench-gated: reject unless it beats the R7 one-pass combiner. |
| `retrieval/oracle-runtime-interfaces.md` | supported-by-spike | OracleInput/OracleFinding runtime contract, primitive-vs-composite oracle taxonomy, the two oracle altitudes (retrieval vs task), deterministic-vs-LLM I/O boundary | Interfaces drafted; AssertionPipeline wired observe-only into recall (`4450395`). Does not re-own the RetrievalOracle/combiner math (oracle-amplified-retrieval.md) or the write boundary (oracle-execution-and-performance.md); composite/brief layer sits in the L3/L4 GAP — needs ctharvey to sequence. |
| `schemas/layer4-knowledge-artifacts.md` | speculative | The generic L3/L4 knowledge-artifact **schema**: artifact types, knowledge-promotion lifecycle status, review state, evidence-as-first-class, temporal validity, supersession; the "generic artifacts interpreted by oracles" decision | Day-2 deliverable (Priority 4). Instantiates the SOS direction (`semantic-operating-system.md` owns it); this owns the schema. Spec only — Program B/D, the unsequenced GAP. Promote when a generic artifact store lands through the MemoryMutator (R9). **Much of the substrate already exists** (the `scope='CANDIDATE'` review tier, conflict/decay pipeline) — see the doc's "Prior art in menhir" reuse map; net-new = types + first-class Evidence node + LLM proposer. |
| `schemas/cold-start-brief.md` | speculative | The task-shaped **ColdStartBrief** schema, assembly pipeline, and context-pack provenance rules; the OraclePacket-vs-ColdStartBrief distinction | Day-2 deliverable (Priority 3 + 5). SOS owns the vision; this owns the schema. Spec only — Program E over the GAP. Promote when a ColdStartOracle produces a brief from the R7 OraclePacket + L3/L4 artifacts. |
| `retrieval/retrieval-control-rails.md` | speculative | CostAwareOracleScheduler, SelfReinforcementGuard, ProductiveTouchGate, EvidenceAnchorGate, RetrievalSpiralGuard failure modes | Promote when a menhir spike or archolith-bench fixture proves improved oracle tail latency, deterministic scheduling, stale-heat suppression, or self-reinforcement loop control. |
| `belief-temporal/connected-data-substrates.md` | speculative | hypergraph/n-ary representation, Datalog/differential-dataflow blast radius, semiring provenance, sheaf consistency, tensor factorization, ResearchScout lanes | Promote a lane only when a menhir spike or archolith-bench fixture shows it improves temporal blast radius, belief drift, or structure-aware recall over a Neo4j/Graphiti baseline. |
| `belief-temporal/tracehead-braidtrace.md` | parked | Tracehead, BraidTrace, BraidFrame, TraceCrossing, Cairn | Promote when there is a result object, formatter, or bench trace artifact using this vocabulary. Do not build the full BraidFrame graph schema until a fixture proves existing Episode/Entity/Fact projection is insufficient. |
| `retrieval/oracle-execution-and-performance.md` | supported-by-spike | Oracle/Combiner/Mutator write boundary, observe→decide→write rule, query-snapshot rule, oracle cost model + latency budget + hard caps, semantic-floor risk and source-aware candidate priors | Promotion condition #3 met by R1: source-aware candidate priors now let non-vector candidates survive the similarity floor (`domain/retrieval_tuning.py`, `services/scoring_service.py`). Still open: named MemoryMutator write boundary, and a retrieval trace with oracle phase timings + per-phase budgets. |
| `vision/cognitive-replay-and-phasing.md` | speculative | Development phase ladder (Memory Foundation → Progressive Retrieval → Temporal → Experience → Background Cognition → CIP → Model Adapters), Cognitive Replay capability, epistemic-separation law (observations/interpretations/decisions/state) | Promote Cognitive Replay when a replay query surface reconstructs past belief/understanding state (not a log-to-fixture test harness); promote the phase ladder when a release plan adopts it; the epistemic law extends the observe/decide/write boundary. Does not re-document the oracle pipeline (owned elsewhere). |
| `privacy/sealed-recall.md` | parked | MemoryIndex/MemoryBlob/KeyMap/AuditLog layering, envelope encryption, privacy levels L0-L4, local-embedding + top-k selective decrypt, SemanticShadowLeak threat-model caveat, Git/temporal decrypt-set narrowing; failure modes PlaintextMemoryDump / WholeStoreLLMIngestion / CodeMetadataLeak / UnaccountedDecrypt | Trust property, NOT a north-star recall mechanism — stays off the roadmap until earned. Promote to supported-by-spike when a Level-1 spike (local embed + encrypted-at-rest + vector search without full decrypt + top-k-only decrypt + audited) lands against a named failure mode; supported-by-eval when archolith-bench shows DecryptMinimization holds with no recall loss vs plaintext. No heavy crypto/PIR/TEE/HE dependency before a transparent baseline. |
| `privacy/trusted-memory-admission.md` | parked | AdmissionFirewall (admit_memory), memory namespaces, required source_type + trust_tier (T0-T6), signed provenance envelopes + ReplayGuard, object-level authz, promotion workflow over the existing CANDIDATE/conflict pipeline, admission audit; failure modes SilentUserInfoContamination / AgentInferenceLaundering / ToolOutputAsAssertion / LeakedTokenHighTrustWrite / MemoryEventReplay / CrossUserWrite / UnexplainableMemoryOrigin | Integrity/provenance counterpart to sealed-recall. Has a real seam into belief-layer (trust tier as confidence prior / evidence-attribution source). Promote to supported-by-spike on a Level-1 spike (required source_type/trust_tier + admission policy table + no agent/tool/external user_info writes + audit row); supported-by-eval when archolith-bench's ContaminationRate hits zero false durable user_info admission without dropping genuine user facts. Reuse existing namespace + scope=CANDIDATE + conflict pipeline; no signing/OAuth before Stage-1 policy + baseline. |

> Positioning/category docs (`archive/agent-experience-substrate.md`, `archive/cognitive-artifacts-and-software-cognition.md`, `archive/cognitive-infrastructure-platform.md`) have been consolidated into the canonical `positioning/positioning.md` (see Canonical docs and Superseded docs).

## Parked concepts (no dedicated doc)

| Concept | Status | Home | Promotion condition |
|---|---|---|---|
| Immune-system analogy | source metaphor only | this index + `belief-temporal/belief-layer.md` | Promote only concrete mechanisms: AnergicBeliefGate, RetrievalExhaustionPenalty, bounded structural expansion, SelfToleranceGate. |
| Gemcutting/faceting analogy | source metaphor only | `retrieval/facet-retrieval.md` | Promote only concrete mechanisms: MemoryFacetIndex, MeetPointReranker, ExpansionDriftBreaker, RetrievalTransferFixture. |
| Quantum search analogy | source mechanism only | `retrieval/oracle-amplified-retrieval.md` | Promote only classical mechanisms: RetrievalOracle, OracleCombiner, OracleAmplifiedRetrieval, MeasurementBudgetGate. Do not claim quantum speedup. |
| Umb/stone naming stack | parked/naming only | chat/issue comments | Do not promote unless it becomes product-facing terminology. |
| Real probabilistic-circuit backend | speculative | `belief-temporal/belief-layer.md` | Evaluate only after transparent BeliefCircuit/breaker baselines have bench artifacts showing need. |

## Superseded docs

These files are kept as historical pointers so old links do not break, but they are no longer the active source of truth:

```text
docs/research/archive/probabilistic-belief-layer.md
docs/research/archive/probabilistic-circuit-breakers.md
```

The active replacement is:

```text
docs/research/belief-temporal/belief-layer.md
```

The three positioning/category docs below were consolidated (no content lost —
each became a labelled lens in the canonical doc):

```text
docs/research/archive/agent-experience-substrate.md            -> positioning/positioning.md (Lens 2)
docs/research/archive/cognitive-artifacts-and-software-cognition.md -> positioning/positioning.md (Lens 3)
docs/research/archive/cognitive-infrastructure-platform.md     -> positioning/positioning.md (Lens 1)
```

The active replacement is:

```text
docs/research/positioning/positioning.md
```

The original CIP vision tag remains where Codex left it, as a futures pointer:

```text
.agent/memory-futures.md (Vision Tag: Cognitive Infrastructure Platform)
```

## Doc status values

Use these labels at the top of research docs:

```text
canonical:
  policy or process doc that governs future work.

active:
  live design doc tied to implementation or upcoming fixtures.

speculative:
  source-grounded idea, not yet code-backed.

parked:
  useful vocabulary or design direction, but no current implementation path.

superseded:
  replaced by another doc or rejected by later evidence.

supported-by-spike:
  implemented in a small branch/PR but not proven by bench.

supported-by-eval:
  has archolith-bench artifacts and baseline comparison.

rejected:
  intentionally not pursuing.

External comparison note:
  prior-art/ cluster only. Not a menhir idea in any promotion state — an analysis of an
  external system/paper's claims and their overlap with menhir. Does not use the
  promotion ladder below; a comparison "graduates" by informing a positioning or roadmap
  decision, not by becoming code.
```

## Promotion ladder

Ideas should move through this order:

```text
chat brainstorm
-> issue comment
-> research note
-> code spike
-> bench fixture
-> canonical doc
```

Most ideas should stop at issue comment.

Promote only when the idea has:

```text
specific mechanism
specific code surface
specific fixture
specific metric
```

## Anti-sprawl rules

```text
1. One owner doc per concept.
2. New ideas update the owner doc instead of creating duplicate docs.
3. Metaphors do not become subsystems by default.
4. Research docs must include a status and promotion condition.
5. Canonical claims require source cards, code, or bench artifacts.
6. archolith-bench decides whether implementation claims graduate.
7. Do not add heavy dependencies before a transparent baseline fails.
8. Retrieval tuning knobs are lower-stack configuration, not new memory theory.
9. Retrieval-control rails must not change final ranking nondeterministically.
10. Retrieval alone must not promote truth, currentness, or durable usefulness.
```

## Current durable save list

Keep these from the recent research/naming/synthesis work:

```text
Agent Experience Substrate (external category)
Agentic Context Control Plane (internal architecture name)
retrieval-as-control-system framing
cognitive artifact abstraction (memory is one artifact)
FATES lenses / scientific-instrument framing
software cognition category (vs AI memory)
distill-understanding design principle
Cognitive Infrastructure Platform (CIP) layering + primitives
decision-quality-per-token thesis
CIP bench metrics (Context Compression Ratio, Decision Accuracy per Retrieved Token, ...)
development phase ladder (Memory Foundation -> ... -> Model Adapters)
Cognitive Replay (replay cognitive/belief evolution, not just code)
epistemic-separation law (observations / interpretations / decisions / state)
Oracle / Combiner / Mutator observe-decide-write boundary (FATES = lens, not the writer)
MemoryMutator write surface
query-snapshot rule (oracles do not fetch the world)
oracle cost model / latency budget / hard caps
source-aware candidate priors (semantic-floor survival)
AnergicBeliefGate
HISTORICAL_ONLY / ANERGIC_CURRENT bucket split
productive-recency vs unproductive-recency
RetrievalExhaustionPenalty
bounded structural expansion
Tracehead / BraidTrace vocabulary, parked until code needs it
SelfToleranceGate as repo/branch/scope guard
EmbeddingDimensionSweep
HybridAlphaSearch
CrossEncoderRerankOracle
MemoryFacetIndex
MeetPointReranker
ExpansionDriftBreaker
RetrievalTransferFixture
RetrievalOracle
OracleResult
OracleExecutor
OracleCombiner
OracleAmplifiedRetrieval
MeasurementBudgetGate
role-specific log-space contradiction handling
OracleInput / OracleFinding runtime contract
primitive-vs-composite oracle taxonomy (ColdStart/Risk/Debug/Refactor/Planning/StaleKnowledge)
two oracle altitudes (retrieval per-candidate vs task per-run)
R7 OraclePacket (retrieval-shaped) vs ColdStartBrief (task-shaped) — do not overload "Cold Start Brief"
generic KnowledgeArtifact schema (one store, oracle-interpreted) + knowledge-promotion lifecycle
ColdStartBrief schema + context-pack provenance (epistemic label / evidence / requesting oracle / risk)
CostAwareOracleScheduler
SelfReinforcementGuard
ProductiveTouchGate
EvidenceAnchorGate
RetrievalGravityWell
MetaMemoryRecursion
SyntheticSupportLoop
StaleHeatLeak
ContextModeCollapse
Sealed Recall (encrypted content + local embeddings + selective decrypt; trust property)
MemoryIndex / MemoryBlob / KeyMap / AuditLog (sealed-recall layering)
envelope encryption (root -> project -> per-memory data key)
Sealed Recall privacy levels L0-L4
SemanticShadowLeak (vector index leaks approximate topics even when content is sealed)
DecryptMinimization / MetadataSealing bench metrics
Trusted Memory Admission (provenance-gated memory writes; integrity counterpart to Sealed Recall)
AdmissionFirewall / admit_memory (admit/downgrade/quarantine/propose/reject gate)
memory namespaces (user_info / user_observed / project_memory / external_claims / agent_inferences / proposed_user_info / ...)
source_type + trust_tier (T0 external -> T6 system_attested) as required write fields
SignedProvenanceEnvelope + ReplayGuard (nonce/timestamp/event_id)
ContaminationRate bench metric (false durable user_info admission rate)
```

Do not promote these yet:

```text
full immune architecture
full gemcutting architecture
quantum retrieval / quantum speedup claims
umb/stone naming stack
somatic vector mutation
synthetic recombination
clearance convergence
large persisted BraidFrame schema
real probabilistic-circuit engine adoption
ProjectionCalibrationLayer
OrientationAnchor / Dop Anchor
MemoryYieldMetric
RetrievalInterferenceGraph
HamiltonianRanker
phase-history encoding
entangled-memory terminology
Sealed Recall L4 research mode (secure kNN / TEE / HE / differential-privacy embeddings)
formal cryptographic privacy claims (no claim until prior-art lane + spike)
```

## Current next implementation targets

The ordered, dependency-aware build sequence (research → code → bench) now lives
in one execution plan:

```text
.agent/research/menhir-research-execution-ladder.md
```

That ladder maps each rung to its mechanism owner doc, code surface, archolith-
bench fixture, metric, and dependencies. Update the ladder — not this list —
when build order changes.

Guiding principle:

```text
Recent and useful should get hotter.
Recent and unproductive should cool down.
Superseded should become historical, not dead.
Semantic ranking is one signal, not the retrieval authority.
Fine-grained retrieval should be built from inspectable oracles, not hidden monolithic scores.
Parallel oracle execution should be fast, bounded, and deterministic.
Retrieval is evidence of attention, not evidence of truth.
Only external or productive outcomes can increase durable retrieval weight.
```
