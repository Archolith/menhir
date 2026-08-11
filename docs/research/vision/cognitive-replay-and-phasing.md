# Cognitive replay, development phasing, and the epistemic-separation law

## Status

speculative

## Promotion condition

Promote individual pieces independently:

```text
Development phases:
  becomes the working roadmap framing when referenced from .agent/memory-roadmap.md
  or a release plan adopts the phase ladder.

Cognitive Replay:
  promote when there is a replay query surface or a fixture that reconstructs
  belief/understanding state at a past point (not just a log-to-fixture test
  harness). Needs a code surface or archolith-bench artifact.

Epistemic-separation law:
  already partially in force as the observe/decide/write boundary; promote the
  four-layer (observations/interpretations/decisions/state) formulation when a
  reasoning-engine swap or replay feature actually depends on it.
```

## What this doc owns / does not own

Owns: the staged phase ladder, the Cognitive Replay capability, and the
epistemic-separation law.

Does **not** re-document the oracle pipeline (Candidate Retrieval → Oracle
Evaluation → Combination → Belief → Mutator), oracle families, scheduler lanes,
combiner roles, belief outputs, or the Mutator responsibilities. Those are
already owned:

```text
oracle-amplified-retrieval.md      oracle interface, families, combiner, amplification
retrieval-control-rails.md         CostAwareOracleScheduler lanes, SelfReinforcementGuard
oracle-execution-and-performance.md observe/decide/write boundary, snapshots, budgets
belief-layer.md                    assertion policy (current/historical/anergic/conflict/blocked)
positioning.md                     system hierarchy, "retrieval is evidence of attention"
```

The recap of that pipeline in the source dump is intentionally not duplicated
here; this doc captures only what those docs do not already hold.

## Development phases

The current direction organizes into a staged evolution. This is a conceptual
ladder, distinct from the M0–M7 *delivery milestones* in
`.agent/memory-roadmap.md`.

```text
Phase 1 — Memory Foundation
  Durable memory substrate: ingestion, graph identity, lifecycle, promotion,
  contradiction handling, code structure, Git integration, temporal metadata,
  retrieval.
  Goal: become a production-quality memory system before becoming a research
  platform.

Phase 2 — Progressive Retrieval
  Retrieval becomes layered rather than vector-first:
    exact lookup
    -> cached summaries
    -> hierarchy narrowing
    -> candidate pools
    -> semantic retrieval
    -> oracle evaluation
  Key principle: expensive retrieval should always leave reusable cache artifacts.

Phase 3 — Temporal Intelligence
  Chronostratum evolves from timestamps into temporal reasoning: belief drift,
  supersession, historical reconstruction, temporal blast radius, validity
  intervals, learned time, expired facts.

Phase 4 — Experience Memory
  Store complete experiences, not isolated facts. Experience record:
    State, Goal, Lead-up, Plan, Action, Transition, Outcome, Surprise,
    Constraint, Friction.
  Supports future planners and world-model clients without redesigning the
  substrate.

Phase 5 — Background Cognition
  Idle processing continuously improves memory: summary refresh, contradiction
  analysis, candidate pool generation, cache rebuilding, pattern mining,
  PainScan, durable memory extraction, skill promotion, hook generation.

Phase 6 — Cognitive Infrastructure Platform
  Menhir becomes infrastructure rather than only memory: identity, time,
  structure, Git provenance, experience, constraints, planning support, belief
  management, background cognition. Memory becomes one subsystem.
  (Full elaboration in positioning.md, Lens 1.)

Phase 7 — Model Adapters
  Future reasoning engines consume the same durable substrate: autoregressive
  LLMs, planning agents, JEPA-style systems, latent world models. The substrate
  stays stable while adapters evolve.
```

Note: many Phase 2/4/5/7 components already have homes in
`.agent/memory-futures.md` (progressive retrieval, experience records,
background digestion / PainScan, world-model adapters). This ladder is the
synthesis view over them, not a replacement.

## Cognitive Replay

The headline new capability. Instead of treating Git history, conversations,
experiences, belief evolution, temporal memory, and blast radius as independent
features, they combine into a unified replay system over the project's
*cognitive* history.

Questions replay should answer:

```text
Why was this implemented?
Which alternatives were rejected?
What assumptions changed?
Which later evidence invalidated this belief?
What sequence of observations produced the current understanding?
```

How it differs from source control:

```text
Git stores code evolution.
Menhir stores cognitive evolution.
```

The result is replaying not only what changed, but what the agent believed
throughout the project's history.

This is distinct from the existing "replay" surfaces in the repo, which are test
and ops tooling (log-to-fixture replay harness in `.agent/post-v1-todo.md`,
merge replay queue in `.agent/memory-design.md`, ops replay tooling in
`.agent/architecture.md`). Cognitive Replay reconstructs *belief/understanding
state over time*, not request logs.

Dependency: Cognitive Replay needs the epistemic-separation law below — you can
only replay interpretations and decisions if they were stored separately from
observations and state.

## The epistemic-separation law

Elevate the layer separation into a core design law. This is the data-layer
companion to the observe/decide/write boundary (owned by
`oracle-execution-and-performance.md` / `positioning.md`):

```text
The memory graph stores observations.
Belief stores interpretations.
Reasoning produces decisions.
Mutators produce state.
```

Mapping to the existing boundary:

```text
observe/decide/write (pipeline roles):   Oracles observe. Combiners decide. Mutators write.
epistemic separation (data layers):      observations / interpretations / decisions / state
```

Why it matters:

```text
Keeping these responsibilities separate preserves explainability and lets future
reasoning engines replace today's models without rewriting Menhir's durable
substrate.
```

This is the architectural guarantee behind Phase 7 (model adapters) and the
precondition for Cognitive Replay: durable observations and recorded
interpretations outlive whichever reasoning engine produced them.

## Relationship to existing docs

```text
.agent/research/menhir-research-execution-ladder.md:
  the executable rung order that realizes these phases (Phase rungs P4/P5/PR/PA).
  This doc owns the conceptual phases; the ladder owns build order.

.agent/memory-roadmap.md:
  M0-M7 delivery milestones (what shipped/when). This doc's phases are the
  conceptual ladder, not those milestones.

.agent/memory-futures.md:
  owns the individual future threads (progressive retrieval, experience records,
  background digestion, world models) this ladder synthesizes.

positioning.md:
  Phase 6 (CIP) and the system hierarchy; the observe/decide/write law.

oracle-execution-and-performance.md:
  the observe/decide/write boundary the epistemic law extends.

belief-layer.md:
  belief = interpretations layer, in mechanism detail.
```

## Non-goals

Do not:

```text
re-document the oracle pipeline / families / scheduler / combiner here
treat the phase ladder as a committed release plan; memory-roadmap.md owns delivery
build Cognitive Replay before observations/interpretations/decisions are stored
  as separable layers
conflate Cognitive Replay with the log-to-fixture replay harness or ops replay tooling
```
