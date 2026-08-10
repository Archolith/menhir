# RCA: Retrieval ranking gap — correct fact exists in graph but doesn't surface in top-K

**Date:** 2026-07-15
**Severity:** Medium — the data pipeline works; this is a scoring/ranking problem, independently
fixable via `recall_lab`'s existing tuning surface.
**Status:** Root cause identified via direct graph inspection; NOT yet root-caused at the scoring-
algorithm level (i.e. we know the fact exists and isn't returned, we have not yet traced *why* the
current `RetrievalTuningConfig` scores it too low). Fix direction proposed, not implemented.

## Summary

Of 22 genuine LME retrieval misses examined (of 49 investigated; miss = LLM judge, given the
correct answer, verdicts a result set "no" — insufficient to derive the answer), at least 3 are
cases where the correct supporting fact **is present, correctly extracted, in the graph**, but
`/api/recall`'s top-10 (`limit=10`, `candidate_k=50`) never surfaced it. This is a pure
ranking/scoring problem, not a data problem — the fix is in retrieval tuning, not extraction.

## Evidence

### `gpt4_2655b836` (temporal-reasoning)
- **Question:** "What was the first issue I had with my new car after its first service?"
- **Gold answer:** "GPS system not functioning correctly"
- **menhir /api/recall top-10:** LLM judge verdict **no** — "The snippets discuss car maintenance
  and detailing but do not address any problems with the GPS system."
- **Direct graph search** (`MATCH (n:Entity) WHERE n.group_id='lme-gpt4_2655b836' AND toLower(n.summary) CONTAINS 'gps'`)
  found the exact fact, in an entity never surfaced in top-10:
  > `"dealership": "the dealership replaced the entire GPS system in the user's car on
  > 2023-03-22T00:00:00Z. The dealership was able to fix the GPS system."`

### `6d550036` (multi-session)
- **Question:** "How many projects have I led or am currently leading?"
- **Gold answer:** "2"
- **menhir top-10:** verdict **no** — "The snippets do not provide any information regarding the
  number of projects led or currently being led."
- **Direct graph search** for "project"/"lead" found a directly relevant, unsurfaced entity:
  > `"Marketing Research class project": "The user participated in the Marketing Research class
  > project to lead the data analysis team and conduct a market analysis for a new product
  > launch."`
- Caveat: this question requires *aggregating* across multiple project mentions (a count), so even
  with this entity surfaced, correctly answering "2" also needs the second project entity in
  context simultaneously — a compounding factor on top of the pure ranking gap.

### `7161e7e2` (single-session-assistant)
- **Question:** "...what was the rotation for Admon on a Sunday?"
- **Gold answer:** "Admon was assigned to the 8 am - 4 pm (Day Shift) on Sundays."
- **menhir top-10:** verdict **no** — "The snippets do not provide any specific information about
  Admon's shift time on Sundays."
- **Direct graph search** found a relevant but under-specific entity:
  > `"Admon": "Admon is one of the GM social media agents involved in the shift rotation."`
  This confirms Admon is in the rotation but the entity itself doesn't carry the specific
  day/shift-time detail — possibly a secondary extraction-granularity issue (the shift-rotation
  table may not decompose into per-day-queryable facts) layered on top of the ranking gap. Flagged
  here rather than in the extraction-gap RCA because *some* directly relevant content exists and
  simply wasn't ranked high enough to reach top-10 either.

## Root cause (as far as verified)

Not yet traced to a specific scoring formula defect. What's established: `candidates_evaluated`
for a typical LME namespace search is in the range of 20-50 (see `service_ms`/`candidates_evaluated`
in Recall Lab responses observed during this investigation), and the correct entity is *among* the
evaluated candidates but ranks below the `limit=10` cutoff. This is consistent with either:
1. Underweighted semantic similarity for facts phrased very differently from the query (the query
   asks about a "car issue after first service"; the fact is phrased around "dealership... GPS
   system... fixed" — genuinely low lexical/semantic surface overlap despite being the right
   answer), or
2. Recency/prominence/adjacency scoring components (`recall_lab`'s `score_parts`:
   `recency_bonus`, `prominence_bonus`, `adjacency_bonus`) diluting a candidate that has strong
   topical relevance but weak positional/recency signal.

Neither hypothesis is confirmed — this requires inspecting the actual `score_parts` breakdown for
the "dealership" candidate specifically (was it in the candidate pool with a low `final_score`, or
was it excluded from the candidate pool entirely before scoring?), which was not done in this pass.

## Recommended fix direction (not implemented)

1. **Immediate, cheap:** re-run these 3 (and the wider 22-question sample) through `recall_lab`'s
   existing tuning arms (A-H) via `/explorer/api/recall-lab/run` with `candidate_k` raised and/or
   `enable_facet_candidates`/`enable_oracle_ranking` toggled, to see if any existing tuning knob
   already fixes ranking for these cases without new code. This is the fastest way to tell "scoring
   weight problem" from "candidate pool problem."
2. If no existing tuning arm fixes it: inspect `score_parts` for the specific missing candidates
   directly (Recall Lab already returns this — `similarity`, `bm25_rank`, `cosine_rank`,
   `score_parts.semantic_similarity/adjacency_bonus/recency_bonus/prominence_bonus`) to identify
   which scoring component is suppressing a topically-correct-but-lexically-distant candidate.
3. Do NOT tune blind. `analyze_recall_lab_scores.py` already exists to aggregate judge-dimension
   scores across many runs — the natural next step is running these 22 questions (or the full
   LME corpus's miss set) through Recall Lab's judge-scored arms, not one-off manual tuning.

## Verification plan

- Re-run `gpt4_2655b836`, `6d550036`, `7161e7e2` through `/explorer/api/recall-lab/run` with each
  of the 8 default tuning arms, `judge=true`, and check whether the "dealership"/"Marketing
  Research class project"/"Admon" entities move into top-10 under any arm.
- If yes for a specific arm: that arm's tuning delta is the fix; validate it doesn't regress other
  categories via the existing `msc`/`ablation` sweep scripts before promoting it.
- If no arm fixes it: this points at a systematic scoring gap not covered by existing tuning knobs,
  and needs new work, not a config change.

## Related

- `.agent/reviews/rca-lme-extraction-admission-gap-2026-07-15.md` — the other major genuine-miss
  category; distinguishing the two required checking graph content directly per question, since
  the symptom (menhir returns nothing useful) looks identical from the outside.
- `archolith-bench/scripts/longmemeval/results/lme-recall-lab-investigate/investigate-2026-07-15.md`
  — full per-question judge output for all 49 investigated questions.
