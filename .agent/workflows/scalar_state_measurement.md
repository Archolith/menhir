# Measuring the ScalarStateView Path (Ingest Coverage)

**Read this before writing a script to answer "did the scalar path work?" — the instruments already
exist and they were hard to find.** They live in `archolith-bench`, not menhir, which is why two
separate sessions have re-derived their results by hand: log-scraping, one-off cypher, ad-hoc probe
scripts. That work is wasted and it produces worse answers (a hand-rolled probe finds one thing and
stops; these enumerate).

They stay in the bench because they import its fixture definitions
(`archolith_bench.harness.menhir_scalar_state.SCALAR_FIXTURE_SETS`) and its prod-bolt guard
(`archolith_bench.harness.scalar_bolt.assert_not_prod`). Copying them into menhir would fork the
fixtures, so this file is the pointer instead.

Bench root: `projects/archolith/archolith-bench`.

## The four-stage model

`scripts/scalar_state_coverage.py` measures four stages **separately, never collapsed into one
pass/fail rate**, so a drop localizes to the stage it happens at:

| Stage | Question | A drop here means |
|-------|----------|-------------------|
| `assertion_emitted` | did the perceiver emit a TypedAssertion of the expected kind+value? | extraction/gate problem — prompt, k-sample consistency, threshold |
| `subject_bound` | did that assertion bind to an entity (`binding_pending = false`)? | identity problem — the subject resolved to nothing, or to the wrong node |
| `view_materialized` | does a current `scalar_state` View exist for that kind? | projection problem — assertion is bound but nothing was written |
| `fold_correct` | does the View's `ss_value` equal the expected value? | fold problem — wrong anchor, missed delta, stale contributors |

The gap **between** stages is the diagnosis. A run reporting only stages 1 and 3 (as the LME build
manifest's `typed_assertions` / `scalar_views` counts do) cannot tell you which of the four broke.

Positive denominator is 7, not 9: the fixture's two negative controls (possessed-object "my car is
red"; one-off event "I paid $250") are reported separately as controls clean/violated, never as
missing positives.

## Offline deterministic shadow (Bench-owned)

`archolith-bench/scripts/measure_deterministic_scalar_shadow.py` compares deterministic extraction
with the frozen LLM proposal/gate using only pure, offline logic. A typical invocation is:

```bash
MENHIR_COMMIT=$(git -C ../menhir rev-parse HEAD)
python scripts/measure_deterministic_scalar_shadow.py \
  path/to/frozen-capture.json \
  --json-out results/deterministic-scalar-shadow.json \
  --markdown-out results/deterministic-scalar-shadow.md \
  --menhir-root ../menhir \
  --expected-menhir-commit "$MENHIR_COMMIT"
```

Each capture must be emitted by Menhir `scripts/freeze_scalar_samples.py`. For each capture, the
instrument enforces `truncated_completions == 0`; every captured namespace has exactly `k` samples;
`llm_calls == k * captured namespace count`; and every proposal and episode span passes validation.
When multiple captures are combined, their `model`, `k`, `temp`, and `max_tokens` settings must
match. The report records each capture's SHA-256 and the selected Menhir checkout's commit and
dirty state; `--expected-menhir-commit` fails closed when the checkout has drifted from the
expected commit. The instrument imports the real Menhir proposal, gate, pure deterministic
extractor, and canonical comparator; it makes no new LLM, network, Neo4j, Docker, or service
calls. It reports exact/aligned one-to-one agreement, router misses, per-class attribution,
explicit denominators, and conservative namespace-batch projected scalar-call savings. Any
fallback episode keeps all `k` calls for that namespace; the instrument does not project savings
from a partially fallback namespace. Token/dollar savings are `null` because the frozen capture
schema has no measured token/cost fields.

### First held-out smoke capture

The first measurement uses the pre-registered, non-LME fixture
`archolith-bench/fixtures/deterministic_scalar_heldout_v1.json`. It has one fully-covered and one
fallback/adversarial namespace, exact stable UUID/content rows, and no categorical event-history
cases; the one-off payment is a negative control. The static source bypasses graph-shape ambiguity
and is validated before the LLM client is constructed. With `-k 3`, it makes exactly six capture
calls (`2 namespaces × 3 samples`); this is smoke evidence only, not a promotion or population
gate.

From the Menhir checkout, after confirming the output parent is the intended Bench `results`
directory:

```powershell
.\.venv\Scripts\python.exe scripts\freeze_scalar_samples.py `
  --episodes-json ..\archolith-bench\fixtures\deterministic_scalar_heldout_v1.json `
  --out ..\archolith-bench\results\deterministic-scalar-heldout-v1.json -k 3
```

Then, from the Bench checkout, run only the offline report over that capture:

```powershell
$menhirCommit = git -C ..\menhir rev-parse HEAD
.\.venv\Scripts\python.exe scripts\measure_deterministic_scalar_shadow.py `
  ..\archolith-bench\results\deterministic-scalar-heldout-v1.json `
  --json-out ..\archolith-bench\results\deterministic-scalar-heldout-v1-report.json `
  --markdown-out ..\archolith-bench\results\deterministic-scalar-heldout-v1-report.md `
  --menhir-root ..\menhir --expected-menhir-commit $menhirCommit
```

Do not substitute an LME fixture, enable Docker/Neo4j, or infer a promotion decision from this
six-call smoke. Preserve the capture and both reports as the first real frozen evidence bundle.

An optional label sidecar is valid only when bound to the exact capture SHA-256 set. Its labels are
known-negative targets, so `known_negative_target_hit_rate` is not a population false-positive or
false-current rate and is not the plan's precision/confidence-interval acceptance gate.

This is separate from the four-stage live graph path above: it does not measure binding,
projection, fold, recall, population gate evidence, or scalar-versus-recall spend attribution.
It changes no routing or promotion behavior, and no real frozen campaign measurement has run yet.

## Frozen methodology — do not violate

From the script's own header:

> every measured run MUST use a FRESH ISOLATED menhir+neo4j stack. Stacking multiple `--keep` rounds
> on ONE stack is INVALID for comparative yield — later rounds' episodes come back unenriched
> (`processing_state=None` as the enrichment worker degrades under pile-up), producing false
> low-yield outliers.

Bring up one throwaway per run, read its matrix before teardown, aggregate per-run namespaces in the
script. Mutating a graph and then re-measuring the same stack does not produce a comparable number.

## The instruments

| Script | What it does |
|--------|--------------|
| `scripts/scalar_state_coverage.py` | the four-stage coverage matrix above. Read-only over bolt; refuses a prod-looking URI |
| `scripts/run_scalar_state_e2e.sh` | end-to-end run against a THROWAWAY menhir with the typed-scalar scheduler ON; fresh ephemeral Neo4j, serve, harness, teardown. Reuses LongMemEval `config.sh` conventions |
| `scripts/inspect_scalar_state_graph.py` | read-only autopsy of one namespace: user `:Episodic` bodies (is the producer shape right?), resolved `:Entity` nodes (was there anything to bind to?), full `:TypedAssertion` rows with `binding_pending` and subject |
| `scripts/scalar_view_authority_live.py` | Step 7c live production-recall A/B for current-state View authority SUPPRESSION; drives the real recall endpoint behind `MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED` |
| `scripts/scalar_leads_authority_live.py` | live measurement of the ADDITIVE authority LEADS path (G14 + Phase 4b) |
| `scripts/scalar_phase_d.py` | Phase D counterfactual: seeds stale-predecessor + current-value episodes, scores baseline recall vs a View-aware answer composed OFFLINE in the harness |

Results land in `archolith-bench/results/menhir_scalar_state_e2e*.json|md`.

## When to reach for which

- **"Did my ingest/extraction change help?"** → `scalar_state_coverage.py` on a fresh stack. Compare
  per-stage, not the aggregate.
- **"Why did this one namespace produce nothing?"** → `inspect_scalar_state_graph.py`.
- **"Do Views change what recall returns?"** → `scalar_view_authority_live.py` (suppression) or
  `scalar_leads_authority_live.py` (leads). Both drive real recall; Phase D composes offline and
  answers a different question.

## Scalar history Views

`scalar_history` is a second projection kind for the same slot, preserving ordered delta evidence
without computing a current value. It activates in recall as an advisory lane (never authority) for
history-intent queries or when `scalar_state` abstains. The dashboard's scalar explorer shows both
projections side by side, including the postcard regression (state abstains, history shows the two
deltas in source-time order). Feature flag: `MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED`.

The existing instruments above measure `scalar_state` only. To measure `scalar_history` coverage,
use the dashboard explorer's history cards or the `archolith-bench/tests/test_scalar_viewer.py`
postcard regression fixture.

## Related but NOT built

`.agent/research/menhir-projection-coverage-audit.md` (Audit A assertion lifecycle / Audit B fold
parity) and `.agent/research/menhir-realization-coverage.md` are research proposals. The plan
`.agent/plans/menhir-projection-realization-coverage-implementation.md` is READY FOR IMPLEMENTATION
(2026-07-19) with no `feat` commit against it. Do not go looking for that code — it does not exist.
The scripts above are what is real today.
