# Template: Compound Audit (all eight types, one pass)

Fill the `{{...}}` placeholders from `MODULE-MAP.md`, run the probe, attach its
output, and paste everything below the line.

Validated on M2: independently rediscovered all three security findings that had
been deliberately withheld from the brief, with coverage reconciled exactly.

**Withhold known findings.** If findings already exist for this module, leave
them out so independent discovery is measurable. Seeding a brief with an answer
and then reporting the answer as independent proves nothing -- that mistake was
made once here and the result was worthless.

---

# Menhir {{MODULE_ID}} - Compound Correctness, Security, Architecture, Performance, Maintainability, Test-Coverage, LLM/AI and Compliance Audit

**Repository:** {{REPO_URL_OR_PATH}}
**Commit:** `{{FULL_SHA}}`
**Rule:** read-only. The only file you write is your report.

## Scope - {{FILE_COUNT}} files, {{LINE_COUNT}} lines

`{{MODULE_PATH}}`:

{{FILE_TABLE}}

Read every one. There is no depth-versus-breadth tradeoff to make: the scope is
already narrowed to one module. Reconcile the measured line total at the end.
Any file not read must be marked NOT READ - an unread file must never inherit a
"covered" row.

Supporting context (read, do not audit as scope): {{SUPPORTING_PATHS}}

## Stack and deployment

{{STACK_NOTES}}

## Mechanical probe - run this first

```
python .agent/audit/menhir_audit_probe.py {{MODULE_PATH}} --type {{A1_OR_A2}}
```

Paste its output verbatim into your Bug-Class Sweep section. Do not summarise
its numbers; quote them. Where your narrative and the probe disagree, **the
probe wins**, and you report the disagreement.

Every report section must map to a probe check or be labelled
**NOT MECHANICALLY VERIFIED**. That label is not a failure - many real findings
are judgement calls - it tells the verifier which claims to re-derive.

## Run all eight audits over this scope

**A1 Functional correctness.** Logic errors, inverted conditions, off-by-one,
type coercion, API contract violations (parameter order, arity, return-type
assumptions). Boundary cases: empty, None, zero, negative, oversized, malformed.
Concurrency: shared mutable state, TOCTOU, missing `await`, fire-and-forget
where the result matters, cancellation handling.

**A2 Security.** Authorization completeness - find anything reachable without
the privilege it should require, and trace the actual call chain rather than
trusting decorators. Credential handling, comparison timing, storage, expiry,
revocation. Injection reaching a query language, subprocess, or filesystem.
Trust derived from client-controllable input. Error and info disclosure.

**A3 Architecture.** Layering violations, blast radius of shared plumbing,
failure-mode analysis of the request path, observability gaps, unbounded
fan-out.

**A4 Maintainability.** God files - count DISTINCT responsibilities with line
ranges; a file is not a god file because it is long. DRY violations quantified
with line ranges on BOTH sides. Dead code with the proving search. Comment rot.
Cross-module private-symbol imports.

**A5 Performance.** Per-request work on hot paths, unbounded in-memory
structures, synchronous I/O inside `async def` without an executor, repeated
parsing or hashing, unbounded result sets.

**A6 Test coverage.** {{TEST_FILES}} Identify which properties are asserted
versus merely exercised, and which of your findings no existing test would
catch. A test that asserts a state transition but not the property around it is
a coverage gap - say so.

**A7 LLM/AI.** Does request-derived or user-derived text reach a model prompt
through this layer, and is any model output trusted for a control-flow or
authorization decision? If the answer is none, say so plainly rather than
manufacturing findings.

**Compliance.** Licensing headers, secret material in source or logs, PII
handling, and whether error responses leak information a public deployment
should not expose.

## Confirmed bug classes in this codebase - sweep for all six

The probe covers most of these mechanically. Confirm against its output and
extend by hand where it states a limit.

1. **Duplicate definitions** - the later silently overrides. Compare BODIES, not
   signatures: two confirmed instances had compatible signatures and dispatched
   to different implementations; one silently drops data.
2. **Names used only in `except` handlers, never bound** - an unbound `logger`
   produced `NameError` in 9 handlers, destroying the original exception.
3. **`except Exception` where `asyncio.CancelledError` escapes** and skips
   cleanup or state reset. It derives from `BaseException`.
4. **Lexicographic timestamp comparison** - Python `isoformat()` (`T`) versus
   SQLite `datetime('now')` (space) compared as TEXT; also mixed UTC offsets
   sorted as strings.
5. **Module constants documenting an invariant nothing reads.**
6. **Keyword-argument contract mismatch between a caller and the implementation
   it selects at runtime** - one confirmed case raised `TypeError` on every
   invocation, swallowed by a bare `except Exception` into a misleading message.

## Non-negotiable rules

- **A comment is NOT evidence of the invariant it asserts.** Nine confirmed
  findings here are comments describing controls the code does not implement,
  including a docstring claiming "fail-closed" over a function that fails open.
  This codebase's comments are articulate and have misled prior reviewers.
- Cite exact `file:line` for every claim. No claim without a citation.
- Report every issue including low severity. Do not pre-filter - a separate
  verification pass does the filtering. Omissions are the failure mode.
- If you investigate a candidate and disprove it, say so **with the evidence
  that disproved it**. A disproof needs the same rigor as a finding; two
  disproofs here were later shown wrong by execution.
- Anything believed but not traced goes under Open Questions, labelled.
- Severity by consequence: **Critical** = data loss/corruption, auth bypass,
  crash on valid input, silent wrong result in a security-critical path.
  **High** = wrong results for common inputs, silent failure swallowing real
  errors, deadlock off the exceptional path. **Medium** = wrong results in rare
  edge cases, resource leak. **Low** = cosmetic, recoverable on pathological
  input.

## Execute rather than reason

Where a reproduction is cheap, run it and paste real output. Every Critical
found in this project came with an executed reproduction; every incorrect claim
came from reasoning about behavior instead of running it. Project venv:
`{{VENV_PATH}}`.

## Output

One report: `{{OUTPUT_PATH}}`. Write a draft as soon as you have first findings
and refine it in place - do not batch all writing to the end.

Sections:

1. Executive Summary, highest-risk result stated first
2. Findings by audit type (A1-A7 + Compliance): severity, `file:line`,
   reproduction or code-path trace, impact, fix
3. {{DOMAIN_SPECIFIC_MATRIX}}
4. Bug-Class Sweep Results - probe output quoted, plus hand-extension
5. Test Coverage Gap Analysis - which findings no existing test would catch
6. Disproved Candidates, with the evidence that disproved them
7. Open Questions - suspected but unproven, and what would settle each
8. Coverage Table - every file, with line reconciliation against probe output
9. What Was Checked, and what could not be verified in this environment
10. Review Confidence (/100) using the rubric in `.agent/audit/README.md`

Work autonomously to completion. Do not ask questions.
