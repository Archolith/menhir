# Intent State View

Status: **planned; implementation not started**

Replaces: recall-packet phrase matching for tentative plans in
`typed-recall-packet-prototype.md`.

## Why

- Tentative intent is currently inferred in Recall Lab by searching an entire retrieved memory for
  phrases such as “thinking about.” An aggregate containing one tentative clause and one factual
  clause is therefore labelled tentative as a whole.
- `lme-830ce83f` demonstrated the defect: “thinking about visiting Rachel” and “Rachel moved back
  to the suburbs” were combined in one retrieved entity summary and given one tentative label.
- Tentative/plan status is durable semantic state with time, provenance, supersession, completion,
  and cancellation. It belongs at ingest behind an evidence-grounded assertion and deterministic
  View fold, not in a recall formatter.

## Scope

In scope:

- Add an atomic, evidence-grounded `TypedIntentAssertion` and deterministic `intent_state` View.
- Reuse the scalar consolidation page spine, subject/target binding, world-time discipline,
  projection rebuild, contributor proof, namespace cleanup, and default-off rollout conventions.
- Represent an intent lifecycle conservatively: `considering`, `planned`, `committed`, `completed`,
  `cancelled`, or `expired`.
- Render admitted Intent Views in Recall Lab and LLM context as advisory intent.
- Remove all recall-side inference of tentative authority from free text. Untyped retrieved content
  remains General Content until ingest has produced an admitted Intent View.

Out of scope:

- Turning an intent into current factual state merely because it is newer or more confident.
- Manufacturing a completed event from an Intent View. Explicit completion evidence may separately
  produce both an intent-state transition and a `TypedEventAssertion`.
- A broad action ontology, benchmark-specific phrases, task IDs, or noun/verb allowlists shaped to
  LongMemEval.
- Enabling intent authority by default or running the full KU78 panel before generic gates pass.

## Proposed Design

```text
TurnEvidence
  -> intent perception and admission (same consolidation spine as scalar/event ingest)
  -> TypedIntentAssertion (immutable, atomic, source-grounded)
  -> deterministic latest-state fold by world time
  -> intent_state View (disposable, recallable projection)
  -> structured advisory intent in context and Recall Lab

The recall formatter only renders the View. It never decides that prose is tentative.
```

### Contract and identity

`TypedIntentAssertion` carries namespace, resolved actor, canonical action, resolved target when
available, grounded target/action text, lifecycle status, `valid_at`, `learned_at`, exact source
span/quote, episode/TurnEvidence identity, evidence tier, perceiver version, and binding state.

The lane is `(namespace, actor_uuid, action, target_key, scope)`. The action vocabulary stays small
and structural; the target remains open-world and grounded, with a durable entity UUID when safely
resolved. Ambiguous action or target identity is retained for audit but cannot materialize a View.

The durable assertion log is the source of truth. `IntentStateKind` is a new LWW View kind using the
existing generic View writer, rather than changing `TypedAssertion` identity or allowing intent into
`ScalarStateKind`. This reuses scalar fold mechanics without mixing “what is true” with “what someone
may do.” Exact contributor edges make every View rebuildable and auditable.

### Deterministic fold

- Order eligible assertions by parsed `valid_at`; `learned_at` is audit time only.
- Exact replay deduplicates by assertion/source identity.
- Distinct same-time winning statuses fail closed as ambiguous.
- A later explicit cancellation, completion, or expiry closes the active intent; silence does not.
- Intent never writes a Scalar State View. Explicit factual-state evidence follows the existing
  scalar path, and explicit completed-occurrence evidence follows the Event History path.
- Rebinding, namespace deletion, repair, and projection reconciliation mirror the existing scalar
  and event contracts.

### Delivery phases

1. **Retire recall inference.** Delete `_TENTATIVE_MODALITY` classification from the typed packet.
   Until Intent Views exist, route ordinary retrieved memories to General Content. Add a regression
   proving that mixed prose cannot acquire tentative authority during recall.
2. **Pure domain contract.** Add `TypedIntentAssertion`, `IntentLane`, lifecycle status, stable
   identities, and a pure fold with replay, time, transition, ambiguity, and namespace tests.
3. **Perception and admission.** Extend the existing consolidation traversal to emit atomic intent
   proposals from exact user evidence. Share binding/time/evidence utilities; keep a separate cursor
   only if replay measurements prove the scalar cursor cannot safely serve both outputs.
4. **Persistence and View.** Add the durable repository, `IntentStateKind`, deterministic rebuild,
   exact contributor proof, merge rebind/unmerge repair, and namespace cleanup.
5. **Transport and inspection.** Project Intent Views through graph inspection, Recall Lab, REST/MCP
   context, and the LLM packet as structured advisory entries. Rendering may filter or budget already
   typed Views but may not infer their type.
6. **Rollout.** Shadow/default-off first, measure clause atomicity and false-current rate on generic
   governance, coding, personal-plan, negation, cancellation, and completion examples; then run a
   frozen focused panel and finally KU78.

## Alternatives Considered

- **Improve the recall regex.** Rejected: aggregate prose has no single correct modality, and every
  larger phrase list moves semantic admission into an unauditable read-time heuristic.
- **Store intent directly as ordinary Scalar State.** Rejected: it would let plans compete with facts
  and would require changing the existing TypedAssertion/slot identity contract.
- **Store all intent as Event History.** Rejected: contemplated actions are not occurrences. Event
  History may record explicit completion, while Intent State records the plan lifecycle.
- **Use only raw semantic relationships.** Rejected: they lack lifecycle supersession, authority,
  ambiguity handling, and a query-sufficient current projection.

## Risks

- Over-broad action normalization can merge unrelated plans; ambiguous identity must abstain.
- LLM perception can collapse multiple clauses; admission requires one assertion per exact source
  span and rejects aggregate summaries as grounding.
- “Will” can be prediction, promise, or plan; modality ambiguity must remain unmaterialized rather
  than forced into a status.
- Completion can accidentally promote a plan into fact; event and scalar admission remain separate,
  independently grounded decisions.
- Adding another consolidation cursor can create replay drift; prefer the existing page spine and
  introduce an independent watermark only with a documented ownership reason.

## Invariants

- Recall never assigns tentative authority from free text.
- Intent is advisory and never overrides Scalar State or proves an Event.
- Every materialized Intent View terminates in admitted TurnEvidence with an exact quote.
- `valid_at` controls fold order; `learned_at` never substitutes for world time.
- Flag-off ingest, graph, recall, API, and packet behavior remains compatible except that the
  inspection prototype stops claiming tentative authority for untyped content.
- Production code contains no benchmark IDs, gold answers, or fixture-specific vocabulary.
- Views remain disposable; assertions remain durable and repairable.

## Validation

- Pure tests: stable identity, lane isolation, replay, malformed/missing time, same-time ambiguity,
  cancellation, completion, expiry, future assertions, and invalid transitions.
- Perception tests: misspellings, informal grammar, contractions, mixed factual/tentative clauses,
  questions, hypotheticals, negation, quoted plans, coding plans, and governance decisions.
- Repository tests: binding, contributor edges, deterministic rebuild, merge/unmerge, namespace
  deletion, and flag-off compatibility.
- Recall regression: no `_TENTATIVE_MODALITY` or equivalent free-text authority classifier remains;
  untyped content is General Content and typed Intent Views preserve evidence/status/time.
- Frozen acceptance: `lme-830ce83f` must produce separate advisory visit/ask intents and a factual
  suburbs relationship/event/state path; its ID and wording may appear only in Bench fixtures/tests.
- Promotion gates: zero false-current or false-completed admissions, 100% grounding/provenance,
  deterministic replay, and a pre-registered generic held-out panel before KU78.

## Docs To Update

- `.agent/data_models.md`
- `.agent/architecture.md`
- `.agent/memory-governance.md`
- `.agent/default-off-features.md`
- `.agent/memory-backlog.md`
- `.agent/endpoints.md` if transport payloads change
- `CHANGELOG.md` and the sibling Bench data-model/runbook docs

## Decision Log

- **I1:** Tentative authority is admitted at ingest and materialized as a View; recall only renders.
- **I2:** Intent uses a sibling durable assertion and `intent_state` View kind, reusing scalar fold
  infrastructure without entering factual Scalar State identity.
- **I3:** Event completion and factual state remain independently grounded outputs.
- **I4:** Recall-side tentative phrase matching is retired before further packet-quality evaluation.
