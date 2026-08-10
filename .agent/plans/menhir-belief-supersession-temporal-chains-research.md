# Menhir Research Task: Belief Supersession & Temporal Belief Chains

**Status: Research / Prototype — SAVED, NOT ACTIVE.** Not implemented, not scheduled. Do not begin
work from this document without explicit instruction; this file exists so the idea isn't lost.

**Provenance:** authored by the user working with Codex (2026-07-15), pasted into this Claude Code
session for safekeeping. Not written or edited by Claude — saved verbatim below.

**Cross-reference (added by Claude, 2026-07-15):** this plan's core example — a fact restated with
a different value ("I spent $350,000" → later "I spent $400,000") surfacing as two competing
memories instead of one resolved current belief — is strikingly close to a real, independently
found case from the same day's work: `.agent/reviews/rca-lme-stale-fact-retention-2026-07-15.md`
case `852ce960` (LME question about a mortgage pre-approval amount: $350,000 in session 0, restated
as $400,000 in session 1; menhir's graph retained only the $350,000 entity). That RCA also
root-caused *why*, at the code level: menhir's general conflict-resolution pipeline
(`scan_for_conflicts` → `run_llm_conflict_review` → `resolve_conflict`) is scheduler-driven
(`services/maintenance_scheduler.py`) and disabled under `MENHIR_BENCHMARK_MODE=1`
(`core/runtime.py:518-522`); the only ingest-time mechanism (`services/correction_resolver.py`) is
deliberately narrow — numeric-only, requires an explicit correction phrase, binds only to "counter
View" entities. Whoever picks up this research plan should read that RCA first — it's real,
current-system evidence for exactly the failure mode this plan sets out to research, including
which existing code paths are relevant (`correction_resolver.py`, `mcp/tools/conflict/*`,
`maintenance_scheduler.py`) and which ones are not yet built (a fast, ingest/session-time
supersession path, as opposed to the existing slow 14-day-floor background reconciliation).

---

## Original document (verbatim from here down)

## Status

**Research / Prototype**
Do **not** immediately implement this into the main memory pipeline.

Instead:

1. Audit the current Menhir codebase.
2. Determine what pieces already exist.
3. Design the final architecture.
4. Prototype the ranking behavior inside **Recall Labs**.
5. Only integrate after Recall Labs demonstrates measurable improvement on LongMemEval (and eventually coding workloads).

---

# Background

While evaluating LongMemEval we discovered a recurring failure mode:

The system retrieves multiple versions of the same fact.

Example:

> I spent $350,000.

Later:

> I spent $400,000.

Current retrieval frequently surfaces both memories with similar weight.

This causes confusion for ranking and ultimately hurts answer quality.

The problem is not duplicate detection.

The problem is **belief evolution**.

The memory graph currently stores historical events well, but retrieval needs to understand which belief represents the current state.

---

# Core Idea

Instead of ranking independent memories, Menhir should evolve toward ranking **beliefs**.

A belief may have multiple revisions over time.

Retrieval should normally surface:

* the newest valid belief
* the strongest supported belief
* the most relevant belief

while suppressing superseded beliefs unless history is explicitly requested.

---

# Research Goals

Investigate whether Menhir should introduce first-class support for:

* belief chains
* supersession
* refinement
* correction
* historical traversal

without losing provenance.

---

# Step 1 — Audit Existing Code

Determine what already exists.

Questions:

* Do we already have any supersession edges?
* Do Evidence nodes already support this?
* Does provenance already contain pieces we can reuse?
* Are temporal edges already sufficient?
* Is there existing consolidation logic that can be extended?

Produce a gap analysis rather than assuming new work.

---

# Step 2 — Current Pipeline Review

Document today's ingest pipeline.

For each stage determine:

Extraction

↓

Normalization

↓

Embedding

↓

Entity resolution

↓

Memory creation

↓

Graph linking

↓

Consolidation

Where would supersession naturally belong?

Possible candidates:

* extraction
* post-ingest
* consolidation
* retrieval
* hybrid

Do not assume the answer.

---

# Step 3 — Investigate Existing Memory Families

Rather than searching the entire graph every ingest, investigate whether memories can naturally be grouped into "belief families."

Possible grouping signals:

* entity overlap
* project
* document
* file
* repository
* semantic similarity
* graph locality
* shared provenance
* identical subject

The goal is reducing supersession detection to a very small candidate set.

We should **never** need O(N) comparisons against the entire memory graph.

---

# Proposed Pipeline

One likely direction:

```
New Memory

↓

Entity Extraction

↓

Candidate Retrieval
(small neighborhood)

↓

Supersession Classifier

↓

Create tentative links

↓

Background consolidation

↓

Resolved belief chain
```

Notice:

No global graph scan.

Only local candidate analysis.

---

# Candidate Retrieval Research

Investigate how candidates should be found.

Possible signals:

* embedding similarity
* shared entities
* graph distance
* identical repositories
* identical files
* identical users
* temporal locality

This stage should intentionally over-include.

The classifier should remove false positives.

---

# Supersession Classification

Investigate a dedicated classifier instead of treating this as similarity.

Potential outputs:

* SUPERSEDES
* REFINES
* CORRECTS
* DUPLICATE
* RELATED
* CONTRADICTS
* UNKNOWN

The classifier should determine relationships, not retrieval ranking.

---

# Tentative vs Final Links

Strong recommendation:

Do not immediately create permanent supersession edges.

Instead:

```
POSSIBLE_SUPERSEDES

↓

Background reasoning

↓

SUPERSEDES
```

This allows improved reasoning later without rewriting history.

---

# Belief Chains

Rather than isolated nodes:

```
350k

↓

375k

↓

400k
```

or

```
Version 1

↓

Version 2

↓

Version 3
```

Retrieval normally returns the head.

History queries return the chain.

---

# Temporal Reasoning

Previous research discussed partially ordered events.

Example:

```
A

before

B

before

C
```

No dates known.

Later:

```
B = Jan 15
```

Now:

```
A < Jan 15

C > Jan 15
```

Investigate whether belief chains should leverage the same temporal constraint propagation.

Avoid duplicating systems if temporal ordering already exists.

---

# Retrieval Behavior

Normal queries:

Return current belief.

Historical queries:

Traverse chain.

Questions involving corrections:

Show correction path.

Coding queries:

Prefer current design decisions.

Historical debugging:

Return evolution.

---

# Ingest vs Retrieval

Investigate the proper division of responsibility.

Current thinking:

## Ingest

* build candidate relationships
* create tentative supersession links
* preserve provenance

## Consolidation

* resolve chains
* repair chains
* merge duplicates
* compute ordering

## Retrieval

Interpret chain according to query intent.

Do **not** push all reasoning into either ingest or retrieval.

A hybrid architecture appears preferable.

Validate this assumption.

---

# Recall Labs Work

This research should first be implemented inside Recall Labs.

The purpose is experimentation.

Create interchangeable ranking pipelines.

Measure:

* retrieval quality
* LongMemEval
* latency
* candidate counts
* false supersession rate
* recall
* precision

No production implementation until measurable improvement exists.

---

# Belief Explorer

One potentially valuable Recall Labs feature:

## "Current Belief Explorer"

Given a topic:

Display

```
Current Belief

↓

Previous Belief

↓

Previous Belief

↓

Original
```

Include:

* why links exist
* classifier confidence
* timestamps (learned + valid if available)
* provenance
* evidence
* suppression status

Also display:

"What retrieval would return today."

This becomes a debugging interface for belief evolution.

---

# Oracle Interaction

Recent experiments showed the Evidence Oracle sometimes reduced retrieval quality because attached files received too much ranking weight.

This is likely because:

* provenance
* Git integration
* evidence scoring

are still incomplete.

Investigate whether oracle weighting should depend on subsystem maturity.

Until those systems mature, they should not dominate retrieval.

---

# Coding Agent Implications

This work is not only for LongMemEval.

It may be significantly more valuable for coding agents.

Code agents constantly retrieve outdated assumptions:

* old APIs
* previous architecture
* abandoned plans
* superseded implementations

Belief chains allow retrieval to answer:

"What is currently true?"

while still preserving:

"How did we get here?"

This may become one of Menhir's biggest differentiators.

---

# Success Criteria

The proposal should only move into Menhir if Recall Labs demonstrates measurable improvement.

Measure:

* LongMemEval score
* current-belief accuracy
* historical query accuracy
* coding-session startup quality
* retrieval latency
* graph growth
* consolidation cost

The architecture should optimize for long-term maintainability rather than short-term benchmark gains.

The goal is not simply better retrieval.

The goal is enabling Menhir to reason about **changing knowledge** instead of treating every memory as equally current.
