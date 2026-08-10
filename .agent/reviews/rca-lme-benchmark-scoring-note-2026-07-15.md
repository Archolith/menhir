# Benchmark methodology note: term/synonym canonicalization for `retrieval_quality.py` scoring

**Date:** 2026-07-15
**Not a menhir product RCA** — this is about the M1 benchmark harness's own scoring accuracy, not
a menhir retrieval/extraction defect. Filed alongside the other RCAs because it explains part of
the gap between the harness's reported 16-29% presence numbers and menhir's actual retrieval
behavior.

## Origin

Raised during the M1 investigation: could an LLM pass that groups synonymous terms (e.g.
"Valentine's Day" = "February 14th") into a canonical form improve `retrieval_quality.py`'s
token-overlap `gold_rank`/`support_rank` matching, without replacing it with a per-question LLM
judge (which would reintroduce the cost/scale problem the harness was designed to avoid)?

## Finding: real, but narrow

Of 10 cases where the gold-aware LLM judge scored menhir's retrieval **"yes"** (sufficient to
derive the answer) while the token-overlap harness had scored the same question a complete miss
(`gold_rank=None`), only **1** (`58ef2f1c`: gold "February 14th", memory content "the 'Love is in
the Air' fundraising dinner on Valentine's Day") is a clean synonym/alias mismatch matching this
hypothesis. The other 9 are a mix of:
- Answers requiring simple **arithmetic/inference over stated facts** (e.g. `0bb5a684`: the
  content states two specific dates; the gold answer is the number of days between them — no
  synonym issue, this is a computation the token-overlap check was never going to catch regardless
  of canonicalization).
- Plainly-stated facts (`945e3d21`: "three yoga sessions per week" vs gold "Three times a week")
  that likely fail token-overlap on tokenization/phrasing details narrower than true synonymy.

**So: canonicalization would fix roughly 1 in 10 of these "false miss" cases directly** — real,
but not the dominant driver of the gap between reported and actual retrieval quality.

## Recommended approach if pursued

Given the narrow yield, prefer the **cheap, corpus-level** version over a per-question LLM judge:
1. One-time LLM pass over LME's 500 gold answers to extract a small alias table for calendar
   dates/holidays and similarly canonicalizable term classes (this is where nearly all the
   real hits are — date/holiday naming, not general vocabulary).
2. Apply the alias table to normalize both `gold` text and candidate `content` text before the
   existing token-overlap accumulation check in `gold_rank`/`support_rank`
   (`archolith-bench/scripts/longmemeval/analysis/lib/retrieval_quality.py`).
3. Keep this separate from arithmetic/inference-requiring answers — those need a different
   solution entirely (if a "does this require computation" gate is wanted, that's a different,
   larger scoring-methodology change, not covered by this note).

## Not recommended based on current evidence

Building this as a **menhir product feature** (query-time or ingest-time entity/date-alias
resolution) — no evidence from this investigation shows it would fix any of the *genuine* misses
(the ranking/extraction/stale-fact RCAs). In every genuine-miss case checked, the problem was
"the fact isn't retrievable at all" or "the fact isn't in the graph," not "the fact is retrievable
but phrased differently than the query." Don't build this into menhir itself without separate
evidence that query/fact phrasing mismatch is actually costing real recall — right now it's only
shown to cost *benchmark scoring accuracy*, not real answers.

## Related

- `.agent/reviews/rca-lme-retrieval-ranking-gap-2026-07-15.md`
- `.agent/reviews/rca-lme-extraction-admission-gap-2026-07-15.md`
- `archolith-bench/scripts/longmemeval/results/lme-recall-lab-investigate/investigate-2026-07-15.md`
