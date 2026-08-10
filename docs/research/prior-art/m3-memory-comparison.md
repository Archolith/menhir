# M3 Memory vs Menhir — Prior-Art / Benchmark-Relevance Comparison

**Date:** 2026-07-30  
**Status:** External comparison note; use for benchmark planning, architecture positioning, and roadmap triage.  
**Compared project:** [`skynetcmd/m3-memory`](https://github.com/skynetcmd/m3-memory)  
**Analyzed public revision:** `693a335e647e22fdb142179a5fdfcc1e52423956`  
**Primary question:** Is M3 Memory a direct architectural competitor to Menhir, and what must Menhir's benchmark prove beyond M3's published LongMemEval result?

---

## 1. Executive verdict

M3 Memory is the closest direct architectural comparison reviewed so far.

It is not merely a vector store, session-summary workflow, or thin MCP wrapper. It ships a typed memory schema, provenance fields, confidence, bitemporal columns, non-destructive supersession, an audit history, hybrid retrieval, entity extraction and resolution, graph relationships, lifecycle maintenance, local-first storage, and a substantial public benchmark suite.

A reasonable summary is:

```text
feature-level overlap:       high
retrieval/storage overlap:   high
knowledge-semantics overlap: medium
whole-system architecture:   approximately 55–65% similar
```

Both projects occupy the same broad category:

> durable, local-first, temporally aware memory infrastructure for heterogeneous agents.

The central architectural difference is the unit that carries truth.

M3's primary unit is a typed, mutable-lifecycle memory row:

```text
memory row
-> embedding
-> similarity-based contradiction check
-> optional supersession
-> retrieval and graph expansion
```

Menhir's intended primary units are evidence-grounded assertions and rebuildable projections:

```text
raw episode / source evidence
-> grounded typed assertions
-> canonical identity binding
-> semantic-slot grouping
-> deterministic fold/reconcile
-> current and historical Views
-> recall
```

Therefore:

> M3 maintains typed, supersedable memory records. Menhir constructs canonical state from typed, source-grounded observations.

M3 is a direct competitor and should be included in Menhir's prior-art and benchmark story. Menhir's additional machinery is only justified if it produces measurably better update correctness, identity continuity, provenance, historical reconstruction, contradiction handling, and abstention.

---

## 2. Scope and terminology

This note focuses on architecture and benchmark relevance. Product usability, installation quality, dashboard polish, agent integrations, and packaging are not evaluated here.

Two uses of the word `oracle` must remain separate.

### 2.1 LongMemEval dataset variants

LongMemEval publishes different dataset variants:

```text
LME Oracle
    evidence-only history supplied for each question

LME-S
    relevant evidence buried among a smaller distractor history

LME-M
    relevant evidence buried among a much larger history,
    commonly around hundreds of sessions
```

The Oracle dataset name does not mean a benchmark harness was manually assisted. It is an official evidence-only dataset variant.

### 2.2 Oracle routing metadata

A separate benchmark choice is whether a system uses privileged question metadata, such as LongMemEval's question-type label, to choose a retrieval or answering strategy.

M3's report distinguishes an older oracle-routed configuration from its current no-oracle routing configuration. That routing terminology is independent of the `longmemeval_oracle` dataset variant.

---

## 3. What M3 actually is

M3 is a local-first memory service exposed through MCP and CLI tools.

Its primary store is SQLite by default, with PostgreSQL available as a first-class primary backend and as an optional synchronization warehouse.

The core schema includes:

```text
memory_items
memory_embeddings
memory_relationships
memory_history
entities
entity embeddings and mention links
fact-enrichment queues and rows
sync, retention, trust, and lifecycle support tables
```

A `memory_items` row carries fields such as:

```text
id
memory type
title
content
metadata
agent/model/change-agent provenance
importance
source
origin device
user and scope
valid_from / valid_to
created_at / updated_at
expiry and refresh fields
confidence and belief counters
conversation id
content hash
lifecycle state
```

M3's architecture is broad enough that two different comparison surfaces must be distinguished:

1. the complete feature surface,
2. the exact production path used for its published LongMemEval result.

The complete feature surface includes contradiction checks, entity extraction, graph traversal, confidence, consolidation, and procedural memory.

The published LME-S result primarily demonstrates a raw-turn retrieval and answer pipeline using hybrid lexical/vector retrieval, MMR, routing, optional session expansion, and a frontier answer model. The report explicitly says the headline retrieval result used raw turns and no knowledge graph.

---

## 4. High-level architecture mapping

| Concern | M3 Memory | Menhir | Similarity |
|---|---|---|---|
| Durable input | Typed memory row, often caller supplied | Episode/evidence followed by perception | Medium |
| Raw source retention | Verbatim row content + hash/history | Episode/evidence records and source spans | High in intent, different granularity |
| Fact representation | Memory row; optional `fact_enriched` children | Typed assertions | Medium |
| Entity model | Optional post-write entity graph | Foundational canonical entities | Medium |
| Identity resolution | Exact → token Jaccard → embedding cosine | Aliases, binding, merge/unmerge, canonical identity | Medium |
| Contradiction handling | Same-type high-cosine differing text | Same semantic slot reconciled by fold/policy | Low–medium |
| Temporal model | `valid_from` / `valid_to` plus transaction timestamps | `valid_at` / `learned_at`, assertion lifecycle, Views | Medium |
| Current state | Live/non-deleted rows, newer superseding rows | Rebuildable current View | Low–medium |
| History | Closed rows + history events + as-of queries | Historical assertions and history Views | High in intent |
| Provenance | Row-level source/agent/device/hash/history | Source claim, episode, exact span, contributor edges | Medium |
| Retrieval | FTS/BM25 + vectors + MMR + routing/expansion | Multi-lane retrieval + weighted fusion + authority layer | High |
| Graph retrieval | Memory and entity relationship expansion | Canonical graph, assertion/View and structural lanes | Medium–high |
| Lifecycle | Decay, TTL, dedup, consolidation, archive, erasure | Scope/freshness lifecycle, compression, rehydration, deletion repair | Medium–high |
| Trust | Confidence, Bayesian counters, corroboration | Source/evidence tier, conflict status, promotion policy | Medium |
| Procedural memory | Procedure rows distilled from task runs | Evidence-backed procedural direction | Medium |
| Benchmark posture | Published LME-S full-stack result | Current LME Oracle development result | Both relevant, not directly comparable |

---

## 5. The principal difference: row-as-truth versus assertion-to-View

### 5.1 M3's unit of truth

M3 stores a memory item as the primary semantic object. The row has content, type, provenance, confidence, temporal fields, an embedding, and relationships.

The row's lifecycle may change:

```text
created
updated
corroborated
contradicted
superseded
expired
consolidated
archived
erased
```

The original text is preserved when superseded, and a history table records changes. This is materially stronger than an overwrite-only memory table.

However, the semantic claim and its storage row are still closely coupled. A row generally represents what the system believes or wants to retrieve.

### 5.2 Menhir's unit of truth

Menhir separates at least three layers:

```text
source evidence
    what was actually observed and where

typed assertion
    a grounded interpretation of that evidence

View
    a current or historical projection built from assertions
```

A current View is not the original evidence and is not assumed to be irreducible truth. It can be retired and rebuilt from assertions.

This separation matters when:

- multiple source statements support one fact,
- one statement contains several independent facts,
- a correction changes one semantic slot but not another,
- identity binding changes after ingestion,
- source-time and learned-time differ,
- two valid interpretations remain unresolved,
- a prior current value must remain historically queryable,
- projection logic is upgraded and current state must be replayed.

### 5.3 Consequence for benchmark design

Menhir should not expect its more complex architecture to win merely by retrieving the right session.

The architecture must prove value on cases where a memory-row model is structurally under-specified:

```text
one row contains several claims and only one changes
new wording is semantically corrective but not highly similar
same wording refers to a different entity
an alias changes after earlier evidence was stored
world-valid time differs from ingestion time
a current answer must be rebuilt after policy or identity changes
```

---

## 6. Write and ingestion pipelines

### 6.1 M3 direct write path

A direct `memory_write` call approximately performs:

```text
validate and safety-check content/title/metadata
-> optionally classify or queue classification
-> derive confidence from provenance/observer metadata
-> create row with temporal and scope fields
-> generate or defer embedding
-> run contradiction/corroboration checks
-> create relationships and history events
-> optionally trigger fact/entity enrichment
```

Important implementation properties:

- Verbatim content is stored before optional enrichment succeeds.
- Classification can be deferred to avoid blocking the write path.
- Entity and fact extraction are queueable and fail-open.
- Benchmark variants can be tagged so experimental rows do not leak into canonical memory.
- Session-scoped memories receive automatic TTL.

This is operationally mature and worth treating as real prior art.

### 6.2 M3 enrichment path

M3 can derive:

```text
atomic fact rows
entity rows
memory-to-entity mention links
entity-to-entity typed relationships
summaries
beliefs
procedures
```

These are post-write enrichments. The underlying memory store remains functional when enrichment is absent, delayed, or disabled.

### 6.3 Menhir ingestion path

Menhir's central path makes perception and assertion construction load-bearing:

```text
episode or turn evidence
-> extraction/perception
-> entity resolution and binding
-> typed assertion creation
-> fold/reconcile
-> View lifecycle
```

An extraction failure therefore has a different consequence. The source episode may survive, but semantic recall can lose the fact unless the system explicitly retains and searches raw evidence.

This creates a useful benchmark distinction:

```text
M3 risk:
    noisy or under-structured rows remain retrievable

Menhir risk:
    failed perception creates a semantic omission
```

Menhir's admission and projection audits are therefore strategically important, not merely internal correctness checks.

---

## 7. Contradiction and supersession

### 7.1 M3 automatic contradiction rule

M3's inline contradiction check compares the new row against existing rows that are generally:

```text
same memory type
same tenant/user scope
same agent scope unless corroboration broadens the scan
not deleted
not the new item itself
```

The default rule uses:

```text
cosine similarity > 0.92
content differs
same type
```

The default `loose` title gate does not require title overlap. `strict` adds title matching, while a research mode can bypass more checks.

When the rule fires, M3:

```text
closes the older row's validity interval
marks it deleted/inactive for current use
creates new -> old `supersedes` relationship
records a history event
keeps the original content queryable historically
```

This is a real, non-destructive supersession mechanism.

### 7.2 What the rule actually detects

The rule is conservative by design. It primarily detects near-restatements of one claim with changed content.

M3's own documentation gives a useful example:

```text
old: The auth service uses RS256 JWTs.
new: The auth service now uses EdDSA, replacing RS256.
```

The documentation says these score around `0.74`, below the automatic threshold, so both are retained unless the caller explicitly supersedes the old memory.

This exposes the architectural limit:

> Similarity is being used as a proxy for semantic-slot identity.

A correction can be semantically exact while lexically and embedding-wise distant. Conversely, two highly similar rows may describe different entities or contexts.

### 7.3 Menhir's target model

Menhir aims to supersede or reconcile at the level of:

```text
canonical subject
predicate / semantic slot
value or operation
source-valid time
assertion lifecycle
```

That allows:

```text
Rachel lives in Chicago
Rachel moved back to the suburbs
```

to compete for the same residence slot even if the sentences are not close enough for a conservative cosine overwrite rule.

### 7.4 Benchmark cases that separate the systems

The comparison should include:

1. **Low-similarity correction**  
   Different wording, same entity and semantic slot.

2. **High-similarity non-contradiction**  
   Similar text about two distinct entities.

3. **Partial-row correction**  
   A memory row contains three facts; only one changes.

4. **Same value, different validity period**  
   A repeated value is historically distinct rather than duplicate.

5. **Correction without explicit replacement language**  
   The newer statement implies replacement through chronology.

6. **Concurrent disagreement**  
   Two sources disagree and neither is automatically authoritative.

7. **Retraction versus update**  
   The fact ceases to be true without a replacement value.

---

## 8. Temporal architecture

### 8.1 M3

M3 stores:

```text
valid_from
valid_to
created_at
updated_at
```

It can query closed facts historically and reconstruct what rows were considered valid at a point in time.

This is a genuine bitemporal record model when callers provide correct valid-time metadata.

However:

- `valid_from` defaults to write time when omitted,
- automatic supersession closes `valid_to` at detection time,
- world-valid time is therefore only as accurate as the writer or extractor supplies,
- the current-state model remains row lifecycle rather than a separate deterministic projection.

### 8.2 Menhir

Menhir's assertion model explicitly separates:

```text
valid_at
    when the claim applies in the world

learned_at
    when the system acquired the assertion
```

Views are built from materializable assertions using deterministic ordering and policy.

Menhir's scalar history work adds a distinct advisory history projection rather than treating a closed current row as the only historical interface.

### 8.3 Important distinction

M3 has:

> bitemporal memory records.

Menhir is aiming for:

> bitemporal belief construction and rebuildable state.

The difference only matters if Menhir demonstrates correct behavior when source time, ingest time, correction time, and query time diverge.

---

## 9. Entity identity

### 9.1 M3 entity subsystem

M3 ships a real entity layer with:

```text
configurable entity types and predicates
SLM-based entity/relationship extraction
canonical entity rows
memory-item mention links
entity relationship rows
stored entity-name embeddings
three-tier name resolution
```

The resolution cascade is:

```text
exact normalized name
-> token-set Jaccard fuzzy match
-> embedding cosine
```

The implementation includes queueing, concurrency limits, retry controls, cached vectors, and deferred vector writes to reduce SQLite contention.

This is much stronger than storing entity names only in JSON metadata.

### 9.2 Architectural limit

The entity graph is an optional post-write layer. M3's primary memory row, contradiction check, and raw retrieval path can operate without canonical entity resolution.

That means identity does not always participate in deciding whether two memory rows represent the same claim.

### 9.3 Menhir identity model

Menhir treats canonical identity as foundational to assertion and View construction.

Relevant mechanisms include:

```text
binding-pending assertions
alias handling
merge and exact unmerge
contributor-derived provenance
canonical subject UUIDs
projection rebuild after identity changes
```

### 9.4 Separating benchmark cases

Useful cases include:

```text
nickname -> legal name
maiden name -> married name
company rename
same name, different person
merged identity later split
pronoun/shorthand resolved from adjacent turns
entity mentioned before canonical name is known
correction arrives under a new alias
```

---

## 10. Provenance and trust

### 10.1 M3 provenance

M3 records strong row-level operational provenance:

```text
source
agent_id
model_id
change_agent
origin_device
variant
user and scope
content hash
created/updated timestamps
history events
relationship lineage
```

It can derive a first-class confidence value from explicit input, provenance, and observer metadata.

It also carries:

```text
corroboration_count
contradiction_count
belief alpha/beta counters
agent trust controls
```

Several knowledge-maintenance features are intentionally gated or default-off, including confidence-based ranking, autonomous consolidation, corroboration updates, and trust autotuning. This is a strength: the repository distinguishes shipped substrate from riskier policy activation.

### 10.2 Menhir provenance

Menhir's assertion provenance is more granular:

```text
source identity
claim identity
assertion identity
semantic slot identity
episode UUID
exact stated span offsets
valid and learned times
evidence tier
perceiver version
contributor edges into Views
```

The intended advantage is not merely auditability of the row. It is the ability to answer:

```text
Which exact source words support this current value?
Which assertions contributed to this View?
Which source was excluded or abstained?
Can this View be rebuilt from the same evidence?
```

### 10.3 Trust distinction

M3 confidence often attaches to the memory row.

Menhir tries to keep separate:

```text
source authority
interpretation confidence
evidence support
currentness
conflict state
projection authority
```

Menhir should preserve this separation rather than compressing it into one retrieval score.

---

## 11. Retrieval architecture

### 11.1 M3 retrieval

The production search stack includes:

```text
FTS5 / BM25
BGE-M3 dense embeddings
hybrid score fusion
MMR diversity
recency and temporal boosts
query and intent routing
optional session expansion
optional memory-relationship graph expansion
optional entity-graph expansion
optional reranking
exact and semantic caches
explainable score components
```

M3's architecture documentation describes the primary hybrid formula as weighted vector and BM25 scoring followed by MMR.

Its routed benchmark surface can widen temporal retrieval and expand whole sessions around strong hits.

### 11.2 Menhir retrieval

Menhir's current retrieval pipeline combines multiple candidate families, including:

```text
BM25
name vectors
content vectors
FACET
entity and pending-entity candidates
file-linked context
structural code context
scalar state and history lanes
source-aware admission
authority-layer interpretation
retrieval traces
```

Menhir's weighted reciprocal-rank fusion is more general as a multi-lane composition mechanism.

### 11.3 Relative strengths

M3 is currently stronger in:

```text
published end-to-end retrieval evidence
simple local deployment
raw-turn/session retrieval
MMR diversity
retrieval-score explainability
benchmark reproducibility and reporting
```

Menhir is architecturally stronger in:

```text
current-state authority separation
assertion/View provenance
identity-aware retrieval lanes
history/current distinction
code and file structural context
abstention and advisory-vs-authoritative context
```

These are architectural claims until measured.

---

## 12. Lifecycle and consolidation

### 12.1 M3

M3 includes:

```text
session TTL
expiry
decay toward neutral
refresh scheduling
deduplication
consolidation into summaries or beliefs
archival
retention policies
GDPR erasure
procedure distillation
```

Its row lifecycle is practical and broad.

Consolidation can generate a new summary, link it to sources, and soft-delete or archive lower-order rows. Autonomous consolidation is gated.

### 12.2 Menhir

Menhir has a more explicit retrieval lifecycle:

```text
session
persistent
active
compressed
gone
rehydration
```

It also distinguishes evidence and projections, which permits aggressive regeneration of Views without discarding source assertions.

### 12.3 Risk comparison

M3 consolidation risk:

> a generated summary may become the dominant memory representation while source rows are demoted or archived.

Menhir consolidation risk:

> complex lifecycle and projection invariants may fail, leaving assertions unprojected or Views stale.

Benchmark and audit requirements differ accordingly.

---

## 13. Benchmark comparison

### 13.1 M3 published result

M3 reports on LongMemEval-S:

```text
500 questions
92.0% end-to-end QA accuracy: 460 / 500
99.2% session hit-rate at k=10
100% session hit-rate at k=20
no privileged question-type routing labels
Opus 4.6 answerer
upstream GPT-4o judge
```

The report carefully distinguishes retrieval session hit-rate from end-to-end QA accuracy.

It also states that the headline retrieval path used:

```text
raw turns
hybrid FTS5 + BGE-M3 vector retrieval
MMR
no knowledge graph
```

This is important. The result validates M3's retrieval substrate, routing, and answer stack. It does not independently validate every advertised knowledge-maintenance feature.

### 13.2 Menhir current result

As of this note, the owner reports Menhir at approximately 90% on the official LongMemEval Oracle dataset variant.

That run uses the evidence-only benchmark history, avoiding the cost of ingesting and searching the complete distractor fixture during rapid development.

It is a legitimate benchmark result, but it is not directly comparable to M3's LME-S result.

Menhir's current result primarily tests:

```text
evidence ingestion
fact and update capture
identity and temporal processing
View construction
context serialization
answering from Menhir-produced context
```

M3's LME-S result additionally tests:

```text
retrieval from distractor sessions
session ranking
query routing
raw-turn expansion
```

### 13.3 Correct interpretation

The defensible statement is:

> Menhir is near M3's published answer accuracy when evaluated on the evidence-only LME Oracle variant, while M3 has the stronger published end-to-end retrieval result on LME-S.

Do not claim a two-point head-to-head gap.

### 13.4 Benchmark implication

Menhir should keep two benchmark loops:

#### Fast semantic-correctness loop

```text
LME Oracle
all or stratified questions
frequent runs
focus: ingestion, updates, history, state, answering
```

#### Retrieval sentinel loop

```text
LME-S stratified subset
50–100 questions initially
run on significant retrieval changes
focus: distractor resistance and corpus-wide retrieval
```

#### Milestone proof

```text
full LME-S 500-question run
frozen config and commit
published per-category breakdown
retrieval and QA metrics reported separately
```

The full LME-S run is expensive, but it is eventually required for a like-for-like public comparison.

---

## 14. What Menhir's benchmark must prove

M3 already demonstrates that a strong raw-turn hybrid retriever can reach near-ceiling session recall.

Menhir's benchmark story therefore cannot be only:

```text
we found the relevant session
```

It must prove the value of semantic state construction.

### 14.1 Required axes

1. **Capture recall**  
   Was the relevant fact admitted as an assertion?

2. **Current-state accuracy**  
   Does the current View contain the latest valid value?

3. **Historical-state accuracy**  
   Can the system return the earlier value for the correct time?

4. **Update precision**  
   Did the system replace only the affected slot?

5. **Identity continuity**  
   Did aliases and merges preserve the correct subject?

6. **Contradiction precision**  
   Were true conflicts detected without collapsing related facts?

7. **Provenance completeness**  
   Can every answer be traced to exact source evidence?

8. **Abstention correctness**  
   Does the system refuse when the evidence does not determine a current value?

9. **Projection parity**  
   Does stored View state equal a deterministic rebuild from assertions?

10. **Retrieval success**  
    Did current, historical, and raw evidence lanes surface the needed context?

### 14.2 M3-specific adversarial suite

A focused comparison suite should include:

```text
low-cosine semantic updates
high-cosine different-entity facts
multi-fact row with one changed slot
alias changes across sessions
same fact valid in two distinct intervals
late-arriving evidence with old valid-time
retraction without replacement
concurrent conflicting sources
incorrect automatic supersession
manual supersession required
entity merge then unmerge
answer requiring current state plus historical explanation
```

---

## 15. What Menhir should borrow

### 15.1 Benchmark reporting discipline

M3's report clearly separates:

```text
retrieval session hit-rate
end-to-end QA accuracy
routing errors
answer-side errors
per-category results
reproduction commands
methodological caveats
```

Menhir should match or exceed this standard.

### 15.2 Raw-turn retrieval control arm

M3 proves that raw-turn retrieval is an extremely strong baseline.

Menhir should retain a control arm that bypasses semantic projections and retrieves raw evidence using the same embedding and answer stack.

Without this arm, an improvement may be incorrectly attributed to the graph or Views when it came from the reader model or retrieval tuning.

### 15.3 Session expansion

M3's optional whole-session expansion is useful for:

```text
side-clause recall
preference questions
knowledge updates
multi-turn context
```

Menhir should test bounded episode/session expansion as an explicit lane, with source and token-budget tracing.

### 15.4 Variant isolation

M3 tags experimental ingestion variants and rejects certain untagged benchmark-derived summary rows.

Menhir should preserve strict namespace/run isolation so experimental benchmark artifacts cannot contaminate production or other arms.

### 15.5 Non-blocking enrichment

M3 stores verbatim input first, then lets optional extraction fail open or queue.

Menhir should continue strengthening the source-evidence fallback so failed perception never makes the original statement undiscoverable.

### 15.6 Embedding-space identity

M3 explicitly tracks embedding model, dimension, normalization, and space compatibility.

Menhir should treat embedding-space identity as durable metadata and prevent silent comparison of incompatible vectors.

### 15.7 Retrieval explanation

M3 returns component-level retrieval scores.

Menhir's trace should expose similarly clear fields:

```text
raw cosine / lexical score
lane rank
fusion contribution
authority adjustment
temporal adjustment
admission reason
final rank
```

### 15.8 Conservative auto-supersession

Although M3's cosine rule is semantically limited, its conservative threshold reflects the correct safety principle:

> false supersession destroys current-state correctness more severely than retaining an unresolved conflict.

Menhir's semantic-slot system should remain precision-first and abstain when identity or update intent is uncertain.

---

## 16. What Menhir should not copy directly

### 16.1 Do not use embedding similarity as semantic-slot identity

Similarity is useful for candidate generation, never sufficient proof that two claims compete for one slot.

### 16.2 Do not make the memory row the only truth object

Menhir should preserve the evidence → assertion → View separation.

### 16.3 Do not collapse trust into one confidence number

Source authority, evidence support, interpretation uncertainty, currentness, and conflict state should remain inspectable separately.

### 16.4 Do not make retrieval frequency evidence of truth

Access and utility signals may influence retrieval, but they must not establish factual authority.

### 16.5 Do not treat task success as factual corroboration

A successful answer or tool run does not prove every memory used was true.

### 16.6 Do not hide failed perception behind successful source storage

Retaining the source is necessary but insufficient. Menhir must visibly account for episodes that were stored but never became recallable assertions or Views.

---

## 17. Novelty and positioning impact

M3 substantially narrows several claims Menhir should make carefully.

Menhir should not claim standalone novelty for:

```text
typed local agent memory
MCP-native shared memory
bitemporal memory rows
non-destructive supersession
automatic contradiction handling
confidence and Bayesian counters
entity extraction and relationship graphs
hybrid lexical/vector retrieval
MMR diversity
query routing
lifecycle decay and consolidation
procedural-memory distillation
```

M3 already occupies those lanes publicly.

Menhir's stronger positioning is:

> an evidence-backed assertion and View substrate that constructs identity-aware, temporally correct, rebuildable semantic state from raw agent episodes.

The differentiators to defend are:

```text
exact source grounding
assertion identity separate from source text
canonical entity binding as a prerequisite for state
semantic-slot reconciliation
valid-time versus learned-time reasoning
deterministic current-state Views
advisory historical Views
projection parity and assertion-accounting audits
merge/unmerge-aware rebuilds
explicit authority and abstention boundaries
integration of semantic memory with code/file structure
```

---

## 18. Recommended baseline matrix

Menhir's public benchmark should eventually compare at least these arms:

| Arm | Stored representation | Retrieval | Purpose |
|---|---|---|---|
| Raw-turn baseline | Verbatim turns | BM25/vector + optional session expansion | Strong M3-shaped control |
| Curated memory-row baseline | One typed row per selected memory | Hybrid retrieval | Tests benefit of manual/LLM curation |
| Atomic-fact baseline | Extracted fact rows without canonical Views | Hybrid fact retrieval | Isolates extraction from folding |
| Menhir assertions only | Grounded assertions | Assertion/evidence retrieval | Tests source-grounded representation |
| Menhir Views | Assertions + current/history Views | Authority-aware multi-lane recall | Full architecture |
| Menhir Views without identity merge | Same, binding constrained | Full recall | Measures identity layer contribution |
| Menhir Views without history lane | Current state only | Full recall | Measures history projection contribution |

Every arm should use the same:

```text
question set
answer model
judge
max context budget
ingest source text
retrieval top-k policy where possible
```

---

## 19. Concrete follow-up work

1. Add M3 as a named external baseline in the benchmark plan.
2. Reproduce M3's raw-turn retrieval arm inside Recall Labs before attempting full M3 deployment parity.
3. Add a stratified LME-S sentinel set.
4. Freeze and publish Menhir's current ~90% LME Oracle run metadata.
5. Report retrieval and QA separately.
6. Add the M3-specific adversarial update/identity suite.
7. Measure assertion capture, View correctness, and provenance independently of final QA.
8. Run a full 500-question LME-S milestone once ingestion cost is acceptable.
9. Avoid claiming direct superiority until the dataset variant and answer/judge stack match.

---

## 20. Final positioning

M3 is not merely adjacent prior art. It is a direct competitor with substantial shipped overlap and a strong published benchmark.

Its architecture shows that a carefully engineered memory-row system can provide:

```text
local ownership
strong raw retrieval
historical rows
conservative supersession
entity and relationship enrichment
lifecycle maintenance
cross-agent integration
```

Menhir's answer cannot be more feature labels.

The defensible distinction is:

```text
M3
    maintains typed, temporally versioned memory records

Menhir
    preserves evidence, derives typed assertions, binds canonical identity,
    folds semantic slots, and materializes rebuildable current/history Views
```

That distinction is meaningful, but only if the benchmark demonstrates that it improves correctness on updates, history, identity, provenance, and abstention beyond what M3's simpler row-centric architecture can achieve.
