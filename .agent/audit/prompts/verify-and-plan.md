# Template: Verify a Finding, Then Plan the Fix

For a finding that already exists and needs independent confirmation plus a
remediation plan. This is where severity gets corrected -- **in both
directions**. Two findings in this project were re-graded by such a pass: one
Critical dropped to High once its reachability was traced, and one Medium rose
to High once the two duplicate bodies were actually diffed.

Fill the `{{...}}` placeholders and paste everything below the line.

---

# Verify {{FINDING_ID}}, then plan the remediation

**Repository:** {{REPO_URL_OR_PATH}}
**Commit:** `{{FULL_SHA}}`
**Rule:** read-only on source. The only file you write is your report.

## Part 1 - confirm or refute

The claim:

> {{FINDING_TEXT}}

**Do not take this on trust.**

1. Read {{TARGET_FILES}} in full.
2. **Execute** the relevant code with the project venv at `{{VENV_PATH}}`. Paste
   the actual command and its actual output. Test at minimum:
   - every case named in the claim
   - **at least four adversarial cases you design that the reporter did not
     try** -- reproducing only the reporter's examples is confirmation bias, not
     confirmation
   - **at least three cases that SHOULD legitimately pass**, to establish the
     false-positive side. An over-tight reading of a defect is its own defect.
3. Determine the exact mechanism and quote the implementing line.
4. **Establish reachability.** Trace what the behavior actually controls
   downstream, with citations. If the result is discarded, or the guard is one
   of several, that materially changes severity and you must say so. A Critical
   here was correctly downgraded because the reporter confirmed the function was
   *called* but never checked what it *applied to*.
5. State a verdict: CONFIRMED / PARTIALLY CONFIRMED / REFUTED, with your own
   severity and reasoning. **If you think the reporter overstated it, say so
   plainly and show why.** A refutation backed by executed evidence is as
   valuable as a confirmation.

## Part 2 - remediation plan

Only if CONFIRMED or PARTIALLY CONFIRMED. A plan, not code.

- Read the module docstring for the stated design intent and preserve it. If the
  module claims fail-closed, the fix must actually fail closed.
- Evaluate **at least three** candidate approaches. For each: what it fixes,
  what it breaks, false-negative cost, complexity, and whether it needs a new
  dependency.
- **RECOMMEND ONE.** Decisive reason first.
- Ordered implementation steps naming exact functions and line ranges.
- **A test matrix you have EXECUTED, not authored.** Write the candidate
  predicate to a scratch file, import it, run every row, paste real pass/fail.
  A plan here presented an authored matrix that would have failed on its own
  first row; nobody noticed until it was run. Include the must-still-pass cases.
- Blast radius: what else calls this, which existing tests cover it (name the
  files), what could regress.
- Honest effort and risk.

## Rules

- Cite exact `file:line` for every claim. No claim without a citation.
- **A comment is NOT evidence of the behavior it describes.** Nine confirmed
  findings here are comments asserting controls the code does not implement.
- Execute rather than reason wherever it is cheap. Paste real output, never
  invented output.
- If your recommendation still fails any canonical case, **say so explicitly in
  the verdict** rather than presenting it as a fix.
- Anything you cannot verify goes under Open Questions.

## Output

One file: `{{OUTPUT_PATH}}`.

1. Verdict (CONFIRMED / PARTIALLY CONFIRMED / REFUTED) and your severity
2. Verification Evidence - every executed command and its real output
3. Mechanism - exactly how it works, implementing line quoted
4. Reachability and Downstream Effect, cited
5. Candidate Approaches (at least three) with tradeoffs
6. Recommendation, decisive reason first
7. Implementation Steps, ordered, exact functions and line ranges
8. Test Matrix - EXECUTED, with real output, including must-still-pass cases
9. Blast Radius and Existing Test Coverage
10. Effort and Risk
11. Open Questions

Work autonomously to completion. Do not ask questions. Do not modify source.
