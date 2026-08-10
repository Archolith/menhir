# Oracle execution: write boundary, performance budget, and candidate priors

## Status

supported-by-spike

Condition #3 below is met by R1: source-aware candidate priors now let non-vector
(BM25/facet/structure/file) candidates survive the similarity floor
(`domain/retrieval_tuning.py`, `services/scoring_service.py`, `services/recall_service.py`).
Condition #1 (named MemoryMutator write boundary) remains open. Condition #2's phase-timing trace
is now built (`domain/retrieval_trace.py`, R0 RetrievalTrace — per-phase timings + per-candidate
source, opt-in `recall(trace=True)`), though the per-phase latency *budget* is not yet wired.

> **2026-07-11:** the oracle pipeline these operational concerns wrap is built but benched
> neutral-to-negative on LongMemEval and ships default-off; the active build direction is write-time
> consolidation. See [`README.md`](README.md).

## Promotion condition

This note became `supported-by-spike` when Menhir gained any of:

```text
1. an observe/decide/write split with a named MemoryMutator boundary in code, or
2. a retrieval trace with oracle phase timings and a per-phase latency budget, or
3. source-aware candidate priors that let non-vector candidates survive the
   current similarity floor.  [MET — R1]
```

It becomes supported-by-eval only if bench/trace artifacts show the budget caps
hold under load, or that source-aware priors recover non-vector candidates
without flooding context with wrong-scope hits.

## Purpose

This note owns the operational concerns that make the oracle pipeline safe and
fast in the real recall code. The conceptual oracle layer, the combiner math,
and the anti-spiral rails are owned elsewhere:

```text
oracle-amplified-retrieval.md:
  oracle interface, combiner math, thread-safety rules, amplification.

retrieval-control-rails.md:
  CostAwareOracleScheduler, SelfReinforcementGuard.

retrieval-tuning-stack.md:
  candidate generation and semantic tuning knobs.
```

This doc captures three things none of those own:

```text
1. the Oracle / Combiner / Mutator write boundary;
2. the performance/latency budget and snapshot rule;
3. the semantic-floor risk and source-aware candidate priors.
```

## 1. Oracle / Combiner / Mutator write boundary

The pipeline has three role classes, and the safety property is that they never
blur:

```text
Oracles observe.
Combiners decide.
Mutators write.
```

More fully:

```text
Oracle:
  read-only evidence scorer over a candidate memory. Reveals/answers.

Combiner:
  reduces evidence into role-specific logits. Decides role probabilities.

Mutator:
  the only layer allowed to change state, after the decision boundary.
```

Why the boundary matters for safety and concurrency:

```text
many oracles run in parallel
one combiner reduces evidence deterministically
mutators run only after the decision boundary
```

Code-facing vocabulary:

```text
RetrievalOracle + MemoryMutator
```

The clean rule:

```text
Oracles observe. Combiners decide. Mutators write.
```

Naming note: "FATES" / "Fates" is reserved for the observational *lenses* that
emit cognitive artifacts (see positioning.md, Lens 3) — the observe end of the
pipeline. The write boundary is the **Mutator**, never "Fate". Do not reattach
the Fates-as-writer mythology here.

### Mutator verbs

The Mutator's state changes decompose into three functional verbs (the split is
what matters, not any costume):

```text
create:
  spin a new memory thread

assign:
  set scope, weight, lifespan, role

expire:
  cut, expire, delete, block, or prune
```

## 2. Performance simulation and the snapshot rule

The system stays performant only if oracles read from a prefetched snapshot
instead of fetching live per candidate.

Rule:

```text
Oracles do not fetch the world.
Oracles read from a query snapshot.
```

Bad:

```text
GitOracle(candidate) calls git for every candidate.
StructureOracle(candidate) hits Neo4j for every candidate.
BeliefOracle(candidate) fetches belief state for every candidate.
```

Good:

```text
Before oracle fan-out:
  compute commit-range snapshot once
  compute dependency cone once
  bulk-fetch candidate metadata once
  bulk-fetch belief/conflict fields once

During oracle fan-out:
  each oracle reads the snapshot and returns OracleResult
```

### Cost model

```text
T_oracles ~= T_prefetch + ceil((N * M) / C) * L + T_reduce

N = candidate count
M = oracle count
C = bounded concurrency
L = average oracle evaluation latency
```

Regimes:

```text
Good case:
  N=100, M=6 cheap oracles, C=24, L=0.1-0.5 ms snapshot lookup
  -> roughly 5-20 ms plus overhead

Acceptable case:
  N=200, M=8 oracles, C=32, L=0.5-2 ms
  -> roughly 25-100 ms plus overhead

Bad case:
  N=200, M=8 oracles, C=16, L=20 ms live DB/Git call per oracle
  -> roughly 2 seconds

Disaster case:
  N=300, M=10, each oracle shells to Git / hits Neo4j / runs a model
  -> multi-second latency and graph/database pressure
```

### Latency impact by stage

```text
current baseline:                                        baseline
add BM25/lexical candidate path:                         +5-40 ms
add facet candidate path:                                +5-30 ms
add cheap oracles over metadata:                         +5-30 ms
add structure oracle (prefetched dependency cone):       +10-60 ms
add Git oracle (prefetched commit range):                +20-100 ms
add belief/contradiction oracle (bulk fetch):            +20-100 ms
add local reranker top 25-50:                            +100-800 ms (model dependent)
iterative amplification reusing oracle results:          +5-50 ms
iterative amplification re-running expensive oracles:    bad, likely seconds
```

### Hard caps to start

```text
candidate_k:
  50 default, 100 max for normal recall, 200 only for bench/deep mode

cheap_oracle_timeout:
  25-50 ms

expensive_oracle_timeout:
  250-750 ms

max_total_oracle_concurrency:
  16 or 32

rerank_top_n:
  25 default, 50 max

amplification_iterations:
  0 initially, then 2-3 in simulator only
```

### Fallback rule

```text
If the oracle phase exceeds budget,
return the current baseline ranking plus partial oracle annotations.
```

Performance metrics:

```text
oracle_p95_latency
oracle_tail_wait_ms
oracle_cost_budget_used
per_oracle_timeout_rate
partial_oracle_result_rate
fallback_to_baseline_rate
```

## 3. Semantic floor and source-aware candidate priors

Current scoring (`src/menhir/services/scoring_service.py`,
`src/menhir/services/recall_service.py`) applies a semantic-similarity floor.
That floor will silently drop BM25/facet/structure candidates unless they are
handled explicitly.

Options:

```text
1. assign source-specific baseline similarity;
2. change the min-similarity filter into a candidate-source-aware filter;
3. move filtering after oracle scoring;
4. add an explicit candidate prior separate from semantic similarity.
```

There is already precedent in the codebase:

```text
file-linked structural candidates are injected with a baseline similarity
so they survive the semantic floor.
```

Recommendation:

```text
Do not simply shove BM25 candidates into existing scoring.
Give each candidate source an explicit candidate prior.
```

Candidate prior sources:

```text
vector
BM25
facet
structure
file-context
pending
manual/user-confirmed
Git/test/log-linked
```

## Candidate code surfaces

```text
src/menhir/services/oracle_executor.py     # bounded parallel execution + snapshots
src/menhir/services/oracle_scheduler.py    # CostAwareOracleScheduler (owned by control-rails)
src/menhir/services/memory_mutator.py      # the write boundary (Mutator) — NAME/CONSOLIDATE, not new:
                                           #   the write ops already exist scattered across
                                           #   candidate_repository.promote_candidate/delete,
                                           #   ConsolidationRepository (decay + conflict-resolve),
                                           #   memory_graph_adapter. R9 unifies them + adds the
                                           #   no-write-in-evaluate assertion.
src/menhir/services/recall_service.py      # source-aware candidate prior / floor change
src/menhir/services/scoring_service.py     # min-similarity -> source-aware filter
```

> Prior-art note (2026-06-28 audit): the observe/decide/write split's *write* side is largely
> implemented — the `scope='CANDIDATE'` review tier (`candidate_repository.py`, `CandidateService`),
> conflict resolution, and decay all exist. Promotion condition #1 ("named MemoryMutator boundary")
> is therefore a *consolidation* of existing operations, not a from-scratch build.

## Non-goals

Do not:

```text
let an oracle write state during evaluate()
let a mutator run before the combiner decides
call the write boundary "Fate"; FATES is the lens/observe side — the writer is the Mutator
run live per-candidate Git/Neo4j/model calls inside oracle fan-out
add BM25/facet candidates without an explicit source prior
let the reranker or expensive oracles run without a budget/fallback
```

## Recommendation

Build in this order:

```text
1. Add retrieval trace with phase timings and candidate source.
2. Add source-aware candidate priors so non-vector candidates survive the floor.
3. Prefetch query snapshots (commit range, dependency cone, bulk metadata).
4. Enforce per-phase budgets and the fallback-to-baseline rule.
5. Name the MemoryMutator write boundary; keep oracles read-only.
```

Canonical rules to preserve:

```text
Oracles observe. Combiners decide. Mutators write.
Oracles do not fetch the world; they read from a query snapshot.
```
