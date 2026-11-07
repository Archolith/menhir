---
artifact_schema: 1
artifact_uuid: 858c738f-9b88-4fde-88d6-47b6d9e3aec0
artifact_type: investigation
artifact_status: COMPLETE
---

# Recall experiment record

Last updated 2026-11-06.

Kept so recall ideas aren't retested without a new reason. It records what has been tried, on which
part of recall and which data, what it showed, and what is still open. Consumers: #515 (can recall
tell whether it holds an answer), #516 (entity recall options) and #433 (what scores mean). Read this
before proposing a recall change, and add new results here.

This note authorizes no implementation.

## The short version

- Recall has two separate retrieval paths, and most past work was on the entity path. The episode
  path feeds the reader, and it has had far less testing.
- Name cosine is a poor ranking signal. RRF stays. A cosine floor was dropped.
- Added entity lanes mostly tied or lost, and a lane can crowd out good results.
- Scores can't say whether memory holds an answer. A reader can say the retrieved text doesn't answer
  the question, but that isn't the same as the answer not being in memory.
- With distractors, the main failure is evidence not reaching the top 10. Reading top 30 on a flag
  recovers most of the lost accuracy but doesn't fix confident wrong answers.
- Keyword search and reranking have not been tested on episodes.

## How recall is built

| Path | Searches | Ranking today | Notes |
|---|---|---|---|
| Entity | Entity nodes | Graphiti fused BM25 + name cosine, then RRF-based scoring | `similarity` is a rank score, not a cosine. The 0.15 floor is a rank cut. |
| Source memories | Raw episodes (`Episodic.content_embedding`) | Cosine top-k, then session spread | Default limit 10, 1-50 per request. Pure cosine, no keyword input. |

**Session spread is on.** It shipped default on in #386 and has been always on since the #168 freeze.
It fetches 3x the limit (capped at 50), drops exact repeats, gives each session's best row a slot, then
fills by cosine. So recall already has session diversity, and the distractor results below include it.

## Entity path experiments

| Experiment | Data | Result | Outcome |
|---|---|---|---|
| Normalized similarity scale | LME oracle, 07-04 | 143/500 either way, same questions | Removed |
| Fact-edge lane | 30 questions, 07-02 | Off 9/30, standalone 1/30, pointer 8/30 | Removed |
| Fact-edge lane | #171 ablation | Standalone lost MRR on both corpora. Pointer neutral to slightly negative on LME, no effect on AMA | Removed |
| Attributed BM25 hybrid | LME | 0.700 vs 0.710, `wrong_scope` 0.034 to 0.081 | Removed |
| Attributed BM25 hybrid | #171 ablation | Depth 50 +6/-0 LME, +11/-0 AMA, about 3x the results, top-10 gain tiny with CIs touching 0 | Removed; candidate for a held-out test (#516) |
| Content-vector lane | Human anchors, 07-14 | Tied | Removed, no-graduate |
| Content-vector lane | #171 ablation | Top 10 -.123 LME, -.086 AMA, **but the lane was empty** (see below) | Not a real measurement |
| Facet candidates | #171 ablation | Neutral on LME, MRR loss on AMA | Left off |
| Cross-encoder probe | Small July probe | 0.000 on two no-answer queries, 0.953 on one answerable, about 1s plus an LLM call, and its order pushed the top hit to rank 9 | Not recommended; sample too small to settle it |
| Name cosine vs RRF | o500 and 10-namespace pools | Judge labels: -0.288 / -0.302 oracle, -0.225 / -0.264 distractor (pooled / within-question AUC) | Killed |
| Cosine floor | Same | Phase 4 dropped after name cosine failed. No floor was tested directly | Dropped |
| Rank floor at 0.15 | Same, heuristic labels | Dropped 35% of positives and 52% of negatives, but cost only 4 of 414 questions their last positive | Not a main cause |
| Summary cosine vs RRF | Same, judge labels | +0.083 / +0.177 oracle, +0.099 / +0.133 distractor | Supported as registered, weaker on checks below |

**The content-vector result is often misread.** Nothing ever wrote `Entity.content_embedding`, so
both #171 graphs had none. The losses came from the flag replacing fused BM25 + cosine with cosine
alone. That says fused search beats name cosine alone. It says nothing about content similarity.

**Summary cosine is weaker than its headline.** 47% of positives are `user`-like entities. Without
them the gain is +0.024 / +0.058 oracle and +0.046 / +0.051 distractor, below the +0.05 bar on at
least one scale. On oracle, summary length alone scores +0.036 / +0.159 against RRF. The judge also
reads the summary, so some circularity remains.

**Coverage limit.** In the #171 run, 33 of 187 LME questions had no entity or edge linked to their
answer turns, so no entity lane could reach them. Source memories bypass this.

**Labels.** "Mentioned in a gold turn" had 0.102 precision for "carries the answer". Use judge labels
for entity ranking.

## Episode path experiments

| Experiment | Data | Result | Outcome |
|---|---|---|---|
| Source-memory lane | AMA, 09-30 | Fixed 41, broke 19 | Shipped |
| Anchor pools on top | AMA, 09-30 | Fixed 43, broke 20; 15 vs 14 head to head | Removed in #399 |
| Lane latency | #171, 10-20 | Recall p50 +2.1%, p95 +2.9%. Ingest p50 about +340 ms per memory | Passed |
| Session spread | o500 | 0.810 vs 0.804; changed 18 of 500 blocks, fixed 3, broke 0 | On, but only measured where sessions are few |

## What does the graph add?

Stored o500 contexts, 444 questions outside `single-session-assistant`, blocks removed, answered
fresh, 2026-11-06.

| Context | Accuracy | Context chars |
|---|---|---|
| Episodes | 0.921 | 1.18M |
| Episodes + typed values | 0.903 | 1.44M |
| Full | 0.890 | 3.66M |

Neither typed values nor entities added measurable accuracy (both differences within 5 points). Full
vs episodes was -0.032, CI -0.059 to -0.002, outside the registered pair.

**This doesn't show the graph is useless.** o500 namespaces hold only answer sessions, which favours
episodes. Provenance, inspection and deletion weren't measured. The deciding test is the same
comparison on merged pools (#516).

## Can recall tell it has no answer?

Null queries on o500: `absent` keeps the topic but the answer isn't there, `collision` shares a word
with a different subject. `_abs` is LME's 30 human-written unanswerable questions.

| Signal | Collision | Absent | Reading |
|---|---|---|---|
| `high` relevance label | 99.3% labelled `high` | 100% | No use |
| Episode cosine | AUC 0.958, 88.0% flagged | AUC 0.594, 15.9% flagged | Topic signal only |
| Summary cosine | AUC 0.950, 88.0% flagged | AUC 0.683, 18.8% flagged | Topic signal only |
| Rare-word query coverage | AUC 0.801 | AUC 0.352 | Killed on absent, adds nothing over cosine |

Coverage fails because 71 of 134 absent queries use only words memory holds. What's missing is a
relation between known words.

### Reader test

A model reads the retrieved episodes, and a blind judge labels its reply `missing`, `partial` or
`answered`. "Flagged" here is `missing`, except the `_abs` rows, which count `missing` or `partial`.

| Setting | Absent | Collision | Real | `_abs` | Real, with `partial` |
|---|---|---|---|---|---|
| o500, GPT reader | 96.3% | 86.6% | 3.1% | 90.0% | 5.3% |
| 10-namespace pools, top 10 | 96.3% | 84.6% | 11.4% | 83.3% | 17.9% |
| Pools, top 30 on a flag | 94.8% | 81.2% | 1.9% | 76.7% | 2.9% |
| Pools, top 50 on a flag (descriptive) | 93.3% | 82.6% | 2.2% | 83.3% | 3.1% |

- A DeepSeek reader on o500 gave similar results: absent 97.7%, collision 96.6%, real 5.6%. A DeepSeek
  verifier agreed with the kept generated queries on 430 of 432, so the GPT figures weren't inflated
  by using one model family.
- Top 10 on pools failed the rule on real questions (Inconclusive, Inconclusive, Killed). Top 30 on a
  flag met it on all three checks.
- Flags track evidence. With all, some or no evidence in the top 10 (n = 238, 130, 46), `missing` was
  2.9%, 13.8% and 47.8%. So a flag means the retrieved text doesn't answer the question, not that
  memory lacks the answer.

### Widening on a flag

| Reply on 414 real questions | Accuracy |
|---|---|
| o500, no distractors | 0.925 |
| Pools, top 10 | 0.720 |
| Pools, top 30 on a flag | 0.843 |
| Pools, top 50 on a flag | 0.850 |

Top 30 vs top 10: +12.3 points, CI +9.2 to +15.5, 54 gains, 3 losses.

- **Evidence is often just outside the top 10.** Of 74 flagged real questions, 12 had complete
  evidence in the top 10, 68 at top 30 and 73 at top 50.
- **Confident wrong answers didn't move:** 54 to 55, about 13% of real questions. 44 of the 54 had
  incomplete evidence. Re-reads only run on a flag, and these never flag.
- **Unknown:** 114 of 176 real questions with incomplete evidence never flagged, so there's no wider
  data for them.

Limits: pools hold about 330 episodes and about 20 sessions of other users' data, not one person's
long history. 30 is 9% of a pool, and may not be enough in a large memory. Each query was answered
once, and `_abs` is 30 questions.

## Not tested yet

These are open, not ruled out. Earlier negative results don't cover them.

| Idea | Why it's open | Issue |
|---|---|---|
| Spread on vs off at pool scale | Only measured on o500, median 2 sessions. On pools of about 20 sessions it may cost same-session evidence its slot. This is a hypothesis | #515 |
| Keyword search over episodes | Past BM25 work was on entity search | #515 |
| Reranking episodes | The July probe was on entity gating. The 11-06 root-cause read against a reranker came from o500, which has no distractors | #515 |
| Always reading top 30 | The remaining test for confident wrong answers | #515 |
| Entity value on merged pools | Decides how much entity work matters | #516 |
| Summary similarity with controls | The `user` entity and length weren't controlled in the registered test | #516 |

## Lessons for new tests

- Say which path a result applies to. Entity results don't transfer to episodes.
- Include distractors. o500 alone can't show ranking failures.
- Report what an added lane displaces.
- Check what a mechanism actually fired on before calling a change a win.
- Generated null queries can be circular when one model writes, filters and reads them. Check them
  with another model family, and report human-written `_abs` alongside.
- `single-session-assistant` questions are excluded because the graph doesn't ingest assistant turns.
  That's a test setup limit, not a Menhir fault.

## Sources

- In this repo: [`../archive/removed-features-2026-10-27.md`](../archive/removed-features-2026-10-27.md),
  [`r1-hybrid-candidate-generation.md`](r1-hybrid-candidate-generation.md),
  [`anecdotal-recall-oracle-ladder.md`](anecdotal-recall-oracle-ladder.md),
  [`../archive/plans/menhir-content-embedding-recall.md`](../archive/plans/menhir-content-embedding-recall.md).
- Issues and PRs: #168, #171 (ablation and qualification comments), #348, #386, #396, #399, #433,
  #470, #515, #516.
- Workspace plans, not in this repo: `menhir-recall-real-similarity-test-plan-2026-11-05.md`,
  `menhir-recall-no-match-signal-plan-2026-11-06.md`, `menhir-graph-value-ablation-plan-2026-11-06.md`.
  Each holds the registered rules, and run data sits under `~/.lme-bench/runs/`.
