# P4 pilot: promoting a snapshot into a restored production graph

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P4)
Design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md` (build order, step 4)
Date: 2026-09-20

## Why this run exists

Every P4 test before this one ran against an empty throwaway database. "A snapshot write cannot
disturb local scanning" was true there and cheap, because there was nothing to disturb.

This ran against a restored production dump that already contained menhir's own local structure,
under the same project name and the same relative paths the snapshot carries. A same-named
collision at real scale is where the separation either holds or does not.

## Setup

| | |
| --- | --- |
| Dump | `backups/prod-neo4j-export-20260820_232038.jsonl.gz` (55,693 nodes / 142,731 rels, no embeddings) |
| Target | throwaway container `menhir-p4-pilot`, bolt on `127.0.0.1:7690`, removed afterwards |
| Production | never contacted; the importer refuses the dump's own source URI outright |
| Snapshot source | the real `menhir` working tree, scanned by the real `ProjectScanner` |

**The backup had never been restored before.** `scripts/export_graph_backup.py` says the format is
"restorable by a straightforward importer" and no importer exists in the repo. One was written for
this run (kept out of tree; see Gaps). The restore reproduced the manifest exactly — 55,693 nodes
and 142,731 relationships in 39.4s — which is the first evidence that these dumps are recoverable
at all.

## What was exercised

Scan of the live repo: 1,424 files, 18,489 symbols, 117 directories, `partial_index` false, 27.2s.

1. Two promotions of that scan into the restored graph (20,183 nodes and 35,166 edges each).
2. `restore_previous` at full scale.
3. `sweep_view_roots` after expiring the leases.
4. Local structure reads, with a live snapshot present and again after removing it.

## Results

### The local graph did not move

| Measure | Baseline | After two promotions | After restore + sweep |
| --- | --- | --- | --- |
| `menhir` local structure rows | 14,267 | 14,267 | 14,267 |
| digest over those rows | `bf3df2bc119a0d56` | `bf3df2bc119a0d56` | `bf3df2bc119a0d56` |
| `:Entity` nodes | 50,742 | 50,742 | 50,742 |
| relationships | 142,731 | 213,063 | 142,731 |
| total nodes | 55,693 | 96,062 | 55,696 |

The digest covers path, role, name, identity field, mtime and content for every local row, so a
merge onto one of them or a prune of one would move it. It did not move.

The final `+3` nodes are 2 `ViewRoot` and 1 `CanonicalView` — P4's own bookkeeping, and nothing
else.

### The collision was real, not hypothetical

* **13,972 structure paths existed in both graphs at once** — the snapshot's rows and the local
  scan's rows, same project, same paths.
* **0 nodes carried both labels.** The separation is structural, so a shared path is two distinct
  nodes rather than one contended node.

### Local reads returned identical answers

Seven real `query_*` methods were run with 20,183 snapshot nodes live, then the snapshot was
deleted and the same queries were run again. All seven digests matched, including `symbols_all` at
2.75 MB of result:

`overview`, `files`, `files` (filtered), `dependencies`, `tests`, `symbols_all`, `imports`.

This is the part the data digest alone does not cover: the digest proves nothing was mutated, this
proves nothing was *read across*.

### Timings at scale

| Operation | Time |
| --- | --- |
| Restore whole dump | 39.4s |
| Scan the live repo | 27.2s |
| First promotion (20,183 nodes, 35,166 edges) | 24.6s |
| Second promotion, same size | 6.2s |
| `restore_previous` | 0.11s |
| Sweep: retire + purge 20,183 nodes | 1.9s |

The pointer flip and the rollback are effectively constant-time regardless of snapshot size, which
is the property the design was built for: compensation is a pointer move, not a traversal.

The 24.6s → 6.2s difference between identical promotions is page-cache and index warming, not a
behavioural difference. It has not been investigated further.

## Gate items

| Gate item | Status here |
| --- | --- |
| deletion has a tested outcome | Yes — covered offline; promotion never prunes, so deletion needs no delete |
| failed write has a tested outcome | Yes — offline |
| process kill at every state | Yes — offline, per state |
| failed compensation has a tested outcome | Yes — offline; degrades the view durably |
| restore from `previous` | Yes — offline and here at scale |
| no name-keyed prune remains | Yes — the promotion path contains no prune at all |

## Gaps and honest limits

1. **Nothing schedules the sweep.** `sweep_view_roots` is tested and proven but has no caller in
   the product. Until it is wired into the maintenance scheduler, abandoned roots accumulate on a
   live deployment. Scheduling belongs with enabling `write` mode.
2. **No importer ships.** The one written for this run lives outside the tree. menhir can export a
   backup and cannot restore one, which means recoverability rests on a script that exists only in
   a scratch directory. This is worth closing independently of P4 — it is a backup problem, not a
   snapshot problem.
3. **One residual in the promotion path is unclosed by design.** A process killed between the flip
   and the compensation leaves the view serving a root a caller had already judged bad, with
   nothing recording that judgement. Narrowed by marking the view degraded when compensation
   fails; not closed, because the *intent* to compensate is not durable. Closing it needs a
   promotion-attempt record and a reconciler.
4. **This dump predates the identity work.** Its `:Entity` rows carry no `structure_project_id`,
   so the pilot did not exercise interaction with CF-257 identity binding on populated data.
5. **One project, one shape.** The pilot promoted menhir into a graph containing menhir. It did
   not test many projects promoting concurrently, nor a project whose local and snapshot identities
   disagree.
6. **Reads do not yet surface snapshot status.** The parent plan asks for `[SNAPSHOT ...]` in
   structure queries; nothing reads a published view yet, which is why "local reads are unaffected"
   is currently easy to satisfy.
