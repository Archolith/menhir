# Plan: Law-3 bias coverage + cross-check independence experiment

**Status: PART 2 DONE 2026-07-04** (Part 1 remains — awaits live-LLM experiment; Part 3 — prod
wiring — sequenced after Part 1).
Design authority: `.agent/memory-aggregation-under-uncertainty.md` §4a/§4f/§9 (corroboration
independence — a corroborator catches only errors it is causally independent of) and the gap
analysis of 2026-07-03. Closes the two riskiest findings before prod wiring bakes the config in.

## The two problems

1. **Law-3 reconciles have zero bias-class corroboration.** For an anchor+delta candidate
   ("I have 3 tanks" + "got another" → 4): veto-3 is skipped by design (the reconcile IS the
   corroboration, `services/perception.py:632`), and veto-4's condition `stated is None`
   (`perception.py:653`) is false for every Law-3 candidate — so in the prod plan's locked config
   ("cross-check on") they commit on self-consistency + floor alone. The concrete hazard in the
   blind spot: a post-anchor **re-mention** of an anchored item ("my 5-gallon tank is dirty" →
   `kind=item` after the anchor) is indistinguishable from a delta, so `_reduce`
   (`perception.py:524-527`) computes 3+1=4 when the truth is 3. Even C4 as prompted cannot see it:
   `VERIFY_PROMPT` audits double-counting *among listed items*, not overlap with the anchor's
   unenumerated contents.
2. **Veto-4's independence is asserted, not measured.** The holistic cross-check
   (`extract_stated_total`, `perception.py:334-352`) is the same model reading the same episodes
   under a different prompt — independence is prompt-deep only. On the failure mode that matters
   most (4c re-narration), the correlation is plausibly strongest: a duplicate narration that fools
   the itemized path may fool the holistic read identically, in which case veto-4 stamps the wrong
   value `triangulated=True` — the exact §2 catastrophe. Evidence it works: one live-run anecdote
   (bike_spend 225 vs 185), from the eval corpus. The code's own comment (`perception.py:423`) calls
   C4 "meant to replace the noisy cross-check"; the prod plan nevertheless pins cross-check and
   never mentions verify.

## Part 1 — the correlated-error experiment (decides Part 3's config)

**Fixtures:** invented domains only (§8 firewall — zero LME vocabulary; reuse the
`test_perception_generalization.py` style: photographer, renter, gym). Three populations:
(a) deliberate re-narration duplicates — one purchase narrated in 2–3 episodes with differing
wording and differing inferred dates; (b) genuine recurrences (controls — same value, different
days, truly separate); (c) clean baselines. ~15–25 scenarios total.

**Procedure:** run against the real model via the bench harness's LLM seam (home: a marked live-LLM
module beside `archolith-bench/scripts/longmemeval/analysis/perception_write.py`, or a
`@pytest.mark.live` test skipped without an API key — either is fine, corpus knowledge stays in the
harness). For each scenario measure:
- **D** — itemized double-count rate (extractor folds the duplicate twice);
- **R** — holistic reproduction rate given D (the correlated-error rate: cross-check agrees with
  the inflated itemized total);
- **V** — C4 verify catch rate on the same inflated candidates (`verify_candidate`, k=3);
- holistic stability at k=1 vs k=3 (the plan's "holistic totals are stable" cost-guard claim).

**Decision rule (pre-registered so we don't fit the answer):**
- R > 10% of D-cases → the cross-check loses "independent corroborator" status: it stays as a veto
  (abstain-only, still catches what it catches) but C4 verify becomes the **primary** second
  opinion, pinned on in prod.
- R ≤ 10% and V ≥ R-complement → pin both (verify primary, cross-check supplementary).
- V materially below the cross-check's catch rate → keep cross-check primary, file the C4 prompt
  for rework instead. (Not expected, but the experiment must be allowed to say it.)

## Part 2 — Law-3 coverage (code, independent of Part 1's outcome)

1. **Widen veto-4 to Law-3 candidates.** Condition at `perception.py:653` becomes
   `(stated is None or is_law3) and reducer != "stated"` (`is_law3` is already in scope from
   `reduced[top_key]`). The holistic derivation reads the same episodes and naturally computes the
   reconciled current value ("I have 3, got another" → 4), so it is a genuine second opinion for
   anchor+delta; disagreement abstains, agreement corroborates. Law-3's upfront
   `triangulated=True` is then earned, not assumed.
2. **Law-3-aware verification.** `verify_candidate` (`perception.py:413-432`) gains an optional
   anchor: for a reconciled candidate, render the assertion event as a distinct
   `stated base: N on <date>` line (it is already `events[0]` — `[anchor, *post]` from `_reduce`)
   and extend the prompt with **(d): "could any listed post-anchor item already be included in the
   stated base?"** — the only guard that can see the re-mention hazard. Non-Law-3 candidates keep
   the current three-question prompt unchanged.
3. **Tests** (invented domains, fake judges): the re-mention scenario must abstain under the
   extended verifier (anchor "I own 4 lenses", later "my 50mm lens" re-mentioned → fake judge
   answers not-correct → abstain, never 5); a genuine delta still commits (anchor 4 + "bought a
   macro lens" → 5); veto-4-on-Law-3 abstains on holistic disagreement and commits on agreement;
   prompt-shape unit test for the `stated base` rendering.

## Part 3 — amend `../../archive/plans/perception-consolidation-prod-wiring.md`

Per Part 1's outcome, update the locked config in the prod plan **before** it is built:
- pin `enable_verify=True` (`verify_k` per the existing call-budget cap; ~3 extra calls per
  would-commit measure — abstained measures cost nothing);
- record cross-check's measured status (primary vs supplementary) with the experiment numbers;
- pin `enable_coref=True` (required: the dedup-signature plan's `unresolved_coreference` veto makes
  coref-off configurations abstain on every recurring-purchase measure);
- add the abstention-receipt persistence line item (design hook in the dedup/receipts plan).

## Explicitly NOT in scope (decided, not forgotten)
- Interval rung, ask-user channel, cross-aggregate invariants, ingest-time annotations — all gated
  on receipt telemetry or prod deployment (§9's receipts-decide rule).
- The prod wiring itself (scheduler task, dirty query) — stays in its own plan.
- Retuning `k`, `threshold`, or `triangulation_tol` — §8: no tuning to the measurement.

## Verification
1. Unit: Part 2 tests green; all existing perception/fold tests green and unmodified.
2. Experiment artifact: measured D/R/V rates + k-stability written to
   `.agent/reviews/perception-crosscheck-independence-results.md` with the pre-registered decision
   applied — the record of WHY the prod config says what it says.
3. One benchmark pass with the new config: no previously-correct commit regresses; any new
   abstentions are Law-3 candidates vetoed by the widened gates (each observable via the
   `veto` receipt field from the companion plan).
