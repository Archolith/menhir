# Building on Menhir: Practical Cognitive Applications

> **Status:** speculative (positioning/vision catalog; not code-backed — see `research-process.md`
> vocabulary). Promotion condition: none — this is a catalog of possible downstream applications,
> not a build proposal; individual applications would need their own doc + code surface to promote.
> **Purpose:** describe practical systems that can be built on top of Menhir, and show why Menhir should be understood as cognitive infrastructure rather than a single memory application.

## Core thesis

Menhir is not only an AI memory application.

It is a substrate for building systems that need durable, structured, temporal, inspectable cognitive state.

Most AI products need some combination of:

- memory,
- identity,
- belief tracking,
- source provenance,
- temporal awareness,
- contradiction handling,
- structured retrieval,
- trust calibration,
- decision history,
- workflow memory,
- and model-independent context assembly.

Today, many projects rebuild those pieces separately. Menhir's opportunity is to make those cognitive primitives reusable.

The practical question is not only:

> What does Menhir remember?

It is:

> What kinds of systems become easier to build once memory, provenance, time, structure, and cognitive state are available as infrastructure?

This document catalogs those systems.

---

## 1. Agentic software engineering

Software engineering is Menhir's most immediate use case because code is already structured, versioned, and full of temporal relationships.

### Applications

- **Agentic coding memory**: preserve what an agent tried, why it tried it, what worked, what failed, and which files or symbols were involved.
- **Git-aware debugging**: answer questions like, "This test used to pass; what changed in the dependency cone between then and now?"
- **Bug archaeology**: reconstruct the history of a bug across commits, issues, logs, test failures, and agent sessions.
- **Architectural decision memory**: connect design decisions to the files, symbols, tests, discussions, and failures that motivated them.
- **Symbol history**: track how a function, class, module, or API evolved over time.
- **Regression explanation**: connect new failures to earlier passing states, relevant code changes, and related memories.
- **Refactoring assistant**: maintain a temporal map of old names, new names, moved files, deleted symbols, and compatibility risks.
- **Code review memory**: remember recurring review patterns, team preferences, risky areas, and reviewer feedback.
- **Dependency blast radius**: combine structure graphs with time to identify what changed inside the impacted cone.
- **Agentic Git**: rewind not only code changes, but the reasoning chain that produced them.

### Example query

```text
Test C failed today. What does it depend on structurally, and which of those dependencies changed since the last passing run?
```

### Why Menhir helps

A normal vector store can retrieve semantically similar snippets. It cannot reliably join code structure, Git history, test state, and prior reasoning into one temporal explanation. Menhir can make that join a first-class operation.

---

## 2. Project and product memory

Many teams lose the reasons behind their decisions long before they lose the documents themselves.

### Applications

- **Decision provenance**: connect a decision to the meetings, issues, tradeoffs, experiments, and constraints that produced it.
- **Roadmap memory**: track how priorities changed over time and why.
- **Feature lineage**: explain why a feature exists, which users requested it, and which later changes modified the original intent.
- **Product research memory**: connect interview notes, customer feedback, support tickets, and design changes.
- **Experiment tracking**: record hypotheses, variants, outcomes, and follow-up decisions.
- **Launch memory**: preserve what was shipped, what was cut, what broke, and what the team learned.

### Example query

```text
Why did we choose local-first storage for this feature, and what objections did we have at the time?
```

### Why Menhir helps

Product memory is not just document search. It is a temporal graph of decisions, beliefs, objections, evidence, and reversals.

---

## 3. Organizational memory

Organizations accumulate knowledge that is hard to find because it lives across chats, documents, tickets, meetings, emails, source control, and people's heads.

### Applications

- **Institutional memory assistant**: answer questions using the organization's accumulated history.
- **Onboarding assistant**: explain not only how things work, but why they are the way they are.
- **Meeting knowledge graph**: connect meetings to decisions, owners, follow-ups, and later outcomes.
- **Policy evolution tracker**: show how rules, procedures, and internal policies changed over time.
- **Team expertise map**: infer who has worked on which areas and what knowledge they hold.
- **Tribal knowledge preservation**: turn repeated informal explanations into durable knowledge.
- **Handoff memory**: help one person or agent resume work from another with full context.

### Example query

```text
Who last worked on this subsystem, what decisions did they make, and what risks did they left behind?
```

### Why Menhir helps

Organizational memory is inherently temporal and social. It needs provenance, authority, contradiction handling, and aging. Menhir can model old information as old instead of treating it as equally current.

---

## 4. Research systems

Research work depends on remembering claims, evidence, contradictions, failed hypotheses, and evolving interpretations.

### Applications

- **Literature graph**: connect papers by claims, methods, datasets, benchmarks, limitations, and citations.
- **Hypothesis tracking**: record proposed hypotheses, supporting evidence, contradicting evidence, and current confidence.
- **Experiment lineage**: connect experimental setup, code, results, failures, and interpretation.
- **Contradiction detection**: identify when a new source conflicts with an existing claim.
- **Evidence provenance**: track where a belief came from and how strongly it is supported.
- **Research timeline**: show how an idea evolved across papers, notes, conversations, and experiments.
- **Failure memory**: preserve negative results and dead ends so agents do not repeat them.
- **Cross-domain synthesis**: connect mechanisms from one field to another without flattening everything into vague analogy.

### Example query

```text
What changed in our belief about temporal memory after the oracle retrieval experiments?
```

### Why Menhir helps

Research knowledge is not a pile of PDFs. It is a changing belief network with provenance, uncertainty, and time. Menhir can make that network queryable.

---

## 5. Long-term AI agents

A long-running agent needs more than chat history. It needs a durable cognitive state that survives model swaps, session resets, and changing tools.

### Applications

- **Long-term agent memory**: preserve goals, failures, preferences, decisions, and task history.
- **Experience replay**: retrieve past situations similar to the current task and reuse lessons learned.
- **Tool learning**: remember which tools worked for which jobs and under what constraints.
- **Failure recovery**: identify repeated failure modes and avoid known traps.
- **Belief revision**: update agent beliefs when new evidence contradicts old assumptions.
- **Goal persistence**: maintain long-running objectives across sessions.
- **Multi-agent shared memory**: allow different agents to contribute to and query a shared cognitive substrate.
- **Agent handoff**: transfer not only task state, but rationale, uncertainty, and open questions.

### Example query

```text
Have we tried this approach before? If so, what failed, and what should we do differently this time?
```

### Why Menhir helps

Most agent memory is either a transcript, a vector store, or a summary. Menhir can make memory structured, temporal, inspectable, and reusable across models.

---

## 6. Cognitive identity and personality systems

Personality can be treated as more than tone. It can be represented as durable cognitive state: beliefs, habits, preferences, values, trust settings, risk tolerance, and learned heuristics.

### Applications

- **External identity graph**: represent an agent's beliefs, habits, values, goals, preferences, and expertise as inspectable objects.
- **Temporal personality history**: show how an agent's behavior changed over time and why.
- **Mode activation**: activate different cognitive modes such as Bug Hunter, Research Scientist, Teacher, Security Auditor, or Product Manager.
- **Preference learning**: preserve user or team preferences without hiding them inside a model prompt.
- **Trust calibration**: remember which sources, tools, people, and processes have proven reliable.
- **Style plus substance**: separate writing style from deeper cognitive behavior like skepticism, caution, curiosity, or empirical preference.
- **Personality backend**: use Menhir as the cognitive data layer while an application-specific frontend defines the behavioral rules, interpretations, and projections that turn backend state into a personality.

### Example query

```text
Why did this agent become more conservative about code changes after June?
```

### Why Menhir helps

Current persona systems often live inside prompts or model activations. Menhir can represent identity externally as a model-agnostic, inspectable, evolving graph.

A personality program built on Menhir could treat personality as a projection over backend data rather than as a hard-coded prompt. The backend stores memories, preferences, failures, trust scores, beliefs, goals, and temporal changes. The frontend decides how to interpret those signals into behavior.

```text
Menhir backend:
  memories + beliefs + preferences + failures + trust + goals + provenance + time

Personality runtime:
  interpretation rules + behavioral policies + mode selection + output constraints

LLM:
  language generation + reasoning + tool use
```

This allows multiple personality frontends to share the same cognitive substrate while behaving differently. A teacher interface, coding-agent interface, game-NPC interface, and personal-assistant interface could all interpret the same Menhir graph in different ways.

---

## 7. Personal knowledge systems

Personal knowledge tools often capture information but fail to preserve context, time, confidence, and why something mattered.

### Applications

- **Second brain**: store ideas, notes, documents, conversations, and decisions as a connected temporal graph.
- **Learning journal**: track what a person learned, when, from where, and how their understanding changed.
- **Decision diary**: record major decisions, assumptions, evidence, expected outcomes, and later results.
- **Idea incubator**: preserve unfinished ideas and reconnect them when new evidence or projects appear.
- **Goal tracker**: connect goals to actions, obstacles, revisions, and outcomes.
- **Life admin memory**: remember procedures, contacts, constraints, recurring tasks, and past outcomes.

### Example query

```text
When did I first start thinking this project should become cognitive infrastructure, and what led to that shift?
```

### Why Menhir helps

Personal knowledge is full of soft context. Menhir can preserve the trail of how a person's thinking changed, instead of only storing the final note.

---

## 8. Education and tutoring

Education depends on understanding not only what a learner knows, but how that knowledge changed over time.

### Applications

- **Personalized tutoring memory**: remember a student's strengths, gaps, preferences, and prior explanations.
- **Misconception tracking**: identify repeated conceptual errors and when they were corrected.
- **Mastery graph**: connect skills, prerequisites, practice attempts, and confidence.
- **Adaptive curriculum**: adjust learning paths based on past performance and current goals.
- **Teacher handoff**: transfer context between tutors, classes, or school years.
- **Learning provenance**: show which examples, explanations, or exercises caused improvement.

### Example query

```text
What misconception about recursion keeps recurring, and which explanation helped last time?
```

### Why Menhir helps

Tutoring systems need temporal learner models. A static profile cannot distinguish never learned, learned and forgotten, recently corrected, and repeatedly misunderstood.

---

## 9. Games and simulations

Game worlds become more believable when characters and factions remember events, relationships, injuries, betrayals, promises, and history.

### Applications

- **Persistent NPC memory**: remember what the player did, said, promised, stole, saved, or destroyed.
- **Faction history**: track alliances, betrayals, conflicts, debts, and grudges.
- **Dynamic reputation**: compute reputation from remembered events rather than fixed counters.
- **Quest provenance**: preserve why a quest exists, who requested it, and what changed afterward.
- **World history**: maintain a living timeline of settlements, wars, disasters, discoveries, and migrations.
- **Character personality evolution**: allow NPCs to change based on experience.

### Example query

```text
Why does this village distrust the player, and who still supports them despite that?
```

### Why Menhir helps

Most game memory systems are shallow flags. Menhir could support richer, inspectable, temporally grounded world state.

---

## 10. Healthcare and care coordination

Healthcare knowledge is deeply temporal: symptoms evolve, medications change, tests contradict, and care decisions depend on history.

### Applications

- **Patient timeline**: connect symptoms, visits, diagnoses, medications, lab results, and care plans.
- **Treatment rationale**: record why a treatment was chosen or discontinued.
- **Care coordination**: preserve context across doctors, specialists, caregivers, and family members.
- **Symptom evolution**: track when symptoms appeared, worsened, improved, or changed character.
- **Medical literature linkage**: connect patient-specific questions to external research with provenance.
- **Insurance and referral memory**: track denials, approvals, referrals, and follow-ups.

### Example query

```text
When did this symptom first appear, what changed around that time, and which tests addressed it?
```

### Why Menhir helps

A healthcare memory system must distinguish current facts from historical facts. Menhir's temporal model is directly relevant to this distinction.

---

## 11. Legal, compliance, and policy

Legal and compliance work depends on precedent, authority, versioning, provenance, and interpretation.

### Applications

- **Regulation history**: track how laws, policies, or internal rules changed over time.
- **Contract memory**: connect clauses, obligations, deadlines, amendments, and disputes.
- **Evidence provenance**: preserve where a claim came from and what supports it.
- **Case evolution**: track filings, arguments, rulings, dates, and changing legal theories.
- **Compliance assistant**: map requirements to controls, owners, evidence, and review cycles.
- **Policy contradiction detection**: identify when a new policy conflicts with an old rule or practice.

### Example query

```text
Which version of this policy was active when the disputed action happened?
```

### Why Menhir helps

Legal and compliance questions often fail when systems collapse time. Menhir can keep valid-time, learned-time, and superseded information distinct.

---

## 12. Cybersecurity

Security work is built around timelines, indicators, adversary behavior, vulnerabilities, and incident memory.

### Applications

- **Incident timeline**: connect alerts, logs, hosts, users, indicators, decisions, and remediation.
- **Threat intelligence graph**: track IOCs, campaigns, tools, techniques, actors, and confidence.
- **Vulnerability lineage**: connect vulnerabilities to code, dependencies, exploits, patches, and mitigations.
- **Root-cause archaeology**: reconstruct how an incident became possible.
- **Security review memory**: remember recurring risky patterns across reviews.
- **Detection evolution**: track how rules changed and which alerts they produced.

### Example query

```text
Have we seen this indicator before, and what did it connect to last time?
```

### Why Menhir helps

Security is not only similarity search. It requires event chains, provenance, confidence, and changing threat context.

---

## 13. Finance and investment reasoning

Financial decisions often depend on remembered theses, assumptions, market context, and belief revision.

### Applications

- **Investment thesis tracking**: preserve the reason for entering, holding, changing, or exiting a position.
- **Market belief evolution**: track how macro, technical, and fundamental beliefs changed.
- **Trade journal**: connect trades to hypotheses, risk limits, outcomes, and postmortems.
- **Portfolio reasoning**: remember why allocations exist and what would invalidate them.
- **Risk memory**: preserve recurring mistakes, drawdowns, and ignored signals.
- **Economic knowledge graph**: connect data releases, policy changes, narratives, and asset moves.

### Example query

```text
What would invalidate the original thesis for this position, and has any of that happened yet?
```

### Why Menhir helps

A good financial memory system needs to remember what someone believed at the time, not rewrite the past with hindsight.

---

## 14. Manufacturing, operations, and maintenance

Operational environments accumulate recurring failures, fixes, exceptions, and local knowledge.

### Applications

- **Equipment history**: track failures, repairs, parts, technicians, symptoms, and operating conditions.
- **Maintenance memory**: remember what fixed a problem before and how long it lasted.
- **Process evolution**: connect process changes to quality outcomes and incidents.
- **Supply chain knowledge**: track vendors, delays, substitutions, and downstream effects.
- **Quality investigation**: connect defects to batches, machines, operators, materials, and timestamps.
- **Operational handoff**: preserve shift-to-shift context.

### Example query

```text
When this machine made this sound before, what failed afterward and what fixed it?
```

### Why Menhir helps

Operations knowledge is practical, temporal, and local. Menhir can preserve that experience as structured memory instead of letting it disappear into logs and anecdotes.

---

## 15. Creative work and storytelling

Creative projects need continuity, evolving intent, character consistency, and design rationale.

### Applications

- **Story continuity**: remember events, timelines, character relationships, and unresolved threads.
- **Character memory**: track motivations, secrets, promises, traumas, and changes over time.
- **Worldbuilding graph**: connect places, factions, artifacts, histories, laws, and myths.
- **Design rationale**: preserve why a visual, mechanical, or narrative choice was made.
- **Art direction memory**: remember style decisions, rejected directions, references, and constraints.
- **Narrative contradiction detection**: identify continuity breaks.

### Example query

```text
Does this scene contradict anything we established about this character's history?
```

### Why Menhir helps

Creative memory is not only factual. It includes intent, tone, continuity, and evolution. Menhir can represent those as connected, revisable context.

---

## 16. Public knowledge and civic systems

Public institutions need memory that is transparent, sourced, temporal, and auditable.

### Applications

- **Government institutional memory**: preserve decisions, meeting records, policies, budgets, and public comments.
- **Public records assistant**: make civic documents searchable by issue, date, actor, and decision.
- **Policy timeline**: show how a policy proposal changed from first mention to final vote.
- **Community knowledge graph**: connect local issues, stakeholders, documents, meetings, and outcomes.
- **Historical reconstruction**: rebuild the timeline of a public decision from fragmented records.
- **Transparency tools**: expose provenance for claims made about public actions.

### Example query

```text
When was this vendor first discussed, who approved it, and what alternatives were considered?
```

### Why Menhir helps

Civic knowledge requires trust. Menhir's value is not just retrieval; it is provenance, chronology, and inspectability.

---

# Reusable cognitive primitives

The applications above look different on the surface, but many are composed from the same primitives.

## Temporal primitives

- valid-time
- learned-time
- supersession
- expiration
- sequence
- event chains
- temporal joins
- before/after reasoning
- historical reconstruction

## Memory primitives

- episodic memory
- semantic memory
- procedural memory
- structural memory
- reflection memory
- failure memory
- preference memory
- workflow memory

## Knowledge primitives

- claims
- evidence
- confidence
- contradictions
- provenance
- source authority
- trust scores
- belief revision
- uncertainty

## Structure primitives

- files
- symbols
- modules
- APIs
- dependencies
- ownership
- hierarchy
- blast radius
- identity reconciliation

## Agent primitives

- goals
- plans
- actions
- observations
- tool use
- outcomes
- mistakes
- learned heuristics
- mode selection
- handoff state

## Social and organizational primitives

- people
- teams
- roles
- decisions
- meetings
- responsibilities
- approvals
- expertise
- policies
- institutional memory

## Identity and personality primitives

- preferences
- values
- habits
- behavioral policies
- cognitive modes
- trust calibration
- risk tolerance
- style preferences
- explanation preferences
- temporal personality drift

---

# Design implication

Menhir should avoid becoming too tightly coupled to any one application.

The more important goal is to expose a small set of reusable cognitive infrastructure primitives that other systems can build on:

```text
Ingest → Structure → Link → Time-index → Retrieve → Interpret → Act
```

Different products can then define their own frontends, policies, schemas, and behavioral projections.

A coding agent may interpret the graph as repo memory.

A research assistant may interpret it as claim and evidence memory.

A personality program may interpret it as identity, preference, and behavioral policy memory.

A game may interpret it as NPC and world history.

A civic tool may interpret it as public decision provenance.

The backend primitives can stay largely the same.

---

# Practical build examples

## Example 1: Menhir-backed coding agent

A coding assistant connects to Menhir before every task. It retrieves relevant files, symbols, tests, prior failures, architectural decisions, and recent Git changes. After each task, it writes back what it attempted, what changed, and what should be remembered.

The result is an agent that gets better within a repository over time.

## Example 2: Menhir-backed research notebook

A researcher ingests papers, notes, conversations, and experiments. Menhir stores claims, evidence, contradictions, confidence, and timelines. The assistant can answer how a hypothesis evolved and which sources currently support or weaken it.

The result is a research system that remembers not only documents, but changing beliefs.

## Example 3: Menhir-backed organizational assistant

A team connects documents, meetings, tickets, pull requests, and chat exports. Menhir builds institutional memory with provenance and time. New employees can ask why things are the way they are, not just where a document is.

The result is onboarding and decision memory that survives turnover.

## Example 4: Menhir-backed personality runtime

A personality application uses Menhir as the durable state layer. The application defines rules for interpreting memories, preferences, trust scores, goals, and prior interactions. Different frontends can project the same graph into different behaviors.

The result is an inspectable personality system that can explain why it behaves the way it does.

## Example 5: Menhir-backed game NPC system

A game stores player actions, NPC reactions, faction history, promises, betrayals, and world events. Dialogue agents retrieve relevant history before speaking or acting.

The result is a game world where memory is not a few reputation counters, but a living historical substrate.

---

# Positioning

Menhir can be described as a memory system, but that undersells it.

A stronger framing is:

> Menhir is cognitive infrastructure for systems that need durable, structured, temporal, inspectable state.

Or more provocatively:

> Menhir is a backend for building applications that remember, revise, explain, and evolve.

The long-term opportunity is not one product. It is an ecosystem of applications that share common cognitive primitives.

Relational databases made business software easier to build because developers no longer had to reinvent structured persistence for every application.

Menhir can play a similar role for cognitive software: not replacing the model, not replacing the frontend, and not replacing domain logic, but providing the durable substrate that makes long-term cognition practical.
