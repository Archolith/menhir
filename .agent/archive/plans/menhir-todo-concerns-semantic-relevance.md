# menhir — relevance for :Todo CONCERNS edges

> **ARCHIVED 2026-08-10.** This proposal was superseded by
> [`menhir-todo-declared-links.md`](menhir-todo-declared-links.md) after confirming that
> `CONCERNS` had no consumer. The measurements below remain useful historical evidence.

Status: **SUPERSEDED** by `menhir-todo-declared-links.md` (2026-08-02).
Reason: this plan proposed improving CONCERNS precision, but CONCERNS has no consumer —
nothing in the codebase reads it. Improving an unread edge is not worth doing. The
measured findings below are kept as evidence.
Date: 2026-08-02
Follows: `930a77e` (word-boundary + specificity ranking), `5e015ec` (read ordering)

## Correction

An earlier draft of this plan recommended embedding-based reranking. **That was
wrong for the observed noise**, and the data below is why. Embeddings remain
plausible for a different, smaller problem; they are no longer the proposal.

## What is already solved

`930a77e` removed the mechanical defect: CONCERNS matched entity names as bare
substrings, producing 295 of 989 artifact edges ("remaining" -> `main`). Matching is
now word-boundary, ranked longest-first, deterministic; all 231 todos re-linked;
artifact edges zero.

## What remains

Whole-word matches that are lexically correct but worthless. On `b65e906d`:

```
vps_db_query_custom   <- a blocker the todo names
vps_exec              <- a blocker the todo names
delete                <- noise
```

## Why embeddings cannot fix this

Coverage is not a backlog. It splits by how the node was written:

| Origin | Embedded |
|---|---|
| `claude-code` / `codex` / `opencode` / `claude-chat` (memories) | 99-100% |
| `project-scan` (the code graph) | 1% (610 / 50,701) |
| `symbol` 34,111 / `directory` 8,754 / `file` 5,165 | 0-2% |

Memories pass through Graphiti enrichment and get embedded. Code-structure nodes are
written by `ingest_project` in direct Cypher — the same bypass `:Todo` uses. The 87%
without embeddings *is* the code graph, by design.

Now cross that against what CONCERNS actually links:

| Target kind | Embedded |
|---|---|
| memory entities | 306 / 306 |
| `symbol` | 6 / 174 |
| `directory` | 13 / 109 |
| `file` | 3 / 50 |
| `endpoint` | 0 / 10 |

`delete` is a `symbol` from `project-scan` with no embedding and no path to one.

**The noise lives entirely in the unembedded population.** Embeddings would only
refine the memory half — which is already 100% embedded and already the well-behaved
half. It is the wrong instrument.

## Proposed instead — name distinctiveness (IDF proxy)

Duplicate-name count separates the two populations cleanly, for free:

| Noise | nodes | | Signal | nodes |
|---|---|---|---|---|
| `main` | 300 | | `vps_exec` | 1 |
| `config` | 25 | | `vps_db_query_custom` | 1 |
| `test` | 16 | | `TcgPlayerPriceService` | 1 |
| `delete` | 4 | | `yawn.market` | 2 |

A name shared by 300 nodes carries almost no information about which todo it belongs
to. A name unique in the graph is a near-certain reference.

Design:

1. Candidates — unchanged word-boundary match.
2. Score each candidate by how many PERSISTENT entities share its name.
3. Drop or heavily deprioritize high-count names; keep longest-first as tiebreak.
4. Threshold from data, not intuition. Somewhere between 4 (`delete`) and 16 (`test`)
   on this corpus, but it should be measured across all 231 todos, not fitted to one.

Properties that make this the right fit: no model call, no provider dependency,
preserves the documented ":Todo never queued for LLM processing" bypass, works
identically on the 87% that has no embedding, and is deterministic and testable.

Cost: one aggregation over entity names, cacheable — it changes only when the graph
is re-scanned.

## Where embeddings could still earn a place

A narrower job: among *memory* entities (100% embedded), distinguishing genuine topical
relevance from incidental mention. That is a real improvement but a smaller one, and it
should wait until the distinctiveness filter is in and measured — it may leave little
noise behind.

## Explicitly not proposed

Per-todo LLM adjudication. Most expensive option, breaks the no-LLM property outright,
and makes todo creation depend on a provider being reachable.

## Open questions

1. Threshold for the duplicate-name cut — needs a sweep over all 231 todos, scoring
   precision by hand on a sample.
2. Drop low-value edges, or keep and deprioritize in read output? Dropping is lossy;
   deprioritizing preserves provenance and is reversible.
3. Should `symbol`-role targets be weighted below memory targets independently of name
   count, or does the count alone capture it?
