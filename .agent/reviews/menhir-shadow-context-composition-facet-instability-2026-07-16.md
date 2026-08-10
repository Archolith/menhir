# Shadow Context Composition (Stage 1) — Facet-Label Instability Results

**Date:** 2026-07-16
**Scope:** `src/menhir/services/shadow_context_composition.py` (Stage 1 of
`.agent/plans/menhir-context-composition-production-integration.md`), and the LLM prompt in
`src/menhir/infrastructure/llm.py` that produces its `shadow_facet` / `shadow_state_family` labels.
**Commits:** `c7d39b0` (build), `25c2140` (review-pass fixes), `488bfcc` (smoke-test fixes), `ac96484`
(this result written up + broad smoke script committed).

## Summary

Stage 1's shadow-mode eligibility filter is safe to run broadly (zero crashes, zero added latency
to real ingest, confirmed on 10 real traces against `menhir-lme-neo4j`) but has **never once
selected a real candidate**. Across 9 post-bugfix real runs spanning 6 different LongMemEval
namespaces, every single one abstained. The cause is visible directly in the raw trace data: the
grounding LLM produces free-text `shadow_facet` labels independently for the message and for each
candidate, and those labels do not converge on shared vocabulary even within the same LLM call —
so the deterministic exact-string-match eligibility check can never agree with itself.

## Problem

Stage 1's design (per the plan) intentionally avoids a fixed hand-authored ontology — the Extraction
Lab's Phase 5 finding was that grounding classification in real candidate content beats an abstract
taxonomy. The system prompt tells the model this explicitly:

```python
# src/menhir/infrastructure/llm.py:58
_SHADOW_GROUNDED_SYSTEM_PROMPT = """You are a routing component for a conversational memory
system, running in SHADOW mode (observe-only; nothing you say here changes what gets stored).

You are given a CURRENT MESSAGE and a list of REAL CANDIDATE FACTS that already exist in the
memory graph for entities the message might be about -- each has a fact_uuid, the fact text
itself, and the two entity names it connects.

Two tasks:
1. message_hypotheses: up to 2 ranked guesses at what topic/state the CURRENT MESSAGE is
   about, each as a short free-text (shadow_facet, shadow_state_family) label pair with a
   confidence 0.0-1.0. Ground these labels in the vocabulary the CANDIDATE FACTS themselves
   suggest -- do not invent a rigid taxonomy. If nothing plausibly matches, return an empty list.
2. candidate_labels: for EVERY candidate fact given, a (shadow_facet, shadow_state_family,
   shadow_scope) label grounded ONLY in that candidate's own fact text and endpoints -- describe
   what real-world topic/state that specific fact is about, independent of whether it matches the
   message.

Return JSON only:
{"message_hypotheses": [{"shadow_facet": "...", "shadow_state_family": "...", "confidence": 0.0}],
 "candidate_labels": [{"fact_uuid": "...", "shadow_facet": "...", "shadow_state_family": "...",
                        "shadow_scope": "..."}]}"""
```

This is a single joint call — both `message_hypotheses` and `candidate_labels` come from one LLM
response, not two independent calls — grounded in the real candidate pool retrieved for that
episode:

```python
# src/menhir/infrastructure/llm.py:342
async def classify_shadow_context(
    self,
    episode_body: str,
    candidates: list[dict[str, str]],
) -> str | None:
    """... max_tokens scales with candidate count: the response schema requires one
    candidate_labels entry PER candidate (up to shadow_context_composition.py's
    _MAX_CANDIDATE_FACTS=30 cap), and a flat 800-token budget truncated the JSON
    mid-object on real graph data with >~10 candidates (confirmed via manual
    smoke test against menhir-lme-neo4j) -- surfacing as malformed_llm_response
    with a JSONDecodeError at the truncation point, not as an obviously-a-limit
    error.
    """
    return await self._chat_text(
        system_prompt=_SHADOW_GROUNDED_SYSTEM_PROMPT,
        user_prompt=_shadow_grounded_user_prompt(episode_body, candidates),
        operation="shadow_context_composition",
        max_tokens=max(800, 100 + len(candidates) * 80),
        temperature=0.0,
        max_retries=0,
    )
```

The eligibility filter that consumes these labels requires an **exact, case-insensitive string
match** on both `shadow_facet` and `shadow_state_family` between the message's top hypothesis and
each candidate's own label:

```python
# src/menhir/services/shadow_context_composition.py:606
def _label_matches_hypothesis(label: ShadowCandidateLabels, hyp: ShadowRankedHypothesis) -> bool:
    if label.shadow_facet is None or label.shadow_state_family is None:
        return False
    return (
        label.shadow_facet.strip().lower() == hyp.shadow_facet.strip().lower()
        and label.shadow_state_family.strip().lower() == hyp.shadow_state_family.strip().lower()
    )
```

```python
# src/menhir/services/shadow_context_composition.py:637 (inside _select_eligible_candidate)
for c in candidates:
    ...
    label = labels_by_uuid.get(c.fact_uuid)
    if label is None:
        rejected.append(ShadowRejection(c.fact_uuid, "no_matching_label"))
        continue
    if not _label_matches_hypothesis(label, top_hypothesis):
        rejected.append(ShadowRejection(c.fact_uuid, "shadow_label_mismatch"))
        continue
    survivors.append(c)
```

The combination — an open-vocabulary prompt that deliberately avoids a fixed taxonomy, feeding an
exact-string-match filter — is structurally unlikely to agree with itself, because nothing anchors
the message-side label and the candidate-side label to the same surface form even when they
describe the same real-world topic.

## Empirical result (real telemetry, `menhir-lme-neo4j`)

10 real shadow traces exist in the live telemetry DB (`.agent/mcp_telemetry.db`,
`lifecycle_events` where `phase='ingest_shadow'`), produced by
`scripts/smoke/shadow_context_composition_smoke.py` (single episode, run twice — once pre-fix,
once post-fix) and `shadow_context_composition_broad_smoke.py` (7 episodes across 6 real
namespaces). The first row is the pre-fix `malformed_llm_response` case (token-truncation bug,
already fixed in `488bfcc`); the 9 rows after it are all post-fix:

| Time (UTC) | Namespace | Status | Candidates | Rejection reasons | Message hypotheses |
|---|---|---|---|---|---|
| 15:48:21 | lme-830ce83f | abstained_no_eligible_candidates | 18 | shadow_label_mismatch: 18 | `Rachel`/`moving` (0.9); `suburbs`/`relocation` (0.7) |
| 15:49:37 | lme-830ce83f | abstained_no_eligible_candidates | 13 | shadow_label_mismatch: 13 | `Rachel`/`relocation` (0.9); `suburbs`/`residential_movement` (0.7) |
| 15:55:22 | lme-830ce83f | abstained_no_eligible_candidates | 13 | shadow_label_mismatch: 13 | `Rachel`/`relocation` (0.9); `suburbs`/`residential_movement` (0.7) |
| 15:55:33 | lme-a3838d2b | abstained_no_eligible_candidates | 7 | shadow_label_mismatch: 6, not_known_at_reference_time: 1 | `breast cancer research`/`donation` (0.9); `charity events`/`participation` (0.5) |
| 15:56:09 | lme-28dc39ac | abstained_no_eligible_candidates | 30 | shadow_label_mismatch: 27, not_known_at_reference_time: 3 | `The Witcher 3`/`gameplay` (0.9); `The Witcher 3`/`replay` (0.7) |
| 15:56:25 | lme-81507db6 | abstained_no_eligible_candidates | 10 | shadow_label_mismatch: 8, not_known_at_reference_time: 2 | `Hootsuite Certification`/`Social Media Marketing` (0.8) |
| 15:56:41 | lme-e3038f8c | abstained_no_eligible_candidates | 14 | not_known_at_reference_time: 10, shadow_label_mismatch: 4 | `professional book conservator`/`first edition` (0.9) |
| 15:56:48 | lme-b46e15ed | abstained_no_eligible_candidates | 1 | shadow_label_mismatch: 1 | `AI models`/`cancer treatment` (0.8); `success rates`/`cancer treatment` (0.7) |
| 15:57:07 | lme-830ce83f (control) | abstained_no_eligible_candidates | 18 | no_message_hypothesis: 18 | *(empty — correct: episode deliberately unrelated to namespace content)* |

**9/9 abstained. Zero selections. Zero LLM tie-breaker fires.** `shadow_label_mismatch` is the
dominant rejection reason in every case except one namespace where `not_known_at_reference_time`
(a real, separate, working signal — the bitemporal filter) also contributed. The one clean row
(15:57:07, the deliberately-unrelated control) correctly produced an *empty* `message_hypotheses`
list and abstained via `no_message_hypothesis` — that is the filter working as intended; every
other row is the filter failing to work as intended on genuinely relevant content.

**Concrete example of the divergence** (namespace `lme-830ce83f`, 15:48:21, 18 real candidates):

- Message hypothesis: `shadow_facet="Rachel"`, `shadow_state_family="moving"`
- Sample of the candidate-side labels returned in the *same LLM response*:
  - `{"shadow_facet": "user", "shadow_state_family": "trip planning", "shadow_scope": "Fort Myers Beach"}`
  - `{"shadow_facet": "user", "shadow_state_family": "visiting", "shadow_scope": "the city"}`
  - `{"shadow_facet": "Nisha's dad", "shadow_state_family": "family relationship", "shadow_scope": "Nisha"}`
  - `{"shadow_facet": "Chicago", "shadow_state_family": "coffee culture", "shadow_scope": "Belmont Arts Center"}`

Both sides of the comparison are free-text and both are plausible descriptions of their respective
inputs — but neither the model's own message-side vocabulary (`"Rachel"` / `"moving"`) nor its
candidate-side vocabulary (`"user"`, `"Chicago"`, `"Nisha's dad"`, …) land on a shared string, even
though the model produced both halves in one response and had the full candidate list in context
when generating the message hypothesis. There is no anchor forcing convergence.

One additional, lower-severity observation: the `lme-28dc39ac` run (30 candidates, the
`_MAX_CANDIDATE_FACTS` cap) took 29,750ms of its 30,000ms shadow-processing timeout budget —
worth watching if the candidate cap is ever raised.

## Root cause

The eligibility check (`_label_matches_hypothesis`) requires exact surface-form agreement between
two labels that were never designed to share a vocabulary — the prompt explicitly instructs the
model to avoid a rigid taxonomy (correct, per the lab's Phase 5 finding, for *classification
quality*), but pairs that with a comparison mechanism (exact string match) that assumes a shared
taxonomy exists. The two design choices are individually well-motivated and mutually
incompatible.

## Recommendation

Not yet implemented. Two directions, either compatible with keeping the open-vocabulary prompt:

1. **Canonicalize labels before comparison** — map both sides through a small normalization step
   (e.g. embed `shadow_facet`/`shadow_state_family` and compare by cosine similarity above a
   threshold, or a cheap second LLM call that judges "are these the same topic?") instead of
   `str.lower() == str.lower()`.
2. **Replace exact-match with embedding or LLM-judged similarity** directly in
   `_label_matches_hypothesis`, changing its return type from a hard boolean to a scored match with
   a tunable threshold — closer in spirit to the tie-break LLM call already used elsewhere in this
   module, but for eligibility instead of just the final tie-break.

Either change should be re-validated against the same broad-smoke harness
(`scripts/smoke/shadow_context_composition_broad_smoke.py`) before revisiting Stage 2 readiness —
Stage 2 (counterfactual extraction) has nothing to counterfactually test while the filter never
selects anything.

## References

- `.agent/plans/menhir-context-composition-production-integration.md` — "Stage 1 execution result"
  subsection (short-form version of this finding, written the same day)
- `.agent/plans/menhir-extraction-context-ablation-handoff.md` — Phase 5, the Recall-Labs-only
  investigation this stage carries forward (its winning `predict_candidate_aware_ranked` approach
  used hand-authored fixtures with a much smaller, more controlled candidate vocabulary — this
  instability did not surface there)
- `src/menhir/services/shadow_context_composition.py` — eligibility filter, status vocabulary
- `src/menhir/infrastructure/llm.py:58-124` — the grounding + tie-break prompts
- `.agent/mcp_telemetry.db` (`lifecycle_events`, `phase='ingest_shadow'`) — raw source data for the
  table above
