# Retrieval control rails

## Status

speculative

> **2026-07-11 status update.** Promotion condition #2 is **met** — `SelfReinforcementGuard` and the
> guard set are built (`domain/self_reinforcement.py`, `domain/exhaustion.py`, `domain/diversity.py`),
> default-off / bench-gated; effective status is **supported-by-spike** for the self-reinforcement rail.
> `CostAwareOracleScheduler` (R5) remains unbuilt (planned). The oracle stack these rails wrap benched
> neutral-to-negative on LME (default-off). See [`README.md`](README.md).

## Promotion condition

This note becomes active when Menhir has either:

```text
1. a CostAwareOracleScheduler / OracleScheduler code spike, or
2. a SelfReinforcementGuard / RetrievalSpiralGuard code spike, or
3. an archolith-bench fixture showing retrieval self-reinforcement, stale heat leak, or oracle tail-latency behavior.
```

It becomes supported-by-eval only if bench artifacts show improved latency, determinism, stale suppression, loop control, or answer grounding against the current recall baseline.

## Purpose

This note captures two cross-cutting rails that came out of the oracle/retrieval discussion:

```text
1. keep oracle execution fast by scheduling likely long-running oracle jobs first;
2. keep memory chaining safe by preventing retrieval self-reinforcement loops.
```

This is not a new architecture branch.

It is a control layer around the existing stack:

```text
retrieval tuning stack
-> candidate generation
-> thread-safe oracles
-> cost-aware oracle scheduling
-> deterministic oracle combiner
-> self-reinforcement / spiral rails
-> BeliefLayer
-> mutator writes
```

## Rule zero

```text
A memory being retrieved is evidence of attention.
It is not evidence that the memory is true, current, or useful.
```

This rule exists to stop retrieval from reinforcing itself without external/productive evidence.

## Object 1: CostAwareOracleScheduler

Definition:

```text
A scheduler that orders oracle evaluation jobs by estimated runtime and resource class so slow or expensive oracle batches start early while cheap oracle work backfills unused executor capacity.
```

Research name:

```text
Longest-runner-first oracle scheduling
```

Code-facing name:

```text
CostAwareOracleScheduler
```

Why it matters:

```text
Oracle execution is mostly parallel, but one slow oracle can dominate wall time.
The scheduler should attack tail latency, not only average latency.
```

Bad shape:

```text
cheap oracles finish immediately
system waits on one late GitOracle / StructureOracle / CrossEncoderRerankOracle
```

Better shape:

```text
likely slow jobs launch first
cheap metadata oracles backfill executor slots
all oracle packets finish closer together
```

## Oracle cost classes

```text
CHEAP:
  scope checks, metadata checks, timestamp checks

IO:
  Neo4j, Git, file reads, HTTP, model server calls

EXPENSIVE:
  dependency cones, commit-range analysis, conflict-group expansion

MODEL:
  cross-encoder reranker, local NLI, local LLM verifier
```

Examples:

```text
ScopeOracle:
  CHEAP

TemporalOracle:
  CHEAP if metadata was prefetched

StructureOracle:
  IO/EXPENSIVE unless dependency cone was prefetched

GitOracle:
  IO/EXPENSIVE unless commit snapshot was prefetched

CrossEncoderRerankOracle:
  MODEL

ContradictionOracle:
  CHEAP/IO depending on conflict-group prefetch
```

## Scheduling policy

Minimum policy:

```text
1. prefetch shared snapshots first;
2. estimate oracle job runtime;
3. launch longest estimated jobs first;
4. backfill cheap jobs;
5. enforce per-oracle and per-class concurrency limits;
6. return missing/neutral OracleResult on timeout;
7. reduce results in deterministic order.
```

Pseudocode:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class OracleJob:
    oracle_name: str
    candidate_id: str
    estimated_ms: float
    cost_class: str


def schedule_oracle_jobs(jobs: list[OracleJob]) -> list[OracleJob]:
    return sorted(
        jobs,
        key=lambda job: (
            -job.estimated_ms,
            job.cost_class,
            job.oracle_name,
            job.candidate_id,
        ),
    )
```

Do not let longest-runner-first starve cheap work. Use lanes:

```text
cheap lane:
  metadata, scope, temporal over prefetched fields

io lane:
  graph, Git, file-backed oracles

model lane:
  reranker, NLI, verifier
```

## Runtime estimation

Start with static cost classes, then learn from telemetry:

```text
oracle_name
candidate_count
query_shape
snapshot_available
last_runtime_ms
p50_runtime_ms
p95_runtime_ms
timeout_rate
```

Schedule by estimated p95, not average, because the target is tail latency.

## Object 2: SelfReinforcementGuard

Definition:

```text
A guard that prevents retrieval events, agent summaries, and memory-call hints from making memories hotter unless there is external or productive evidence.
```

Alternative name:

```text
RetrievalSpiralGuard
```

Use SelfReinforcementGuard in code unless the more vivid failure-mode name is useful in research notes.

## Retrieval self-reinforcement loop

Bad loop:

```text
query
-> memory A retrieved
-> LLM sees A and frames answer around A
-> system records A was used / touched / relevant
-> A gets hotter
-> follow-up query retrieves A again
-> LLM writes summary/meta-memory saying A is important
-> meta-memory points back to A
-> A + meta-memory now dominate retrieval
```

This is a retrieval-time feedback loop. It is closer to recommender feedback than model collapse, because the model is not being retrained. But it can still create context collapse, stale dominance, and false confidence.

## Dangerous meta-memory recursion

The riskiest form is memory about how to call memories:

```text
M1:
  "The CE willow crash is caused by missing texture cache."

M2:
  "When debugging CE willow, retrieve M1 first."

M3:
  "The system successfully used M1 for CE willow debugging."

M4:
  "CE willow queries should prioritize the missing texture cache theory."
```

If later evidence says the issue was actually load order or compatibility, M1 can stay hot because M2/M3/M4 keep retrieving it.

## Failure modes to track

```text
RetrievalGravityWell:
  a memory or memory cluster becomes increasingly likely to retrieve itself.

MetaMemoryRecursion:
  memories about how to retrieve memories recursively retrieve one another.

SyntheticSupportLoop:
  agent-generated summaries become the main evidence for future agent-generated summaries.

StaleHeatLeak:
  stale/superseded memories keep gaining recency/prominence from repeated retrieval.

ContextModeCollapse:
  retrieved context loses diversity and repeatedly collapses to the same few memories.

ProductiveRecencyConfusion:
  system treats accessed as useful instead of requiring evidence of usefulness.
```

## Guard 1: ProductiveTouchGate

Do not make a memory hotter merely because it was retrieved.

Bad:

```text
retrieved -> last_accessed touched -> recency boost
```

Better:

```text
retrieved -> pending touch
pending touch becomes productive only if there is a productive outcome
```

Productive outcomes:

```text
user confirmation
test pass
code compiles
external source supports
accepted final answer
contradiction resolved
manual approval
```

Unproductive outcome:

```text
retrieved repeatedly
no new evidence
no user confirmation
no task progress
same answer loop continues
```

Policy:

```text
productive retrieval:
  may increase durable heat

unproductive retrieval:
  should not increase durable heat
  may receive session-local exhaustion penalty
```

## Guard 2: SyntheticSupportCap

Agent-generated memory should not recursively support itself.

```text
LLM summary can point to evidence.
LLM summary should not become the primary evidence forever.
```

Policy:

```text
If support_chain contains only agent-generated memories,
do not promote to current truth.
```

Allowed buckets:

```text
MENTION_WITH_UNCERTAINTY
HISTORICAL_ONLY
NEEDS_EVIDENCE
CONFLICT_SET
```

## Guard 3: MetaMemoryDepthBudget

Cap memory-call chains.

```text
max_meta_memory_depth = 1 or 2
```

Allowed:

```text
query -> memory-call hint -> factual memory
```

Suspicious:

```text
query -> memory-call hint -> memory-call hint -> memory-call hint -> factual memory
```

## Guard 4: RetrievalDiversityGate

If the same candidates dominate context, require alternate evidence families.

Trigger:

```text
top_memory_dominance > threshold
or retrieval_entropy < threshold
```

Required diversity families:

```text
semantic
temporal
Git
structure
user/log/test
belief/conflict
lexical/facet
```

This prevents one semantic cluster from monopolizing the context window.

## Guard 5: EvidenceAnchorGate

Every current-truth assertion needs at least one non-self anchor.

Anchors:

```text
user statement
log
test result
Git diff
file content
external source
manual confirmation
structured timestamp
```

Non-anchors:

```text
LLM said it before
summary of LLM said it before
retrieval trace said it was retrieved before
memory-call hint said to retrieve it
```

## Guard 6: RetrievalExhaustionPenalty

If a session repeatedly retrieves the same memory cluster without progress, apply a session-local penalty.

```text
same cluster keeps appearing
+ no new evidence
+ no productive outcome
= session-local exhaustion penalty
```

Do not globally delete or demote the memory based on one stuck session.

## Guard 7: ContradictionInterrupt

Contradiction should interrupt reinforcement.

```text
contradiction detected:
  freeze productive touches
  route to conflict/currentness review
  allow historical retrieval
  suppress current assertion until resolved
```

## Metrics

Scheduler metrics:

```text
oracle_wall_time_ms
oracle_parallel_speedup
oracle_p95_latency
oracle_tail_wait_ms
oracle_timeout_rate
ranking_determinism
```

Self-reinforcement metrics:

```text
retrieval_cycle_rate
self_reference_ratio
synthetic_support_ratio
evidence_anchor_ratio
retrieval_entropy
top_memory_dominance
meta_memory_depth
productive_touch_rate
unproductive_retrieval_streak
stale_reinforcement_rate
stale_heat_leak
context_mode_collapse_rate
```

## CE willow fixture

Use the CE willow sequence as a small self-reinforcement fixture.

Events:

```text
E1: CE willow texture-cache crash observed.
E2: Patch added.
E3: Agent writes "patch likely fixed issue."
E4: Agent writes "for CE willow, retrieve patch-fix memory."
E5: New evidence says compatibility/load-order issue remains.
E6: Load-order fix resolves issue.
```

Bad system:

```text
keeps retrieving E3/E4
keeps saying patch fixed it
promotes patch-fix meta-memory
misses E5/E6
```

Good system:

```text
retrieves E3/E4 as historical belief
suppresses them as current truth
retrieves E5/E6 for current answer
flags E4 as stale memory-call hint
```

Metrics:

```text
current_truth_accuracy
historical_preservation
meta_memory_recursion_depth
stale_heat_leak
retrieval_entropy
synthetic_support_ratio
productive_touch_rate
```

## Relationship to existing docs

```text
retrieval-tuning-stack.md:
  candidate-generation and semantic tuning knobs

facet-retrieval.md:
  deterministic structured candidate generation

oracle-amplified-retrieval.md:
  thread-safe oracles, oracle combiner, role-specific log-space scoring

belief-layer.md:
  current/historical/anergic/conflict assertion policy

retrieval-control-rails.md:
  scheduling and anti-spiral guardrails around the retrieval control loop
```

## First menhir spike

Candidate files:

```text
src/menhir/services/oracle_scheduler.py
src/menhir/services/self_reinforcement_guard.py
src/menhir/domain/retrieval_control.py
```

Minimum domain shape:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class OracleRuntimeEstimate:
    oracle_name: str
    cost_class: str
    estimated_ms: float
    p95_ms: float | None = None
    timeout_rate: float | None = None


@dataclass(frozen=True)
class RetrievalOutcomeSignal:
    memory_id: str
    productive: bool
    anchors: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class SelfReinforcementSignal:
    memory_id: str
    retrieval_count: int
    meta_depth: int
    synthetic_support_ratio: float
    evidence_anchor_ratio: float
    stale_heat_leak: bool
```

Minimum service behavior:

```text
schedule oracle jobs by estimated p95 runtime and cost lane
record oracle wall-time and timeout metrics
mark retrieval touches as pending, not immediately productive
require productive outcome before durable heat increase
apply session-local exhaustion penalty for repeated unproductive retrieval
block self-only support chains from current-truth promotion
```

## Non-goals

Do not:

```text
turn scheduler policy into a new retrieval theory
let oracle scheduling change final deterministic ranking
let retrieval alone promote memory truth/currentness/usefulness
let agent-generated summaries become primary evidence without anchors
turn every repeated retrieval into global decay/delete
block historical access to stale memories
```

## Recommendation

Build in this order:

```text
1. Add oracle runtime telemetry.
2. Add static cost classes and bounded lanes.
3. Add CostAwareOracleScheduler.
4. Add pending-touch vs productive-touch distinction.
5. Add SelfReinforcementGuard as session-local rail.
6. Add CE willow self-reinforcement fixture to archolith-bench.
```

Canonical rule to preserve:

```text
Retrieval is evidence of attention, not evidence of truth.
Only external or productive outcomes can increase durable retrieval weight.
```
