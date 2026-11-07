---
artifact_schema: 1
artifact_uuid: f65b1445-ae35-4c22-acbf-837acafeffc6
artifact_type: investigation
artifact_status: COMPLETE
---

# Menhir recall: test whether a real cosine beats rank scores

> Completed test record, copied on 2026-11-07 from the maintainer's workspace plan of the same
> name. Local paths, host names and private notes were removed and stale status lines updated. The
> rules and results are otherwise unchanged. Run scripts and data are not in this repo.

- **Status:** Closed 2026-11-06, see Closeout
- **Date:** 2026-11-05
- **Projects:** Menhir (instrumentation, Phase 1); archolith-bench (labels and analysis, Phase 2). One project per session.
- **Related:** #392 (conflict similarity floor, same rank-vs-cosine confusion), #434 (context tokens), #444 (coding-memory hit@k), archolith/menhir#123 (Gate E IR numbers)

## Compact Summary

Every score and relevance label recall shows is a rank-fusion position, and the real cosine is computed and then discarded. The theory is that a real cosine, used as a floor and as a calibrated relevance estimate, separates answer-bearing memories from noise better than the rank score and loses no answers. The test builds on Recall Lab:

1. Record the real cosine and provenance for every candidate.
2. Run recall only (no LLM) in batches over LongMemEval o500 and labelled null queries.
3. Label candidates from LME `has_answer` turns.
4. Test four pre-registered hypotheses with kill criteria.

The no-LLM phases cost only embeddings. End-to-end answer checks are about $1 and need approval. Nothing here changes production. A positive result leads to separate PRs.

## What recall does today (verified in code, 2026-11-05)

- **`search_scored`** (`graphiti_client.py:747-853`) fuses BM25 and `name_embedding` cosine with `rrf()`: `1/(i+1)` per lane, 0-based. The top hit of one lane scores 1.0, and the top hit of both lanes scores 2.0. It returns only `(uuid, name, rrf_score)`. The cosine lane pre-filters at `DEFAULT_MIN_SCORE` 0.6 on Neo4j's (1+cos)/2 scale, which is a raw cosine of about 0.2.
- **The scoring floor** `MIN_SIMILARITY_THRESHOLD` 0.15 is applied to that RRF score. It is a rank cut, not a similarity cut. `final_score` = similarity + preset-weighted adjacency, recency, prominence (normalized within the batch) and conflict, plus a type boost.
- **Labels** (`formatters.py:275-296`) give `relevance` high if the RRF score is at least 0.7 and medium if at least 0.4. `relevance_basis` is "legacy_rrf_threshold_unvalidated". So the top hit of any lane that returns anything is labelled **high**, whatever it is about.
- **Real cosines exist elsewhere.** The source-memories lane (episode `content_embedding`) and the observation lane (assertion `stated_span`, floor raw 0.35) use real cosines. `count_similar_by_cosine` (:855) is a true-cosine filter.
- **Recall Lab** traces `bm25_rank`, `cosine_rank` and `content_rank`. `content_cosine` is always None. It serializes `score_parts` only for final results, drops `provenance`, and has no batch mode, no gold labels and no cosine-floor knob.
- **Already tried and removed:**
  - Content-vector lane: no `Entity.content_embedding` writer ever existed, so that result is weak evidence.
  - Fact-edge lane: 8/30 vs 9/30.
  - BM25 hybrid: 0.700 vs 0.710.
  - Normalized similarity scale: 143/500 on both, the same set.
  
  None of these measured a real cosine against labelled candidates, which is what this plan does.

## Hypotheses (pre-registered)

The unit of analysis is the question. CIs come from 2,000 bootstrap resamples over questions, with paired differences. Seeds, splits, thresholds and analysis code are fixed and committed before the Phase 3 data is opened.

| # | Claim | Primary measure | Supports the claim if | Kills the claim if |
|---|---|---|---|---|
| H1 | Rank-based labels call off-topic hits relevant | Share of null queries whose result list contains a `high` label, against real queries | Null queries get `high` at least 50% as often as real ones | Null queries get `high` under 10% as often as real ones (lane pre-filters already guard it) |
| H2 | Real name cosine separates answer-bearing candidates better than the RRF score | Candidate AUC and PR-AUC, pooled and mean within-question; paired difference cosine minus RRF | Difference at least +0.05 AUC, CI above 0 | CI includes 0, or RRF wins |
| H3 | A cosine floor removes noise without losing answers | Per floor tau: questions that lose their last answer-bearing result in top-k; noise share in top-k; `build_context` tokens; then answer accuracy | Some tau loses answers on no more than 1% of questions, cuts non-answer results in top-k by at least 25% or tokens by at least 15%, and stays non-inferior on accuracy (paired, margin 1.5 pp) | No tau meets the loss bound with any noise gain |
| H4 | A calibrated P(answer-bearing) built from cosine, lane agreement, rank and source stays calibrated out of sample | ECE and Brier on held-out question types, then on R1 | Held-out ECE at most 0.05, R1 ECE at most 0.10, and better than calibrating the RRF score alone | Held-out ECE above 0.10, or no gain over a calibrated RRF |

H2 is the hinge. If it is killed, H3 and H4 are reported as exploratory only, and the theory is false as stated.

### Rival explanations and the control for each

| Rival | Control |
|---|---|
| Labels track hub entities, such as the user entity mentioned by every episode | Exclude hubs (entities MENTIONed by more than 20% of a namespace's episodes); report H2 with and without them |
| Cosine only proxies lexical overlap | Add BM25 rank and token-overlap baselines; report AUC of cosine given overlap (stratified) |
| Any per-query normalization would do as well | Baseline: RRF min-max normalized per query, and a per-query rank percentile |
| Name embeddings are too short; summaries carry the signal | Score summary cosine and max(name, summary) offline as signals only. A summary win reopens the content lane as a finding, not a ship |
| Misses dominate, so scoring cannot help | Report the coverage ceiling: share of questions with any answer-bearing candidate in the full pre-floor pool (candidate_k 50). Gate E has gold present@10 at only 189/500 |
| Oracle haystacks are too clean to show noise | Distractor setting (Phase 3b) and R1 |
| Label noise from heuristic mapping | LLM judge on a stratified sample; report the labeller's precision and recall and rerun H2 under the judge labels |

## Data

### Primary: LongMemEval o500 oracle, graph `o500-counted`

This is the canonical 0.3.0 graph that Gate E's IR numbers were served on.
- Ingest was adaptive segmentation, store-only user context, and assistant turns evidence-only.
- Embedder: `text-embedding-3-small`. Phase 0 confirms the vector dimension and presence.
- 500 questions, a median of 23 turns. 479 have at least one `has_answer` turn. 21 `_abs` items have none.
- In oracle, haystack sessions equal `answer_session_ids` for all 500, so session-level labels carry no signal. **Labels must be per turn.**
- Assistant-side gold (54 of 896 has_answer turns, mostly SSA) is largely unreachable on this graph, where SSA support present@10 is 7/56. Report SSA separately and leave it out of the primary pooled numbers.

### Null queries (H1, and negatives for H3)

- **Swapped:** question j against namespace i, with j drawn from a different question type, seeded. Namespaces are per question (`lme-<question_id>`), so the answer is absent by construction. Topics such as "the user's job" can still overlap, so a judge sample of 100 swapped results checks for accidental relevance.
- **Abstention:** the 21 `_abs` items in their own namespace.

### Distractor setting (Phase 3b)

Search namespace i plus 9 random others, if recall can take several group_ids; Phase 0 checks. Candidates from the other namespaces are clean negatives. This turns the oracle graph into a noisy haystack with no new ingest. If recall cannot do this without a code change, use D4.

### Transfer corpus (H4): R1 `fixtures/r1_dummy_gold.json`

159 coding memories and 135 queries with `support_ids`. The families are symbol 40, paraphrased_debug 40, exact_error 30 and wrong_repo 25; wrong_repo is a natural null family. It needs a small graph built (the D3 budget). `run_r1_live.py` already maps entity uuids to fixture memories (first source wins).

## Labels (Phase 2)

A candidate is answer-bearing if it traces to a `has_answer` turn of that question.

- **Turn identity** is recomputable offline. `turn_key = lme-<qid>|lme-<qid>-<sid>|t<n>`. The queue Episodic `ingest_key` is `sha256(prefix + ns + \0 + idempotency_key)`, where the key is `turn_key#s<seg>` or `#w<writer>`. TurnEvidence `turn_key` is `sha256(ns \0 key)`. Enumerate every (session, turn, segment) from the dataset and join on the hash.
- **Entity candidates:** Entity, then MENTIONS from a Graphiti Episodic, then the receipt `resolved_episode_uuid`, then the queue Episodic `ingest_key` (or `ADMITTED_ON` TurnEvidence), then turn_key, then `has_answer`. Most user episodes are `verbatim_skipped` since about 2026-10-25, so provenance often runs through the evidence-projection episode; follow that path too.
  - *Strict:* not a hub, and at least one MENTIONS resolves to a has_answer turn.
  - *Lenient:* any MENTIONS resolves to a has_answer turn.
  - Use the full MENTIONS set, not the 5-turn `provenance.turn_evidence_ids` cap.
- **Source-memory candidates:** the queue Episodic itself, via `ingest_key`, which gives an exact turn.
- **Views, scalar and observation results:** label via `episode_uuids` and `TypedAssertion` FOUNDS/GROUNDS where present. Report them as their own stratum; they use real cosines already and are not part of H2.
- **Fallback** where no link resolves: the 60-char normalized prefix match from `entropy.py`, recorded as `label_route=prefix` so it can be reported separately.
- **Judge sample:** 300 candidates stratified by label, lane and cosine decile, plus 100 swapped-null results, judged against the gold answer with a cheap model. It estimates labeller precision and recall. The prior gold-aware review found 27 of 49 "complete misses" had useful content, so heuristic labels undercount.

## Phases

### Phase 0: feasibility (read-only, throwaway container)

Restore a copy of `dcf17cb-counted` into a uniquely named throwaway Neo4j (D1). Answer with counts, not samples:

1. Share of `:Entity` nodes with a non-null `name_embedding` of the expected dimension.
2. Share of entities in 20 random namespaces whose MENTIONS resolve to at least one turn_key by each route (ADMITTED_ON, receipt plus ingest_key, evidence projection, prefix). **Gate: at least 90% resolvable by a non-prefix route.** Below that, stop and report: labels would be too noisy for H2.
3. Which recall steps call an LLM. Read `RecallLabTuning` defaults (oracle ranking, intent lens, warden, contradiction interrupt) and the MCP `recall_memories` path. The **baseline arm is production MCP defaults**, not Lab defaults. Any LLM step in the baseline sets the batch cost.
4. Whether `run_recall` accepts several namespaces (for Phase 3b).
5. The pre-floor candidate pool size per query, and whether trace candidates include floor-dropped ones (`retrieval_trace_models.py:46-58` says they do).

Exit: a short Phase 0 note appended to this plan with the five answers. Cleanup: the container and tunnel are removed.

### Phase 1: Menhir instrumentation PR (diagnostic only, default off)

Files, subject to Phase 0 and a blast-radius check:

- **`src/menhir/explorer/recall_lab.py`**
  - Serialize **every trace candidate**, including floor-dropped ones, with: `score_parts`, raw prominence before batch normalization, lane ranks, `survived_floor`, `name_cosine`, the MENTIONS episode uuids, and the queue-episode `ingest_key`. Today only final results are serialized and `provenance` is dropped.
  - Add `RecallLabTuning.name_cosine_floor: float | None = None`.
- **`src/menhir/infrastructure/graphiti_client.py`**
  - One batched read of `name_embedding` for a uuid list, reusing `fetch_pair_name_embeddings` if #392 has merged (D5).
  - The query vector comes from `embed_query` (:738).
  - Cosine via `pair_cosine` (`correlation_service_signals.py`), or Neo4j `vector.similarity.cosine` converted with 2s-1. Pick one and test it against the other.
- **Recall pipeline, scoring floor:** apply `name_cosine_floor` only when the tuning sets it. Two arm variants:
  - (a) floor on graphiti candidates only, with `FLOOR_EXEMPT_SOURCES` kept;
  - (b) floor on every candidate that has a name embedding.
  
  A candidate with no vector is never dropped, which mirrors #392's no-signal rule.
- **`scripts/recall_lab_batch.py`** (new): reads a JSONL of `{namespace(s), query, arm}`, runs `run_recall` with `trace=True` against a given graph, and writes JSONL candidates. No answer model, no judge.

Acceptance:
- **Parity.** With `name_cosine_floor=None`, results and order are identical to main for 50 fixed queries on the Phase 0 graph (online test).
- **Cosine agreement.** `name_cosine` matches Neo4j's value to within 1e-6 after the scale conversion.
- **No production read path change.** `recall_memories`, `build_context` and the API return byte-identical payloads (existing tests are untouched; one new parity test).
- Focused tests only; CI runs the full suite.

### Phase 2: bench labeller and analysis (archolith-bench, offline, no LLM)

- **`scripts/longmemeval/analysis/candidate_labels.py`:** the Labels section above. Inputs are the batch JSONL, the dataset, and a read-only graph. Output is per-candidate `label_strict`, `label_lenient`, `label_route` and `is_hub`.
- **`scripts/longmemeval/analysis/recall_signal_eval.py`:**
  - AUC and PR-AUC with question-bootstrap CIs.
  - A floor sweep that re-runs nothing: it reads the floor-arm batches.
  - Calibration fits (logistic and isotonic) with reliability tables.
  - Per-type breakdowns.
  - A flips list for H3, naming the questions whose answer-bearing result moved and whether the floor fired on them.
- Summary cosine for the H2 rival: embed entity summaries once. This is an embeddings-only cost, under $0.05 for o500.
- Unit tests on a hand-built 3-question fixture with known AUC and known labels.

### Phase 3: H1 and H2 run (no LLM)

- **Batches:** 500 real queries, 500 swapped nulls and 21 abstention items, on the baseline arm. Cost is query embeddings only, about $0.001, if Phase 0 finds no LLM in the baseline.
- **3b, if feasible:** the 500 real queries in the 10-namespace distractor setting.
- **Output:** H1 table; H2 pooled and within-question AUC for RRF, final_score, name cosine, summary cosine, max, normalized-RRF, BM25 rank and overlap; strict and lenient labels; with and without hubs; and the coverage ceiling.

### Phase 4: H3 floor (sweep without LLM, then a paid end-to-end check)

- **Sweep:** raw tau in {0.10, 0.15, ..., 0.50} on both arm variants, as batches. Report per tau and per k in {5, 10}: questions losing their last answer-bearing result, noise share, and `build_context` tokens at the default 2000 budget.
- **End-to-end:** baseline plus the best 2 tau, 2 repeats each, on o500 with the bench answer model and judge (Flex, about $0.11 per arm-repeat, so about $0.66). The test is paired McNemar plus the flips list. Per the standing rule, no claim of gain is made from aggregate deltas inside noise: each flip is checked for whether the floor actually fired on that question.

### Phase 5: H4 calibration and transfer

- **Splits, fixed now:**
  - Primary: fit on KU, MS and SSU (281 questions); test on TR and SSP (163). SSA is reported alone.
  - Sanity check: a 5-fold split by question id.
- Features: name cosine, summary cosine, lane agreement (present in both lanes), RRF rank, and source.
- Compare with isotonic-calibrated RRF.
- Transfer to R1 (build its graph under D3) with no refit. wrong_repo queries count as negatives.

### Phase 6: results and decision

Write `.agent/reviews/menhir-recall-real-similarity-results.md` with:
- Verified-vs-reported per hypothesis.
- The labeller's precision and recall from the judge sample.
- Remaining assumptions and a Review Confidence score.

Decisions map to separate, later PRs:

| Outcome | Follow-up |
|---|---|
| H1 holds (expected from code alone) | Rename the label basis honestly, or stop calling it relevance. Small PR; worth doing whatever H2 shows |
| H2 and H4 hold | Replace `relevance` with calibrated probability bands, and expose the cosine |
| H2 holds, H4 fails | Expose raw cosine with its scale stated. No probability label |
| H3 holds | A floor PR at the chosen tau and variant, default on only after a second graph agrees |
| H2 killed | Theory false as stated. Keep RRF and record why. If summary cosine won, open a content-lane research issue |
| Coverage ceiling low (for example under 60%) | Record that candidate generation, not scoring, is the main recall problem |

Also store the outcome in memory, unflagged.

## Cost

| Item | Estimate | Approval |
|---|---|---|
| Query and summary embeddings, all phases | Under $0.10 | None needed |
| Judge label sample (400 items, cheap model) | About $0.05-0.10 | Pre-approved (under $0.25, with monitor) |
| R1 graph ingest (159 memories) | About $0.05-0.15, estimated from about $0.00035-0.0017 per turn | Pre-approved if monitored under $0.25 |
| Phase 4 end-to-end, 3 arms x 2 repeats | About $0.66 | **D3** |
| LME_S rebuild (only if D4 picks it) | $5-9, plus an index-size fix first | **D4** |

Every paid step runs with a cost monitor, a hard ceiling and a progress-feed entry.

## Owner decisions

- **D1 Graph.** May Phase 0 restore a copy of `o500-counted` into my own throwaway container? The snapshot stays untouched; no shared bench container is used. Recommended: yes.
- **D2 Real queries.** Use none from your Menhir memory. The plan does not need them. A later fixture with gold answers (your stated next step) can add them with your sign-off.
- **D3 Budget.** Allow about $1 total ceiling for Phase 4 end-to-end, the judge sample and the R1 build.
- **D4 Noisy haystack.** If the multi-namespace distractor setting is not possible, choose between a small recall-pipeline change to allow it (preferred, still diagnostic-only) and an LME_S subset rebuild ($5-9, which first needs a fix for the s12 failure: "Property value is too large to index" on `episodic_content_idx`).
- **D5 Order.** Start Phase 1 after #392 merges so it can reuse `fetch_pair_name_embeddings`, or duplicate a small read now.

## Out of scope

- Any production default change, gate or threshold.
- Conflict detection (#392, #511).
- Candidate-generation changes (new lanes, chunking).
- The gold-answer fixture for your own memories.
- Rewriting `relevance` before results exist.

## Known limits of this design

- **The labels are heuristic.** The judge sample measures their error but does not remove it.
- **The oracle graph is small and clean.** Without the distractor setting or R1, H3 can only show "no harm", not "less noise".
- **Assistant-turn gold is mostly absent** from this graph, so the results speak for user-stated facts.
- **One embedder** (`text-embedding-3-small`). The thresholds do not transfer to another model without a rerun.
- **H1 is close to certain from the code.** The run measures how often it bites, rather than whether it can.

## Owner decisions recorded (2026-11-05)

- **D1 yes.** Restore a copy of `o500-counted` into a throwaway container.
- **D2 agreed.** No real queries from the owner's memory for now.
- **D3 yes.** About $1 total ceiling.
- **D4 approved.** Read as the preferred option: a small diagnostic recall-pipeline change if multi-namespace search needs one. No LME_S rebuild.
- **D5 approved.** Read as the first option: Phase 1 starts after #392 merges. #392 was open at 2026-11-05.

## Phase 0 note (2026-11-05)

Measured on a throwaway copy of `o500-counted`, whole graph, no sampling. No label was compared with any cosine, so the pre-registration still holds. Phase 2 rebuilds this logic in `candidate_labels.py`.

1. **Embeddings.** All 11,177 plain entities have a 1536-dim `name_embedding`. The 5,334 `scalar_state` and `scalar_history` Views and 291 `admission_audit` nodes have none, so a cosine floor has no signal for them and never drops them.
2. **Label gate: passed.**
   - The recomputed `turn_key` hash matches 10,956 of 10,956 TurnEvidence nodes.
   - All 896 `has_answer` turns have TurnEvidence (842 user, 54 assistant).
   - 10,887 of 11,177 entities (97.4%) resolve to a turn. The working route is MENTIONS, then the receipt `resolved_episode_uuid`, then `ADMITTED_ON`.
   - The other 290 are mentioned but unresolved. They get `label_route=none` and are reported separately.
   - `ingest_key` hashing is not needed for entities. TurnEvidence MENTIONS reach no plain entity (Views only).
   - Source memories: 5,507 of 5,803 episodes with a vector have a turn.
3. **LLM in recall.** Grep finds no LLM call in `services/recall*.py` or `domain/oracle*.py`, and the Lab judge defaults to off. This is a static check, so the Phase 1 batch must assert zero LLM calls at run time.
4. **Several namespaces.** Recall takes one namespace. `namespace_to_group_ids` returns one group, and `None` means every group. Phase 3b needs the D4 change for a 10-namespace pool. Other lanes' behaviour under `None` was not checked.
5. **Pool size.** Upper bound only: plain entities per namespace are min 2, median 22, p90 39, max 82, and only 16 of 493 namespaces exceed `candidate_k` 50. The exact pool comes from the Phase 1 batch. `CandidateTrace` has `survived_floor`.

**Changes to the plan from these results:**

- **Phase 3b is now required for H3's noise claim.** With about 22 entities per namespace, the pool is roughly the whole namespace, so the oracle graph alone can only show "no harm".
- **Entity coverage ceiling, known before any run.** Of 479 questions with gold, 427 have an answer-bearing entity (lenient) and 401 strict. 7 have no entities at all and 45 have entities but none linked to a gold turn. Those 52 can only be answered through other lanes, so they count as misses in entity-level H3.
- **Positives are common.** Median positive share per namespace is 28.6% lenient and 17.3% strict. Report PR-AUC against that base rate.
- **Hub rule.** The 20% rule marks 1,471 entities, partly because namespaces are small. It is kept as written, and H2 is reported both ways as planned.

**Phase 1 stays on hold** until #392 merges (D5).

## Phase 1 note (2026-11-05)

The owner said to go without waiting for #392, so Phase 1 branched from main with its own small embedding read.

**Changes from the Phase 1 section:**

- **Gated on an explicit `record_name_cosine` tuning, not on `trace`.** `frontier_shadow` turns `trace` on for production recalls (`backend_runtime_data_ops.py:334`).
- **Cosines live in `RecallResult.name_cosine`, not on `CandidateTrace`.** The `RetrievalTrace` shape is pinned by `test_recall_trace_boundaries.py`.
- **No provenance fields or raw prominence were added.** Phase 0 showed the labeller can join on uuid from the graph, and floor arms are real reruns, so offline re-scoring is not needed.
- **The batch serializes candidates itself.** `recall_lab.py` gained the tuning fields and a per-result `name_cosine` only.
- **D4: no multi-namespace change.** A tuning that widens search across namespaces would widen a tenant boundary through an API-exposed setting. Phase 3b should instead merge groups of 10 namespaces inside its throwaway graph copy (rewrite `group_id` and `namespace` there), which needs no recall change. Not built or tested yet.
- **Token metric.** The batch records estimated tokens per result, not `build_context` tokens, because `build_context` recalls with access updates on. H3 tokens are therefore an estimate of result content.

**For Phases 3 and 4:**

- **Query vectors must be pinned.** Embedding jitter flipped about 1 in 50 questions between reruns. The batch pins vectors per input in `<output>.vectors.json`. Run all arms against one output or copy the vector file.
- **The existing floor is a large rank cut.** On a 20-question smoke, the baseline arm dropped 185 of 393 candidates at the 0.15 RRF floor. These stay in the output with `survived_floor=false`, and H2 scores them too. H3's baseline includes this cut.
- **Floor variants may be identical.** In the smoke, every candidate was either `vector` or had no name vector, so variants (a) and (b) gave the same results. If that holds on 500 questions, drop variant (b) from the end-to-end check.

No cosine was compared with any label, so the pre-registration still holds.

## Phase 2 note (2026-11-06)

Built in archolith-bench: `candidate_labels.py`, `recall_summary_cosine.py` and `recall_signal_eval.py` under `scripts/longmemeval/analysis/`, with `tests/test_recall_signal_analysis.py`.

**Choices the plan left open, fixed now before any data is opened. Owner to confirm:**

- **H2 primary cell:** strict label, hubs removed, SSA and null queries out. The other three label and hub cells are sensitivity checks.
- **H2 reading, against RRF:**
  - Supported needs a gain of at least +0.05 with CI above 0 on both pooled and mean within-question AUC.
  - Killed means neither CI is above 0, or RRF wins on either.
  - Anything else is inconclusive, including a gain on one scale only.
- **H3 reading:** k=10, noise and tokens over real plus null queries, loss over real questions outside SSA, and "answer-bearing" means the lenient label. k=5 is reported only.
- **H4 reading:** the plain logistic model is the primary model, and "better than calibrated RRF" means lower held-out Brier. The isotonic layer is fitted on the same data as the logistic, so its numbers are reported but not used for the verdict.
- **Unknown labels:** an untraceable candidate gets a `None` label and is left out of AUC and noise counts, never counted as a negative. Prefix-route rows count as positives and are reported by route.

**Changes from the Labels section:**

- **Hub rule uses episodes, as the plan says.** It marks 1,623 entities. Phase 0's 1,471 divided by turns.
- **Views label through `TurnEvidence-[:MENTIONS]->View`, then `FOUNDS` through the anchor edges, then `episode_uuids`.** Observations are `TypedAssertion` ids and label through `FOUNDS`.
- **Prefix fallback uses `has_answer` turns only, not the adjacent assistant reply `entropy.py` also takes,** and only inside the question's own namespace.
- **Batch ids:** a swapped null is `<question_id>@<namespace>`. Phase 3's input file must use this.
- **Phase 3b:** label against the unmerged graph, since labels join on uuid and hubs are per original namespace.

**Not built here:**

- The paired accuracy test for H3 (McNemar and non-inferiority). A floor that clears the sweep stays `pending_accuracy` until Phase 4 adds it.
- The R1 transfer for H4. The report saves the coefficients, and the status stays `pending_r1`.
- The judge sample (paid), and the seeded swapped-null input file. Both belong to Phase 3.

**Checks:**

- Whole-graph labels on a throwaway copy reproduce Phase 0: 11,177 entities, 10,887 resolved, 427 lenient questions. Strict is 400, not 401, because of the hub denominator.
- On the real 20-question smoke batch, all 564 labels resolved to a node, and 18 had no route.
- `recall_signal_eval.py` ran on that batch with **shuffled** labels, so no cosine met a true label.

No cosine was compared with a true label, so the pre-registration still holds.

## Phase 3 note (2026-11-06)

**H1 supported. H2 killed. H3 and H4 are now exploratory only.**

Run from `cae1427d` (Menhir) and bench `d8a135f` plus the query builder `5921e6b`. The owner said to continue without changing the Phase 2 readings, so they stayed as written. `recall_signal_eval.py` hash at run time: `4b5c227ce47c0478`. 1,500 recalls, 0 failed, 0 LLM calls. Total spend about $0.03 (embeddings plus $0.0065 judge), an estimate for the embeddings.

**H1.** 92.5% of 530 null queries got a `high` label, against 98.5% of 470 real ones. Ratio 0.939 [0.913, 0.963]. The judge found 0 of 100 sampled `high` swapped-null results relevant.

**H2, primary cell (strict, hubs out, no SSA), cosine minus RRF:**

| Graph | Questions | Pooled AUC | Within-question AUC |
|---|---|---|---|
| Oracle | 405 | -0.033 [-0.049, -0.017] | -0.036 [-0.054, -0.019] |
| 10-namespace distractor | 414 | +0.002 [-0.011, +0.016] | -0.017 [-0.030, -0.004] |

RRF wins on at least one scale on both graphs, which is the kill rule. Checked: no missing cosines, and a separate brute-force recompute matched both headline numbers to four decimals.

**Summary cosine beats RRF under the heuristic labels:** pooled +0.062 [+0.046, +0.078] and within +0.057 [+0.039, +0.074] on oracle; +0.086 [+0.075, +0.097] and +0.069 [+0.058, +0.080] on distractor. **The judge does not confirm this:** see below.

**Judge control (288 stratified candidates, 100 swapped-null results, gpt-6-luna Flex):**

- **Label precision is 0.102 and recall 0.853, weighted.** Most "answer-bearing" entities are mentioned in a gold turn but do not state the answer in their name or summary. The label measures "mentioned in the gold turn", not "carries the answer".
- **Only 17 judge positives,** so this is underpowered. Under judge labels: name cosine minus RRF -0.128 [-0.316, +0.027], summary minus RRF +0.013 [-0.116, +0.122]. Name cosine still doesn't win, which the kill rule also reads as killed. The summary win is unconfirmed, even though the judge reading the summary should favour it.
- **The same sample under heuristic labels** gives summary minus RRF +0.040 [-0.016, +0.099], so this sample size could not have confirmed the full-data gain either.

**Coverage and the rank floor (heuristic labels, 414 questions outside SSA):** strict labels give pool 391, after the floor 387, top 10 381, top 5 352. The floor drops 35% of positive candidates and 52% of negatives, but costs only 4 questions their last positive. Given the label precision, these counts overstate how often the answer itself is reached, so the "coverage ceiling low" branch can't be read from them.

**Corrections and changes:**

- **The Phase 1 note overrated the rank floor as a possible main cause.** It is not, on these labels.
- **Question counts:** there are 30 `_abs` questions, not 21; 9 of them have `has_answer` turns. So real is 470 and abstention 30. The 21 is the count of questions with no gold.
- **`strict_no_hubs` and `lenient_no_hubs` are the same cell by construction,** so H2 has three distinct cells, not four.
- **Phase 3b merge:** works as planned on Entity, Episodic, TurnEvidence, TypedAssertion, TypedAssertionHead and edges. Watermark and flag nodes are unique per group and stay unmerged; recall doesn't read them. Pool size hits `candidate_k` there (median 50).
- **A better label is needed before any further comparison.** Any Phase 4 or content-lane work on these labels inherits the 0.102 precision.

## Owner decisions after Phase 3 (2026-11-06)

- **H1 follow-up:** Menhir PR #514 changes `relevance_basis` to `rank_score_threshold_not_similarity`. The `relevance` key stays.
- **Phase 4 is dropped.** The paid run would test a cosine floor that H2 failed to support. No floor work follows from this plan.
- **Better labels first, then retest summary cosine.** Rule below, written before any judge label for this retest existed.

## Judge-label retest: pre-registered (2026-11-06)

**Question:** does summary cosine, or name cosine, beat RRF at ranking candidates that really carry the answer?

**Labels.** gpt-6-luna on Flex judges every own-namespace plain-entity candidate of real questions outside SSA, once per (question, entity), on both graphs. Candidates from another namespace in the distractor graph are negatives by construction. The judge sees the question, gold answer, gold-turn text (truncated), entity name, summary and up to 8 RELATES_TO facts. It never sees a score, rank or heuristic label.

**Criterion.** True if the memory states the gold answer or a specific fact needed to derive it. Same topic is not enough. One call returns two verdicts:

- `in_record` (name, summary or facts): **primary**. It is less circular for summary cosine, because the answer may sit outside the summary.
- `in_summary` (name or summary only): sensitivity check.

**Scoring.** The frozen `recall_signal_eval.py` code, unchanged: `candidate_table`, `cell_rows` on `strict_no_hubs` with the judge verdict as the label and `is_hub` false, `signal_metrics`, `bootstrap_weights`, `interval`, SEED 20261105, N_BOOT 2000, resampled by question. Paired differences against RRF.

**Reading, per signal and per graph, on `in_record`** (the H2 rule):

- Supported: gain of at least +0.05 with CI above 0 on both pooled and within-question AUC.
- Killed: neither CI above 0, or RRF wins (CI below 0) on either.
- Otherwise inconclusive.

**Overall.** Summary cosine is supported only if supported on both graphs, and killed if killed on either. Name cosine is read the same way. A different `in_summary` reading is reported next to the result and does not override it.

**Validity checks, fixed now:**

- At least 100 positives per graph, otherwise that graph reads underpowered, not killed.
- At most 2% of pairs unjudged. Unjudged pairs are left out and counted.
- Repeat 200 random pairs. Report agreement on `in_record`, and on the first run's positives separately. If agreement on those positives is under 0.8, every verdict above is downgraded to inconclusive.
- Read 15 disagreements between judge and heuristic labels by hand, and report what they show. This does not change the verdict.

**Cost.** Hard ceiling $0.50 across pilot and run. A pilot of 200 calls measures cost only; its verdicts are not read before the rule is applied.

**Known limits.** The judge reads the summary, so a summary-cosine win is still partly circular. Pools hold only what retrieval returned, so this says nothing about answers never retrieved. One judge model, one dataset family.

## Judge-label retest: result (2026-11-06)

**Pre-registered reading: summary cosine supported, name cosine killed, on both graphs and both label targets. The exploratory checks below shrink what that means: about half the positives are one kind of entity, and without it the gain falls below the +0.05 bar.**

Run from bench `2978421`, frozen analysis unchanged. 8,059 pairs judged, 0 unjudged, 0 entities unread, 468 entities had facts capped at 8. Spend $0.2656 including pilot and repeats.

**Primary, `in_record`, minus RRF (414 questions):**

| Graph | Positives | Signal | Pooled AUC | Within-question AUC | Reading |
|---|---|---|---|---|---|
| Oracle | 684 | Summary cosine | +0.083 [+0.064, +0.103] | +0.177 [+0.156, +0.199] | Supported |
| Oracle | 684 | Name cosine | -0.288 [-0.312, -0.265] | -0.302 [-0.327, -0.277] | Killed |
| Distractor | 647 | Summary cosine | +0.099 [+0.086, +0.113] | +0.133 [+0.115, +0.152] | Supported |
| Distractor | 647 | Name cosine | -0.225 [-0.250, -0.203] | -0.264 [-0.290, -0.238] | Killed |

`in_summary` reads the same on every cell. A separate brute-force recompute matches the summary and RRF numbers on both graphs.

**Validity checks:** positives pass (at least 100). Unjudged 0%. Repeat agreement 0.985 on 200 pairs, and 12 of 14 on first-run positives (0.857, passes 0.8, but n=14 is a thin check).

**Exploratory, not pre-registered:**

| Rows kept | Oracle pooled / within | Distractor pooled / within |
|---|---|---|
| All | +0.083 / +0.177 | +0.099 / +0.133 |
| No heuristic hubs | +0.045 / +0.084 | +0.066 / +0.074 |
| No `user`-like entities | +0.024 / +0.058 | +0.046 / +0.051 |
| Neither | +0.032 / +0.068 | +0.056 / +0.060 |

- **47% of positives are `user`-like entities** (322 of 684 oracle, 307 of 647 distractor), which are 7% and 11% of rows. Removing them, the rule would read inconclusive on both graphs. The sign holds, with CIs above 0 on both scales.
- **Summary length is a strong rival on oracle:** length alone scores +0.036 / +0.159 against RRF. Summary cosine over length is +0.047 [+0.023, +0.071] pooled and +0.018 [-0.006, +0.044] within. On distractor, length loses to RRF and cosine beats length by +0.204 / +0.167.
- **Oracle's shortest-summary third shows no clear gain** (-0.015 / +0.036, both CIs span 0).
- **Circularity is not removed.** Only 41 positives are record-only. Summary cosine still beats RRF on them (+0.113 / +0.123 oracle, +0.120 / +0.137 distractor), but that is a small slice.
- **Missing summaries gave no free win:** 0 scored rows lack one.
- **Name cosine's loss is partly structural.** A name can rarely state an answer, so this does not show names are useless for finding entities.
- **Null separation, new:** the best summary cosine per query averages 0.566 on real questions and 0.173 on swapped ones, AUC 0.993. Swapped queries are easy nulls from an unrelated user. Topic present but answer absent is untested.

**Hand read (15 of 2,080 judge/heuristic disagreements, 477 judge-only positives and 1,603 heuristic-only):** 4 of 8 positives are visibly right in the first 260 characters. The other 4 are long `user` summaries I could not confirm from that excerpt. The 7 negatives are mostly right, but two look strict: a dated workshop fact and a dated cuttings fact were marked false on questions that need them. So the judge may undercount partial facts in derived answers.

**Decision reading:** the plan's row applies: H2 stays killed for name cosine, keep RRF for now, and summary cosine justifies a content-lane research issue. That issue should treat the `user` entity and summary length as the first thing to control. Not opened here. Phase 4 stays dropped. Total plan spend about $0.30.

## Closeout (2026-11-06)

Owner approved. #513 closed unmerged, branch kept at `cae1427d`. #470 closed as not planned: name-cosine floor not pursued, though no floor was tested directly. Its false-positive problem moves to #515, the content-lane research issue. #433 stays open. Bench #51 pins `recall_lab_batch.py` to `cae1427d`.
