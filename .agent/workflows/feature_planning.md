# Feature Planning Workflow

Use this before implementing any semi-large `menhir` feature or any change that introduces a new graph object type, new MCP/API surface, lifecycle exception, or cross-cutting architectural behavior.

## When a plan is required

Write a short plan first if the change does any of the following:

- adds a new node label, edge family, or durable store
- adds or materially changes MCP tools, API routes, or query surfaces
- changes lifecycle, recall, ingestion, or scheduler behavior
- creates a second path around existing architecture instead of extending it
- spans more than one subsystem or more than a small local refactor

Small bug fixes, copy updates, and local refactors do not need a standalone plan.

## Plan length

Keep it short. A good plan is usually 0.5 to 2 pages.

Preferred location:

- add a dedicated doc under `.agent/` when the feature is substantial
- add a section to an existing roadmap/backlog/design doc when the feature naturally belongs there

## Required template

Copy this template and fill it in before implementation:

```md
# <Feature Name>

## Why
- What problem is being solved?
- Why now?
- What user or operator pain does this remove?

## Scope
- What is in scope?
- What is explicitly out of scope for this pass?

## Proposed Design
- What existing architecture does this extend?
- What new components, node types, edges, tools, or endpoints are added?
- What data flow changes?

## Alternatives Considered
- Option A:
- Option B:
- Why the chosen direction is better for this project right now

## Risks
- Architectural risks
- Product-boundary risks
- Migration or backward-compatibility risks
- Testing risks

## Invariants
- What existing contracts must remain true?
- What should definitely not get worse?

## Validation
- Unit tests
- Integration tests
- Manual checks
- Observability or rollout checks

## Docs To Update
- architecture.md
- data_models.md
- endpoints.md
- roadmap/backlog docs
- CHANGELOG.md
```

## Review checklist

Before implementation starts, confirm:

- the feature extends the existing architecture instead of bypassing it without a clear reason
- the product boundary is still clear
- multi-project and temporal behavior have been considered where relevant
- test strategy exists before code lands
- the docs that define the contract are identified up front

## Default rule

If a feature feels “obviously useful” but was not planned, stop and write the short plan anyway. That is the point where `menhir` has recently taken on avoidable architectural drift.
