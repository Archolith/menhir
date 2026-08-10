# Retrieval tuning stack

## Status

speculative

> **2026-07-11 status update.** Promotion condition #1 is **met** — the configurable hybrid
> candidate-generation stack (vector + BM25 + source-aware priors) is built
> (`domain/retrieval_tuning.py`, `services/hybrid_retrieval.py`), default-off. The bench sweep (#2,
> `hybrid_alpha`) was **run and did NOT graduate** — R1 landed neutral-to-negative on the live corpus,
> so `hybrid_alpha` stays unset. Effective status: supported-by-spike, benched-negative. See the
> execution ladder's "Bench verdicts — reconciliation".

## Promotion condition

This note becomes active when Menhir has either:

```text
1. a configurable retrieval candidate-generation stack with at least vector + lexical/BM25 + existing graph/file-context paths, or
2. an archolith-bench sweep that compares embedding dimensions, hybrid alpha, reranker use, and oracle-combiner settings.
```

It becomes supported-by-eval only if bench artifacts show a tuning setting improves retrieval quality, cost, latency, or stale/wrong-scope suppression against current recall baselines.

## Purpose

This note saves the practical conclusion from the retrieval/embedding discussion:

```text
Do not train or replace the embedding model first.
Tune the retrieval stack around fixed embeddings.
Treat semantic ranking as one signal, not the retrieval authority.
```

This is a lower-stack implementation note. It should not become a new architecture branch competing with facet retrieval, oracle retrieval, or BeliefLayer. It defines knobs that feed those systems.

## Core claim

There is no clean replacement for semantic ranking. Menhir should instead demote semantic ranking from monarch to evidence channel.

```text
Semantic ranking finds what sounds related.
Lexical/facet/graph retrieval finds what is explicitly connected.
Oracles decide what is temporally, structurally, evidentially, and belief-wise usable.
BeliefLayer decides assertion behavior.
```

## Where this sits

```text
Candidate generation:
  vector search
  lexical / BM25 search
  facet search
  graph / file-context expansion

Candidate semantic tuning:
  embedding dimensions
  hybrid alpha
  optional local reranker
  optional learned projection, later

Oracle layer:
  semantic
  temporal
  Git
  structure
  belief
  scope
  contradiction
  evidence

Combiner:
  role-specific log-space scoring

BeliefLayer:
  current vs historical vs anergic vs conflict assertion policy
```

## Immediate tuning knobs

### EmbeddingDimensionSweep

Definition:

```text
Evaluate whether shorter embedding vectors improve cost, latency, and storage without unacceptable retrieval loss.
```

Use cases:

```text
reduce vector storage
reduce ANN latency
compare fidelity at different embedding dimensions
measure whether shorter vectors blur or broaden recall
```

Important caution:

```text
Do not assume lower dimensions are always broader or stricter.
Measure behavior in archolith-bench.
```

Bench variables:

```text
1536
1024
512
256
```

Metrics:

```text
recall_at_k
precision_at_k
paraphrase_stability
stale_hit_rate
wrong_scope_hit_rate
latency_ms
storage_bytes_per_memory
```

### HybridAlphaSearch

Definition:

```text
Tune the blend between semantic vector retrieval and lexical/BM25 retrieval.
```

Why it matters for code memory:

```text
Semantic retrieval is weak for exact strings.
Code debugging often depends on exact strings.
```

Exact-match-heavy signals:

```text
error strings
file paths
symbol names
test names
commit hashes
mod IDs
stack traces
config keys
class names
method names
```

Candidate formulation:

```text
alpha = 1.0 -> pure vector
alpha = 0.0 -> pure lexical/BM25
alpha = 0.5 -> blended
```

Bench variables:

```text
0.0
0.25
0.5
0.75
1.0
query-adaptive alpha
```

Query-adaptive alpha idea:

```text
If query contains file path, symbol, stack trace, commit hash, or quoted error string,
shift alpha toward lexical/BM25.

If query is vague, conceptual, or paraphrased,
shift alpha toward vector retrieval.
```

### CrossEncoderRerankOracle

Definition:

```text
An optional local reranker oracle that jointly reads the query and candidate text and emits a relevance score.
```

Why it belongs as an oracle:

```text
A reranker improves semantic relevance but cannot decide currentness, Git scope, temporal validity, or belief safety by itself.
```

Oracle question:

```text
Given the query and candidate text together, how relevant is this candidate?
```

Recommended placement:

```text
candidate generation produces top 50-200
CrossEncoderRerankOracle scores a budgeted subset
OracleCombiner combines reranker relevance with temporal/Git/structure/belief/scope signals
```

Performance caution:

```text
A reranker is more expensive than vector/BM25/facet checks.
Run it behind MeasurementBudgetGate or an explicit rerank budget.
```

### ProjectionCalibrationLayer

Status:

```text
parked
```

Definition:

```text
A small learned projection or calibration layer over fixed embeddings and/or oracle features.
```

Examples:

```text
PCA over embeddings
linear projection
small MLP over embedding + oracle features
logistic calibration over oracle outputs
```

Promotion condition:

```text
Promote only after Menhir has labeled retrieval traces or archolith-bench fixtures showing that fixed embedding + hybrid + reranker + oracle combiner still fails on domain-specific ranking.
```

Do not start here.

## Relationship to oracle-amplified retrieval

This note owns lower-stack retrieval knobs.

`oracle-amplified-retrieval.md` owns the oracle interface, oracle combiner, role-specific logits, thread-safe oracle execution, and optional iterative amplification.

The clean split:

```text
Retrieval tuning stack:
  How do we generate and semantically tune candidate pools?

Oracle layer:
  How do we evaluate fine-grained evidence properties on candidates?

BeliefLayer:
  What can the answer safely assert?
```

## Relationship to facet retrieval

Facet retrieval is not a competitor to hybrid/vector retrieval.

It is an additional deterministic candidate generator:

```text
vector:
  meaning similarity

BM25 / lexical:
  exact text match

facet:
  structured slot match

structure graph:
  code relationship match
```

The candidate pool should be assembled from multiple sources before oracles run.

## Relationship to current scoring code

Current recall already has:

```text
vector candidate generation
candidate metadata filtering
adjacency scoring
recency/prominence/conflict scoring
file-context structural injection
namespace/scope filtering
```

This note does not replace those systems. It proposes a clearer split:

```text
candidate generation:
  vector/BM25/facet/graph paths

semantic ranking:
  vector similarity and optional reranker

oracle evaluation:
  temporal/Git/structure/belief/scope/evidence properties

combiner:
  deterministic policy over oracle outputs
```

## First menhir spike

Candidate files:

```text
src/menhir/domain/retrieval_tuning.py
src/menhir/services/hybrid_retrieval.py
src/menhir/services/rerank_oracle.py
```

But do not create these before a small configuration/data-model pass.

Minimum config shape:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalTuningConfig:
    embedding_dimensions: int | None = None
    hybrid_alpha: float = 1.0
    enable_bm25: bool = False
    enable_cross_encoder_rerank: bool = False
    rerank_top_n: int = 50
    enable_projection_calibration: bool = False
```

Minimum service behavior:

```text
allow vector-only baseline
allow lexical-only baseline
allow hybrid vector + lexical candidate generation
allow optional reranker oracle over top-N
emit retrieval trace with config values for bench comparison
```

## First archolith-bench sweep

Fixture families:

```text
exact_error_string
symbol_name_query
paraphrased_debug_question
stale_semantic_neighbor
wrong_repo_same_topic
buried_relevant_memory
historical_only_vs_current_truth
```

Baseline ladder:

```text
A: current recall baseline
B: vector-only at default dimensions
C: vector-only with dimension sweep
D: BM25 / lexical only
E: hybrid vector + lexical with alpha sweep
F: hybrid + facet candidates
G: hybrid + facet + CrossEncoderRerankOracle
H: hybrid + facet + oracles + BeliefLayer gates
```

Metrics:

```text
recall_at_k
precision_at_k
MRR
NDCG
paraphrase_stability
exact_string_recall
symbol_recall
stale_hit_rate
wrong_scope_injection_rate
historical_context_preservation
answer_grounding_accuracy
latency_ms
storage_bytes_per_memory
rerank_wall_time_ms
```

## Non-goals

Do not:

```text
train a new embedding model before exhausting retrieval-stack tuning
turn every tuning knob into a subsystem
let reranker scores override temporal/Git/belief safety
let embedding dimension sweeps distract from currentness/supersession fixes
promote ProjectionCalibrationLayer without labeled traces
replace OracleCombiner with a monolithic hidden score
```

## Recommendation

Build or bench in this order:

```text
1. Hybrid BM25/vector candidate generation.
2. Facet candidate generation.
3. Thread-safe RetrievalOracle API.
4. One-pass OracleCombiner.
5. Optional local CrossEncoderRerankOracle.
6. Dimension and alpha sweeps in archolith-bench.
7. ProjectionCalibrationLayer only if simpler knobs fail.
```

The important product thesis:

```text
You do not tune the embedding model.
You tune the retrieval stack and treat embedding similarity as one signal among many.
```
