# Plan: certain-only dedup signature + structured veto receipts

> **ARCHIVED 2026-07-11 (ctharvey-approved).** Both parts verified live in the perception gate:
> `GateDecision.veto` + by-veto bucketing (`perception.py:202,1418`, `perception_report.py:97`),
> certain-only `_event_signature` (`fold_algebra.py:89,134`), tri-state `coref_memo`
> (`perception.py:553,1313`), `VETO_UNRESOLVED_COREFERENCE` (`:196`). The one owed item — the live
> `perception_write.py` benchmark receipt readout — is tracked in
> `deferred-verification.md` (Perception / write-side gate) so it survives archival.
> Archived per owner rule (a) fully implemented/shipped.

**Status: DONE 2026-07-03.** Part 2 (structured veto receipts) landed earlier in commit 9c6d2b0
(`GateDecision.veto`, per-site stamping, by-veto abstention bucketing). Part 1 (certain-only
signature + same-day candidate extension + tri-state coref memo + `unresolved_coreference` veto)
implemented this pass; `pytest tests/test_fold_algebra.py tests/test_perception.py
tests/test_perception_generalization.py` = 63 passed. Live benchmark receipt readout (verification
step 3) deferred to the next explicit `perception_write.py` run.
Two small, precision-first changes to the perception boundary, sequenced before
`perception-law3-bias-coverage-and-crosscheck-independence.md` (whose experiment measures the
itemized totals this plan changes). Design authority: `.agent/memory-aggregation-under-uncertainty.md`
§4d (the directional-merge caution) and §7/§9 (receipts drive channel decisions).

## Part 1 — narrow `_event_signature` to the certain case; make the ambiguity explicit

### The defect
`_event_signature` (`domain/fold_algebra.py:84-96`) keys the merge on `category or identity or what`.
The extractor tags category on EVERY purchase, so **all same-day, same-value, same-category purchases
silently collapse** — including two genuinely distinct items ($40 lights + $40 pump, both 'biking',
same day) — committing a wrong-low total with no judge involved. The docstring defends this as
"undercount is the safe error"; doc §4d now explicitly forbids it: §2 defines safety as *abstention*,
not a directional bet. `test_dedup_events_category_coreference_same_day`
(`tests/test_fold_algebra.py:118-124`) encodes the hazard as a feature, and
`test_dedup_events_keeps_genuinely_distinct_events` (`:158-164`) passes only because its fixtures
carry no category — add `category="biking"` to the lights+helmet pair and today's code merges them.

### The design (three moves, one principle: merge only what is certain; judge or abstain the rest)
1. **Signature narrows to the identical mention.** `_event_signature` keys on
   `identity or what` (never category): same kind + value + day + same normalized wording = the
   certain re-narration, still merged deterministically. Category leaves the signature entirely.
2. **The formerly-silent case becomes a judge candidate.** Extend `coreference_candidates`
   (`fold_algebra.py:124-152`) to also emit **same-day clusters** whose members share
   (category, value) but differ in `identity`/`what` — the exact population the old signature
   swallowed. (Same-day + identical wording never reaches here; the narrowed signature already
   collapsed it.) The existing cross-day rule is unchanged.
3. **Unresolved ambiguity vetoes.** New conjunctive gate veto `unresolved_coreference`: if
   `coreference_candidates` (with the same-day extension) is non-empty for a sum/count group and any
   cluster was not confidently resolved by the judge, the gate abstains that measure. This requires
   `resolve_coreference` (`services/perception.py:370-410`) to stop conflating "unsure" with
   "confidently separate": the memo value becomes tri-state — `merge` (votes/k ≥ threshold),
   `separate` (0 same-votes), `unsure` (anything between). `merge` and `separate` are resolutions;
   `unsure` and never-judged (coref disabled) veto. `perceive_and_fold` threads its existing memo
   (`perception.py:757`) into `gate` so the gate can see resolution state.

### Why the veto, not just the narrowing
Narrowing alone converts a silent wrong-low commit into a possible wrong-high commit whenever coref
is off (default) — trading one §2 violation for another. The veto restores the doc-pure outcome:
known ambiguity + no confident resolution = abstain. Side effects are the intended ones: it also
covers the cross-day case when coref is off (today that folds both mentions and hopes the
cross-check catches it), it pushes deployments toward enabling coref (correct per the doc), and the
tri-state memo is the exact signal the §9 interval rung will need later. Recall cost lands on
recurring purchases when coref is off; that cost becomes *observable* via Part 2's receipts instead
of invisible.

### Test impact (deliberate behavior change, not a refactor)
- `test_dedup_events_category_coreference_same_day` — **updated**: the same-day different-wording
  category pair no longer merges in `dedup_events`; assert instead that it surfaces as a same-day
  coreference candidate. Call out in the commit message that this test encoded the §4d hazard.
- `test_coreference_candidates_ignores_same_day_and_singletons` — **updated**: identical-wording
  same-day pairs remain non-candidates (dedup's job); different-wording same-day pairs are now
  candidates. Split the assertions accordingly.
- **New** (invented domains per §8 — no LME vocabulary): two distinct same-day same-value
  same-category purchases stay separate through dedup, become a candidate, and the measure ABSTAINS
  with `unresolved_coreference` when unjudged; commits when a fake judge confidently answers
  `separate`; merges (and commits the single value) when it confidently answers `merge`; abstains on
  a split judge. Categorized variant of `test_dedup_events_keeps_genuinely_distinct_events`.
- All other existing tests must pass unmodified.

## Part 2 — structured veto receipts on `GateDecision`

`GateDecision.reason` encodes the firing veto only as a freeform string prefix, and abstentions
reach only `logger.info` (+ one run-level `perception_abstained` tally). §9's "let the receipts
decide" needs abstentions bucketed by firing veto.

- Add `veto: str | None` to `GateDecision` (`perception.py:176-192`): one of `self_consistency`,
  `count_floor`, `triangulation_stated`, `cross_check`, `verification`, `unresolved_coreference`;
  `None` on commits. Set at each abstain site in `gate()`; `reason` stays the human string.
- Expose the field through `PerceptionResult` (already carries full decisions) so the bench harness
  can bucket abstentions per run with no further plumbing.
- **Persistence is explicitly out of scope** — it rides prod wiring. Record the design hook there:
  per-measure abstention receipts written with `name_embedding=None` (the `perception_abstained`
  pattern) so receipts never enter semantic recall.

## Explicitly NOT in scope (decided, not forgotten)
- Interval rung / provisional tier / evidence-set fallback — gated on receipt telemetry (§9).
- Receipt persistence + nightly bucketing — prod-wiring plan.
- Any change to embedding dedup (`_canonicalize_identities`) — DISTINCT's separate-bias is the
  correct direction there and untouched.

## Verification
1. `pytest tests/test_fold_algebra.py tests/test_perception.py tests/test_perception_generalization.py`
   — updated + new cases green; untouched tests unmodified and green.
2. Grep proof: no production-path caller can silently merge on category
   (`_event_signature` no longer reads `category`).
3. One benchmark pass (explicit `perception_write.py` run): committed values unchanged or abstained
   — **no measure may commit a different value than before**; diffs must all be commit→abstain, each
   carrying `veto="unresolved_coreference"`. Bucket counts by the new field as the first live
   receipt readout.
