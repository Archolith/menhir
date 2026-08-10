# RCA: a structure project node disappeared between two writes

**Date:** 2026-08-09

**Severity:** Low as observed, unknown as a class. One project silently lost its `root_path`
and therefore its staleness checkability. It self-healed on re-ingest, and no data was lost.
The reason it was written up despite being small is that the mechanism was initially unidentified,
so the blast radius could not be bounded.

**Status:** RESOLVED. Graphiti semantic deduplication selected a structural `:Entity` as a
semantic candidate, untyped hydration replaced its attributes with `{}`, and Graphiti's
whole-map save removed the structure-owned properties. Menhir now excludes structural candidates
and preserves existing attributes for untyped hydration.

## Summary

During a batch of 27 `ingest_project` calls, the sample child-project entity was written at
21:54:43 and did not exist at 21:56:54, when the sample umbrella scan created a fresh one in
its place. The replacement carried no `root_path`, so the child project moved from STALE (root
recorded, directory gone) to NEVER SCANNED (no root recorded) — a strictly worse state, because
a node with no recorded root cannot be checked against disk at all.

Re-ingesting the child project after the umbrella restored `root_path` onto the same node. The graph
is currently correct.

## Impact

| | |
|---|---|
| Projects affected | 1 of 27 ingested in that window |
| Entities lost | 0 — only the project node's `root_path` property |
| Detection | `query_structure("projects")` listing, by inspection |
| Recovery | one re-ingest |
| Currently reproducible | no |

The wider concern is that the staleness checker added earlier the same day reads exclusively
from project nodes. A defect that silently removes or replaces a project node disables the
detector that would otherwise report the project as unverifiable, and does so quietly.

## Timeline

All times local (UTC-5). Entity `created_at` is stored in UTC; the two are offset by 5 hours.

| local | event |
|---|---|
| 21:53:53 | `write_project` for the sample child project begins (`now` stamped on all its entities) |
| 21:54:43 | Structure write completes for the child project: 124 entities, 153 edges |
| 21:55:11 | Graphiti enrichment of the child-project episode runs; identity-gate vetoes logged |
| 21:56:54 | The sample umbrella `write_project` begins; **creates** a child-project node |
| 21:56:58 | Structure write completes for the sample umbrella |
| 21:58:49 | manual re-ingest of the child project; `root_path` restored on the 21:56:54 node |

The 124-entity count matches the scan exactly (5 dirs + 15 files + 6 deps + 2 endpoints +
95 symbols + 1 project), confirming the project entity was written at 21:54:43. Both
`_write_calls_edge` and `_write_contains_repo_edge` set `created_at` under `ON CREATE` only, so
the 21:56:54 stamp on the surviving node means a genuine node creation, not a property update.

Concurrency in this window was heavy: the `SCANNER_SCHEMA_VERSION` 4 → 5 bump had invalidated
every stored fingerprint, so the scheduler's structure watcher was re-scanning all projects while
the manual ingests ran. Unrelated writes for two neighboring projects interleave with the sequence
above.

## Hypotheses eliminated

### 1. The umbrella writer overwrote the child — REJECTED

Tested directly against the live database with throwaway names: wrote a child project carrying a
`root_path`, then wrote an umbrella declaring it as a nested repo, then re-read the child.

Result: `root_path` and `created_at` both unchanged; the umbrella created its own node and left
the child alone. Consistent with the code — both `_write_calls_edge` and
`_write_contains_repo_edge` use `MERGE ... ON CREATE SET` exclusively and have no `ON MATCH`
clause touching the target node, so they cannot overwrite an existing one.

This was the working hypothesis reported before the test, and it was wrong. Ingest order is not
the hazard.

### 2. The correlation service absorbed the node in a merge — REJECTED

`check_ineligible_node_veto` in `correlation_queries.py` makes any node with
`structure_role IS NOT NULL` merge-ineligible, and the veto is evaluated before every merge.
No `merge_audit` entry anywhere in the graph mentions the child project.

Noted for a separate thread: 8 structure nodes currently carry a `merge_audit` property, so that
veto has not always been in force or has not always held. Not investigated here.

### 3. Normal enrichment deletes structure nodes — INITIAL PROBE INCONCLUSIVE

A scratch project was ingested through the running server, exercising the full path including the
semantic episode and Graphiti enrichment. Its project node was polled every 5 seconds for 5
minutes.

Result: created at t+5s, present and unchanged at every subsequent sample. Probe entities and the
temporary directory were removed afterwards. This did not exercise the later-confirmed failure
condition: a semantic candidate search must select the structural node and then run the untyped
attribute replacement-save path.

## Additional verified structural defect

**There is no uniqueness constraint on `:Entity`** — none on `(structure_project, structure_path)`,
and none on any structure property. Confirmed via `SHOW CONSTRAINTS`. Every structural node's
identity rests on bare `MERGE`.

Neo4j only guarantees `MERGE` will not duplicate when a uniqueness constraint backs the matched
pattern. Without one, two concurrent transactions matching the same pattern can both create.
Two different MERGE shapes are in use against the same logical node, which widens the window:

- `_merge_entity` merges on two keys: `{structure_project, structure_path}`
- `_write_calls_edge` and `_write_contains_repo_edge` merge on three:
  `{structure_project, structure_path, structure_role}`

This is consistent with the symptom — the umbrella creating a second child-project node while the
original still existed, with the listing then reading whichever node it happened to match. It is
**not proof**. No duplicate `(structure_project, structure_path)` pairs exist in the graph now, so
under that theory something must also have removed the original, and that remains unexplained.

## Root cause

Graphiti's semantic candidate collection admitted Menhir structure nodes because both structures
and semantic memories use the generic `:Entity` label. When deduplication selected a structural
node, untyped attribute extraction returned `{}` and Graphiti persisted the candidate with
`SET n = node`. That whole-map replacement retained the UUID and relationships while stripping
`structure_project`, `structure_path`, `structure_role`, and `root_path`, making the node appear to
have vanished from structure queries.

The fix filters candidates with `structure_role` before semantic resolution and, as defense in
depth, preserves a copy of existing attributes whenever no typed attribute schema exists.

## Follow-up hardening

1. Add a uniqueness constraint on `(structure_project, structure_path)` for `:Entity`, after a
   duplicate sweep. This is worth doing on its own merits regardless of this incident, but it is
   a schema change against a live database and needs an explicit decision.
2. Standardise the MERGE key. `_merge_entity` and the two edge writers should agree on the
   identity of a project node rather than using two- and three-key variants of it.
3. Add an assertion at the end of `write_project`: re-read the project node and confirm it
   carries the `root_path` just written. That converts a silent property loss into a log line
   with a timestamp, which is the evidence this investigation lacked.
4. If it recurs, capture the graph state at the moment of loss rather than reconstructing it
   afterwards. Every conclusion above had to be inferred from timestamps because nothing recorded
   the deletion.

## Corrections to earlier claims in this session

- **Timezone.** Earlier analysis in this session assumed the host was UTC-7 when comparing entity
  `created_at` (UTC) against server logs (local). The host is **UTC-5**. Two conclusions about
  which run wrote which entities were wrong as a result, including the initial reading of the
  umbrella endpoint history.
- **Ingest ordering.** Reported as the likely mechanism before it was tested. Hypothesis 1 above
  disproves it.
- **The 62 edgeless `archolith` endpoints**, described earlier as unexplained with a Neo4j deadlock
  as a suggestive but unproven cause, are explained by the corrected timezone: that write began at
  14:15:48 local and the server crashed at 14:43:47, before step 10 (`EXPOSES`) ran. No
  `Structure write complete: project=archolith` line exists between 13:59 and 14:47. The
  underlying defect there is that `write_project` is 13 sequential transactions with no
  atomicity and no reconciliation of partial writes — tracked separately from this RCA.
