# Plan: reachability receipts + bundle honesty

**Status: PLANNED 2026-07-03 (not started).**
The now-worthy slice of the 2026-07-03 retrieval design review: three small, independent changes
that are safe in every configuration. Companion to
`retrieval-scale-contract-and-gap-remediation.md` (mechanical fixes) and prerequisite to
`retrieval-recency-split-and-view-injection.md` (whose decisions consume Part 1's data). Design
authority: `.agent/memory-retrieval-under-uncertainty.md` §6 (probe purity), §4e (context
collapse), §7 (reachability is the delivered metric).

## Part 1 — persist view-reachability outcomes (change 1)

The D0 probe (`recall_service.py:1226-1250`) computes where the first current View landed — rank
and token-depth — then discards it with the trace. The A6 decision is specified to be made from
exactly this data; today the data has nowhere to accumulate.

- **Sink: telemetry, not the graph.** Emit a `record_mcp_event(kind="background",
  operation="view_reachability", ...)` whenever the trace block runs, carrying query (truncated),
  namespace, view_kind, rank, tokens_to_view, result_count — and a `view_absent` event when no
  current View surfaced at all (the absent case is the one A6 cares about most). The read path
  must not write the graph (probe purity, §6); telemetry already exists on this path and is the
  right sink.
- **Bench-side aggregation.** A small analysis helper (bench harness, not src) that buckets
  reachability events by query lens: median first-View rank, share of aggregate-lens recalls where
  no View surfaced. This produces the two numbers the A6 decision rule needs.
- Trace-gated as today (`trace=True` recalls only) — zero cost on untraced production recalls;
  benchmark and probe runs generate the corpus.

## Part 2 — render the trust verdicts the pipeline already computes (change 2)

`warden_label`, `is_superseded_view`, and unresolved-conflict state are computed, attached to
`ScoredMemory`, and then dropped at packing — the default bundle renders flat
`[Memory i] name: content` (`context_builder.py:226-233`), so the belief machinery's conclusions
never reach the answering model unless the off-by-default brief builder is on.

- Minimal inline markers on the memory line, appended after the name:
  `(superseded view)` when `is_superseded_view`; `(flagged: <label>)` when `warden_label` is set;
  `(unresolved conflict)` when the conflict signal is 1.0 (`breakdown.conflict_bonus`).
- **No content expansion, no budget change** — markers are a few tokens and count against the
  same budget via the existing `_tokens_for`.
- Behavior-neutral in the default config by construction: warden labels require the warden gate
  (off), superseded views require `include_superseded` (off) — so default bundles are
  byte-identical today, and the markers activate exactly when the features that produce them do.

## Part 3 — abstention honesty in the bundle (addition 3)

`RecallResult.note` ("No memories matched with sufficient relevance." /
"Only pending (unprocessed) memories found...") is dropped by `build_context`, so an empty context
is indistinguishable from "memory was never consulted" — the answering model fabricates hardest
when given nothing and told nothing.

- When recall returns zero packable memories, the context becomes an explicit single line:
  `Memory: nothing relevant found for this query.` (plus the recall note when present).
- When a note exists alongside results (pending-only, frontier notes), append it as a final
  `Memory note: ...` line inside the budget.
- Read-side mirror of the write side's "abstention is observable, never a silent skip"
  (`GateDecision` always carries its reason; the bundle now does too).

## Explicitly NOT in scope (decided, not forgotten)
- Acting on reachability data (A6 View injection, lens router) — the companion plan, gated on
  Part 1's numbers.
- Rendering temporal_facts / currency chains in the default bundle — that is the brief builder's
  job; promoting it is a separate measured decision.
- Any scoring change (recency split lives in the companion plan behind an A/B).

## Verification
1. Unit: reachability event emitted with correct rank/tokens on a fixture recall (and
   `view_absent` when no View); default-config bundle byte-identical to today when no verdicts
   fire; markers render when warden label / superseded flags are present; empty recall produces
   the explicit no-results line; notes propagate.
2. One traced benchmark pass: reachability events visible in telemetry; bench aggregation
   produces the lens-bucketed table.
3. Full retrieval + context-builder test suites green; untouched tests unmodified.
