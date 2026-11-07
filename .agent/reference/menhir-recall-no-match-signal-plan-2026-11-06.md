---
artifact_schema: 1
artifact_uuid: 02a23d2e-9d7c-4081-b806-769545771503
artifact_type: investigation
artifact_status: COMPLETE
---

# Menhir recall: can a signal say "nothing here really matches"?

> Completed test record, copied on 2026-11-07 from the maintainer's workspace plan of the same
> name. Local paths, host names and private notes were removed and stale status lines updated. The
> rules and results are otherwise unchanged. Run scripts and data are not in this repo.

Date: 2026-11-06. Follows `menhir-recall-real-similarity-test-plan-2026-11-05.md`. Issues: #433, #515 (replaces #470).

## Question

Relevance labels are rank thresholds (#514), so an off-topic query can still show `relevance: high`. Is there a signal available at recall time that separates "memory holds this" from "memory does not", on nulls harder than swapped questions?

This is a measurement only. No recall output, score, ranking, threshold or default changes. How a result would surface is a later owner decision.

## Data

- Graph: read-only throwaway copy of `lme-snap-o500-ship-dcf17cb-counted`. Public LongMemEval data only (D2).
- Sample: 150 questions, `random.Random(20261106)`, drawn from the 414 that are neither `single-session-assistant` nor `_abs`.
- Per question, one gpt-6-luna Flex call sees the user turns of that namespace and writes three queries in the same style:
  - `control`: answerable, the answer is stated in the history.
  - `collision`: reuses a distinctive word or phrase from the history but asks about a different subject the history never covers (the #470 shape).
  - `absent`: same subject as the history, asks for a detail never stated.
- A separate verifier call sees all turns of the namespace and marks each query answerable or not. Keep `control` only if answerable, nulls only if not answerable.
- `control` is the primary positive so that query author is the same on both sides. Real LME questions are reported as a secondary positive. LME's 30 `_abs` questions are reported as an outside check on `absent`, descriptive only.

## Signals

- S1: highest `source_memories` cosine (episode content cosine, already computed by recall).
- S2: highest summary cosine over candidates (`recall_summary_cosine.py`).
- Baseline: highest RRF `similarity` among returned results.

## Registered rule

Fixed before any null query exists. Bootstrap 2000 resamples, seed 20261106, clustered by source question. Seeded half split into calibration and held-out.

For each signal and each null type (`control` vs `collision`, `control` vs `absent`):

- Threshold: 5th percentile of `control` scores on the calibration half. Below it means "no match".
- **Supported**: held-out AUC CI lower bound >= 0.80, AND held-out nulls flagged >= 50%, AND held-out controls flagged <= 10%, AND AUC gain over baseline has CI above 0.
- **Killed**: held-out AUC CI upper bound < 0.70.
- **Inconclusive**: anything else.

Validity gates, checked first; a failed gate means no verdict for that null type:

- At least 100 verified queries in each class.
- At least 50% of `collision` queries return a `high` result. Otherwise the set does not reproduce the #470 problem.

## Limits known in advance

- o500 namespaces hold only answer sessions, so they are small and on-topic. A threshold from here is not a production threshold, and it depends on the embedding model.
- A cosine measures topic, not whether the answer is present. `absent` may fail for that reason, which would limit any claim to "no topical match".
- Generated queries are not user queries. Transfer to real use is not tested (#444).

## Budget

Ceiling $0.15 Flex, estimate about $0.10, enforced through `run_flex` `max_usd`. Embeddings are free of approval. Lane total stays under the $1 D3 ceiling.

## Result (2026-11-06)

Ran as registered. 149 questions, 149 control, 149 collision, 134 absent, 432 recalls, 0 errors, 0 LLM calls in recall. Cost $0.0729. Both gates passed. `high` appeared on 99.3% of collisions, 100% of absent and 100% of controls.

| Null | Signal | Held-out AUC | CI | Baseline AUC | Nulls flagged | Controls flagged | Verdict |
|---|---|---|---|---|---|---|---|
| collision | S1 source cosine | 0.958 | 0.931-0.980 | 0.548 | 88.0% | 14.7% | Inconclusive |
| collision | S2 summary cosine | 0.950 | 0.915-0.977 | 0.548 | 88.0% | 12.0% | Inconclusive |
| absent | S1 source cosine | 0.594 | 0.507-0.675 | 0.465 | 15.9% | 14.7% | Killed |
| absent | S2 summary cosine | 0.683 | 0.587-0.769 | 0.465 | 18.8% | 12.0% | Inconclusive |

Collision missed Supported only on the 10% control limit. The rule stands as written.

Descriptive, outside the rule:

- Real questions score lower than generated controls. The control-calibrated cuts flag 24.6% (S1) and 17.9% (S2) of the 414 real non-SSA questions, mostly counting and temporal questions for S1.
- Cuts set on real questions, in-sample: 5% real flagged catches 59.1% of collisions (both signals) and 3-4% of absent. 10% catches 73.8% (S1) or 66.4% (S2) of collisions and 7-8% of absent.
- Real vs absent AUC is 0.444 (S1) and 0.523 (S2), so neither detects a missing answer.
- LME's 30 `_abs`: all returned `high`. 56.7% (S1) and 50.0% (S2) fall below the control cut. Swapped nulls: 99.2%.
- The first descriptive print counted 60 `_abs` rows because swapped ids also end in `_abs`. The numbers here use the true 30. The registered table was not affected.

Reading: cosine says whether memory is on the topic, not whether the answer is there. It could support a soft "weak topical match" hint. It can't support a "no answer here" verdict.

Limits: o500 namespaces are small and hold only answer sessions; generated queries are not user queries; cuts depend on the embedding model and are not production thresholds.

Scripts and run data are not in this repo. Throwaway graph removed.

## Follow-up: rare-word query coverage (registered before scoring, 2026-11-06)

Owner asked whether the graph's keyword matching should handle this. BM25 rank keeps a shared word and loses how much of the query matched. Test the opposite question: how much of the query does memory cover, weighted toward rare words?

Offline, no LLM, no graph, no spend. Reuses the 432 verified queries, their stored S1 and S2 scores, and the 414 real non-SSA questions.

Signals:

- C1: `sum(idf of query terms found in the namespace) / sum(idf of all query terms)`. Terms: lowercase `[a-z0-9]+`, fixed stopword list dropped, trailing `s` stripped when length > 3. `idf = log(1 + 500 / df)`, df counted over the 500 namespaces, no labels used. Namespace text: all user and assistant turns from the public fixture. A query with no content terms scores 1.0.
- C2: logistic combination of C1, S1, S2, fit on the calibration half only, real vs pooled nulls.

Rule (seed 20261107, 2000 bootstraps clustered by question, one seeded half split over the 414 real questions, nulls follow their question):

- Positives are real questions. Cut: 5th percentile of real calibration scores.
- **Supported**: held-out AUC CI lower bound >= 0.80, AND held-out nulls flagged >= 50%, AND held-out real flagged <= 10%, AND AUC gain over both S1 and S2 has CI above 0.
- **Killed**: held-out AUC CI upper bound < 0.70.
- **Inconclusive**: anything else.
- Judged separately for `collision` and `absent`.

Scale check for C1: repeat with each namespace merged with the other 9 in its `distractor-groups.json` group, recalibrating the cut. If nulls flagged falls below 50%, a Supported verdict is reported as oracle-only.

Descriptive: generated controls at the same cuts, and the 10th percentile cut.

Known limits: collision queries were built to share a word and change subject, so they favour C1. Fixture text is an upper bound on what the graph stores. 10 namespaces is still far smaller than a long-lived memory. A shipped version would also need a term index, which this does not test.

## Coverage result (2026-11-06)

Ran as registered, offline, no spend. 414 real questions, 207 held out, with 72 collision and 64 absent nulls in the held-out half.

| Null | Signal | Held-out AUC | CI | Nulls flagged | Real flagged | Verdict |
|---|---|---|---|---|---|---|
| collision | C1 coverage | 0.801 | 0.741-0.854 | 47.2% | 13.5% | Inconclusive |
| collision | C2 coverage + cosine | 0.898 | 0.853-0.938 | 59.7% | 5.8% | Inconclusive |
| absent | C1 coverage | 0.352 | 0.285-0.420 | 1.6% | 13.5% | Killed |
| absent | C2 coverage + cosine | 0.499 | 0.425-0.579 | 4.7% | 5.8% | Killed |

C1 lost to both cosines on collisions (gain CI -0.154 to -0.044 vs S1, -0.138 to -0.033 vs S2). C2 met the other limits but added nothing measurable (gain CI -0.023 to +0.024 vs S1, +0.000 to +0.027 vs S2). In C2 the fitted weight on coverage was -0.084 against 0.324 (S1) and 0.939 (S2).

Scale check failed: merged with 9 other namespaces, C1 flagged 37.5% of collisions at 9.2% real flagged, AUC 0.717.

Cosine alone at the same real-calibrated cut, held-out, no verdict because it is only the comparison here: S1 AUC 0.898, 69.4% of collisions at 8.7% real flagged. S2 AUC 0.886, 58.3% at 6.3%. Absent: S1 6.2%, S2 3.1%.

Why coverage fails, from hand reads: real questions often use words memory lacks (`sibling`, `Miami`, `weekend`), and 71 of 134 absent queries are fully covered because their missing piece is a relation between known words, not an unknown word.

Conclusion: keyword coverage adds nothing over cosine here. Missing-answer detection stays unsolved on every signal tried.

## Follow-up: does a reader catch missing answers? (registered before any call, 2026-11-06)

An offline look at the graph ablation's episodes-only answers suggested the answer model already says when an answer is missing: 26 of 30 `_abs` judged correct, and few real answers lost to over-caution. That was an unregistered read of 30 questions. This tests it on the harder `absent` set. Measurement only; nothing in recall changes.

Reader: `gpt-6-luna`, the canonical answer prompt (fingerprint `10ef922505`, checked by the script), `build_messages`, the source question's date, `max_completion_tokens` 16384. Context is the episodes block only, rendered exactly as `context_builder.py` does, from the 10 `source_memories` stored for each query. Episode text comes from a read-only throwaway copy of the graph, looked up by stored uuid.

Populations:

- Null: the 134 `absent` and 149 `collision` queries, answered fresh, once each.
- Real (primary positive): the 414 non-SSA, non-`_abs` questions, reusing their stored arm A answers from the graph ablation. Same model, prompt and context type.
- Secondary: the 149 generated `control` queries, answered fresh. The 30 `_abs` from arm A, descriptive.

Label: a blind judge call sees only the question and the response, not the class or the memories, and returns one of:

- `missing`: says the memory lacks what was asked and gives no answer to it.
- `partial`: answers part of the question and says another part is missing.
- `answered`: commits to an answer, right or wrong.

Flagged means `missing`. `missing` or `partial` is reported as a descriptive second reading.

Rule (seed 20261109, 2000 bootstraps clustered by source question, judged separately for `absent` and `collision`):

- **Supported**: null flagged CI lower bound >= 0.50, AND real flagged CI upper bound <= 0.10.
- **Killed**: null flagged CI upper bound < 0.50, OR real flagged CI lower bound > 0.10.
- **Inconclusive**: anything else.

There is no threshold to fit, so no calibration split.

Validity gates, checked first; a failed gate means no verdict:

- No more than 2% of calls in any class fail or are dropped.
- Hand check: 40 judge labels, seeded sample stratified over class and label, read against the response. At least 90% agreement.

Descriptive: flagged rate by real question type; for flagged real questions, whether the arm A answer was judged correct; `absent` answers labelled `answered`, which are confident answers to a question memory can't answer; overlap with the episode cosine cut.

Known limits: o500 namespaces hold only answer sessions, so this is close to a best case for the reader. Real answers were generated in the ablation, not this run. A flagged `control` may be a retrieval miss rather than over-caution, because the reader sees only 10 episodes. The reader costs one model call per recall; whether that belongs in Menhir or in the calling agent is a later owner decision. Generated queries are not user queries (#444).

Budget: ceiling $0.15 ($0.12 reader, $0.03 judge), enforced by `run_flex` `max_usd`. Lane total stays under the $1 D3 ceiling.

## Reader result (2026-11-06)

Ran as registered. 432 reader calls, 876 judge calls, 0 failed or unlabelled. Cost $0.0544. Hand check: 37 of 40 labels agreed (92.5%), passing the 90% gate. The three disagreements are borderline `missing`/`partial`/`answered` calls (samples 12, 17, 25). Episode text came from a throwaway graph copy, since removed.

| Class | n | Flagged (`missing`) | 95% CI | `missing` or `partial` | Verdict |
|---|---|---|---|---|---|
| absent | 134 | 96.3% | 92.5-99.3 | 97.0% | Supported |
| collision | 149 | 86.6% | 81.2-91.9 | 86.6% | Supported |
| real (positive) | 414 | 3.1% | 1.7-4.8 | 5.3% | |
| control (secondary) | 149 | 0.0% | 0.0-0.0 | 0.0% | |
| `_abs` (descriptive) | 30 | 73.3% | 56.7-86.7 | 90.0% | |

**Limit found after the run, not in the registered list: the generated classes are partly circular.** `gpt-6-luna` wrote the queries, and the same model's verifier kept nulls only if it called them unanswerable and controls only if it called them answerable. The reader is the same model, so agreement is partly built in. That inflates `absent`, `collision` and `control`. The verdicts stand as the rule reads, but the independent figures are the human-written ones: `_abs` 73.3% (90.0% with `partial`) against real 3.1% (5.3%). `_abs` is only 30 questions.

Descriptive:

- Real flagged by type: temporal 5/127, multi-session 3/121, single-session-user 2/64, preference 2/30, knowledge-update 1/72.
- 10 of the 13 flagged real answers were still judged correct, because the reader says the asked thing is missing and then gives the nearby fact (jewelry question, chandelier from aunt). Unflagged real accuracy is 0.930.
- All 4 sampled `collision` answers labelled `answered` were general advice (hard drive size, ghost story, inbox, leather repair), which needs no memory. The 20 are not all misses.
- 4 `absent` queries were answered: one general advice, three best guesses (train fare, website date, how Alex commutes).
- Overlap with the episode cosine cut, registered as descriptive, was not computed. It doesn't affect the verdicts.

Reading: for the first time a signal separates missing answers from real questions, where cosine caught 6.2% and coverage nothing. The signal is reading the text, not scoring it. On human-written questions the evidence is 30 `_abs`, not the 96%.

Untested: a reader only sees what recall returned, so it can't tell "not in memory" from "not in the top 10". On o500 that rarely matters. With distractors, real questions may be flagged more often, so the 3.1% is a best case.

Scripts and run data are not in this repo.

## Follow-up: reader test with another model family (registered before any call, 2026-11-06)

Removes the circularity above. `deepseek-v4-flash`, thinking off (the bench's default for it), replaces `gpt-6-luna` as both verifier and reader. Public data only. The queries were still written by `gpt-6-luna`; that part can't be removed without new queries.

Verifier: the original verifier prompt, one call per source question, question order shuffled with seed 20261110 so position gives no hint of class. A generated query stays in the primary set only if this verifier agrees with its class. It sees the namespace's user turns only. Amended before any call: full history priced at about $0.27 with caching, over the ceiling, and this graph stores only user turns, so they are all a reader could ever find. The original verifier saw both sides, so a null kept here may be stated in an assistant turn; that is still absent from this memory.

Reader: the canonical prompt (fingerprint `10ef922505`), `build_messages`, the same episode context as before, once each, over all 432 generated queries, the 414 real and the 30 `_abs`. `max_tokens` 2000.

Judge: unchanged, `gpt-6-luna` with the same blind labels. It sees only question and reply, and it's a different family from this reader. Changing it too would change two things at once.

Rule (seed 20261110, 2000 bootstraps clustered by source question), same as the reader rule, with flagged meaning `missing`:

- **Supported**: null flagged CI lower bound >= 0.50, AND real flagged CI upper bound <= 0.10.
- **Killed**: null flagged CI upper bound < 0.50, OR real flagged CI lower bound > 0.10.
- **Inconclusive**: anything else.

Judged for `absent` and `collision`, on DeepSeek-verified queries with the DeepSeek reader, against DeepSeek real answers.

The same rule is also applied to the human-written `_abs` against real, now as a registered check, with flagged meaning `missing` or `partial` on both sides, because many `_abs` questions have two parts and only one is missing. It involves no generated query, so it is the check least exposed to circularity.

Validity gates, checked first; a failed gate means no verdict:

- No more than 2% of calls in any class fail or are dropped.
- At least 100 DeepSeek-verified queries in each null class.
- Hand check: 40 judge labels, seeded, stratified over class and label. At least 90% agreement.

Descriptive: verifier agreement by class; `gpt-6-luna` reader rates on the DeepSeek-verified subset, which isolates the verifier's effect; agreement between the two readers per class.

Known limits: a lower flag rate here could mean less circularity or a weaker reader with thinking off, and the two-reader table is the only way to separate those. Same o500 best-case retrieval as before. One answer per query.

Budget: ceiling $0.23 ($0.20 DeepSeek, $0.03 judge), enforced in the script. Lane total stays under the $1 D3 ceiling. Any 429, 402 or quota error stops the run.

## Cross-family result (2026-11-06)

Ran as registered and amended. 1,025 DeepSeek calls, 876 judge calls, 0 failed or unlabelled. Cost $0.1546 ($0.1374 DeepSeek, $0.0172 judge). Lane total about $0.78.

Gates passed. Verified queries: 149 collision, 133 absent. Hand check: 36 of 40 labels agreed, exactly the 90% limit. All four disagreements (samples 3, 19, 24, 35) label a reply as less of a refusal than it reads, so `missing` is slightly under-counted on every class, real included.

| Class | n | Flagged | 95% CI | Verdict |
|---|---|---|---|---|
| absent (`missing`) | 133 | 97.7% | 94.7-100 | Supported |
| collision (`missing`) | 149 | 96.6% | 94.0-99.3 | Supported |
| real (`missing`) | 414 | 5.6% | 3.6-8.0 | |
| control (`missing`, secondary) | 148 | 0.0% | 0.0-0.0 | |
| `_abs` (`missing` or `partial`) | 30 | 83.3% | 70.0-93.3 | Supported |
| real (`missing` or `partial`) | 414 | 7.0% | 4.8-9.4 | |

The circularity concern in the reader result did not hold up on the verifier or reader side. DeepSeek's verifier agreed with the original classes on 430 of 432 queries (148 of 149 control, 149 of 149 collision, 133 of 134 absent), so the earlier statement that the generated figures were inflated is not supported. The `gpt-6-luna` reader's rates barely move on the DeepSeek-verified set: absent 96.2%, collision 86.6%, control 0%.

Descriptive:

- The readers agree on nulls and differ on false alarms. Absent: 129 flagged by both, 2 only DeepSeek, 0 only GPT. Real: 9 by both, 14 only DeepSeek, 4 only GPT. Most false alarms are reader-specific, not a property of the question.
- DeepSeek's higher collision rate is mostly style: it says it doesn't know where `gpt-6-luna` offers general advice.
- Real flagged by type: preference 5/30, single-session-user 5/64, temporal 9/127, multi-session 4/121, knowledge-update 0/72. Preference questions ask for advice, and refusing them is a wrong kind of caution (Miami hotel).
- Whether DeepSeek's real answers were correct was not judged, so the "flagged but still right" finding applies to `gpt-6-luna` only.

Reading: on o500, a reader reliably separates missing answers from real questions, across two model families. The false-alarm cost is about 3-7% of real questions depending on reader and label. Generated controls understate that cost (0%), so real questions are the number to use.

Still open: all generated queries were written by `gpt-6-luna`; `_abs` is 30 questions; one answer per query; and retrieval is near best case. A reader can't tell "not in memory" from "not in the top 10", so distractors remain the deciding test.

Scripts and run data are not in this repo.

## Follow-up: reader test with distractors (registered before any call, 2026-11-06)

Tests the open risk: does "not in the top 10" become a false "memory lacks this"? No ingest. No LME_S graph exists on the test server by size and name (`s12` failed and took no snapshot), and an earlier note here that this needs a new build was wrong. This reuses the Phase 3b method: restore `lme-snap-o500-ship-dcf17cb-counted` to a throwaway copy and merge namespaces into groups of 10 with `p3_merge.py` and `distractor-groups.json`.

Recalls: the stored `mix.jsonl` for real and `_abs` questions; a fresh recall on the merged copy for the 432 generated queries, using the same script and arm. Embeddings only.

Reader and judge: `gpt-6-luna`, the same prompt (fingerprint `10ef922505`), same render of the 10 episodes, same blind judge, once each, on all 876 queries. All answers are fresh, since the episodes differ from the oracle run.

Rule (seed 20261111, 2000 bootstraps clustered by source question), unchanged:

- **Supported**: null flagged CI lower bound >= 0.50, AND real flagged CI upper bound <= 0.10.
- **Killed**: null flagged CI upper bound < 0.50, OR real flagged CI lower bound > 0.10.
- **Inconclusive**: anything else.

Judged for `absent` and `collision` with flagged meaning `missing`, and for `_abs` against real with flagged meaning `missing` or `partial`.

Validity gates, checked first; a failed gate means no verdict:

- Merge leaves 0 old-namespace memory nodes and edges.
- No more than 2% of recalls or calls in any class fail or are dropped.
- Hand check: 40 judge labels, seeded, stratified. At least 90% agreement.

Descriptive:

- Real flagged rate against the oracle run's 3.1% (5.3% with `partial`), same questions and reader.
- Real questions split by whether their evidence reached the top 10: an episode counts as evidence if it matches a `has_answer` turn of the question's own namespace. Reported as all evidence, some evidence, none. Flags on questions with evidence present are over-caution; flags with none are correct about the context and wrong about memory.
- Own-namespace episodes per top 10.
- Replies that use another namespace's episode as if it were the user's, from hand reads of `absent` and `collision` replies labelled `answered`.

Known limits: pools hold about 20 sessions against about 48 for LME_S. Distractors come from other LongMemEval users, so they are less alike than one person's own history. Nulls were verified against their own namespace only, and in a merged pool another namespace may genuinely hold the answer, which would count against the flag rate. One answer per query. The merged pool is a test device; recall stays single-namespace.

Budget: ceiling $0.15 ($0.12 reader, $0.03 judge), enforced by `run_flex` `max_usd`. Lane stays under the $1 D3 ceiling.

## Distractor result (2026-11-06)

Ran as registered. Merge left 0 old-namespace nodes and edges. 432 fresh recalls, 0 failed, 0 LLM calls. 876 reader and 876 judge calls, 0 failed or unlabelled. Cost $0.1085. Lane total about $0.89. Hand check: 38 of 40 labels agreed (39 if sample 26 is accepted), passing the gate. Throwaway container and volume removed; snapshot untouched.

| Class | n | Flagged | 95% CI | Oracle, same reader | Verdict |
|---|---|---|---|---|---|
| absent (`missing`) | 134 | 96.3% | 93.3-99.3 | 96.3% | Inconclusive |
| collision (`missing`) | 149 | 84.6% | 78.5-89.9 | 86.6% | Inconclusive |
| real (`missing`) | 414 | 11.4% | 8.4-14.7 | 3.1% | |
| control (`missing`, secondary) | 149 | 13.4% | 8.1-18.8 | 0.0% | |
| `_abs` (`missing` or `partial`) | 30 | 83.3% | 70.0-96.7 | 90.0% | Killed |
| real (`missing` or `partial`) | 414 | 17.9% | 14.3-21.7 | 5.3% | |

Nulls stayed flagged. The verdicts fail on real questions: flags rose from 3.1% to 11.4%, and 17.9% with `partial`, over the 10% limit. So a reader flag does not mean memory lacks the answer once distractors are present.

The cause is retrieval, not reader caution. Real questions by evidence reaching the top 10:

| Evidence in top 10 | n | `missing` | `missing` or `partial` |
|---|---|---|---|
| All | 238 | 2.9% | 5.0% |
| Some | 130 | 13.8% | 30.8% |
| None | 46 | 47.8% | 47.8% |

With all evidence present, the flag rate matches the oracle run. 176 of 414 real questions (42.5%) lost at least some evidence, and they produce 40 of the 47 `missing` flags. The reader is right about the text it saw and wrong about memory.

Descriptive:

- Own-namespace episodes per top 10: mean 2.6 for real, median 2. Distractors took most slots.
- Real flags by type: temporal 23/127, single-session-user 7/64, knowledge-update 9/72, preference 2/30, multi-session 6/121.
- The 23 `collision` replies labelled `answered` are mostly general advice that needs no memory.
- Replies using another namespace's episode as the user's were not traced. A few look like it ("Which language have I been learning?" answered English).
- Real answers were not judged for correctness here. 24 of the 46 no-evidence questions were not flagged, so they may be confident wrong answers, or the text match may have missed evidence. Unmeasured.

Reading: a reader detects "the retrieved text doesn't answer this", reliably, in both settings. It does not detect "memory doesn't hold this", because that depends on retrieval. Any wording shown to a user has to say the first. The oracle verdicts above hold only for near-perfect retrieval.

Limits: pools hold about 20 sessions against about 48 for LME_S, so real use could be worse. Other users' episodes are a different kind of distractor from one person's own history. One answer per query.

Scripts and run data are not in this repo.

## Follow-up: widen on a flag and read again (registered before any call, 2026-11-06)

Tests a two-step read on the merged 10-namespace graph. If the reader flags the top 10, recall again with more episodes and read again. `source_memory_limit` is already a per-request setting (1 to 50), so this needs no code. The owner lifted the lane cap for this test.

Protocol: a query whose distractor-run label is `missing` or `partial` is re-read with its top 30 episodes, and that reply and label are final. Every other query keeps its first reply. Same reader, prompt, render and blind label judge. The top 50 is run the same way as a descriptive comparison.

Rule on final labels (seed 20261112, 2000 bootstraps clustered by source question), unchanged:

- **Supported**: null flagged CI lower bound >= 0.50, AND real flagged CI upper bound <= 0.10.
- **Killed**: null flagged CI upper bound < 0.50, OR real flagged CI lower bound > 0.10.
- **Inconclusive**: anything else.

Judged for `absent` and `collision` with flagged meaning `missing`, and for `_abs` against real with flagged meaning `missing` or `partial`.

Correctness, descriptive: the official judge (`_judge_messages`, `_parse_yes`) scores real and `_abs` replies, first pass and final. Reported: accuracy for first pass, top 30 final and top 50 final, against the oracle arm A figure; paired difference of final minus first pass with a 95% CI; unflagged replies judged wrong, which are the confident wrong answers; accuracy by evidence in the top 10.

Validity gates: merge leaves 0 old-namespace nodes and edges; no more than 2% of recalls or calls fail in any class; hand check of 40 labels from re-read replies, at least 90% agreement.

Known limits: as the distractor test. A wider read also costs about three times the context on flagged queries. More episodes also give a null more chances to find a plausible wrong fact, and the rule measures that only through the flag rate. Nulls were verified against their own namespace only.

Safety ceiling $0.60 ($0.45 reader, $0.15 judge), enforced by `run_flex` `max_usd`.

## Widen-and-reread result (2026-11-06)

Ran as registered. Merge left 0 old-namespace nodes and edges. 377 flagged queries re-read at each size, 754 reader, 754 label and 642 correctness calls, 0 failed or unjudged. Top-50 recalls returned 42 to 50 episodes. Cost $0.1788; lane total about $1.07, over the old $1 after the owner lifted it. Hand check: 38 of 40 labels agreed (samples 1 and 13 read as less of an answer than labelled). Throwaway container, volume and tunnel removed; snapshot untouched.

| Class | n | Top 10 only | Top 30 on a flag | 95% CI | Top 50 on a flag | Verdict at 30 |
|---|---|---|---|---|---|---|
| absent (`missing`) | 134 | 96.3% | 94.8% | 91.0-98.5 | 93.3% | Supported |
| collision (`missing`) | 149 | 84.6% | 81.2% | 75.2-87.2 | 82.6% | Supported |
| real (`missing`) | 414 | 11.4% | 1.9% | 0.7-3.4 | 2.2% | |
| control (`missing`, secondary) | 149 | 13.4% | 0.0% | 0.0-0.0 | 0.0% | |
| `_abs` (`missing` or `partial`) | 30 | 83.3% | 76.7% | 60.0-90.0 | 83.3% | Supported |
| real (`missing` or `partial`) | 414 | 17.9% | 2.9% | 1.4-4.6 | 3.1% | |

Re-reading cleared 62 of 74 flagged real questions and all 21 flagged controls, and only 3 of 131 flagged `absent` and 5 of 126 flagged `collision`. Evidence on flagged real questions went from 12 of 74 complete to 68 of 74 at top 30.

Correctness on the 414 real questions, official judge, descriptive:

| Reply | Accuracy |
|---|---|
| Oracle arm A | 0.925 |
| Top 10 only, distractors | 0.720 |
| Top 30 on a flag | 0.843 |
| Top 50 on a flag | 0.850 |

Top 30 minus top 10: +0.123, CI +0.092 to +0.155, 54 gains, 3 losses. Top 10 minus oracle: -0.205, CI -0.246 to -0.164.

By evidence in the first top 10, accuracy first pass then top 30: all 0.941 to 0.937 (n=238), some 0.462 to 0.708 (n=130), none 0.304 to 0.739 (n=46).

**Confident wrong answers did not change.** Unflagged replies judged wrong: 54 at top 10, 55 at top 30, 55 at top 50, about 13.3% of real questions. A re-read only triggers on a flag, and these never flag. 44 of the first-pass 54 had incomplete evidence (33 some, 11 none), so the reader answered from part of the evidence without knowing it was partial. This is now the largest remaining failure, well above the 1.9% false flags.

Reading: on these pools the two-step read makes a flag close to "memory doesn't hold this", and recovers over half the accuracy distractors cost. It doesn't address confident wrong answers. Three of the 3 `absent` queries that flipped to `answered` are hedged best guesses, a mild cost.

Not tested: always reading top 30, which might also fix confident wrong answers. Whether top 30 is enough in a large memory: pools hold about 330 episodes, so 30 is 9% of the pool, and top 50 added little here only because of that. Real memories have far more. One answer per query. Other limits as the distractor test.

Scripts and run data are not in this repo.
