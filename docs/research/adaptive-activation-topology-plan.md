# Adaptive Activation Topology: Implementation Plan

> Status: speculative (implementation plan; no code surface yet — see `research-process.md`
> vocabulary). Promotion condition: the "Activation Trace Receipts v0" audit-only first slice lands.
> Related research: `docs/research/prior-art/fluxmem-connectivity-prior-art.md`
> Goal: let Menhir learn which graph connections help context construction without mutating evidence or canonical truth
> Proposed lane: adaptive activation topology

## 1. Summary

Menhir currently retrieves memories, applies stale/currentness logic, formats results, and builds context. The next evolution is to make the task-local connectivity used by recall observable and eventually adaptive.

The target loop is:

```text
query or task
-> candidate memories/evidence/concepts
-> activated task-local subgraph
-> context serialization
-> task feedback
-> activation feedback receipt
-> bounded routing improvement
```

This is not a plan to let task feedback rewrite historical memory.

The core invariant is:

```text
Adaptive connectivity may evolve.
Canonical evidence may not silently evolve with it.
```

The lane should be built in layers:

```text
1. observe activation
2. record feedback
3. learn bounded routing hints
4. evaluate against replay fixtures
5. induce procedural candidates
6. propose granularity changes
```

The first slice is audit-only:

```text
Activation Trace Receipts v0
```

It records what entered context, why it entered, and what was omitted. It does not alter retrieval, ranking, graph relationships, or memory content.

---

## 2. Problem statement

A recall system can fail even when all required information exists in storage.

Typical failures include:

```text
under-connection
    the system failed to activate required context

over-connection
    the system activated distracting or misleading context

granularity mismatch
    the selected unit was too broad or too narrow

warning omission
    a stale/safety warning was lost during formatting or budgeting

currentness failure
    older evidence displaced the current view
```

Today these failures are difficult to diagnose because the final context often does not preserve a complete explanation of how each item was selected, expanded, filtered, or omitted.

Menhir needs a task-local activation trace before it can safely learn from these failures.

---

## 3. Architectural separation

Adaptive activation must be separate from Menhir's evidence and canonical-view layers.

### 3.1 Evidence/event layer

Examples:

```text
FactEvent
ToolEvent
CodeChangeEvent
VerificationReceipt
DocumentationReviewReceipt
```

Properties:

```text
append-oriented
provenance-preserving
temporally scoped
not removed because a task found them distracting
```

### 3.2 Canonical/materialized view layer

Examples:

```text
current memory view
current documentation claim state
current code-symbol view
current procedural recommendation
```

Properties:

```text
deterministic fold/reconcile
supersedable
rebuildable where possible
currentness-aware
```

### 3.3 Activation layer

Examples:

```text
candidate set
activated nodes
expansion edges
budget omissions
formatter inclusions
context serialization order
```

Properties:

```text
task-specific
ephemeral or receipt-backed
safe to expand/prune
not a truth layer
```

### 3.4 Learned routing layer

Examples:

```text
this concept often helps this task family
this edge often introduces noise for this role
this evidence family is repeatedly missing with this query class
```

Properties:

```text
scoped
confidence-weighted
supersedable
bounded in influence
never authoritative by itself
```

### 3.5 Procedural layer

Examples:

```text
reusable investigation sequence
verified debugging procedure
stable task circuit
```

Properties:

```text
derived from episodes
versioned
maturity-scored
traceable to source evidence and feedback
```

---

## 4. Non-negotiable invariants

### 4.1 No evidence mutation

```text
Activation feedback must not rewrite, delete, or mark historical evidence false.
```

### 4.2 No silent canonical mutation

```text
Activation feedback must not directly supersede a memory or current-state view.
```

Canonical changes require the existing evidence/fold/reconcile or explicit review lifecycle.

### 4.3 Utility is not truth

```text
A useful memory may be false or stale.
A true memory may be irrelevant to a task.
```

Store utility feedback separately from support, currentness, and review status.

### 4.4 Mandatory warnings are atomic

```text
If a stale or safety-sensitive item enters context,
its required warning must enter with it.
```

A learned policy may not prune:

```text
stale warnings
contradiction warnings
scope warnings
review-required advisories
```

while retaining the associated content.

### 4.5 Scope learned behavior

Every learned activation hint must be scoped by the narrowest practical combination of:

```text
project
tenant/user
task family
role
query class
model/runtime family
```

### 4.6 Conservative failure handling

If activation tracing or feedback processing fails:

```text
normal recall continues
existing deterministic policies remain in force
no learned update is applied
```

### 4.7 No transcript or file-content capture by default

Receipts should contain IDs, reasons, scores, structured feedback, and bounded excerpts only when already permitted by the originating data model.

---

## 5. Terminology

### Activation session

One bounded recall/context-building operation associated with a task or query.

### Activation candidate

A node considered for inclusion before final policy, budget, and formatting decisions.

### Activated node

A node selected into the task-local subgraph.

### Activation edge

The reason/path connecting an activated node to the task or another activated node.

### Activation trace receipt

An audit record describing candidates, selected nodes, paths, warnings, and omissions.

### Activation feedback receipt

A later structured assessment indicating which parts of the activation helped, were missing, or interfered.

### Learned routing hint

A noncanonical, confidence-weighted suggestion used by future activation policies.

### Procedural circuit candidate

A proposed reusable task pattern derived from multiple successful activation sessions.

---

## 6. Proposed data shapes

## 6.1 Activation trace receipt

```json
{
  "activation_id": "act_01J...",
  "project": "menhir",
  "task_family": "code_question",
  "query_fingerprint": "sha256:...",
  "started_at": "2026-07-13T18:00:00Z",
  "completed_at": "2026-07-13T18:00:00.124Z",
  "policy_version": "recall-v3",
  "model_family": "local-qwen",
  "candidates": [
    {
      "node_id": "memory:m1",
      "node_type": "Memory",
      "source_family": "semantic",
      "candidate_score": 0.82,
      "candidate_reason": "hybrid_retrieval"
    }
  ],
  "activated_nodes": [
    {
      "node_id": "memory:m1",
      "node_type": "Memory",
      "included_in_context": true,
      "context_order": 2,
      "token_cost": 117,
      "required_warning_ids": ["warning:stale:m1"]
    }
  ],
  "activation_edges": [
    {
      "from": "task:current",
      "to": "memory:m1",
      "edge_type": "RETRIEVED_FOR",
      "reason": "semantic_and_lexical_match"
    },
    {
      "from": "memory:m1",
      "to": "warning:stale:m1",
      "edge_type": "REQUIRES_WARNING"
    }
  ],
  "omissions": [
    {
      "node_id": "memory:m8",
      "reason": "budget",
      "score": 0.41
    }
  ],
  "context_hash": "sha256:..."
}
```

The trace should not store the full generated prompt by default.

## 6.2 Activation feedback receipt

```json
{
  "feedback_id": "af_01J...",
  "activation_id": "act_01J...",
  "project": "menhir",
  "outcome": "partial_success",
  "recorded_at": "2026-07-13T18:01:00Z",
  "recorded_by": "agent",
  "feedback": [
    {
      "kind": "helpful",
      "node_id": "memory:m1",
      "basis": "used_in_final_answer"
    },
    {
      "kind": "over_connection",
      "node_id": "memory:m7",
      "basis": "irrelevant_to_current_branch"
    },
    {
      "kind": "under_connection",
      "missing_node_id": "evidence:e4",
      "basis": "required_after_tool_followup"
    }
  ]
}
```

Initial feedback kinds:

```text
helpful
unused
under_connection
over_connection
granularity_too_coarse
granularity_too_fine
stale_context_used
warning_missing
contradictory_context
unknown
```

## 6.3 Learned routing hint

```json
{
  "hint_id": "rh_01J...",
  "project": "menhir",
  "scope": {
    "task_family": "code_question",
    "role": "implementation_reviewer"
  },
  "from_selector": {
    "concept_id": "concept:stale-anchor-verification"
  },
  "to_selector": {
    "node_family": "CodeChangeEvent"
  },
  "effect": "boost_activation",
  "weight": 0.12,
  "confidence": 0.73,
  "support_count": 8,
  "contradiction_count": 1,
  "derived_from_feedback_ids": ["af_..."],
  "created_at": "2026-07-13T18:05:00Z",
  "expires_at": null
}
```

Routing hints must never target mandatory-warning suppression.

---

## 7. Storage recommendation

### Receipts

Activation and feedback receipts should be durable, append-oriented records.

Candidate graph representation:

```text
(:ActivationTrace)
(:ActivationFeedback)
(:LearnedRoutingHint)
```

Edges:

```text
(ActivationTrace)-[:ACTIVATED]->(Memory/Evidence/Concept/etc.)
(ActivationTrace)-[:OMITTED]->(node)
(ActivationFeedback)-[:EVALUATES]->(ActivationTrace)
(ActivationFeedback)-[:MARKS_HELPFUL]->(node)
(ActivationFeedback)-[:MARKS_DISTRACTOR]->(node)
(LearnedRoutingHint)-[:DERIVED_FROM]->(ActivationFeedback)
```

However, high-volume candidate-level details may be better stored as compact JSON properties or a sidecar table rather than one graph edge per candidate.

Recommended split:

```text
Neo4j / Menhir graph
    activation header
    feedback header
    links to durable graph entities
    learned hints

sidecar or compact JSON field
    full candidate list
    scores
    budget diagnostics
    serialization order
```

Do not make a sidecar mandatory for v0. Start with bounded receipt payloads and strict caps.

---

## 8. Proposed API surface

### Record/read activation traces

```text
POST /api/activation-traces
GET  /api/activation-traces/{activation_id}
GET  /api/activation-traces
```

The initial trace should generally be written internally by recall/context services rather than accepted from arbitrary clients.

### Record/read feedback

```text
POST /api/activation-feedback
GET  /api/activation-feedback/{feedback_id}
GET  /api/activation-feedback?activation_id=...
```

### Diagnostics

```text
GET /api/activation-diagnostics
```

Possible output:

```json
{
  "trace_count": 125,
  "feedback_count": 38,
  "feedback_coverage": 0.304,
  "under_connection_events": 7,
  "over_connection_events": 12,
  "warning_missing_events": 0,
  "average_activated_nodes": 8.4,
  "average_context_tokens": 2140
}
```

### Later routing controls

```text
GET  /api/activation-routing-hints
POST /api/activation-routing-hints/rebuild
```

Rebuild should be an explicit agent/operator operation until the policy is proven.

---

## 9. Security and tiering

Suggested tiers:

```text
GET traces/feedback/diagnostics
    readonly

POST structured feedback
    agent

rebuild or enable learned routing
    operator initially
```

Additional controls:

```text
project scoping required
cross-project activation links prohibited by default
bounded list limits
bounded candidate counts
no raw prompt storage by default
no credentials/tool secrets in feedback basis
```

---

## 10. Staged implementation packs

## Pack 0: Activation observability design lock

Goal:

```text
define stable receipt schemas and instrumentation points
```

Deliverables:

- typed internal activation trace model
- size and privacy caps
- policy/version fields
- clear candidate versus activated distinction
- mandatory-warning representation
- no persistence required yet

Non-goals:

```text
no ranking changes
no feedback
no learned edges
```

## Pack 1: Activation Trace Receipts v0

Goal:

```text
persist bounded audit traces for recall/context construction
```

Capture:

```text
activation ID
project/task family
candidate IDs and source families
selected node IDs
activation reasons/paths
budget omissions
required warnings
context hash and token estimate
policy version
phase timings
```

Behavior:

```text
best effort / fail open
no retrieval changes
no graph truth mutation
no full prompt storage
```

This is the recommended first implementation slice.

## Pack 2: Activation Feedback Receipts v0

Goal:

```text
record structured helpful/missing/distracting feedback against a trace
```

Feedback is audit-only.

No automated routing change.

## Pack 3: Activation Diagnostics Pack

Goal:

```text
measure failure modes before learning from them
```

Reports:

```text
feedback coverage
under/over-connection rates
warning-missing rate
candidate-to-context conversion
budget omission patterns
task-family breakdown
```

## Pack 4: Learned Routing Hints v0

Goal:

```text
derive bounded noncanonical hints from repeated feedback
```

Constraints:

```text
minimum support count
confidence threshold
small capped influence
project/task-family scope
explicit rebuild
fully reversible
no mandatory-warning suppression
```

Initially run in shadow mode:

```text
compute what ranking would change
record comparison
do not affect production context
```

## Pack 5: Controlled Activation Policy Experiment

Goal:

```text
enable learned hints for a narrow task family behind a flag
```

Compare:

```text
baseline deterministic policy
shadow learned policy
active learned policy
```

Rollback must be immediate.

## Pack 6: Procedural Circuit Candidates

Goal:

```text
identify repeated successful activation/episode patterns
and propose reusable procedural views
```

Candidates must retain:

```text
source activation IDs
source feedback IDs
supporting evidence
version lineage
maturity metrics
```

Promotion should require evaluation and optionally review.

## Pack 7: Granularity Proposals

Goal:

```text
propose split/merge/summary views when repeated feedback indicates mismatch
```

Do not rewrite source evidence.

Possible outputs:

```text
SplitMemoryProposal
SummaryViewProposal
ProceduralAbstractionProposal
```

---

## 11. Recommended first implementation slice

### Name

```text
Activation Trace Receipts v0
```

### Branch

```text
feat/activation-trace-receipts-v0
```

### PR title

```text
feat: add activation trace receipts v0
```

### Goal

Persist a bounded, best-effort audit record of how recall/context selected and serialized nodes.

### Required scope

- generate an `activation_id` per normal recall/context operation
- capture selected node IDs and source families
- capture deterministic reasons already available from recall phases
- capture stale/safety warning dependencies
- capture budget omissions when known
- capture policy version and timing metadata
- provide readonly trace lookup/list API
- provide diagnostics counts
- fail open if trace persistence fails

### Non-goals

```text
no ranking changes
no dynamic graph expansion changes
no feedback endpoint yet
no learned routing hints
no procedural consolidation
no memory content mutation
no evidence mutation
no prompt/transcript storage
no file-content capture
```

### Candidate files likely touched

Exact paths should be confirmed against current `main`, but likely seams include:

```text
recall service
context builder
formatter/context serialization path
repository/storage adapter
API routes/models
new activation trace domain model
new targeted test module
maintenance/diagnostic script or endpoint
```

Avoid central adapter-protocol changes unless necessary. Prefer an additive optional capability with fail-open behavior.

---

## 12. Trace construction strategy

Do not attempt to reconstruct the entire internal graph after the fact.

Instrument the existing phases as they run:

```text
candidate generation
-> policy filtering
-> stale/currentness enrichment
-> graph/path expansion
-> formatter transformation
-> budget selection
-> final context serialization
```

Each phase appends bounded structured observations to one trace builder.

Pseudo-interface:

```python
class ActivationTraceBuilder:
    def add_candidate(self, node_id: str, family: str, reason: str, score: float | None) -> None: ...
    def add_activation(self, node_id: str, reason: str, path: list[str] | None = None) -> None: ...
    def add_required_warning(self, node_id: str, warning_id: str) -> None: ...
    def add_omission(self, node_id: str, reason: str) -> None: ...
    def finish(self, *, context_hash: str, token_count: int | None) -> ActivationTrace: ...
```

The builder must enforce caps:

```text
maximum candidates
maximum activated nodes
maximum path length
maximum reason length
maximum omission records
```

If caps are exceeded, record aggregate truncation counters.

---

## 13. Mandatory warning modeling

Warnings should be represented as dependencies, not merely strings added late in formatting.

Example:

```text
Memory m1
-[:REQUIRES_CONTEXT_COMPANION]->
StaleWarning w1
```

The trace should record whether both were serialized.

Diagnostic invariant:

```text
included(memory m1) AND requires_warning(m1, w1)
=> included(warning w1)
```

A violation should produce:

```text
warning_missing
```

This is an error-level diagnostic even before adaptive learning exists.

---

## 14. Testing strategy

## 14.1 Pack 1 unit tests

Test:

```text
trace generated for ordinary recall
activated IDs recorded
source family recorded
policy version recorded
stale warning dependency recorded
non-stale result has no stale warning dependency
budget omission recorded
trace caps truncate safely
persistence failure does not fail recall
trace lookup respects project scope
list limit bounded and non-negative
```

## 14.2 Context invariants

Test:

```text
stale memory and warning remain atomic
trace reports both as serialized
warning omission produces explicit diagnostic
```

## 14.3 Privacy tests

Test that traces do not contain:

```text
full user prompt
full generated response
full transcript
raw file contents
bearer tokens or credentials
```

## 14.4 Storage tests

Test:

```text
append-only trace creation
unique activation IDs
stable timestamp parsing
project filtering
bounded payload size
```

## 14.5 Suggested targeted test command

```bash
pytest tests/test_activation_traces.py -q
pytest tests -q -k "activation or recall or context or formatter or stale"
```

Full suite is not required for an additive trace pack unless central runtime/storage protocols change.

---

## 15. Replay and benchmark plan

Before enabling learned routing, create a replay fixture set.

Each fixture should include:

```text
query/task description
available graph nodes
expected required nodes
known distractors
mandatory warnings
budget
baseline context
outcome labels
```

Fixture classes:

```text
missing current evidence
stale memory with mandatory warning
contradictory memories
same concept across multiple projects
one useful and several distracting neighboring nodes
broad document retrieved for a narrow claim
procedural memory helpful only for one task family
```

Metrics:

```text
required-node recall
context precision
distractor count
warning compliance
currentness compliance
token cost
task success where executable
```

Shadow learned policies should be evaluated on these fixtures before activation.

---

## 16. Learning policy constraints

When Pack 4 is reached, learned hints should influence a bounded secondary term rather than replace deterministic ranking.

Conceptual formula:

```text
final_score = deterministic_score + capped_activation_hint
```

Constraints:

```text
abs(capped_activation_hint) <= configured small cap
currentness and stale policy remain hard constraints
family contribution caps remain active
mandatory warnings bypass learned pruning
invalid/expired hints are ignored
```

A hint should need repeated supporting feedback:

```text
support_count >= N
confidence >= threshold
contradiction ratio <= threshold
```

Negative evidence should reduce or supersede the hint rather than mutate history.

---

## 17. Procedural maturity

A future procedural circuit should not become authoritative after one successful trace.

Candidate maturity signals:

```text
number of successful source episodes
diversity of tasks/projects where allowed
failure rate
version stability
review confirmations
age/currentness of supporting evidence
replay performance against source fixtures
```

Procedural circuits should be:

```text
versioned
supersedable
disableable
traceable to source activations
```

Do not store a procedure as established truth merely because it improves reward.

---

## 18. Relationship to existing Menhir lanes

### Hook Center

Provides change events and environmental signals that may influence activation.

### Stale-anchor lane

Provides mandatory warning/currentness constraints that learned activation may not override.

### Verification receipts

Provide explicit audit evidence that can inform utility/currentness separately.

### Painscan

Can produce candidate under/over-connection feedback, but should emit structured receipts rather than directly changing routing.

### Evidence / FactEvent lane

Remains the source of support and provenance. Activation utility does not alter evidence truth.

### Document graphlets

Provide bounded semantic regions that can be activated at claim or section granularity.

### Oracle-amplified retrieval

The adaptive hint term should compose with existing routing/combiner work rather than replace it. Per-family caps and currentness protections remain important.

---

## 19. Open questions

1. Should activation traces be sampled in production or recorded for every call?
2. Which trace fields are cheap enough to capture without measurable latency?
3. Should candidate-level details live in Neo4j, compact JSON, or a sidecar store?
4. What is the minimal reliable task-family classifier?
5. Which outcomes can be inferred automatically versus requiring explicit feedback?
6. How should feedback from different models be combined?
7. How quickly should learned hints decay when not reinforced?
8. How should contradictory utility feedback be reconciled?
9. Can procedural circuits be evaluated without storing sensitive trajectories?
10. Which activation paths are sufficiently stable to expose through MCP/API?

---

## 20. Decision

Proceed in this order:

```text
Activation Trace Receipts v0
-> Activation Feedback Receipts v0
-> Activation Diagnostics
-> shadow Learned Routing Hints
-> narrow controlled activation experiment
-> procedural circuit candidates
-> granularity proposals
```

Do not begin with automatic rewiring.

The first milestone is observability, not intelligence.

Definition of done for the first slice:

```text
For a normal recall/context operation, Menhir can produce a bounded,
project-scoped, privacy-safe receipt explaining which graph entities entered
context, why they entered, what mandatory warnings accompanied them, and what
was omitted—without changing the resulting context or mutating memory.
```
