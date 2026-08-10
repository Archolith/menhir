# Cold Start Brief — schema, assembly, and context-pack provenance

## Status

speculative

Day-2 deliverable of `docs/roadmap/weekend-oracle-runtime-roadmap.md` (Priority 3 + Priority 5). **Spec
only — no code yet.** It owns the concrete *schema* + assembly + provenance rules for the task-shaped
Cold Start Brief; the *vision* is owned by `semantic-operating-system.md` ("Cold Start Brief" + Program
E). The composite oracle that produces it sits in the **unsequenced GAP** (Program E over Program B/D),
so do not build from this doc or invent a ladder rung.

## Promotion condition

`supported-by-spike` when a `ColdStartOracle` produces a `ColdStartBrief` from primitive findings + the
R7 OraclePacket without mutating state. `supported-by-eval` only when archolith-bench measures a brief
against a *task* fixture (brief completeness / Decision-Accuracy-per-Retrieved-Token), not retrieval
recall.

## The distinction this doc protects (do not overload "Cold Start Brief")

```text
R7 OraclePacket = RETRIEVAL-shaped evidence packet (combiner output: ranked candidates, role logits).
                  The kernel the brief is built FROM. Not the brief. (oracle-runtime-interfaces.md)
ColdStartBrief  = TASK-shaped synthesized brief: what ColdStartOracle produces by reasoning across
                  primitive findings, Layer 3/4 knowledge artifacts, AND the OraclePacket.
```

## Schema

The brief is **evidence-first** and keeps the fact/hypothesis spine structural — same discipline as
`OracleFinding`. Every item carries provenance; the agent can always ask "why is this here?"

```python
from dataclasses import dataclass, field
from enum import Enum


class Epistemic(str, Enum):
    FACT = "fact"                  # deterministic, anchored (Layer 2)
    TRUSTED = "trusted"            # reviewed + evidence-backed (Layer 3/4, status=TRUSTED)
    HYPOTHESIS = "hypothesis"      # interpretive, carries confidence


@dataclass(frozen=True)
class BriefItem:
    text: str
    epistemic: Epistemic
    confidence: float = 1.0                 # 1.0 for FACT; calibrated for HYPOTHESIS
    evidence: tuple[str, ...] = ()          # EvidenceRef ids / structural anchors
    requested_by: str = ""                  # which oracle asked for this (provenance)
    mitigates_risk: str | None = None       # what risk including it addresses


@dataclass
class ColdStartBrief:
    task: str
    # epistemic buckets (oracle-runtime-interfaces.md ordering)
    known_facts: tuple[BriefItem, ...] = ()          # what the structural layer proves
    trusted_knowledge: tuple[BriefItem, ...] = ()     # capabilities/policies/decisions (TRUSTED)
    likely_interpretations: tuple[BriefItem, ...] = ()  # hypotheses, with confidence
    open_questions: tuple[str, ...] = ()
    risks: tuple[BriefItem, ...] = ()                 # incidents, regressions, risky deps
    # the agent-facing payoff (SOS Cold Start Brief content list)
    failed_approaches: tuple[BriefItem, ...] = ()      # FailureMemory — do not repeat
    stale_or_contradicted: tuple[BriefItem, ...] = ()  # superseded beliefs flagged, not hidden
    protecting_tests: tuple[str, ...] = ()
    recommended_context: tuple[str, ...] = ()          # files/symbols (the context pack handle)
    recommended_first_actions: tuple[str, ...] = ()    # commands/tests to run
    evidence_links: tuple[str, ...] = ()
```

## Context-pack provenance rules (weekend Priority 5)

The **Context Engine packages; it does not decide truth.** Each packed item must carry:

```text
- why it was included
- which oracle requested it (BriefItem.requested_by)
- whether it is fact / trusted / hypothesis (BriefItem.epistemic)
- what evidence supports it (BriefItem.evidence)
- what risk it mitigates (BriefItem.mitigates_risk)
```

Packing rules:

```text
1. Smallest useful pack for the target model — decision quality per token, not recall.
2. Never silently promote a HYPOTHESIS to FACT in the rendered context.
3. stale_or_contradicted is surfaced, not dropped (the agent must see what NOT to trust).
4. recommended_context is a request the Context Engine may trim; it does not obey blindly.
```

## Assembly pipeline

```text
task
  -> deterministic retrieval (candidates + structural anchors)
  -> oracle evaluation (primitive RetrievalOracles -> OracleResult)
  -> OracleCombiner               -> R7 OraclePacket (ranked, role-aware)
  -> ColdStartOracle (composite)  -> reasons over OraclePacket + L3/L4 KnowledgeArtifacts
                                     -> ColdStartBrief (epistemic buckets + provenance)
  -> Context Engine               -> packs the smallest useful, provenance-tagged context
  -> agent session
```

Boundary rules preserved: oracles observe; the combiner + ColdStartOracle decide; only the MemoryMutator
(R9) writes. The ColdStartOracle MAY call an LLM (within budget) but its LLM output lands only as
`likely_interpretations` (HYPOTHESIS), never as `known_facts`.

## How it's measured (the strategic reframe)

Most systems benchmark *did we retrieve the right chunks*. The brief lets us benchmark *was the agent
prepared to make the right change* — the higher-value target. Bench surfaces (owed):

```text
brief_completeness          fraction of gold brief items present
decision_accuracy_per_token the CIP headline metric over the packed context
stale_surfaced_rate         contradicted beliefs flagged (not hidden) in the brief
provenance_fidelity         items with correct epistemic label + evidence
```

A `ColdStartBrief` archolith-bench fixture (task -> gold brief items) is the natural next bench artifact
once the L3/L4 store exists.

## Non-goals

```text
do not implement ColdStartOracle / the brief from this doc — it is a spec
do not invent a ladder rung; the composite/brief layer is the unsequenced GAP (Program E over B/D)
do not overload "Cold Start Brief" onto the R7 OraclePacket (they are different artifacts)
do not let the Context Engine decide truth or promote hypotheses to facts
do not re-own the Layer 3/4 artifact schema (layer4-knowledge-artifacts.md) or the SOS vision
```
