# Org-scale Menhir — grouped enterprise direction

## Status

Strategic roadmap note. This is **not a ladder rung** and does not authorize implementation by itself.

This document groups the personal-to-organization design shifts discussed for Menhir. The working assumption is that Menhir began as a personal / small-agent memory system, but heavier organizational use requires stronger boundaries around governance, permissions, audit, lifecycle, and knowledge economics.

The core reframe:

> Personal Menhir can optimize for recall and usefulness. Org Menhir must optimize for governed institutional memory: evidence, accountability, access control, lifecycle, and measurable knowledge health.

---

## Group 1 — Governance, trust, and safety foundation

These are the first changes needed before broad organizational use.

### 1. Formal governance lifecycle

Replace implicit personal trust with explicit status transitions:

```text
CANDIDATE -> REVIEWED -> TRUSTED -> SUPERSEDED / HISTORICAL
```

Required fields: reviewer, timestamp, evidence, owner/team, reason, rollback path.

Rule: no artifact becomes trusted without evidence and accountable review.

### 2. Multi-tenant permissions and visibility

Access must be scoped by org, team, repo, project, branch, environment, and sensitivity class.

Not every memory should be visible to every agent or person. Security incidents, customer-specific context, private roadmap information, and sensitive operational notes need hard boundaries.

### 3. Audit log everything

Every meaningful state change should be auditable:

```text
who created it
who promoted it
what evidence supported it
what changed
what it superseded
which agent used it
which output it influenced
```

This supports trust, debugging, compliance, and agent accountability.

### 4. Hot-path vs background cognition split

Org systems need predictable latency and cost.

```text
hot path:
  deterministic retrieval
  trusted facts
  bounded oracle calls

background:
  LLM proposal
  clustering
  contradiction detection
  decay
  candidate generation
```

No surprise expensive LLM reasoning should happen during a coding task unless explicitly budgeted.

### 5. Org-level memory quality controls

Teams need dashboards and health metrics:

```text
candidate backlog
trusted artifact count
stale artifact rate
conflict rate
evidence coverage
agent reuse rate
failed-approach avoidance
false-trust incidents
```

Without this, Menhir risks becoming another stale wiki.

---

## Group 2 — Ownership, lifecycle, and organizational structure

These turn individual memories into institutional assets.

### 6. Team memory as a first-class object

Artifacts should carry team ownership and stewardship:

```text
Decision
  team
  owners
  stakeholders
  review cadence
  successor team
```

People leave; institutional memory should not.

### 7. Automated memory lifecycle

Every artifact should have a lifecycle:

```text
Candidate -> Trusted -> Needs Review -> Deprecated -> Historical -> Archived
```

Review can be triggered by age, major release, repo split, owner change, incident, or conflicting evidence.

### 8. Memory economics

At scale, an organization cannot keep every memory equally hot forever.

Track:

```text
retrieval count
agent usage
human usage
last referenced
importance
maintenance cost
storage tier
```

This turns archival into an evidence-backed decision instead of a storage panic.

### 9. Organizational trust graph

Not all sources deserve equal weight.

Example ordering:

```text
production incident postmortem   highest
approved ADR                     highest
principal engineer decision       high
code review                       medium
Slack discussion                  low
raw LLM suggestion                very low
```

This should become a graph of institutional trust, not just a flat confidence number.

### 10. Organizational identity model

Org-scale Menhir needs entities beyond files and symbols:

```text
Capability -> Service -> Repository -> Files -> People -> Incidents -> Customers -> Roadmap -> Architecture
```

The target query is not only “what memory mentions auth.py?” but “everything we know about Authentication.”

---

## Group 3 — Knowledge contracts, supply chain, and beliefs

These move Menhir from passive memory capture toward intentional knowledge management.

### 11. Memory contracts

Teams declare what must be remembered for specific artifact classes.

Example incident contract:

```text
root cause
timeline
evidence
mitigation
follow-up tasks
owner
verification
```

Example decision contract:

```text
problem
alternatives considered
why this option won
evidence
success criteria
supersession rule
```

This shifts memory from “remember whatever seems important” to validated organizational knowledge.

### 12. Knowledge supply chain

Treat knowledge like software dependencies.

```text
Incident -> Decision -> Implementation -> Tests -> Runbook -> Customer Docs -> Training -> Support FAQ
```

When upstream knowledge changes, Menhir should identify downstream knowledge that needs updating.

### 13. Organizational beliefs

Organizations run on beliefs, not just facts.

Example:

```text
Postgres scales far enough
  -> belief weakens
  -> benchmark
  -> migration decision
  -> current belief: multi-region requires different storage
```

Menhir should support queries like “what beliefs changed this year?”

### 14. Organizational experiments

Every failed or successful experiment should become reusable institutional knowledge.

```text
Hypothesis -> Experiment -> Metrics -> Outcome -> Lesson
```

This directly supports “have we already tried this?”

### 15. Decision impact graph

Decisions should be linked to consequences:

```text
Decision -> Changed APIs -> Introduced bugs -> Required migration -> Improved latency -> Created incident
```

This enables queries like “which architectural decisions caused the most downstream work?”

### 16. Confidence decay

Confidence should decay with evidence age and relevance, not just wall-clock age.

Example: a three-year-old benchmark should enter review even if the statement is still plausible.

---

## Group 4 — Contradiction, friction, and agent performance

This group extends the friction / dream lens. It should not only find human workflow pain; it should also learn how agents perform, where they fail, and which agents/models are best suited for which work.

### 17. Organizational contradictions

Detect contradictions across code, docs, ADRs, runbooks, onboarding, monitoring, and production behavior.

Example:

```text
Runbook says restart service
Code disables restart path
Production incident shows restart failed
```

Contradictions should become reviewable artifacts, not buried retrieval oddities.

### 18. Organizational friction graph

Capture recurring workflow pain:

```text
build fails
five people search Slack
thirty minutes lost
same issue repeats next sprint
```

At scale, Menhir should be able to say which problems waste the most engineering time.

### 19. Agent performance lens

The friction / dream system should also track agent performance by task, model, repo, and failure mode.

Examples:

```text
Claude: strong migration planning, weak at this repo's test harness quirks
GPT: strong SQL review, weak at long-running refactors
Gemini: strong large-context summarization, weak at precise patching
local model: useful for cheap classification, unsafe for trusted artifact proposal
```

This is not generic model benchmarking. It is organization-specific agent memory:

```text
agent/model
  task type
  repository/context
  success/failure
  correction required
  human intervention needed
  cost/latency
  outcome quality
```

Use cases:

```text
route work to the best available agent/model
avoid repeating known agent failure modes
explain why an agent was selected
measure whether prompt/tooling changes improve outcomes
identify where humans still outperform agents
```

This belongs beside friction because agent failure is a new form of organizational friction.

### 20. Agent self-reinforcement guardrails

At org scale, agent performance memory must not become self-congratulating noise.

Rules:

```text
agent success requires external evidence
agent failure records should preserve correction evidence
agent performance claims decay over time
model-version changes invalidate old performance priors
```

---

## Group 5 — Coverage, health, and operational intelligence

These are the observability surfaces for organizational memory.

### 21. Knowledge coverage

Think code coverage, but for institutional knowledge.

```text
Critical service
  architecture? yes
  runbook? yes
  incident history? yes
  owners? yes
  recent benchmark? no
```

This reveals missing knowledge instead of only searching existing docs.

### 22. Organizational memory health

Every repository/service/team can have a memory-health score.

Signals:

```text
evidence coverage
decision freshness
conflict rate
documentation drift
unknown ownership
candidate backlog
false-trust incidents
```

### 23. Memory debt

Knowledge debt should become a backlog like bugs or tech debt.

Examples:

```text
undocumented decisions
conflicting beliefs
missing evidence
no owner
stale benchmark
unreviewed candidate
unknown rationale
```

### 24. Institutional onboarding

Instead of a static onboarding doc, Menhir assembles role-specific context:

```text
role -> team -> services -> decisions -> common failures -> current work -> key people -> first tasks
```

### 25. Living architecture

Architecture diagrams should emerge from Git, dependencies, incidents, decisions, ownership, and evidence.

The diagram is not the source of truth; it is a projection of current organizational memory.

---

## Group 6 — Simulation and organizational digital twin

These are long-horizon research directions, not near-term product commitments.

### 26. Organizational simulation

Ask questions like:

```text
What happens if Team X disappears?
What services lose their only knowledgeable owner?
Which decisions become orphaned?
Which runbooks have no current maintainer?
```

This uses ownership, services, incidents, artifacts, and evidence to identify institutional risk.

### 27. Organizational digital twin

The long-term enterprise framing is not “better RAG” or “better memory.” It is a living semantic twin of the engineering organization.

It models:

```text
people
teams
services
repositories
capabilities
decisions
evidence
incidents
experiments
beliefs
risks
roadmaps
policies
AI agents
temporal evolution
```

The goal is not to imitate the organization. The goal is to remember, explain, and reason about how the organization changes over time.

---

## Near-term implication for Menhir

The immediate codebase should not try to build all of this.

Near-term priority remains:

```text
L4 artifacts: Decision / Failure / Incident
first-class Evidence
MemoryMutator / R9-lite write boundary
read-only MemoryOracle
ColdStartBrief v0
```

But the org-scale direction changes the design pressure:

```text
build evidence and audit in now
avoid personal-only trust shortcuts
keep agent performance as a first-class future lens
make every write path explainable
make every artifact ownable, reviewable, and eventually expirable
```
