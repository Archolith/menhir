# Belief layer for menhir

## Status

active

> **2026-07-11 status update.** The R3 belief-bucket + currentness ladder is **built and bench-graduated**
> on the demo/fixture families (archolith-bench `r3/`: D currentness — stale-current 0.60 -> 0.00 with
> zero historical loss; C/D/E/F + 7 real fixtures landed). Code: `domain/belief.py`, `domain/warden.py`
> (`CurrentnessWarden`), `domain/git_staleness.py`, `domain/temporal.py`. The belief-gate is wired but
> ships **default-off** (`frontier_belief_gate`, requires `frontier_warden_gate`). Caveat: on the harder
> LongMemEval the aggregate read-side stack was neutral-to-negative, but R3's specific mechanism is
> **essential for confidently-held stale beliefs** (the refactor case, where plain node retrieval is
> useless). So the promotion condition is met on the R3 fixtures; graduation of the whole doc to
> `supported-by-eval` awaits a belief-family win on a shipped, default-on path.

## Promotion condition

This doc graduates from active design to supported-by-eval when archolith-bench contains at least one fixture family showing that belief-aware recall/breaker policies reduce stale current assertions or poisoned context injection against honest baselines.

## Purpose

This document is the single owner for BeliefCircuit, belief-aware recall packets, probabilistic breaker vocabulary, and the immune-inspired anergy/apoptosis split.

It replaces the older overlapping notes:

```text
docs/research/archive/probabilistic-belief-layer.md
docs/research/archive/probabilistic-circuit-breakers.md
```

Those files are historical pointers. New belief/breaker work should update this document instead of creating another parallel research note.

## Core problem

Menhir should not flatten memory into:

```text
fact + timestamp
```

A useful agent memory has to preserve:

```text
what is relevant
what is true now
what was believed then
what is supported by evidence
what was superseded later
what should be asserted, hedged, shown as conflict, or withheld
```

This matters most in debugging memory. A stale but semantically strong memory can poison an agent if it is retrieved as current truth.

Example:

```text
E1: Original CE willow texture-cache crash observed.
E2: CE willow patch added.
E3: Crash appears resolved.
E4: Compatibility/load-order issue appears.
E5: Load-order fix resolves the remaining issue.
```

At E3, it may be reasonable to believe the patch fixed the crash. At E5, that belief is superseded or narrowed. Menhir should preserve both states without asserting the old belief as current truth.

## Existing menhir substrate

Current menhir already contains proto-belief-governance mechanisms:

```text
two-phase vector -> score/rank recall
file-context structural candidate injection
scope and freshness filtering
namespace filtering
conflict detection and routing
session -> persistent consolidation
compression / deletion / rehydration lifecycle
structure graph / blast radius traversal
BeliefCircuit domain spike
```

This doc does not propose replacing those systems. It names the missing policy layer that coordinates them.

## Core design principle

```text
Recent and useful should get hotter.
Recent and unproductive should cool down.
Superseded should become historical, not dead.
```

Recency is not bad. Unproductive recency is bad because an agent can reinforce its own loop:

```text
memory retrieved
-> last_accessed touched
-> recency score improves
-> memory retrieved again
-> agent repeats same failed reasoning
```

The fix is not to remove recency. The fix is to distinguish productive recency from loop recency.

## Belief heads

Do not collapse all uncertainty into one confidence score.

BeliefCircuit should keep these heads separate:

```text
RELEVANT:
  Is this memory relevant to the query?

CURRENT:
  Is this fact/belief safe as current truth?

SUPPORTED:
  Is this explanation supported by the evidence set?

SUPERSEDED:
  Has this old belief been contradicted, narrowed, or replaced by later evidence?
```

A belief can be relevant and no longer current.

Example:

```text
Relevant("patch fixed crash", query) = high
Current("patch fixed crash", now) = low
Supported("patch fixed original symptom", evidence) = medium/high
Superseded("patch fully fixed issue", later evidence) = high
```

## Recall buckets

The current `do_not_assert` bucket is too coarse. It mixes historically useful superseded memories with unsafe/noisy memories.

Replace the simple initial bucket set:

```text
SAFE_TO_ASSERT
MENTION_WITH_UNCERTAINTY
CONFLICT_SET
DO_NOT_ASSERT
```

with a more precise policy surface:

```text
SAFE_TO_ASSERT:
  high-confidence current truth or well-supported claim.

MENTION_WITH_UNCERTAINTY:
  plausible but incomplete, weakly supported, or missing corroboration.

CONFLICT_SET:
  useful because competing memories disagree or unresolved contradiction exists.

HISTORICAL_ONLY:
  useful for timeline / belief-drift explanation, but not current truth.

ANERGIC_CURRENT:
  semantically relevant but suppressed for current-truth retrieval/assertion.

BLOCKED:
  unsafe/noisy/unsupported enough that it should not enter normal answer context.
```

The key split:

```text
HISTORICAL_ONLY / ANERGIC_CURRENT are not dead.
They remain available for historical BraidTrace traversal.

BLOCKED is stronger.
It is for bad extractions, unsafe merges, unsupported claims, or poisoned context.
```

## Anergy vs apoptosis

Use immune vocabulary only where it maps to behavior.

### AnergicBeliefGate

```text
AnergicBeliefGate = suppress a memory from current-truth retrieval or assertion while preserving it for historical/conflict traversal.
```

Use when:

```text
memory is semantically relevant
but stale, superseded, branch-wrong, repo-wrong, or temporally invalid for the current query
```

Example:

```text
"The CE willow patch fully fixed the issue"
```

After load-order evidence, this should not be asserted as current truth. It can still be shown as a former belief.

### ApoptoticIndexPrune

```text
ApoptoticIndexPrune = remove, quarantine, or demote a candidate from active retrieval/index paths.
```

Use only when:

```text
bad extraction
corrupted duplicate
unsafe merge candidate
proven invalid active-index entry
low-value decayed memory selected by lifecycle policy
```

Do not use apoptosis for ordinary supersession. Superseded memories often explain why the agent believed something before.

## Breaker operation vocabulary

A breaker converts belief/evidence state into action policy.

Initial operations:

```text
ASSERTION:
  Can the assistant assert this as current truth?

RETRIEVAL_INJECTION:
  Should this retrieved memory enter the LLM context, and with what label?

WRITE_PROMOTION:
  Should this extraction become durable memory?

TEMPORAL_CURRENTNESS:
  Is this true now, true then, or only believed then?

ENTITY_MERGE:
  Should these graph nodes be merged, linked, or kept separate?

CAUSAL_CLAIM:
  Can menhir say X caused Y, or only correlated/suspected?

AGENT_ACTION:
  Is the evidence strong enough for an agent to edit code, or only draft/run tests?

LIFECYCLE:
  Can this memory be compressed, preserved, rehydrated, or deleted?

RESEARCH_CLAIM:
  Can this architecture claim graduate from speculation to supported-by-eval?

PROVIDER_EXTRACTION:
  Can this model extraction be merged, retried, quarantined, or stored raw only?
```

Initial decisions:

```text
ALLOW
ALLOW_WITH_LABEL
HEDGE
QUARANTINE
REQUIRE_CORROBORATION
BLOCK
```

The output is not merely a score. It is an operational decision.

## Evidence signals

Start with transparent, inspectable signals. Do not add a heavy probabilistic-circuit dependency first.

```text
embedding_match
entity_match
graph_path
same_file_context
same_symbol_context
same_test_context
inside_dependency_cone

is_valid_at_query_time
is_expired
has_valid_at
has_invalid_at
has_created_at
has_expired_at
later_correction_exists
relative_time_resolved

mentioned_by_user
mentioned_by_agent
source_is_user
source_is_agent_inference
source_is_log
source_is_git
source_is_test
source_is_graphiti
source_is_summary

observed_error
test_failed
test_passed
file_changed
symbol_changed
dependency_changed
changed_between_good_and_bad
before_failure
after_failure
later_contradicted
later_confirmed

known_good_exists
known_bad_exists
test_result_exists
commit_range_exists
repo_state_known

source_reliability
memory_age
evidence_count
missing_evidence_count
```

## Productive recency and exhaustion

Add a session-local retrieval penalty for agent loops.

### RetrievalExhaustionPenalty

```text
RetrievalExhaustionPenalty = dynamic score attenuation for repeatedly retrieved memories that do not help progress within a session.
```

Inputs:

```text
session_retrieval_count
same_trace_retrieval_count
unproductive_retrieval_count
last_progress_event
```

Possible progress events:

```text
test passed
failure changed
user confirmed
patch accepted
conflict resolved
memory became a cairn/landmark
source-of-truth citation used successfully
```

Exemptions:

```text
current task goal
active error log
explicit user instruction
source-of-truth docs
foundational architecture memories
```

Do not penalize repeated retrieval by itself. Penalize repeated unproductive retrieval.

## Structural expansion

Current menhir already has structural candidate injection via file context and blast-radius traversal. The next step is to generalize it into bounded structural expansion.

### BoundedStructuralExpansion

```text
semantic/vector hit
-> structural neighbor expansion
-> bounded candidate clone pool
-> rerank / survival selection
```

Candidate clone types:

```text
caller/callee symbols
imported/importer files
tests covering source
recent Git neighbors
same-error historical memories
superseded belief neighbors
open TODOs linked to impacted files
```

Required guards:

```text
max_clones_per_hit
max_total_clones
centrality_penalty
utility-symbol suppression
blast-radius depth limit
```

Do not create unbounded graph expansion.

## SelfToleranceGate

The existing namespace/scope filters are the seed of self/non-self tolerance. Extend the idea to code-memory identity.

```text
SelfToleranceGate = prevent wrong-scope memories from entering current context.
```

Self signals:

```text
same user/session/task scope
same project
same repo
same branch
same commit or valid commit range
same file/symbol identity
same dependency version
same namespace
```

Non-self risks:

```text
old branch memory
other repo memory
renamed symbol treated as same symbol
same filename in different project
agent inference stored as user fact
obsolete dependency behavior
```

## BraidTrace relationship

Tracehead/BraidTrace vocabulary is useful, but parked until implementation needs it.

Current working vocabulary:

```text
Tracehead:
  selected entry point into a braided memory/evidence structure.

BraidTrace:
  query-time traversal through interwoven strands.

BraidFrame:
  stored braided memory/event/belief unit.
```

For now, use BraidTrace as a result/projection shape over existing data, not a persisted graph schema.

Potential result shape:

```python
@dataclass(frozen=True)
class Tracehead:
    id: str
    kind: str
    target_id: str
    reason: str

@dataclass(frozen=True)
class BraidTrace:
    tracehead: Tracehead
    strands: tuple[str, ...]
    crossings: tuple[str, ...]
    breaker_decisions: tuple[str, ...]
```

Promotion condition:

```text
Create a persisted BraidFrame schema only after archolith-bench shows that projection over existing Episode/Entity/Fact/Evidence nodes is insufficient.
```

## Implementation ladder

### Rung 0: transparent baseline

Use the current BeliefScorer approach: weighted evidence, log-odds update, inspectable rationale.

```text
Purpose:
  stabilize vocabulary
  write fixtures
  define metrics
  avoid premature dependencies
```

### Rung 1: belief-aware recall policy

Add bucket split and retrieval-time policy:

```text
current query:
  suppress HISTORICAL_ONLY / ANERGIC_CURRENT from current assertion

historical query:
  allow HISTORICAL_ONLY as former belief

conflict query:
  allow CONFLICT_SET
```

### Rung 2: Git/structure stale evidence

Feed actual repo/structure signals into evidence:

```text
FILE_CHANGED
SYMBOL_CHANGED
DEPENDENCY_CHANGED
changed_between_good_and_bad
inside_dependency_cone
```

### Rung 3: bounded structural expansion

Generalize file-context injection into bounded structural candidate expansion.

### Rung 4: bench-gated probabilistic backend

Only evaluate ProbLog, PyJuice, or true probabilistic circuits after transparent baselines produce bench artifacts and limitations.

## Belief gate (implemented, default-OFF)

The belief gate activates `CurrentnessWarden` in the assertion pipeline:

- **Producer**: `domain/belief_evidence.py` assembles `BeliefEvidence` + scores via `BeliefScorer` from candidate metadata temporal markers.
- **Pipeline**: `AssertionPipeline(belief_gate=True)` appends `CurrentnessWarden` and populates `WardenContext.belief_score`/`.evidence`.
- **Flag**: `MENHIR_FRONTIER_BELIEF_GATE` (default OFF) threads through `RetrievalTuningConfig.enable_belief_gate` → both active `_apply_frontier` and observe-only `_run_assertion_shadow`.
- **Temporal markers**: When the gate is on, `_belief_markers_from_facts` derives `belief_superseded`/`belief_has_temporal` from `fetch_temporal_facts` and merges them into candidate metadata.
- **Permissive by default**: Only candidates with a temporal marker (superseded or timed fact) are scored; the wardens ADMIT unmarked candidates.
- **Requires `warden_gate` to take effect**: `belief_gate` only ADDS `CurrentnessWarden` to the chain. The master switch that APPLIES the chain's verdicts (drop REFUSED / label FLAGGED) in `_apply_frontier` is `warden_gate`. With `belief_gate` on but `warden_gate` off, belief verdicts are computed and discarded; recall logs a warning and appends a `belief_gate has no effect without warden_gate` note. To actually gate on belief, enable BOTH `MENHIR_FRONTIER_BELIEF_GATE` and `MENHIR_FRONTIER_WARDEN_GATE`.
- **Git/structure staleness (implemented)**: when the gate is on, recall derives code staleness deterministically. `infrastructure/git_log.capture_changes` runs `git log <belief_commit>..HEAD` over a candidate's anchor paths via `services/change_log_provider.CachedGitChangeLog` (cached per `(repo, HEAD)` — a swap seam for a future persistent sidecar); `fetch_candidate_provenance` supplies `anchor_paths`; ingest stamps `belief_commit`/`belief_branch` per memory; `_staleness_evidence_for` calls `derive_structural_staleness` and merges its `LATER_CONTRADICTED` evidence into `belief_evidence`, so a memory anchored to code changed after it was formed is treated as superseded and gated by `CurrentnessWarden`. Committed beliefs use grounded ANCESTRY (`belief_commit..HEAD`); memories without a `belief_commit` fall back to the ungrounded DATE_HEURISTIC. Best-effort: any git failure or unresolvable repo degrades to no staleness, never breaking recall.

### Forward / before activation

The following are deferred and must be addressed before `MENHIR_FRONTIER_BELIEF_GATE` is
turned on in any environment. #3 is a capability gap, #2 is tuning, #4 is the activation gate.
#1 (git/structure staleness) is now **implemented** (see the section above); only the
follow-ons below remain.

#### 1. Git-staleness follow-ons (core implemented; extensions remain)

- **Persistent sidecar for the change log:** `ChangeLogProvider`/`CachedGitChangeLog` caches
  in-process per `(repo, HEAD)`. A persistent sidecar backend (SQLite/file; survives restarts;
  pre-populatable out-of-band) is the production answer when repos are not present on disk at
  recall — it slots behind the same Protocol without touching callers.
- **Repo-availability caveat:** staleness only fires where menhir resolves the project's repo on
  disk (`repo_root_for_project`) with a meaningful HEAD; elsewhere it degrades silently to no
  signal. On deployments without the dev repos checked out, the sidecar above is required.
- **WORKTREE / dirty-belief mode:** `derive_structural_staleness` supports a worktree-hash mode,
  but ingest captures only `belief_commit`/`belief_branch` (not `belief_worktree_hash`) and the
  recall feed does not yet hash dirty anchors (`git status`). Uncommitted-belief staleness is
  therefore not yet grounded.
- **SYMBOL/DEPENDENCY change kinds:** the git feed emits only `file` changes; symbol- and
  dependency-level granularity (and rename-following beyond file paths) remain.

#### 2. Richer provenance weighting (tuning; lowest risk)

- **Exists:** `DEFAULT_SIGNAL_WEIGHTS` is a flat per-signal lookup (e.g. `IS_EXPIRED 1.2`,
  `LATER_CONTRADICTED 1.1`, `IS_VALID_AT_QUERY_TIME 0.9`, `MENTIONED_BY_USER 0.7`,
  `SOURCE_IS_GIT 0.65`, `MENTIONED_BY_AGENT 0.35`). `belief_evidence.assemble_belief_evidence`
  emits each provenance kind as `SUPPORTS` at `strength=1.0` and lets these weights do the work.
- **Missing:** graded `strength` (hardcoded 1.0); multiplicity/independence (three independent
  git anchors should outweigh one; duplicates currently collapse); calibration against bench
  outcomes (weights are hand-set; `scripts/_calibrate_combiner.py` is combiner calibration, not
  belief-weight calibration).
- **Why deferred:** refinement on top of a working transparent baseline (Rung-0 ethos: ship the
  inspectable flat weights first); should follow #4 so there is something to calibrate against.
- **Risk while skipped:** mild mis-confidence in bucketing; the flat weights are already sensible.

#### 3. CAUSE/FIX/REGRESSION inference (capability gap)

- **Exists:** `BeliefCandidateType` defines `CAUSE, FIX, REGRESSION, SUPERSESSION,
  DEPENDENCY_STATE`. `score_candidate_belief` only ever assigns `SUPERSESSION` (expired) or
  `DEPENDENCY_STATE` (neutral current).
- **Missing:** inference for the three belief-drift narrative types ("the patch fixed X" = FIX,
  "load order caused Y" = CAUSE, "it regressed" = REGRESSION). Requires assembling the
  failure-relation signals already in the enum but never produced (`BEFORE_FAILURE`/
  `AFTER_FAILURE`, `OBSERVED_ERROR`, `TEST_FAILED`/`TEST_PASSED`) plus light classification of
  memory content. Shares part of #1's git/test feed.
- **Why deferred:** depends on signals not yet assembled.
- **Risk while skipped:** the gate answers "is this current?" but not "what role does this belief
  play in the drift story?" -- the rich recall packet (`SAFE_TO_ASSERT` /
  `MENTION_WITH_UNCERTAINTY` / `HISTORICAL_ONLY` / `ANERGIC_CURRENT` / `CONFLICT_SET`) collapses
  toward a current-vs-superseded binary. The `ce_willow_belief_drift` expected packet is not
  reproducible until this lands.

#### 4. Bench validation (activation gate; process, not code)

- **What it is:** before `MENHIR_FRONTIER_BELIEF_GATE` is turned on, the gate must be proven on
  the archolith-bench belief-drift fixtures (`ce_willow_belief_drift`,
  `auth_payload_refactor_stale_memory`, `out_of_order_insertion`, `retroactive_correction`,
  `wrong_repo_or_branch_memory`, `agent_retrieval_loop`, `structural_neighbor_bug`) against the
  defined metrics (`stale_current_assertion_rate`, `poisoned_context_injection_rate`,
  `historical_context_preservation`, `valid_context_density`, ...).
- **Required:** run the gate shadow-mode (observe-only, the `_run_assertion_shadow` pattern) OFF
  vs ON; confirm `stale_current_assertion_rate` drops WITHOUT regressing
  `historical_context_preservation`/`valid_context_density`; only then flip the flag. The fixture
  runner likely needs building/extending in the archolith-bench repo (separate).
- **Why deferred:** "menhir proposes, archolith-bench proves" -- no flag flips without artifacts.

**Dependency ordering:** #1 and #3 are the real capability work and share a feed (git/test
signals + per-memory commit provenance) -- do them together. #2 is tuning that should follow #4.
#4 blocks activation regardless.

## archolith-bench fixtures

Initial fixture families:

```text
ce_willow_belief_drift
auth_payload_refactor_stale_memory
out_of_order_insertion
retroactive_correction
wrong_repo_or_branch_memory
agent_retrieval_loop
structural_neighbor_bug
```

Baseline ladder:

```text
A: graph recall only
B: graph recall + temporal metadata
C: graph recall + belief buckets
D: graph recall + belief buckets + anergic gate
E: graph recall + belief buckets + anergic gate + exhaustion penalty
F: graph recall + belief buckets + anergic gate + bounded structural expansion
```

Metrics:

```text
stale_current_assertion_rate
historical_context_preservation
poisoned_context_injection_rate
current_vs_historical_accuracy
belief_drift_accuracy
evidence_attribution_accuracy
wrong_scope_injection_rate
retrieval_loop_rate
valid_context_density
blast_radius_recall_at_k
latency_ms
```

## CE willow expected behavior

Query:

```text
What broke after I added the CE willow patch, and what did we believe before the load-order fix?
```

Expected packet:

```text
SAFE_TO_ASSERT:
  Load order caused or contributed to the remaining compatibility issue.

MENTION_WITH_UNCERTAINTY:
  The patch may have addressed the original texture-cache symptom.

HISTORICAL_ONLY:
  At one point, it appeared the patch fixed the crash.

ANERGIC_CURRENT:
  The patch fully fixed all issues.

CONFLICT_SET:
  Any unresolved evidence where patch behavior and load-order behavior disagree.
```

The answer should explain belief drift instead of flattening the story.

## Non-goals

Do not:

```text
replace Neo4j or Graphiti
build one global probabilistic circuit over all memory
turn immune metaphors into subsystems by default
delete superseded memories just because they are old
add PyJuice or another heavy dependency before bench pressure exists
persist a giant BraidFrame graph schema before projection fails
claim causality from temporal proximity alone
promote research claims without archolith-bench artifacts
```

## Immediate implementation targets

```text
1. Extend RecallBucket / BeliefRecallPacket with HISTORICAL_ONLY, ANERGIC_CURRENT, and BLOCKED.
2. Add retrieval-time policy for current vs historical vs conflict query intent.
3. Add session-local retrieval counters and RetrievalExhaustionPenalty.
4. Feed Git/structure stale signals into BeliefEvidence.
5. Generalize file-context injection into bounded structural candidate expansion.
```

## One-sentence thesis

BeliefCircuit and probabilistic breakers are not a replacement for menhir's graph memory; they are the policy layer that decides when relevant memory is current truth, historical context, conflict evidence, tentative support, or unsafe context.
