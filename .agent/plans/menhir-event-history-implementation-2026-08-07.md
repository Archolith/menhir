# Event History Implementation

Status: **Phases 1–5 complete on main; default-off production-capable** (see Implementation Checkpoint)

Builds on: `menhir-temporal-event-history-view-2026-07-30.md` and the existing
`Event -> Fold -> View` architecture.

## Why

The canonical KU78 run exposed categorical temporal questions that scalar state cannot answer:
which item was acquired most recently, and which event occurred immediately before another event.
Menhir already has a deterministic fold algebra and a lossless `timeline` View, but it does not yet
persist predicate-bearing event assertions or produce an authoritative latest/predecessor verdict.

## Scope

In scope:

- A durable, evidence-grounded `TypedEventAssertion` contract.
- Deterministic event lanes keyed by namespace, subject, predicate, and optional domain.
- Pure latest and predecessor selection by source/world time.
- Reuse and extension of the existing `TimelineKind`; no competing event-history store.
- Flag-gated perception, persistence, projection, recall authority, repair, and explorer support.
- Generic tests first, followed by frozen acceptance checks for `lme-41698283` and `lme-0977f2af`.

Out of scope:

- Replacing numeric scalar state/history.
- Inferring loss of ownership from a newer acquisition.
- A broad event ontology, unrestricted temporal reasoning, or benchmark-specific production rules.
- Enabling event authority before evidence, ambiguity, replay, and repair gates pass.

## Proposed Design

```text
TurnEvidence
  -> event perception and admission (LLM boundary, flag-gated)
  -> TypedEventAssertion (immutable, source-grounded)
  -> deterministic lane fold
  -> existing timeline View (disposable projection)
  -> latest/predecessor selector
  -> event authority verdict with quote and valid_at
```

`TypedEventAssertion` carries stable source identity, namespace, resolved subject, canonical
predicate, object identity/display, optional domain, `valid_at`, `learned_at`, exact quote/span,
founding episode/TurnEvidence identity, time basis, evidence tier, and perceiver version. It records
an occurrence and never supersedes a sibling event merely because it is newer.

The timeline lane key is `(namespace, subject_uuid, predicate, domain)`. Timeline entries are ordered
by parsed `valid_at`, never ingest order. Exact replay deduplicates by assertion/source identity.
Missing or invalid source times are retained for audit but cannot win an authoritative temporal
selection.

Pure selection supports:

- `latest`: greatest eligible `valid_at` at or before `as_of`.
- `predecessor`: greatest eligible `valid_at` strictly before a resolved anchor event/time.
- fail-closed ambiguity when distinct candidates tie for the winning world time.

The existing scalar authority payload stays scalar-specific. Event recall gets a separate structured
verdict that contains predicate, selected object, source time, quote/evidence identity, status, and
the first failed gate. Advisory results may be shown; only fully grounded, uniquely ordered results
may lead.

## Delivery Phases

### Phase 1 — domain contract and pure selector

- Add `TypedEventAssertion`, lane identity, temporal intent, and selection result types.
- Add pure latest/predecessor selection with namespace/lane filtering, `as_of`, tie handling, and
  deterministic output.
- Add synthetic tests covering malformed time, ties, future events, lane isolation, replay, and
  predecessor anchors. No LongMemEval IDs or phrases in production code.

### Phase 2 — persistence and timeline projection

- Add typed-event repository writes/reads with idempotent source and assertion keys.
- Extend `TimelineKind` and its repository wrapper to key and parse predicate/domain lanes while
  preserving the existing subject-only timeline API.
- Rebuild projections from assertions and retain explicit contributor provenance.

### Phase 3 — perception and admission

- Add a small predicate registry (`purchased/acquired`, then measured additions).
- Preserve exact quotes and distinguish completed events from intent/hypotheticals.
- Shadow first; record abstentions and disagreements. LLM output is perception only—ordering,
  folding, and selection remain deterministic.

### Phase 4 — recall authority

- Detect latest and predecessor temporal queries independently of scalar current-state intent.
- Resolve subject/predicate/domain and apply evidence, time, uniqueness, and foundation gates.
- Serialize a separate event verdict without changing scalar verdict contracts.

### Phase 5 — repair, explorer, and rollout

- Add deletion/policy repair receipts and deterministic projection rebuild.
- Render evidence -> event assertion -> timeline -> selected verdict in Recall Lab.
- Run frozen acceptance cases, then a stratified temporal-categorical set before any enablement.

## Implementation Checkpoint

Status updated 2026-08-07. Phases 1–5 are complete on `main` through commit `370eff1`, as a
**default-off production-capable** path. Every event settings/flag defaults off and flag-off behavior
is byte-compatible; scalar assertion/state/history/authority and wire contracts are unchanged. There
is no dedicated event endpoint, no default enablement, and no canonical-run gain claim from this
infrastructure.

What landed (all default-off):

- **Phase 1 — domain contract + pure selector.** Immutable `TypedEventAssertion` / `EventLane`
  contract with stable `source_key` (binding-stable locator) / `assertion_key` (fully-interpreted
  identity) / `lane` (fold-selection scope) identities and a pure latest/predecessor selector
  (`select_event_assertion`). `valid_at` (world/source time) is the only ordering/selection time;
  `learned_at` is audit/ingest time only.
- **Phase 2 — persistence + timeline projection.** Durable append/audit
  `TypedEventAssertionRepository` with idempotent head/source keys, strict-rank supersession,
  binding-safety (`binding_mismatch` fails closed), pending→bound adoption, provenance, and monotonic
  evidence-tier upgrade. Predicate/domain event-lane support on the existing `TimelineKind` (legacy
  subject-only timeline behavior preserved) plus event timeline Views with exact `EVENT_HISTORY_ENTRY`
  contributor edges and a deterministic exact-lane rebuild
  (`EventHistoryService.rebuild_lane(s)`). `MemoryGraphAdapter` delegates for the assertion log and
  the event-lane timeline View sink.
- **Phase 3 — perception + admission.** `services/event_history_perception.py` is the generic,
  offline LLM extraction/admission seam (single completed-acquisition predicate registry
  `acquired`, exact-quote/unique-span grounding, completed-vs-intent/hypothetical/negation
  discrimination, fail-closed admission). LLM output is perception only — ordering, folding, and
  selection remain deterministic. `services/event_consolidation.py` backfills grounded occurrences
  from canonical user `:TurnEvidence` into durable assertions and rebuilds affected lanes, advancing
  an **independent** `:EventConsolidationWatermark` cursor keyed by namespace in `group_id` (never
  disturbing the scalar/counter cursors), under a fail-closed page spine that emits bounded, generic
  metrics.
- **Phase 4 — recall authority.** `services/event_history_recall.py` (pure classifier + selector over
  `EventQueryRoute`) and `services/event_history_authority.py` (structured `EventAuthorityVerdict`)
  reason only over in-memory assertions. `RecallService` has a **conditional**
  `event_history_authority_enabled` gate (default off): only when enabled AND a namespace is present
  does recall probe a recognized conservative first-person event route, read assertions via
  `event_assertions_for_subject_predicate`, and attach `RecallResult.event_authority_layer` — a
  separate structured verdict never interleaved with or reranked among observations, and never
  changing the scalar verdict contract.
- **Phase 5 — transport + lifecycle closeout.** The event authority layer is carried through REST
  `/api/recall` (`event_authority_layer`), the MCP `recall_memories` tool, the `ContextBuilderService`
  context block, and the backend round-trip. The scheduled personal-memory job and the manual
  `POST /api/phase3/run` surface drive event consolidation when enabled and return bounded Phase-3
  event metrics. Namespace cleanup is event-aware (`delete_namespace_with_scalar_cascade` deletes the
  namespace-keyed event log and the independent watermark, and preserves a shared
  `:TypedEventAssertionHead` that still `HAS_VERSION` to a surviving assertion in another namespace —
  shared-head safety; its deleted CURRENT is repaired by a later idempotent write).

Invariants holding as built: `valid_at` (world/source time) is the only ordering/selection time;
`learned_at` is retained only as audit/ingest time and is never authority ordering or fallback.
Invalid `valid_at` stays durable but cannot enter the View or lead. Exact replay dedups; distinct
same-world-time winners fail closed as ambiguous; event siblings are occurrences and never supersede
merely because one is newer. Projection is disposable/rebuildable; the durable
`TypedEventAssertion` + evidence is the source of truth, and rebuild completes only after a
successful View write, exact contributor-edge proof, and exact-lane reconciliation. Namespace/lane
isolation is exact. No dedicated event endpoint, no default enablement, no benchmark IDs/answers in
production code, and no canonical KU78 gain claim.

## Alternatives Considered

- **Categorical scalar state:** rejected because a newer purchase does not invalidate ownership of an
  older item.
- **A new `event_history` View kind:** rejected for now because `TimelineKind` already represents the
  required lossless ordered projection; extending its lane identity avoids a competing source of truth.
- **Timestamped semantic recall only:** useful fallback context, but insufficient for predicate
  filtering, replay, ambiguity, or auditable authority.

## Risks

- Predicate normalization can over-generalize; begin with a small registry and abstain on uncertainty.
- Same-time events can make latest/predecessor ambiguous; ties never produce authority.
- Extending timeline keys can break existing windowed-count callers; the legacy subject-only API and
  key remain backward compatible.
- Benchmark overfitting; production code may not contain fixture IDs, answers, or fixture-specific
  wording.

## Invariants

- Scalar assertion, state, history, authority, and wire contracts remain unchanged.
- Feature flags default off and flag-off behavior is byte-compatible.
- Namespace and lane isolation are exact.
- World/source time controls order; `learned_at` is never a historical tiebreaker.
- Every leading verdict terminates in admitted evidence with an exact quote.
- Event histories are append-only in meaning, replay-safe, and rebuildable.
- Ambiguity, missing foundation, or missing time yields advisory/abstention.

## Validation

- Unit: domain validation, stable keys, lane isolation, replay, future filtering, invalid/missing time,
  ties, latest, and predecessor.
- Repository: namespace isolation, legacy timeline compatibility, contributor edges, and deterministic
  rebuild.
- Integration: `lme-41698283` selects the 70–200mm acquisition while wide-angle intent abstains;
  `lme-0977f2af` selects Instant Pot as predecessor to Air Fryer.
- Regression: scalar suites, windowed fold/recall suites, recall wire/API tests, and flag-off snapshots.
- Rollout: shadow yield/precision/abstention/cost telemetry before canary authority.

## Validation Status (2026-08-07 closeout)

- **230 event-focused tests** covering perception/admission, consolidation + independent cursor,
  recall/authority, and the REST/MCP/context/backend transports.
- **Production canary** (isolated Neo4j-backed): passed **13 checks** with **3 chat + 3 embedding
  calls / 1,954 tokens**.
- **Focused NONCANONICAL 5-case LongMemEval panel**: passed **5/5** with **15 calls / 12,436 tokens**
  and zero safety violations.
- Result artifacts live in the sibling `archolith-bench` repo under
  `results/event-history-canary/production-path-v1-20260807` and
  `results/event-history-acceptance/event-history-production-gate-v4-20260807`.

## Docs To Update

- `.agent/architecture.md`
- `.agent/data_models.md`
- `.agent/endpoints.md`
- `.agent/memory-governance.md`
- `.agent/memory-backlog.md`
- `.agent/CHANGELOG.md` and `CHANGELOG.md`
- Recall Lab and LongMemEval runbooks in `archolith-bench`

## Agent Execution Contract

DeepSeek receives one phase at a time with a named-file fence, no git operations, no test/build
execution, and no scope expansion. The orchestrator owns architecture decisions, reviews every diff,
runs verification centrally, and accepts or rejects each slice before the next begins.
