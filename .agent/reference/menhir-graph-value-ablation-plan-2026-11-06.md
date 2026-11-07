---
artifact_schema: 1
artifact_uuid: ef213c66-079a-4f41-83be-66a20c277870
artifact_type: investigation
artifact_status: COMPLETE
---

# Menhir: what does the graph add over episode search?

> Completed test record, copied on 2026-11-07 from the maintainer's workspace plan of the same
> name. Local paths, host names and private notes were removed and stale status lines updated. The
> rules and results are otherwise unchanged. Run scripts and data are not in this repo.

Date: 2026-11-06. Follows `menhir-recall-no-match-signal-plan-2026-11-06.md`.

## Question

No test has compared full Menhir with plain episode retrieval. How much do typed values add over episodes, and how much do extracted entities add on top of both?

This is a measurement. No component is removed or changed.

## Method

The canonical run `o500-ship-dcf17cb/s4-ship` saved each question's full recall context. Each arm is that stored context with blocks removed, in original order. No graph is needed.

- **A episodes**: `Source memories (raw episodes...)` block only. It is plain cosine top-k over episode embeddings, with no graph involved.
- **B episodes + typed**: A plus `[STRUCTURED MEMORY VERDICT]`, `[AUTHORITATIVE CURRENT MEMORY]` and `[RELATED observation MEMORY ...]`.
- **C full**: the stored context unchanged.

All three are answered fresh, once each, with the same conditions as the canonical run: `gpt-6-luna`, the same system prompt (fingerprint `10ef922505`, checked by the script), `build_messages`, `max_completion_tokens` 16384, and the same official judge (`_judge_messages`, `_parse_yes`). Flex tier only differs. Requests are interleaved by question so a spend cap leaves the arms paired.

Population: the 444 questions outside `single-session-assistant`, including `_abs`. SSA is excluded because the `-counted` graph was built without assistant-turn ingest, so it says nothing about this comparison.

## Registered rule

Fixed before any answer call. Paired by question, 2000 bootstraps, seed 20261108. Questions with an answer or judge failure in any arm are dropped from all arms and counted.

Two comparisons, each on accuracy difference with a 95% CI:

- Typed value: B minus A.
- Entity value: C minus B.

Readings:

- **Helps**: CI entirely above 0.
- **Hurts**: CI entirely below 0.
- **No difference above 5 points**: CI inside -0.05 to +0.05.
- **Inconclusive**: anything else.

Descriptive: per-type accuracy, tokens per arm, fresh C against the stored C as a noise check.

## Limits known in advance

- o500 namespaces hold only answer sessions, which favours episodes. A win for A or B here does not show entities are useless at scale. A clear win for C would still count.
- Typed values are extracted at ingest and may depend on entity resolution, so B is not free of the graph's ingest.
- Episode selection stays at the stored 10. A gets far less context than C, so A is not a budget-matched baseline.
- The system prompt describes all block types, including ones absent from A.
- The prompt and pipeline were tuned on the full context, which favours C.
- One answer per arm; answer noise was about 10% in an earlier test.
- Entities can still have uses this does not measure: provenance, inspection, deletion, ranking of typed slots.

## Budget

Ceiling $0.25: $0.21 answers, $0.04 judge, enforced by `run_flex` `max_usd`.

## Result (2026-11-06)

Ran as registered. 444 questions, 1,332 answers, 0 dropped. Cost $0.1979. No graph or container needed.

| Arm | Accuracy | Correct | Context chars |
|---|---|---|---|
| A episodes | 0.921 | 409 | 1.18M |
| B episodes + typed | 0.903 | 401 | 1.44M |
| C full | 0.890 | 395 | 3.66M |

| Comparison | Difference | 95% CI | Wins / losses | Reading |
|---|---|---|---|---|
| Typed value, B-A | -0.018 | -0.043 to +0.007 | 12 / 20 | No difference above 5 points |
| Entity value, C-B | -0.014 | -0.038 to +0.014 | 15 / 21 | No difference above 5 points |

Descriptive, outside the registered pair: C-A is -0.032, CI -0.059 to -0.002, wins 12, losses 26.

By type, A / B / C: knowledge-update 0.974 / 0.923 / 0.910 (n=78); multi-session 0.857 / 0.850 / 0.850 (133); preference 0.900 / 0.800 / 0.700 (30); single-session-user 0.986 / 0.957 / 0.957 (70); temporal 0.925 / 0.940 / 0.925 (133); `_abs` 0.867 / 0.900 / 0.833 (30, overlapping the types above).

Noise check: fresh C 0.890 against stored C 0.901, with 31 of 444 verdicts flipped (7.0%).

Reading: on o500, neither typed values nor entities added measurable accuracy once episodes were in context, and C used about three times A's context. The July typed-value gain (0.679 vs 0.333 on knowledge updates) predates the source-memory block and did not reappear here.

This does not show the graph is useless at scale. o500 namespaces hold only answer sessions, so top-10 episodes usually contain the answer. The deciding test is the same comparison with distractors. That needs no new ingest: the o500 graph can be merged into 10-namespace pools in a throwaway copy, as in Phase 3b of the real-similarity plan, though it needs full recall contexts from the harness. Provenance, inspection and deletion are not measured.
