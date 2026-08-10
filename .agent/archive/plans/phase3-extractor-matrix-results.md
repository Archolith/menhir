# Phase 3 extractor comparison matrix — results (2026-07-07)

Track-2 of the Phase 3 fix, run **after** measure-key canonicalization + keying were fixed (see
`phase3-canonicalization-guards.md`). Answers: with keying fixed, what is the **cheapest** extractor
setting that recovers known-good measures without admitting garbage?

## Method
- Harness: `build_phase3_report` (deterministic gate only — self-consistency + count-floor +
  stated-span guard; the LLM cross-check/coref/verify levers are NOT run, so recovery numbers are an
  **upper bound**; but those levers are abstain-only and precision-preserving, and a *correct* 125
  derivation is corroborated — not vetoed — by the holistic cross-check).
- Configs: {gpt-4.1-nano (current prod), gpt-4o-mini} x {k=3, k=5}. 3 trials each.
- Scenarios: `cycling_spend_derived` (3 bike purchases -> SUM=125, known-good derived),
  `watchlist_stated` ("I now have 25 movies on my watch list" -> stated=25, known-good stated),
  `iphone_bad` ("I bought an iPhone" -> must NOT commit).
- 429 hard-stop armed; none hit.

## Results

| model         | k | scenario              | recovered | bad-admit | lat/run | tokens |
|---------------|---|-----------------------|-----------|-----------|---------|--------|
| gpt-4.1-nano  | 3 | cycling_spend_derived | **0/3**   | 0/3       | 7.6s    | 8464   |
| gpt-4.1-nano  | 3 | watchlist_stated      | 2/3       | 0/3       | 2.3s    | 5853   |
| gpt-4.1-nano  | 3 | iphone_bad            | 0/3       | 0/3       | 2.1s    | 5710   |
| gpt-4.1-nano  | 5 | cycling_spend_derived | **0/3**   | 0/3       | 9.4s    | 14157  |
| gpt-4.1-nano  | 5 | watchlist_stated      | **0/3**   | 0/3       | 3.9s    | 9752   |
| gpt-4.1-nano  | 5 | iphone_bad            | 0/3       | 0/3       | 4.0s    | 9535   |
| gpt-4o-mini   | 3 | cycling_spend_derived | **3/3**   | 0/3       | 8.5s    | 7550   |
| gpt-4o-mini   | 3 | watchlist_stated      | **3/3**   | 0/3       | 4.1s    | 5787   |
| gpt-4o-mini   | 3 | iphone_bad            | 0/3       | 0/3       | 4.4s    | 5711   |
| gpt-4o-mini   | 5 | cycling_spend_derived | 3/3       | 0/3       | 17.2s   | 12529  |
| gpt-4o-mini   | 5 | watchlist_stated      | 3/3       | 0/3       | 7.2s    | 9651   |
| gpt-4o-mini   | 5 | iphone_bad            | 0/3       | 0/3       | 7.7s    | 9474   |

## Findings
1. **Winner: gpt-4o-mini @ k=3.** 3/3 on both known-good scenarios, 0 garbage, cheapest of the
   winning configs (7550 tok, ~8.5s). k=5 buys nothing on recovery and ~doubles latency (17s) —
   **k=3 is the sweet spot**; do not raise k.
2. **The current prod model (gpt-4.1-nano) cannot do the derived case at all** (0/3 both k). It
   consistently mis-keys bike spend as `grocery_spend=40` — wrong category, only one of three
   purchases — the exact wrong extraction the live cross-check vetoed. Canonicalization can't fix a
   *wrong value under a wrong name*; only a better extractor can.
3. **More k on a weak model HURTS.** nano watchlist recovery fell 2/3 -> 0/3 at k=5: extra samples =
   more naming variance under strict unanimity. Raising k is not a substitute for model quality.
4. **No config admitted the iPhone garbage** — the count-floor holds across the board.
5. With gpt-4o-mini, canonicalization is visibly working: accepted samples show BOTH `bike_spend=125`
   and `cycling_spend=125` committing (the model keys it stably; `cycling_*` variants collapse), and
   the stated case commits as `movies=25` grounded in its span.

## Recommendation
Point the personal-memory consolidation job at **gpt-4o-mini at k=3**, WITHOUT changing the global
`OPENAI_CHAT_MODEL` (that drives Graphiti enrichment too). Add a dedicated, optional
`MENHIR_PERSONAL_MEMORY_CHAT_MODEL` (default = current chat model) consumed by
`make_sync_chat` for this job only. Cost delta is modest (4o-mini ~ a few x nano per call, single
job, dirty-namespace filtered) and it is the difference between Phase 3 folding real derived Views
and folding nothing. `personal_memory_consolidation_k` already exists and should stay 3.

Still upstream and unsolved by this track: **no user-turn capture** (the `Conversation Turn Capture
Surface` ADR). A better extractor folds nothing if nothing user-authored is captured.
