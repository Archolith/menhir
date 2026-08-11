# Temporal Event History View

> **ARCHIVED 2026-08-10.** This design was implemented by
> [`menhir-event-history-implementation-2026-08-07.md`](menhir-event-history-implementation-2026-08-07.md).
> Live Event → Fold → View behavior is owned by [`.agent/architecture.md`](../../architecture.md);
> the body below remains the design rationale.

Status: **IMPLEMENTED (2026-08-07) — this design was built.** Phases 1-5 are complete on
`main`, default-off production-capable. See `menhir-event-history-implementation-2026-08-07.md`
for the build record; this document remains the design rationale it implements against. The
"defer implementation" line below is stale — left in place as the historical starting point,
not current guidance.

Initial acceptance fixture: LongMemEval `lme-41698283`

## Why

Menhir can fold changing numbers, durations, and clock values through `TypedAssertion` into
`scalar_state` and `scalar_history` Views. It lacks an authoritative path for categorical questions
whose answer is the newest event in an immutable history.

`lme-41698283` exposes that boundary:

- 2023-03-11: the user recently acquired a `50mm prime lens`.
- 2023-08-30: the user referred to their new `70-200mm zoom lens`.
- The question asks which lens was purchased most recently.

This is not scalar state. Both lenses may still be owned, so the newer purchase must not supersede
the older one. The semantic path also lost acquisition meaning and source-time ordering: it
extracted ownership for the older lens but only “getting great shots” for the newer lens. Recall
therefore could not safely select the newest purchase.

## Scope

In scope:

- Admit grounded categorical events such as `PURCHASED`, `ACQUIRED`, `ATTENDED`, `STARTED`, and
  `COMPLETED` from `TurnEvidence`.
- Store immutable typed event assertions with source time and evidence provenance.
- Materialize an append-only `event_history` View for a subject/predicate/domain lane.
- Detect “latest/most recent/last” queries and select the newest grounded matching event.
- Return a structured latest-event verdict with the selected object, source time, and quote.
- Rebuild the projection after source deletion or policy exclusion.

Out of scope:

- Numeric, duration, clock, balance, and count updates; those remain scalar.
- Treating an event history as a current single-valued property.
- Inferring that an older purchased item is no longer owned.
- A general event ontology or unrestricted temporal-reasoning engine.
- Changing LongMemEval fixtures or gold answers.

## Proposed Design

### Evidence and assertions

Extend the existing admitted-evidence flow:

```text
TurnEvidence
  -> temporal-event perception/gate
  -> TypedEventAssertion
  -> EventHistoryView
  -> latest-event recall verdict
```

`TypedEventAssertion` is an immutable event claim with:

- idempotent assertion/source keys and namespace
- subject UUID/display
- canonical predicate
- object UUID when resolvable plus canonical object key/display
- optional domain/category such as `camera_lens`
- `valid_at` source/world time, separate from `recorded_at`
- original `stated_span` and founding `TurnEvidence` ID
- time basis, evidence tier, perceiver version, and binding state

An assertion records that an event occurred. It does not assert current ownership or supersede
another event. Its provenance must terminate in admitted evidence.

### `event_history` View

Add `view_kind="event_history"`, partitioned by:

```text
namespace + subject + predicate + optional domain
```

Its payload contains source-time-ordered entries with assertion ID, predicate, object, `valid_at`,
and source quote. Use explicit `EVENT_ENTRY` contributor edges. The View is append-only in meaning
and `lww_register=False`; it remains disposable and rebuildable from assertions. Ordering always
uses `valid_at`, never ingest/receive time.

### Perception gate

Start with a small predicate allowlist. Preserve acquisition language such as “recently got,”
“purchased,” and “bought.” Admit “my new X” only when surrounding context grounds acquisition.
Normalize predicate aliases while retaining the exact quote.

For the acceptance fixture:

- `recently got a new 50mm prime lens` -> acquisition event
- `my new 70-200mm zoom lens` -> acquisition event
- `thinking about getting a wide-angle lens` -> intent only; abstain

### Recall authority

Add a `LATEST_EVENT` query intent that resolves a bounded subject/predicate/domain filter. Select
the grounded event with the greatest `valid_at` and emit, for example:

```text
[AUTHORITATIVE LATEST EVENT]
latest camera-lens purchase: 70-200mm zoom lens
occurred at: 2023-08-30T04:01:00Z
provenance: "my new 70-200mm zoom lens"
```

Do not force this into `ScalarAuthorityVerdict`; its slot/value/unit contract is scalar-specific.
During the contract spike, choose between a separate `event_authority_layer` and a deliberately
generalized structured-verdict payload while preserving existing scalar serialization.

A verdict leads only with a valid foundation. Missing or tied source times that could change the
winner produce an advisory/ambiguous verdict. Future events cannot lead a present-time query.
Related semantic observations cannot outrank a founded latest-event verdict.

### Lifecycle

Use the scalar projection-repair pattern. Either generalize `ScalarProjectionRepair` with a
projection kind or add `EventProjectionRepair`: assertion removal/policy exclusion and a pending
repair receipt occur atomically; the receipt completes only after the affected history rebuilds.
All feature flags default off, and flag-off recall/wire output remains unchanged.

## Alternatives Considered

- **Add timestamps to semantic recall only:** useful, but insufficient for grounded predicate
  filtering, ambiguity handling, replay, and an auditable winner.
- **Store the latest lens as categorical scalar state:** rejected because purchases are
  multi-valued events; a newer lens does not invalidate an older one.
- **Query existing Graphiti relationships directly:** retains them as observations but does not
  repair lost event semantics or provide a stable authority contract.
- **Re-extract directly from TurnEvidence on every recall:** useful as a shadow oracle, but too
  expensive and not inspectable enough for the primary path.

## Risks

- An event ontology could expand without bounds; grow only from measured failures.
- “My new X” can indicate ownership without proving purchase; abstain when acquisition is unclear.
- Object aliasing could merge distinct products or fragment one product into multiple histories.
- Query intent could select the wrong domain when several latest-event histories match.
- Repeated mentions must deduplicate without erasing genuinely repeated events.
- Any use of `recorded_at` for historical ordering would invalidate replayed fixtures.

## Invariants

- Scalar extraction, folds, authority, and history remain unchanged.
- `lme-41698283` continues to produce zero scalar assertions and scalar Views.
- Event history never implies loss of ownership or supersedes earlier events.
- Namespace isolation is exact.
- Source/world time controls ordering.
- Every leading event verdict terminates in admitted evidence.
- Views are disposable and deterministically reconstructable.
- Ambiguous ordering never produces false authority.
- Flag-off APIs and recall remain backward compatible.

## Validation

Unit coverage:

- Predicate normalization and intent-vs-acquisition abstention.
- Out-of-order ingestion sorted by source time.
- Duplicate mentions, tied/missing times, and future events.
- Domain/namespace isolation and projection repair.

Integration acceptance:

1. Ingest isolated `lme-41698283`.
2. Assert grounded acquisitions for `50mm prime lens` at 2023-03-11 and
   `70-200mm zoom lens` at 2023-08-30.
3. Assert wide-angle-lens intent does not become a purchase.
4. Assert one `event_history` View contains both acquisitions in source-time order.
5. Ask the benchmark question and verify the latest-event verdict selects the `70-200mm` lens with
   its quote and source time.
6. Delete/exclude the newer evidence, rebuild, and verify `50mm` becomes latest.
7. Confirm the task still creates no scalar assertion or View.

Rollout:

- Shadow extraction first; measure yield, false positives, abstentions, and LLM cost.
- Review a stratified temporal-categorical failure set before enabling recall.
- Add explorer rendering for evidence -> event assertion -> View -> selected verdict.
- Compare recall-only accuracy on a fixed graph when typed events already exist; otherwise rebuild.

## Implementation Sequence

1. Inventory temporal-categorical failures and freeze `lme-41698283` as the first regression.
2. Map graph adapter, View repository, recall, HTTP/MCP, and context-builder blast radius; choose
   the authority payload contract.
3. Implement typed event admission, storage, projection rebuild, and repair receipts behind flags.
4. Implement latest-event intent, selection, provenance, and ambiguity handling.
5. Add explorer support and isolated/stratified benchmark measurement.
6. Update contracts and promote shadow -> flag -> measured candidate only after gates pass.

## Docs To Update

- `.agent/architecture.md`
- `.agent/data_models.md`
- `.agent/endpoints.md`
- `.agent/memory-governance.md`
- `.agent/memory-backlog.md` or active roadmap
- `.agent/CHANGELOG.md`
- `archolith-bench/.agent/architecture.md`
- Recall Lab and LongMemEval runbooks
