# R2 — Facet candidate generation (bench-first)

**Date:** 2026-06-27
**Status:** PLANNED — bench-first rung. **No menhir production change lands from this rung** until the
benchmark shows a measurable win. Implementation + fixture live in `archolith-bench`.

> **Status note 2026-08-08 (curator audit).** One correction: a `FacetCandidateSource` seam
> (`src/menhir/domain/facet_candidate_source.py`, `facets.py`) already exists in menhir itself —
> not purely bench-side as stated above — but it is dormant, not referenced by
> `recall_service.py`/`scoring_service.py`. This is the "Production CandidateSource.FACET seam
> reserved" mentioned in `menhir-research-execution-ladder.md`. Doesn't change the verdict: still
> bench-first, no live production recall change.
**Rung:** R2 in [`menhir-research-execution-ladder.md`](../../research/menhir-research-execution-ladder.md) (`depends_on R1`).
**Owner (mechanism):** [`facet-retrieval.md`](../../../docs/research/retrieval/facet-retrieval.md).
**Bench owner:** [`archolith-bench-operational-model.md`](../../../docs/research/process/archolith-bench-operational-model.md).

## Why

R2 is not a production recall rewrite. Its purpose is to test, before adding any production surface,
whether deterministic facet retrieval improves recall behavior.

**Hypothesis:** facet-first candidate generation + meet-point reranking improves paraphrase
stability and reduces stale / wrong-scope memory injection, compared with embedding-only, BM25,
hybrid (R1), and existing graph/file-context recall.

R2 produces *evidence*, not a feature. The research doc already gives the mechanism; this rung makes
it falsifiable.

## Scope

**In:**
- A reproducible benchmark fixture for facet retrieval (hand-authored memories first).
- Minimal benchmark-local `MemoryFacetSet`, `FacetExtractor`, `MemoryFacetIndex`, `MeetPointReranker`.
- Comparison against honest baselines (BM25 / embedding top-k / BM25+embedding / existing graph-file
  context / facet+embedding rerank / facet+meet-point rerank).
- Captured metrics, raw outputs, traces, failure notes.

**Out (explicitly):**
- Production recall integration.
- Durable graph schema changes.
- New MCP/API surfaces.
- Full code-structure identity modeling; Git/Structure Time Join.
- BeliefLayer gating (except as a later bench condition G).
- LLM-heavy extraction as the default path.

## Proposed Design

A benchmark fixture in `archolith-bench` containing:
- 50 hand-authored memories; 20 queries; known support memory IDs per query.
- Stale distractors; wrong-repo distractors; a symbol-rename case; ≥1 vague query where embedding is
  *expected to beat* facets.
- Each memory carries **both** raw text and explicit facet labels, so the first run doesn't depend on
  extractor quality.

**Minimum facet set:** `actor, object, operation, file, symbol, test, valid_time, learned_time,
evidence_type, source_id, repo, project, namespace, belief_bucket`.

**Two facet modes** (separating two questions):
1. *Gold facets* — hand-authored, used directly → "do facets help if correct?"
2. *Extracted facets* — simple deterministic rules → "can a cheap extractor recover enough?"

**`MemoryFacetIndex`** — deterministic candidate generator: returns candidates by *compatible facet
overlap*, not semantic similarity.

**`MeetPointReranker`** — scores convergence across shared support structure:
```
meet_score =
    weighted required-facet overlap
  + file/symbol/test convergence
  + evidence/source convergence
  + time-window compatibility
  - stale/superseded penalty
  - wrong-scope penalty
```
Every candidate emits an **explanation trace**: which facets matched, which penalties fired, why it
ranked where it did. (Determinism + explainability are invariants, not extras.)

## Alternatives Considered

- **A: build R2 directly in menhir production recall.** Rejected — risks architectural drift before
  the retrieval claim is proven.
- **B: design note only.** Rejected — insufficient; R2 must produce evidence.
- **C: bench-first with a JSON fixture + benchmark-local implementation.** **Chosen** — fastest
  falsifiable path, production recall untouched until the mechanism beats honest baselines.

## Risks (and mitigations)

- Facets may look good only because the fixture is too clean → include vague queries where embedding
  should win; preserve raw outputs + failure notes, not just scores.
- Hand-authored facets may overstate real extractor performance → keep gold-facet and extracted-facet
  results **separate**.
- Meet-point scoring may overfilter sparse-but-relevant memories → report recall/precision/stale-hit/
  wrong-scope/support-sufficiency **together**.
- Facets may cut wrong-scope hits while hurting broad exploratory recall → same combined reporting.
- A deterministic-looking match can create false confidence → require the explanation trace.

## Invariants

- R2 does **not** replace R1 hybrid retrieval.
- R2 does **not** promote production behavior without benchmark evidence.
- R2 does **not** claim novelty, and does **not** treat top-k stability as correctness.
- R2 must preserve enough recall to stay useful for debugging.
- R2 must be deterministic and explainable.

## Validation

**Bench conditions:**
```
A  BM25
B  embedding top-k
C  BM25 + embedding
D  existing graph/file-context retrieval
E  facet index + embedding rerank
F  facet index + meet-point rerank
G  facet index + meet-point rerank + BeliefLayer gates   (later only)
```

**Primary metrics:** `recall_at_5, precision_at_5, MRR, NDCG, paraphrase_stability, stale_hit_rate,
wrong_scope_injection_rate, support_sufficiency, false_neighbor_rate, answer_grounding_accuracy,
latency_ms`.

**Promotion gate:** R2 graduates only if **facet index + meet-point rerank (F)** improves
`stale_hit_rate`, `wrong_scope_injection_rate`, or `support_sufficiency` against BM25, embedding, and
hybrid baselines **without an unacceptable recall loss**.

## Deliverables

1. This design note (`.agent/plans/`). ✅
2. Benchmark fixture in `archolith-bench`.
3. Benchmark-local facet implementation (`MemoryFacetSet` / `FacetExtractor` / `MemoryFacetIndex` /
   `MeetPointReranker`).
4. Run artifacts: config, metrics, raw outputs, traces, notes.
5. Short research report: does R2 move toward menhir production integration?

## Decision

R2 begins as a **bench-first** rung. Menhir production integration is deferred until the benchmark
shows a measurable win.

## Connection to R1 (this repo)

R1 already reserved the seam: `CandidateSource.FACET` exists in
`src/menhir/domain/retrieval_tuning.py`, with `SOURCE_PRIORS` / `FLOOR_EXEMPT_SOURCES` slots ready.
**Do not wire facet into production recall as part of R2** — that wiring is the *post-graduation*
step, gated on the promotion criterion above. The reserved slot just means integration won't require
touching the source/prior taxonomy again.

> Repo-scope note: deliverables 2–4 live in `archolith-bench`, which is outside the current session's
> GitHub scope and needs the home bench environment. They are tracked in
> [`deferred-verification.md`](deferred-verification.md) until they can be built and run.
