# Facet-extraction improvement plan (R2 bottleneck)

## Status

supported-by-spike

Weekend roadmap Priority 6 (also a Day-3 deliverable). Owner-adjacent to `facet-retrieval.md` (which
owns the facet mechanism); this owns the *extractor* improvement path. **The hybrid extractor is now
built in the facet bench** (`archolith_bench/facet` `hybrid` mode) and confirms the central claim: on the
real DRAFT fixture it takes F's recall@5 from 0.28 (pure extraction) → 0.83 (hybrid; gold 0.85) and
re-graduates the gate, with stale/wrong-scope at gold levels. Still a DRAFT fixture + stand-in embedder,
so `supported-by-spike`, not `supported-by-eval`. See
`archolith-bench/.agent/benchmark-notes/facet-r2-demo-run.md`.

> **2026-07-11:** the structural-facet decomposition landed — symbol facets are text-improvable
> (extractor snake/SCREAMING_SNAKE rules), but **file facets have recall 0.00 from prose** and require
> the code graph's ANCHORED_TO edges; live coverage is 24.5%. So extraction has a ceiling at file facets;
> the remaining lever is ANCHORED_TO coverage (graph-gated). See
> `archolith-bench/.agent/benchmark-notes/facet-r2-structural-facet-decomposition.md`.

## Why this exists

R2's honest signal (see `archolith-bench/.agent/benchmark-notes/facet-r2-demo-run.md`): **gold facets
help; extracted facets collapse.** The cheap regex/vocab `FacetExtractor` cannot recover facets from
real prose, so condition F fails in `extracted` mode. The extractor — not the retrieval engine — is the
near-term bottleneck. The engineering hypothesis:

> Better facet extraction may improve retrieval **without changing the retrieval engine**.

## The five design questions, answered

**1. Which facets are deterministic from structure? (no LLM)**
`file, symbol, test, repo, project, namespace, source_id`. These come from the **Layer-2 structural
model** (the shipped `ingest_project` / `structural_anchoring` / `structure_queries` path) and the
ingest envelope — not from regex over prose. Extracting them from text is the current extractor's
mistake; they should be *read from structure*, where they are exact.

**2. Which require LLM interpretation?**
`actor, object, operation, evidence_type` — the semantic-intent facets. These are interpretive and
should be LLM-extracted, carried as **hypotheses with confidence + provenance** (Layer-3 flavour), never
asserted as deterministic. This is where the extraction-model benchmark applies (use a blessed keeper —
`gpt-4.1-nano` / `qwen3-next-80b` — for the structured-JSON extraction call).

**3. Which can be inferred from Git history?**
`valid_time` / `learned_time` (commit/authored timestamps), `file` / `symbol` (changed paths/defs in the
commit), `actor` (commit author), `source_id` (commit sha). A Git-aware ingest/GitOracle populates these
deterministically — strictly better than guessing from text.

**4. Which should be forbidden for vague queries?**
Per the facet validator's facet-less-vague rule: a vague / embedding-should-win query must carry **no**
`repo/file/symbol/valid_time` facet. The extractor must **not over-facet** — inventing a scope/structure
facet for a vague query wrongly filters the candidate pool and hands the win to nobody. Forbid emitting
scope/structure facets below a confidence threshold; let embedding handle vague queries.

**5. How should extractor confidence be scored?**
Per-facet, by **source**, not a single blob:

```text
structural (Layer 2)   1.0   deterministic — trust as a hard facet
git-derived            0.9   deterministic from history
vocab / regex          0.7   advisory
LLM-interpreted        calibrated (carry as hypothesis; gate by model + self-consistency)
```

Overall extraction confidence gates **how** a facet is used: high-confidence facets drive candidate
generation (hard overlap); low-confidence facets are advisory priors only, never filters.

## The hybrid extractor (deterministic-first)

```text
1. Structural pass   — read file/symbol/test/repo/project/namespace/source_id from the Layer-2 model.
2. Git pass          — read valid/learned time, changed files/symbols, actor, sha from history.
3. LLM pass          — extract only actor/object/operation/evidence_type, as confidence-tagged hypotheses.
4. Vague guard       — drop scope/structure facets below threshold; do not over-facet vague queries.
```

This mirrors the spine: deterministic facets are facts; interpreted facets are hypotheses with
provenance. It also reuses, not reinvents: structure from the shipped structural graph, time/authorship
from Git, and the interpretive call from the benchmarked extraction model.

## Bench result (built — hybrid mode in archolith_bench/facet)

The `hybrid` facet mode reads the deterministic facets from gold (the Layer-2 / Git stand-in) and
extracts only the interpretive facets from prose. On the real DRAFT fixture (lexical embedding stand-in):

```text
mode        F facet+meet R@5   F stale   F wrong-scope   gate
gold        0.85               0.15      0.07            graduates
extracted   0.28               0.00      0.73            fails (recall loss 0.575)
hybrid      0.83               0.13      0.07            graduates (recall loss 0.025)
```

The hybrid recovers F from 0.28 → 0.83 (gold is 0.85) — confirming the bottleneck is structural-facet
**extraction**, not the engine. The small residual vs gold is the genuinely-interpretive facets the
regex still misses.

Still owed (`supported-by-spike` → `supported-by-eval`):

```text
- replace the gold stand-in for deterministic facets with the actual Layer-2 structural model + a
  Git pass (so "deterministic" is real, not borrowed from gold);
- replace the regex interpretive pass with a benchmarked extraction model (gpt-4.1-nano / qwen3-next-80b);
- a real embedder for conditions B/C/E; ctharvey's hardened fixture.
```

## Non-goals

```text
do not implement the extractor from this doc — it is a plan
do not extract structural facets from prose (read them from the Layer-2 model / Git)
do not let LLM-interpreted facets become deterministic/hard facets (carry confidence + provenance)
do not over-facet vague queries (respect the facet-less-vague guard)
do not change the retrieval engine to compensate for a weak extractor — fix the extractor
```
