# Menhir ScalarStateView - Implementation Plan (code-grounded)

**Project:** menhir (`projects/archolith/menhir`)
**Date:** 2026-07-18
**Author:** Claude Code
**Status:** Rev 3 (2026-07-20) - Pieces A + B + **C COMPLETE and FROZEN** (see the per-sub-piece
freeze commits below); Piece D (recall authority) remains a separate, deferred effort not yet started.
**Piece C is SHADOW-BUILD-ONLY:** behind the `enable_scalar_state` flag it perceives typed scalars,
persists the durable assertion log, folds and materializes `scalar_state` Views, and keeps them
correct across merges/unmerges + crash-recovery repair — but NOTHING user-facing reads a
ScalarStateView for authority yet. Exposing them to recall is Piece D's job and is gated behind its
own staged shadow -> counterfactual -> canary validation. Turning the flag off leaves the numeric
counter path (`perception.py`) byte-identical.
**Final-gate freeze (C.4.4):** C.4.4.1 `98820f9` · C.4.4.2 `bc05a0c` · C.4.4.3 `bd9bbce` ·
C.4.4.4 `a180043` (head; based on `c0a1580`). The live-Neo4j gate — including 2-worker concurrency
idempotency for both the receiptless-unmerge and projection-pending View paths — passed against the
:7688 test instance.
**Status (Rev 2, 2026-07-18):** Pieces A + B APPROVED for offline implementation; Pieces C + D
revised per code-grounded review, ready for review. NO production code for C/D until re-approved.
**Design basis (approved Rev 2):** `menhir-scalar-state-view-design-plan.md`
**Bench evidence:** `archolith-bench/scripts/longmemeval/analysis/TYPED-VALUE-ARM.md`,
`oracle_entity_grouping_probe.py` (bench `8d3fb56`)
**Status note:** Pieces A + B are IMPLEMENTED + review-approved (menhir `main` commits `9c3996d`,
`3035349`); C + D are GREENLIT under the Rev 3 binding/merge/evidence rules below.
**Rev 2 corrections (review):** (1) NO Menhir resolver service exists - Graphiti resolves internally
in `add_episode()` and returns `graphiti_result.nodes`; Piece C binds against those (or via
`fetch_linked_entity_uuids_for_episode`), never by calling `resolve_extracted_nodes()` on a subject.
(2) Perception currently extracts only numeric countable events (`Event.value: float | None`; sum/
count/distinct_count) - Piece C needs a NEW typed-scalar proposal schema for the 9 kinds, not just a
sink. (3) `GateDecision` has no `valid_at` - use `latest(d.events).when`. (4) Event-log needs
idempotency keys, span grounding, perceiver-version replacement, delta de-dup, and latest-anchor-
plus-subsequent-deltas (not "exactly one anchor globally"). (5) Piece D hook named; untyped
candidates lack typed metadata so overlap is demote-only until proven. (6) Piece B hardening.
**Rev 3 corrections (A/B approval review):** (7) Bind after Menhir CORRELATION, not just Graphiti
resolution - `stamp_and_finalize()` runs a correlation/merge pass that can delete a resolved node;
use `fetch_linked_entity_uuids_for_episode(resolved_episode_uuid)` for the final identity (3.2).
(8) Later entity merges must rebind the event log + rematerialize under the survivor UUID, NOT rewrite
keys in place (which can collide on a shared slot) (3.2a). (9) Authority tier derives from the
persisted assertions, not the View's first-write receipt, since an unchanged-sig refresh does not
update audit/valid_at (3.5).

## 0. Code read - the seams this plan hooks into (verified)

| Seam | Location | What it gives us |
|------|----------|------------------|
| Perception boundary | `src/menhir/services/perception.py:1286` `perceive_and_fold()` | k-sample extract -> `gate()` -> commit XOR abstain; committed groups fold via the deterministic sink; abstain = NO-OP; stamps an audit receipt; invariant "model totals are gate inputs, NEVER stored on a View" |
| Deterministic sink | `src/menhir/services/event_fold.py:33` `fold_events_to_counter()` | folds `list[Event]` with a scalar reducer -> `record_counter(subject, counter, value, valid_at, episode_uuids, source, name_embedding, audit)`; `valid_at = latest(events).when`; batch re-fold = absolute value (Law-2 safe). NOTE: reducers are numeric only (sum/count/distinct_count) |
| Timeline sink | `event_fold.py:73` `fold_events_to_timeline()` | lossless dated event list View (history surface) |
| Event model | `perception.py` `Event(when, kind, value: float \| None, identity, what, episode_uuid)` | numeric-only today; the 9 ValueKinds need a new typed-scalar schema (Piece C). Valid time = per-event `when`; `GateDecision` has NO `valid_at` field |
| Entity resolution | Graphiti-internal `resolve_extracted_nodes()` in `add_episode()`; results returned as `graphiti_result.nodes`, consumed by Menhir `stamp_and_finalize()` | there is NO Menhir `resolve(subject)->uuid` service; Piece C binds against the already-resolved `graphiti_result.nodes` for that episode (or `fetch_linked_entity_uuids_for_episode(episode_uuid)`), never by calling Graphiti resolution on a subject |
| View kind contract | `src/menhir/infrastructure/view_repository.py:~95` `ViewKind` (abstract) | `key_discriminator`, `signature`, `surface`, `write_props`, `parse`, `read_fields`, `lww_register`, `valid_at`, `episode_uuids` |
| Existing scalar kind | `view_repository.py:154` `CounterKind` (`lww_register=True`) | proves a scalar (subject, counter)->value register with LWW supersession already exists |
| Upsert + keying | `view_repository.py:331` `record()`, `:316` `_key()`, `:377` `_write_version()` | key = `ns::subject.lower()::discriminator`; `sig` idempotency (equal=refresh, diff=new version + SUPERSEDES); `require_newer` LWW guard (temporally-older `valid_at` never overwrites = fold-algebra Law 1); FACT/METRIC class; `source`/`source_confidence`; `audit_props` receipts |

**Consequence:** most of the design already exists as machinery. The four new/changed things are:
(A) a `ScalarStateView` `ViewKind`, (B) a `view_subject_uuid` keying contract change on
`record()`/`_key`, (C) a perception extension that proposes typed assertions + binds them to the
resolved entity UUID + persists a durable event log, (D) a recall-composer authority gate. The
supersession/recency and history semantics are REUSED, not rebuilt.

## 1. Piece A - `ScalarStateView` ViewKind (new)

New `ViewKind` subclass in `view_repository.py` (registered in `self.KINDS`), name `scalar_state`,
`lww_register=True` (a current-value register; newer `valid_at` wins, fold-algebra Law 1 - the same
mechanism `CounterKind` uses and exactly what the bench "latest-learned current" needs).

- `key_discriminator(payload)` -> `f"{attribute}:{scope}:{value_kind}:{unit}"` (the per-entity slot;
  the entity UUID is the subject segment via Piece B). Distinct attribute/scope/kind/unit never
  supersede each other - encodes the bench "MCU vs all films", "owned vs sold" separations.
- `signature(payload)` -> the normalized current value (supersede on value change), like `CounterKind`.
- `surface(subject_display, payload)` -> `(name, summary)` recall text using the DISPLAY subject
  (e.g. "wake time on Saturdays: 7:30 am"), never the UUID.
- `write_props` -> `view_value` (normalized), `ss_kind` (the ValueKind), `ss_unit`, `ss_scope`,
  `ss_attribute`, `ss_display` (raw display string for advisory candidate rendering).
- `read_fields` / `parse` -> mirror `CounterKind`.
- Covers the 9 `ValueKind`s (boolean, status, count, duration, frequency, money, measurement,
  clock_time, weekday) as REGISTER (latest-value) state. Numeric counters that need SUM/COUNT stay
  on `CounterKind`; ScalarStateView is the register (current-value) semantics.

## 2. Piece B - `view_subject_uuid` keying contract change (repository)

The review's core fix. Today `_key(namespace, subject, discriminator)` keys on subject TEXT
(`view_repository.py:316`) and `view_subject` stores the display (`:452`).

Change (backward-compatible):
- `record(..., subject: str, subject_uuid: str | None = None, **payload)` - add an OPTIONAL
  `subject_uuid`. When present, `_key` uses `subject_uuid` as the identity segment; when absent,
  it falls back to `subject.strip().lower()` exactly as today (every existing kind unchanged).
- `_key(namespace, key_subject, discriminator)` where `key_subject = subject_uuid or subject.lower()`.
- `_write_version` stores BOTH: `view_subject` = display (unchanged, `:452`) AND a new
  `view_subject_uuid` prop for identity/debug. Display text keeps driving `surface`/embedding.
- **Never pass the UUID as `subject`** - that would corrupt `surface()`/summary/retrieval text.

Migration/compat: existing counter/timeline/admission Views pass no `subject_uuid` -> identical
keys, zero migration. Only `scalar_state` writes supply `subject_uuid`. `_current_by_key` already
matches on `view_key`, so no read change.

### 2.1 Hardening (review requirements)
- **Reject blank `subject_uuid`.** Normalize (strip); a present-but-blank/whitespace `subject_uuid`
  raises (a scalar-state write MUST have a real UUID identity - never silently fall back to text
  keying, which would recreate the lexical sidecar).
- **Index `view_subject_uuid`.** Add a Neo4j index on the new prop (its own constraint/index in the
  schema-init path alongside the existing `view_key`/`qs_key` indexes) so per-entity lookups and the
  overlap proof (Piece D) are not scans.
- **Canonical discriminator, not raw colon concat.** Build the per-entity slot from a canonically
  serialized structure (sorted, escaped) or a stable hash of `{attribute, scope, value_kind, unit}`,
  not `f"{a}:{b}:{c}:{d}"` - so a value/scope containing `:` cannot collide keys.
- **Keep normalized slot components as node properties** (`ss_attribute`, `ss_scope`, `ss_kind`,
  `ss_unit`) for inspection/debug even though the key is hashed/canonical.

## 3. Piece C - typed-scalar perception schema, post-Graphiti binding, event log (REVISED)

Piece C is NOT merely an extra sink on committed counter groups. Perception today extracts only
numeric countable events (`Event.value: float | None`; reducers sum/count/distinct_count), so the 9
ValueKinds (boolean, status, clock_time, weekday, duration, frequency, money, measurement, count)
require a **new typed-scalar proposal schema inside the perception boundary**. And binding uses the
entities Graphiti ALREADY resolved, not a later re-resolution.

### 3.1 Typed-scalar proposal schema (new, inside the perception boundary)
A `TypedScalarProposal` produced by an extraction pass (a distinct prompt + parser from the numeric
count extractor, gated by the same k-sample consistency machinery):
`{attribute, scope, value_kind, unit, operation (absolute|delta), value (kind-typed, not float-only),
display, stated_span (source quote/offsets), subject_text}`. It runs behind an opt-in flag
`enable_scalar_state` (mirroring the existing `enable_*` levers); off -> byte-identical to today.
The gate, abstention NO-OP, and "model totals never stored" invariant are preserved.

### 3.2 Post-CORRELATION entity binding (identity anchor) - CORRECTED (Rev 3)
There is NO Menhir `resolve(subject)->uuid` service; Graphiti resolves entities internally in
`add_episode()` and returns `graphiti_result.nodes`. **But `graphiti_result.nodes` is NOT the final
identity:** `stamp_and_finalize()` then runs Menhir's correlation/merge pass, which can absorb and
delete one of those resolved nodes (the merge path rebinds the episode's MENTIONS edges onto the
survivor before deleting the absorbed entity). So bind against the **final post-finalization episode
provenance**, not the raw Graphiti nodes, and do NOT call `resolve_extracted_nodes()` on a subject:
- **Bind via final provenance:** after finalization, get the episode's surviving entities with
  `fetch_linked_entity_uuids_for_episode(resolved_episode_uuid)` - this reflects merges (survivor,
  not absorbed) and is the correct Menhir identity for that ingest.
- Runs inline post-finalization (preferred) or in the scheduled recovery pass over the same query.

Binding rule (fail-closed): match `subject_text` against the final linked entities; **abstain from
authority when zero OR multiple candidates remain.** An unresolved assertion MAY persist in the
event log as advisory evidence, but MUST NOT create a display-text-keyed ScalarStateView (that would
recreate the rejected lexical sidecar). Only a uniquely bound `subject_uuid` yields a scalar_state
View.

### 3.2a Later entity merges (event-log rebinding, not key rewrite) - Rev 3
A future merge deletes the absorbed entity WITHOUT rewriting existing `view_subject_uuid` props or
scalar View keys, so scalar state would orphan under a dead UUID. Handle via **merge-triggered
event-log rebinding + View rematerialization under the survivor UUID** (preferred over rewriting the
key in place, which can COLLIDE when both entities already hold the same scalar slot). On an
`ENTITY_MERGE`: rebind the absorbed entity's `:TypedAssertion` events to the survivor `subject_uuid`,
then `rebuild_scalar_state(survivor_uuid)` folds the union deterministically (dedup by the idempotency
key; the latest-anchor + post-anchor-delta semantics resolve overlapping slots). Canonical-UUID
resolution through merge lineage at read time is the fallback if a rebind is deferred.

### 3.3 Durable typed-assertion event log (fixes exact-rebuild; explicit rules)
Perception is probabilistic, so the View is rebuilt from a persisted event log, never by
re-perception. Persist each accepted assertion as a `:TypedAssertion` node linked to its episode(s)
(reuse the FACT episode-provenance edge pattern in `_write_version`, `view_repository.py:~458`):
`{view_subject_uuid, view_subject_display, attribute, scope, value, unit, value_kind, operation,
valid_at, learned_at, evidence_tier, perceiver_version, stated_span, provenance:[episode_uuid...]}`.
Explicit rules (review):
- **Deterministic idempotency key** per assertion = hash of `{subject_uuid, attribute, scope,
  value_kind, unit, operation, value, stated_span, episode_uuid}`. Re-running perception over the
  same episode is a no-op (same key); this **prevents repeated batch runs from duplicating delta
  events**.
- **Source-span grounding required:** every persisted assertion carries `stated_span` (the source
  quote/offsets); an assertion without a grounded span is not persisted.
- **Perceiver-version replacement:** a higher `perceiver_version` for the same
  `{subject_uuid, attribute, scope, kind, unit, episode_uuid}` SUPERSEDES the older event
  (non-destructive; old kept, marked superseded) - a deliberate versioned revision, not silent
  mutation.
- **Rebuild:** `rebuild_scalar_state(subject_uuid)` reads the current (non-superseded) event log for
  a key and re-runs the deterministic fold -> identical View.

### 3.4 Fold semantics: latest anchor + subsequent deltas (CORRECTED)
"Exactly one anchor globally" is too restrictive - real histories carry several legitimate absolute
updates ("I have 37", later "I have 40"). Fold per `(subject_uuid, attribute, scope, kind, unit)`:
1. select the **latest authoritative absolute anchor** by `valid_at` (register/LWW - reuse the
   `require_newer` guard, `_write_version:442`);
2. apply **only deltas with `valid_at` strictly after that anchor** (`sum` reducer over those
   deltas);
3. current value = anchor + post-anchor deltas. Deltas before the latest anchor are already
   subsumed by that stated absolute and are NOT re-applied.
`fold_events_to_scalar_state()` (new sink modeled on `fold_events_to_counter`, `event_fold.py:33`)
calls `record("scalar_state", subject=display, subject_uuid=uuid, ...)`. Reuse
`fold_events_to_timeline` (`event_fold.py:73`) for the history/candidate surface (advisory).

### 3.5 Authority evidence comes from the event log, not the View's first write - Rev 3
`_write_version` refreshes provenance on an UNCHANGED signature but does NOT update `audit_props` or
`valid_at`. So if authority depended only on the View's original evidence tier, a later USER
confirmation of an agent-proposed value (same value -> unchanged sig -> refresh-only) would leave the
View advisory forever. Fix: **derive the current value's authority tier from its persisted
assertions**, not the View's first-write receipt. The fold computes the best evidence tier among the
assertions supporting the current value and the composer reads THAT. Implementation: either (a) the
composer resolves the tier from the event log at read time, or (b) a **monotonic same-value refresh**
that may UPGRADE the stamped tier (agent -> trusted_tool -> manual -> user) but NEVER silently
downgrade it. Prefer (a) (single source of truth = the event log); (b) is the optimization if the
read-time lookup is too costly.

## 4. Piece D - recall composer authority gate (hook named; demote-only for untyped) (REVISED)

### 4.1 Concrete hook
The authority composer runs AFTER scoring/frontier processing but BEFORE the top-k slice, where
recall currently does:
```
top_results = pending_fallback + scored[:remaining_limit]
```
The composer consumes `scored` (with scalar_state matches identified by `view_subject_uuid`),
applies the intent gate + overlap proof, then produces the final ordering fed to the slice.

### 4.2 Authority vs advisory
- **Confident current-state query** (scalar_state match on a resolved `view_subject_uuid` +
  unambiguous current value + `evidence_tier in {user, manual, trusted_tool}`): emit the current
  value AND suppress overlapping untyped facts - but ONLY on a PROVEN overlap (4.3). Else DEMOTE.
- **Previous-value / comparison / history / uncertain intent** OR `evidence_tier == agent`: emit the
  scalar_state history as ADVISORY candidates; the answer model reasons (bench `71315a70`,
  `e66b632c`).

### 4.3 Overlap proof is demote-only for untyped candidates (CORRECTED)
Ordinary Graphiti candidates do NOT inherently carry `attribute`, `scope`, `value_kind`, `unit`, so
the full overlap proof (same `view_subject_uuid` + attribute/state-family + compatible scope +
kind/unit) usually cannot be PROVEN against an untyped snippet. **Until those fields can be proven
for an untyped candidate, the fail-closed contract permits DEMOTION only, never suppression.**
Suppression is reserved for candidates whose typed metadata is available and fully matches; every
other overlap is a demotion. This structurally prevents the v3 failure of deleting a
correct-but-untyped fact.

### 4.4 Intent classifier (deterministic first)
Deterministic markers before any model call: "previous"/"used to"/"before" -> history/advisory;
"more"/"less"/"than" -> comparison/advisory; "how many ... now"/"current"/"currently" ->
current-state/authoritative-eligible. Ambiguous -> advisory (fail-safe).

## 5. Validation staging (gated rollout - no user impact until canary)
1. **Shadow-build:** materialize `scalar_state` Views + the event log from live ingestion with the
   recall composer NOT consuming them. Verify: rebuild determinism (`rebuild_scalar_state` == live
   fold), provenance links, `view_subject_uuid`/display split intact, zero effect on existing Views.
2. **Counterfactual recall:** offline, compare View-composed context vs current recall on the bench
   KU fixture + a sample of real namespaces. No user impact. Confirm the 6 oracle-probe cases
   improve and the 2 reasoning cases stay advisory (no regression).
3. **Current-state-only canary:** enable authoritative suppression for confident, grounded,
   proven-overlap current-state queries ONLY, measured, before any broader authority.

## 6. Test plan (new tests; existing untouched)
- `ScalarStateView` kind: key_discriminator isolation (attribute/scope/kind/unit), signature
  supersession on value change, LWW temporally-older skip, surface uses display not UUID.
- `record()` contract: `subject_uuid` drives the key; absent -> identical legacy key (compat);
  `view_subject` display unchanged; UUID never in surface text.
- Perception extension: opt-in flag off -> byte-identical to today; on -> a resolved typed-scalar
  proposal yields a persisted TypedAssertion + scalar_state fold; abstention still NO-OP.
- Post-correlation binding: unique surviving entity -> bound `subject_uuid`; zero OR multiple ->
  abstain from authority (advisory-only event-log entry, NO display-keyed View). Binding uses
  `fetch_linked_entity_uuids_for_episode` (post-finalization), so a merged-away resolved node is not
  used as identity.
- Merge rebinding: after an ENTITY_MERGE, absorbed events rebind to the survivor `subject_uuid` and
  `rebuild_scalar_state(survivor)` folds the union; no orphaned View under a dead UUID; a shared slot
  dedups (no key collision).
- Event log: deterministic idempotency key makes re-runs no-ops (no duplicate deltas); missing
  `stated_span` -> not persisted; higher `perceiver_version` supersedes same-key event.
- Fold semantics: latest absolute anchor + only strictly-later deltas; pre-anchor deltas not
  re-applied; multiple legitimate absolutes over time each supersede by `valid_at`.
- Durable log + rebuild: `rebuild_scalar_state` reproduces the live View exactly; re-perception
  appends (versioned), never mutates.
- Authority tier from the event log: a later USER confirmation of an agent-proposed value (same value,
  unchanged sig) UPGRADES the effective tier to authoritative; the tier never silently downgrades.
- Recall authority: full typed-overlap match suppresses; untyped candidate overlap DEMOTES only;
  agent-tier stays advisory; previous/comparison intent stays advisory.

## 7. Non-goals (first implementation)
Same as the design: no new extractor (extend the perception boundary), no Graphiti redesign, no
general temporal-reasoning engine, no arithmetic/filtered-count/coref engine beyond the delta fold,
no parallel truth store. Scalar-state is an additional sink + a register ViewKind + an event log.

## 8. Remaining code reads before coding C/D (A/B done; do these first in the C/D session)
1. `perception.py` `Episode`/`Event`/`GateDecision` dataclasses (per-event `when`/`episode_uuid`;
   confirm `GateDecision` has no `valid_at` so the fold uses `latest(d.events).when`).
2. `stamp_and_finalize()` and the correlation/merge pass it runs AFTER `graphiti_result.nodes`, plus
   `fetch_linked_entity_uuids_for_episode(resolved_episode_uuid)` - to pin the post-correlation
   binding point (3.2) and confirm the merge path rebinds MENTIONS onto the survivor.
3. The `ENTITY_MERGE` saga (workspace merge/delete lifecycle) - the hook to trigger event-log
   rebinding + `rebuild_scalar_state(survivor)` (3.2a).
4. `graph_adapter.record_counter` -> `ViewRepository.record` adapter to mirror for
   `record_scalar_state` (uses the `subject_uuid` param from Piece B, already shipped).
5. The episode-provenance edge write in `_write_version` (`view_repository.py`) to reuse for
   `:TypedAssertion` provenance.
6. The recall path around `top_results = pending_fallback + scored[:remaining_limit]` for the Piece D
   pre-top-k hook and what typed metadata `scored` candidates carry.
7. `.agent/` in `menhir` for project conventions (per menhir CLAUDE.md) before any C/D edit.

## 9. Risks & mitigations (implementation-specific)
| Risk | Mitigation |
|------|-----------|
| `subject_uuid` change breaks existing View keys | optional param; absent -> byte-identical legacy key; only scalar_state supplies it; test compat explicitly |
| Perception extension changes existing counter behavior | opt-in `enable_scalar_state` flag; off = byte-identical; additional sink only, gate unchanged |
| Probabilistic rebuild drift | rebuild from the persisted event log, never re-perception; `perceiver_version` stamps; determinism test |
| Wrong entity binding | bind via post-correlation `fetch_linked_entity_uuids_for_episode` (survivor, not absorbed); zero/multiple -> advisory-only, NO display-keyed View |
| Stale identity after a later merge | merge-triggered event-log rebind + `rebuild_scalar_state(survivor)`; never key-rewrite-in-place (collision) |
| Authority stuck advisory after user confirms | tier derived from the event log (or monotonic same-value upgrade); never silent downgrade |
| Over-suppression | fail-closed overlap proof + evidence-tier gate + canary; demote-not-remove default |
| LWW installs stale current | reuse `require_newer` guard (`_write_version`) - already fold-algebra Law 1 correct |

## 10. Deliverable order

- **A (ViewKind) + B (keying)** - DONE (menhir `9c3996d`, `3035349`). Self-contained, offline.
- **C - split into FOUR reviewable commits** (deterministic core first; merge-rebinding isolated so
  the sensitive merge lifecycle has its own review/rollback boundary):
  1. `feat(scalar-state): add durable typed-assertion repository` - `:TypedAssertion` schema +
     indexes, absolute/delta model, deterministic idempotency key, source span, evidence tier,
     perceiver version, learned/valid time, episode + entity provenance, repository CRUD/query tests.
     NO View writes. Invariant: **accepted assertions are durable and idempotent.**
  2. `feat(scalar-state): add deterministic fold and rebuild` - `fold_events_to_scalar_state`,
     latest-anchor + subsequent-deltas, `record_scalar_state` adapter, `rebuild_scalar_state`,
     replay/idempotency tests, ambiguous-anchor abstention, live-fold vs rebuild parity. Invariant:
     **durable assertions deterministically produce the correct View.**
  3. `feat(scalar-state): rebind assertions across entity merges` - DONE / FROZEN (final rework
     `ff61dee`). Merge saga is UNTOUCHED (it is fingerprinted); rebinding hangs off a best-effort
     post-COMMIT hook in CorrelationService (`on_merge_committed`) and the inverse off
     UnmergeCoordinator (`on_unmerge_committed`), injected only when the feature is on. Identity is
     source_key-anchored (evolved through review): the head MERGEs atomically on the BINDING-STABLE
     `source_key` (episode+span+ordinal, DB-unique, no subject_uuid) so concurrent first writes and
     post-merge re-perception converge on ONE head; `assertion_key` is built from `source_key` (not
     claim_key) so a re-confirmation after a rebind is idempotent. `TypedAssertionRepository`:
     `rebind_assertions` (move surviving `subject_uuid` onto survivor + re-link HAS_ASSERTION,
     journaled per-op via DB-unique `:AssertionRebind{rebind_key}`; head moves scoped to the moved
     source_keys), `restore_rebound_assertions` (op-scoped unmerge inverse), dual-direction
     `:ScalarReconcile` receipts keyed by each op's OWN id + kind (ENTITY_MERGE|ENTITY_UNMERGE),
     `orphaned_subject_uuids` (crash-repair input). `ScalarStateService.handle_merge` (retire
     absorbed Views -> rebind -> rebuild survivor; receipt written LAST so a rebind-ok/rebuild-failed
     merge is still found receiptless) + `handle_unmerge` + `repair_incomplete_reconciliations`
     (repairs any committed merge OR unmerge lacking a receipt of its own kind). MIGRATION (final at
     `f21ea32`): because the source_key identity contract has no in-place migration to/from any other
     version, activation is EXACT-MATCH FRESH-ONLY: every node is stamped
     `identity_version = IDENTITY_VERSION` (ON CREATE only); `incompatible_identity_nodes_exist()`
     counts anything unstamped OR `<> IDENTITY_VERSION` (older AND newer both fail closed, so a
     rolled-back binary meeting future v3 nodes refuses too); the source_key identity DDL is moved
     OUT of the unconditional bootstrap into a gated `activate_scalar_state()` that refuses
     (`ScalarStateActivationError`) unless the store matches exactly. `purge_scalar_state_nodes()` is
     the explicit dev/operator escape hatch and deletes the WHOLE footprint — event log + lifecycle
     nodes AND every materialized `kind='scalar_state'` View (all versions), so no stale projection
     survives a reset. The in-place `backfill_head_source_keys()` migration was removed (it was the
     mixed-identity path the gate forbids). Tested (offline, FakeNeo4j + stateful fakes): merge chain
     A->B->C, duplicate-slot resolution, idempotent replay, unmerge round-trip, hook best-effort
     failure, dual-direction repair, exact-match refusal over BOTH a legacy head+assertion and
     newer-v3-stamped nodes, full-footprint purge. FOLLOW-UP (in/after C.4 when the feature wires
     up): scheduled orphan-rebind repair pass; transitive orphan repair across merge chains (A->B
     failed but B->C succeeded must replay downstream B->C reconciliation despite its receipt); and
     the live-Neo4j merge->rebind->unmerge->restore integration test (uniqueness constraints active,
     record concurrency) as the final integration gate. Invariant: **entity identity remains correct
     across lifecycle changes.**
  4. `feat(perception): extract and persist typed scalar assertions` - nine-kind proposal schema +
     parser, span/grounding validation, post-finalization binding via episode-linked entity UUIDs,
     evidence-tier assignment, `enable_scalar_state` flag, unresolved/ambiguous -> advisory-only (no
     View), flag-off parity + abstention-noop integration tests. ACTIVATION ORDERING (from C.3
     review): when the flag is on, `activate_scalar_state()` MUST run and pass BEFORE perception
     workers can record assertions, before the merge/unmerge hooks are installed, and before repair
     jobs start. Call the gate even when the required indexes already appear online (do not skip on a
     `scalar_state_schema_ready()` short-circuit), then verify `scalar_state_schema_ready()` AFTER
     activation — so a rollback or manually altered DB cannot bypass the identity-version check.
     Invariant: **probabilistic perception may feed the proven deterministic system.**

     C.4 is itself split into reviewable sub-commits (mirroring the C.1-C.4 discipline), each frozen
     before the next; existing perception (`perception.py`, the counter path) stays byte-identical
     when `enable_scalar_state` is off:
     - **C.4.1 - typed-scalar proposal schema + prompt + deterministic parser** (self-contained,
       offline; NEW module `services/typed_scalar_perception.py`, `perception.py` untouched). A
       `TypedScalarProposal` value object over the 9 ValueKinds, a distinct extraction prompt, and a
       pure parser that maps LLM JSON -> validated proposals: kind-typed value coercion via the
       domain validators (VALUE_KINDS/OPERATIONS/validate_value/normalize_scalar), fail-closed drop
       of malformed/ungrounded proposals, UNIQUE source-span grounding (the quote must occur exactly
       once in the episode text -> located char offsets; zero/multiple -> dropped), so identity is
       order-independent (claim_ordinal always 0; same-span proposals share a source_key as competing
       interpretations). Strict full-string ISO `when` parsing (malformed -> dropped, absent -> None);
       finite-number + real-clock validation in the domain validator (oversized ints that would
       `OverflowError` in `math.isfinite(float(x))` fail closed to `ValueError` via
       `_is_finite_number`, so one bad row never aborts the pass); clock_time canonicalized to
       zero-padded `HH:MM` (so `7:30`/`07:30` are one value, not C.4.2 vote scatter); scope/unit
       deterministically canonicalized. Shared `build_source_key` prevents proposal/assertion identity
       drift. No gate,
       no persistence, no binding. LLM injected -> fully offline-testable. Invariant: **a proposal is
       well-typed, grounded, and deterministically identified or it does not exist.**
     - **C.4.2 - k-sample typed-scalar consistency gate** - DONE (menhir `fd162cd`, offline).
       SOURCE-CLAIM-FIRST voting (C.4.1
       review): the earlier flat tuple `(attribute, scope, value_kind, unit, operation,
       normalized_value)` omits source episode/span, subject, and effective date, so it would collapse
       UNRELATED unbound proposals before binding resolves identity (e.g. "Alice owns 2 cats" and
       "Bob owns 2 cats" share the tuple). Instead vote in two levels:
       `source_key = build_source_key(episode_uuid, span offsets, ordinal)` groups the k samples by
       the SAME source statement; then within a group vote on
       `interpretation_key = normalized subject_text + attribute + scope + value_kind + unit +
       operation + normalized_value + normalized/resolved when`. A sample that OMITS a source claim
       counts AGAINST agreement (an absent vote in the denominator), not as a vanished row. AT MOST
       ONE VOTE PER SAMPLE PER source_key (C.4.1 review): since C.4.1 deliberately lets two
       interpretations from one sample share a source_key, a sample emitting two DIFFERENT
       interpretations for the same source claim is internally conflicted and must contribute NO vote
       (treated as an abstaining/conflicted sample), never two; the denominator stays the configured
       k (including complete omissions). Abstain on scatter; retain a span-grounding veto as
       defense-in-depth; evidence-tier assignment. Committed/abstained decisions, no persistence.
       IMPLEMENTED as `gate_typed_scalars` in `typed_scalar_perception.py`: returns one
       `TypedScalarDecision` per source claim (first-seen order, deterministic) carrying the
       representative proposal (first-seen for the winning interpretation, so 37 vs 37.0 commit the
       first typed value) + `evidence_tier='agent'` (perception is always the lowest tier; the fold's
       effective tier is still the weakest required contributor at read time, never trusted here).
       14 offline gate tests incl. omission-counts-against, conflicted-sample-casts-no-vote,
       subject/when participate in the vote, threshold, determinism/value-type preservation,
       ungrounded defense-in-depth veto, and an end-to-end clock-canonicalization-prevents-scatter
       check through the C.4.1 extractor.
     - **C.4.3 - binding + persistence + rebuild + `enable_scalar_state` flag** - DONE (menhir
       `69d3b08`, offline). Post-finalization binding via a NEW name-carrying
       `fetch_linked_entities_for_episode` (survivor entities as {uuid, name}; the existing
       uuid-only variant cannot match the extracted subject_text) — fail-closed: UNIQUE normalized
       name match -> bound `subject_uuid` (materializes a View); ZERO or MULTIPLE -> a deterministic
       per-source-claim `unbound:<source_key>` sentinel subject the store flags `binding_pending`, so
       NO display-keyed View is built (a later orphan-rebind pass, C.4.4, resolves it). Time-basis
       selection: explicit stated `when` -> `episode_reference` -> `learned_fallback`. Committed
       proposals persist as `:TypedAssertion` (C.1 repo, `record_typed_assertion` delegate) then
       `rebuild_scalar_state` (never rebuilt for a `binding_pending` row). ACTIVATION ORDERING
       enforced by `ensure_scalar_state_activated`: ALWAYS calls `activate_scalar_state()` (no
       schema_ready short-circuit, so a rollback/hand-altered DB still hits the identity-version
       gate), then verifies `scalar_state_schema_ready()` AFTER and fails closed
       (`ScalarStateNotActivatedError`) before recording; refusal over a legacy store
       (`ScalarStateActivationError`) records nothing. Wired into `consolidate_personal_memory` via
       `enable_scalar_state` (default OFF -> counter path byte-identical); activation refusal there
       disables ONLY the typed-scalar shadow path, never the counter consolidation. 20 offline tests
       (`test_typed_scalar_bind_persist.py`): unique/zero/multiple binding, intended-bound-but-store-
       pending, abstain no-op, idempotent re-perception, time basis, activation-runs-before-record,
       activate-once, legacy-refuse and unready-schema record nothing. Live Neo4j NOT available:
       uniqueness constraints, MERGE atomicity, and real concurrency remain the C.4.4 gate.
       - **C.4.3 REVIEW FIXES** - DONE (menhir `edf1121`, CI green). Seven blockers from the user's
         review, all with offline coverage (full unit suite 1996 passed):
         1. Gate/coordinator input validation: `gate_typed_scalars` rejects a non-finite/out-of-(0,1]
            `threshold` (NaN/zero/negative no longer fail open via `agreement < NaN`) and commits ONLY
            a UNIQUE modal interpretation (`TS_VETO_TIE` abstains a 2-2 split even when it meets
            threshold); the coordinator requires a real integer `k >= 1` (bool/zero/negative fail
            closed rather than silently collapsing to one sample).
         2. Pending->bound adoption in the durable store (`_RECORD_CYPHER` ON MATCH): a claim first
            written as an unbindable advisory that later becomes uniquely bindable and is re-perceived
            lands on the SAME node (assertion_key omits subject_uuid); the write now adopts the
            resolved `subject_uuid`/`subject_display`/`claim_key` onto the assertion AND its head, so
            the subject_uuid-keyed fold can retrieve it. (The bug: previously ON MATCH updated only the
            evidence tier, so the node kept the `unbound:` sentinel while `binding_pending` cleared.)
            Live MERGE semantics remain the C.4.4 Neo4j gate — the offline test guards the clause.
         3. Binding candidates exclude derived Views: `fetch_linked_entities_for_episode` filters
            `is_view`/`is_quantstate`/`view_kind` in Cypher AND Python, so a scalar assertion can never
            bind to a counter/scalar View `:Entity` (the scheduler writes counter Views first).
         4. Independent scalar watermark: new `:ScalarConsolidationWatermark` +
            `list_scalar_dirty_namespaces`/`mark_scalar_consolidated`; the shadow path is now a
            SEPARATE pass over scalar-dirty namespaces (marked only after the sink actually ran), so
            enabling the feature backfills every namespace and an activation-refused run stays
            scalar-dirty for retry. Flag-off leaves the result dict byte-identical (no scalar_* keys);
            flag-on is explicitly additive.
         5. Authority hardening: `bind_and_persist_typed_scalars` FORCES `PERCEPTION_EVIDENCE_TIER`,
            never trusting a decision's `evidence_tier`.
       Carried to C.4.4: a LIVE-Neo4j regression for the pending->bound transition
       (unbound write -> entity becomes bindable -> re-record -> one node, real subject_uuid,
       binding_pending=false, materializable query returns it, View rebuild succeeds) — offline fakes
       cannot execute the MERGE. Also: an explicit pending-binding repair pass (advisory rows are
       excluded from `orphaned_subject_uuids`, so they rely on ordinary re-perception to bind); and a
       LIVE already-bound-mismatch negative case (same assertion_key presented with a DIFFERENT entity
       B must NOT add a second `B-[:HAS_ASSERTION]` while leaving `a.subject_uuid=A` — force an
       explicit fail-closed policy for that mismatch, per the 2nd review's C.4.4 note).
       - **C.4.3 PRODUCTION-INTEGRATION FIXES** - DONE (menhir `993297b`, CI green). Three blockers
         from the 2nd review, all offline-covered (full unit suite 2003 passed):
         1. Scheduler wiring: the feature was dead in the scheduled job (`MaintenanceScheduler` never
            passed the flag). Added `scalar_state_enabled` + `scalar_state_perceiver_version` fields,
            forwarded through `_make_consolidate_personal_memory`, sourced in `runtime.py` from two new
            settings (`personal_memory_scalar_state_enabled` / `_perceiver_version`, env
            `MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED` / `_PERCEIVER_VERSION`). Tested at the
            factory, not only the task function.
         2. `purge_scalar_state_nodes` now also deletes `:ScalarConsolidationWatermark` (returned in
            the counts) — a purge no longer leaves a stale cursor that makes a fresh namespace look
            consolidated and skip backfill.
         3. Truncation-safe, version-aware backfill cursor: `:ScalarConsolidationWatermark` stores a
            resumable `(cursor_at, cursor_uuid, perceiver_version)` cursor, NOT a "last run at" stamp.
            `list_scalar_dirty_namespaces` is existence-by-cursor; `load_next_scalar_batch` pages AFTER
            the cursor and resets on a version change; `advance_scalar_cursor` moves through only
            processed episodes. The scheduler walks a namespace page-by-page (`scalar_batch_size`,
            default 500) honoring the LLM budget, so a >500-episode namespace never strands its tail,
            a budget-capped run resumes exactly where it stopped, and a perceiver_version bump revisits
            history so a newer perceiver can correct prior claims.
       - **C.4.3 CURSOR-CORRECTNESS FIXES** - DONE (menhir `6268f8b`, CI green). The round-2 cursor
         conflated monotonic work-discovery identity with world-time; two bugs from the 3rd review,
         both offline-covered with the 5 required regressions (full unit suite 2008 passed):
         1. World-time cursor stranded late-finalized episodes. Ordering on
            `coalesce(valid_at, created_at)` (world-time first) meant an episode finalizing later but
            carrying an earlier reference date sorted BEHIND a passed cursor and was never discovered.
            FIXED: the cursor key is now `cursor_at = coalesce(created_at, valid_at)` — INGESTION order
            — in both the dirty check and the loader. Stored property renamed `cursor_valid_at` ->
            `cursor_at`. PRECISION: `created_at` is captured at Graphiti operation START, not at
            finalization; native episodes become visible in `created_at` order because Graphiti
            persistence is serialized per namespace by the ingest lock (the scalar cursor is likewise
            namespace-scoped), so within one namespace the next episode cannot get a later `created_at`
            until the prior one is persisted. The cursor is safe by that serialization + save ordering,
            NOT because `created_at` is itself a completion timestamp. Out-of-order imports or writers
            that bypass `IngestGate` would need the optional processing-receipt cursor (C.4.4).
         2. Cursor timestamp reused as semantic `valid_at`. The loader returned the coalesced ordering
            key as `valid_at`, so an episode with no world-time but a `created_at` persisted with
            `time_basis=episode_reference` (a bogus ingestion timestamp) instead of `learned_fallback`.
            FIXED: the loader returns `cursor_at` (monotonic key) and `valid_at` (GENUINE world-time,
            possibly null) as SEPARATE fields; the scheduler advances on `cursor_at` and passes only
            genuine `valid_at` to `episode_reference_time`.
         Fail-closed on timeless rows: rows sort timeless-last; the scheduler advances through the last
         row carrying a usable `cursor_at`, so a timestamp-less episode never blocks earlier valid
         rows; an all-timeless page stops without advancing (bounded, no loop). NOTE: `(created_at,
         uuid)` is the accepted primary fix; a per-episode/perceiver processing RECEIPT (even safer for
         imported/out-of-order data) is the remaining optional hardening, deferrable to C.4.4.
       With these, C.4.3 is FROZEN (user sign-off) at menhir `6268f8b`.
     - **C.4.4 - repair, identity-mismatch rejection, and the live-Neo4j gate.** Authorized to start
       (C.4.1/C.4.2/C.4.3 all frozen). Acceptance set: already-bound identity-mismatch rejection;
       live advisory->bound adoption; explicit pending-binding repair; transitive orphan repair across
       merge chains + scheduled orphan-rebind pass (carried C.3 obligation); live-Neo4j atomicity +
       concurrency validation as the final gate. Optional hardening: per-episode/perceiver processing
       receipt cursor (safer than `(created_at, uuid)` for out-of-order imports / non-`IngestGate`
       writers). Sub-split:
       - **C.4.4.1 - already-bound identity-mismatch rejection** - DONE (menhir `ba0c071` initial +
         `98820f9` head-owner restructuring, CI green). The durable invariant belongs to the SOURCE
         CLAIM (head), across every assertion VERSION and interpretation: once a claim is durably bound
         to A, a record presenting ANY different subject identity must NOT rebind it (rebinding is the
         merge path's job). `binding_mismatch` is computed from the head's EXISTING current owner
         BEFORE the assertion MERGE / CURRENT move / supersession:
         `cur IS NOT NULL AND NOT coalesce(cur.binding_pending, true) AND cur.subject_uuid <> $subject_uuid`
         — NOT gated on the presented entity existing (an unresolved `unbound:` sentinel also counts as
         a different identity, so it can never de-authorize A). EVERY mutation is gated on
         `NOT binding_mismatch`: CURRENT move, supersession (via `will_supersede`), pending->bound
         adoption, `binding_pending` recompute, the evidence-tier upgrade (moved out of `ON MATCH` into
         a gated step), and both provenance edges. A mismatched version stays a non-current,
         superseded-flagged AUDIT node that can never mutate the current/head or become materializable.
         The round-1 patch (`ba0c071`) had two holes the restructuring (`98820f9`) closed: (a) a newer
         perceiver_version/changed interpretation made a NEW node whose absent `binding_pending` read as
         `was_pending=true`, so the late check passed and B became current; (b) `n IS NOT NULL` let an
         `unbound:` sentinel recompute `binding_pending` and de-authorize A. `record_assertion` surfaces
         `binding_mismatch`; `bind_and_persist_typed_scalars` counts it `mismatched` (not bound), does
         NOT rebuild for the presented subject, and logs it. Offline tests assert the guard precedes the
         MERGE and every mutation is mismatch-gated; behavioral regressions 1-5 (A stays current + sole
         owner across v2/B, changed-interpretation/B, unbound-sentinel keeps A materializable,
         mismatched higher-tier cannot upgrade A, idempotent same-subject control) are ONLINE
         (`--run-online`) live tests asserting the CURRENT pointer, head owner, HAS_ASSERTION edge
         count, and assertion props against a real Neo4j — part of the C.4.4 final gate.
       - **C.4.4.2 - explicit pending-binding repair pass** - DONE (menhir `68ffed7`, CI green).
         Advisory assertions (binding_pending, `unbound:<source_key>` sentinel, no View) are excluded
         from `orphaned_subject_uuids`, so merge repair never touches them; before this they only bound
         if the same episode happened to be re-perceived. New:
         `TypedAssertionRepository.pending_advisory_assertions(namespace, limit)` returns current
         (NOT superseded) binding_pending rows with every field needed to reconstruct + re-record (a
         superseded advisory is never revived). `repair_pending_bindings(...)` re-resolves each row's
         `subject_display` against the episode's CURRENT linked entities (which may have been
         created/merged since); a UNIQUE match re-records with the resolved subject — assertion_key
         omits subject_uuid so it lands on the SAME node via the C.4.3 pending->bound adoption
         (evidence tier forced to agent; interpretation + world-time preserved) — then rebuilds the
         View. Zero/multiple/blank -> still_pending; store still binding_pending (episode/entity node
         absent) -> still_pending (not rebuilt); binding_mismatch (claim bound elsewhere meanwhile) ->
         surfaced, left to the owner. `TypedScalarPerceptionService.repair_pending_bindings` enforces
         the activation gate first and is LLM-free. Wired into `consolidate_personal_memory` after the
         backfill loop (bounded by `scalar_repair_limit`, default 200) when `enable_scalar_state` is on,
         reporting `scalar_repaired` / `scalar_repair_pending`; flag-off byte-identical. +9 offline
         tests + a live adoption test (advisory -> entity appears -> re-record -> ONE node, real owner,
         non-pending, materializable) for the C.4.4 Neo4j gate.
         - **C.4.4.2 fairness + scoping fix** - DONE (menhir `bf5804f`, CI green). Two production-
           liveness blockers: (a) the oldest-first bounded scan could permanently STARVE newer
           advisories (unresolved first page rescanned forever). Fixed with a durable retry order:
           `pending_advisory_assertions` orders UNATTEMPTED-first, then least-recently
           `binding_repair_attempted_at`, then learned_at/assertion_id as a stable tiebreak; new
           `mark_binding_repair_attempted(ids, at)` stamps EVERY examined row (bound, zero/multiple,
           store-pending, mismatch) so the frontier advances and the bounded scan is EVENTUALLY
           COMPLETE. `repair_pending_bindings` passes all scanned assertion_ids to a `mark_attempted`
           seam; the coordinator wires it. (b) The scheduler ignored namespace scoping — a
           `namespaces=["tenant-a"]` override could repair tenant-b. Fixed: an explicit override
           restricts repair to exactly those namespaces and distributes the bounded limit across them
           (`max(1, limit // n)` each); the normal scheduled invocation uses the global fair queue.
           +6 offline tests (incl. eventual-completeness past an unresolved first page, and an override
           never touching another tenant) + a live full-path repair test
           (pending_advisory_assertions -> repair -> lookup -> re-record -> rebuild).
         - **C.4.4.2 integration fix (namespace/bound/crash)** - DONE (menhir `bc05a0c`, CI green).
           Three integration blockers: (1) the global sweep lost the row's namespace (rebuild bound to
           the coordinator's outer None), so a tenant-a advisory could rebuild its View into the default
           silo. FIXED: repair rebuilds each row in its OWN namespace (`rebuild_scalar_state(uuid, ns)`)
           and an explicit allowlist is enforced fail-closed per row. (2) the per-namespace split
           (`limit // n` invoked once per namespace) multiplied the bound and starved later tenants.
           FIXED: a SINGLE fair query with a deduped `WHERE a.namespace IN $namespaces` allowlist under
           one global LIMIT (`pending_advisory_assertions(namespaces: list)`); `limit` validated
           non-negative. (3) adoption->rebuild had no crash recovery — adoption clears binding_pending
           in the write, so a crash before the separate rebuild left a bound assertion with no View and
           no retry. FIXED with a durable `projection_pending` marker: the record cypher sets it
           atomically when a row is newly bound; `pending_advisory_assertions` also selects
           projection_pending rows (already bound -> just rebuild); repair clears it via
           `mark_projection_complete` ONLY after a successful rebuild; `bind_and_persist` clears it after
           its rebuild too; per-row exceptions are isolated so one bad row can't abort the batch or block
           the fairness stamp. +13 offline tests + 2 live tests (global repair rebuilds in the row's
           namespace with no default-silo View; full-path repair). Live crash-recovery + atomicity
           remain part of the final C.4.4 Neo4j gate.
       - **C.4.4.3 - transitive orphan repair across merge chains + scheduled orphan-rebind pass** -
         DONE (menhir `44f674d`, CI green). The carried C.3 obligation. (1) TRANSITIVE repair:
         `repair_incomplete_reconciliations`, after re-running a receiptless merge, walks FORWARD along
         the merge lineage and replays every downstream merge EVEN WITH A RECEIPT — if A->B (op1) failed
         but B->C (op2) succeeded, repairing op1 rebinds A's assertions onto B and op2's receipt is now
         stale (those assertions must still travel B->C). `handle_merge` is idempotent (per-op rebind
         journaling + idempotent rebuild + receipt MERGE) so replaying a completed downstream op is
         safe; a visited set guards cycles; returns `downstream_replayed_op_ids`. (2) SCHEDULED
         orphan-rebind pass: `repair_orphaned_assertions` resolves assertions whose subject Entity is
         gone (merge whose post-commit rebind never ran) to their survivor via merge lineage, rebinds +
         rebuilds transitively; an orphan with no lineage is surfaced (`unresolved`), never guessed. (3)
         `GraphOperationsJournal.list_committed_merges` is the read-only lineage source (COMMITTED
         ENTITY_MERGE pairs, oldest-first). (4) the scheduler runs the orphan pass after pending-binding
         repair when `enable_scalar_state` is on (journal read failure isolated), reporting
         `scalar_orphans_repaired`/`scalar_orphans_unresolved`; flag-off omits the keys. +10 offline
         tests (stateful-fake, mirroring the C.3 `test_scalar_state_merge.py` precedent). Live
         transitive/orphan behavior under real Neo4j joins the final C.4.4 gate.
         - **C.4.4.3 orchestration fix (namespace/lineage/fairness/receiptless)** - DONE (menhir
           `481d53c`, CI green). Four production-orchestration blockers: (1) orphan repair violated
           namespace isolation (ran with `namespace=None` -> default-silo rebuilds, cross-namespace
           retire). FIXED: `orphaned_assertions` returns `{subject_uuid, namespace}` work items with a
           namespace allowlist; repair rebuilds/retires in each orphan's OWN namespace; allowlist
           enforced fail-closed in the service. (2) lineage query hid valid merges (filtered non-merge
           rows AFTER the limit; oldest-N truncation stranded later chains). FIXED: filter
           `state='COMMITTED' AND operation_kind='ENTITY_MERGE'` IN SQL with a high SEPARATE
           `scalar_lineage_limit` (default 5000) distinct from the bounded orphan-work limit; added
           `list_committed_unmerges`. (3) orphan scan had the advisory-scan starvation bug (no order/
           stamp). FIXED with durable `orphan_repair_attempted_at` fair order (unattempted-first, then
           least-recently-attempted, stable tiebreak); every examined orphan stamped regardless of
           outcome; per-row exceptions isolated; a journal-read failure SKIPS both journal-driven passes
           WITHOUT stamping (transient outage != missing lineage). (4) receiptless reconciliation was
           not wired — the scheduler only ran the orphan pass, so the rebind-ok/rebuild-failed merge (no
           orphan) and committed unmerges whose scalar hook failed were never repaired. FIXED: the job
           runs `repair_incomplete_reconciliations` (full lineage + committed unmerges) BEFORE the fair
           orphan pass, reporting `scalar_recon_repaired`. FAIL-CLOSED SURVIVOR: the chain's terminal
           survivor Entity must exist (`entity_exists`) or the orphan stays unresolved, never projected
           against a dead uuid. +offline tests + a live namespace-isolation regression. Live
           reconciliation/orphan/concurrency behavior remains part of the final C.4.4 Neo4j gate.
         - **C.4.4.3 deep-scoping fix (event-log namespace / complete lineage / isolation)** - DONE
           (menhir `1774490`, CI green). Four freeze blockers pushing namespace all the way into the
           event log: (1) namespace isolation now reaches the OPERATIONS, not just work-item selection —
           `assertions_for_entity`/`materializable_assertions_for_entity`/`fold_entity`/
           `rebuild_scalar_state`, `rebind_assertions`, `restore_rebound_assertions` all take an optional
           namespace (None = all, unchanged); `:AssertionRebind` persists its namespace for a silo-scoped
           restore; orphan attempts stamped by (subject_uuid, namespace); `entity_exists` namespace-
           scoped; replay visitation keyed by (namespace, op_id). (2) receiptless reconciliation derives
           each op's OWN namespace fail-closed from its assertions (`namespaces_for_subjects`), NOT
           `allow_list[0]`/None — one namespace scopes it, none skips, >1 errors (invariant violation, no
           receipt); the allowlist filters roots, never collapses the lineage into one namespace arg. (3)
           `list_committed_merges`/`list_committed_unmerges` now PAGE through ALL committed history
           (page_size batch, not a total cap; SQL-filtered), so a later root/downstream op is never
           hidden and malformed rows can't consume a limit ahead of valid lineage (hard memory ceiling
           only). (4) `repair_incomplete_reconciliations` isolates each root (per-op try/except): a
           failing op is `errored_*` with NO receipt while unrelated roots + the orphan pass still
           proceed; the scheduler no longer aborts on a reconciliation raise. Also fixed the live orphan
           fixture to create+bind THEN delete the entity (a true orphan, not an advisory). Live
           reconciliation/orphan/2-worker concurrency behavior remains the final C.4.4 Neo4j gate.
         - **C.4.4.3 identity/receipt/lineage fix** - DONE (menhir `843035f`, CI green). Three
           correctness blockers: (1) the namespace-scoped survivor check invented an `Entity.namespace`
           contract that assertion binding does NOT use (`_RECORD_CYPHER` binds via a UUID-global
           `OPTIONAL MATCH`), rejecting valid canonical survivors. `entity_exists` is now UUID-global,
           matching binding; namespace stays on assertions + Views. (2) namespace-specific repairs wrote
           a GLOBAL receipt, so for one op spanning two silos a tenant-A success could certify a failed
           tenant-B repair and permanently mask its missing View. Receipt identity is now
           `(operation_id, kind, namespace)` via `receipt_key` (null silo sentinel);
           `reconcile_complete` is namespace-scoped by default (`any_namespace` opt-out); DDL swaps the
           `operation_id UNIQUE` constraint for `receipt_key UNIQUE` + an operation_id index (old one
           dropped at activation); reconciliation iterates each affected namespace independently. Op
           namespace derivation moved OFF survivor-native rows -> `namespaces_for_operation` reads the
           op's OWN affected assertions (its `:AssertionRebind` records + rows still on the absorbed
           uuid), so a tenant-A merge whose survivor holds tenant-B-native assertions is tenant-A, not a
           multi violation. (3) the lineage reader's `_LINEAGE_MAX_ROWS` ceiling was a PERMANENT cutoff
           (every call restarts at offset 0, so beyond-ceiling rows were never read on any pass while
           the scheduler treated the truncated list as complete) — removed; pagination runs to
           exhaustion. Live fixture corrected to the stated isolation case: BOTH tenants bind to the
           SAME absorbed uuid before deletion; repairing only tenant-a asserts tenant-b stays on the
           dead uuid with no survivor View, no default-silo View, and stamps touching only tenant-a.
         - **C.4.4.3 unmerge crash-window fix (namespace evidence outlives restore)** - DONE (menhir
           `bd9bbce`, CI run `29715343534` green). The unmerge half of the frozen receiptless
           crash-recovery contract: `restore_rebound_assertions` DELETES the forward op's
           `:AssertionRebind` records, which were the only durable carrier of the unmerge's namespace.
           On restore-succeeded / rebuild-failed, no completion receipt is written, yet the next pass's
           namespace discovery (which looked only at the now-deleted forward rebinds) found nothing and
           silently skipped the receiptless unmerge FOREVER. The prior regression only covered a scalar
           hook that never started, where the rebind records still exist. Fix: `handle_unmerge` writes a
           PENDING `:ScalarReconcile` marker BEFORE restore, sharing `receipt_key` with the completion
           write so completing PROMOTES the same node; `ON MATCH` is deliberately omitted so a replay can
           never downgrade a complete receipt to pending. Unmerge namespace discovery moved to
           `namespaces_for_unmerge` = forward-merge surviving rebind records UNION the unmerge's own
           markers (union, not fallback: a partially restored op may have evidence in both). Invariant:
           namespace evidence is never consumable before the namespace-keyed receipt is complete.
           Regressions: restore-ok/rebuild-failed stays discoverable, repairs exactly once with no
           double-restore corruption, receipt completes, third run is a no-op (verified load-bearing —
           removing the intent write fails the test); a pending marker does NOT satisfy the completion
           check; repo-level intent + union cypher contracts.
         - **C.4.4.4 final live-Neo4j gate** - DONE (menhir `c0a1580`, CI green; run against the
           stood-up :7688 test instance). First execution of the accumulated `online` scalar suite
           against a REAL Neo4j surfaced two defects the offline fakes structurally could not:
           (1) `repair_orphaned_assertions` stamped `orphan_repair_attempted_at` AFTER the repair had
           already rebound the assertion onto the live survivor, so `mark_orphan_repair_attempted`
           (keyed by the now-stale dead subject_uuid) matched nothing for exactly the repaired rows —
           silently violating the method's own "EVERY examined orphan is stamped" contract (the offline
           fake stamps by work-item key regardless of row movement, so it never caught this). Fix: stamp
           every examined orphan UP FRONT, before the chain replay relocates it; rebind only sets
           subject_uuid/rebound_at so the stamp rides along on the moved node. (2) `list_scalar_state_views`
           filtered by `n.group_id` but did not PROJECT it, so a caller could not assert which silo a
           rebuilt View landed in; now returns `n.group_id AS namespace` (matching the sibling `list_*`).
           Live isolation fixture (`test_orphan_repair_is_namespace_isolated`) now passes end-to-end on
           real MERGE semantics. Added the required **2-worker concurrency idempotency** gate: two
           independent-driver workers repair the SAME receiptless unmerge simultaneously and converge
           EXACTLY-ONCE (one CURRENT, one owner, one completion receipt, zero leftover rebind records),
           then a sequential pass no-ops; stable over 8x repeated runs. Full live scalar gate: 10 passed.
           Full offline unit suite: 2064 passed, 0 failures. DISCLOSED non-blocker: the PRE-EXISTING
           `test_phase_one_bootstrap_live.py` module fixture errors (5) — its own fixture calls
           `Neo4jRepository(...)` without the now-required `database` arg — stale harness rot outside this
           piece's changed path, not a regression from C.4.4.
         - **C.4.4.4 projection-pending two-worker View race (DB-level dedup)** - DONE (menhir
           `a180043`, CI green). Reviewer caught that the earlier unmerge concurrency test never
           inspected any View-level invariant, leaving a real database race unproven: `_write_version`
           reads the current View in one query, then `CREATE`s a new node with a random uuid, and
           `view_key` had only a plain index — so two independent workers rebuilding the SAME
           `projection_pending` assertion can each observe "no current" and each create a
           `view_current=true` node for the slot. No query-level check-then-create can prevent this under
           read-committed isolation; only a DB constraint can. Fix: a uniqueness constraint on
           `ss_view_key_current`, a property carried ONLY by a CURRENT scalar_state node (set on create,
           REMOVED on supersession and on retire). It is NULL on every non-scalar fact, every :Metric,
           and every superseded/retired scalar node, so those never participate — the fingerprinted
           metric saga and all other View kinds stay byte-identical off the scalar path. The losing
           concurrent CREATE fails the constraint; `_write_version` catches `neo4j ConstraintError`
           (scalar_state only) and converges on the winner instead of forking a duplicate. Activation
           BACKFILLS current scalar nodes before bringing the constraint online, and
           `scalar_state_schema_ready()` gates on it. New live gate: a deterministic 2-worker test
           synchronized at the projection-WRITE boundary (inside `_current_by_key`, after the read,
           before the CREATE) verifies exactly-once — one TypedAssertion, one CURRENT edge, one owner,
           one current View in the correct namespace, no default-silo View, no duplicate versions,
           `projection_pending` cleared, third pass a no-op. Verified LOAD-BEARING: with the stamp
           neutered the race yields two current Views (`assert 2 == 1`). Stable over repeated runs. Full
           scalar live gate 11 passed; offline unit 2064 passed, 0 failures.
       - **Piece C FROZEN and SHADOW-BUILD-ONLY** - DONE (this docs commit). Every C.4 sub-piece is
         frozen (C.4.4.1 `98820f9`, C.4.4.2 `bc05a0c`, C.4.4.3 `bd9bbce`, C.4.4.4 `a180043`). The whole
         write side — perception -> durable typed-assertion log -> deterministic fold -> `scalar_state`
         Views -> merge/unmerge reconciliation + repair passes — is built and gated behind
         `enable_scalar_state`, but is SHADOW-BUILD-ONLY: no user-facing recall reads a ScalarStateView
         for authority. That exposure is Piece D and is deliberately NOT started here.
- **D (recall authority)** - SEPARATE, DEFERRED effort (not started). After C, gated. -> staged shadow
  -> counterfactual -> canary validation. No user-facing authority until the canary stage.
