# Plan: Summary substitution — Views shadow their source episodes at recall (FRE 1006)

<!-- Filename convention: <feature>-plan.md -->

**Status:** backlog — proposed 2026-07-11
**Gap source:** `.agent/reference/menhir-frontier-transfer-forensic-admissibility.md` §2.3 + §6 (elevated
#3) + `.agent/reference/menhir-cross-domain-representation-research-2026-07-02.md` arch-critique #2.
**Related:** `../../reference/fold-algebra.md` (Views + `view_sig`), the completeness-watermark partial (View coverage
boundary), `retrieval-recency-split-and-view-injection.md`.

---

## The gap (one line)

Views compete in the **same recall pool** as the episodes they summarize, so a query can retrieve the
View **and** its sources — double-counting evidence and paying the exact token cost D0 exists to
eliminate.

## Current default (code-anchored)

- Views are stamped `:Entity` into the same candidate pool as their `MENTIONS`-support episodes.
  "Never replacements" is a write-side virtue but a **read-side liability**: the rank-1 D0 win partially
  leaks back at ranks 2–5 (a query pays for the summary and its own sources).
- Confirmed on the write side: `services/perception.py` states "**Views are purely additive**, so the
  absence of a View IS the fallback" — i.e. a committed View sits *alongside* its sources with no
  recall-time subsumption. This gap (2026-07-11 re-verification) is genuinely unbuilt.

## Promotion criteria (default → substituted)

The default flips from **"summary and sources both compete"** to **"summary XOR sources before the
fact-finder."**

- **supported-by-spike** when, for a namespace/kind, an **ACTIVE View shadows its `MENTIONS`-covered
  episodes** in the default recall pool; the agent can **pass the View's id to fetch its covered child
  episodes on demand** (a first-class "show me the receipts" affordance — substitution must never blind
  the agent, only default it to the summary); and a View **challenge/invalidation lifts the exclusion
  atomically** (restores sources — never both at once).
- **Falsifier (E3):** on retrieval-entropy queries where a View ranks first, filter its covered
  episodes from the candidate pool (query-side, experiment only); measure rank 2–5 contamination and
  answer correctness. A **correctness drop** → Views under-cover → substitution would hide truth →
  falsified.

## Path (how to get there)

1. **Recall subsumption rule:** episodes `MENTIONS`-covered by an ACTIVE View are excluded from the
   default pool for that namespace/kind — one predicate in candidate-fetch, composing with the
   existing scope/freshness/CONFLICT filters.
2. **Expand-by-id affordance (the inspection right, made first-class):** given a View uuid, return its
   `MENTIONS`-linked child episodes so the LLM can pull the receipts and confirm the summary when it
   wants. The query already exists — reuse `view_repository` provenance (`view_repository.py:337`,
   `(:Episodic)-[:MENTIONS]->(view)`) / `fetch_candidate_provenance` (`memory_queries.py:179`); it just
   needs an agent-facing surface (an MCP `expand_view(view_uuid)` tool, or an `expand=<uuid>` param on
   recall — matching the existing `node_uuid`-taking tools). This is what makes substitution *safe*:
   the agent gets the compact View by default and deterministically audits it on request.
3. **Atomic un-substitution:** a View accuracy challenge/invalidation lifts the exclusion and restores
   the sources atomically (summary XOR sources, never both).
4. **Define View coverage** by the `recorded_at` high-water mark of folded events (the 1006 coverage
   boundary; ties to the completeness-watermark partial).

## Non-goals

- Never surface summary **and** sources for the same issue (that is the double-count bug).
- Do not delete covered episodes — they are shadowed and fully recoverable via provenance.

## Risks

- **Views under-covering → hiding truth** — E3 is precisely the guard; judge substitution by *answer
  correctness*, never by top-k overlap.

## Source

Forensic-admissibility §2.3 (FRE 1006 summary substitution) + §6 (the "Views compete in the same pool"
attack) + cross-domain review arch-critique #2. "Deletes symptom 7 by construction rather than by
ranking."
