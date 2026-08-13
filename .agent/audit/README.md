# Menhir Audit Kit

Reusable audit prompts, the mechanical probe they all run, and the module
partition they address. Everything here is read-only tooling: audits never
modify source.

## Contents

| File | What it is |
|---|---|
| `menhir_audit_probe.py` | Canonical mechanical probe. Every lane runs this. |
| `test_menhir_audit_probe.py` | Ground-truth test for the probe itself. |
| `PROBE-PROTOCOL.md` | The rule binding report sections to probe checks. |
| `MODULE-MAP.md` | The 11-module partition, exhaustive and disjoint. |
| `AUDIT-FIT.md` | Which workspace audit types fit Menhir, and where they do not. |
| `prompts/compound-audit.md` | All eight audit types in one pass. |
| `prompts/single-type-audit.md` | One audit type, maximum depth. |
| `prompts/verify-and-plan.md` | Confirm or refute a reported finding, then plan the fix. |

## Which prompt to use

**`compound-audit.md`** — a module with little or no coverage. Eight audit types
in one run. Validated on M2: it independently rediscovered all three security
findings that had been deliberately withheld from the brief, with coverage
reconciled exactly. Breadth without measurable dilution.

**`single-type-audit.md`** — one audit type where a compound pass returned empty
or thin, or where a module is high-stakes enough to want depth. Spot-checking a
compound pass's empty lane is cheap and has surfaced real findings.

**`verify-and-plan.md`** — a finding already exists and needs independent
confirmation plus a remediation plan. This is where severity gets corrected in
both directions; two findings in this project were re-graded by such a pass.

## Non-negotiables, learned the hard way

These are in every prompt because omitting each one produced a real failure:

1. **Run the probe; quote its output verbatim.** Do not summarise its numbers.
   A lane whose narrative said 5,566 lines while its own probe printed 5,565 had
   "reconciled" against itself.

2. **Label anything the probe did not measure `NOT MECHANICALLY VERIFIED`.** A
   lane declared "clean layering" in the one section its probe never checked.
   There were 11 violations.

3. **A comment is not evidence of the invariant it asserts.** Nine confirmed
   findings in this codebase are comments describing controls the code does not
   implement, including a docstring claiming "fail-closed" over a function that
   fails open on contradiction.

4. **A disproof needs the same evidence as a finding.** Two "disproofs" were
   later shown wrong by execution. If you disprove a candidate, paste the run
   that disproved it.

5. **Read every file in scope.** Scope is already narrowed by module; there is
   no depth-versus-breadth tradeoff to make. An unread file is marked NOT READ
   and caps confidence. A lane once claimed 73 files while skipping a 2,579-line
   subdirectory that later yielded 31 findings.

6. **Execute rather than reason wherever it is cheap.** Every Critical found in
   this project came with an executed reproduction. Every incorrect claim came
   from reasoning about behavior instead of running it.

## Confidence rubric

Scores were meaningless until anchored. One pass self-reported 100/100 while its
report contradicted its own probe. Anchor to what was verifiable:

Start at 50.

| Adjustment | Condition |
|---|---|
| +15 | every file in scope read, count reconciled against probe output |
| +15 | every load-bearing finding backed by executed output |
| +10 | dynamic behavior observed (tests run, service exercised) |
| +5 | findings cross-checked against existing tests for coverage gaps |
| -20 | any scope unread (never score above 70 with an unread file) |
| -15 | no runtime verification possible in this environment |
| -10 | any number in the report not traceable to probe output |
| -10 | any load-bearing claim resting on a comment rather than code |

**Ceiling 90.** A static read of a live system cannot exceed it. Above 90 is
reserved for a pass that ran the service and reproduced findings against it.

## Running an audit

```
# 1. mechanical pass
python .agent/audit/menhir_audit_probe.py src/menhir/<module> --type a1

# 2. confirm the probe itself is sound (do this when it matters)
python .agent/audit/test_menhir_audit_probe.py

# 3. give a lane the prompt, with probe output attached
```

Reports land in the workspace `.agent/reviews/` as
`menhir-<module>-<audit-type>-results.md`.

## Verification pass

Lane output is candidate, not fact. Independently re-derive every load-bearing
finding against current source before it lands anywhere durable. In this
project's audit that pass corrected something in nearly every lane — including
four of the orchestrator's own errors, twice in the direction of *lowering* a
severity it had over-graded.

## After remediation

Re-run the probe and diff. A duplicate-definition group that disappears is a fix
landing; one that appears is a fix introducing another. Note that a clean re-run
proves absence of regression, not correctness of the fix: a substring-matching
fix in this project passed 10 of 10 test rows while still admitting
`"Alice claimed the deploy failed"` as a user assertion.
