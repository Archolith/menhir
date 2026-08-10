# Oracle-amplified retrieval for temporal code memory

## Status

supported-by-spike

> **2026-07-11 status update.** Promotion condition #1 is **met** — the RetrievalOracle / OracleExecutor
> / OracleCombiner spike is ported into `src` (`domain/oracles.py`, `services/oracle_executor.py`,
> `services/retrieval_oracles.py`, `domain/oracle_combiner.py`). But the base R4-R7 oracle pipeline was
> then **benched neutral-to-negative on LongMemEval** (node-only 0.400 > full stack 0.333) and ships
> **default-off**; **R11 iterative amplification remains bench-gated / blocked** (it never beat the R7
> one-pass combiner, which itself lost). So this does NOT graduate to supported-by-eval. The active
> direction moved to write-time consolidation — see the retrieval cluster note ([`README.md`](README.md))
> and the execution ladder's "Bench verdicts — reconciliation".

## What shipped vs what's blocked (maturity map — read first)

This doc spans two maturities. Do not read it as one uniformly-speculative note.

```text
SHIPPED (in src/menhir, default-off, benched neutral-to-negative on LongMemEval):
  R4  RetrievalOracle interface        domain/oracles.py
  R6  cheap oracle set                 services/retrieval_oracles.py (default_oracles)
  --  bounded parallel executor        services/oracle_executor.py
  R7  one-pass OracleCombiner          domain/oracle_combiner.py (role-specific log-space logits)
      wired into recall/assertion       services/recall_service.py, services/assertion_pipeline.py
  Bench verdict: node-only 0.400 > full stack 0.333 -> ships OFF, not as a win.

BLOCKED / NOT in src (failed the doc's own "killer baseline" gate):
  R11 OracleAmplifiedRetrieval         iterative amplification never beat the one-pass R7 combiner
      MeasurementBudgetGate            bench-next companion to R11; unbuilt

PARKED (explicitly, promotion-gated on a fixture that needs global optimization):
  RetrievalInterferenceGraph · HamiltonianRanker
```

The oracle *abstraction* (R4-R7) is the durable, shipped contribution; iterative
*amplification* (R11) is the frontier claim this doc still owns but which is blocked until a
fixture shows it beats the one-pass combiner. Everything below elaborates both — the maturity
of each object is fixed by this map, not by its section's tone.

## Promotion condition

This note becomes active when there is either:

```text
1. a menhir code spike defining RetrievalOracle / OracleResult, or
2. an archolith-bench simulator comparing one-pass scoring against iterative oracle amplification.
```

It becomes supported-by-eval only if iterative oracle amplification beats one-pass weighted oracle scoring on buried-memory, stale-memory, wrong-scope, or temporal-code retrieval fixtures.

## Purpose

This note captures a mechanism-transfer pass from quantum-style search thinking into Menhir retrieval.

The goal is **not** to emulate quantum hardware or claim quantum speedups.

The useful transferable mechanism is:

```text
Manipulate a probability distribution over candidates using independent property tests,
instead of committing too early to a single nearest-neighbor ranking.
```

For Menhir, the important durable abstraction is the oracle layer:

```text
RetrievalOracle = a composable evidence predicate/scorer over a candidate memory.
```

Oracle separation gives Menhir a fine-grained query surface:

```text
Find memories that are semantically related,
valid at time T,
learned before time U,
inside this dependency cone,
touching this symbol,
not superseded,
from this repo/branch,
and supported by test or Git evidence.
```

## Non-quantum warning

Use quantum ideas as mechanism inspiration only.

Do not claim:

```text
quantum retrieval
quantum speedup
true amplitude behavior
true superposition over memory
true quantum interference
```

The honest framing is:

```text
quantum-inspired distribution manipulation for classical retrieval
```

or more plainly:

```text
oracle-driven probabilistic retrieval
```

## Core mismatch with standard semantic retrieval

Standard semantic retrieval asks:

```text
Which memories are closest to this query embedding?
```

Oracle-amplified retrieval asks:

```text
Can we maintain a probability distribution over candidate memories and amplify
candidates that satisfy independent evidence properties?
```

That distinction matters because Menhir has evidence channels that embeddings do not represent cleanly:

```text
valid-time / learned-time
expired or superseded state
Git commit range
file/symbol/test structure
dependency cone
source reliability
belief bucket
contradiction state
repo / branch / namespace scope
```

## Existing Menhir substrate

Menhir already has several pieces that can become oracles:

```text
semantic similarity from vector retrieval
candidate metadata and scoring
adjacency / graph context
file-context structural injection
scope/freshness/namespace filtering
blast-radius traversal
BeliefCircuit heads and recall buckets
lifecycle state: active/compressed/gone
```

This note proposes wrapping those evidence channels in a common oracle interface before trying to build a new retrieval engine.

## Primary object: RetrievalOracle

Definition:

```text
A RetrievalOracle evaluates one evidence property of a candidate memory for a query.
It returns a calibrated probability-like score, confidence, evidence notes, contradiction notes, and missing-data notes.
```

Suggested interface:

```python
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol


class OraclePolarity(str, Enum):
    SUPPORT = "support"
    CONTRADICT = "contradict"
    MISSING = "missing"
    NEUTRAL = "neutral"


class OracleTarget(str, Enum):
    RELEVANCE = "relevance"
    CURRENTNESS = "currentness"
    HISTORICALITY = "historicality"
    CONFLICT = "conflict"
    SCOPE = "scope"
    SAFETY = "safety"


@dataclass(frozen=True)
class QueryContext:
    text: str
    intent: str | None = None
    as_of_time: str | None = None
    repo: str | None = None
    branch: str | None = None
    project: str | None = None
    file: str | None = None
    symbol: str | None = None
    test: str | None = None
    namespace: str | None = None


@dataclass(frozen=True)
class CandidateMemory:
    id: str
    content: str | None
    scope: str
    memory_type: str
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class OracleResult:
    oracle: str
    probability: float
    confidence: float
    polarity: OraclePolarity = OraclePolarity.SUPPORT
    target: OracleTarget = OracleTarget.RELEVANCE
    directness: float = 1.0
    scope_match: float = 1.0
    source_family: str | None = None
    supports: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


class RetrievalOracle(Protocol):
    name: str

    def evaluate(
        self,
        query: QueryContext,
        candidate: CandidateMemory,
    ) -> OracleResult:
        ...
```

Important rules:

```text
An oracle result is evidence, not final truth.
The combiner/ranker decides how much each oracle matters for a query.
The oracle API should be immutable and thread-safe by default.
```

## Thread-safe oracle execution model

Oracle separation is useful only if it stays fast. Most oracle calls are independent across candidates and across oracle types, so the API should be designed for parallel execution from the start.

### Concurrency goal

```text
Evaluate many independent oracles in parallel without shared mutable state,
then combine results deterministically after all oracle calls finish.
```

### Thread-safety rules

```text
1. QueryContext, CandidateMemory, and OracleResult are immutable value objects.
2. Oracles should be stateless or use explicitly thread-safe caches.
3. Oracles must not mutate CandidateMemory metadata.
4. Oracles must not write to the graph, lifecycle state, telemetry, or recall state during evaluate().
5. All side effects happen outside evaluate(), after the combiner has selected an action.
6. The combiner reduces OracleResult values in deterministic order.
7. Shared expensive resources use bounded pools, not unbounded per-candidate calls.
```

### Safe parallel shape

```python
async def evaluate_oracles(query, candidates, oracles, executor):
    tasks = []
    for candidate in candidates:
        for oracle in oracles:
            tasks.append(executor.submit(oracle.evaluate, query, candidate))

    results = await gather_bounded(tasks)
    return group_by_candidate_then_oracle(results)
```

The executor may be:

```text
thread pool for CPU/light IO or existing sync code
async task group for async IO or Neo4j/HTTP-backed oracles
process pool only for CPU-heavy local models, if needed later
```

### Deterministic reduction

Parallel oracle execution must not make ranking nondeterministic.

Reduction order:

```text
candidate_id sort
oracle priority order
source_family grouping
stable tie-breakers
```

This makes repeated runs comparable in archolith-bench.

### Cache boundaries

Caches are allowed but must be scoped:

```text
per-query cache:
  safe for computed dependency cones, parsed query facets, resolved repo state

per-process read-only cache:
  safe for static project structure snapshots or loaded config

shared mutable cache:
  avoid unless protected by locks or replaced with immutable snapshots
```

Oracle examples:

```text
SemanticOracle:
  may reuse embedding vectors but must not mutate candidate metadata.

StructureOracle:
  may use a read-only dependency-cone snapshot for the query.

GitOracle:
  may use a commit-range snapshot computed before oracle fan-out.

BeliefOracle:
  may read a belief packet but should not update belief state during evaluation.
```

### Performance guardrails

```text
max_candidates_per_round
max_oracles_per_round
per_oracle_timeout_ms
per_oracle_concurrency_limit
fallback_on_timeout
oracle_cost_budget
```

The first implementation should support cheap oracles first:

```text
SemanticOracle
ScopeOracle
TemporalOracle
StructureOracle over already-fetched metadata
```

Then add expensive oracles behind budgets:

```text
GitOracle over commit ranges
BeliefOracle over scorer outputs
ContradictionOracle over conflict groups
```

## Initial oracle set

### SemanticOracle

Question:

```text
Does the candidate mean something similar to the query?
```

Evidence:

```text
embedding similarity
BM25 / lexical overlap
entity overlap
facet overlap
```

### TemporalOracle

Questions:

```text
Was this fact valid at query time?
Was this belief learned before the query's as-of point?
Was it expired or superseded later?
Is the query asking for current truth or historical belief?
```

Evidence:

```text
valid_at
invalid_at
created_at
expired_at
target_date
query_as_of_time
query_intent_current
query_intent_historical
```

### GitOracle

Questions:

```text
Was the referenced file/symbol/dependency changed in the relevant commit range?
Is this memory attached to the current repo state?
Is it from the right branch or commit window?
```

Evidence:

```text
commit range
changed files
changed symbols
dependency version changes
branch
working tree snapshot
```

### StructureOracle

Questions:

```text
Is this memory inside the dependency cone?
Does it touch the file, symbol, endpoint, or test involved in the query?
Does it explain an affected test or caller/callee path?
```

Evidence:

```text
file_context
IMPORTS edges
TESTS edges
CALLS edges
DEFINES edges
blast radius
function callers
linked memories
```

### BeliefOracle

Questions:

```text
Is this safe to assert?
Is it historical only?
Is it anergic for current truth?
Is it contradicted or superseded?
```

Evidence:

```text
BeliefHead.RELEVANT
BeliefHead.CURRENT
BeliefHead.SUPPORTED
BeliefHead.SUPERSEDED
RecallBucket
conflict status
later contradiction
later confirmation
```

### ScopeOracle

Questions:

```text
Is this memory from the same user, project, repo, branch, namespace, and task scope?
Could this be wrong-scope contamination?
```

Evidence:

```text
namespace
user_id
session_id
project
repo
branch
file/symbol identity
source confidence
```

### EvidenceOracle

Questions:

```text
What supports this memory?
Is the support direct or inferred?
Is evidence missing?
```

Evidence:

```text
source_is_user
source_is_log
source_is_git
source_is_test
source_is_agent_inference
evidence_count
missing_evidence_count
support-chain length
```

### ContradictionOracle

Questions:

```text
Does this candidate contradict current memory?
Is it in a conflict group?
Was the conflict resolved or false-positive?
```

Evidence:

```text
conflict_group_id
conflict_status
contradiction notes
resolved-pair telemetry
```

## Fine-grained query examples

Oracle separation allows very fine-grained retrieval without turning every query into a bespoke Cypher query.

Examples:

```text
Find memories about AuthService that were valid before commit abc123 but superseded after the MFA refactor.

Find historical beliefs about the CE willow patch that should not be asserted as current truth.

Find memories inside the failing test's dependency cone that changed between last-known-good and current broken state.

Find user-confirmed facts about this repo, excluding agent inferences and stale branch memories.

Find unresolved contradictions touching TreeWillowLeaflessImmature and Combat Extended plant bounds.

Find memories that mention the right file but are outside the current namespace or branch.
```

These map naturally to oracle combinations:

```text
SemanticOracle + TemporalOracle + GitOracle + BeliefOracle
StructureOracle + GitOracle + EvidenceOracle
ScopeOracle + ContradictionOracle
```

## Object 2: OracleAmplifiedRetrieval

Definition:

```text
An iterative retrieval algorithm that maintains a probability distribution over candidate memories and updates it using independent RetrievalOracle outputs.
```

Research name:

```text
Probability Amplification Retrieval
```

Code-facing name:

```text
OracleAmplifiedRetrieval
```

Algorithm sketch:

```text
1. Generate a broad candidate pool.
2. Initialize P(candidate) from semantic/facet/graph priors.
3. Evaluate independent retrieval oracles in parallel.
4. Convert oracle outputs into likelihood updates.
5. Update candidate probabilities.
6. Normalize.
7. Repeat until entropy stabilizes or budget is exhausted.
8. Select top candidates only at the end.
9. Send selected candidates into BeliefLayer / AnergicBeliefGate.
```

Pseudocode:

```python
def amplify_candidates(query, candidates, oracles, iterations=3, rho=0.5):
    logits = initialize_logits(query, candidates)

    for _ in range(iterations):
        packets = evaluate_oracles_parallel(query, candidates, oracles)
        next_logits = {}

        for candidate in candidates:
            local_logits = combine_oracle_packet(packets[candidate.id])
            pairwise = contradiction_messages(candidate, candidates, logits)
            next_logits[candidate.id] = (1 - rho) * logits[candidate.id] + rho * (local_logits + pairwise)

        logits = next_logits

        if entropy(softmax(logits)) < STOP_ENTROPY:
            break

    return softmax(logits)
```

## Why iterate instead of one-pass score?

The core research question is whether iteration adds value over a one-pass weighted score.

A one-pass score is:

```text
score = semantic + temporal + git + structure + belief
```

Iterative amplification may help when:

```text
weak candidates become stronger only after multiple evidence channels agree
initial embedding rank is poor but structural/temporal support is strong
candidate neighborhoods reinforce each other through shared evidence
contradictory or stale candidates need repeated suppression
```

But if iteration does not beat the one-pass weighted oracle score, this direction should not become a separate retrieval engine.

## Mathematical core: contradiction as destructive evidence

Model destructive interference as contradiction evidence in log-probability space.

Do not subtract from a final score. Instead, add negative log-likelihood evidence to the candidate's role-specific logit.

```text
p_t(i) = probability candidate i deserves retrieval at iteration t
z_t(i) = log unnormalized weight for candidate i
p_t(i) = softmax(z_t(i) / temperature)
```

For oracle `o`, contradiction strength is:

```text
q_o(i) =
    P_o(contradiction | query, memory_i)
  * confidence_o
  * directness_o
  * scope_match_o
  * independence_o
```

Contradiction penalty:

```text
D_o(i) = λ_o * q_o(i)^γ
```

Update:

```text
z_current(i) -= D_o(i)
```

Recommended defaults:

```text
γ = 1.0:
  linear penalty

γ = 1.5-2.0:
  harsher treatment for high-confidence contradictions

λ_o:
  oracle-specific penalty strength
```

Example:

```text
q = 0.9
λ = 3.0
γ = 1.5

D = 3.0 * 0.9^1.5 ≈ 2.56
multiplier = exp(-2.56) ≈ 0.077
```

That candidate loses roughly 92% of its current-truth probability mass before normalization.

## Role-specific logits

Contradiction should suppress a candidate's eligibility for a specific retrieval role, not erase the memory.

Maintain separate logits:

```text
z_relevant(i)
z_current(i)
z_historical(i)
z_conflict(i)
z_blocked(i)
```

For a current-truth query:

```text
TemporalOracle says memory is expired/superseded:
  z_current(i) -= strong_penalty
  z_historical(i) += small_boost
```

For a historical query:

```text
TemporalOracle says memory is superseded:
  z_current(i) -= strong_penalty
  z_historical(i) += strong_boost
```

This preserves the BeliefLayer rule:

```text
Superseded should become historical, not dead.
```

## Pairwise contradiction messages

Some contradiction is candidate-vs-candidate, not candidate-vs-query.

Example:

```text
m_a: "Patch fully fixed the issue."
m_b: "Load-order fix resolved the remaining issue."
```

If `m_b` has high probability and directly contradicts `m_a` as current truth, it should send a suppressive message to `m_a`.

Pairwise factor:

```text
K(i, j) = P(memory_i contradicts memory_j) * confidence * same_scope
```

Message:

```text
message_{j→i,t} = -λ_pair * K(i, j) * p_t(j)
```

Update:

```text
z_current_{t+1}(i) += Σ_j message_{j→i,t}
```

Interpretation:

```text
A contradictory memory suppresses another memory strongly only when the contradictory memory itself has high probability.
```

## Independence and source-family caps

Do not let duplicate evidence create fake certainty.

If several oracles are derived from the same source chain, downweight them:

```text
independence_o = 1 / sqrt(number_of_oracles_from_same_source_family)
```

Recommended source families:

```text
semantic
facet
structure
git
temporal
belief
scope
evidence
contradiction
```

Cap total contribution per family:

```text
family_contribution = min(max_family_contribution, Σ family_updates)
```

## Missing evidence is not contradiction

Missing evidence should increase uncertainty or trigger another oracle/budget decision. It should not automatically suppress a candidate as false.

```text
missing evidence:
  lower confidence
  increase uncertainty
  maybe trigger MeasurementBudgetGate
```

not:

```text
missing evidence:
  candidate is false
```

## Suggested initial weights

Starting support weights:

```text
semantic support α = 0.8
structure support α = 1.0
Git support α = 1.2
user/log/test evidence support α = 1.5
```

Starting contradiction penalties:

```text
temporal contradiction λ = 2.5
scope contradiction λ = 2.0
test contradiction λ = 3.0
direct user correction λ = 3.5
belief supersession λ = 3.0
```

These are placeholders for calibration, not constants.

## Object 3: MeasurementBudgetGate

Research name:

```text
Collapse Delay Gate
```

Code-facing name:

```text
MeasurementBudgetGate
```

Definition:

```text
A gate that delays expensive LLM inspection or final context assembly until the candidate distribution is concentrated enough or the retrieval budget is exhausted.
```

Why it matters:

```text
Once memories enter the prompt, the agent tends to reason around them.
Bad early context selection can steer the rest of the session.
```

Inputs:

```text
candidate probability entropy
top-k probability mass
oracle disagreement
missing evidence count
LLM inspection budget
latency budget
```

Decisions:

```text
continue_oracle_updates
inspect_top_candidates
assemble_context
ask_for_more_evidence
fallback_to_baseline
```

Failure modes:

```text
extra latency
never converges
blocks useful early context
optimizes probability concentration instead of answer quality
```

## Parked object: RetrievalInterferenceGraph

Status:

```text
parked
```

Definition:

```text
A graph of positive and negative retrieval influence between memories, evidence, and query constraints.
```

Park because:

```text
It is easy to implement as ordinary additive scoring with a cooler name.
```

Promote only if an eval requires explicit positive/negative propagation across candidate relationships.

## Parked object: HamiltonianRanker

Status:

```text
parked
```

Definition:

```text
A ranker that minimizes an energy/cost function over semantic distance, contradiction, temporal inconsistency, missing citations, and wrong-scope evidence.
```

Park because it overlaps heavily with:

```text
energy-based models
weighted cost minimization
graph optimization
factor graphs
belief propagation
```

Promote only if simpler oracle scoring and iterative amplification fail but the fixture clearly needs global optimization.

## Relationship to facet retrieval

Facet retrieval and oracle-amplified retrieval are complementary.

```text
Facet retrieval:
  creates deterministic candidate pools using query/memory slots.

Retrieval oracles:
  evaluate whether candidates satisfy semantic, temporal, Git, structure, belief, scope, and evidence properties.

Oracle amplification:
  iteratively updates probability over candidates using oracle outputs.
```

Possible pipeline:

```text
Query
  -> MemoryFacetIndex / vector / BM25 candidate generation
  -> RetrievalOracle evaluation
  -> one-pass oracle score or iterative amplification
  -> MeasurementBudgetGate
  -> BeliefLayer / AnergicBeliefGate
  -> recall packet
```

## Relationship to BeliefLayer

The BeliefLayer decides what selected memories mean for answer behavior.

Oracle retrieval decides which memories are worth considering.

```text
Oracle layer:
  retrieve and rank candidates.

BeliefLayer:
  classify candidates as current truth, historical-only, anergic-current, conflict, uncertain, or blocked.
```

Some oracles may call BeliefCircuit heads as evidence, but the final assertion policy still belongs to BeliefLayer.

## First menhir spike

Candidate files:

```text
src/menhir/domain/oracles.py
src/menhir/services/retrieval_oracles.py
src/menhir/services/oracle_combiner.py
src/menhir/services/oracle_executor.py
src/menhir/services/oracle_amplified_retrieval.py
```

Minimum domain model:

```python
@dataclass(frozen=True)
class QueryContext:
    text: str
    intent: str | None = None
    as_of_time: str | None = None
    repo: str | None = None
    branch: str | None = None
    project: str | None = None
    file: str | None = None
    symbol: str | None = None
    test: str | None = None
    namespace: str | None = None


@dataclass(frozen=True)
class OracleResult:
    oracle: str
    probability: float
    confidence: float
    polarity: OraclePolarity
    target: OracleTarget
    directness: float = 1.0
    scope_match: float = 1.0
    source_family: str | None = None
    supports: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class OraclePacket:
    candidate_id: str
    results: tuple[OracleResult, ...]
    combined_probability: float
    role_logits: dict[str, float]
    uncertainty: float
    rationale: tuple[str, ...]
```

Minimum service behavior:

```text
wrap existing candidate metadata in CandidateMemory
run SemanticOracle and ScopeOracle first
add TemporalOracle / StructureOracle next
execute oracles through a bounded parallel executor
combine probabilities once without iteration
compare one-pass score against current scoring
only then add iterative amplification simulator
```

## First archolith-bench fixture

Fixture shape:

```text
candidate memories with known relevance labels
initial embedding ranks
semantic score
temporal score
git score
structure score
belief score
scope score
expected support memory IDs
```

Include cases where:

```text
ground truth starts below rank 100 by embedding similarity
ground truth is recovered by temporal + structure evidence
stale semantic neighbor starts high but should be suppressed
wrong-repo/wrong-branch memory starts high but should be suppressed
historical-only memory should be retrieved for historical query but not current query
direct contradiction should suppress current-truth eligibility but boost historical/conflict usefulness
```

Baseline ladder:

```text
A: embedding top-k
B: BM25 + embedding
C: reciprocal rank fusion
D: graph expansion + reranking
E: one-pass weighted oracle score
F: one-pass log-space oracle combiner with role-specific logits
G: iterative oracle amplification
H: iterative oracle amplification + MeasurementBudgetGate
```

Metrics:

```text
recall_at_k
MRR
NDCG
temporal_accuracy
stale_hit_rate
wrong_scope_injection_rate
historical_context_preservation
current_truth_suppression_accuracy
oracle_ablation_delta
entropy_reduction
convergence_iterations
token_cost
latency_ms
oracle_wall_time_ms
oracle_parallel_speedup
oracle_timeout_rate
ranking_determinism
```

## Research question

```text
Can iterative probability updates over semantic, temporal, Git, structure, belief,
scope, and evidence oracles recover relevant memories that one-shot similarity
or one-pass reranking misses?
```

## Hypothesis

```text
Oracle-amplified retrieval will improve recall and temporal correctness on tasks
where relevant memories are semantically buried but supported by independent
Git, structure, temporal, or belief evidence.
```

## Killer baseline

The most important baseline is:

```text
one-pass weighted oracle score
```

If iterative amplification does not beat this baseline, keep the oracle interface but reject amplification as a separate algorithm.

Also compare against:

```text
one-pass log-space oracle combiner with role-specific logits
```

If the log-space combiner handles contradiction/currentness well enough, do not promote iterative amplification.

## Related-work search terms

Use these before making novelty claims:

```text
probabilistic information retrieval
relevance feedback retrieval
pseudo relevance feedback
iterative retrieval reranking
belief propagation information retrieval
probabilistic graphical models for information retrieval
energy-based retrieval models
retrieval calibration
query expansion
reciprocal rank fusion
adaptive retrieval systems
quantum-inspired optimization
quantum-inspired recommendation algorithms
complex-valued embeddings
parallel retrieval reranking
concurrent information retrieval systems
thread-safe ranking pipeline
```

## Success criterion

This direction is useful if it improves at least one of:

```text
recall_at_k for buried relevant memories
temporal_accuracy
stale_hit_rate
wrong_scope_injection_rate
historical_context_preservation
current_truth_suppression_accuracy
answer-grounding accuracy
```

without unacceptable loss in:

```text
latency_ms
token_cost
calibration
simple-query performance
ranking determinism
```

## Non-goals

Do not:

```text
claim quantum speedup
claim quantum behavior
implement quantum simulation
build a global probabilistic model over all memory
replace current scoring before a simulator wins
add heavy probabilistic dependencies before transparent baselines fail
hide oracle miscalibration behind a final score
let oracle evaluation mutate memory state
let parallel oracle execution make ranking nondeterministic
```

## Recommendation

Build first:

```text
RetrievalOracle
OracleResult
OracleExecutor
one-pass OracleCombiner
role-specific log-space contradiction handling
```

Bench next:

```text
OracleAmplifiedRetrieval simulator
MeasurementBudgetGate
```

Park:

```text
RetrievalInterferenceGraph
HamiltonianRanker
phase-history encoding
entangled-memory terminology
```

The main value is the oracle abstraction: it makes retrieval fine-grained, inspectable, composable, and parallelizable. Probability amplification is only worth promoting if it beats a one-pass oracle score under archolith-bench.
