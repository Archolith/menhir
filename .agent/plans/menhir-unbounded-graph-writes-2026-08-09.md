# Unbounded Graph Writes

Status: **planned; implementation not started**
**Last verified:** 2026-08-18 — ACCURATE, not started. Inverse test: no graph-write bound exists. `sync_edge_counts`/`compose_episode_body`/`edge_count` are the UNBOUNDED code the plan indicts, not its output; the only `MAX_EDGES` in `src/` is `domain/scalar_dependency_evidence.py:19`, an unrelated evidence-payload cap.


Two independent writes that scale with data and have no bound. Neither is a mystery — both are
documented in the codebase as known limits — so this is a design note, not an RCA. What was missing
in both cases was a measurement saying the limit had been reached. This supplies it.

## A. `sync_edge_counts` recounts every entity in one transaction

`infrastructure/consolidation_queries.py:45-67` runs, as a single statement with no batching:

```cypher
MATCH (n:Entity)
OPTIONAL MATCH (n)-[r]-()
WHERE NOT type(r) = 'ANCHORED_TO'
WITH n, count(DISTINCT r) AS edge_count
SET n.edge_count = edge_count
```

Its own docstring states the limit:

> Scaling note: this touches every Entity node in a single transaction. Fine for personal-scale
> graphs (hundreds to low thousands of nodes). If the graph reaches tens of thousands of densely-
> connected entities, consider migrating to `CALL {} IN TRANSACTIONS` for batched writes.

**The graph now holds 61,787 entities** (`get_memory_stats`, 2026-08-09) — an order of magnitude
past the stated trigger. The condition the author wrote the note for has arrived.

It is not a one-off. It runs at startup (`core/bootstrap.py:304`) and on **every decay sweep**
(`services/lifecycle_decay.py:253`), so a full-graph write-transaction recurs on the maintenance
schedule.

Risk is transaction memory and lock duration on the remote Neo4j (`bolt://prod.example.internal:7687`),
which is shared with the in-flight benchmark work. A long single transaction over 61k nodes plus
their relationships is the kind of thing that shows up as unexplained latency elsewhere rather than
as a clean failure.

### Plan

1. **Measure first.** Time one `sync_edge_counts` against the live graph and record transaction
   duration and peak memory. If it completes comfortably, this is a watch item rather than work —
   but record the number, because "61k entities" is the measurement that made it actionable and the
   next reader deserves the same courtesy.
2. **Migrate to batched writes** if the measurement warrants:
   ```cypher
   MATCH (n:Entity)
   CALL { WITH n
     OPTIONAL MATCH (n)-[r]-()
     WHERE NOT type(r) = 'ANCHORED_TO'
     WITH n, count(DISTINCT r) AS edge_count
     SET n.edge_count = edge_count
   } IN TRANSACTIONS OF 5000 ROWS
   ```
   Note this changes atomicity: the sweep becomes incrementally visible rather than all-or-nothing.
   That is acceptable here — `edge_count` is a derived cache recomputed on a schedule, not a
   correctness invariant — but it should be a stated decision, not a silent consequence.
3. **Update the docstring** with the real measured ceiling instead of a prospective one.

Acceptance: sync completes in bounded transactions; measured duration recorded before and after;
decay-sweep behaviour otherwise unchanged.

## B. The raw diff is persisted to Neo4j untruncated

`infrastructure/episode_lifecycle.py:149` writes the caller-supplied `diff` straight onto the
`:Episodic` node. Nothing upstream clamps it — `core/backend_runtime_data_ops.py:41` passes it
through verbatim from the MCP boundary.

The guard that exists is on a **different** path. `services/enrichment_steps.py:157`:

```python
MAX_DIFF_CHARS = 50_000
```

bounds `compose_episode_body`, i.e. the text sent to Graphiti for extraction. That protects the LLM
context and token spend. It does not protect the stored property, so a 5 MB diff is truncated for
enrichment and stored in full on the node.

This was recorded in `.agent/memory-backlog.md` as "auto-summarize/truncate large diffs before
Neo4j storage (no size guard currently)". On 2026-08-09 that entry was corrected from "done" to
half-done: the enrichment half landed in `99c9743`, the storage half never did. The original
wording was accurate all along.

Consequences: Neo4j string-property size limits, node bloat, and every read of that episode paying
for the full diff.

### Plan

1. **Bound the stored property** at the write boundary, not per call site — the same chokepoint
   lesson as the namespace work. `create_episode` is where the value lands; clamp there so any
   future caller inherits it.
2. **Pick the limit deliberately.** `MAX_DIFF_CHARS = 50_000` is calibrated for LLM context, which
   is the wrong basis for a storage bound. Decide a storage limit on its own terms and name it
   separately rather than reusing the enrichment constant for a different purpose.
3. **Truncate visibly.** Append an explicit marker, as `compose_episode_body` already does
   (`"\n... [diff truncated]"`), so a reader can tell a clipped diff from a short one.
4. **Decide the over-limit policy explicitly:** truncate silently, truncate with a marker, or
   reject the write. Truncate-with-marker is the least surprising and matches existing behaviour.

Acceptance: a diff larger than the storage limit is stored clamped and marked; the enrichment path
is unchanged; a regression test covers a diff exceeding both limits and asserts each path applies
its own bound.

## Shared note

Both are the same shape: a bound was reasoned about correctly, documented honestly in the code, and
then either never revisited (A) or applied to only one of two consumers (B). Neither needed
detection work — both were written down years before they mattered. What was missing was anything
that checks a documented limit against the live system and says "this one is now true."

Worth considering separately: a periodic check that asserts documented scaling limits against
current graph statistics, so a docstring that says "revisit at tens of thousands" produces a signal
when the graph crosses it.
