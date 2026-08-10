# Layer 4 (and Layer 3) knowledge-artifact schema

## Status

speculative

Day-2 deliverable of `docs/roadmap/weekend-oracle-runtime-roadmap.md` (Priority 4). This is **spec only —
no code yet**. It owns the concrete *schema* for the SOS semantic/institutional overlay; the
*direction* is owned by `semantic-operating-system.md` (four-layer model, evidence-as-first-class,
knowledge-promotion lifecycle, temporal semantics). That overlay is **SOS Program B (semantic) + Program
D (institutional)** — the part the execution ladder flags as the **unsequenced GAP**. Do not build from
this doc or invent a ladder rung; it is design that pends ctharvey's sequencing.

## Promotion condition

`supported-by-spike` when menhir gains a generic knowledge-artifact store with at least one artifact type
landing through the `MemoryMutator` write boundary (R9) carrying provenance/confidence/valid-time/review
state. `supported-by-eval` only when archolith-bench shows oracle interpretation over these artifacts
improves a task brief (not just retrieval recall).

## The one decision this doc commits to

The roadmap's open question — *specialized memory tables/classes, or generic artifacts interpreted by
oracles?* — resolves to:

> **Store generic knowledge artifacts. Let oracles provide specialized interpretation.**

One schema, a `type` discriminator, and oracles (`MemoryOracle`, `EvidenceOracle`, `SemanticNodeOracle`,
`TemporalOracle`) that read it for their own purposes. This keeps every future memory kind from becoming
a one-off subsystem — the same anti-sprawl move as the primitive/composite oracle split.

## The generic artifact

```python
from dataclasses import dataclass, field
from enum import Enum


class Layer(str, Enum):
    SEMANTIC = "L3"        # interpretive model: capabilities, policies, constraints, invariants
    INSTITUTIONAL = "L4"   # what the org learned: decisions, failures, incidents, assumptions


class ArtifactType(str, Enum):
    # Layer 3 — semantic model
    CAPABILITY = "capability"
    POLICY = "policy"
    CONSTRAINT = "constraint"
    INVARIANT = "invariant"
    # Layer 4 — institutional knowledge
    DECISION = "decision"          # DecisionMemory (ADR-like)
    FAILURE = "failure"            # FailureMemory — a tried-and-rejected approach
    INCIDENT = "incident"          # IncidentMemory — a production regression
    ASSUMPTION = "assumption"      # AssumptionMemory — believed-true, may decay
    REVIEW = "review"              # ReviewMemory — a review conclusion
    AGENT_DISCOVERY = "agent_discovery"  # something an agent learned mid-task


class Status(str, Enum):
    """The SOS knowledge-promotion lifecycle (observation -> ... -> historical)."""

    OBSERVATION = "observation"          # raw, unreviewed
    CANDIDATE = "candidate"              # proposed knowledge, low confidence
    EVIDENCE_COLLECTED = "evidence_collected"
    TRUSTED = "trusted"                  # reviewed + evidence-backed
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"
    HISTORICAL = "historical"            # no longer current, still valid as history


class ReviewState(str, Enum):
    UNREVIEWED = "unreviewed"
    AGENT_REVIEWED = "agent_reviewed"
    HUMAN_REVIEWED = "human_reviewed"


@dataclass(frozen=True)
class EvidenceRef:
    """Evidence as a first-class entity (SOS) — never 'because an LLM said so'."""

    kind: str        # function | test | commit | incident | adr | conversation | benchmark | log
    ref: str         # structural anchor or external id
    directness: float = 1.0
    note: str | None = None


@dataclass(frozen=True)
class StructuralAnchor:
    """A deterministic Layer-2 anchor (the spine). Never LLM-derived."""

    kind: str        # file | symbol | test | commit | dependency
    ref: str


@dataclass
class KnowledgeArtifact:
    id: str
    layer: Layer
    type: ArtifactType
    summary: str
    body: str = ""
    status: Status = Status.OBSERVATION
    review_state: ReviewState = ReviewState.UNREVIEWED
    confidence: float = 0.0              # applies to interpretation, not to anchored facts
    origin: str = ""                     # who/what proposed it (agent id, user, importer)
    # temporal (SOS temporal semantics)
    learned_at: str | None = None        # when it entered the store
    valid_from: str | None = None
    valid_to: str | None = None
    # provenance + structure
    evidence: tuple[EvidenceRef, ...] = ()
    anchors: tuple[StructuralAnchor, ...] = ()   # deterministic Layer-2 ties
    # supersession graph
    supersedes: tuple[str, ...] = ()
    superseded_by: str | None = None
    invalidated_by: str | None = None
```

## Invariants (the spine, applied to storage)

```text
1. Structural anchors are deterministic (Layer 2) and never LLM-derived.
2. A semantic/institutional artifact may START as an AI hypothesis, but it carries
   status + confidence + review_state + valid-time + supersession and MUST NOT
   silently become trusted. Promotion OBSERVATION -> TRUSTED needs evidence + review.
3. Evidence is first-class: a trusted artifact has >= 1 EvidenceRef; a claim with no
   evidence stays CANDIDATE at best.
4. Superseded is not deleted — it becomes HISTORICAL (retrievable for historical
   queries; suppressed for current ones). Mirrors the BeliefLayer rule.
5. Only the MemoryMutator (ladder R9) writes/promotes/expires artifacts. Oracles read.
```

## How oracles interpret one store

```text
MemoryOracle        reads artifacts by type/anchor for a task
EvidenceOracle      scores an artifact's support (evidence kind/directness/count)
SemanticNodeOracle  reads L3 capability/policy/constraint/invariant nodes
TemporalOracle      reads valid_from/valid_to/learned_at/superseded_by for currentness
```

The same artifact serves a current-truth query (TRUSTED + valid) and a historical query (SUPERSEDED)
differently — the artifact is generic; the *oracle* + query intent decide its role.

## Correspondence to the bench (keep these coherent)

The `archolith_bench/oracle` fixture model is a **flattened subset** of this schema — when the real
store lands, the bench metadata should project from it, not diverge:

```text
KnowledgeArtifact            archolith_bench OracleMemory
--------------------------   ----------------------------
status/superseded            superseded, belief_bucket (current/historical/anergic/blocked)
valid_from/valid_to/learned  valid_at / invalid_at / created_at
evidence[].kind              evidence_kinds
anchors (file/symbol/test)   files / symbols / tests
type                         (not yet modelled in the bench — add when types land)
```

## Prior art in menhir (reuse, don't rebuild)

A code audit (2026-06-28) found much of this substrate **already exists** — this schema is largely a
*formalization + extension*, not a greenfield build. Map the spec onto what's shipped:

```text
spec concept                     already in menhir (REUSE)                              status
-------------------------------  ----------------------------------------------------  -----------------
CANDIDATE (untrusted) status     scope='CANDIDATE' tier (candidate_repository.py:        EXISTS
                                 "pre-structured :Entity nodes, not recalled until
                                 approved"); promote_candidate(); add_candidate tool;
                                 CandidateService.approve/reject; explorer review surface
promotion CANDIDATE->TRUSTED     promote_candidate() -> PERSISTENT + contradiction       EXISTS (B-1)
                                 check; deliberately NOT the user_flagged shortcut
confidence / human-vs-LLM trust  source_confidence (1.0/0.9/0.5) + source field         EXISTS
supersession / conflict          conflict_status, `contradicts` edge, resolution        EXISTS (B-4)
                                 history + cooldown (conflict-resolution-history)
decay of un-promoted             ConsolidationRepository Entity decay                   EXISTS (B-3)
anchors (artifact -> structure)  ANCHORED_TO / CREATED_FROM edges                       EXISTS (edges)
passive emitter                  cth.painscan emits candidates                          EXISTS (B-2)
clustering / dedup               cluster_id on candidates + _content_overlap_ratio      EXISTS (A-6)
the MemoryMutator write boundary write ops exist (promote_candidate, delete, decay,     SCATTERED — R9 =
                                 conflict-resolve) but are NOT yet a named boundary      name/consolidate
the knowledge-promotion lifecycle spread across scope + freshness + source_confidence + PARTIAL — the clean
  (observation->...->trusted)    conflict_status, not one clean `status` enum            status field is new
```

**Genuinely NEW (the real build):** institutional/L3 artifact **types** (memory_types.py has
EPISODIC/SEMANTIC/… but no Decision/Incident/Failure/capability/policy); the **first-class `Evidence`
node** (no `:Evidence` label exists today — A-1's one migration from edges); a single clean
`status`/`review_state` field (or an explicit mapping onto scope+freshness); and the **LLM proposer**
(just another candidate emitter on the existing path). Everything else above is reuse.

## Open questions (for sequencing)

```text
1. Is the supersession graph stored on the artifact (superseded_by) or as edges in
   the graph backend? (Prefer edges once Graphiti/Neo4j models it; field is the spec.)
2. Confidence decay over time (SOS confidence_over_time) — a stored series, or
   recomputed by the TemporalOracle from evidence age? (Prefer recompute.)
3. How much of L3 (capability/policy) is LLM-proposed vs imported from structure?
   This is the highest-scope-risk part of Program B and needs ctharvey to sequence.
```

## Non-goals

```text
do not implement a store or types from this doc — it is a spec
do not invent a ladder rung; L3/L4 overlay is the unsequenced GAP (Program B/D)
do not let an LLM mint a TRUSTED artifact or a deterministic anchor
do not re-own the four-layer direction, evidence model, or lifecycle (semantic-operating-system.md)
do not duplicate the Cold Start Brief schema (cold-start-brief.md owns it)
```
