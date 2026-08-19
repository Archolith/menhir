# Menhir ScalarStateView - Typed Scalar Memory as a Derived Authoritative View (Design Plan)

**Project:** menhir (production memory system; design only - NO prod code in this plan)
**Date:** 2026-07-18
**Author:** Claude Code (design brief by Charles Harvey)
**Status:** DRAFT (Rev 2) - design for review. No implementation until approved.
**Rev 2 (2026-07-18):** incorporates code-grounded review. Anchored the ingestion seam on the
existing perception boundary (`services/perception.py` / `perceive_and_fold`); replaced the
"exactly rebuildable by re-perception" claim with a durable persisted typed-assertion event log
(perception is probabilistic); split View identity into `view_subject_uuid` (keying) vs
`view_subject` (display) as an explicit `ViewRepository` contract change; and defined a fail-closed
overlap contract + grounded-evidence requirement for authoritative suppression.
**Evidence basis (archolith-bench typed-value arc):**
- `scripts/longmemeval/analysis/TYPED-VALUE-ARM.md` (v1 validated; v2/v3/v4/v5 + oracle probe)
- `scripts/longmemeval/analysis/oracle_entity_grouping_probe.py` (bench commit `8d3fb56`)
- Bench arc commits: v3 `af22aa4`, v4 `6270425`, v5 `8c86a31`, oracle probe `8d3fb56`
- Bench-side plan (rejected sidecar line): `archolith-bench-typed-value-supersession-arm-plan.md`,
  `archolith-bench-typed-value-derivation-arm-plan.md`

## 1. Why this plan (what the bench proved)

The bench spike ran a typed scalar recall arm through four independent sidecar variants and a
decisive oracle probe:
- **Representation validated.** v1 typed-value recall: 0.679 vs `menhir_recall` 0.333 vs
  `no_memory` 0.064 on 78 knowledge-update items; recovered 21/29 extraction-loss cases where
  ordinary recall scored 0/29 *with the same context*. Typed scalars are recoverable facts that
  Graphiti's entity-to-entity edge model does not surface.
- **Lexical sidecar authority + lexical entity grouping rejected** across four variants:
  v2 (lexical clustering fragments), v3 (coarse over-merges; authoritative single-pick regresses
  by deleting the correct candidate), v4 (deterministic supersession tier fires 0/78), v5
  (delta fold fires 1/78). All failed for the *same* reason: sentence tokens are not entity identity.
- **Oracle probe (decisive).** Regrouping the 8 residual-miss cases by hand-labeled resolved
  `(entity, attribute)` and re-running the SAME selection + delta logic recovered **6/8**
  (`dfde3500` Juan-vs-Maria, `b6019101` MCU-vs-all-films scope, `59524333` gym-vs-meeting,
  `f9e8c073`/`dad224aa` recency, `69fee5aa` delta). The 2 misses (`71315a70`, `e66b632c`) are
  **reasoning**, not entity resolution (mention-order != truth; previous-value question), already
  handled by the advisory show-candidates path.

**Conclusion the plan graduates:** the selection/delta logic already works; the blocker is real
entity/scope resolution. That resolution exists only in Menhir/Graphiti's entity layer, not a
lexical sidecar. So typed scalar state must be a **derived, entity-linked, selectively
authoritative View inside Menhir** - not another sidecar and not new Graphiti edges.

## 2. Architecture: ingestion seam + ownership boundary

Menhir already has a typed-scalar path: `services/perception.py` converts episodes into typed
`Event`s at a **probabilistic perception boundary** (multiple model samples), then
`perceive_and_fold()` canonicalizes, gates, and **deterministically folds** accepted events into
counter Views. The ScalarStateView is an **extension of that perception boundary**, not a free
output of Graphiti combined extraction:

```
grounded episode / TurnEvidence
  -> existing scalar perception (services/perception.py; probabilistic proposal)
  -> proposed TypedAssertion (absolute value) OR DeltaEvent (signed increment)
  -> bind subject to resolved Menhir/Graphiti entity UUID   (identity anchor)
  -> persist typed assertion/event to the durable derived event log  (see 2.2)
  -> deterministic fold (perceive_and_fold-style gate + reducer)
  -> ScalarStateView materialization
  -> recall composer (selectively authoritative)
```

### 2.1 Ownership boundary

**Menhir View / perception layer OWNS**
- the typed-scalar perception extension (proposing TypedAssertion / DeltaEvent);
- binding each proposed assertion's subject to a resolved entity UUID;
- the durable typed-assertion/event log (rebuild inputs, with provenance);
- typed assertion history and entity-linked attribute series;
- absolute values and delta events; the deterministic fold/reducer;
- current / historical selection; confidence and provenance;
- authoritative-versus-advisory recall behavior;
- rebuilding ScalarStateView from the persisted event log (see 2.2).

**Graphiti OWNS (unchanged)**
- entity creation and resolution; general relational facts;
- episode-to-entity / fact provenance; broader graph recall.

Graphiti **supplies the resolved identity + supporting evidence**; it does not store scalar state
and its entity-to-entity edges are not the scalar store. Entity resolution runs in the existing
ingestion path; the perception extension binds to the resolved UUID *after* resolution.

### 2.2 Derived-projection invariant (durable event log, not re-perception)

Perception is **intentionally probabilistic** - re-running it over the same episodes can accept a
different event set - so "rebuild the View exactly by re-perceiving episodes" is NOT true. The
invariant is layered so exact rebuild is real:

```
Episode / TurnEvidence   = source of evidence (immutable)
TypedAssertion / DeltaEvent = grounded, normalized, entity-bound event
                              PERSISTED as the durable derived event log (with provenance)
ScalarStateView          = disposable projection, rebuilt EXACTLY from the event log
```

- ScalarStateView is rebuilt deterministically **from the persisted typed-assertion/event log**,
  not by re-perceiving episodes. Given the same event log, the fold reproduces the View exactly.
- Re-perception is a **separate, versioned** operation: it may intentionally change the event log
  when the perceiver/model version changes (record `perceiver_version` on each event). That is a
  deliberate log revision, not a silent View drift.
- Episodes remain the evidence source; typed assertions are the rebuild inputs; Views stay
  disposable. No second inconsistent truth store.

## 3. The View key + repository contract change

Identity is anchored on the resolved UUID (the decisive probe result), which requires splitting
keying from display in `ViewRepository`. Today the repository keys on the lowercased **textual
subject**, and `record()` accepts only that display subject - that must change.

```
View key: namespace + view_subject_uuid + attribute/state_family + scope + value_kind + unit

New/!changed ViewRepository fields:
  view_subject_uuid   # identity + keying (resolved_entity_uuid) - NEW, drives the key
  view_subject        # human-readable display surface (summaries, retrieval text) - unchanged role
```

- **`view_subject_uuid` is the identity anchor** and the key component; lexical canonicalization
  MAY *propose* `attribute/state_family` (watched vs on_list, owned vs sold) but must **never
  decide entity identity**.
- **Do NOT pass the UUID as the existing `subject`.** Overloading the display subject with a UUID
  would damage View summaries and retrieval text. This is a real `record()` / keying contract
  change; the existing generic `ViewKind` architecture (centralized keying, signatures,
  versioning, provenance, supersession) is well suited to it, but the change is explicit.
- `scope` carries the differentiating modifier that must not collapse distinct series (MCU vs all
  films; Korean vs Italian). `value_kind` + `unit` keep only same-typed values in competition.

## 4. ScalarStateView = extension of existing View machinery (not a parallel subsystem)

Add a new View **category** `ScalarStateView` (a new `ViewKind`) to Menhir's existing generic View
layer, reusing the Phase-3 counter-View consolidation machinery (Views already carry `history` +
`superseded`, a correction resolver, and abstention receipts). Extend from counters to the 9 scalar
`ValueKind`s, using the deterministic `perceive_and_fold` fold path as the model.

Do NOT create a separate `typed_values` truth store. Reusing the generic View machinery inherits
provenance, history/superseded semantics, correction resolution, and abstention receipts, and keeps
one consistent memory model. The only repository-level change is the `view_subject_uuid` / display
split in section 3.

## 5. Recall authority model (query-intent gated, fail-closed overlap)

The View must not merely add snippets beside stale untyped recall (the v1-v5 structural ceiling:
the sidecar governed ~3/10 slots while untyped backfill reintroduced stale values). It owns
composition authority, gated on query intent AND on a proven overlap contract:

- **Confident current-state query** (entity-resolved, unambiguous current value, grounded evidence):
  `authoritative typed View match -> emit current value -> suppress overlapping untyped facts.`
  (Probe: the 6 entity-grouped cases.)
- **Previous-value / comparison / history / uncertain intent:**
  `typed View history + supporting facts -> advisory context -> answer model reasons.`
  (Probe: `71315a70`, `e66b632c` - forcing a current pick is wrong; showing candidates is right.)

### 5.1 Overlap proof (fail-closed) - what licenses SUPPRESSION
An untyped fact may be **suppressed** only when overlap with the authoritative View value is
*proven* on all of:
```
same resolved entity UUID
+ same attribute / state family
+ compatible scope
+ compatible value_kind / unit
```
When any element of the proof is **incomplete or uncertain, DEMOTE rather than suppress.** This is
fail-closed: without full proof, the untyped fact stays visible (demoted), so authority cannot
repeat v3's failure of removing a correct-but-differently-represented fact.

### 5.2 Evidence tier gate - what may be AUTHORITATIVE
Authoritative Views require **grounded user/manual or trusted-tool evidence** on the current value.
**Agent-inferred** values remain **advisory** regardless of confidence. Authority is earned by
(entity resolution + unambiguous intent + grounded evidence + proven overlap), never unconditional.

## 6. Plan scope - four bounded pieces (first plan only)

1. **Domain contract.** `TypedAssertion` (absolute value) and `DeltaEvent` (signed increment);
   fields: `view_subject_uuid`, `attribute/state_family`, `scope`, `value` + `unit`, `operation`
   (absolute|delta), `learned_at`, optional `valid_at`, `evidence_tier`
   (user|manual|trusted_tool|agent), `perceiver_version`, `provenance` (episode / TurnEvidence
   anchor). This is the **durable persisted event log** (2.2) - the rebuild input, not a cache.
2. **Materialization.** Perception extension proposes events; bind to resolved UUID; persist to the
   event log; deterministic fold rebuilds one scalar-state history per View key. Idempotent and
   fully regenerable from the persisted log.
3. **Recall authority.** Query-intent gate (current-state vs previous/comparison/history/uncertain)
   + the fail-closed overlap proof (5.1) + the evidence-tier gate (5.2) for suppression/demotion.
4. **Validation staging (gated rollout).**
   - **Shadow-build** Views (materialize from the event log; no recall effect) - verify rebuild
     determinism, provenance, and the UUID/display split.
   - **Counterfactual recall** - compare View-composed vs current recall offline; no user impact.
   - **Current-state-only canary** - allow authoritative suppression for confident, grounded
     current-state queries only, measured, before any broader authority.

## 7. Non-goals (explicit - NOT in the first plan)
- **No brand-new extractor, but this IS an extension of the perception boundary.** Reuse
  `services/perception.py` + `perceive_and_fold`; add typed-scalar proposal + UUID binding + fold
  to ScalarStateView. It is not a free output of Graphiti combined extraction.
- **No schema-wide Graphiti redesign.** Graphiti keeps entity resolution + relational facts as-is.
- **No generalized temporal-reasoning engine.** `valid_at` is captured when the claim states it;
  world-valid temporal inference is out of scope.
- **No arithmetic/filtered-count/coreference reasoning engine.** Delta fold (absolute + signed
  increments) is in; comparative, filtered-count, and coreference are separate future primitives.
- **No parallel typed-value truth store.** ScalarStateView is a disposable projection over the
  persisted typed-assertion event log.

## 8. Risks & mitigations
| Risk | Mitigation |
|------|-----------|
| Probabilistic perception breaks exact rebuild | rebuild from the PERSISTED event log, not re-perception; `perceiver_version` on each event; re-perception is a deliberate versioned log revision |
| Second inconsistent memory system | derived-projection invariant; event log = rebuild input with provenance; Views disposable; reuse generic View machinery |
| UUID overloaded onto display subject damages summaries/retrieval | explicit `view_subject_uuid` (key) vs `view_subject` (display) contract split in ViewRepository |
| Authoritative suppression hides a correct fact | fail-closed overlap proof (UUID+attribute+scope+kind/unit); demote when unproven; canary before broad authority |
| Agent inference wrongly made authoritative | evidence-tier gate: only user/manual/trusted-tool grounds authority; agent inference stays advisory |
| Entity resolution errors bind values to wrong series | view_subject_uuid is the only identity anchor; lexical signals never decide identity; confidence gate |
| Scope collapse merges distinct series | scope component in the key; carried from perception, not lexical re-derivation |

## 9. Open questions (for the implementation plan - needs a Menhir code read)
- Exact hook point in `services/perception.py` / `perceive_and_fold` for the typed-scalar proposal
  and UUID binding (after entity resolution).
- Storage for the durable typed-assertion/event log (new table/collection vs extending an existing
  provenance store) and its provenance links to TurnEvidence.
- `ViewRepository` change surface for `view_subject_uuid` keying without disturbing existing
  display-subject Views (migration/compat).
- Confidence signal + intent classifier for the authority gate (deterministic markers first:
  "previous", "used to", "more/less", "how many ... now").
- Whether `valid_at` capture is in the first contract or deferred to shadow-build learnings.

## 10. Next step after review
On approval, a Menhir-side **implementation plan** (separate doc) grounded in an actual read of
`services/perception.py`, `perceive_and_fold`, and `ViewRepository`: exact hook points, the durable
event-log store, the `view_subject_uuid` contract change, the ScalarStateView `ViewKind` +
materializer, the recall-composer authority gate (overlap proof + evidence tier), and the
shadow -> counterfactual -> canary rollout. No production code until that implementation plan is
itself reviewed.
