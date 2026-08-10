# Doc Drift Watch — MVP plan

## Status

MVP plan / strategic slice. This is **not a ladder rung yet**.

This plan captures the practical version of the Knowledge Supply Chain idea: automatically flag working docs when code, artifacts, or decisions they depend on are completed, changed, superseded, or invalidated.

The goal is not to solve all documentation drift. The goal is to make Menhir notice when a document that agents or humans may rely on is now probably stale.

---

## Problem

Menhir's research and planning docs evolve quickly. Today a doc can become wrong because:

```text
code changed
an artifact was superseded
a decision was reversed
a benchmark result changed
a fixture was hardened
a plan item was completed
a branch moved from prototype to production
```

But the doc itself remains visually unchanged. Future agents may retrieve it as if it were current.

This creates a specific failure mode:

> a stale working doc becomes stronger than current truth because it is well-written, nearby, and easy to retrieve.

Doc Drift Watch exists to interrupt that failure mode.

---

## MVP thesis

Start with a small deterministic loop:

```text
DocArtifact
  has anchors / dependencies

ChangeEvent
  touches anchors / artifacts / statuses

DocDriftWatch
  matches event -> doc dependencies
  emits DocReviewCandidate

ColdStartBrief / MemoryOracle
  surfaces: this doc may be stale because X changed
```

No LLM proposal is required for v0.

---

## Non-goals

```text
do not rewrite docs automatically
do not decide truth from docs alone
do not build full Knowledge Supply Chain yet
do not require a full organizational ontology
do not mutate trusted facts outside MemoryMutator / artifact service
do not block merges in v0
do not rely on LLM semantic guesses for drift detection
```

---

## Scope for MVP

### Document classes tracked

Track only working docs that are likely to affect future agents:

```text
.agent/plans/*.md
docs/research/*.md
docs/roadmap/*.md
.agent/benchmark-notes/*.md
.agent/verified-current-findings.md
```

Later expansion can include runbooks, ADRs, README files, support docs, and onboarding material.

### Dependency types

A doc may depend on:

```text
file path
symbol name
test name
artifact id
fixture name
benchmark command
branch name
rung id
commit sha
```

MVP can begin with explicit frontmatter or a lightweight dependency block in docs.

Example:

```yaml
---
menhir_doc:
  id: oracle-r4-r7-demo-run
  status: supported-by-spike
  depends_on:
    files:
      - src/menhir/services/scoring_service.py
      - src/menhir/domain/retrieval_tuning.py
    fixtures:
      - fixtures/oracle_hard.json
    rungs:
      - R4
      - R6
      - R7
    artifacts:
      - floor_fix
---
```

For docs without metadata, v0 may use a conservative fallback: path mentions and code-style tokens in the document body.

---

## Data model

### DocArtifact

A tracked document represented as a lightweight artifact or index row.

```python
DocArtifact:
    doc_id: str
    path: str
    title: str
    status: str                  # current | needs_review | possibly_stale | historical
    doc_kind: str                # plan | research | roadmap | benchmark_note | runbook | adr
    anchors: tuple[str, ...]     # files/symbols/tests/rungs/artifact ids
    last_verified_at: str | None
    verified_against: tuple[str, ...]  # commit shas, artifact ids, benchmark run ids
    owner: str | None
```

### ChangeEvent

A normalized event generated from Git, artifact transitions, benchmark runs, or manual commands.

```python
ChangeEvent:
    event_id: str
    kind: str                    # code_change | artifact_status_change | benchmark_change | plan_done
    source: str                  # git | artifact_service | bench | manual
    changed_refs: tuple[str, ...] # files, symbols, artifacts, fixture names, rungs
    summary: str
    commit_sha: str | None
    occurred_at: str
```

### DocReviewCandidate

A candidate review item, not a claim that the doc is definitely wrong.

```python
DocReviewCandidate:
    doc_id: str
    path: str
    reason: str
    event_id: str
    changed_refs: tuple[str, ...]
    severity: str                # low | medium | high
    status: str                  # open | acknowledged | verified_current | marked_historical
```

---

## Core invariants

```text
1. Drift Watch never rewrites a doc automatically.
2. Drift Watch emits review candidates, not truth claims.
3. A doc marked possibly_stale remains retrievable, but must be surfaced as possibly stale.
4. A verified-current action must record who/what verified it and against which commit/artifact/event.
5. Historical docs are preserved, not deleted.
6. LLM semantic matching can suggest doc dependencies later, but cannot be the only reason for a high-severity stale flag in v0.
7. ColdStartBrief must show stale/possibly-stale docs separately from trusted current context.
```

---

## MVP pipeline

### Step 1 — Explicit doc dependency parser

Parse frontmatter / dependency blocks from tracked docs.

Output:

```text
path -> doc_id, status, anchors, verified_against
```

Fallback for docs without metadata:

```text
extract code-like references:
  *.py, *.md, fixture names, R\d+ rungs, artifact ids if obvious
```

The fallback should be advisory only.

### Step 2 — ChangeEvent producer

Start with Git diff input:

```text
git diff --name-only <base>..<head>
```

Convert changed paths into `ChangeEvent(kind=code_change)`.

Second event source:

```text
ArtifactService status transitions:
  trusted -> historical
  candidate -> trusted
  superseded_by added
```

Third event source later:

```text
benchmark result changed
plan item marked done
```

### Step 3 — Deterministic matcher

Match events to docs when:

```text
changed file path exactly matches doc anchor
changed fixture matches doc anchor
changed artifact id matches doc anchor
changed rung id matches doc anchor
changed path appears in doc body and confidence is advisory
```

Severity rules:

```text
high:
  trusted/supported doc depends on changed file/artifact/fixture directly
  benchmark result changed for a doc that reports numbers

medium:
  plan/research doc mentions a changed file or artifact indirectly
  artifact superseded by a new artifact the doc does not mention

low:
  body-token fallback only
```

### Step 4 — Emit review candidates

Create `DocReviewCandidate` records.

Possible storage options:

```text
v0 option A: write to .agent/doc-review-candidates.jsonl
v0 option B: store as CANDIDATE artifacts through ArtifactService
v0 option C: both, JSONL first for easy review; artifacts after schema stabilizes
```

Recommendation: start with JSONL or a bench-local output, then promote to artifacts once the matching logic is proven.

### Step 5 — Surface in MemoryOracle / ColdStartBrief v0

When a task retrieves a doc or artifact, attach doc freshness warnings:

```text
possibly stale docs:
- docs/research/retrieval/oracle-runtime-interfaces.md
  reason: scoring_service.py changed after last verification
  event: code_change abc123
```

The warning should not suppress the doc; it should change how the agent treats it.

---

## First benchmark fixture

### Fixture: `doc_drift_watch_basic.json`

Corpus:

```text
Doc A: oracle-r4-r7-demo-run.md
  anchors: scoring_service.py, oracle_hard.json, R7
  status: verified_current

Doc B: l4-artifact-loop-v0.md
  anchors: artifact_repository.py, ArtifactService, Evidence
  status: verified_current

Doc C: old-handoff.md
  anchors: yawn.scheduler
  status: historical

Change 1:
  code_change: scoring_service.py changed

Change 2:
  artifact_status_change: old_floor superseded by floor_fix

Change 3:
  code_change: unrelated README.md
```

Expected:

```text
Change 1 flags Doc A high severity.
Change 2 flags docs depending on old_floor or floor policy medium/high depending directness.
Change 3 flags nothing.
Historical Doc C is not promoted; if surfaced, it remains historical.
```

Metrics:

```text
doc_drift_precision
stale_doc_flagged_rate
false_stale_rate
high_severity_accuracy
historical_preservation
brief_warning_fidelity
```

Pass condition for MVP:

```text
- all directly anchored changed docs are flagged;
- unrelated docs are not flagged;
- historical docs remain historical;
- every warning includes a concrete changed ref and event id;
- no doc content is modified automatically.
```

---

## Implementation slices

### Commit 1 — Plan + fixture only

Files:

```text
docs/roadmap/doc-drift-watch-mvp.md
archolith-bench/fixtures/doc_drift_watch_basic.json
```

Expectation:

```text
no production code
fixture describes docs, anchors, changes, expected flags
```

### Commit 2 — Bench-local detector

Files:

```text
archolith_bench/doc_drift/models.py
archolith_bench/doc_drift/detector.py
archolith_bench/doc_drift/metrics.py
scripts/run_doc_drift_bench.py
```

Expectation:

```text
run fixture and produce review candidates + metrics
```

### Commit 3 — Menhir doc dependency parser

Files:

```text
src/menhir/services/doc_dependency_parser.py
tests/test_doc_dependency_parser.py
```

Expectation:

```text
parse frontmatter/dependency block
fallback extracts only conservative code-like refs
```

### Commit 4 — Menhir drift detector service

Files:

```text
src/menhir/domain/doc_drift.py
src/menhir/services/doc_drift_watch.py
tests/test_doc_drift_watch.py
```

Expectation:

```text
ChangeEvent + DocArtifact -> DocReviewCandidate
no graph writes yet
```

### Commit 5 — Review candidate storage

Options:

```text
A. JSONL output under .agent/doc-review-candidates.jsonl
B. CANDIDATE artifacts via ArtifactService
```

Recommendation for first implementation: JSONL first, ArtifactService later.

### Commit 6 — ColdStartBrief / MemoryOracle warning seam

Files:

```text
src/menhir/services/memory_oracle_service.py
src/menhir/services/cold_start_brief_v0.py   # if present / when present
```

Expectation:

```text
retrieved docs/artifacts can carry possibly_stale warnings
warnings have evidence refs
```

---

## Open design questions

```text
1. Is a working doc itself an L4 artifact, or a separate DocArtifact index?
   Default: separate DocArtifact index first; later bridge to L4 artifact if useful.

2. Should drift candidates use ArtifactService immediately?
   Default: no. Keep v0 detector side-effect-light until precision is known.

3. How does a human mark verified_current?
   Default: explicit command/action that records verifier + commit/artifact/event.

4. Should docs block retrieval when stale?
   Default: no. Surface warning; do not suppress.

5. Can LLMs infer missing dependencies?
   Default: later, low-confidence suggestions only.
```

---

## Relationship to org-scale Menhir

Doc Drift Watch is the first practical slice of Knowledge Supply Chain.

It supports:

```text
Knowledge Supply Chain
Memory Contracts
Knowledge Coverage
Memory Debt
ColdStartBrief provenance
Organizational Memory Health
```

It also protects the current Menhir development process: branch docs, research notes, benchmark notes, and handoff docs can remain useful without silently becoming false-current context.

---

## Near-term recommendation

Do this before a full Organizational Beliefs system.

Reason:

```text
it has immediate local payoff
it is mostly deterministic
it reinforces evidence/provenance discipline
it reduces stale-doc risk for future agents
it gives ColdStartBrief a concrete warning surface
```

The smallest useful outcome is not automation. It is a visible warning:

> “This doc may be stale because the code/artifact/fixture it depends on changed after it was verified.”
