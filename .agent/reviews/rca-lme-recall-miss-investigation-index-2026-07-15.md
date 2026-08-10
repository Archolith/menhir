# Index: LME recall-miss investigation (2026-07-15)

Follow-up to the M1 gate run (`.agent/plans/menhir-m1-oracle-lme-ir-benchmark.md`,
`docs/roadmap/menhir-mvp-roadmap.md` M1). The M1 gate itself PASSED (menhir beats the graphiti
vector-only baseline by ~11.5x on Hit@3), but the absolute numbers were low (menhir found
supporting evidence for only 16.2% of the full 500-question corpus). This investigation asks: is
that a genuine retrieval problem, or partly a benchmark-measurement artifact?

## Method

Built a new harness (`archolith-bench/scripts/longmemeval/analysis/lib/recall_lab_investigate.py`)
combining `retrieval_quality.py`'s menhir/graphiti recall calls with menhir's Recall Lab tuning arm
E and a new **gold-aware, per-arm-absolute** LLM judge (menhir's existing `recall_lab.judge_recall_lab`
is comparative-only and has no ground truth — this judge is given the actual LME correct answer and
independently verdicts yes/partial/no per arm). Ran on 49 hand-picked questions that scored a
complete miss (`gold_rank=None` for both menhir and graphiti) in the 2026-07-15 full n=500 run.

## Headline result

Of menhir's 49 "complete miss" questions: **10 yes + 17 partial = 27 (55%) actually had useful
content retrieved.** Only 22 (45%) were genuine "nothing relevant was retrieved" misses. The
token-overlap benchmark scoring understates menhir's real retrieval quality by roughly half on
this hardest-case sample.

Of those 22 genuine misses, direct graph inspection (11 of 22 cases checked so far) found **three
independently-fixable failure modes**, plus one benchmark-scoring-only issue. **Updated same day:**
deeper inspection (reading full raw haystack text, not just the gold-answer keyword) reclassified 2
of the original 6 "extraction gap" cases as knowledge-update cases instead — ground truth for all
cases below is now fully verified against the raw source conversation, not just against the graph.

| Document | Failure mode | Verified cases | Fixable via retrieval tuning? |
|---|---|---|---|
| `rca-lme-retrieval-ranking-gap-2026-07-15.md` | Fact exists, correctly extracted, ranks below top-10 | 3 | Yes — `recall_lab` tuning arms |
| `rca-lme-extraction-admission-gap-2026-07-15.md` | Plain, single-statement fact never extracted into the graph at all | 4 | **No** — needs extraction/admission-gate work |
| `rca-lme-stale-fact-retention-2026-07-15.md` | **ROOT CAUSE CONFIRMED (controlled A/B test):** `RELEVANT_SCHEMA_LIMIT=10` recency window + "when in doubt, do NOT extract" prompt causes entity re-mentions to be silently under-extracted once >10 episodes separate them from the entity's establishing context | 3 confirmed cases, mechanism proven via controlled test | No — needs a graphiti-core config/prompt change, not conflict-scan or retrieval tuning |
| `rca-lme-superseded-value-loss-2026-07-15.md` | Update applied correctly; old value lost, but the question asks about the old value | 1 | Possibly — `include_superseded` flag may already solve it, untested |
| `rca-lme-benchmark-scoring-note-2026-07-15.md` | Not a menhir bug — token-overlap scoring can't recognize synonyms (e.g. "Valentine's Day" = "Feb 14") | 1 of 10 "false miss" cases | N/A — benchmark fix, not a menhir fix |

**11 of the 22 genuine misses remain unclassified** — pending the same direct-graph-inspection
method applied to the rest of the widened sample. **The knowledge-update pattern is now the
strongest-evidenced finding of the four**: every one of the 3 cases checked against raw source text
shows the graph failing to end up with the correct current value (0/3) — 2 by keeping the stale
value, 1 by losing both. Given `knowledge-update` also has the worst raw miss rate of any type in
the full run (92%), this is likely the single highest-value fix to pursue first, ahead of the
extraction-gap RCA which affects fewer confirmed cases and needs real code tracing before a fix
direction is even known.

## Immediate next steps, in cheapest-first order

1. **`rca-lme-stale-fact-retention`: ROOT CAUSE CONFIRMED — read the RCA's "CONFIRMED: controlled
   A/B test" section.** Final mechanism, proven with a same-message/varied-context A/B test:
   graphiti-core only gives extraction the last `RELEVANT_SCHEMA_LIMIT=10` episodes as context
   (`search_utils.py:64`); with the Chicago-establishing episode in view, extraction correctly
   proposed `Rachel`/`suburbs` (5 entities); with zero prior context (simulating the real >10-episode
   gap), it proposed only `user` (1 entity) for the identical message. Confirmed via 4 investigation
   passes: scheduler-disabled (real, insufficient alone) → `scan_for_conflicts` (mechanically works,
   structurally can't help — no second entity to link) → isolated trace (false positive, too little
   context) → real re-ingest (reproduced 3x, ruling out transient failure) → controlled A/B (proof).
   Side finding: `DELETE /api/namespace` does not reliably clear Episodic nodes — a separate bug.
   Fix directions in the RCA, tied to `.agent/plans/menhir-belief-supersession-temporal-chains-research.md`.
2. `rca-lme-superseded-value-loss`: test `include_superseded=true` on `07741c44` — could resolve
   with zero code change if the data is retained but excluded by default. Still untested, still
   cheap.
3. Widen past n=3: classify the ~9 remaining unclassified `knowledge-update` misses by counting
   how many episodes separate their update statement from the entity's establishing episode, to
   confirm the >10-episode-gap pattern holds generally, not just for `830ce83f`.
4. `rca-lme-retrieval-ranking-gap`: re-run the 3 confirmed cases through Recall Lab's existing
   tuning arms (A-H) — may already have a fix with no new code.
5. `rca-lme-extraction-admission-gap`: trace `89527b6b` (dense multi-entity turn, no pre-existing
   entity involved — a genuinely different mechanism from the recency-window finding above) through
   extraction separately; the two RCAs' root causes have now diverged (recency-window vs.
   turn-density), so this is its own investigation, not a combined one.

## Evidence sources

- `archolith-bench/scripts/longmemeval/results/lme-recall-lab-investigate/investigate-2026-07-15.md`
  / `.json` — full per-question judge output, all 49 questions.
- `archolith-bench/benchmarks/longmemeval-menhir-2026-07-15.md` — the M1 gate evidence this
  investigation follows up on.
- Direct Neo4j queries against `menhir-lme-neo4j` (ad hoc, not scripted — see each RCA's Evidence
  section for the exact Cypher used).
