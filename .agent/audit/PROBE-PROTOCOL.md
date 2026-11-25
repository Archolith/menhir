# Audit Probe Protocol

Every audit lane runs the same mechanical probe. Lanes interpret; they do not
re-invent the measurements.

## Why

Across this workspace's Menhir audit, four separate failures traced to lanes
asserting things they had not measured:

| Failure | What happened |
|---|---|
| Claimed coverage | A lane reported "all 73 files" while silently skipping a 2,579-line subdirectory. |
| Report drift | A lane's narrative said 5,566 lines; its own probe printed 5,565. |
| Unmeasured section | A lane declared "clean layering" in the one section its probe never checked. There were 11 violations. |
| False disproof | A lane declared a real defect "DISPROVEN" from reasoning; execution showed it was real. |

Every one is the same shape: a claim with no instrument behind it. A shared
probe removes the option.

## The rule

**Every report section maps to a probe check. Any claim not traceable to probe
output must be labelled `NOT MECHANICALLY VERIFIED`.**

That label is not a failure — plenty of real findings are judgement calls. It is
a signal to the verifier about which claims to re-derive.

Where probe output and the report disagree, **the probe wins**, and the
disagreement is itself reported.

## Usage

Install `projects/ctharvey/cth.auditprobe` once (`pip install -e ../../ctharvey/cth.auditprobe`;
see `PROBE.md`), then run the `auditprobe` entry point from the Menhir checkout:

```
auditprobe <module_dir> [--type correctness|security|architecture]
```

Paste the output verbatim into the report's Bug-Class Sweep section. Do not
summarise the numbers; quote them.

Core checks, run for every module and audit type:

| # | Check | Answers |
|---|---|---|
| 1 | Line reconciliation | How many files and lines are actually in scope |
| 2 | Duplicate definitions | Same name defined twice, with body-digest comparison |
| 3 | Unread module constants | Constants nothing in the repo reads |
| 4 | Cross-module private imports | `_name` imported across a package boundary |
| 5 | Layering edges | Which layers import which, with per-edge counts |
| 6 | Unreferenced top-level symbols | Candidates for dead code |
| 7 | Control-asserting comments | Comments claiming a guarantee, for verification |

Plugins:

- `--type correctness` — `CancelledError` escaping `except Exception`; mixed timestamp
  formats compared as TEXT; intra-module keyword-argument mismatches
- `--type security` — route/tier coverage; request data reaching logs unredacted;
  synchronous I/O on an async path, direct and one hop
- `--type architecture` — import cycles repo-wide; blast radius by import in-degree;
  responsibility proxy (lines, publics, classes, decorated) per file

## Reading the output honestly

The probe reports *shape*, not *verdict*. Three checks in particular produce
candidates rather than findings, and the output says so inline:

- **Layering edges** — an edge is not automatically a violation. Judge direction
  against the intended architecture.
- **Unreferenced symbols** — decorator-dispatched handlers (FastAPI routes, MCP
  tools) always appear here and are not dead. Confirm the dispatch mechanism.
- **Control-asserting comments** — each is a claim to verify. This codebase has
  nine confirmed cases where such a comment described a control the code lacks.

## Known limits

State these in the report rather than working around them silently.

- Hop detection for blocking I/O is intra-module and name-based. A helper
  imported from another package is not resolved.
- Keyword-mismatch detection is intra-module. A confirmed defect of that class
  lives across files and would not be caught.
- Nothing here judges intent, counts responsibilities, or evaluates test
  quality. Those stay human judgement; the probe narrows what needs it.

## Trusting the probe

The probe's synthetic fixture test suite lives in `projects/ctharvey/cth.auditprobe`
(see `PROBE.md`). Run its pytest suite from `cth.auditprobe` before trusting a report
after probe changes; a failure needs a human decision rather than blindly updating
expectations.

## After remediation

The probe doubles as a regression harness. Re-run it against a fixed tree and
diff: a duplicate-definition group that disappears is a fix landing, one that
appears is a fix introducing another.
