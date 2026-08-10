# RCA: Conflict detection applies a cosine threshold to RRF rank scores

**Date:** 2026-08-09
**Severity:** Medium. Detecting a conflict is itself non-destructive, so nothing has been lost by
this alone. It matters for three reasons: it floods the operator queue with non-contradictions
(~3 of 7 current groups), `conflict_status == "unresolved"` feeds recall scoring
(`scoring_service.py:156`), and it is the input to the destructive suggestion in the companion RCA
`rca-conflict-suggestion-destructive-default-2026-08-09.md`. Together those two form a path from
"two facts look alike" to "delete one."
**Status: ROOT CAUSE CONFIRMED by code read plus arithmetic; one live case traced end-to-end.**

## Summary

`SIMILARITY_CONFLICT_THRESHOLD = 0.85` is a cosine-calibrated constant. The value it is compared
against is a **graphiti RRF rank-fusion score**, not a similarity.

This is not merely a scale error. RRF is **ordinal** — it encodes *rank position only* and discards
similarity magnitude entirely. Two entities that are 0.99 cosine-similar and two that are 0.30
cosine-similar produce the *identical* RRF score if they occupy the same rank. **No threshold on an
RRF score can express "these are 85% similar,"** because the quantity carries no similarity
information. Rescaling cannot fix an ordinal/cardinal mismatch; only changing the signal can.

## 1. The arithmetic

`lifecycle_consolidation.py:395-397` obtains the score from
`graphiti_client.search_scored(query, num_results=5, group_ids=...)`, which configures
(`graphiti_client.py:942-950`) two search methods — `bm25` and `cosine_similarity` — fused by
`NodeReranker.rrf`.

graphiti's `rrf` (`.venv/.../graphiti_core/search/search_utils.py:1780-1786`):

```python
def rrf(results, rank_const=1, min_score=0):
    for result in results:
        for i, uuid in enumerate(result):
            scores[uuid] += 1 / (i + rank_const)
```

With `rank_const=1`, the rank-0 hit contributes exactly `1/1 = 1.0`. Two methods ⇒ maximum `2.0`,
which is why `GRAPHITI_RRF_DUAL_METHOD_MAX = 2.0` exists in the codebase.

So against a `0.85` threshold:

| Situation | Score | ≥ 0.85? |
|---|---|---|
| Top hit in **either** lane alone | 1.0 | **yes** |
| Rank 2 in **both** lanes | 0.5 + 0.5 = 1.0 | **yes** |
| Rank 2 in one lane only | 0.5 | no |
| Rank 3 in both | 0.33 + 0.33 = 0.67 | no |

The effective rule is **"is this the top hit for one of the two search methods, or top-2 in
both?"** — a rank test. Since the search is namespace-scoped with `num_results=5`, *something* is
almost always rank 1, so in a small namespace nearly every node acquires a conflict candidate
regardless of whether anything contradicts.

The documented intent does not survive contact with this scale. `lifecycle_consolidation.py:367-369`
describes "pairs with similarity 0.70–0.85 receive a RELATES_TO edge... Only pairs >= 0.85 are
flagged as conflicts" — a cosine band. On the RRF scale, `0.70 ≤ s < 0.85` is a narrow sliver
reachable only by odd rank combinations (`1/2 + 1/5 = 0.70`, `1/2 + 1/4 = 0.75`), so the
correlation band is effectively arbitrary too.

**The same score is normalized elsewhere.** `recall_pipeline.py:384` divides by the constant:

```python
(uuid, name, min(1.0, max(0.0, score / GRAPHITI_RRF_DUAL_METHOD_MAX)))
```

The recall path knows the scale; the correlation path does not. `correlation_service._route`
(`:382-397`) compares the raw value directly against `_merge_threshold`, `_conflict_threshold`,
and `_related_threshold` with no normalization.

## 2. Live case traced end to end

Group `f985d192`: **"Instrument Serif 400"** vs **"JetBrains Mono 400/700"**, surfaced as a
contradiction.

`get_provenance` on both (2026-08-09):

| | Instrument Serif 400 | JetBrains Mono 400/700 |
|---|---|---|
| uuid | `525f8a9c…` | `a3ac2e02…` |
| episode | `fe3f1f94…` | `fe3f1f94…` |
| anchors | `ShareButton.tsx`, `fonts-data.ts` | `ShareButton.tsx`, `fonts-data.ts` |

**Both were extracted from the same episode, with identical anchor paths.** One source asserted
both simultaneously — the yawn.frontend OG-image feature embeds two fonts. They are co-asserted
facts about one feature and cannot contradict each other.

They were flagged because their surface text is nearly identical apart from the font name
("yawn.frontend share/OG image feature uses *X* font embedded in the feature"), which makes each
the other's top BM25 hit — RRF 1.0, over the 0.85 bar. The mechanism produced exactly the outcome
the arithmetic predicts.

The other two false positives fit the same pattern: two `index.html` nodes (`669bcd9a`) are a pure
name collision across different projects, and Piece C / Piece D (`b6305d1a`) share near-identical
phrasing while the content itself says they are separate efforts.

## 3. Why the LLM review did not filter them

The conflict state machine is: similarity flags → `pending_llm_review` → LLM confirms →
`unresolved` (surfaced), or LLM clears → `false_positive` + `keep_both`.

But `lifecycle_consolidation.py:437-448` writes `unresolved` **directly**, bypassing review, when
either member has `PROMOTED` scope. This is deliberate and documented (SSOT-08): a claim
contradicting a PROMOTED node is not a symmetric disagreement for an LLM voter to adjudicate,
because the PROMOTED side is ground truth, so it goes straight to manual operator review.

So an `unresolved` group has two possible origins — LLM-confirmed, or review-bypassed — demanding
different operator responses. **I could not determine which produced these seven.** The listing
Cypher collects `scope: n.scope` (`consolidation_queries.py:606`) but `list_conflicts.py` never
surfaces it, and `get_provenance` does not return scope either. That observability gap is a finding
in its own right.

## 4. The conflict branch has no deterministic vetoes

`_route` sends `merge_proposed` through `_handle_merge_proposal`, which runs deterministic vetoes
first (including a promoted-node veto) and then an LLM judge, failing safe toward "conflict"
(`correlation_service.py:405-415`).

The `conflict` branch (`:393-394`) runs **none of that**. It returns `"conflict"` immediately and
the caller writes the queue entry. The destructive path was hardened; the queue-flooding path was
not. A shared-episode check — the signal that would have caught the fonts case instantly — exists
nowhere.

## 5. Relationship to the 2026-07 auto-merge incident

`.agent/memory-review-tracker.md` §4 and the lifecycle scale probe recorded ~2,679 bad auto-merges
caused by applying a cosine-calibrated `CORRELATION_MERGE_THRESHOLD` to graphiti RRF scores — the
same category error, on the `merge` branch of this same `_route` function.

That incident was mitigated by **disarming the consumer gates and adding the judge**, not by
correcting the scale. The underlying mismatch was left in place, and the `conflict` branch still
routes on it today. This RCA is the same defect surfacing on the branch that did not get a
mitigation.

## 6. Root cause

**A rank-fusion score is being used where a similarity score is required.** `search_scored` fuses
BM25 and cosine lanes with RRF, which by construction discards magnitude and returns ordinal
position; every routing threshold in `CorrelationService` is written and documented as a cosine
value.

Contributing factors:

1. **The variable is named `similarity`.** `classify_pair(source_uuid, target_uuid, similarity)`
   and `_route(similarity)` both name the parameter for the quantity they *expect*, not the one
   they *receive*, so every reader downstream is told it is a similarity.
2. **The correct normalization exists but only on one consumer.** `recall_pipeline.py` divides by
   `GRAPHITI_RRF_DUAL_METHOD_MAX`; correlation does not. Nothing makes the boundary explicit.
3. **No deterministic vetoes on the conflict branch** (§4).
4. **The PROMOTED bypass** delivers unreviewed pairs to the operator (§3), so the LLM gate that
   would have caught the remainder does not always run.
5. **Precision was never measured.** There is no metric for conflict-detection precision, so a
   flagging rule that fires on rank rather than similarity produced no alarm.

## 7. What is *not* the cause

- **Not the LLM contradiction check being wrong.** It may never have run on these groups (§3), and
  where it does run it correctly clears false positives to `keep_both`.
- **Not the threshold value being merely mistuned.** Moving 0.85 up or down cannot fix an ordinal
  signal; `post-v1-todo.md`'s "threshold tuning from operational data" item is necessary but
  addresses the wrong layer on its own.
- **Not conflict detection failing to run.** It runs; its precision is the problem.

## Remediation

See `.agent/plans/menhir-conflict-detection-signal-2026-08-09.md`.
