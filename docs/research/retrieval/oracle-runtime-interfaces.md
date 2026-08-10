# Oracle Runtime: interfaces, taxonomy, and the two oracle altitudes

## Status

supported-by-spike

> **2026-07-11:** the *retrieval*-altitude oracles this doc references (R4-R7) are now ported into
> `src` but benched neutral-to-negative on LongMemEval (default-off). The *task*-altitude / composite
> layer this doc actually owns (`OracleInput` / `OracleFinding`, `ColdStartOracle`) is still
> **spec-only** — it depends on the unsequenced L3/L4 GAP. See [`README.md`](README.md) and the SOS
> build-status note.

This is the **Day-1 deliverable** of `docs/roadmap/weekend-oracle-runtime-roadmap.md`
(Oracle Runtime spec): the input/output schema, the primitive/composite taxonomy, combiner
responsibilities, and the deterministic-vs-LLM boundary. It is **spec only — no code yet** — and the
composite/runtime layer it defines sits in the SOS **Program E / the L3/L4 GAP**, which the execution
ladder says is **ctharvey's to sequence before building** (see "Where this lands" below). Do not
implement composite oracles or invent a numbered rung from this doc.

## Promotion condition

Becomes `supported-by-spike` when a menhir spike defines `OracleInput` / `OracleFinding` and at least
one composite oracle (e.g. `ColdStartOracle`) that synthesizes primitive findings without mutating
state. Becomes `supported-by-eval` only when archolith-bench measures a composite oracle's output
against a task fixture (e.g. Cold Start Brief completeness / decision-accuracy-per-token), not just
retrieval recall.

## Purpose

The corpus already specifies an oracle layer, but at **one altitude**: the *retrieval* oracle —
`RetrievalOracle.evaluate(query, candidate) -> OracleResult`, a per-candidate evidence scorer whose
results a combiner reduces into ranking logits (`oracle-amplified-retrieval.md`, ladder R4–R7).

The weekend roadmap introduces a **second altitude**: a *task* oracle —
`Oracle(OracleInput) -> OracleFinding`, which reasons about a whole task and emits a finding (facts,
hypotheses, evidence, risk, suggested context) that ultimately becomes the **Cold Start Brief**.

These are not the same object and must not be collapsed into one. This doc owns the **runtime
contract** that connects them: the `OracleInput`/`OracleFinding` schema, the primitive-vs-composite
taxonomy, and how a task flows `primitive evidence -> combiner -> composite synthesis -> brief`. It
does **not** re-own the retrieval interface or the combiner math (those stay in
`oracle-amplified-retrieval.md`) or the write boundary / budget rules (those stay in
`oracle-execution-and-performance.md`).

## The two altitudes (the central reconciliation)

```text
Altitude 1 — RETRIEVAL evidence (per candidate)        owner: oracle-amplified-retrieval.md
  RetrievalOracle.evaluate(QueryContext, CandidateMemory) -> OracleResult
  "Does THIS candidate satisfy this evidence property?"   ladder: R4 (interface) / R6 (cheap oracles)

Reduction — RANKING                                     owner: oracle-amplified-retrieval.md (math)
  OracleCombiner(OracleResult[]) -> role logits / OraclePacket   ladder: R7 (killer baseline)
  "Combine per-candidate evidence into z_relevant/current/historical/conflict/blocked."

Altitude 2 — TASK reasoning (per task)                  owner: THIS doc
  Oracle(OracleInput) -> OracleFinding                  ladder: Program E / GAP (unsequenced)
  "Given the task + assembled context + budget, what is known / hypothesized / risky?"

Packaging — CONTEXT                                     owner: oracle-architecture.md
  ContextEngine(OracleFinding[] / brief) -> context pack
  "Package the smallest useful, provenance-tagged context for the target model."
```

The two altitudes share **evidence sources** (structure, git, test, temporal, belief, semantic,
evidence/provenance, scope) but consume them in **two modes**:

```text
retrieval mode:  score a candidate          -> OracleResult   (rank the pool)
task mode:       read a class of evidence    -> OracleFinding  (brief the agent)
```

The bridge is the combiner: a composite (task) oracle MAY consume the combiner's ranked output as one
of its inputs, but a task oracle is broader — it also reads structural/semantic/institutional context
that is not a "candidate" at all.

## OracleInput

What every task oracle receives. Immutable; assembled once before oracle fan-out (the query-snapshot
rule from `oracle-execution-and-performance.md` applies — oracles do not fetch the world).

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class RunMode(str, Enum):
    COLD_START = "cold_start"   # brief an agent before it begins
    RISK = "risk"              # what could this change break?
    DEBUG = "debug"           # what does the failing behavior touch?
    REFACTOR = "refactor"     # what depends on this shape?
    PLANNING = "planning"     # what constrains the approach?
    STALE_CHECK = "stale_check"  # what trusted knowledge is now contradicted?


@dataclass(frozen=True)
class OracleInput:
    task: str                                  # the agent's task / question
    run_mode: RunMode
    # Layer-keyed context snapshots (deterministic vs interpretive — see boundary below):
    structural_context: Mapping[str, object]   # Layer 2: symbols, deps, tests, git anchors (FACT)
    semantic_context: Mapping[str, object]     # Layer 3: capabilities/policies/constraints (EVIDENCE-BACKED)
    institutional_knowledge: Mapping[str, object]  # Layer 4: decisions, incidents, failures (EVIDENCE-BACKED)
    budget: "OracleBudget"
    as_of_time: str | None = None              # temporal anchor; None = now
    scope: Mapping[str, object] = field(default_factory=dict)  # repo/branch/project/namespace


@dataclass(frozen=True)
class OracleBudget:
    max_latency_ms: int
    max_tokens: int | None = None
    max_llm_calls: int = 0          # 0 = deterministic-only oracle; >0 allows interpretive synthesis
    cost_class: str = "cheap"       # cheap | io | expensive | model (see CostAwareOracleScheduler, R5)
```

Note: `structural_context`/`semantic_context`/`institutional_knowledge` are **snapshots**, not live
handles. Building them is the Context-Engine/retrieval job that runs *before* the oracle phase; a task
oracle reads them, it does not query Neo4j/Git per call.

## OracleFinding

What every oracle returns. The **fact/hypothesis split is the spine rule made structural**: an oracle
may *propose* hypotheses, but facts must be deterministic and evidence-backed, and nothing here is
allowed to silently become truth downstream.

```python
class FindingType(str, Enum):
    SUMMARY = "summary"
    RELEVANT_CONTEXT = "relevant_context"
    RISK = "risk"
    CONTRADICTION = "contradiction"
    OPEN_QUESTION = "open_question"


@dataclass(frozen=True)
class Evidence:
    kind: str                # git | test | log | structure | user | agent_inference | semantic_node
    ref: str                 # anchor: commit, file:symbol, test id, memory id, node id
    directness: float = 1.0  # direct evidence vs inferred
    note: str | None = None


@dataclass(frozen=True)
class OracleFinding:
    oracle_name: str
    finding_type: FindingType
    summary: str
    facts: tuple[str, ...] = ()         # deterministic, each must carry >=1 Evidence anchor
    hypotheses: tuple[str, ...] = ()    # interpretive; MUST stay labelled as hypothesis downstream
    evidence: tuple[Evidence, ...] = ()
    confidence: float = 0.0             # calibrated; applies to hypotheses, not to anchored facts
    risk: float = 0.0
    open_questions: tuple[str, ...] = ()
    suggested_context: tuple[str, ...] = ()  # anchors the Context Engine MAY include (it decides size)
```

Hard rules (inherited, not re-litigated here):

```text
1. Oracles observe. Combiners decide. Mutators write.   (oracle-execution-and-performance.md)
   An oracle MUST NOT change graph/lifecycle/belief/recall state inside its evaluate.
2. A fact without an Evidence anchor is not a fact — demote it to a hypothesis.
3. confidence is a property of hypotheses; deterministic facts are not "80% true".
4. suggested_context is a request, not a guarantee — the Context Engine packages, it does not obey.
5. Parallel oracle execution must not make output nondeterministic (deterministic reduction order).
```

## Primitive vs composite oracles

```text
Primitive oracle:
  reads ONE class of evidence; cheap; deterministic by default (max_llm_calls = 0).
  StructureOracle · GitOracle · TestOracle · MemoryOracle · SemanticNodeOracle ·
  TemporalOracle · EvidenceOracle
  (in RETRIEVAL mode these are exactly the RetrievalOracle set in oracle-amplified-retrieval.md,
   emitting OracleResult per candidate; in TASK mode they emit an OracleFinding over the snapshot.)

Composite oracle:
  synthesizes primitive findings (and/or the combiner's ranked output) for a task; one per RunMode;
  MAY use an LLM (max_llm_calls > 0) but only to produce HYPOTHESES, never to assert facts.
  ColdStartOracle · RiskOracle · RefactorOracle · DebugOracle · PlanningOracle · StaleKnowledgeOracle
```

This split is the anti-sprawl guard the roadmap calls for: every future task does **not** become a
one-off subsystem — it becomes a composite oracle over the same primitives.

```text
RunMode          composite oracle      reads (primitives)
cold_start   ->  ColdStartOracle    -> all primitives + R7 OraclePacket   -> ColdStartBrief
risk         ->  RiskOracle         -> Structure + Git + Test + Memory(incidents)
debug        ->  DebugOracle        -> Structure + Git + Test + Temporal
refactor     ->  RefactorOracle     -> Structure + Test + SemanticNode
planning     ->  PlanningOracle     -> SemanticNode + Memory(decisions) + EvidenceOracle
stale_check  ->  StaleKnowledgeOracle -> Temporal + Memory + EvidenceOracle
```

## Combiner responsibilities (reference, not re-derivation)

The combiner math (role-specific log-space logits, contradiction as negative log-evidence,
source-family independence caps, missing-evidence ≠ falsity) is **owned by
`oracle-amplified-retrieval.md`** and is not restated here. In the runtime the combiner has two
jobs:

```text
1. RETRIEVAL: reduce per-candidate OracleResult[] into role logits -> ranked pool (ladder R7).
2. TASK: provide that reduced, ranked, contradiction-aware view as ONE input to a composite oracle,
   so the composite oracle reasons over a decided ranking instead of raw candidate noise.
```

The combiner is still the only component allowed to *synthesize across* oracle outputs for ranking;
a composite oracle synthesizes for a *task answer*. Both are "decide", never "write".

## Deterministic vs LLM boundary

```text
Deterministic (never an LLM):
  Layer 1/2 = source + structural_context. Facts and their Evidence anchors. Ranking determinism.
  Primitive oracles default to max_llm_calls = 0.

Interpretive (LLM allowed, evidence-backed, carries confidence/provenance):
  Layer 3/4 = semantic_context + institutional_knowledge. hypotheses in an OracleFinding.
  Composite oracles MAY call an LLM (within budget) — output lands as hypotheses, never facts.

Forbidden:
  an LLM minting a "fact"; an oracle mutating state; a hypothesis losing its label downstream;
  retrieval rank alone promoting truth/currentness (retrieval is evidence of attention, not truth).
```

This is the structural-vs-semantic spine of the SOS direction, applied to the oracle I/O contract.

## Where this lands (ladder reconciliation — no new rung)

```text
RetrievalOracle / OracleResult         -> ladder R4 (interface) + R6 (cheap oracles)   [owned: oracle-amplified-retrieval.md]
OracleCombiner -> R7 OraclePacket      -> ladder R7 (killer baseline)                   [owned: oracle-amplified-retrieval.md]
OracleInput / OracleFinding (this doc) -> the runtime CONTRACT both altitudes share     [owned: THIS doc]
Composite oracles + ColdStartBrief     -> SOS Program E / the L3/L4 GAP — UNSEQUENCED    [needs ctharvey]
Context Engine packaging               -> SOS Program E                                  [owned: oracle-architecture.md]
```

Per the execution ladder's "SOS direction reconciliation": Program E (oracle-driven context assembly)
maps to R4→R5→R6→R7. **Keep two artifacts distinct — do not overload "Cold Start Brief":**

```text
R7 OraclePacket = the RETRIEVAL-shaped evidence packet — the combiner's output: ranked candidates,
                  role logits, contradiction-aware reduction. It is NOT the brief.
ColdStartBrief  = the TASK-shaped synthesized brief — what ColdStartOracle produces by reasoning
                  across primitives, Layer 3/4 context, AND the R7 OraclePacket.
```

The OraclePacket is the kernel the brief is *built from*, not the brief itself. The task-shaped
`ColdStartBrief` is the richer artifact this doc specs, and it depends on the Layer-3/Layer-4 semantic
overlay (Program B/D) that the ladder flags as the unsequenced GAP. So: **build R4–R7 first** (they
need no LLM and no L3/L4, and they yield the OraclePacket); treat composite oracles and the
`ColdStartBrief` as design-only until ctharvey sequences the L3/L4 track.

## Open questions (for sequencing)

```text
1. Does ColdStartOracle consume the R7 OraclePacket directly, or re-run primitives in task mode?
   (Prefer: consume the OraclePacket as one input; do not double-pay the oracle fan-out.)
2. Cold Start Brief schema lives in the weekend roadmap (Priority 3) — does it become its own owner
   doc, or a section here, once it has a code surface? (Anti-sprawl rule 1: one owner per concept.)
3. Layer-4 knowledge-artifact schema (weekend Priority 4) is a sibling spec, not part of this contract
   — keep them separate; this doc owns the I/O contract, that owns storage.
4. Which composite oracles earn a bench fixture first? (ColdStartOracle is the headline; measure with
   Decision Accuracy per Retrieved Token, per the CIP metrics track.)
```

## Non-goals

```text
do not implement composite oracles or a runtime from this doc — it is a spec
do not invent a numbered ladder rung for the composite/brief layer (it is the unsequenced GAP)
do not re-own the RetrievalOracle interface or combiner math (oracle-amplified-retrieval.md)
do not re-own the write boundary / budget / snapshot rules (oracle-execution-and-performance.md)
do not let a composite oracle mutate state or mint facts via LLM
do not collapse the two altitudes into one object
```
