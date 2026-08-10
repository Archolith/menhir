# Athena-Public vs Menhir — Prior-Art / Benchmark-Relevance Comparison

**Date:** 2026-07-27  
**Status:** External comparison note; use for benchmark planning, roadmap triage, and novelty/positioning discipline.  
**Compared project:** [`winstonkoh87/Athena-Public`](https://github.com/winstonkoh87/Athena-Public)  
**Analyzed public revision:** `c6d78d41734a8ccc721fd4d04e6462fa9de1726c`  
**Primary question:** Does Athena overlap Menhir's knowledge-correctness benchmark target, and which mechanisms are useful enough to preserve as prior art or test as baselines?

---

## 1. Executive verdict

Athena is substantial prior art for a **personal cognitive workspace built around model-written Markdown, hybrid RAG, session rituals, and governance protocols**.

It is not strong prior art for Menhir's central technical claim:

> reconstructing, maintaining, and recalling grounded, current, identity-aware knowledge from raw episodes without requiring the agent to curate the truth first.

Athena's durable semantic loop is approximately:

```text
conversation
-> model follows /end workflow
-> model writes decisions, learnings, summaries, and checkpoints
-> deterministic scripts parse marked sections
-> Markdown and chunks are indexed
-> later queries retrieve files/chunks with hybrid RAG
```

Menhir's intended loop is materially different:

```text
raw episode
-> perception of grounded assertions
-> durable identity binding
-> evidence/provenance preservation
-> temporal reconciliation and supersession
-> deterministic or governed View construction
-> recall of current and historical state
```

The benchmark conclusion is:

```text
Athena should be treated as a serious model-curated-summary + hybrid-RAG baseline.
It should not be treated as a direct substitute for an evidence-backed knowledge system.
```

The product-usability comparison is deliberately out of scope. Athena is visibly more packaged as an end-user workflow, but Menhir's current priority is proving knowledge correctness.

Menhir should not claim novelty for:

- local-first Markdown memory
- portable memory across model providers
- model-authored session summaries
- boot-time memory files
- vector retrieval over session chunks
- weighted reciprocal-rank fusion
- cross-encoder or LLM reranking
- retrieval telemetry
- agent governance encoded as prompts, protocols, and hooks

Menhir remains differentiated if the benchmark proves:

- buried fact and update capture from raw conversation
- current-versus-historical state correctness
- source-grounded assertions and evidence lineage
- entity identity continuity across aliases, merges, and splits
- contradiction and correction handling
- deterministic rebuildability of current Views
- explicit separation of observed evidence from derived memory

---

## 2. What Athena actually is

Athena is closer to:

```text
personal operating workspace
+ context-engineering discipline
+ curated knowledge archive
+ retrieval layer
+ behavioral/governance framework
```

than to:

```text
automatic proposition-level knowledge maintenance from raw episodes
```

Its public repository combines:

- a large Markdown workspace
- session logs and a small boot memory bank
- slash-command workflows such as `/start`, `/end`, `/ultrastart`, and `/ultraend`
- hundreds of protocols and skills
- a Python SDK and MCP server
- Supabase/pgvector chunk storage
- local SQLite indexes and caches
- weighted RRF retrieval
- reranking
- telemetry
- governance and safety rules

Most of this sits above or beside the narrower memory-correctness problem Menhir is currently benchmarking.

---

## 3. Durable memory and extraction boundary

Athena's primary human-readable memory unit is a Markdown file.

The public architecture describes:

- session logs
- case studies
- observations
- profile documents
- a ten-file memory bank
- `CANONICAL.md`
- project and decision indexes
- protocol and workflow files

Its vector layer stores document chunks with:

```text
file_path
table_name
chunk_index
title
content
embedding
metadata
```

The key semantic boundary is `/end`.

The workflow asks the active model to write:

- decisions
- insights
- action items
- system learnings marked `[S]`
- user learnings marked `[U]`
- validated patterns
- a compact checkpoint
- optional canonical-memory updates
- project-state updates

Deterministic shutdown code then parses marked sections and propagates them into longer-lived files.

This is a real architecture, but its semantic reliability depends on the model correctly deciding:

- what mattered
- what was a decision
- what was learned about the user
- what should become canonical
- what contradicted existing canonical text
- what should be retained or omitted

The central distinction is:

```text
Athena asks the model to curate memory explicitly.
Menhir is trying to derive and maintain knowledge from the raw episode itself.
```

Athena's indexed unit is generally:

```text
file or chunk of model-/human-authored prose
```

not:

```text
source-grounded proposition with stable claim identity,
entity binding, valid time, learned time, evidence tier,
and deterministic state-slot semantics
```

---

## 4. Retrieval architecture

Athena contains a genuine hybrid retrieval implementation.

The current public search code includes live channels for:

- canonical Markdown
- vector chunks
- local SQLite
- filenames
- framework and memory-bank documents
- optional web search

Results are fused with weighted RRF and may then be reranked.

Vector results are divided by content type so different source classes receive different weights, including:

- sessions
- protocols
- case studies
- capabilities
- workflows
- system documents
- user profile
- entities

Athena also includes:

- exact-result cache
- semantic cache
- SQLite embedding cache
- batch embedding support
- fallback when vectors are unavailable
- archive-path suppression
- retrieval telemetry
- source attribution

These are meaningful engineering strengths.

### 4.1 Documentation drift

The architecture document describes seven parallel channels and includes GraphRAG language.

The current search implementation explicitly says:

- tag and exocortex channels were retired because their backing data was absent or archived
- GraphRAG was removed because it was stale
- vector retrieval is the only live semantic channel
- the remaining local channels are lexical or structural lookups

The implementation should be treated as the source of truth for benchmark comparison.

### 4.2 Menhir overlap

Menhir already has or is evaluating:

- BM25 and vector candidate lanes
- weighted RRF
- content-vector retrieval
- source attribution and source-aware admission
- reranking/scoring stages
- retrieval traces
- file-context and structural candidate injection
- read-without-reinforcement evaluation modes

Athena does not create a new retrieval direction for Menhir.

Its value is as a clean baseline shape:

```text
curated summary artifacts
+ multiple simple retrieval lanes
+ weighted fusion
+ reranking
```

---

## 5. Temporal knowledge, corrections, and evidence

Athena has strong document chronology:

- dated session files
- previous/next session lineage
- Git history
- active-context checkpoints
- project switchboards
- decision logs
- canonical-memory updates

This is useful auditability, but it is not proposition-level temporal knowledge.

Athena does not visibly provide a general durable primitive equivalent to:

```text
subject identity
+ state slot
+ value
+ operation
+ valid_at
+ learned_at
+ exact source span
+ evidence tier
+ current/superseded assertion head
```

### 5.1 Example update

Given:

```text
Session 1: Rachel moved to Chicago.
Session 2: Rachel moved back to the suburbs.
```

Athena can succeed if the `/end` model:

- notices the update
- writes the new fact
- locates the old canonical statement
- edits or replaces it correctly
- preserves enough history elsewhere

But the architecture does not independently guarantee:

- one durable Rachel identity
- one `residence` state slot
- Chicago becoming historical
- suburbs becoming current
- exact source-span preservation
- deterministic rebuilding from append-oriented observations

This is exactly the gap Menhir's update benchmark should expose.

### 5.2 Contradictions and corrections

Athena has procedural instructions for canonical checks, red-team review, and disagreement with the user.

Its correction path is roughly:

```text
model notices contradiction
-> model edits canonical prose
-> Git preserves document history
```

Menhir's intended path is stronger:

```text
new grounded observation
-> identity and slot correlation
-> contradiction/update classification
-> append or supersede assertion
-> preserve old assertion and evidence
-> rebuild current View
```

### 5.3 Provenance

Athena preserves useful document-level provenance:

- session IDs
- dates
- Git commits
- file paths
- provenance tags for canonical entries
- retrieval source metadata

But a model-written summary does not necessarily retain:

- the exact quoted source span
- character offsets
- competing interpretations
- perceiver version
- evidence authority
- binding decision
- deterministic path from evidence to current state

Menhir should preserve the distinction:

```text
session summary = derived artifact
source episode = evidence
assertion = grounded interpretation
View = rebuildable current-state projection
```

---

## 6. Governance is interesting but outside the present benchmark

Athena's distinctive product direction includes:

- constitutional laws
- capability levels
- ruin checks
- circuit breakers
- red-team protocols
- anti-sycophancy framing
- explicit labels for code-enforced, agent-discretion, and aspirational mechanisms

The public README is unusually candid that some governance behavior is only code-enforced in specific environments and falls back to model discretion elsewhere.

That documentation discipline is worth copying.

However, governance should not distort the current benchmark.

Current scope:

```text
Did the system capture, maintain, and recall the correct knowledge?
```

Later product scope:

```text
Did the system package that knowledge into a useful workflow?
Did it challenge the user appropriately?
Did the boot experience feel coherent?
```

Those are separate axes.

---

## 7. Direct comparison

| Dimension | Athena-Public | Menhir target |
|---|---|---|
| Primary durable unit | Markdown file / vectorized chunk | Episode, entity, grounded assertion, evidence, View |
| Semantic extraction | Model writes `/end` summary and learnings | System perceives claims from raw episodes |
| Identity | Names and prose references | Durable entity identity with alias/merge/split continuity |
| Temporal model | Session chronology, Git history, canonical edits | Valid time, learned time, supersession, historical assertions |
| Contradictions | Model-mediated canonical edit | Durable conflict/update classification and governed fold |
| Provenance | File, session, commit, optional tag | Exact source span, episode, tier, perceiver, lineage |
| Retrieval | Chunk vectors + lexical lanes + RRF + rerank | Multi-lane retrieval + graph/structure/evidence-aware scoring |
| Rebuildability | Re-read current files/history | Recompute Views from durable observations |
| Human discipline | `/end` and curation loop are load-bearing | Raw episode should remain sufficient for repair/replay |
| Product focus | Personal cognitive workspace | Evidence-backed semantic substrate |

---

## 8. Recommended Athena-style benchmark arm

Athena is most useful as a baseline architecture, not merely a named competitor.

### 8.1 Arm definition

```text
Athena-style Summary RAG

Input:
    the same raw sessions provided to Menhir

Write path:
    after each session, call a fixed model prompt that emits:
    - summary
    - decisions
    - user learnings
    - current state
    - pending items
    - optional canonical updates

Storage:
    Markdown artifacts plus chunk embeddings

Recall:
    lexical lanes + vector lane
    weighted RRF
    rerank top candidate pool

Answer:
    same answer model and answer token budget as other arms
```

### 8.2 Two useful variants

#### Variant A: append-only summaries

Each session summary is stored independently. No canonical file is edited.

This measures:

- summary extraction quality
- retrieval quality
- contradiction accumulation
- whether the answer model selects the latest value

#### Variant B: model-maintained canonical memory

The close model may update a bounded canonical-memory document after each session.

This measures:

- model-driven consolidation
- stale-fact replacement
- information loss during rewriting
- dependence on correct close-time curation

Menhir should be compared against both.

### 8.3 Fairness constraints

- Same underlying model family where possible.
- Same raw source sessions.
- No gold labels in the close prompt.
- No manually corrected summaries.
- Fixed summary schema and token budget.
- Fixed retrieval top-k and answer budget.
- Record write-time and recall-time costs separately.
- Preserve generated artifacts for failure analysis.
- Run multiple seeds when the summarizer is nondeterministic.

---

## 9. Benchmark cases Athena helps motivate

### 9.1 Summary omission

A fact is explicit but not salient enough for the close model to retain.

```text
summary system may permanently lose it
source-grounded system can still perceive it from the episode
```

### 9.2 Buried update

A changed value appears in a subordinate or unrelated clause.

Measure proposal/capture rate, current-state accuracy, and stale-value retention.

### 9.3 Repeated rewriting loss

A canonical memory document is rewritten across many sessions.

Measure historical preservation, gradual detail loss, semantic broadening, and provenance retention.

### 9.4 Alias continuity

The same subject is referred to by full name, nickname, role, pronoun, and renamed project.

Measure whether one evolving state is reconstructed or fragmented.

### 9.5 Correction versus later change

Compare:

```text
Actually, I meant 37.
```

with:

```text
I had 20 then; now I have 37.
```

The first corrects prior evidence. The second changes world state.

### 9.6 Conflicting authority

A user statement conflicts with an agent-generated summary or inferred pattern.

Measure whether both evidence records survive while the proper authoritative View is selected.

### 9.7 History query versus current query

The same corpus should answer both:

```text
Where does Rachel live now?
Where did Rachel live before moving back?
```

A system that only overwrites canonical prose may answer the first while losing the second.

### 9.8 Retrieval pollution

Large archives contain repeated summaries, stale protocol text, and superseded statements.

Athena's archive exclusion is useful prior art. Measure stale candidates, duplicates, current-answer accuracy, and supporting evidence in top-k.

---

## 10. Recommended metrics

### Knowledge correctness

- current-state answer accuracy
- historical-state answer accuracy
- update capture rate
- correction classification accuracy
- contradiction detection accuracy
- entity continuity accuracy

### Evidence quality

- supporting source retrieved
- exact source-span availability
- evidence-authority correctness
- provenance completeness
- unsupported-answer rate

### Pipeline localization

- extraction miss
- consolidation miss
- identity/binding miss
- state-fold miss
- retrieval-ranking miss
- answer-synthesis miss

### Cost and stability

- write-time calls and tokens
- storage/index growth
- recall latency and tokens
- rewrite amplification
- variance across runs
- degradation over session count
- reproducibility after rebuilding derived state

### Human dependence

Track separately:

- required manual curation
- required explicit close command
- manual correction count
- hidden benchmark-specific tuning

This is not a usability score. It measures how much human work is required to preserve correctness.

---

## 11. What Menhir should borrow

### 11.1 A summary-RAG control arm

The highest-value borrow is experimental:

> implement a faithful model-curated-summary baseline so Menhir proves its complexity against a serious alternative.

Without this arm, Menhir risks demonstrating only that it beats raw vector search.

### 11.2 Progressive context tiers

Athena's lightweight, standard, and deep boot modes demonstrate:

```text
context depth should scale with task need
```

Menhir/Beacon can later express this through disclosure profiles and context budgets rather than static boot files. It is not required for the immediate benchmark.

### 11.3 Retrieval telemetry

Athena logs retrieval invocations and result classifications.

Menhir should keep every benchmark retrieval traceable:

- lanes admitting each candidate
- rank in each lane
- fused rank
- rerank/scoring changes
- suppressed candidates
- stale/current status
- evidence reachability
- final answer citations

### 11.4 Archive and channel-health guards

Athena's removal of stale GraphRAG and retired channels is an important lesson:

> a configured retrieval lane without valid backing data is worse than an absent lane.

Menhir should fail visibly when a lane is empty, stale, or silently bypassed.

### 11.5 Embedding cache and batch synchronization

Athena's SQLite embedding cache and batch-embedding path are pragmatic implementation prior art for benchmark repeatability and indexing cost.

### 11.6 Explicit epistemic-status labels

Athena distinguishes:

- code-enforced
- agent-discretion
- aspirational

Menhir docs should continue making equivalent distinctions:

- shipped invariant
- experimental arm
- proposed mechanism
- positioning claim

### 11.7 Git-readable derived projections

Athena demonstrates the practical value of a human-readable memory projection.

Menhir could later export current profiles, project state, decisions, belief history, and open contradictions as generated Markdown Views.

Those files should remain projections, not the canonical source of truth.

---

## 12. What Menhir should not copy directly

### 12.1 Do not make model-written summaries authoritative by default

A close summary is one probabilistic interpretation of the source session.

Store it as a derived artifact, candidate View, synopsis, or orientation aid—not unquestioned evidence.

### 12.2 Do not make `/end` discipline a correctness requirement

A memory system should degrade gracefully when a client disconnects, crashes, or never runs a close workflow.

Raw episodes must remain sufficient for later perception or repair.

### 12.3 Do not overwrite history to maintain canonical prose

Use:

```text
append-oriented assertions/events
-> deterministic fold
-> regenerated readable projection
```

### 12.4 Do not label RRF thresholds as calibrated confidence

RRF scores are rank-fusion values, not probabilities. `HIGH`, `MED`, and `LOW` are only defensible after calibration on held-out data.

### 12.5 Do not confuse Git provenance with claim provenance

Git can show who changed a file and when. It cannot by itself show the exact source statement, perceiver interpretation, entity binding, or authority decision behind a claim.

### 12.6 Do not keep a graph lane merely for positioning

Athena retired its stale graph channel. That is not evidence against graph memory; it is evidence that an unmaintained graph becomes retrieval debt.

Menhir's graph must justify itself through benchmarked identity, relationship, evidence, temporal, and structural gains.

---

## 13. Positioning implications

Athena already occupies public language around:

- local-first AI memory
- model portability
- owned personal context
- compounding personalization
- cognitive workspace / operating system
- governed agents
- Markdown memory

Menhir should not lead with those as unique claims.

A benchmark-supported Menhir position is narrower and stronger:

> Menhir is an evidence-backed knowledge substrate that turns raw agent/user episodes into identity-aware, temporally correct, rebuildable semantic state.

Athena can preserve what a model decided to write down.

Menhir should prove that it can determine:

- what was actually stated
- what changed
- what remains current
- what is historical
- who or what the statement was about
- which evidence supports the result
- whether the current View can be rebuilt and audited

---

## 14. Recommended next actions

### Immediate benchmark work

1. Add an Athena-style append-only summary-RAG arm.
2. Add an Athena-style canonical-rewrite variant.
3. Use the same close model, source sessions, and answer model across runs.
4. Persist all generated summaries and canonical revisions for failure analysis.
5. Score current state, history, provenance, identity, contradiction, and cost separately.

### Research discipline

6. Mark Markdown memory, session summarization, hybrid RAG, RRF, reranking, and portable model context as established prior art.
7. Keep usability and governance comparisons outside the present benchmark report.
8. Treat Athena's compounding-personalization claims as longitudinal N=1 evidence, not benchmarked proof.

### Later architecture/product work

9. Consider generated human-readable Markdown as a Menhir View/export.
10. Revisit progressive boot and task-shaped workflows only after the knowledge benchmark establishes the substrate's value.

---

## 15. Final classification

```text
Athena-Public
    strong prior art for:
        personal cognitive workspaces
        model-curated session memory
        Markdown ownership and portability
        hybrid chunk retrieval
        RRF + reranking
        retrieval telemetry
        prompt/workflow governance

    weak prior art for:
        source-grounded assertion identity
        entity continuity
        bitemporal proposition state
        deterministic state folds
        evidence-authority resolution
        contradiction-preserving current Views

Menhir benchmark role:
    serious summary-RAG baseline
    not the target architecture
```

The core lesson is:

> Menhir must prove that evidence-backed knowledge maintenance beats a disciplined model-summary and hybrid-RAG workflow on the cases where summaries silently omit, flatten, overwrite, fragment, or misclassify evolving knowledge.
