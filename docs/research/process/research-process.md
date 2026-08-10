# menhir research process

## Status

canonical

## Purpose

This document defines how we turn frontier ideas into research-quality work for menhir and the broader Archolith ecosystem.

The goal is not to pretend an LLM is a researcher with perfect knowledge. The goal is to use LLMs as research copilots while keeping the source of truth grounded in fresh sources, reproducible experiments, and honest claims.

The core loop is:

```text
fresh sources -> source cards -> claim ledger -> design hypothesis -> small spike -> eval harness -> research note -> technical report
```

LLMs may propose, synthesize, critique, and draft. They do not get to be the source of truth.

## Repository roles

### menhir

`menhir` holds the system implementation and research notes tied directly to memory architecture.

Use `menhir` for:

```text
research notes
architecture docs
design decisions
issue tracking
small code spikes
integration points
system behavior notes
```

Recommended paths:

```text
docs/research/*.md
docs/architecture/*.md
src/menhir/*
tests/*
```

### archolith-bench

`archolith-bench` is the preferred home for reproducible research harnesses and benchmark artifacts.

Use `archolith-bench` for:

```text
evaluation harnesses
fixtures
synthetic and real-world task suites
baseline runners
experiment configs
run outputs
metric calculators
paper/report tables
plots
ablation scripts
```

Recommended shape:

```text
archolith-bench/
  README.md
  pyproject.toml
  benchmarks/
    chronostratum/
    belief_circuit/
    temporal_blast_radius/
  fixtures/
    ce_willow/
    out_of_order_memory/
    retroactive_correction/
  baselines/
    embedding_only/
    graph_recall/
    graph_temporal/
    graph_temporal_belief/
  results/
    YYYY-MM-DD-run-name/
      config.yaml
      metrics.json
      outputs.jsonl
      notes.md
  reports/
    belief-circuit-eval-001.md
```

Rule of thumb:

```text
menhir explains and implements the idea.
archolith-bench tests whether the idea actually works.
```

## Research north star

menhir research should improve at least one of these capabilities:

```text
temporal recall
belief drift handling
contradiction handling
structure-aware recall
temporal blast radius
evidence attribution
uncertainty / calibration
agent debugging continuity
```

If a paper, technique, or implementation idea does not improve one of these, it can stay in notes but should not affect the product roadmap.

## Research principles

### 1. Small claims, honestly tested

Bad research claim:

```text
menhir has better memory.
```

Good research claim:

```text
Belief-aware recall packets reduce stale or superseded fact assertions on debugging-memory fixtures compared with graph recall alone.
```

A research claim must have:

```text
claim
baseline
intervention
fixture or dataset
metric
failure analysis
```

### 2. LLMs synthesize; sources verify; evals decide

Use LLMs for:

```text
summarizing papers
identifying connections
turning notes into hypotheses
designing fixtures
writing first-pass code
drafting research docs
playing skeptic
```

Do not use LLMs as final authority for:

```text
latest paper status
novelty claims
paper citations
benchmark numbers
library capabilities
implementation correctness
statistical significance
```

### 3. Every important claim gets a source card

If a claim affects architecture or research direction, it needs a source card or an experiment result.

Example:

```yaml
id: source:pc-kg-completion-2025
kind: paper
title: Probabilistic Circuits for Knowledge Graph Completion with Reduced Rule Sets
url: https://arxiv.org/abs/2508.06706
claim_used: Probabilistic circuits can model rule contexts for knowledge-graph completion.
menhir_relevance: May map to rule/evidence contexts for debugging belief inference.
confidence: medium
status: read-abstract-and-method-summary
risk: KG completion is not the same as long-term agent memory.
action: Use as motivation for BeliefCircuit experiments, not as proof.
```

### 4. Research notes are allowed to be wrong if they are versioned

Early notes may contain hypotheses, weak evidence, and speculative mappings. That is fine if they are labeled.

Use explicit status labels:

```text
speculative
supported-by-paper
supported-by-code-spike
supported-by-eval
rejected
superseded
```

### 5. Code spikes must be disposable

A spike exists to answer a question, not to become permanent architecture automatically.

Spike rules:

```text
one question
one branch/PR
small fixture
small test
clear success/failure condition
no heavy dependency unless the spike is explicitly about that dependency
```

## Source tiers

### Tier 1: Primary technical sources

Use these for claims about what a system, library, paper, or API actually says.

```text
peer-reviewed papers
arXiv/preprint PDFs
official docs
official API docs
official GitHub repositories
release notes
source code
benchmark datasets
```

### Tier 2: Discovery and citation graph sources

Use these to find papers and related work, not as final proof.

```text
Semantic Scholar
DBLP
OpenReview
Papers With Code
Crossref
Google Scholar / Scholar-like search
conference proceedings pages
```

### Tier 3: Community and signal sources

Use these to detect momentum, implementation pain, or adoption. Do not cite them as scientific proof unless the claim is about community behavior.

```text
GitHub issues and discussions
Hacker News
Reddit
Discord/forum notes
blog posts
library examples
benchmarks in README files
```

### Tier 4: LLM summaries

Use these only as drafts or search guides. Never cite an LLM summary as evidence.

## Source registry

### arXiv

Use for frontier preprints and fast-moving research areas.

Official API/docs:

```text
https://info.arxiv.org/help/api/index.html
https://info.arxiv.org/help/api/user-manual.html
```

Recommended use:

```text
fresh paper discovery
query packs by topic
paper metadata
abstracts
PDF links
version tracking
```

Notes:

```text
arXiv offers public API access and asks users to follow API terms, API basics, and the user manual.
Acknowledge arXiv data usage when using it in a product or public output.
```

### Semantic Scholar

Use for citation graph expansion, related papers, authors, venues, abstracts, and influential-citation discovery.

Official API/docs:

```text
https://api.semanticscholar.org/api-docs/graph
https://api.semanticscholar.org/api-docs/recommendations
https://api.semanticscholar.org/api-docs/datasets
```

Recommended use:

```text
find papers citing/related to a seed paper
pull abstract, venue, year, authors
collect citation counts as signal, not proof
build related-work maps
```

### OpenReview

Use for current ML conference submissions, reviews, decisions, and discussion context.

Official client/docs:

```text
https://openreview-py.readthedocs.io/en/latest/
https://docs.openreview.net/
```

Recommended use:

```text
ICLR / NeurIPS / other venue discovery
review context
paper status
public discussion when available
```

Notes:

```text
OpenReview data can be noisy before final decisions.
Reviews are evidence about reception, not ground truth.
```

### Crossref

Use for DOI metadata, publication metadata, corrections/retractions signals, and bibliographic normalization.

Official docs/repo:

```text
https://github.com/CrossRef/rest-api-doc
https://api.crossref.org/
```

Recommended use:

```text
DOI lookup
publication metadata
journal/conference metadata
retraction/correction checks when available
```

### DBLP

Use for computer-science bibliography normalization.

Recommended use:

```text
author disambiguation
venue/year metadata
publication lists
conference proceedings lookup
```

### Papers With Code

Use for implementation and benchmark leads.

Recommended use:

```text
code availability
benchmark/dataset leads
model/task taxonomy
```

Caution:

```text
Papers With Code is useful for discovery, but benchmark claims should be verified against the paper and repository.
```

### GitHub

Use for source-code truth and implementation maturity.

Recommended use:

```text
library APIs
release notes
issues
stars/forks as weak adoption signal
commits and tags
license checks
maintenance activity
```

Caution:

```text
README claims are marketing until verified by code, tests, examples, or issues.
```

### Project-local sources

Use menhir and archolith-bench as primary sources for our own behavior.

```text
repo code
issues
PRs
docs/research/*.md
eval fixtures
test outputs
trace logs
Graphiti/Neo4j data snapshots
SQLite audit sidecar
archolith-bench run outputs
```

These are primary sources for our own system behavior.

## Research lanes for menhir

### Chronostratum

Research question:

```text
Can temporal metadata, belief-time/history projection, and ingestion-order independence improve long-term agent memory?
```

Key capabilities:

```text
valid-time vs learned-time distinction
current belief vs historical belief
out-of-order insertion
retroactive correction
interval/partial-order memory
```

### Git/Structure Time Join

Research question:

```text
Can code structure, Git history, and memory history be joined to answer debugging questions that generic memory systems cannot?
```

Killer query:

```text
Test C failed. What does it depend on, and which dependencies changed between when it last passed and now?
```

Key capability:

```text
blast radius x time
```

### BeliefCircuit

Research question:

```text
Can belief-aware recall packets reduce stale, superseded, or unsupported assertions in long-term coding-agent memory?
```

Key capabilities:

```text
Relevant(memory, query)
Current(fact, as_of_time)
Supported(explanation, evidence_set)
Superseded(old_belief, later_evidence)
```

Output policy buckets:

```text
safe_to_assert
mention_with_uncertainty
conflict_set
do_not_assert
```

### Frontier connected-data substrates

Research question:

```text
What representations preserve multi-way, temporal, uncertain, provenance-rich relationships better than binary graph edges, and which help with code-memory reasoning?
```

Candidate lanes:

```text
hypergraphs / hyperevents
Datalog / differential dataflow
semiring provenance
cellular sheaves
tensor/factorized representations
probabilistic circuits
category-theoretic schema integration
```

## Research workflow

### Step 0: Define the research question

Every research item starts with a question, not a technology.

Template:

```yaml
question: "Can X improve Y compared with Z?"
why_it_matters: "..."
menhir_capability: "temporal recall | belief drift | temporal blast radius | ..."
expected_artifact: "note | issue | spike | eval | report"
```

Example:

```yaml
question: "Can BeliefCircuit recall packets reduce stale fact assertion compared with graph recall alone?"
why_it_matters: "Agent memory currently risks asserting old or superseded beliefs as current truth."
menhir_capability: "belief drift handling"
expected_artifact: "archolith-bench fixture + menhir research note"
```

### Step 1: Build query packs

Do not use one broad search. Use focused query packs by lane.

Example query pack for BeliefCircuit:

```text
probabilistic circuits knowledge graph completion
probabilistic circuits missing data
sum-product networks retrieval augmented generation
uncertainty-aware knowledge graph retrieval LLM
trustworthy RAG uncertainty provenance
probabilistic logic programming memory
ProbLog knowledge graph reasoning
```

Example query pack for temporal blast radius:

```text
temporal code change impact analysis
software regression localization git history dependency graph
program slicing change impact analysis tests
fault localization commit history dependency graph
software repository mining bug localization
```

Example query pack for connected-data alternatives:

```text
knowledge hypergraph n-ary relations
cellular sheaf neural networks heterogeneous data
semiring provenance Datalog
incremental Datalog differential dataflow
factorized databases recursive queries
```

### Step 2: Collect source cards

Each source gets a card.

```yaml
id: source:<short-id>
type: paper | docs | code | dataset | blog | issue
title:
authors:
year:
venue_or_source:
url:
doi_or_arxiv:
code_url:
status: unread | skimmed | read | reproduced | rejected
main_claims:
  - claim:
    evidence_location:
    confidence: low | medium | high
menhir_relevance:
limitations:
follow_up:
```

Store source cards either in a research note or in archolith-bench if they are attached to an eval.

### Step 3: Make a claim ledger

A claim ledger prevents accidental laundering of weak evidence into strong claims.

```yaml
claim_id: claim:belief-packets-reduce-stale-assertions
claim: "Belief-aware recall packets reduce stale/superseded assertion."
status: speculative | supported-by-paper | supported-by-spike | supported-by-eval | rejected
sources:
  - source:uncertainty-rag-survey
  - source:pc-kg-completion-2025
experiments:
  - bench:belief-circuit-eval-001
risks:
  - "Fixtures may be too synthetic."
  - "Baseline may be weak."
next_action: "Run against 20 debugging-memory fixtures."
```

### Step 4: Write a hypothesis before coding

Every spike should have a hypothesis.

```yaml
hypothesis: "Adding do_not_assert and conflict_set buckets reduces stale current-truth assertions."
baseline: "graph recall + recency"
intervention: "graph recall + BeliefCircuit recall packet"
fixtures:
  - ce_willow_belief_drift
  - out_of_order_insert
  - retroactive_correction
metrics:
  - stale_assertion_rate
  - belief_drift_accuracy
  - evidence_attribution_accuracy
  - latency_ms
success_threshold: "At least 30% reduction in stale assertions without >15% relevance loss."
```

### Step 5: Put evals in archolith-bench

Research-quality claims need reproducible evaluation artifacts.

Minimum fixture format:

```yaml
id: ce_willow_belief_drift
capability: belief_drift
input_events:
  - id: E1
    text: "Original CE willow texture-cache crash observed."
    valid_at: "2026-06-23T10:00:00-05:00"
  - id: E2
    text: "CE willow patch added."
    valid_at: "2026-06-23T12:00:00-05:00"
  - id: E3
    text: "Crash appears resolved."
    valid_at: "2026-06-23T13:00:00-05:00"
  - id: E4
    text: "Compatibility/load-order issue appears."
    valid_at: "2026-06-24T10:00:00-05:00"
  - id: E5
    text: "Load-order fix resolves the remaining issue."
    valid_at: "2026-06-24T14:00:00-05:00"
query: "What broke after I added the CE willow patch, and what did we believe before the load-order fix?"
expected:
  safe_to_assert:
    - "Load order caused or contributed to the remaining compatibility issue."
  mention_with_uncertainty:
    - "The patch may have addressed the original texture-cache symptom."
  do_not_assert:
    - "The patch fully fixed all issues."
  evidence_should_include:
    - E2
    - E4
    - E5
```

Minimum result format:

```json
{
  "fixture_id": "ce_willow_belief_drift",
  "runner": "graph_temporal_belief",
  "commit": "...",
  "timestamp": "...",
  "metrics": {
    "stale_assertion_rate": 0.0,
    "belief_drift_accuracy": 1.0,
    "evidence_attribution_accuracy": 0.67,
    "latency_ms": 185
  },
  "output": {
    "safe_to_assert": [],
    "mention_with_uncertainty": [],
    "conflict_set": [],
    "do_not_assert": []
  }
}
```

### Step 6: Compare against baselines

A research result needs a comparison.

Common baselines:

```text
embedding-only recall
BM25-only recall
graph recall without temporal fields
graph recall + recency
graph recall + temporal fields
graph recall + BeliefCircuit buckets
graph recall + Git/Structure Time Join
```

Do not claim improvement unless the intervention beats at least one honest baseline.

### Step 7: Run the skeptic pass

Before turning results into a research doc, ask:

```text
Is the fixture too easy?
Is the baseline unfair?
Did we leak the answer into the prompt?
Did the metric reward formatting instead of correctness?
Would a simpler heuristic do the same thing?
Is the result stable over multiple phrasings?
What failed?
```

Every report should include failure cases.

### Step 8: Write the research note

Research note template:

```md
# Title

## Status

speculative | supported-by-spike | supported-by-eval | rejected | superseded

## Question

## Motivation

## Related work

## Hypothesis

## Method

## Baselines

## Fixtures / dataset

## Metrics

## Results

## Failure cases

## Interpretation

## What this does not prove

## Next steps
```

### Step 9: Decide whether it graduates

A research direction graduates only if it has:

```text
working spike
fixtures
baseline comparison
failure analysis
clear system implication
```

Graduation options:

```text
merge into menhir
keep as bench-only experiment
write technical report
reject/supersede
collect more data
```

## LLM roles in the process

Use separate prompts/agents for separate roles.

### Scout

Goal:

```text
Find recent sources and build source cards.
```

Instructions:

```text
Search recent papers and official docs.
Prefer primary sources.
Return source cards, not conclusions.
Flag weak or secondary sources.
```

### Librarian

Goal:

```text
Deduplicate and normalize sources.
```

Instructions:

```text
Merge duplicate papers.
Normalize titles, authors, years, URLs, arXiv IDs, DOIs, code links.
Separate peer-reviewed, preprint, docs, code, and blog sources.
```

### Skeptic

Goal:

```text
Attack the claim.
```

Instructions:

```text
Find missing baselines, weak assumptions, overclaims, likely confounders, and simpler alternatives.
```

### Architect

Goal:

```text
Translate research into menhir architecture.
```

Instructions:

```text
Map the idea to menhir components.
Identify the smallest useful spike.
Avoid heavy dependencies unless justified.
```

### Evaluator

Goal:

```text
Design tests that could falsify the idea.
```

Instructions:

```text
Create fixtures, metrics, expected outputs, and failure cases.
Prefer small repeatable tests over broad vibes.
```

### Writer

Goal:

```text
Turn results into a durable research note.
```

Instructions:

```text
Do not hide failures.
Separate speculation from demonstrated results.
Use citations/source cards for factual claims.
```

## Common failure modes

### LLM citation laundering

Problem:

```text
An LLM summary sounds authoritative, then becomes a cited claim in our docs.
```

Mitigation:

```text
No claim enters architecture docs without a source card or experiment result.
```

### Novelty overclaiming

Problem:

```text
We think an idea is novel because we have not seen it before.
```

Mitigation:

```text
Search related work before using novelty language.
Prefer "differentiated for our use case" over "novel" until proven.
```

### Benchmark theater

Problem:

```text
The eval rewards a system for matching our wording rather than solving the task.
```

Mitigation:

```text
Use multiple phrasings, hidden expected facts, and failure-case fixtures.
```

### Toy fixture overfitting

Problem:

```text
The system passes CE willow but fails other debugging histories.
```

Mitigation:

```text
Create fixture families: out-of-order insertion, retroactive correction, interval reasoning, stale belief, conflicting memory, temporal blast radius.
```

### Baseline weakness

Problem:

```text
We compare against a strawman.
```

Mitigation:

```text
Always include at least one strong simple baseline, such as graph recall + recency or a hand-tuned heuristic.
```

### Hidden implementation leakage

Problem:

```text
The expected answer leaks into the prompt, memory names, or fixture labels.
```

Mitigation:

```text
Separate fixture metadata from model-visible input.
```

### Research drift

Problem:

```text
We read many papers and lose the concrete menhir use case.
```

Mitigation:

```text
Every research note must say which menhir capability it improves.
```

### Premature dependency adoption

Problem:

```text
We add a heavy library before proving the concept.
```

Mitigation:

```text
Prototype with transparent local abstractions first. Add dependencies only when the eval requires them.
```

## Metrics catalog

### Recall and retrieval

```text
relevance precision
relevance recall
MRR / nDCG for candidate ranking
context usefulness score
```

### Temporal memory

```text
current-fact accuracy
historical-fact accuracy
valid-time ordering accuracy
learned-time ordering accuracy
out-of-order insertion robustness
retroactive correction accuracy
```

### Belief drift

```text
stale assertion rate
superseded belief detection
belief drift accuracy
do_not_assert precision
do_not_assert recall
conflict-set precision
conflict-set recall
```

### Temporal blast radius

```text
changed-in-cone precision
changed-in-cone recall
suspect ranking MRR
affected-test recall
commit-window accuracy
```

### Calibration

```text
Brier score
expected calibration error
abstention quality
confidence-vs-correctness curve
```

### Explainability

```text
evidence attribution accuracy
source provenance correctness
rationale faithfulness
missing-evidence disclosure
```

### Engineering

```text
latency
index/update cost
memory footprint
token cost
failure rate
run reproducibility
```

## Artifact types

### Research issue

Use for:

```text
tracking a question
collecting sources
listing hypotheses
planning spikes/evals
```

### Research note

Use for:

```text
stable design thinking
source summaries
architectural implications
experiment interpretation
```

### Code spike PR

Use for:

```text
small domain model
small integration path
small testable behavior
```

### Benchmark fixture

Use for:

```text
reproducible task input and expected output
```

### Benchmark report

Use for:

```text
run results
baseline comparison
metric tables
failure analysis
```

### Technical report

Use for:

```text
paper-shaped synthesis after multiple experiments
```

## Publication ladder

Research can mature through levels.

### Level 1: Internal research note

```text
question + sources + design hypothesis
```

### Level 2: Reproducible eval

```text
fixtures + baselines + metrics + run outputs in archolith-bench
```

### Level 3: Technical report

```text
paper-shaped markdown with methods/results/failures
```

### Level 4: Public preprint or workshop paper

```text
only after the result survives baselines and skeptic review
```

## Current immediate process for BeliefCircuit

1. Keep `docs/research/probabilistic-belief-layer.md` as the living design note.
2. Keep PR #10 as a code spike, not final architecture.
3. Add archolith-bench fixtures for:
   - CE willow belief drift
   - out-of-order insertion
   - retroactive correction
   - stale preference update
   - conflicting code explanation
4. Compare:
   - graph recall only
   - graph recall + temporal metadata
   - graph recall + BeliefCircuit buckets
5. Measure:
   - stale assertion rate
   - belief drift accuracy
   - do_not_assert precision/recall
   - evidence attribution accuracy
6. Write `reports/belief-circuit-eval-001.md` in archolith-bench.
7. Copy conclusions back into menhir docs only after the eval exists.

## Current immediate process for Chronostratum

1. Surface Graphiti temporal fields in recall:
   - `valid_at`
   - `invalid_at`
   - `created_at`
   - `expired_at`
2. Add fixtures for out-of-order insertion and retroactive correction.
3. Compare recall before/after temporal fields.
4. Measure current/historical belief accuracy.
5. Write `reports/chronostratum-temporal-recall-001.md` in archolith-bench.

## Current immediate process for temporal blast radius

1. Build Git/Structure Time Join fixtures.
2. Use at least one real debugging trace and one synthetic trace.
3. Compare:
   - dependency cone only
   - Git diff only
   - dependency cone ∩ Git diff
   - dependency cone ∩ Git diff + memory/belief state
4. Measure suspect ranking and changed-in-cone accuracy.
5. Write `reports/temporal-blast-radius-001.md` in archolith-bench.

## Final rule

Research is not what we believe. Research is what survives contact with sources, baselines, and reproducible tests.

For menhir, the shortest path to research-quality work is:

```text
real debugging pain
-> small formal fixture
-> honest baseline comparison
-> documented failure analysis
-> repeatable bench harness
-> research note
```
