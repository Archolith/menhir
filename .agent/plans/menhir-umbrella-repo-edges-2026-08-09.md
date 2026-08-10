# Umbrella Repo Containment Edges

Status: **design note; small addition**

Follows: `d88cee9` (nested git repos are scan boundaries).

## Why

Making nested repos a scan boundary fixed the duplication — `archolith` went from absorbing
10,367 files across 11 child repos to walking 425 — but it threw away a true fact in the process.
The umbrella now knows nothing about its children. `archolith` is 8 files and no relationships,
when the structurally interesting thing about it is precisely that it *contains* menhir, beacon,
archolith-bench, and eight more.

Skipping is right for **files**; it is wrong for **structure**. The boundary should record the
relationship it declines to descend through.

## Design

When the walk prunes a directory for being its own repository, record it rather than discarding
it, and write a project→project edge.

- **Scanner**: `ProjectScanResult.nested_repos: list[NestedRepo]` where `NestedRepo` carries
  `rel_path` (umbrella-relative) and `name` (directory basename).
- **Name matching**: `ingest_project` defaults a project's name to its directory basename
  (`name or root.name`), so the basename is already the child's graph identity. No new resolution
  rule; `archolith/menhir` → project `menhir`.
- **Writer**: `CONTAINS_REPO` edge, umbrella project entity → child project entity, following the
  existing `_write_calls_edge` pattern — which already MERGEs a target project entity that may not
  be ingested yet. An umbrella can therefore be scanned before its children and the edges still
  land; the child fills in its own detail when scanned.

### Why a new edge type

`CONTAINS` is project→directory→file, a within-repo containment chain, and `blast_radius` and
friends walk it. Reusing it for project→project would let those traversals cross a repository
boundary — reintroducing the coupling the boundary exists to prevent, in edge form instead of
entity form. `CONTAINS_REPO` is a separate, non-traversed relation.

`CALLS` is wrong too: it means a runtime/import dependency (`mechanism: http|import|shared_db`).
Containment is not a dependency — an umbrella does not call its children.

## Scope

In scope: detection during the walk, the result field, the edge write, and surfacing the edge in
`overview`.

Out of scope:

- Inferring dependencies *between* siblings. That is what `cross_project_refs` already does, on
  evidence; containment implies nothing about it.
- Auto-ingesting children when an umbrella is scanned. Tempting, but it makes one tool call fan out
  into arbitrarily many background writes with no budget. Discovery should be reported, not acted
  on.
- Any change to how a repo scanned directly behaves.

## Acceptance

- Scanning `archolith` produces 11 `CONTAINS_REPO` edges and still walks ~425 files.
- Edges land whether or not the child is already ingested.
- No `CONTAINS_REPO` edge is traversed by `blast_radius` / `affected_tests`.
- Scanning a child directly is unchanged.

## Risks

- **A vendored third-party checkout is also a nested repo**, and would be recorded as a contained
  project. That is arguably correct — it *is* a repo inside this one — but it will surface names
  that are not workspace projects. Acceptable: the edge is a factual observation, and a stub
  project entity with no files is visibly different from an ingested one.
- **Stale edges** if a child directory is removed. The write path MERGEs and does not prune
  `CONTAINS_REPO`; a removed child leaves a dangling edge until something clears it. Noted, not
  handled here.
