# menhir — semantic embedding for the structural code graph

> **ARCHIVED 2026-08-10.** The todo-motivated proposal was superseded by
> [`menhir-todo-declared-links.md`](menhir-todo-declared-links.md). Its measurements and
> separate-embedding design are retained as evidence for any future semantic-code-search proposal.

Status: **SUPERSEDED** by `menhir-todo-declared-links.md` (2026-08-02) *as a todo-motivated
project*. Its justification was CONCERNS scoring, and CONCERNS turned out to have no
consumer. The idea may still stand on its own for semantic code search, `query_structure`
and `blast_radius` — if so, re-propose it on those merits. The findings below are worth
keeping either way: the separate `structure_embedding` property that answers the
documented "would pollute the vector space" objection, and the 20,925 memory-to-structure
edges that rule out a separate store.
Date: 2026-08-02
Related: `menhir-todo-concerns-semantic-relevance.md` (the problem that surfaced this)

## Premise

50,701 of 58,005 PERSISTENT entities carry no embedding. They are the code graph —
`symbol` 34,111, `directory` 8,754, `file` 5,165, `test` 1,570, `endpoint` 426 —
written by `ingest_project` in direct Cypher, bypassing Graphiti enrichment.

Embedding them would give the structural half of the graph semantic reach it does not
have today: recall, `blast_radius`, `query_structure`, and CONCERNS matching all
currently treat these nodes as opaque strings.

## This contradicts an existing decision — read first

`infrastructure/embedding_dimensions.py` states it plainly:

> STRUCTURAL nodes (code files/dirs/symbols from the structure scanner, identified by
> `n.structure_role`) are a path/symbol index and are **intentionally never embedded**
> — they must be excluded here, or health reports a false problem and **a backfill
> would pollute the vector space**.

The health check already excludes `structure_role IS NOT NULL` from its null-embedding
count. So this plan proposes reversing a decision that is documented and enforced in
code. The stated objection is specific and correct: if 50,701 structural vectors land
in the same space memories occupy, semantic recall starts returning file paths and
function names where it used to return knowledge.

**Any version of this that writes to `name_embedding` is wrong.** That is the property
the memory vector index and the health check both use.

## Proposed resolution — a separate vector space

Write structural vectors to a distinct property, e.g. `structure_embedding`, never
`name_embedding`.

Consequences, all desirable:

- Memory recall is untouched. Nothing new competes in that space.
- The health check keeps excluding structural nodes from `name_embedding` nulls and
  needs no change to stay correct.
- Structural similarity becomes a deliberate, separately-queried lane rather than an
  implicit contaminant.
- The original objection ("pollute the vector space") is answered structurally rather
  than argued away.

## What to embed — the unit matters more than the decision to embed

Embedding a bare name is close to worthless. `delete` as a token cannot disambiguate
the 4 `delete` symbols, and `main` is shared by 300 nodes. The generic-name problem is
a property of the name, not of a missing vector.

Symbol nodes already carry everything needed for a discriminative text, so **no
scanner change is required**:

| Field | Example |
|---|---|
| `structure_path` | `tests/test_delete_coordinator_live.py::test_crash_before_delete...` |
| `symbol_signature` | full call signature |
| `symbol_kind` | function / class / method |
| `symbol_parent` | enclosing class or module |
| `symbol_decorator` | decorators |
| `structure_project` | owning repo |

Proposed embedding text: qualified path + kind + name + signature (+ parent, decorator
when present). That makes `vps/diagnostics_tools.py::delete_stale_sessions(session_id)`
distinguishable from any other `delete`, which is the whole point.

Verify before building: how populated `symbol_signature` actually is across the 34,111
symbols. The two nodes sampled had it; a sweep should confirm the rate, since a symbol
with only a name falls back to the low-value case.

## Provider and dimension — a hard constraint

Current config:

```
GRAPHITI_EMBED_PROVIDER=openai
OPENAI_EMBED_MODEL=text-embedding-3-small
LOCAL_LLM_EMBED_BASE_URL=http://localhost:8083/v1   # nomic-embed-text-v1.5, llama.cpp CUDA
```

Vectors are only comparable within one model. `embedding_dimensions.py` already tracks
a `mixed` dimension signal, so inconsistency is detectable but not free.

The choice follows from one question: **will structural vectors ever be compared
against memory vectors?**

- **Yes** (e.g. scoring a todo's text against symbol vectors — the CONCERNS use case):
  both sides must use the same model. That means OpenAI `text-embedding-3-small` for
  50,701 nodes, and the run needs costing before it is approved.
- **No** (structure-to-structure similarity only): the local nomic server can own that
  space at zero marginal cost, and dimension divergence is harmless because the two
  spaces are never mixed.

This is the decision that determines the cost of the project, and it should be made
explicitly rather than inherited from whichever provider is configured at the time.

## Sequencing

1. Settle the comparability question above. **Blocking** — it sets the provider.
2. Measure `symbol_signature` coverage across all symbol nodes.
3. Add `structure_embedding` as a distinct property; leave `name_embedding` alone.
4. Backfill behind a flag, one project at a time, starting with a small repo.
5. Make `ingest_project` maintain it incrementally for changed symbols only — it
   already computes `changed_paths` for the mtime-based incremental diff.
6. Only then wire consumers (`query_structure` similarity, CONCERNS scoring).

## Relationship to the CONCERNS noise

This does **not** replace the name-distinctiveness fix proposed in the companion plan.
Distinctiveness is free, deterministic, and lands now; it remains a useful prior even
once structural embeddings exist. This project is larger and independently valuable —
its payoff is mostly in structural recall and `blast_radius`, with CONCERNS as a
secondary beneficiary.

## Risks

- **Reversing a documented decision.** The pollution concern is real; the separate
  property is the mitigation, and it must hold for every write path, not just backfill.
- **Cost, if OpenAI.** 50,701 embeddings plus incremental re-embedding on every scan of
  a changed symbol. Needs a real number before approval.
- **Staleness.** A symbol's vector is wrong the moment its signature changes.
  Incremental maintenance is not optional; without it the space silently rots.
- **Scale of the vector store.** 50,701 additional vectors is roughly 7x the current
  7,771 embedded entities. Index size and query latency should be measured, not assumed.
