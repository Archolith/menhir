# Concept Tree Design

## Purpose

This document defines a token-efficient working format for representing graph concepts inside
agent-facing design docs.

Read this document before opening large graph/design docs in full. Use it to identify the concept
cluster you need, then load only the relevant section from the heavier reference document.

The storage model for `menhir` remains a graph. This format is only a projection for:

- design work
- planning
- compact reasoning
- partial loading in agent context

The main rule: **edit a tree, preserve the graph**.

That means each concept gets one primary location in the document tree, while non-tree
relationships are expressed as compact cross-links.

## Why This Exists

Long prose docs are expensive to load and hard to diff mentally. A compact concept tree gives us:

- smaller context windows
- stable anchors for recall and editing
- clearer parent/child ownership
- explicit graph edges without repeating whole sections

This is especially useful when a topic has:

- many related concepts
- repeated references across sections
- both hierarchy and cross-links

## Constraints

- The runtime data model is still a graph, not a tree.
- A concept may have many semantic parents in reality.
- The tree must choose exactly one primary parent for document placement.
- All secondary relationships must remain recoverable through links.
- The format must stay readable in plain Markdown.

## Core Model

Each concept has:

- `id`: stable document-local identifier
- `label`: short human-readable name
- `kind`: concept type such as `domain`, `pipeline`, `policy`, `node`, `edge`, `query`, `risk`
- `parent`: one primary document parent
- `links`: zero or more graph-style cross-links
- `status`: optional maturity marker such as `v1`, `post-v1`, `open`, `deprecated`

## Namespace Rules

- Prefer the shared registry in `concept-ids.md` over inventing new ids in place.
- Use top-level families consistently: `runtime.*`, `memory.*`, `model.*`, `mcp.*`.
- Use the most specific stable id that still has a single clear owner doc.
- When a concept becomes canonical enough to reference from more than one doc, add it to `concept-ids.md`.

## Working Rules

1. Every concept appears once in the tree body.
2. Repeated discussion should reference the concept by `id`, not duplicate prose.
3. Cross-domain relationships belong in `links`, not extra duplicate subtrees.
4. Keep labels short and descriptive.
5. Keep prose under each concept to one short block unless detail is essential.
6. Prefer bullets and compact fields over paragraphs.

## Agent Reading Workflow

When working from a large graph-heavy doc:

1. Read the tree or document map first.
2. Identify the concept ids or section anchors relevant to the task.
3. Open only those sections.
4. Expand to adjacent sections only if the local concept graph is insufficient.
5. Avoid reading the entire source document unless the task is broad by nature.

## Recommended Markdown Shape

Use this structure:

```md
# Topic

## Tree

- `memory`
  - `memory.ingest`
    - `memory.ingest.queue`
    - `memory.ingest.graphiti`
  - `memory.recall`
    - `memory.recall.search`
    - `memory.recall.score`

## Concepts

### `memory.ingest.graphiti`
- label: `Graphiti extraction`
- kind: `pipeline`
- parent: `memory.ingest`
- links:
  - `depends_on -> memory.runtime.scheduler`
  - `writes -> model.episode`
  - `feeds -> memory.recall.search`
- notes:
  - single-flight per runtime
  - bounded by timeout
  - scheduler watchdog may fail fast on stall
```

This keeps the tree cheap while preserving graph semantics.

## Compact Link Vocabulary

Prefer a small fixed verb set:

- `depends_on`
- `feeds`
- `writes`
- `reads`
- `owns`
- `guards`
- `conflicts_with`
- `promotes_to`
- `derived_from`
- `related_to`

Do not invent near-synonyms casually. Link vocabulary should stay tight so the document is easy to scan.

## Primary Parent Selection

When a concept could live in several places, choose the parent that answers:

"Where would someone look first if they wanted to edit this?"

Examples:

- A retry classifier belongs under failure handling, not under every caller.
- A queue row state belongs under the processing model, not under each tool that reads it.
- A scheduler trace concept belongs under runtime ops, even if ingest emits it.

## Graph Preservation Pattern

Tree placement handles containment. Cross-links handle graph truth.

Example:

```md
### `memory.processing.failed_retry`
- label: `Failed retry sweep`
- kind: `policy`
- parent: `memory.processing.maintenance`
- links:
  - `reads -> model.episode.failed`
  - `depends_on -> memory.failure.classifier`
  - `related_to -> memory.processing.stale_recovery`
```

This avoids copying the same concept into maintenance, failures, queueing, and telemetry sections.

## Token-Efficient Authoring Pattern

When a section grows, split it into:

1. Tree
2. Concept entries
3. Edge index

Optional edge index format:

```md
## Edge Index

- `memory.ingest.graphiti depends_on memory.runtime.scheduler`
- `memory.ingest.graphiti writes model.episode`
- `memory.recall.score reads model.entity.edge_count`
```

This is cheaper than repeating full explanatory paragraphs in multiple places.

## Suggested File Roles

Use the pattern like this:

- `memory-design.md`: policy and lifecycle concepts
- `architecture.md`: runtime/process concepts
- `data_models.md`: storage contracts
- focused future docs: compact tree for one subsystem when a section becomes too large

Do not force the whole project into one giant tree file. Use a tree per topic when it improves locality.

## Candidate Future Format

If Markdown trees become too noisy, a future sidecar format could be introduced:

```yaml
concepts:
  - id: memory.ingest.graphiti
    label: Graphiti extraction
    kind: pipeline
    parent: memory.ingest
    links:
      - type: depends_on
        target: memory.runtime.scheduler
      - type: writes
        target: model.episode
```

For now, plain Markdown is preferred because it is easier to diff and edit in normal agent workflows.

## Risks

- Tree projection can hide legitimate multi-parent structure if links are neglected.
- Over-compression can turn docs into opaque shorthand.
- Unstable ids will destroy the benefit of compact references.

## Guardrails

- Never remove a cross-link just because the concept already appears elsewhere in prose.
- Never create a second primary copy of the same concept to "make the tree look nicer."
- When in doubt, keep the tree shallow and express complexity in links.
- Prefer explicit ids over positional references like "the section above."

## Starter Template

```md
# <Topic>

## Tree

- `<root>`
  - `<root.child>`
  - `<root.other_child>`

## Concepts

### `<root.child>`
- label: ``
- kind: ``
- parent: `<root>`
- links:
  - `related_to -> <target>`
- notes:
  - 
```

## Decision

For `menhir`, the best approach is:

- keep the real system model graph-first
- add tree-projected topic docs for compact editing
- use stable concept ids plus explicit cross-links
- avoid duplicate prose when a link is enough

This gives us most of the token-efficiency benefit of a tree without sacrificing graph reality.
