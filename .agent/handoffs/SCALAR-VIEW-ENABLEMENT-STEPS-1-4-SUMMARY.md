# ScalarStateView Enablement — Steps 1-4 Summary

**Date:** 2026-07-20
**Scope:** Menhir Piece C (ScalarStateView / typed-scalar memory) end-to-end enablement, steps 1-4 of a
7-step sequence.
**Verification:** archolith-bench `menhir-scalar-state` harness (live throwaway menhir + real LLM).
**Full plan:** `IdeaProjects/.agent/plans/menhir-scalar-view-enablement-sequence.md`

The View storage machinery was already proven (frozen, unit + live-DB tested). This work made the
first-person path actually produce Views end-to-end and measured exactly where coverage is lost.

Pipeline under test:
`named/self subject -> extraction -> episode provenance -> binding -> assertion log -> fold -> ScalarStateView`

---

## Step 1 — Close the ingestion/provenance defect  ✅

The e2e blocker was upstream of Piece C: Graphiti combined-extraction dropped a lone subject
("Zero-extraction success") and re-attributed prior entities to the latest episode, so typed-scalar
assertions had no entity to bind to.

- Fix (landed separately): menhir `bddf0fc` (close combined-extraction edge endpoints, detect collapse)
  + `7fe480c` (extraction receipt in the parent task, before the wait_for boundary).
- Verified in Bench: the per-call provenance matrix (`SS_DIAG=1`) is clean — each subject persists on
  its own episode, no cross-attribution; third-party fixture verdict PASS. `clock_time` View confirmed
  (`ss_value='07:30'`, `view_value=0.0` is the intended numeric mirror).

## Step 2 — First-person fixture  ✅

Reran the original `I`/`me`/`my`/`user` fixture. Verdict FAIL, but the RIGHT failure: assertions
emitted, `views_current=0`, all first-person assertions `binding_pending` with `unbound:*` subjects.
Graph proof: no `user` `:Entity` existed to bind to. This isolated the remaining blocker to canonical
self identity — not extraction, not View materialization.

## Step 3 — Canonical self entity  ✅  (menhir `478d11e`)

Added a stable per-namespace self identity so first-person assertions can bind.

- `ensure_self_entity(namespace)` — idempotent MERGE of ONE plain `:Entity` per namespace with a
  deterministic `uuid5` (`menhir-self:{namespace}`), marked `is_self`/`entity_role='self'`. Deterministic
  uuid => replay never forks and namespaces never collide; plain `:Entity` => the assertion write's
  `OPTIONAL MATCH (n:Entity {uuid})` check clears `binding_pending`. Not episode-linked (binding via the
  seam, not MENTIONS), so no per-episode `user` node is ever minted.
- `SELF_TOKENS` — exact allowlist `{user, the user, i, me, myself}`, never a `my <x>` prefix, so
  possessed objects (`my car`) and every named third party fall through to ordinary binding and can
  never bind to self. `_resolve_subject` prefers the injected self seam, else falls back byte-identically
  to `_bind_subject`. Wired into BOTH `bind_and_persist` and `repair_pending_bindings`, so pre-existing
  `user` advisories re-bind on the next repair pass with zero migration.

Verified live (first-person fixture): verdict FAIL->PASS, `views_current` 0->5,
`view_slots_committed` 0/9->3/9; all six first-person assertions bound to the one self entity,
`my car` correctly left advisory. Offline: 28 new tests; full typed-scalar + scalar-state suite 241 green.

## Step 4 — Per-kind, per-stage coverage matrix  ✅  (bench `73a6394`)

Instrument (`scripts/scalar_state_coverage.py`) measures the four stages SEPARATELY per value kind —
`assertion_emitted | subject_bound | view_materialized | fold_correct` — with a `drops_at` localizer and
kind-misclassification detection. 3-run aggregate:

| kind | emitted | bound | view | fold |
|------|:-:|:-:|:-:|:-:|
| count | 1 | 1 | 1 | 1 |
| money | 2 | 2 | 2 | 2 |
| measurement | 2 | 2 | 2 | 2 |
| duration | 3 | 3 | 3 | 3 |
| frequency | 1 | 1 | 1 | 1 |
| clock_time | 3 | 3 | 3 | 3 |
| weekday | 1 | 1 | 1 | 1 |
| status(car) | 3 | 0 | 0 | 0 |
| boolean | 0 | 0 | 0 | 0 |

**Decisive finding: past extraction, the pipeline is loss-less.** For every kind,
`bound == view_materialized == fold_correct == assertion_emitted`. Binding (step 3), the fold (C.2), and
View materialization each carry an emitted assertion through with zero additional loss. The entire
remaining coverage gap is at stage 1 (perceiver yield / extraction quality).

`status(car)` dropping at `subject_bound` is correct-by-design (possessed object, not self — the step 3
boundary), not a defect.

## Bonus — telemetry namespace isolation  ✅  (menhir `0517041`, `4baa478`)

While isolating budget contention: routine admission-audit "grant" nodes (one per user turn) were
flooding user namespaces. Now only denials/downgrades are recorded, and they go to the `agent-status`
telemetry silo, not the user namespace. Config self-perception was found already isolated in
`agent-status` (an earlier "leak" report was a namespace-blind query artifact).

---

## Net state

Steps 1-4 complete. The View pipeline works end-to-end for first-person and named subjects, and coverage
is now bottlenecked entirely at extraction (stage 1). Two concrete stage-1 problems are isolated for
step 5:

1. **Yield** — measurement / frequency / boolean intermittently or never emit an assertion.
2. **Kind misclassification** — weekday "day off is Wednesday" sometimes extracted as `status` (right
   value, wrong kind): a perceiver-prompt / kind-router issue, not fold or binding.

## Remaining sequence

- **5 — Perceiver yield + kind router** (Menhir): now the sole coverage bottleneck.
- **6 — Phase D counterfactual** (Menhir): offline baseline-vs-View-aware recall; no user-facing suppression.
- **7 — Current-state authority canary** (Menhir): narrow rollout where a current View may suppress an
  older overlapping graph fact.

## Commits

- menhir: `bddf0fc`, `7fe480c` (step 1) · `478d11e` (step 3) · `0517041`, `4baa478` (telemetry) —
  pushed to `Archolith/menhir` `main`.
- bench: `fe89416` (step-1 verification + auth fix) · `73a6394` (step-4 matrix) — pushed to
  `ctharvey/archolith-bench` `master`.
- plan: root `ef368fbf`.
