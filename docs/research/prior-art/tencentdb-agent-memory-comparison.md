# TencentDB Agent Memory vs Menhir / Beacon — Prior-Art / Architecture Comparison

**Date:** 2026-08-10  
**Status:** External comparison note; use for architecture positioning, benchmark planning, and roadmap triage.  
**Compared project:** [`TencentCloud/TencentDB-Agent-Memory`](https://github.com/TencentCloud/TencentDB-Agent-Memory)  
**Analyzed public revision:** `0a568c328ea1aae3f22ed3656e7900da7ea565c1`  
**Analyzed branch:** `feat/server_team` (repository default at review time)  
**Primary question:** How closely does TencentDB Agent Memory's L0→L1→L2→L3 hierarchy overlap Menhir/Beacon, and what remains materially different in evidence grounding, identity, truth maintenance, derivation, and benchmark behavior?

---

## 1. Executive verdict

TencentDB Agent Memory is strong prior art for **hierarchical agent memory composition**.

It independently converges on a pipeline that is unusually close to the broad Menhir/Beacon shape:

```text
L0 raw conversation
    ↓ LLM extraction
L1 atomic / structured memories
    ↓ conflict detection + maintenance
L2 scenario blocks
    ↓ synthesis
L3 persona / team operating doctrine
    ↓
context assembly + retrieval + drill-down
```

A reasonable high-level assessment is:

```text
pipeline-shape overlap:       high        ~65–75%
retrieval overlap:            high        ~70%
knowledge-maintenance overlap: medium      ~45–55%
truth/derivation semantics:   moderate     ~35–45%
Beacon-style composition:     high in goal, medium in mechanism
```

The project is much closer to Menhir/Beacon than a flat vector-memory system or a session-summary workflow.

The most important prior-art implication is:

> Menhir/Beacon should not claim novelty merely for layering raw conversations into atomic memories, scenarios, and high-level user/team abstractions.

That general architecture is occupied.

The important remaining distinction is how those higher layers are produced and what guarantees survive composition.

TencentDB Agent Memory primarily uses **LLM-maintained canonical prose artifacts**:

```text
raw messages
  → extracted memory text
  → LLM update / merge decisions
  → LLM-maintained scenario files
  → LLM-maintained persona / doctrine
```

Menhir's intended architecture is instead:

```text
source evidence
  → grounded assertions
  → canonical semantic identity
  → deterministic / policy-governed projections
  → current-state and historical Views
  → adaptive composition for consumer + purpose
```

The concise distinction is:

> TencentDB Agent Memory builds an increasingly compressed hierarchy of model-maintained memories. Menhir/Beacon aims to build an increasingly useful hierarchy of derived semantic state while preserving the durable observations underneath it.

That distinction is meaningful only if Menhir can demonstrate it behaviorally: better correction fidelity, historical reconstruction, provenance, identity continuity, rebuildability, abstention, and purpose-aware composition.

---

## 2. What TencentDB Agent Memory actually is

TencentDB Agent Memory is not simply a chat-memory plugin.

Its public repository contains several memory asset families:

- Chat Memory
- Skills
- Wiki
- CodeGraph
- team / agent asset assignment
- access control and isolation

This note focuses on the Chat Memory hierarchy because that is the closest architectural comparison to Menhir/Beacon.

The memory system explicitly describes four layers:

| Layer | Tencent meaning | Rough Menhir/Beacon analogue |
|---|---|---|
| L0 | raw conversations | Episode / TurnEvidence / raw source evidence |
| L1 | atomic facts, preferences, constraints, events | extracted assertions / durable semantic observations |
| L2 | scenario-oriented knowledge blocks | Derived Semantic Objects / situation summaries |
| L3 | persona or team operating doctrine | high-level Derived / Adaptive Semantic Object-like composition |

The resemblance is real, but the analogues are not exact. The rest of this note explains where they diverge.

---

## 3. Storage model: L0/L1 structured data versus L2/L3 files

Tencent separates the lower and upper layers physically and conceptually.

The code documents two parallel storage abstractions:

```text
IMemoryStore
    L0 / L1 structured data
    SQLite / Tencent Cloud VectorDB

IStorageBackend
    L2 / L3 Markdown artifacts
    local filesystem / COS
```

The file-storage contract explicitly names:

```text
conversations/{date}.jsonl      L0
records/{date}.jsonl            L1
scene_blocks/{name}.md          L2
persona.md                      L3
```

This is important prior art because it treats progressively composed memory as a first-class storage architecture rather than a single index containing differently tagged chunks.

### Menhir implication

Menhir should not position "multiple memory layers" or "derived higher-order memory" as novel by itself.

The stronger claim must concern the semantics of derivation:

- contributor preservation
- identity
- temporal reconstruction
- deterministic or policy-governed rebuilding
- disclosure and consumer awareness
- separation between durable observations and derived state

---

## 4. L0: raw conversation as durable fallback evidence

Tencent's L0 stores raw conversation messages with:

- record ID
- session key
- session ID
- role
- message text
- original timestamp
- recorded timestamp
- team/user/agent/task isolation dimensions

L0 can be searched independently by keyword or vector retrieval.

This produces a useful layered-recall property:

```text
L3 / L2 for orientation
L1 for specific memories
L0 for exact wording / evidence fallback
```

That is architecturally important.

Tencent does not discard raw source material just because higher abstractions exist.

### Where Menhir still differs

L0 is preserved source text, but Menhir's intended provenance model goes further:

```text
source evidence
      ↓ explicit provenance
TypedAssertion / semantic observation
      ↓ contributor relationships
View / projection
```

The key difference is not whether raw text exists somewhere. It is whether each derived semantic statement can identify the source evidence that justified it.

---

## 5. L1 extraction: genuinely structured memory, not merely summaries

L1 extraction is one of the strongest overlaps.

Tencent's extractor reads:

- recent new messages
- a bounded number of background messages
- previous scene context

It performs **scene segmentation and memory extraction in one LLM call**.

Each extracted L1 memory carries fields including:

```text
content
memory type
priority
scene_name
source_message_ids
metadata
timestamps
session key / session ID
team / user / agent / task identity
version
```

Current memory types include both chat-memory and work-memory forms:

```text
persona
episodic
instruction
work_fact
work_task
work_method
work_artifact
```

This is materially beyond a session summary.

The extraction output tries to produce reusable semantic units with source-message linkage.

### Menhir overlap

Both systems therefore contain an ingestion step of the rough form:

```text
raw turns
    ↓
semantic candidates
    ↓
durable memory objects
```

Both also recognize that extracting an atomic/useful fact separately from raw conversation is necessary for later memory quality.

### Menhir distinction

Tencent's primary L1 semantic unit remains **model-authored prose**.

Menhir's intended durable semantic unit is more explicit:

```text
subject identity
predicate / semantic slot
object or scalar value
operation / state semantics
valid time
source evidence
provenance / declarant
```

The distinction matters because prose memory can contain multiple claims whose lifecycle later becomes coupled.

---

## 6. L1 conflict handling: candidate retrieval + LLM state maintenance

Tencent has a serious conflict-maintenance pipeline.

It does not simply append every extracted memory.

For each new memory:

```text
1. retrieve likely related existing L1 records
2. build a candidate pool
3. ask an LLM to judge the relationship
4. choose one of:
       store
       skip
       update
       merge
```

Candidate retrieval is layered:

```text
vector search
    ↓ fallback
FTS / BM25
    ↓ fallback
skip conflict detection and store
```

The LLM receives the candidate memories plus the new candidate and explicitly decides whether they refer to the same fact/event/work object.

This differs from M3's primarily threshold-driven automatic contradiction path. Similarity is used to find candidates; semantic judgment is delegated to the LLM.

### Multi-target maintenance

Tencent supports one new memory replacing or merging multiple old memories through `target_ids[]`.

It also permits cross-type merge/update.

For example, the conflict prompt allows an episodic event and a persona-like memory to become one consolidated record if the model decides they describe the same underlying information.

That makes the system a genuine model-driven knowledge-maintenance engine, not just deduplication.

---

## 7. The central divergence: canonical prose rewriting versus observation preservation

Tencent's L1 maintenance semantics are fundamentally different from Menhir's intended semantic-state model.

Consider:

```text
T1: Rachel moved to Chicago.
T2: Rachel moved back to the suburbs.
```

Tencent's maintenance path is approximately:

```text
L1 old memory:
    Rachel lives in Chicago.

L1 new memory:
    Rachel moved back to the suburbs.

candidate retrieval
    ↓
LLM judgment
    ↓
UPDATE old memory
    ↓
new canonical prose memory:
    Rachel currently lives in the suburbs ...
```

The retrieval-layer old record is removed and replaced by the updated record.

Menhir's intended shape is:

```text
Evidence A
   ↓
Assertion A
Rachel --residence--> Chicago
valid_at = T1

Evidence B
   ↓
Assertion B
Rachel --residence--> suburbs
valid_at = T2

             ↓ deterministic slot fold

ScalarStateView
residence = suburbs

ScalarHistoryView
T1 Chicago
T2 suburbs
```

The current state is derived from observations rather than written as the replacement observation.

### Why this matters

With canonical prose rewriting, several concerns are coupled:

- current value
- historical value
- retained details
- interpretation of the update
- provenance
- wording

With assertion-preserving derivation, these can remain separate.

That should be one of Menhir's primary architectural claims relative to Tencent.

---

## 8. What happens to old L1 records

Tencent's L1 writer documents two storage behaviors:

### Retrieval store

On `update` or `merge`:

```text
old target records
    → deleted from VectorStore
new merged/updated record
    → inserted
```

This improves current retrieval cleanliness.

### JSONL history

L1 JSONL is append-only for backup/recovery.

Old records remain there temporarily, while a cleaner later reconciles old file records against the retrieval store.

This is useful operational history, but it is not equivalent to first-class semantic temporal history.

The live model does not expose the old value as a durable assertion that participates in a formal historical projection.

### Menhir implication

Menhir can credibly distinguish:

> operational change history

from:

> semantically queryable historical state with explicit evidence contributors.

---

## 9. Timestamp and version semantics

Tencent L1 records include:

- `timestamps[]`
- `createdAt`
- `updatedAt`
- monotonic `version`
- activity time ranges for episodic memories

During merge/update, the conflict prompt asks the LLM to union the timestamps from the related memories.

This preserves some temporal evidence about the composed memory.

However, the timestamps are attributes of the resulting prose record rather than independent proposition-validity intervals.

For a multi-claim merged memory, `timestamps[]` may tell us that several moments contributed to the record without specifying which proposition was valid at which moment.

Menhir's temporal distinction remains stronger if it maintains:

```text
claim / slot
valid time
learned time
historical predecessor/successor semantics
```

at the assertion or projection level.

---

## 10. Provenance: stronger than summary systems, weaker than contributor-preserving Views

Tencent deserves credit for `source_message_ids` at L1.

That means an extracted memory can identify which raw messages produced it.

This is substantially better than systems where a summary becomes detached from its source conversation.

However, update/merge complicates provenance.

A merged L1 memory may synthesize multiple earlier records. The resulting writer preserves the new extracted memory's fields and merged timestamps, but the semantic relationship from every clause in the merged prose back to every contributing source is not represented as a first-class contributor graph.

This creates benchmarkable questions:

- Can the system identify the exact message supporting a specific clause after multiple merges?
- Can it distinguish which source supported the old value versus the new value?
- Can it retract one contributor without rebuilding unrelated claims manually?

These are useful Menhir differentiators.

---

## 11. L2 scene blocks: strong prior art for Derived Semantic Objects

Tencent's L2 layer is one of the most relevant parts of the project for Beacon.

L1 memories are fed into an LLM-driven `SceneExtractor`.

The LLM sees:

- incoming memories
- summaries of existing scene blocks
- scene-count constraints
- existing filenames
- current time

It can:

- create a new scene
- update an existing scene
- merge scenes
- reorganize scene content

The output is persisted as Markdown scenario files.

This is an explicit higher-order semantic composition layer.

Conceptually:

```text
atomic memories
     ↓
scenario-oriented composed understanding
```

That overlaps strongly with the motivation for Derived Semantic Objects.

### Important prior-art boundary

Menhir/Beacon should not claim that the idea of synthesizing lower-level memories into higher-level situation objects is itself novel.

Tencent clearly does this.

The remaining distinction is the object contract.

Tencent L2 is:

> mutable LLM-authored prose, maintained in scenario files.

A Menhir Derived Semantic Object should instead be defensible as:

> a typed derived object whose contributors, composition policy, identity, and lifecycle are explicit enough that it can be rebuilt and audited.

---

## 12. L2 maintenance is agentic, not deterministic

The SceneExtractor is deliberately agentic.

It runs an LLM with file tools in a sandbox rooted to the scene-block directory.

The model directly edits the scene files.

The system provides operational safety through:

- sandboxed file visibility
- backups
- checkpoints
- scene indexing
- cleanup of soft-deleted files
- limits on the number of scenes

Those are good engineering controls.

But they are not semantic reproducibility guarantees.

Given the same L1 inputs, model/provider/prompt changes may produce a different L2 composition.

### Menhir implication

Beacon should distinguish two concepts explicitly:

1. **LLM composition** may be used to construct useful derived artifacts.
2. **Derived-object identity/provenance** should not depend on pretending that the model output is itself ground truth.

If Beacon uses model-based composition, the policy/model/version/contributors should be part of the derivation record.

---

## 13. L3 persona: very close to high-level semantic composition

Tencent's chat-mode L3 is a persistent persona document.

The generation prompt asks the LLM to synthesize L2 scenes into several conceptual layers:

```text
Layer 1: basic anchors / facts
Layer 2: interests
Layer 3: interaction protocol
Layer 4: cognitive core / deeper patterns
```

The prompt encourages cross-domain synthesis rather than flat fact listing.

This is important because L3 is not merely "more compression." It attempts to derive a useful model of the user.

### Beacon relevance

This is close to the product motivation behind Beacon:

> compose enough structured understanding that an unfamiliar consumer can become competent quickly.

Tencent has therefore already occupied much of the conceptual space around:

- persistent user orientation
- interaction-preference synthesis
- long-horizon persona composition
- hierarchical context reduction

Beacon must differentiate on its composition contract, not the mere existence of a persona-like artifact.

---

## 14. Team L3: Operating Doctrine is even more Beacon-adjacent

In work/code mode, the L3 persona file becomes a **Team Operating Doctrine**.

The prompt explicitly asks the model to abstract L2 project scenarios into reusable:

- SOPs
- principles
- decision logic
- boundaries
- anti-patterns
- agent rules

It explicitly rejects low-level project facts and one-off task state unless they can be generalized.

This is highly relevant prior art for purpose-oriented derived semantic objects.

The goal is:

```text
many project observations
       ↓
compact reusable behavioral knowledge
       ↓
future agent acts competently without replaying all source work
```

That overlaps strongly with Beacon's competence-oriented composition goal.

### Difference from Adaptive Semantic Objects

Tencent's L3 doctrine is persistent and largely canonical for a team/agent scope.

Beacon's Adaptive Semantic Object concept is stronger if composition is explicitly parameterized by:

```text
targets
consumer
purpose
disclosure profile
context budget
composition policy
```

That means two consumers can legitimately receive different derived objects from the same durable knowledge without mutating the durable layer or establishing one prose artifact as universally canonical.

This should remain a central Beacon distinction.

---

## 15. Recall architecture: progressive disclosure rather than flat RAG

Tencent's recall path is also relevant prior art.

At a high level:

```text
L3 persona / doctrine
    stable context

L2 scene navigation
    orientation + drill-down map

L1 memories
    query-specific retrieval

L0 conversation search
    exact source fallback
```

The auto-recall implementation separates stable from dynamic context:

- L3 persona is loaded into stable system context.
- L2 scene navigation is loaded into stable system context.
- L1 memories are dynamically retrieved per user query.
- tools allow deeper retrieval from L1 or L0 when injected context is insufficient.

This is a sophisticated context architecture.

### Menhir / Beacon implication

The progressive-disclosure idea is not unique:

```text
orientation
   ↓
scenario map
   ↓
atomic facts
   ↓
raw evidence
```

Beacon should treat this as validation of the approach and compete on precision, provenance, consumer adaptation, and competence-per-token.

---

## 16. L1 retrieval: conventional but strong hybrid RAG

Tencent's L1 retrieval supports:

- SQLite FTS5 / BM25
- dense embedding similarity
- client-side RRF
- Tencent VectorDB native hybrid search
- configurable result limits
- score thresholds
- timeout budgets

On the SQLite path, keyword and embedding retrieval run in parallel and are merged with Reciprocal Rank Fusion.

On Tencent VectorDB, native dense+sparse hybrid retrieval can be used in one call.

The retrieval subsystem itself is not a major Menhir novelty boundary.

Menhir should assume hybrid retrieval is baseline infrastructure.

---

## 17. Identity model: substantial scoping identity, limited semantic identity

Tencent has strong operational identity dimensions:

```text
team
user
agent
session
task
memory record ID
scene file
profile scope
asset ownership / ACL
```

This is meaningful and mature multi-tenant engineering.

However, the Chat Memory pipeline does not appear to center a canonical semantic-entity identity layer equivalent to Menhir's identity-bearing Semantic Objects.

A memory record's identity is primarily record/document identity.

A scenario's identity is primarily scenario-file identity.

When content is merged or rewritten, semantic continuity is model-mediated rather than a first-class graph invariant.

### Menhir implication

Canonical entity identity, aliasing, merge/split, contributor continuity, and semantic-object identity remain defensible architecture differences.

---

## 18. Correction model comparison

The systems can be summarized as follows.

### Tencent

```text
new memory
    ↓
retrieve related old records
    ↓
LLM decides same fact/event/object?
    ↓
store / skip / update / merge
    ↓
replace retrieval-layer record(s)
```

### Menhir

```text
new evidence
    ↓
extract grounded assertion
    ↓
bind canonical subject + slot
    ↓
retain prior assertions
    ↓
rebuild slot projection
    ↓
current View + historical View
```

Tencent's design is flexible and semantically capable.

Menhir's intended design trades some flexibility for stronger invariants around history and rebuildability.

That tradeoff should be benchmarked rather than merely asserted.

---

## 19. Failure-mode comparison

### Tencent likely failure modes

Because L1/L2/L3 are model-maintained textual objects, important failure modes include:

1. **merge overreach** — distinct facts are merged because they sound related.
2. **update overreach** — new information replaces old information that was still independently valid.
3. **history collapse** — an old value disappears from the live semantic layer.
4. **clause provenance loss** — a merged prose statement no longer maps cleanly to individual evidence sources.
5. **summary drift** — repeated L2/L3 edits introduce information not justified by source memory.
6. **irreversible compression** — details omitted from L2/L3 cannot be reconstructed without dropping back to lower layers.
7. **cross-claim coupling** — correcting one clause requires rewriting a multi-claim memory.
8. **model-version drift** — rebuilding with a new model/prompt yields meaningfully different canonical artifacts.

### Menhir likely failure modes

Menhir has different risks:

1. extraction misses
2. incorrect entity binding
3. wrong semantic-slot mapping
4. merge/split identity defects
5. incomplete projection rules
6. over-constrained deterministic folds
7. retrieval failure across multiple artifact lanes
8. expensive ingestion / reconstruction

The benchmark should expose both families of failure rather than optimize only for final-answer accuracy.

---

## 20. Benchmark status

Tencent's README currently publishes a PersonaMem result:

```text
without TencentDB Agent Memory: 48%
with TencentDB Agent Memory:    76%
relative improvement:           +59%
```

At review time, a repository search found the public headline claim but not an obvious checked-in PersonaMem benchmark harness/report comparable to M3's LongMemEval materials.

Therefore:

> Treat the 76% result as an externally reported product benchmark, not yet as a reproducible Menhir baseline.

Do not directly compare it to Menhir's LongMemEval results.

The more important use of this project for Menhir is architectural baseline design.

---

## 21. Recommended Tencent-style benchmark baseline

A useful comparison baseline would implement the **mechanism**, not necessarily Tencent's entire product.

### Baseline: layered prose memory

```text
L0
raw sessions

L1
LLM-extracted atomic prose memories
source-message IDs retained

L1 maintenance
retrieve top related memories
LLM chooses store / skip / update / merge

L2
LLM-maintained scenario summaries

L3
LLM-maintained persona / operating doctrine
```

Then compare Menhir against it on the same fixtures and answerer.

This baseline is more informative than a flat vector-RAG baseline because it directly tests whether Menhir's additional semantic structure buys anything over competent hierarchical LLM memory maintenance.

---

## 22. High-value adversarial benchmark cases

### 22.1 Simple scalar update

```text
T1: Rachel lives in Chicago.
T2: Rachel moved back to the suburbs.
```

Measure:

- current-state answer
- previous-state answer
- exact evidence source for both values

### 22.2 Complement versus correction

```text
T1: Alex works at Acme.
T2: Alex manages the infrastructure team.
```

These should coexist, not update each other.

### 22.3 Same topic, different slots

```text
T1: Priya's favorite color is blue.
T2: Priya's car is red.
```

A prose merge is acceptable only if both claims remain independently addressable.

### 22.4 Multi-claim memory partial correction

```text
T1: Sam lives in Dallas and drives a Honda.
T2: Sam moved to Austin.
```

Measure whether residence can update without rewriting or losing the vehicle fact.

### 22.5 Retraction

```text
T1: I am allergic to peanuts.
T2: That was a mistake; I am not allergic to peanuts.
```

Measure current state, historical state, retraction semantics, and evidence.

### 22.6 Ambiguous contradiction

```text
T1: Jordan usually works from home.
T2: Jordan worked from the office today.
```

A one-day event should not overwrite a stable preference/pattern.

### 22.7 Entity alias drift

```text
T1: Robert joined Acme.
T2: Bob was promoted to director.
```

Measure whether both bind to the same durable entity without model-only assumption.

### 22.8 Entity split

Two people initially conflated under one name later become distinguishable.

Measure whether derived state can be repaired without rewriting unrelated evidence.

### 22.9 Merge provenance

Three independent memories are consolidated into one higher-level statement.

Ask for the exact source supporting one clause.

### 22.10 High-level synthesis correction

L3 infers:

```text
User prefers concise technical answers.
```

Later evidence changes that preference.

Measure whether:

- the durable observations remain intact
- the derived orientation updates
- unrelated L3 traits remain stable

### 22.11 Purpose-specific composition

Same durable knowledge, different consumers:

```text
coding agent
support agent
new project collaborator
```

Measure whether each receives different useful orientation without changing durable memory.

### 22.12 Context-budget pressure

Give the system more relevant durable knowledge than fits in the answer context.

Measure:

- answer accuracy
- provenance retention
- competence per token
- stale-fact suppression

---

## 23. Metrics that matter for this comparison

Final QA accuracy is necessary but insufficient.

Use at least:

### Ingestion

- assertion / memory extraction recall
- extraction precision
- source-message linkage accuracy

### Knowledge maintenance

- correction acceptance
- false update rate
- false merge rate
- stale-value suppression
- independent-claim preservation

### Temporal behavior

- current-state accuracy
- historical-state accuracy
- temporal ordering accuracy
- retraction correctness

### Provenance

- evidence precision
- evidence recall
- clause-to-source traceability after merge

### Identity

- alias continuity
- false entity merge rate
- missed entity merge rate
- split repair correctness

### Derived composition

- L2 scenario factuality
- L3 factuality
- unsupported-inference rate
- reconstruction stability
- consumer/purpose relevance
- competence per token

### Retrieval / answer

- recall@k
- answer accuracy
- abstention calibration
- context size
- latency
- write cost

---

## 24. Mechanisms worth borrowing

Tencent contains several mechanisms Menhir/Beacon should consider on their own merits.

### 24.1 Progressive disclosure across memory layers

The L3 → L2 → L1 → L0 drill-down pattern is excellent.

Beacon should support increasingly precise descent from orientation to source evidence.

### 24.2 Source-message IDs on extracted memories

Every extracted semantic memory should retain explicit links to source evidence.

Menhir already aims beyond this; Tencent validates that the linkage is operationally valuable.

### 24.3 Candidate retrieval before expensive semantic conflict judgment

Tencent's two-stage pattern is efficient:

```text
cheap recall
   ↓
expensive semantic decision only on candidates
```

This is a good general pattern for maintenance operations.

### 24.4 Explicit maintenance action taxonomy

`store | skip | update | merge` is simple and inspectable.

Even if Menhir uses different internal semantics, comparable decision receipts would improve debugging and benchmarking.

### 24.5 Stable versus dynamic recall context

Tencent separates:

- stable L2/L3 context
- dynamic per-turn L1 retrieval

This is useful for prompt caching and context budgeting.

### 24.6 Scene-count pressure and consolidation triggers

Tencent explicitly limits scenario proliferation and pushes the model toward merge/update as the number of scenes grows.

Beacon should have an equivalent policy for derived-object proliferation.

### 24.7 Sandboxed composition agents

The L2 writer can only operate within the scene-block workspace.

That is a useful safety pattern if Beacon employs tool-using composition agents.

### 24.8 Backups/checkpoints around model-authored derived artifacts

Model-authored derived state should be recoverable after partial or bad writes.

Even if Menhir's Views are rebuildable, operational checkpoints can still reduce recovery cost.

---

## 25. Mechanisms not to copy blindly

### 25.1 Do not make rewritten prose the only live semantic truth

A canonical prose record is convenient, but independent claims should not lose independent lifecycle.

### 25.2 Do not treat merged timestamps as semantic temporal history

A list of contributing timestamps is not equivalent to per-claim valid-time semantics.

### 25.3 Do not let model synthesis silently erase contributor structure

Higher-order derived objects should preserve contributor identity.

### 25.4 Do not make L3 universally canonical

A persona/doctrine optimized for one consumer or task should not automatically become the universal representation of the underlying knowledge.

### 25.5 Do not confuse backup history with semantic history

Append-only JSONL is valuable provenance infrastructure, but history should be queryable at the semantic level when historical correctness matters.

### 25.6 Do not let L2/L3 correctness depend on prompt stability alone

Composition policy, model/version, contributors, and output identity should be inspectable.

---

## 26. Positioning / novelty consequences

Tencent substantially narrows several claims Menhir/Beacon should avoid.

Do **not** claim novelty for:

- L0→L1→L2→L3 hierarchical memory
- raw conversation + extracted atomic memory
- scenario-level memory composition
- persona-level memory composition
- team operating doctrine derived from work history
- progressive memory retrieval from abstraction to raw evidence
- hybrid retrieval over structured memories
- LLM-driven update / merge maintenance
- source-message linkage on extracted memories
- persistent cross-agent memory assets

Those are existing public prior art.

### More defensible Menhir differentiation

Menhir should center claims around combinations such as:

- evidence-backed TypedAssertions as durable semantic observations
- canonical semantic-object identity with alias/merge/split continuity
- semantic-slot-based current-state reconstruction
- preservation of old observations rather than canonical rewrite
- explicit distinction between valid time and learned time
- deterministic rebuildable current-state Views
- first-class historical Views
- contributor-preserving derivation
- source/declarant trust carried through projections
- abstention when state cannot be safely materialized

### More defensible Beacon differentiation

Beacon should center claims around:

- consumer-aware derived semantic objects
- purpose-aware composition
- disclosure-aware composition
- context-budget-aware composition
- explicit composition policy
- derived-object provenance and rebuildability
- orientation as a specialization optimized for Time-to-Competence rather than a single canonical persona

---

## 27. Relationship to other prior art

This project occupies a different part of the comparison space than M3 or Athena.

### M3 Memory

Strongest overlap with Menhir's:

- typed persistent memory
- supersession
- temporal state
- contradiction maintenance
- local-first retrieval infrastructure

M3 is the closer **knowledge-maintenance/state competitor**.

### TencentDB Agent Memory

Strongest overlap with:

- hierarchical memory abstraction
- progressive composition
- L1 maintenance
- scenario-level derived memory
- persona/doctrine composition
- multi-agent memory delivery

Tencent is the closer **Menhir + Beacon hierarchical-composition competitor**.

### Athena-Public

Useful baseline for:

- model-curated summaries
- canonical memory files
- hybrid retrieval

Athena is less structurally similar than Tencent.

---

## 28. Practical benchmark strategy

A useful staged strategy is:

### Stage A — fast knowledge-maintenance loop

Use the current efficient benchmark fixture to compare:

```text
raw evidence baseline
Tencent-style L1 prose memory baseline
Menhir assertion/state pipeline
```

Focus on:

- updates
- corrections
- temporal questions
- provenance
- identity

### Stage B — composition benchmark

Construct a smaller suite that requires:

```text
L1 factual recall
L2 situation reconstruction
L3 user/team orientation
```

Compare:

```text
Tencent-style persistent L2/L3 summaries
Beacon derived/adaptive objects
```

### Stage C — full distractor retrieval

Only after knowledge behavior is strong, run larger no-oracle / distractor-heavy retrieval suites to test whether the architecture survives corpus-scale retrieval pressure.

This keeps expensive retrieval evaluation from obscuring whether the semantic state itself is correct.

---

## 29. Recommended implementation baseline for Recall Labs

Build a deliberately simple Tencent-inspired arm rather than reproducing the full repo.

### L0

Use benchmark raw sessions.

### L1 extraction

Ask the same model family to emit:

```json
{
  "content": "...",
  "type": "persona|episodic|instruction|fact",
  "source_message_ids": ["..."],
  "priority": 0
}
```

### L1 maintenance

For each new memory:

1. vector-retrieve top 5 existing memories
2. ask model for `store|skip|update|merge`
3. replace/update canonical prose records

### L2

Periodically synthesize scenario blocks from active L1 memories.

### L3

Synthesize a compact persona/orientation document from L2.

### Retrieval

Use:

```text
L3 always-on compact context
L2 navigation or top scenario matches
L1 BM25/vector/RRF
L0 evidence fallback
```

Then compare against Menhir/Beacon with matched answerer, token budget, and corpus.

This would be one of the strongest architecture-level baselines available because it tests whether explicit semantic-object machinery materially beats a competent hierarchical memory system.

---

## 30. Final assessment

TencentDB Agent Memory is significant prior art.

Its importance is not the PersonaMem headline score. Its importance is that it independently demonstrates a coherent production architecture in which:

```text
raw conversations
    become atomic memories
    become scenario knowledge
    become high-level persona / doctrine
    and are recalled through progressive disclosure
```

That overlaps substantially with the broad Menhir/Beacon direction.

The project therefore invalidates weak novelty claims around hierarchical memory and higher-order composition.

It does **not** collapse Menhir's intended semantic-state architecture, because Tencent primarily maintains canonical prose memories through model-driven update/merge operations rather than preserving grounded assertions and deriving current/historical state from them.

It also does **not** collapse Beacon's Adaptive Semantic Object direction, because Tencent's L3 is largely a persistent canonical artifact rather than a consumer-, purpose-, disclosure-, and context-budget-specific composition.

The benchmark obligation is now clearer:

> Menhir must prove that preserving evidence, identity, temporal semantics, and rebuildable projections produces better knowledge correctness than model-maintained hierarchical prose.

And Beacon must prove:

> adaptive, provenance-preserving composition produces better Time-to-Competence than a persistent L2/L3 summary hierarchy.

If those advantages cannot be measured, Tencent's simpler architecture is a serious argument that the extra machinery is unnecessary.
