# Conflict Detection Signal Correction

Status: **planned; implementation not started**
**Last verified:** 2026-08-18 — ACCURATE, not started. `classify_pair` (`services/correlation_service.py:146`) is the PRE-EXISTING method the RCA indicts, not this plan's output; it still takes a bare `similarity: float` and routes on it, and no Phase 0 precision measurement is recorded.


RCA: `.agent/reviews/rca-conflict-detection-rrf-scale-mismatch-2026-08-09.md`

## Why

`CorrelationService._route` compares a graphiti RRF rank-fusion score against thresholds written
and documented as cosine values. RRF is ordinal: it encodes rank position and discards similarity
magnitude, so `>= 0.85` currently means "top-1 in either search lane, or top-2 in both" rather
than anything about similarity. Roughly 3 of 7 live conflict groups are non-contradictions
produced by this rule.

This is the same defect class as the 2026-07 auto-merge incident (~2,679 bad merges), which was
mitigated downstream with a judge rather than corrected at the signal. The `conflict` branch never
received a mitigation.

## Design stance

1. **Fix the signal before the threshold.** A cardinal threshold needs a cardinal input. Tuning
   `0.85` against an ordinal score is unfalsifiable — any value chosen is a rank rule wearing a
   similarity name.
2. **Do not naively normalize.** Dividing by `GRAPHITI_RRF_DUAL_METHOD_MAX` makes the top hit
   `0.5`, below every current threshold, which would silently disable merge, conflict, *and*
   related routing. Swapping over-flagging for never-flagging is not a fix, and it would look like
   success (the queue empties).
3. **Cheap deterministic vetoes before expensive judgement.** The fonts case needed no model call
   to reject — one source asserted both facts.
4. **Measure before changing routing.** Precision is currently unmeasured, so any change is
   unverifiable. Shadow first.

## Scope

In scope:

- Give `CorrelationService` a similarity signal whose scale matches its thresholds.
- Add a shared-episode veto on the `conflict` branch.
- Surface member `scope` and review provenance in `list_conflicts`.
- A precision measurement harness for conflict detection.

Out of scope:

- Re-tuning `CORRELATION_MERGE_THRESHOLD` / re-arming the disarmed merge gates. Same root cause,
  but merge is destructive and carries the 2026-07 history; it needs its own plan and its own
  evidence. This plan must not change merge behaviour as a side effect (see Phase 2 risk).
- Repairing the 7 existing groups. Handle after Phase 1 lands, so the repair is judged by corrected
  detection.
- Anything in the companion suggestion plan (`menhir-conflict-suggestion-remediation-2026-08-09.md`).

## Phase 0 — measure current precision (do this first)

Without a baseline, no later phase is verifiable.

- Add a read-only script that walks current conflict groups and, for each, records: raw RRF score,
  both members' `scope`, whether they share an episode, whether they share anchor paths, and the
  LLM contradiction verdict when re-run in shadow.
- Output a precision figure and the distribution of raw scores that produced flags.

Acceptance: a committed baseline artifact stating conflict-detection precision with n, and the
observed RRF score distribution. Expect the scores to cluster at 1.0 and 0.5 — if they do not, the
RCA's arithmetic is incomplete and Phase 1 must be re-derived before proceeding.

## Phase 1 — supply a real similarity score

`graphiti_client` already has `search_ranked_by_method`, used in the `search_scored` fallback path,
which can return the `cosine_similarity` lane separately. Options:

- **A — take cosine directly (preferred).** Have the correlation path request the cosine lane and
  route on that value. Thresholds regain their documented meaning; `0.85` becomes a real cosine
  bar. Cost: loses BM25's lexical recall as a *candidate generator*, so genuinely contradictory
  pairs phrased differently may be found less often. Mitigate by keeping RRF for **candidate
  generation** and using cosine only for **routing** — rank to find, magnitude to decide.
- **B — RRF-native thresholds.** Keep the fused score and re-derive thresholds on the `[0, 2]`
  scale from Phase 0 data. Cheaper, but leaves an ordinal signal behind a cardinal-sounding name
  and cannot distinguish a 0.99 pair from a 0.30 pair at the same rank. Rejected unless A proves
  impractical.

Option A, stated concretely: candidates from `search_scored` as today; before routing, fetch the
pair's cosine similarity and pass *that* to `classify_pair`. Rename the parameter to make the
contract explicit and prevent the next reader inheriting the same confusion.

Acceptance:

- `classify_pair` receives a value on a documented `[0, 1]` cosine scale.
- The parameter and its docstring name the scale, not just "similarity".
- Phase 0 harness re-run shows improved precision, with the number recorded.
- Merge-branch behaviour is unchanged, or the change is explicitly measured and called out.

## Phase 2 — shared-episode veto on the conflict branch

The `conflict` branch runs no deterministic vetoes, while `merge_proposed` runs four. Add at
minimum:

**Two entities extracted from the same episode cannot contradict each other.** A single source
asserting both simultaneously is affirmative evidence of compatibility. This alone rejects the
fonts case with no model call, and is cheap — provenance is already indexed.

Candidate second veto, pending Phase 0 data: identical `name` with disjoint anchor paths is a name
collision across projects, not a contradiction (the `index.html` case). Only add it if Phase 0
shows the pattern recurs; do not speculate it in.

**Risk to check before implementing:** confirm that a genuine correction never arrives in the same
episode as the claim it corrects. If a single turn can say "X was 5, actually X is 7", the veto
would suppress a real supersession. Test this against the corpus in Phase 0 rather than assuming;
if it happens, scope the veto to pairs whose extracted facts are non-overlapping in attribute.

Acceptance: fonts group (`f985d192`) is vetoed pre-queue; a synthetic same-episode correction pair
is **not** vetoed.

## Phase 3 — surface provenance in `list_conflicts`

An `unresolved` group means either "LLM confirmed a contradiction" or "bypassed review because a
member is PROMOTED" (SSOT-08). Those need different operator responses and are currently
indistinguishable — `scope` is collected in the Cypher (`consolidation_queries.py:606`) and dropped
by the formatter.

- Surface each member's `scope`.
- Surface how the group reached `unresolved`: `llm_confirmed` vs `promoted_bypass`.

Acceptance: for every group, the operator can tell whether a model ever reviewed it.

## Risks

- **Phase 1 changes all three routes at once.** `_route` is a single cascade; changing its input
  changes `related`, `conflict`, and `merge` together. Merge is the one with a destructive history.
  Gate Phase 1 behind a shadow run comparing old and new routing decisions over the same candidate
  set before it takes effect, and confirm the merge gates are still disarmed.
- **Precision improvements can hide recall loss.** An empty conflict queue is indistinguishable
  from a working one without measuring missed contradictions. Phase 0 must record both directions,
  or at least state plainly that recall is unmeasured.
- **`conflict_status` feeds recall scoring** (`scoring_service.py:156`), so changing which pairs
  are flagged shifts ranking. Include a recall spot-check in the Phase 1 acceptance.
- **Blast radius on `search_scored`.** Confirm which other callers depend on its current fused
  score before altering it; prefer adding a separate cosine accessor over changing the existing
  method in place.

## Follow-ups (not this plan)

- Re-derive `CORRELATION_MERGE_THRESHOLD` on the corrected signal and decide whether the merge
  gates disarmed in 2026-07 can be re-armed. Needs the 2026-07 history and its own plan.
- Retire or re-document `SIMILARITY_CONFLICT_THRESHOLD`'s "0.70–0.85 RELATES_TO band", which
  describes a cosine band that never existed on the live scale.
- Conflict-detection precision as a standing metric rather than a one-off harness.
