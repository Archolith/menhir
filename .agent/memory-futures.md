# Memory Futures

Compact companion for the advanced and post-v1 directions described in
[memory-design.md](memory-design.md).

Use this file first when you need the future-facing design threads without loading the full design
doc.

## Vision Tag: Cognitive Infrastructure Platform (CIP)

Working label to preserve: **Cognitive Infrastructure Platform**.

Menhir should be positioned as cognitive infrastructure for autonomous agents, not only as a
memory system. Memory is one capability inside a broader substrate for identity, time,
experience, structure, Git provenance, contradiction handling, friction analysis, planning
support, background digestion, and future world-model adapters.

Short form:

> Menhir provides cognitive infrastructure for AI agents.

Longer form:

> Menhir is a Cognitive Infrastructure Platform that turns raw episodes into durable memory,
> knowledge, constraints, experience records, and reusable behavior for autonomous agents.

Use this term when revisiting product/category positioning, external docs, and long-term roadmap
language.

The full positioning elaboration (CIP architecture/primitives/metrics plus the Agent Experience
Substrate and cognitive-artifacts lenses) now lives in the canonical
`docs/research/positioning/positioning.md`. Keep this tag as the futures-facing pointer; do new positioning
work in that doc.

## Scope

This file covers:

- `memory.design.promotion`
- `memory.design.temporal`
- `memory.design.freeform_queries`
- `memory.design.progressive_retrieval`
- `memory.design.world_models`
- future-facing operator and automation direction

## Sections

### `memory.design.promotion`
- source sections: `Pattern Promotion: Skills and Hooks`
- use for:
  - how repeated memories may become reusable behavior
  - skill vs hook distinction
  - promotion guardrails

Key points:
- repeated patterns can promote into passive skills or active hooks
- promotion should look for repetition across sessions, not just within one chat
- promoted nodes are protected and therefore need quotas and periodic review

### `memory.design.temporal`
- source sections: `Temporal Awareness (post-v1)`
- use for:
  - future time-aware suppression and resurfacing
  - graph-native temporal edges and epochs
  - why chronology and relevance are not the same thing

Key points:
- current recency handling is intentionally simple
- future temporal reasoning should distinguish "fresh and redundant" from "old and worth resurfacing"
- rhythm, epochs, and drift detection all require real usage before they are worth implementing

### `memory.design.progressive_retrieval`
- source sections: `Scoring`, `Query Construction`, `Weak Spots and Feature Ideas Backlog`
- use for:
  - post-v1 retrieval quality work
  - avoiding expensive all-memory reasoning
  - deciding what to cache after an expensive retrieval or synthesis
  - separating practical roadmap items from speculative research architectures

Key points:
- do not make Menhir a Hopfield/tensor/HDC research implementation just because those fields are interesting
- borrow the useful ideas: associative recall, coherence ranking, hierarchy, compression, and compositional indexing
- keep the runtime engineering-first: exact lookup -> cached summaries -> hierarchy descent -> semantic candidate retrieval -> coherence ranking -> LLM synthesis
- most queries should stop before the expensive stages
- every expensive operation should leave behind a reusable cache artifact
- the graph remains the durable substrate; hierarchy, vector search, temporal views, and coherence scoring are retrieval projections over that substrate

Practical shape:
- store each memory once
- attach lightweight references from multiple hierarchies: project, repo, directory, file, symbol, session, time, intent, git commit, test, incident
- maintain cached summaries at useful category nodes: repository summary, file summary, symbol summary, session summary, recent-change summary
- maintain cached candidate pools for common access paths such as `file -> related memories`, `symbol -> known decisions`, `test -> likely causes`, and `commit range -> changed symbols`
- use coherence ranking after candidate narrowing, not as a replacement for narrowing
- treat "energy-style" retrieval as a ranking intuition: compatible memories reinforce each other, contradictions reduce confidence, and the result should prefer a coherent explanation over isolated nearest neighbors

Explicit non-goals for now:
- replacing Neo4j/Graphiti with tensor networks, hyperdimensional computing, or Modern Hopfield Networks
- implementing freeform high-dimensional memory algebra before the existing graph/code/time layers are reliable
- performing expensive global reasoning synchronously in the agent's hot path

### `memory.design.world_models`
- source sections: future model-adapter planning, temporal memory, progressive retrieval, code graph companion
- use for:
  - preparing Menhir for JEPA/world-model/planning-first agents
  - avoiding LLM-only assumptions in the memory substrate
  - designing experience records richer than text facts
  - capturing physical/sensory/friction context around agent work

Core stance:
- Menhir should be an **agent memory system**, not only an LLM memory system.
- Current LLM clients mostly retrieve by natural-language query; future agents may retrieve by latent state, goal state, predicted transition, plan checkpoint, or observed failure.
- The durable substrate should remain useful even if the reasoning engine changes from autoregressive text generation to a world-model/planning architecture.
- Do not redesign Menhir around speculative models now; preserve adapter seams so future model classes can query the same graph/time/code/history substrate.

Experience memory shape:
- **State**: what the environment looked like at the start. For coding agents this includes repo state, git hash, code graph, active files, open tasks, running services, dependency versions, failing tests, and active constraints.
- **Goal**: what the agent or user was trying to accomplish.
- **Lead-up / preconditions**: what happened immediately before the event, including prior failed attempts, recent commits, conversation context, tool history, and user corrections.
- **Plan**: the intended action sequence or hypothesis before acting.
- **Action**: the concrete edit, command, tool call, prompt, refactor, or workflow step.
- **Transition**: what changed between before and after.
- **Expectation**: what the agent predicted would happen.
- **Outcome**: what actually happened.
- **Surprise**: where expectation and outcome diverged.
- **Constraint learned**: durable rule or environmental condition discovered by the event.
- **Physical/sensory context**: what the work *felt like* operationally, represented as observable signals rather than vague prose. Examples: latency, tool slowness, repeated command hangs, CPU/GPU/RAM pressure, noisy logs, flaky tests, long build time, UI responsiveness, network instability, file watcher churn, editor/terminal friction, or "this path feels brittle because every edit causes cascading failures."
- **Friction/emotional context**: user frustration, agent confusion, repeated corrections, confidence collapse, uncertainty, or pain points. This overlaps with `cth.painscan` and should feed candidate review before promotion.
- **Related experiences**: previous states/transitions/outcomes that resemble this one.
- **Durable summary**: compact human-readable summary, generated after the evidence is stored.

Important distinction:
- Store observations first.
- Store reasoning second, with provenance.
- Future models should be able to reinterpret the same evidence.

Good durable evidence:
- `pytest tests/auth/test_login.py` failed after commit X.
- build time increased from 18s to 71s after dependency Y changed.
- the user corrected the agent three times about file Z.
- GPU memory pressure caused local model unload/reload churn.
- a file watcher saw the same generated file rewritten repeatedly.
- the agent expected one integration failure but got five unrelated failures.

Weak durable evidence by itself:
- "the auth system was confusing"
- "the model got it wrong"
- "this felt bad"

Those weak summaries are still useful, but only after anchoring them to concrete observations.

Future retrieval modes:
- natural-language semantic recall
- structural recall by file/symbol/test
- temporal recall by before/after interval
- Git recall by commit range or branch divergence
- state similarity recall: "have we seen a repo state like this?"
- transition recall: "what actions previously moved this state toward this goal?"
- surprise recall: "what previously violated this expectation?"
- constraint recall: "what hidden rules matter before acting?"
- physical/friction recall: "what operational signals made similar sessions painful or slow?"

Adapter direction:
- Keep natural-language MCP tools, but do not make them the only contract.
- Add future adapter shapes that can pass structured state fingerprints, goal embeddings, action/outcome records, and plan checkpoints.
- Treat latent vectors from future models as one retrieval signal, not the ontology.
- Keep graph identity, time, provenance, and code/Git anchors as the stable substrate.

Near-term implications:
- Add fields and caches in ways that can later attach to `Experience`, `State`, `Transition`, `Outcome`, and `Surprise` nodes/edges.
- Preserve raw enough evidence for future reinterpretation: commands, outputs, file hashes, test names, timings, tool errors, user corrections, and git metadata.
- Avoid collapsing everything into final summaries too early.
- Let `cth.painscan` and similar background digesters produce candidate friction/physical-context memories, not automatically promoted durable facts.
- Use progressive retrieval and cached candidate pools so richer experience memory does not make hot-path recall too expensive.

Non-goals for now:
- implementing a JEPA or world model inside Menhir
- replacing text summaries or graph retrieval
- storing opaque latent states without provenance, versioning, or a model identifier
- treating "physical memory" as mystical; it should mean captured operational/sensory evidence plus human friction signals

Success criterion:
- If a future planning-first agent replaces today's LLM client, Menhir should only need a new retrieval adapter and new experience-memory writers, not a rewrite of memory identity, time, provenance, structure, or caching.

### `memory.design.freeform_queries`
- source sections: `Agent-Authored Graph Queries (post-v1)`
- use for:
  - sandboxed Cypher generation
  - read/write guardrails
  - how fixed pipelines may loosen later

Key points:
- freeform graph operations are a later expansion, not a v1 substitute
- any such path needs validation, resource limits, and dry-run behavior
- write safety matters more than flexibility at this stage

### `memory.design.future_automation`
- source sections: `Pattern Promotion: Skills and Hooks`, `Temporal Awareness (post-v1)`, `Progressive Retrieval`, `World Models`
- use for:
  - future self-generated automation and session guidance
  - how stable patterns might turn into workflow assists
  - background digestion of sessions into cheaper future retrieval artifacts
  - digestion of experience records into reusable state/transition/outcome memories

Key points:
- the system may eventually suggest or generate hooks from repeated user behavior
- automation should stay reviewable and reversible
- high-confidence patterns matter more than novelty
- background digestion should refresh summaries, candidate pools, contradictions, and blast-radius caches outside the interactive retrieval path
- background digestion should also extract repeated lead-up/action/outcome/friction patterns from experience records

## View Kinds — Frontier Transfer

Exploratory design note on additional View shapes beyond ScalarState/Counter/Timeline,
transferred from industrial process control. Ranked most → least useful;
includes two counterexamples (Derivative, Feedforward) that test the fold law's
boundary and should stay read-time δ, never stored Views. Keystone transfer is
**SetpointView** — the partner ScalarState lacks for "what should X be," without
which drift is uncomputable as a View.

Full doc: [memory-view-kinds-frontier-transfer.md](memory-view-kinds-frontier-transfer.md).

## Research Watch List

These areas may inspire future scoring or compression work, but they are not implementation requirements:

- **Modern Hopfield Networks / Dense Associative Memory**: useful analogy for coherence ranking and stable explanation selection.
- **Hyperdimensional Computing / Vector Symbolic Architectures**: useful analogy for compositional binding and sequence encoding.
- **Tensor Networks**: useful analogy for hierarchical compression if memory scale becomes the bottleneck.
- **Dynamic Hypergraphs**: useful if binary edges become awkward for commits, incidents, sessions, and other many-participant events.
- **Persistent Homology / Topological Data Analysis**: exploratory idea for finding long-lived architectural motifs across Git/code history.
- **JEPA / world-model / latent-planning agents**: useful future client class; plan for adapters and experience memory, but do not make Menhir depend on this architecture.

Default stance: watch and borrow mechanisms, but build the practical cache + hierarchy + coherence pipeline first.

## Read Next

- Need current ingest/query behavior -> [memory-ingest-queries.md](memory-ingest-queries.md)
- Need current lifecycle/policy behavior -> [memory-policy.md](memory-policy.md)
- Need backlog and open questions -> [memory-backlog.md](memory-backlog.md)
