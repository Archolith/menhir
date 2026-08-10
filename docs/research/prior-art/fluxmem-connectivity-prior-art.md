# FluxMem: Connectivity-Evolving Memory Prior-Art Analysis

> Status: External comparison note; use for positioning, roadmap triage, and benchmark planning.
> Research date: 2026-07-13
> Paper: *Rethinking Memory as Continuously Evolving Connectivity*
> arXiv: https://arxiv.org/abs/2605.28773v1
> Reference implementation: https://github.com/zjunlp/LightMem
> Related Menhir principle: `Event / Evidence -> deterministic Fold/Reconcile -> supersedable View -> recall`

## Executive summary

FluxMem is highly relevant to Menhir because it treats memory as a heterogeneous graph whose connectivity evolves through task execution, feedback, and consolidation. It formalizes context as an activated task-specific subgraph rather than a flat list of retrieved records.

The paper validates several broad directions already present in Menhir:

```text
memory is a graph, not merely a store
context is a task-specific activated region
retrieval quality is partly a connectivity problem
feedback can diagnose missing, noisy, or badly sized memory units
successful trajectories can consolidate into reusable procedural structures
```

FluxMem should change Menhir's novelty claims. Menhir should not claim novelty for heterogeneous graph memory, activated subgraph context, feedback-driven topology refinement, semantic/episodic/procedural layering, or procedural consolidation by themselves.

Menhir remains materially differentiated by its stronger truth and lifecycle boundaries:

```text
bitemporal and change-aware memory
append-oriented evidence and provenance
explicit fold/reconcile into supersedable views
Git/code-aware stale detection
verification and review receipts
document graphlets linked to code symbols
separation of canonical truth from adaptive activation topology
```

The architectural conclusion is:

```text
FluxMem may inspire how Menhir learns which connections to activate.
It should not determine how Menhir rewrites historical evidence or canonical truth.
```

Recommended future lane:

```text
Adaptive Activation Topology

Record which recalled nodes helped, were missing, or interfered,
then improve future context-subgraph construction without mutating
evidence, stale state, review receipts, or historical facts.
```

A concrete staged plan is documented in:

```text
docs/research/adaptive-activation-topology-plan.md
```

---

## 1. Paper status and implementation status

The paper was submitted to arXiv on 2026-05-27 as version 1 and labels itself ongoing work.

The paper points to the LightMem repository. As of this research note, LightMem includes a FluxMem implementation under `src/fluxmem/` and documents:

- semantic, episodic, and procedural node types
- grounding, distillation, and temporary step-link edges
- online initial retrieval
- feedback-driven refinement
- offline consolidation
- configurable PEMS convergence
- pluggable LLM, embedder, and vector-store interfaces

The LightMem repository is MIT licensed. That makes direct evaluation, fixture reuse, and optional adapter work relatively low-friction, subject to normal attribution and project review.

The implementation should still be treated as active research software rather than a settled standard.

---

## 2. FluxMem's core model

FluxMem models memory as a heterogeneous graph:

```text
G = (V, E)
```

Its durable memory nodes are divided into three functional layers.

### 2.1 Semantic knowledge

Semantic nodes contain factual knowledge used as evidence during task execution.

Examples include:

```text
knowledge documents
document chunks
factual statements
tool/API documentation
```

### 2.2 Episodic experiences

Episodic nodes contain concrete task trajectories, including observations and actions.

Examples include:

```text
debugging sessions
tool-use sequences
web navigation trajectories
past task attempts
```

### 2.3 Procedural skills

Procedural nodes contain reusable skills or reasoning patterns distilled from successful trajectories.

Examples include:

```text
multi-step planning heuristics
debugging procedures
recurrent tool-use patterns
successful task templates
```

### 2.4 Typed edges

The reference implementation documents three edge categories:

```text
GroundEdge
    Semantic -> Episodic
    A fact provides evidence for an episodic task step.

DistillEdge
    Episodic -> Procedural
    A reusable skill was distilled from one or more experiences.

StepLinkEdge
    Any -> Any
    A temporary connection activated for the current execution step.
```

This is important because FluxMem does not treat semantic, episodic, and procedural memory as three disconnected stores. They share one connectivity substrate.

---

## 3. Context as an activated subgraph

At each task step, FluxMem selects a task-specific local subgraph containing relevant nodes from all three layers.

Conceptually:

```text
current task and observation
-> retrieve semantic evidence
-> retrieve similar episodes
-> traverse from episodes to applicable procedures
-> construct step-local activated subgraph
-> serialize activated nodes into context
```

The paper's strongest architectural idea is:

```text
Optimizing context is equivalent to editing the topology of the activated subgraph.
```

This is more expressive than treating context building as independent top-k retrieval calls.

For Menhir, the equivalent shape could be:

```text
Task / Query
-> Concept
-> Memory or DocumentationClaim
-> Evidence / FactEvent
-> CodeSymbol
-> CodeChangeEvent
-> StaleState
-> VerificationReceipt
-> Procedural guidance
```

The context builder would serialize a coherent connected package rather than unrelated records that happen to score well independently.

---

## 4. Three-stage memory evolution

FluxMem evolves memory connectivity through three stages.

### Stage I: Initial connection formation

FluxMem initially retrieves relevant semantic, episodic, and procedural memories.

The paper combines:

```text
dense embedding similarity
sparse lexical/BM25 matching
LLM-based verification
```

The selected nodes form a preliminary task-local subgraph.

Menhir analogue:

```text
candidate generation
-> role/family routing
-> temporal/currentness constraints
-> graph expansion
-> initial context graph
```

### Stage II: Feedback-driven refinement

When execution feedback indicates failure, FluxMem attributes the problem to one of several structural failure modes and edits the activated subgraph.

#### Under-connection

Critical context was not activated.

Response:

```text
find likely missing nodes
add task-local links
retry with expanded context
```

#### Over-connection

Irrelevant context interfered or encouraged hallucination.

Response:

```text
identify distractor nodes/edges
prune them from the activated subgraph
retry with reduced context
```

#### Granularity mismatch

The retrieved memory unit was too broad or too narrow for the task.

FluxMem may reshape the memory unit's content to better match the required abstraction level.

This last operation is where Menhir requires a stronger safety boundary. Menhir should not silently rewrite evidence or authoritative historical records because one task preferred a different granularity.

A Menhir-safe response would be:

```text
preserve original evidence
record granularity feedback
produce or select a derived view
optionally propose a split, summary, or procedural candidate
require fold/reconcile or review before durable canonical mutation
```

### Stage III: Long-term consolidation

FluxMem clusters related successful episodes and distills recurrent patterns into procedural skills.

The skills are repeatedly evaluated and refined until a maturity metric converges.

Menhir analogue:

```text
successful episodes / tool traces
-> cluster or group by task family
-> propose reusable procedural circuit
-> replay/evaluate against source episodes
-> accumulate evidence and review
-> promote to a supersedable procedural view when mature
```

---

## 5. PEMS and memory maturity

FluxMem introduces PEMS as a convergence signal for procedural-memory generalizability and evolutionary maturity.

The implementation uses a configurable convergence threshold and repeatedly refines a procedural skill until its score stabilizes.

Menhir should not copy the metric blindly, but it should borrow the principle:

```text
A procedural memory is not mature merely because it was generated once.
```

A Menhir procedural-maturity score could eventually combine:

```text
successful applications
failed applications
source episode count
diversity of source tasks
contradiction rate
human/agent review confirmations
stability between versions
age since last correction
currentness of supporting evidence
```

Any such score must preserve the evidence supporting the promotion decision.

---

## 6. Mapping FluxMem to Menhir

| FluxMem | Menhir analogue | Important difference |
|---|---|---|
| Semantic node | Memory, Evidence, FactEvent, document claim | Menhir needs provenance and temporal validity |
| Episodic node | Tool/session event or bounded task episode | Menhir should avoid unbounded transcript capture |
| Procedural node | Procedure, skill, or supersedable operational view | Promotion should be evidence-backed and reviewable |
| GroundEdge | `SUPPORTED_BY`, `GROUNDED_IN`, evidence linkage | Menhir should preserve source evidence append-only |
| DistillEdge | `DERIVED_FROM`, procedural induction lineage | Menhir should preserve all source episodes and versions |
| StepLinkEdge | Temporary activation/co-retrieval edge | Should normally be ephemeral or receipt-backed |
| Activated subgraph | Context builder output graph | Menhir must force stale and safety warnings into serialization |
| Under-connection | Missing-memory / missing-evidence recall failure | Menhir can record this through pain and feedback receipts |
| Over-connection | Distractor/noise recall failure | Prune activation, not canonical evidence |
| Granularity reshaping | Split/merge/summary view proposal | Do not silently rewrite source memory |
| Consolidated procedural circuit | Mature procedure/skill view | Must be supersedable and provenance-preserving |

---

## 7. What Menhir should borrow

### 7.1 Explicit activation topology

Menhir should represent or trace the task-local graph used to build context.

At minimum, an activation trace should answer:

```text
Which nodes entered context?
Why was each node selected?
Which edge/path caused expansion?
Which warnings were mandatory?
Which nodes were omitted by budget?
Which nodes later proved useful, missing, or distracting?
```

### 7.2 Feedback taxonomy

The FluxMem failure taxonomy is directly useful:

```text
under_connection
over_connection
granularity_mismatch
```

Menhir can extend it with truth/lifecycle-specific categories:

```text
stale_context_used
current_evidence_missing
contradictory_view_selected
warning_dropped
wrong_project_or_scope
procedural_guidance_failed
```

### 7.3 Temporary versus durable edges

Menhir should distinguish:

```text
canonical/durable edges
    SUPPORTED_BY
    SUPERSEDES
    ANCHORED_TO
    ABOUT
    DEFINED_IN

activation/temporary edges
    ACTIVATED_WITH
    EXPANDED_FOR_TASK
    CO_RETRIEVED
    OMITTED_BY_BUDGET

learned-but-noncanonical routing hints
    HELPED_TASK_FAMILY
    DISTRACTED_TASK_FAMILY
    OFTEN_MISSING_WITH
```

The learned routing hints should influence recall but not become truth claims.

### 7.4 Procedural consolidation

Menhir should eventually support evidence-backed procedural circuits, but only after activation observability and feedback receipts exist.

---

## 8. What Menhir should not copy directly

### 8.1 Silent content rewriting

FluxMem may replace a memory unit with a reshaped unit when granularity is poor.

Menhir should instead use:

```text
Evidence remains immutable or append-oriented.
Derived views may be regenerated or superseded.
Granularity changes create proposed views with lineage.
```

### 8.2 Unqualified edge pruning

A connection that distracted one task may be essential for another.

Menhir should scope learned activation feedback by:

```text
project
task family
role
query type
time window
model/runtime configuration
```

Pruning should usually mean:

```text
do not activate this edge in this context
```

not:

```text
delete the canonical relationship
```

### 8.3 Treating success as truth

A successful task execution does not prove every retrieved memory was true.

Menhir should separately track:

```text
task utility
factual support
currentness
review status
```

### 8.4 Unbounded episode capture

FluxMem stores full task trajectories in its episodic layer.

Menhir's existing privacy and data-minimization goals require bounded, explicit episode capture. Do not capture full transcripts or file contents by default.

---

## 9. Menhir's required four-layer separation

The FluxMem comparison suggests a clean Menhir architecture.

### Layer 1: Evidence and events

```text
append-oriented
provenance-preserving
temporally scoped
not silently rewritten by task feedback
```

Examples:

```text
FactEvent
ToolEvent
CodeChangeEvent
ReviewReceipt
VerificationReceipt
```

### Layer 2: Canonical/materialized views

```text
deterministic fold/reconcile
current-state oriented
supersedable
stale-aware
rebuildable from evidence where possible
```

Examples:

```text
current memory view
current documentation state
current code-symbol view
current procedural recommendation
```

### Layer 3: Activation topology

```text
task-specific
adaptive
ephemeral or receipt-backed
safe to expand/prune
not itself a truth layer
```

Examples:

```text
activated subgraph
co-retrieval paths
budget decisions
helpful/distracting routing feedback
```

### Layer 4: Procedural circuits

```text
derived from repeated episodes
versioned
maturity-scored
supersedable
traceable to source experiences
```

This separation lets Menhir benefit from adaptive connectivity without compromising historical integrity.

---

## 10. Relevance to change-aware documentation

A document graphlet is a durable semantic region:

```text
Document
-> Section
-> DocumentationClaim
-> Concept
-> CodeAnchor
-> CodeSymbol
```

A documentation question should activate only the relevant connected region:

```text
Question about sync_prices retry behavior
-> CodeSymbol: sync_prices
-> anchored DocumentationClaim
-> owning Section
-> latest CodeChangeEvent
-> stale/current state
-> latest ReviewReceipt
-> supporting Evidence
```

Feedback can improve this activation topology:

```text
missing code diff
    -> under_connection

irrelevant neighboring documentation
    -> over_connection

whole design document retrieved for one narrow claim
    -> granularity_mismatch
```

But stale warnings and review state are mandatory truth/lifecycle context. A learned activation policy must never prune them when the associated claim is included.

Invariant:

```text
If a stale documentation claim enters context,
its stale warning and relevant change evidence must enter atomically.
```

---

## 11. Novelty boundary after FluxMem

Menhir should not claim novelty for:

```text
heterogeneous graph memory
semantic / episodic / procedural layers
context as a task-local activated subgraph
feedback-driven link expansion and pruning
procedural consolidation from repeated trajectories
maturity-based procedural refinement
```

The potentially distinct Menhir combination is:

```text
adaptive activation topology
+ append-oriented temporal evidence
+ deterministic fold/reconcile
+ supersedable canonical views
+ Git/code-aware stale propagation
+ document graphlets and code anchors
+ durable verification/review receipts
+ mandatory warning serialization
+ strict separation between utility learning and truth mutation
```

This should still be described as a system architecture and integration boundary rather than an absolute novelty claim until a broader academic review is completed.

---

## 12. Reuse options

### Option A: Conceptual reuse only

Borrow the architecture and feedback taxonomy while implementing Menhir-native activation receipts and routing logic.

Advantages:

```text
smallest dependency surface
fits existing Menhir graph and adapters
preserves Menhir lifecycle semantics
```

Recommended for the first slice.

### Option B: Evaluation against FluxMem

Run FluxMem on selected Menhir-style benchmark tasks and compare:

```text
flat retrieval
Menhir current recall/context
FluxMem activated subgraph
Menhir adaptive activation prototype
```

Useful measures:

```text
task success
context token cost
missing-evidence rate
distractor rate
stale-warning compliance
provenance coverage
```

### Option C: Optional adapter or component reuse

Because LightMem is MIT licensed and exposes pluggable interfaces, Menhir could later reuse or wrap:

```text
failure attribution prompts
activation refinement loop
PEMS implementation
benchmark harnesses
```

Any reuse should remain behind a clear adapter and must not bypass Menhir's evidence/view boundary.

---

## 13. Research and implementation risks

### Feedback quality

Incorrect failure attribution can reinforce bad routing.

Mitigations:

```text
store attribution evidence
allow unknown/ambiguous outcome
require repeated observations before durable routing changes
cap learned-edge influence
support rollback and supersession
```

### Feedback loops

If the system only activates what it already believes is useful, it may stop exploring missing context.

Mitigations:

```text
bounded exploration quota
periodic neutral baseline runs
family contribution caps
diversity-aware candidate generation
```

### Task-family overfitting

A useful connection for one task family may be harmful elsewhere.

Mitigation:

```text
scope learned activation hints narrowly
```

### Truth/utility conflation

A node can be useful yet false, stale, or misleading.

Mitigation:

```text
keep utility feedback separate from evidence/currentness/review state
```

### Privacy and retention

Episode capture can accidentally retain sensitive transcripts or file contents.

Mitigation:

```text
bounded structured receipts
no transcript capture by default
no file-content capture by default
explicit retention policy
```

---

## 14. Decision

Adopt FluxMem as major prior art for adaptive connectivity and activated-subgraph context.

Do not replace Menhir's evidence/view lifecycle with FluxMem's mutable-memory model.

Proceed with a Menhir-native lane:

```text
Adaptive Activation Topology
```

Build order:

```text
activation observability
-> structured feedback receipts
-> bounded learned routing hints
-> replay/benchmark evaluation
-> procedural circuit candidates
-> granularity-view proposals
```

The first implementation slice should be audit-only and should not change recall ranking yet.

---

## References

- Paper abstract: https://arxiv.org/abs/2605.28773v1
- Paper PDF: https://arxiv.org/pdf/2605.28773v1
- LightMem repository: https://github.com/zjunlp/LightMem
- FluxMem implementation overview: https://github.com/zjunlp/LightMem/blob/main/FluxMem.md
- Related implementation plan: `docs/research/adaptive-activation-topology-plan.md`
