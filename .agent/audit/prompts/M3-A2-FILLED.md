# Menhir M3 - Focused Security Audit

**Repository:** `ctharvey/menhir`
**Commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`
**Rule:** read-only on source. The only file you write is your report, plus the
probe you write to produce it. Push to branch `audit/m3-mcp-security-external`.

**Do not read other audit reports in this repository before completing this.**
There are ~20 under `.agent/`, several covering adjacent modules, and at least
one covering a module that shares this one's trust boundary. This pass is only
worth running if it is independent. Read them afterward if you like.

## Scope - 70 files, 7,222 lines under `src/menhir/mcp/`

| Group | Files | Lines |
|---|---:|---:|
| `mcp/*.py` (root) | 8 | 2,106 |
| `mcp/telemetry/` | 2 | 170 |
| `mcp/tools/*.py` | 2 | 29 |
| `mcp/tools/ingest/` | 11 | 803 |
| `mcp/tools/recall/` | 6 | 1,225 |
| `mcp/tools/ops/` | 35 | 2,413 |
| `mcp/tools/conflict/` | 6 | 476 |
| **Total** | **70** | **7,222** |

Enumerate the files yourself with `wc -l` and reconcile your coverage table
against those subtotals. Read every one. There is no depth-versus-breadth
tradeoff to make; the scope is already one module. Any file not read is marked
NOT READ - an unread file never inherits a "covered" row. A previous pass on a
70-file module stated 70 while its rows summed to 66.

## This is a security audit only

Menhir is a memory service exposing ~60 MCP tools over three auth tiers
(`readonly` / `agent` / `operator`), reachable on the LAN. This module is the
MCP surface: the dispatch layer, the tool implementations, the resource
handlers, and the telemetry wrapper around them.

The question is what an authenticated caller at one tier can cause that the tier
was not meant to permit, and what a caller can learn or reach across a boundary
the system claims to enforce.

**Do not spend depth on other audit types** - correctness, architecture and
maintainability are covered separately. Note anything you notice in passing as
one line under Open Questions and move on.

## Required analyses - quantitative, not impressionistic

1. **Tier coverage, enumerated.** Produce a table of every tool with its
   declared tier and the enforcement site (`file:line`) that applies it. Then
   state how the enforcement is reached: is it structurally unavoidable for
   every entry path into this module, or does it depend on each tool opting in?
   Count the tools, do not estimate. A tool that reaches state-changing work
   without passing an enforcement site is a finding regardless of its declared
   tier.
2. **Entry paths.** This module is not the only way in. Identify every way a
   request reaches these implementations - tool dispatch is one; find the
   others. For each, state which controls apply and which do not. Where two
   entry paths reach the same implementation under different controls, that
   asymmetry is the finding, and name both sites.
3. **Tenant / namespace isolation.** The system scopes data by namespace and
   claims some callers are pinned to one. Trace where a namespace value comes
   from on each entry path, where it is validated, and where it is applied. A
   value that is accepted from the caller and used without being reconciled
   against the caller's binding is a finding.
4. **Read-side exposure.** Separate from write authorization: what can a caller
   at the lowest tier read, and does any response path return data selected by a
   scope the caller supplied rather than one the server derived?
5. **Injection and untrusted content.** Where does caller-supplied text reach a
   prompt, a query, or a formatter, and what happens to it on the way?
6. **What gets logged.** Trace whether request payloads, identifiers, tokens, or
   memory content reach logs or telemetry unredacted.

For any hypothesis in this brief: **if it is not there, say so plainly with
evidence.** An evidenced negative is a valid finding here and has been accepted
before. Do not manufacture a finding to satisfy a question.

## Write your own probe, and control-test it

You cannot install dependencies or reach the network. Python stdlib `ast` is
enough for everything asked above and has produced the strongest evidence in
this programme.

Before you trust a single number your probe prints, run it against synthetic
cases covering its own blind spots - relative imports, `TYPE_CHECKING` blocks,
decorators, keyword-only arguments, dotted attribute access, annotated
assignments - and paste the self-test result. Two passes here did this and both
caught something real; one discarded its own code-search tool after it returned
nothing for a class visible in the file it had just read.

**If a search returns empty, verify it against a symbol you can SEE defined
before treating empty as absence.**

Commit the probe alongside the report.

## Rules

- **Never convert a static reading into an executed count.** If you cannot run
  something, write `NOT RUN` and the reason. One pass reported a precise count
  of blocking calls across five named files; the function it counted appears in
  one. The finding was real and the numbers were invented, and the whole report
  became unusable.
- **A comment is NOT evidence of the invariant it asserts.** This codebase has
  nine confirmed cases of comments describing controls the code does not
  implement, including a docstring claiming a function fails closed over one
  that fails open. Its comments are articulate and have misled prior reviewers.
  Verify against code, never prose.
- Cite exact `file:line` for every claim. A claim without a citation is not a
  finding.
- Report every issue including low severity. Do not pre-filter - a separate
  verification pass does the filtering. Omission is the failure mode here.
- If you investigate a candidate and disprove it, say so **with the evidence
  that disproved it**. Two disproofs in this programme were later shown wrong
  by execution.
- Anything believed but not traced goes under Open Questions, labelled.

## Output

One report: `.agent/reviews/menhir-M3-mcp-security-external.md`.

Write a draft as soon as you have first findings and refine in place - do not
batch all writing to the end.

Sections:

1. Executive Summary
2. Findings, severity-ordered, each with `file:line` and the concrete path a
   caller takes to reach it
3. Tier Coverage Table - every tool, declared tier, enforcement site
4. Bug-Class Sweep Results - your probe's output quoted verbatim, and its
   self-test result
5. Disproved Candidates, with the evidence that disproved them
6. Open Questions
7. Coverage Table - every file, reconciled against the subtotals above
8. What Was Checked, and what could not be verified in this environment
9. Review Confidence (/100)

Work autonomously to completion. Do not ask questions.
