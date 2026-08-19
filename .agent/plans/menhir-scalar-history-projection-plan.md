# Menhir Scalar History Projection

## Compact Summary

Add a replayable, slot-keyed scalar-history View that preserves delta evidence and provenance without inventing an absolute current value.

**Date:** 2026-07-29
**Status:** READY FOR IMPLEMENTATION
**Last verified:** 2026-08-18 — ACCURATE, unbuilt. `ScalarHistoryRepository` 0 hits; `MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED` appears in settings only.
**Owners:** Menhir scalar pipeline and archolith-bench
**Related plan:** `.agent/plans/menhir-scalar-state-view-implementation-plan.md`

## Decision

Implement a second typed-scalar projection with `view_kind="scalar_history"`.

The existing `scalar_state` View remains the authoritative current-state projection and continues
to abstain when a slot has no absolute anchor. The new `scalar_history` View is an advisory,
chronological projection of the current materializable assertions for one canonical scalar slot.
It makes delta-only histories usable by recall and inspectable in the benchmark dashboard without
claiming that an unanchored delta is an absolute total.

This builds on the typed scalar work already in Menhir. It does not replace the extractor, the
2-of-3 gate, `TypedAssertion`, TurnEvidence, entity binding, the scalar-state fold, or the event log.

## Why

The postcard LongMemEval item exposes a real projection gap:

- namespace `lme-01493427` contains two correctly classified delta assertions;
- the values are `17` at source/world time `2023-08-11` and `25` at `2023-11-30`;
- each value passed the configured 2-of-3 agreement gate;
- both assertions are grounded in source turns;
- `scalar_state` correctly abstains with `no_anchor`;
- no other production projection exposes the ordered typed history to recall.

The expected answer is the latest cumulative delta, 25, because both statements compare against
the same user-described baseline (“since I started collecting again”). It is not an absolute total,
and the two values must not be added to produce 42.

The extractor is doing the right thing. The missing behavior is between durable typed assertions
and recall: Menhir has a current-state fold, but the planned advisory history/candidate surface was
never wired.

## Existing Architecture to Extend

Keep these components as the system of record:

1. Extraction and scalar voting produce typed candidates.
2. The 2-of-3 gate admits a `TypedAssertion`.
3. `TurnEvidence` grounds the assertion through `(:TurnEvidence)-[:FOUNDS]->(:TypedAssertion)`.
4. `(:Episodic)-[:ADMITTED_ON]->(:TurnEvidence)` joins the evidence atom to the source episode.
5. Entity binding assigns the canonical subject and namespace.
6. `ScalarStateService` groups materializable assertions by canonical slot and folds current state.
7. `scalar_state` Views carry explicit contributor edges and may enter the authority lane only
   when the existing foundation rules permit it.
8. Replay rebuilds projections from assertions without invoking an LLM.

The old `fold_events_to_timeline` function is useful prior art but is not the implementation target
unchanged. It has no production caller, is subject-wide instead of slot-keyed, and its payload does
not reliably retain typed numeric values. A history projection should reuse its ordering and
advisory intent, not its current data shape.

## Scope

### In scope

- A new fact ViewKind named `scalar_history`.
- One current history View per canonical scalar slot:
  `(namespace, subject_uuid, attribute, scope, value_kind, unit)`.
- Ordered, lossless-enough history entries derived from current materializable assertions.
- Delta-only slots, including the postcard regression.
- Explicit View-to-assertion provenance edges.
- Source/world time throughout the history payload and UI.
- Supersession, correction, expiry, deletion, merge, unmerge, replay, and namespace isolation.
- A dedicated recall lane that labels history as advisory and never treats it as current-state
  authority.
- Dashboard support showing the history and its complete evidence chain.
- Focused and full LongMemEval validation with immutable run provenance.

### Out of scope

- Reclassifying the postcard assertions as absolute.
- Summing unanchored deltas.
- Converting a delta-only history into an asserted absolute current total.
- Replacing `scalar_state`.
- Enabling the legacy counter pipeline as a shortcut.
- A generic temporal query language.
- A baseline LongMemEval arm. The approved experiment is candidate-only at 2-of-3 agreement.
- Migration of production Neo4j during development or benchmark validation.

## Invariants

1. **Assertions are the durable source of truth.** Views are disposable and replayable.
2. **No anchor means no absolute state.** `scalar_state` must continue to return `no_anchor`.
3. **History is advisory.** A `scalar_history` View cannot satisfy the scalar authority foundation
   gate, suppress raw memories as an authoritative head, or masquerade as an absolute value.
4. **Never add deltas merely because they share a slot.** The history projection orders and exposes
   them; it does not infer interval versus cumulative semantics.
5. **Use source/world time.** `valid_at` drives ordering and display. `learned_at`, `created_at`, and
   benchmark ingest time are audit metadata, never substitutes for the original LME time.
6. **Every displayed entry must be reconstructible.** The View must identify the exact assertion,
   TurnEvidence, source episode, operation, value, stated span, and source time.
7. **Namespace is part of every identity and query.** No edge, View, replay, or recall query may
   cross namespaces.
8. **One current View per slot.** Rebuild is idempotent and atomically replaces contributor edges.
9. **Lifecycle changes are reversible through replay.** Corrections, merge/unmerge, and deletion
   cannot leave a stale current history.
10. **Receipts do not become ranking signals.** Audit data explains the projection but does not
    increase its authority.

## Proposed Data Model

### View identity

Register `ScalarHistoryKind` in `infrastructure/view_models.py` and the ViewKind registry.

The deterministic key is based on:

```text
view_kind=scalar_history
namespace
subject_uuid
attribute
scope
value_kind
unit
```

The View is a fact-class `:Entity` so existing View supersession and `MENTIONS` provenance can be
reused. It must not receive scalar-state authority merely because it is fact-class.

### View payload

Store bounded recall content and a deterministic audit summary on the current View:

```json
{
  "view_kind": "scalar_history",
  "subject_uuid": "...",
  "attribute": "postcard_count",
  "scope": "collection",
  "value_kind": "integer",
  "unit": "postcards",
  "history_entry_count": 2,
  "history_signature": "...",
  "operation_counts": {"delta": 2},
  "first_valid_at": "2023-08-11T00:00:00Z",
  "last_valid_at": "2023-11-30T00:00:00Z",
  "entries": [
    {
      "assertion_id": "...",
      "operation": "delta",
      "value": "17",
      "value_json": 17,
      "valid_at": "2023-08-11T00:00:00Z",
      "episode_uuid": "...",
      "turn_id": "...",
      "evidence_tier": "...",
      "stated_span": "..."
    }
  ]
}
```

The exact persisted property encoding must follow Neo4j property constraints. A JSON property is
acceptable for the bounded display payload, but identity, counts, signature, and time bounds should
remain first-class properties for inspection.

The full history remains in `TypedAssertion` nodes and provenance edges. The View payload should
keep the latest 8–16 entries for recall, with the limit configured and tested. A repository/API
method should paginate the complete contributor list when the dashboard or an operator asks for it.

### Ordering and signature

- Sort entries by `(valid_at, assertion_id)`.
- Treat a missing or invalid `valid_at` as fail-closed for canonical history materialization; emit
  an abstention/repair receipt rather than silently substituting ingest time.
- Compute `history_signature` from the ordered assertion identity and semantic fields:
  assertion ID, operation, normalized value, slot key, valid time, and current/supersession state.
- A rebuild with the same signature is a no-op except for repairing missing provenance edges.

### Provenance graph

Add an explicit relationship:

```text
(:Entity {view_kind:"scalar_history"})
  -[:HISTORY_ENTRY {ordinal, operation, valid_at}]->
(:TypedAssertion)
```

The complete provenance route is:

```text
scalar_history -[:HISTORY_ENTRY]-> TypedAssertion
TurnEvidence    -[:FOUNDS]->       TypedAssertion
Episodic        -[:ADMITTED_ON]->  TurnEvidence
Episodic        -[:MENTIONS]->     scalar_history
new View        -[:SUPERSEDES]->   old View
```

`HISTORY_ENTRY` edges must be redrawn atomically for a View version: remove stale edges and merge the
complete ordered current set in one transaction. A crash must never leave a mixed contributor set
that appears current.

The write receipt must record:

- namespace and canonical slot key;
- current View UUID and superseded View UUID, if any;
- ordered assertion IDs;
- entry count and operation counts;
- source episode IDs and TurnEvidence IDs;
- first/last `valid_at`;
- history signature;
- omitted-entry count when the recall payload is bounded;
- abstentions and repair actions.

## Domain Logic

Add a pure projection builder alongside `scalar_state_fold.py`, for example
`domain/scalar_history.py`.

Input:

- current, fully bound, materializable assertions for one namespace and subject;
- the same normalized slot semantics used by scalar state.

Output per slot:

- ordered history entries;
- signature and summary metadata;
- provenance IDs;
- an optional advisory rendering;
- explicit abstention reasons for malformed time/value/slot data.

The builder must preserve absolute, delta, correction, and expiry operations. The first acceptance
case is delta-only, but choosing the generic history name now prevents another one-off projection
later.

For the postcard case, the builder produces two ordered delta entries and no computed total.
Recall may use the latest statement and its original wording to answer the question, but the View
must label 25 as the latest recorded delta, not an absolute collection count.

Do not infer whether repeated deltas are cumulative or incremental in this phase. Preserve the
source wording (`stated_span`) so the answering layer can distinguish “since I started” from “I
added another.” A later schema extension may add an explicit comparison/baseline key, but only after
measuring extraction reliability.

## Repository and Service Wiring

### Repository

Extend `ScalarViewRepository` or add a narrowly scoped `ScalarHistoryRepository` with:

- `record_scalar_history(...)`;
- `draw_scalar_history_entries(...)`;
- `list_scalar_history(...)`;
- `list_scalar_history_entries(view_uuid, offset, limit)`;
- `retire_scalar_history(...)`;
- stale-current reconciliation by namespace, subject, and slot.

Expose only needed passthroughs through `MemoryGraphAdapter` and its backend protocol.

### Projection coordinator

Refactor `ScalarStateService` into a compatible projection coordinator without breaking existing
callers:

```text
rebuild_scalar_projections(subject_uuid, namespace)
  ├─ rebuild scalar_state for anchored slots
  ├─ rebuild scalar_history for all valid slots
  ├─ reconcile stale state/history Views
  └─ clear projection_pending only after both enabled projections succeed
```

Keep `rebuild_scalar_state(...)` as a compatibility method if tests and callers depend on it.
Typed-scalar persistence and repair paths should call the coordinator when scalar history is
enabled.

Delta-only behavior:

- write/update `scalar_history`;
- preserve the `scalar_state` `no_anchor` receipt;
- do not write `scalar_state`;
- clear `projection_pending` only when both outcomes have been durably recorded.

If one projection succeeds and the other fails, leave the assertion discoverable by the repair
loop. The next rebuild must converge idempotently.

## Lifecycle Requirements

The new View must participate in every existing scalar lifecycle path:

- same-source correction and assertion supersession;
- expiry and reactivation;
- assertion deletion or episode deletion;
- subject merge and absorbed-node retirement;
- unmerge and projection restoration on both identities;
- namespace purge and activation teardown;
- binding mismatch repair;
- stale View reconciliation;
- replay after a code upgrade.

Merge/unmerge acceptance requires:

1. survivor history is rebuilt from the survivor’s materializable assertions;
2. absorbed current history is retired, not silently left recallable;
3. unmerge rebuilds the restored subjects from their own assertion/evidence chains;
4. `HISTORY_ENTRY`, `FOUNDS`, `ADMITTED_ON`, and `MENTIONS` chains remain namespace-local;
5. no assertion is duplicated or reassigned without a corresponding merge/unmerge receipt.

## Recall Contract

Add a dedicated scalar-history observation lane to `recall_pipeline.py`.

It should:

- activate for history/change/comparison questions and as bounded support when a current-state
  query has no anchored scalar state;
- render operation, value, source/world date, and a short source-grounded statement;
- identify the latest entry while retaining earlier entries needed to understand change;
- state: “advisory history; not an absolute current total” for delta-only slots;
- include contributor IDs in structured recall output for provenance inspection;
- remain below an authoritative `scalar_state` head when both exist.

It must not:

- pass the current-anchor foundation check;
- enter the scalar authority layer;
- suppress raw memories on the theory that history is a current head;
- convert latest delta to absolute;
- add delta entries;
- use recorded/ingest time as the displayed event time.

When the feature is disabled, stored `scalar_history` Entities must be excluded from generic recall,
not merely omitted from the dedicated lane. This makes rollback real and prevents an old View from
leaking through vector/entity retrieval.

## Feature Flags and Rollout

Add `MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED`, default off initially.

The gate must control both:

- projection writes/replay; and
- read-side injection plus generic-candidate exclusion.

Suggested rollout:

1. **Dark write in tests:** ViewKind, repository, and pure builder; no production caller.
2. **Replay shadow:** build Views in an isolated copied benchmark graph and inspect receipts.
3. **Advisory read:** enable the dedicated recall lane in a focused fixture.
4. **Candidate benchmark:** fresh 78-item candidate-only LongMemEval run.
5. **Default decision:** enable only after the benchmark evidence and lifecycle tests pass.

Rollback is to disable the read/write flag and retire/purge only `scalar_history` Views and
`HISTORY_ENTRY` edges. Assertions and source evidence remain untouched; no reingest is required.

## Replay and Operations

Add a durable, indexed script or CLI command, for example:

```text
menhir rebuild-scalar-history
  --namespace <namespace or all>
  --neo4j-uri <test URI>
  --receipt <path>
  --dry-run
```

Requirements:

- no LLM calls;
- explicit test Neo4j URI and refusal of known production endpoints unless separately authorized;
- namespace filtering;
- dry-run counts;
- per-slot success/abstention/failure results;
- before/after View and edge counts;
- code commit and effective settings;
- source-time validation;
- idempotence receipt on a second run;
- nonzero exit on any failed slot.

Update `menhir/.agent/scripts-index.md` in the same commit as the durable tool.

## Dashboard and Bench Work

Extend `archolith_bench/scalar_viewer.py`, the dashboard API, and the scalar stage UI to show:

- `scalar_state` and `scalar_history` as distinct projections;
- the canonical slot;
- ordered operation/value/source-time entries;
- `HISTORY_ENTRY` relationships;
- assertion → FOUNDS → TurnEvidence → ADMITTED_ON → Episodic provenance;
- source/world time next to recorded/learned time with unambiguous labels;
- View supersession and current status;
- the fold abstention (`no_anchor`) beside an available history;
- replay/audit receipts correlated from the run-local telemetry database.

The UI must not label a delta-only history “current total.” For the postcard task it should show:

```text
2023-08-11  delta 17
2023-11-30  delta 25  ← latest recorded comparison
scalar_state: abstained (no_anchor)
```

## Focused Postcard Regression

Add a single-item fixture or an explicit question-ID selector before paying for the full 78-item run.
The test must use a fresh isolated graph and the real current pipeline.

Acceptance for `lme-01493427`:

- exactly the expected user turns are ingested with their original LME source dates;
- two current materializable assertions exist for the slot;
- operations are `delta`, values are 17 and 25;
- each assertion has the required vote/gate receipt;
- each assertion traces to TurnEvidence and its source episode;
- one current `scalar_history` View exists for the slot;
- it has exactly two `HISTORY_ENTRY` edges in stable source-time order;
- `scalar_state` remains absent with an explicit `no_anchor` receipt;
- recall returns 25 for the benchmark question;
- recall does not return 42;
- structured recall identifies the result as advisory/latest delta, not an absolute total;
- a second LLM-free projection replay is idempotent.

## Validation Matrix

### Unit

- canonical history key and namespace isolation;
- source-time ordering and assertion-ID tie break;
- deterministic signature;
- numeric/string/JSON value retention;
- bounded rendering versus complete contributor list;
- absolute, delta, correction, and expiry entry preservation;
- delta-only history with no computed state;
- malformed/missing `valid_at` fail-closed behavior;
- advisory wording and no-authority classification.

### Repository and online test Neo4j

- create, idempotent rewrite, supersede, and retire;
- atomic `HISTORY_ENTRY` redraw;
- `MENTIONS` repair from contributor episodes;
- contributor pagination;
- cross-namespace query refusal;
- no duplicate current View keys;
- rollback/read gating with stored Views present.

### Service and lifecycle

- delta-only slot writes history and records state abstention;
- anchor plus later delta writes both state and history;
- correction changes signature and contributor set;
- expiry retires/rebuilds correctly;
- crash between projection writes remains repairable;
- projection pending clears only after all enabled projections complete;
- merge/unmerge, deletion, binding repair, and namespace purge.

### Recall

- history intent surfaces ordered history;
- no-anchor current query may receive advisory history;
- history never enters the authority layer;
- history never suppresses raw evidence;
- anchored state outranks history;
- source/world times render correctly;
- postcard answer is 25, not 42.

### Full verification

- focused Menhir tests;
- all Menhir unit tests;
- relevant online Neo4j suites against the ephemeral test instance;
- archolith-bench dashboard and fixture tests;
- LLM-free replay on the preserved v4 benchmark graph;
- fresh two-item candidate checkpoint;
- fresh candidate-only 78-item ingest and recall/QA score after checkpoint approval.

## Benchmark Provenance Gates

Before the next canonical paid run, harden the wrapper:

1. Set a per-run telemetry database, for example
   `${LME_RESULTS_DIR}/mcp_telemetry.db`, for both ingest and recall Menhir processes.
2. Preserve that database with the results and point the dashboard at it.
3. Refuse a canonical resume when the Menhir commit, bench commit, fixture SHA, effective settings,
   container, or volume differs from the original run.
4. Permit mixed-code resume only behind an explicit `noncanonical` development flag and label every
   output accordingly.
5. Include untracked-file review in operator preflight; the current tracked-only dirty check is not
   enough to prove the executed source.
6. Emit a machine-readable final acceptance report covering manifest cardinality, failed episodes,
   projection counts, source-time integrity, provenance-chain completeness, namespace isolation,
   commit immutability, and telemetry presence.

The preserved v4 graph may be used for LLM-free replay diagnostics. It cannot become final benchmark
evidence because it is partial and spans multiple Menhir/bench commits.

## Implementation Slices

1. `feat(scalar): add slot-keyed scalar history ViewKind`
   - pure builder, ViewKind, key/signature, repository API, unit/repository tests.
2. `feat(scalar): rebuild history with provenance and lifecycle`
   - coordinator, `HISTORY_ENTRY`, repair markers, replay tool, lifecycle and online tests.
3. `feat(recall): surface scalar history as advisory context`
   - read gate, generic exclusion, rendering, authority controls, recall tests.
4. `test(bench): add postcard scalar-history fixture and explorer`
   - focused fixture, dashboard/API, source-time and provenance display tests.
5. `fix(longmemeval): make scalar evidence runs immutable`
   - per-run telemetry, commit/settings resume refusal, final provenance validator, runbook.

Each slice should be independently reviewable. Do not combine code changes with a paid benchmark
continuation. Finish, commit, and verify both repositories before creating the fresh run ID.

## Alternatives Considered

### Treat the latest delta as an absolute scalar state

Rejected. It would make the postcard score look right by changing the meaning of the evidence and
would create false current totals elsewhere.

### Sum all deltas with the legacy counter fold

Rejected. It can turn the postcard history into 42 and bypasses the typed slot, evidence, and
authority contracts already implemented.

### Wire `fold_events_to_timeline` unchanged

Rejected. It is subject-wide, not slot-keyed, loses required typed payload details, and has no
complete lifecycle/provenance integration.

### Put the history only in the dashboard

Rejected. That would make the evidence inspectable to an operator but still unavailable to recall,
leaving the product problem unsolved.

### Change only the extraction prompt

Rejected for this case. The model correctly extracted two deltas. A prompt change would conceal a
projection gap and risk relabeling legitimate relative statements as absolutes.

## Risks

- **Authority leakage:** fact-class Views can enter generic recall. Mitigate with a dedicated read
  lane, explicit exclusion, and tests with the feature toggled off.
- **Semantic overreach:** “delta” alone does not say cumulative versus incremental. Preserve wording
  and do not compute an unanchored total.
- **Stale provenance:** separate View and edge writes can diverge. Use atomic contributor redraw and
  a durable repair marker.
- **Payload growth:** full histories can be large. Keep the graph log lossless and the recall payload
  bounded/paginated.
- **Lifecycle drift:** a new ViewKind can be missed by deletion or merge code. Require the complete
  lifecycle matrix before enabling reads.
- **Benchmark laundering:** resuming across commits can make a result look canonical. Fail closed on
  commit/settings drift and report low scores honestly.
- **Time corruption:** ingest/recorded timestamps are easy to display accidentally. Test original
  LME dates at assertion, View, API, and browser boundaries.

## Acceptance Criteria

Implementation is complete only when:

- the postcard focused fixture passes the exact contract above;
- delta-only slots produce history without producing absolute scalar state;
- every history entry has a complete evidence chain and original source/world time;
- history never becomes authoritative or suppresses raw evidence;
- replay is LLM-free, idempotent, namespace-safe, and receipt-bearing;
- correction, expiry, deletion, merge, and unmerge tests pass;
- the dashboard makes provenance and time semantics inspectable;
- all focused and full test suites pass;
- a fresh candidate-only 78-item run completes on one immutable Menhir/bench commit pair with a
  run-local telemetry database and 78 unique successful manifest rows;
- the final score and failures are reported as measured, without substituting an older or mixed-code
  run.

## Documentation to Update

- umbrella `.agent/architecture.md` and plan index if applicable;
- Menhir `.agent/architecture.md`;
- Menhir `.agent/data_models.md`;
- Menhir `.agent/workflows/scalar_state_measurement.md`;
- Menhir `.agent/scripts-index.md`;
- Menhir `.agent/CHANGELOG.md`;
- archolith-bench `.agent/architecture.md`;
- archolith-bench `.agent/workflows/scalar-state-e2e-runbook.md`;
- archolith-bench `scripts/longmemeval/README.md`;
- archolith-bench `results/lme-ku-buildout/LEDGER.md` after every completed or stopped run.
