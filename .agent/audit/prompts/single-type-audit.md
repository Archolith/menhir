# Template: Single-Type Audit (one type, maximum depth)

Fill the `{{...}}` placeholders from `MODULE-MAP.md`, run the probe, attach its
output, and paste everything below the line.

Use when a compound pass returned empty or thin on one type, or when a module is
high-stakes enough to want depth. Spot-checking a compound pass's empty lane is
cheap and has surfaced real findings here.

**Identical briefs matter.** When comparing two reviewers, give them the same
text. A comparison here was invalidated because one brief said "Read every one"
and the other only said "mark unread files NOT READ" - the second read 11 of 24
files, which is compliant behavior against a weaker instruction. That difference
drove the outcome and was mistaken for a capability gap.

---

# Menhir {{MODULE_ID}} - Focused {{AUDIT_TYPE}} Audit

**Repository:** {{REPO_URL_OR_PATH}}
**Commit:** `{{FULL_SHA}}`
**Rule:** read-only. The only file you write is your report.

**Do not read other audit reports for this module before completing this.** They
are in the same repository. This pass is only meaningful if it is independent.

## Scope - {{FILE_COUNT}} files, {{LINE_COUNT}} lines

{{FILE_TABLE}}

Read every one. There is no depth-versus-breadth tradeoff to make: the scope is
already narrowed to one module. Reconcile the measured line total at the end.
Any file not read must be marked NOT READ - an unread file must never inherit a
"covered" row.

## This is a {{AUDIT_TYPE}} audit only

{{TYPE_SCOPE_SENTENCE}}

**Do not spend depth on other audit types** - they are covered separately. If you
notice something in passing, note it in one line under Open Questions and move
on. The measurement here is how much {{AUDIT_TYPE}} signal exists when a full
pass looks for nothing else.

## Mechanical probe - run this first

```
python .agent/audit/menhir_audit_probe.py {{MODULE_PATH}} --type {{PLUGIN}}
```

Quote its output verbatim in your Bug-Class Sweep section. Where your narrative
and the probe disagree, **the probe wins**, and you report the disagreement.

Every report section must map to a probe check or be labelled
**NOT MECHANICALLY VERIFIED**. A pass here once declared "clean layering" in the
one section its probe never checked; there were 11 violations.

## Required analyses - quantitative, not impressionistic

{{TYPE_SPECIFIC_ANALYSES}}

For any hypothesis stated in this brief: **if it is not there, say so plainly
with evidence.** An evidenced negative result is a valid finding. A reviewer
suggestion of copy-paste duplication was correctly refuted here by a lane that
found only 17 clone groups across 10,988 lines. Do not manufacture findings to
satisfy a question.

## Rules

- **A comment is NOT evidence of the invariant it asserts.** Nine confirmed
  findings here are comments describing controls the code does not implement.
  This module's comments are articulate and have misled prior reviewers.
- Cite exact `file:line` for every claim.
- Report every issue including low severity. Do not pre-filter.
- If you investigate a candidate and disprove it, say so **with the evidence
  that disproved it**. Two disproofs here were later shown wrong by execution.
- Anything believed but not traced goes under Open Questions, labelled.

## Execute rather than reason

Where a reproduction is cheap, run it and paste real output. Project venv:
`{{VENV_PATH}}`.

## Output

One report: `{{OUTPUT_PATH}}`. Write a draft as soon as you have first findings
and refine in place.

Sections:

1. Executive Summary
2. {{TYPE_SPECIFIC_SECTIONS}}
3. Bug-Class Sweep Results - probe output quoted
4. Disproved Candidates, with the evidence that disproved them
5. Open Questions
6. Coverage Table - every file, reconciled against probe output
7. What Was Checked, and what could not be verified in this environment
8. Review Confidence (/100) using the rubric in `.agent/audit/README.md`

Work autonomously to completion. Do not ask questions.
