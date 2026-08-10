# archolith-bench operational model

## Status

canonical

## Purpose

`archolith-bench` is the operational research harness for Archolith/menhir experiments.

Unlike `menhir`, which holds system implementation, architecture notes, and research design, `archolith-bench` is where we run reproducible experiments, A/B comparisons, model/provider sweeps, and ephemeral compute jobs.

This document records the intended role of `archolith-bench` as an already-implemented benchmark target, currently suitable for A/B LongMemEval-style runs and for spin-up / tear-down execution.

## Repository split

```text
menhir:
  product/system implementation
  architecture docs
  research notes
  small code spikes
  memory behavior changes

archolith-bench:
  benchmark harnesses
  fixtures
  baselines
  A/B runners
  provider/model sweeps
  ephemeral run configs
  result artifacts
  research reports
```

Rule:

```text
menhir proposes and implements behavior.
archolith-bench proves or falsifies the behavior.
```

## Why bench matters

The research process only becomes credible when claims are backed by repeatable runs.

`archolith-bench` should be treated as the place where research claims become measurable:

```text
claim:
  BeliefCircuit reduces stale/superseded assertions.

bench task:
  Run graph recall vs graph+temporal vs graph+BeliefCircuit on the same fixtures.

output:
  metrics, run config, model/provider info, raw outputs, failure notes.
```

## Current useful role: A/B LongMemEval

`archolith-bench` is currently doing A/B LongMemEval-style work, which makes it a good target for validating menhir memory changes before they are treated as research results.

A/B comparisons should preserve:

```text
same dataset / fixture set
same prompts where possible
same scoring script
same model/provider unless that is the experimental variable
same run metadata
separate run IDs
raw outputs stored for audit
```

Example A/B shape:

```text
A: baseline graph recall
B: graph recall + temporal fields
C: graph recall + temporal fields + BeliefCircuit buckets
```

A run should not only report a score. It should also preserve enough evidence to explain why the score changed.

## Spin-up / tear-down model

`archolith-bench` is a good target for ephemeral compute because benchmark runs are naturally bounded jobs.

The desired lifecycle is:

```text
1. Resolve experiment config.
2. Spin up compute/provider/model resources.
3. Pull exact code/data refs.
4. Run benchmark.
5. Persist outputs and metrics.
6. Tear down compute.
7. Emit report stub / summary.
```

This matters because research exploration may require expensive models, local GPU nodes, rented GPU nodes, or provider-specific endpoints. The bench harness should let us run those jobs without leaving infrastructure running accidentally.

## Required run metadata

Every bench run should record:

```yaml
run_id:
started_at:
finished_at:
repo_refs:
  menhir_commit:
  archolith_bench_commit:
experiment:
  name:
  hypothesis:
  condition:
  baseline:
model:
  provider:
  model_name:
  model_version:
  quantization:
  endpoint:
runtime:
  machine_type:
  gpu_type:
  vram_gb:
  cpu:
  ram_gb:
  cold_start_seconds:
  run_seconds:
  teardown_confirmed: true
cost:
  estimated_compute_cost:
  estimated_api_cost:
  storage_cost:
data:
  dataset:
  fixture_version:
  sample_count:
outputs:
  metrics_path:
  raw_outputs_path:
  traces_path:
  report_path:
```

## Result artifacts

Each run should produce a durable folder:

```text
results/YYYY-MM-DD/<run_id>/
  config.yaml
  metrics.json
  outputs.jsonl
  traces.jsonl
  environment.json
  cost.json
  notes.md
```

For research reports:

```text
reports/<experiment-name>-<run-number>.md
```

Example:

```text
reports/belief-circuit-eval-001.md
reports/chronostratum-longmemeval-ab-001.md
reports/temporal-blast-radius-001.md
```

## Bench-to-research workflow

```text
1. menhir issue defines the research question.
2. menhir doc records source cards and design hypothesis.
3. menhir PR changes behavior or adds a spike.
4. archolith-bench creates fixtures/runners for that question.
5. bench runs A/B comparisons.
6. bench stores raw outputs and metrics.
7. bench emits report.
8. menhir docs only promote claims that survived the bench run.
```

## LongMemEval role

LongMemEval is useful as a broad memory sanity check, but it should not be the only proof for menhir's differentiators.

Use LongMemEval for:

```text
basic long-memory retrieval quality
regression checks
A/B sanity checks
provider/model comparisons
recall pipeline stability
```

Do not rely on LongMemEval alone for:

```text
structure-aware temporal memory
temporal blast radius
Git/structure time joins
belief drift correctness
superseded belief handling
agent debugging continuity
```

Those need custom Archolith fixtures.

## Custom fixture families needed

Beyond LongMemEval, bench should hold fixture families for menhir-specific claims:

```text
out_of_order_insertion:
  memories arrive out of chronological order but must be placed by valid time.

retroactive_correction:
  later evidence corrects or supersedes an earlier belief.

belief_drift:
  system must state what was believed then vs what is believed now.

temporal_blast_radius:
  failure occurs after a known-good point; system must intersect dependency cone with Git diff.

structure_aware_recall:
  query text alone is insufficient; file/symbol/test context must retrieve relevant memories.

conflict_suppression:
  stale or contradicted facts must go into do_not_assert rather than safe_to_assert.
```

## Provider/model sweep role

Because bench can spin resources up and down, it should be the place for model/provider experiments:

```text
local model vs hosted model
cheap model vs expensive model
small model extraction vs strong model adjudication
quantized model vs full precision
local GPU vs rented GPU
cold spin-up vs persistent endpoint
```

Each sweep should produce:

```text
quality metrics
latency metrics
cost metrics
failure rate
notes on operational pain
```

This lets research and cost engineering share the same artifact.

## Research claim promotion rule

A claim should not move from speculative to supported-by-eval until `archolith-bench` has a run artifact.

Claim states:

```text
speculative:
  source-backed idea but no bench run.

supported-by-spike:
  menhir code/test spike exists, but no bench run.

supported-by-eval:
  archolith-bench run exists with baseline comparison.

rejected:
  bench result failed or simpler baseline matched it.

superseded:
  later method or better eval replaced the claim.
```

## Immediate application: BeliefCircuit

BeliefCircuit should use archolith-bench for:

```text
A/B stale assertion rate
do_not_assert precision/recall
belief drift accuracy
evidence attribution accuracy
latency and cost
```

Initial comparison:

```text
A: graph recall only
B: graph recall + temporal metadata
C: graph recall + temporal metadata + BeliefCircuit recall packet
```

Expected report:

```text
reports/belief-circuit-eval-001.md
```

## Immediate application: Chronostratum

Chronostratum should use archolith-bench for:

```text
LongMemEval A/B
out-of-order insertion fixtures
retroactive correction fixtures
current-vs-historical belief queries
valid-time vs learned-time ordering
```

Expected report:

```text
reports/chronostratum-longmemeval-ab-001.md
```

## Immediate application: temporal blast radius

Temporal blast radius should use archolith-bench for:

```text
dependency cone only baseline
Git diff only baseline
dependency cone intersect Git diff
intersection plus memory/belief state
```

Expected report:

```text
reports/temporal-blast-radius-001.md
```

## Final rule

`archolith-bench` is where claims pay rent.

If a menhir research idea cannot be expressed as a bench fixture, baseline comparison, metric, or run artifact, it is still only design speculation.
