# Facet retrieval for temporal code memory

## Status

supported-by-spike

Condition #2 below is met: the benchmark-local `MemoryFacetIndex` + `MeetPointReranker`
+ A–F ladder live in `archolith-bench` (`archolith_bench/facet/`, 46 unit tests), with a
DEMO-fixture comparison against BM25/embedding/hybrid/file-context. Not yet `supported-by-eval`:
that needs the real 50/20 gold fixture run to clear the promotion gate (see the bench's
`.agent/benchmark-notes/facet-r2-demo-run.md` and menhir `.agent/plans/deferred-verification.md`).

> **2026-07-11 update:** the real-embedder run has since happened (text-embedding-3-small) and **F still
> graduates gold+hybrid** on the DRAFT fixture (`facet-r2-real-embedder-run.md`); `CandidateSource.FACET`
> is now wired **observe-only shadow** in recall. But structural decomposition + a live prod-clone
> measurement show **file facets require the code graph's ANCHORED_TO edges, not extraction**, and only
> **24.5% of memories are anchored** — so FACET is a bounded win on the code-anchored slice. Still
> `supported-by-spike` (ctharvey's hardened real fixture is the remaining owed item); the production
> lever is now ingest-time ANCHORED_TO coverage, not the engine.

## Promotion condition

This note became `supported-by-spike` via:

```text
1. a menhir code spike for MemoryFacetIndex or MeetPointReranker, or
2. an archolith-bench fixture comparing facet-first retrieval against embedding/BM25/graph baselines.  [MET]
```

It becomes supported-by-eval only if bench artifacts show improvement on retrieval stability, stale-hit reduction, or support sufficiency against honest baselines **on the real fixture**.

## Purpose

This note captures a mechanism-transfer pass from gemcutting/faceting into alternatives to Menhir-style semantic memory retrieval from keywords or questions.

The goal is not to turn gemcutting terminology into architecture. The goal is to preserve the concrete mechanisms that could improve retrieval:

```text
discrete repeatable indexing
orientation-preserving identity
meet-point convergence
bounded drift correction
safe transfer/migration checks
compression/yield evaluation
```

## Non-novelty warning

Do not claim broad novelty.

Most of this overlaps established ideas:

```text
Memory Index Wheel      -> faceted / structured retrieval
Meet-Point Reranker     -> graph/provenance/evidence-convergence reranking
Retrieval Angle Stop    -> query-drift guardrail / expansion breaker
Memory Transfer Fixture -> retrieval regression testing / migration evaluation
Dop Anchor              -> canonical identity / entity resolution
Memory Yield Metric     -> summarization faithfulness / information-retention eval
```

The defensible claim is narrower:

```text
Menhir may benefit from combining deterministic facet retrieval, structure-aware reranking,
temporal validity, belief-state gating, and Git/Structure Time Join for coding-agent memory.
```

This is a differentiated composition, not new theory.

## Source mechanism: gemcutting / faceting

Gemcutting/faceting uses repeatable mechanical constraints:

```text
index wheel positions
angle and depth settings
dop/quill alignment
transfer jigs
staged pavilion/crown cutting
meet-point constraints
polishing passes
inspection under light
```

Named stone regions and facets include:

```text
crown
pavilion
girdle
culet
table
mains
star facets
girdle facets
```

Useful transfer mechanisms:

```text
Index wheel:
  discrete, repeatable positions.

Dop orientation:
  stable orientation preserved across operations.

Transfer jig:
  alignment-preserving migration from one stage to another.

Meet-point faceting:
  independent cuts must converge precisely.

Angle stop:
  hard limit preventing over-rotation or drift.

Grit ladder / polish passes:
  staged coarse-to-fine refinement.

Yield retention:
  every cut loses material; every compression loses detail.
```

## Target problem: semantic memory retrieval alternatives

Embedding retrieval is useful but weak on:

```text
paraphrase instability
near-synonym misses
false semantic neighbors
stale result ranked high
identity collision
missing provenance
wrong repo/branch/project context
temporal drift
embedding-only hallucination
```

Menhir already has more than embedding retrieval:

```text
vector retrieval
candidate metadata
adjacency scoring
file-context structural injection
scope/freshness/namespace filtering
blast-radius traversal
BeliefCircuit / breaker direction
```

This note proposes adding a deterministic facet path and convergence reranker alongside those existing systems.

## Proposed architecture

```text
Query
  -> FacetExtractor
  -> MemoryFacetIndex
  -> candidate pool
  -> existing vector similarity
  -> existing adjacency / structure context
  -> MeetPointReranker
  -> BeliefLayer / AnergicBeliefGate
  -> recall packet
```

This should extend the current recall pipeline, not replace it.

## Object 1: MemoryFacetIndex

Research name:

```text
Memory Index Wheel
```

Code-facing name:

```text
MemoryFacetIndex
```

Definition:

```text
A deterministic retrieval index that maps memories and queries into discrete semantic-structural slots before dense similarity is applied.
```

Initial slots:

```text
actor
object
operation
file
symbol
test
valid_time
learned_time
evidence_type
source_id
repo
project
namespace
belief_bucket
```

Retrieval flow:

```text
query
-> extract facets
-> lookup compatible slot candidates
-> optionally apply temporal/scope filters
-> rerank with vector, graph, and belief signals
```

Why it may help:

```text
Embedding similarity is soft and unstable across paraphrases.
Facet slots give repeatable retrieval axes for code/time/belief memory.
```

Failure modes:

```text
bad facet extraction
overly rigid slots
missing entities
alias explosion
low recall for vague/abstract questions
false confidence from deterministic-looking fields
```

Smallest spike:

```text
Create a JSON-only prototype over hand-authored memories.
Extract slots with simple rules or LLM-structured output.
Compare candidate recall against embedding/BM25 baselines.
```

## Object 2: MeetPointReranker

Research name:

```text
Meet-Point Reranker
```

Code-facing name:

```text
MeetPointReranker
```

Definition:

```text
A reranker that rewards candidates whose facets converge on the same claim, evidence, file, symbol, test, time window, or dependency cone.
```

Initial score components:

```text
shared_file
shared_symbol
shared_test
shared_time_window
shared_evidence_source
inside_dependency_cone
same_repo_or_project
source_authority
not_superseded
supports_current_query_intent
```

Simple first formula:

```text
meet_score(candidate, query, graph_context) =
  weighted overlap over required facets
  + dependency-cone support
  + evidence/source support
  - stale/superseded penalty
  - wrong-scope penalty
```

Why it may help:

```text
Semantic neighbors can share a topic without supporting the same claim.
Meet-point scoring asks whether candidates converge on the same support structure.
```

Failure modes:

```text
overfilters sparse memories
rewards superficial shared fields
misses memories with weak explicit facets but strong semantic relevance
requires good extraction of files/symbols/tests/times
```

Smallest spike:

```text
Implement meet_score(candidate, query_facets, graph_context).
Run it after existing candidate generation.
Measure whether false semantic neighbors move down.
```

## Object 3: ExpansionDriftBreaker

Research name:

```text
Retrieval Angle Stop
```

Code-facing name:

```text
ExpansionDriftBreaker
```

Definition:

```text
A breaker that stops query expansion when candidates lose required anchor overlap.
```

Inputs:

```text
query facets
expansion path
candidate facets
anchor overlap
semantic similarity
scope/temporal validity
```

Decisions:

```text
allow_expansion
allow_with_label
stop_expansion
require_anchor
```

Use cases:

```text
vague query starts pulling memories from wrong repo
symbol alias expansion drifts beyond Git rename evidence
semantic expansion finds same topic but wrong time window
```

Failure mode:

```text
stops useful analogical retrieval or cross-repo pattern reuse.
```

Mitigation:

```text
Use different policies for current debugging retrieval vs exploratory research retrieval.
```

## Object 4: RetrievalTransferFixture

Research name:

```text
Memory Transfer Fixture
```

Home:

```text
archolith-bench
```

Definition:

```text
A migration validator that compares retrieval behavior before and after memory/index/model transformation.
```

Useful for:

```text
embedding model changes
index migration
memory summarization/compression
schema rewrite
facet extractor change
```

Outputs:

```text
support-chain preservation
answer-quality delta
stale-hit delta
retrieval stability
known-support recall
```

Caution:

```text
Top-k stability is not correctness.
A new index can be better while returning different candidates.
Judge support sufficiency and answer quality, not overlap alone.
```

## Parked object: OrientationAnchor

Research name:

```text
Dop Anchor
```

Status:

```text
parked
```

Definition:

```text
A stable identity tuple preserving memory orientation across summarization, rewording, re-indexing, and migration.
```

Possible tuple:

```text
source_id
episode_id
repo/project
file/symbol/test
valid_time
learned_time
core entities
claim type
evidence type
```

Do not implement as only a hard hash. Legitimate memory evolution would break the hash or create false non-matches.

Promotion condition:

```text
Promote only if a fixture shows identity drift across summaries, model migration, or symbol rename is causing retrieval loss.
```

## Parked metric: MemoryYieldMetric

Status:

```text
parked
```

Definition:

```text
A metric for how much answer-critical detail survives memory compression or summarization.
```

This belongs with lifecycle/compression/rehydration evaluation, not the first facet retrieval spike.

Potential measurement:

```text
raw source answerability
compressed memory answerability
critical detail recall
qualifier preservation
support-chain preservation
```

Promotion condition:

```text
Promote when compression or rehydration becomes a measured source of retrieval/answer failure.
```

## What not to promote

Do not turn each gemcutting term into a subsystem.

Reject as standalone architecture for now:

```text
full gemcutting architecture
somatic-style query mutation
SyntheticRecombinationGenerator
Culet Evidence Check as its own object
Facet Diagram as product object
Pavilion/crown staged-cut terminology
Girdle Boundary Relation terminology
Overcut Detector as separate system
```

Absorb useful pieces into existing concepts:

```text
Culet Evidence Check -> evidence fragility / minimum support
Girdle Boundary -> current vs historical boundary
Overcut Detector -> compression faithfulness check
Facet Diagram -> trace/debug artifact
```

## First menhir spike

Candidate files:

```text
src/menhir/domain/facets.py
src/menhir/services/facet_extractor.py
src/menhir/services/facet_index.py
src/menhir/services/meet_point_reranker.py
```

Minimum domain shape:

```python
@dataclass(frozen=True)
class MemoryFacetSet:
    actor: tuple[str, ...] = ()
    object: tuple[str, ...] = ()
    operation: tuple[str, ...] = ()
    file: tuple[str, ...] = ()
    symbol: tuple[str, ...] = ()
    test: tuple[str, ...] = ()
    valid_time: tuple[str, ...] = ()
    learned_time: tuple[str, ...] = ()
    evidence_type: tuple[str, ...] = ()
    source_id: tuple[str, ...] = ()
    repo: tuple[str, ...] = ()
    project: tuple[str, ...] = ()
    namespace: tuple[str, ...] = ()
```

Minimum service behavior:

```text
extract query facets
fetch facet-compatible candidates
merge with existing vector candidates
apply meet-point score
return explanations for top candidates
```

## First archolith-bench fixture

Fixture shape:

```text
50 hand-authored memories
20 paraphrased queries
known support memory IDs
stale distractors
wrong-repo distractors
symbol rename case
one vague query case expected to favor embedding baseline
```

Baseline ladder:

```text
A: BM25
B: embedding top-k
C: BM25 + embedding
D: existing graph/file_context retrieval
E: facet index + embedding rerank
F: facet index + meet-point rerank
G: facet index + meet-point rerank + BeliefLayer gates
```

Metrics:

```text
recall_at_5
precision_at_5
paraphrase_stability
stale_hit_rate
wrong_scope_injection_rate
support_sufficiency
false_neighbor_rate
answer_grounding_accuracy
latency_ms
```

## Research question

```text
Do temporal/code/belief facets improve retrieval stability and reduce stale or wrong-scope memory injection compared with embedding-only, BM25, and graph-adjacency baselines?
```

## Hypothesis

```text
Facet-first candidate generation plus meet-point reranking will improve paraphrase stability and reduce stale-hit rate on temporal code-memory tasks, while preserving enough recall to remain useful for agent debugging.
```

## Related-work search terms

Use these before making novelty claims:

```text
faceted search
faceted classification
entity-centric retrieval
schema-guided RAG
hybrid sparse dense retrieval
structured retrieval augmented generation
temporal knowledge graph retrieval
provenance-aware RAG
graph RAG reranking
claim verification retrieval
code-aware retrieval
embedding migration evaluation
retrieval regression testing
summarization faithfulness information retention
```

## Success criterion

This direction is useful if it improves at least one of:

```text
paraphrase stability
stale-hit rate
wrong-scope injection rate
support sufficiency
answer-grounding accuracy
```

without unacceptable loss in:

```text
recall_at_5
latency_ms
ability to answer vague/abstract queries
```

## Recommendation

Build first:

```text
MemoryFacetIndex
MeetPointReranker
```

Bench alongside:

```text
ExpansionDriftBreaker
RetrievalTransferFixture
```

Park:

```text
OrientationAnchor
MemoryYieldMetric
```

Do not claim novelty yet. Treat this as a practical retrieval alternative for temporal code/belief memory, not a new retrieval theory.
