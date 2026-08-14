# Menhir M1 (pass 1 of 2) - Focused Architecture Audit

**Repository:** `Archolith/menhir`
**Commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`
**Rule:** read-only on source. The only files you write are your report and the
probe that produced it. Push to branch `audit/m1-domain-architecture-external`.
If the push fails on DNS as it did last time, say so immediately and paste the
report body instead - do not spend turns retrying it.

**Do not read other audit reports in this repository before completing this.**
This pass is only worth running if it is independent.

## Scope - 26 files, 5,601 lines

`src/menhir/domain/` is 58 files / 12,107 lines, above the size where your
coverage has held. This is pass 1 of 2. Audit exactly these files and no others;
pass 2 covers the remainder.

`src/menhir/domain/truth/` - all 5 files, 571 lines.

`src/menhir/domain/` (root), these 21 files, 5,030 lines:

```
artifact_reconciliation.py  1880    merge_eligibility.py  145
work_artifact.py             477    artifacts.py          133
belief.py                    414    repo_snapshot.py       95
merge_snapshot.py            314    namespace.py           90
warden.py                    310    artifact_role.py       83
artifact_shape.py            218    belief_evidence.py     77
git_staleness.py             194    models.py              76
merge_delta.py               178    bootstrap_scope.py     58
legacy_snapshot.py           150    scope.py               46
                                    session.py             37
                                    edges.py               30
                                    ingest.py              25
```

Verify every count with `wc -l` yourself and reconcile to 5,601. Read every
file. Any file not read is marked NOT READ - an unread file never inherits a
"covered" row.

## This is an architecture audit only

M1 is the domain algebra: types, invariants, and pure operations. Everything
else in the codebase depends on it. A domain layer earns its name by depending
on nothing above it, so the central question is whether that holds and what it
costs where it does not.

**Do not spend depth on other audit types** - security, correctness and
maintainability are covered separately. Note anything you notice in passing as
one line under Open Questions and move on.

## Required analyses - quantitative, not impressionistic

1. **Dependency direction.** Enumerate every import in scope that points at
   `menhir.services`, `menhir.api`, `menhir.mcp`, `menhir.infrastructure`, or
   `menhir.cli`. Each one is an inverted edge; cite both ends. Then state
   whether the inversion is structural or incidental - a type-only import under
   `TYPE_CHECKING` is a different fact from a runtime call, and you should not
   report them as the same thing.
2. **Import cycles** within scope and between scope and the rest of the tree,
   found mechanically. Report each strongly-connected component with its members.
3. **Blast radius.** For each of the 26 files, how many modules across the whole
   repository import it. Rank them. The top of that list is what the codebase
   cannot change cheaply, and it is the finding whether or not anything is wrong
   with those files.
4. **Cohesion of `artifact_reconciliation.py`.** At 1,880 lines it is the largest
   file in scope by a factor of four. Enumerate the distinct responsibilities it
   holds and the public symbols belonging to each. State how many callers each
   group has. A split proposal is only credible if the groups have disjoint
   callers, so check that before proposing one.
5. **Where domain logic is not in the domain.** Identify operations over these
   types that live outside `domain/` - defaulting, validation, invariant checks,
   or state transitions performed by callers. For each, name the type, the
   invariant, and the external site enforcing it. Logic that has escaped the
   layer is worth more than logic that is merely arranged awkwardly inside it.

For any hypothesis in this brief: **if it is not there, say so plainly with
evidence.** An evidenced negative is a valid finding and has been accepted here
before. Do not manufacture a finding to satisfy a question.

## Write your own probe, and control-test it

You cannot install dependencies or reach the network. Python stdlib `ast` covers
1, 2, 3 and the symbol enumeration in 4.

Before you trust a single number it prints, run it against synthetic cases
covering its blind spots - relative imports, `TYPE_CHECKING` blocks,
function-local imports, aliased imports, `__all__` re-exports, conditional
imports in `try/except ImportError` - and paste the self-test result. **If a
search returns empty, verify it against a symbol you can SEE defined before
treating empty as absence.**

Commit the probe alongside the report.

## Citations must resolve

Your last pass on this codebase produced correct findings with line numbers that
did not resolve - offsets from -30 to +19, changing sign, so they had been
reconstructed rather than read. Every claim here needs a `file:line` that points
at the thing being claimed. Before you finalize, re-open a sample of your own
citations and confirm each lands on the cited symbol. Say in the report that you
did this, and what the sample was.

## Rules

- **Never convert a static reading into an executed count.** If you cannot run
  something, write `NOT RUN` and the reason.
- **A comment is NOT evidence of the invariant it asserts.** Ten confirmed
  findings in this codebase are comments describing behavior the code does not
  implement, two of them on the same docstring. Verify against code, never prose.
- Report every issue including low severity. Do not pre-filter - a separate
  verification pass does that. Omission is the failure mode here.
- If you investigate a candidate and disprove it, say so **with the evidence
  that disproved it**.
- Anything believed but not traced goes under Open Questions, labelled.

## Output

One report: `.agent/reviews/menhir-M1-domain-architecture-external.md`.

Write a draft as soon as you have first findings and refine it in place - do not
batch all writing to the end. A previous session in this programme lost a
finished analysis that had never been written down.

Sections:

1. Executive Summary
2. Findings, severity-ordered, each with `file:line`
3. Inverted Dependency Table - edge, both ends, structural or type-only
4. Cycles - one row per SCC with members
5. Blast Radius - all 26 files ranked by importer count
6. `artifact_reconciliation.py` Responsibility Map - group, symbols, callers
7. Bug-Class Sweep - probe output and self-test quoted verbatim
8. Disproved Candidates, with the evidence that disproved them
9. Open Questions
10. Coverage Table - every file, reconciled to a measured 5,601
11. Citation Self-Check - what you re-opened and whether it resolved
12. What Was Checked, and what could not be verified in this environment
13. Review Confidence (/100)

Work autonomously to completion. Do not ask questions.
