# Creating and moving work artifacts

The one instruction surface for creating, copying, moving, archiving, restoring, and reclassifying
a tracked document. If you are about to add a plan, review, handoff, implementation report, or
reference record — or move one that already exists — this file is the contract.

Validate before you commit:

```bash
menhir artifacts validate . --repository menhir
```

After a commit that creates, moves, or edits tracked artifacts, the normal audit uses Menhir's
persisted repository cursor automatically:

```bash
menhir artifacts audit --repo . --repository menhir
```

Use `--from-commit <sha>` only to inspect a deliberate alternate Git interval. It does not update
the stored cursor. Cursor advancement happens only through a digest-approved clean reconcile (or
configured startup `safe_apply`), never through authoring or audit.
If audit reports `evidence valid: False`, stop: the stored commit is missing or cannot be compared
with this checkout. Select and review a valid `--from-commit` before reconcile; do not treat an empty
rename list as a clean interval.

## Why this exists

Menhir records each tracked document as a `WorkArtifact` with a stable `artifact_uuid`, and records
where the bytes live as an `ArtifactSource` with a mutable locator. That split only survives if the
locator can be followed when a file moves. Detectors follow it from Git evidence and, best of all,
from a UUID you declared in the document itself. What they will never do is guess: a detector that
matched on titles or prose would eventually give one plan another plan's history.

So the division of labour is:

| Owned by you | Derived by menhir |
|---|---|
| `artifact_uuid`, `artifact_type`, `artifact_status` | `corpus_lane`, content hash, blob OID, observed commit, size |
| Declared relationships (`implements`, `reviews`, …) | `source_uuid`, resolution state, reconcile basis and run |

Never write a derived key into a document. A stale copy of an observation is worse than no copy —
validation rejects it.

## Where each document goes

| Directory | Type | Lane |
|---|---|---|
| `.agent/plans/` | `plan` | `active` |
| `.agent/plans/backlog/` | `plan` | `backlog` |
| `.agent/reviews/` | `review` | `active` |
| `.agent/handoffs/` | `handoff` | `active` |
| `.agent/for-review/` | `implementation_report` | `active` |
| `.agent/archive/plans/`, `.../reviews/`, `.../handoffs/` | existing type is preserved | `archive` |
| `.agent/reference/` | must be declared | `reference` |

The lane is routing, not meaning. A plan moved to `reference/` is still historically a plan; a plan
moved to `archive/` has not thereby been implemented. Only the directories in this table are part of
the corpus — a document in an undeclared subdirectory is invisible to reconciliation, and `README.md`
is a routing index rather than an artifact.

## The metadata block

New artifacts created from now on carry this block as the first lines of the file:

```yaml
---
artifact_schema: 1
artifact_uuid: 3f7c1e28-9a4b-4d6f-8f21-5c0b7e1d4a93
artifact_type: plan
artifact_status: PROPOSED
---
```

Rules:

- `artifact_uuid` is a UUIDv4, minted once when the document is created and never changed after.
  Generate one with `python -c "import uuid; print(uuid.uuid4())"`.
- `artifact_type` is one of `plan`, `review`, `investigation`, `implementation_report`, `handoff`,
  and must agree with the directory route.
- `artifact_status` uses the lifecycle for that type. The value is the state and nothing else —
  commentary goes in the body.
- Relationship keys (`implements`, `reviews`, `informs`, `supersedes`, `about`, `todos`) are optional
  and remain explicit declarations.

Lifecycle vocabulary per type:

| Type | States | Initial |
|---|---|---|
| `plan` | `PROPOSED` → `REVIEWED` → `APPROVED` → `IMPLEMENTING` → `IMPLEMENTED` | `PROPOSED` |
| `review` | `OPEN` → `COMPLETE` | `OPEN` |
| `investigation` | `OPEN` → `COMPLETE` | `OPEN` |
| `implementation_report` | `DRAFT` → `READY_FOR_REVIEW` → `REVIEWED` → `COMPLETE` | `DRAFT` |
| `handoff` | `OPEN` → `COMPLETE` | `OPEN` |

`SUPERSEDED` and `DEFERRED` are reachable from any state of any type. They are not the same thing:
superseded means a better answer exists, deferred means we chose not to answer yet.

Documents that predate this contract are grandfathered. Reconciliation reads their prose `Status:`
header where it can and reports the rest as unresolved; it never rewrites a document to comply.

## Move, copy, replace

These are three different acts and the difference is the UUID.

- **Move** — same document, new location. Keep the UUID. The locator changes; identity, lifecycle,
  and every relationship survive.
- **Copy** — a new document that started from an old one. Mint a **new** UUID before committing.
  Two files declaring one UUID fails validation, and for good reason: nothing on disk could say
  which was the original.
- **Replace** — a new document that makes an old one obsolete. Mint a new UUID, then call
  `supersede_artifact`. Moving the old file into `archive/` is not a replacement and does not record
  one.

## What needs an MCP call and what does not

Routine file moves need **no** MCP call. The file-event hook picks up renames from supported tools,
and the next corpus audit catches everything else — a shell `mv`, `apply_patch`, an IDE refactor, a
branch switch.

Call MCP for the things a detector is forbidden to infer:

| Change | How |
|---|---|
| Lifecycle status | `transition_artifact` |
| Replacement | `supersede_artifact` |
| Relationships | `link_artifacts`, or a frontmatter declaration |
| A move the detectors could not resolve | `relocate_artifact_source` |
| Checking corpus parity | `audit_artifact_corpus` (read-only) |

## Archiving

An archive move requires an explicit terminal lifecycle decision **in the same reviewable change**.
The path does not choose between `IMPLEMENTED`, `SUPERSEDED`, and `DEFERRED`, and the auditor will
report an archived record whose status is still non-terminal as a contradiction rather than guessing
which one you meant.

So, to archive:

1. Decide the terminal state and apply it with `transition_artifact`.
2. Move the file under `.agent/archive/<kind>/`. Keep the UUID.
3. Remove it from its old index and repair any live referrers.

To restore, reverse it: move the file back, keep the UUID, re-list it in the index, and set a
non-terminal status if it is genuinely executable again.

## Moving to reference

`.agent/reference/` holds material that remains useful but authorizes no implementation. A move
there must:

1. keep the UUID and declare the artifact's existing type explicitly (the reference route has no
   default type);
2. state in the document why it is still useful and who consumes it;
3. remove executable ownership from the plan indexes.

It does not retype the artifact.

## Indexes

Every non-archived document must be reachable from its directory's `README.md`:

| Destination | Index to update |
|---|---|
| `.agent/plans/` | [`../plans/README.md`](../plans/README.md) |
| `.agent/plans/backlog/` | [`../plans/backlog/README.md`](../plans/backlog/README.md) |
| `.agent/reviews/`, `.agent/handoffs/`, `.agent/for-review/` | the README in that directory, if it has one |
| `.agent/reference/` | [`../reference/README.md`](../reference/README.md) |

Archive directories are exempt — an index listing ninety archived plans is a directory listing with
extra steps.

## Before you commit

```bash
menhir artifacts validate . --repository menhir
```

It checks metadata syntax, UUID shape, type/status compatibility, route/type agreement, duplicate
UUIDs across the corpus, H1 presence, and index membership. It reads no database and writes nothing,
so it works in a clone with nothing running.

To see how your change looks to the graph:

```bash
menhir artifacts audit --repo . --repository menhir
```

Also read-only. It reports what reconciliation *would* do and prints a plan digest; applying
anything requires that digest and an operator running
`menhir artifacts reconcile --repository <name> --apply`.

## Examples

**Plan**

```markdown
---
artifact_schema: 1
artifact_uuid: 3f7c1e28-9a4b-4d6f-8f21-5c0b7e1d4a93
artifact_type: plan
artifact_status: PROPOSED
---

# Bounded retry budget for enrichment

## Decision
...
```

**Review of a plan**

```markdown
---
artifact_schema: 1
artifact_uuid: 8b2d4a10-6c31-4e77-9a05-2f8e6b1c7d40
artifact_type: review
artifact_status: OPEN
reviews: 3f7c1e28-9a4b-4d6f-8f21-5c0b7e1d4a93
---

# Review: bounded retry budget
```

**Handoff**

```markdown
---
artifact_schema: 1
artifact_uuid: c41f9e73-0d58-4b22-8e6a-71b3d5a08f92
artifact_type: handoff
artifact_status: OPEN
informs: 3f7c1e28-9a4b-4d6f-8f21-5c0b7e1d4a93
---

# Handoff: retry budget instrumentation
```

**Implementation report**

```markdown
---
artifact_schema: 1
artifact_uuid: 5a90c2b6-31e4-4f08-b7d9-6c2a4e81f035
artifact_type: implementation_report
artifact_status: DRAFT
implements: 3f7c1e28-9a4b-4d6f-8f21-5c0b7e1d4a93
---

# WRAPUP: bounded retry budget
```

**Reference record** — type is required here, and the usefulness note is not decoration:

```markdown
---
artifact_schema: 1
artifact_uuid: 9e13b7c4-8a25-4d61-bf03-4c7e2a950d68
artifact_type: investigation
artifact_status: COMPLETE
---

# Retry-budget ablation: negative result

Kept because it records why per-episode retry caps were rejected; consumed by the enrichment
reliability plan.
```

The fixtures in `tests/test_artifact_reconciliation.py` are the executable version of these
examples. There is deliberately no second template set with a different schema.
