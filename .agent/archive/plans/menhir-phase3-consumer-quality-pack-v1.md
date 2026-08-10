# Plan: phase3-consumer-quality-pack-v1

> **ARCHIVED 2026-07-11 (ctharvey-approved).** All 5 items landed and verified in code:
> `count_spend_compound`/`count_vs_spend_partial` (`perception.py:412,1406`), `verify_retries`
> (default 0) + `verify_votes`/`verify_k` (`settings.py:211`, `perception.py:223,981`), correction
> `_PATTERNS` replacing/replaced-by (`correction_resolver.py:38,64`). Default behavior preserved.
> The owed live 2x characterization (items 1-2, needs :8099) is tracked in `deferred-verification.md`.
> Archived per owner rule (a).

**STATUS: EXECUTED 2026-07-08** on `feat/phase3-consumer-quality-pack-v1`. All 5 items landed per the
resolved decisions below. Verification: menhir new tests (26) + all existing consumer suites (101)
pass; archolith-bench 393 passed + offline phase3 smoke PASS (6 scenarios, gate PASS, invariants
`wrong_view_writes=0 silent_abstentions=0 duplicate_writes=0`). Items 1-2 stochastic effectiveness
NOT measured live (no :8099) — characterization pending a live 2x run.


Branch: `feat/phase3-consumer-quality-pack-v1` (menhir) + evidence updates in archolith-bench.

Consumer-side pack. Governing invariant (unchanged, load-bearing): **a wrong current-state View is
worse than an abstention.** Everything below either keeps precision identical or trades a miss for a
clearer receipt — never a guess.

## Grounding (current code, read 2026-07-08)

- `services/perception.py` — extractor (`extract_once`, `SYSTEM_PROMPT`), the conjunctive veto-gate
  (`gate`), reducers, family voting (`_collapse_measure_families`), and the veto labels
  (`VETO_*`). Committed groups fold via `event_fold.fold_events_to_counter`. Abstentions are recorded
  as `perception_abstained_<veto>` counter receipts (out of recall, `name_embedding=None`).
- Reducer is part of View identity and part of the family-voting signature, so a COUNT and a SUM of
  the same noun **already never merge** — the count-vs-spend "do not merge" property holds today.
- `services/correction_resolver.py` — deterministic `_PATTERNS` (regex old/new), unique value-match
  safety net. Adding phrasings here is pure/testable and cannot wrong-write (can only re-value an
  existing View that already holds `old`).
- `harness/phase3_scenarios.py` — `gate: bool`; `gate=True` scenarios drive the suite verdict,
  `gate=False` are characterization. count-vs-spend is currently `gate=False` with a `distinct_views`
  assertion `(bike,2.0)+(bike,125.0)`.

## The five items

### 1. count-vs-spend co-extraction  "I bought 2 bikes for $125 total."
Wanted: TWO distinct Views — COUNT(bikes)=2 and SUM(bike_spend)=125. Today the extractor usually emits
only the spend (or abstains); the COUNT rarely appears because it requires two `acquire` events for
one noun, which dedup/coref tend to collapse. This is a STOCHASTIC extractor-capability gap, and its
real-world rate can only be measured against a live throwaway Menhir (currently unavailable on :8099).
**Safety is already guaranteed** (no merge, gate abstains when unsure). Two candidate depths — see
DECISION 1.

### 2. fold-SUM stochasticity / retry / receipt clarity
`bike_spend` SUM under gpt-4o-mini is ~8/10 (fails closed on verifier/extraction variance). Deterministic
levers that DON'T weaken fail-closed:
- **Bounded retry:** when the gate abstains on a `verification`/`cross_check`/`self_consistency` veto
  for a SUM, re-sample the stochastic step up to N times and commit only if it then clears. The retry
  loop is deterministic (unit-testable with a mock LLM that fails k-1 times then succeeds); it only
  ever turns a stochastic miss into the SAME value the gate would have committed — never rescues a
  value the gate rejects on agreement.
- **Receipt clarity:** enrich the abstention receipt so a fail-closed is legible — record the firing
  veto + the disagreement (e.g. `value` vs `cross_total`, modal share) as structured audit fields on
  the receipt, not just a count. Deterministic.

### 3. correction phrasings (deterministic, low-risk)
Add 2-3 unambiguous connectives to `_PATTERNS` + tests. Candidates: "make it NEW, not OLD",
"scratch that, NEW not OLD", "correction: OLD -> NEW", "OLD -> NEW". Each requires a connective and is
protected by the unique-value-match net.

### 4. promote stable characterization -> gates
Only promote what is deterministically stable. `currency-worded-sum` and the core cases are already
gates. count-vs-spend can be promoted ONLY IF item 1 makes it reliably co-extract offline-deterministic;
otherwise it STAYS characterization (promoting an unreliable case would flake the gate). See DECISION 1.

### 5. archolith-bench evidence docs
Update `benchmarks/menhir-phase3-*.md` + results with the new receipts, retry behavior, and any gate
promotions.

## Decisions (resolved 2026-07-08)

- **DECISION 1 = SAFETY-ONLY + receipt.** Guarantee no wrong write; when only one side of a
  count/spend compound extracts, emit a structured `count_vs_spend` fail-closed receipt. Leave actual
  co-extraction to the stochastic extractor. **count-vs-spend STAYS characterization** (not promoted).
- **DECISION 2 = LAND ON OFFLINE GATE.** No :8099 this session. Build deterministic machinery + unit
  tests, pass menhir full suite + archolith-bench offline smoke, land. Mark items 1-2 stochastic
  effectiveness "characterization pending live 2x" in the evidence docs. Real :8090 untouched.

## Resolved implementation shape

- Item 1: a deterministic detector `count_spend_compound(text)` that flags a "N <plural-noun> for $M
  [total]" clause; used ONLY to emit a `count_vs_spend_partial` abstention receipt when the gate
  committed one of {count, spend} for that noun but not the other in the same batch. Never writes a
  View; purely observability so a fail-closed is legible. count-vs-spend gate stays characterization.
- Item 2 retry: opt-in `verify_retries` (default 0 = today's behavior). Each retry re-runs the FULL
  k-sample verifier vote and still requires the same unanimous bar, so per-commit precision is
  unchanged; retries only give a flaky-but-correct candidate more chances to prove unanimity. Small R.
- Item 2 receipt clarity: carry verifier vote detail (`verify_votes`/`verify_k`) + the disagreement
  (value vs cross_total, modal share) on `GateDecision`, thread into the abstention `reason`, and add
  structured per-veto receipts so a SUM fail-closed is legible in the decision trail + bench report.
- Item 3: add arrow (`OLD -> NEW`), reverse from/to (`to NEW from OLD`), and `NEW replacing OLD` to
  `_PATTERNS`; unique-value-match net keeps them wrong-write-safe. + tests incl. safety cases.
- Item 4: promote ONLY deterministically-stable characterization; count-vs-spend NOT promoted.
- Item 5: bench evidence docs note the new receipts/retry + the "pending live" status.

## Verification (consolidated gate at end)
menhir `pytest -q`; archolith-bench `pytest -q` + `harness menhir-phase3 --offline-fixture stub`; live
throwaway 2x IF :8099 available (real :8090 untouched). Invariants must stay
`wrong_view_writes=0, silent_abstentions=0, duplicate_writes=0`.
