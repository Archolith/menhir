---
artifact_schema: 1
artifact_uuid: fc0c1c90-96e5-4679-8d68-92ab2c6bfde8
artifact_type: plan
artifact_status: IMPLEMENTING
---

# Menhir: a counting lane that builds on scalars, then retire the counter lane

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Current state (units 1, 3, 3a, R, 6 and 4R shipped, all flags default off; paid A/B per flag pending in Menhir #361 and #362; unit 5, the distinct-items goal and unit 7 are not built and are tracked in Menhir #370) (was: DRAFT rev 4 (2026-10-16), awaiting owner approval; partial: unit 5 and unit 7 not evidenced, PRs 320 and 321 open drafts). Tracker: #170 (closed; parent #168 still open).
Last verified: 2026-10-21 - PARTIAL (high); PRs 292-295 and 303-306 merged (unit 3 b2bc16e); tarball scan of Menhir main found all five counting flags, CountDecision, count_key_embedding and the flag sweep (control symbol found), and 0 matches for any unit 5 symbol; PRs 320 and 321 (separate counted-view-selection and extract-rules plan) still open drafts
History: rev 2 (2026-10-15) designed units 1-7; unit 3 shipped as PR #293 (b2bc16e). Rev 3 (2026-10-16)
added fixes after the first paid bench. Rev 4 folds in the Opus review of rev 3 and two owner
decisions. Reviews: the combined counting-lane review (source `#293`, F1-F17, on PR #293; not moved here);
rev 3 review findings R3-1..R3-21 (same combined review),
each given a disposition below.

## Why

The counter lane re-reads the oldest 500 user turns every pass and never sees turn 501+, yet marks the
namespace clean (`turn_evidence_repository.py:325-342`, `scheduler_tasks.py:709`). Owner direction
(2026-10-15): read each new turn once; compare counts to the counts we already have; keep pieces
separate, each behind its own flag, enabled only if equal or better than today; counting runs after
scalars and builds on them.

## Where we are (bench 2026-10-16)

One paid Flex run, 5 LongMemEval multi-session count questions, gpt-6-luna, k=3: counting OFF 4/5,
ON 3/5. The model-kit question ("How many model kits have I worked on or bought?", gold 5) regressed:
2 counted Views + 2 occurrence observations took 4 of 10 recall slots and pushed out the memory that
listed all five kits (F1), and any authority record made the harness label every raw memory
"non-authoritative" (F2). The clothing pick-up/return question is about obligations, which the counting
prompt excludes by design.

## Owner decisions (2026-10-16)

1. Related counts only: when counting, send only the existing counts related to the happening found;
   the model then decides new or repeat from time and content (unit 4R).
2. Distinct items (one kit bought and later finished = 1 kit) need logic that combines Views: a later
   goal, not this plan. Now: store the specific item on each occurrence so the later step can group.
3. When the repeat vote cannot decide: show it as uncertain ("at least N, plus M that may be
   repeats"), not silently dropped.

## Safety property

A counted total is a lower bound of DISTINCT HAPPENINGS the user mentioned, plus a separate uncertain
count. No happening may add to the counted total twice: not by re-reading the same turn, a re-version,
overlapping runs, a delete, a repaired binding, or a retelling in the same or a later page. A
happening the evidence still supports may not drop out of both counted and uncertain.

## Flags (one per piece; A/B per flag)

- `personal_memory_counting_enabled` (exists): the lane plus the correctness fixes in unit 3a.1-3a.8.
- `personal_memory_counting_extract_v2`: unit 3a.9 extraction changes (new counting perceiver version).
- `personal_memory_counting_recall_authority_only`: unit R, Menhir side.
- archolith-bench harness option `counted_authority_only` (env or CLI): unit R, harness side.
- `personal_memory_counting_wording_enabled`: unit 6.
- `personal_memory_counting_match_enabled`: unit 4R.
All default off. Each needs `personal_memory_counting_enabled` (and scalars).

## Unit 3a: correctness fixes to the shipped lane (counting flag)

1. One count per turn (F3, R3-6). `_fold_counted` (`domain/scalar_state_fold.py:336-362`) collapses
   occurrences to one per (subject, attribute, episode_uuid), ignoring scope, keeping the largest value.
   Under 4R the key becomes (subject, object family, episode_uuid, item), applied before slot choice.
2. Re-version (F3, R3-18). On a counting perceiver_version change the namespace is re-read from a clean
   slate in (valid_at, turn_id) order; prior-version occurrence heads are retired as their turns are
   re-read; the audit records every retirement so a v2 miss is visible.
3. Write fence (F3, R3-7). Copy the `EvidenceNamespaceFence` generation pattern
   (`memory_queries.py:1144-1146`): a per-namespace counting fence with a generation number. A run reads
   the generation at start; every write (assertion persist, decision, cursor advance) is conditional on
   the generation being unchanged in the same transaction; a mismatch aborts the page with no cursor
   advance. The delete cascade bumps the generation, so a stale runner's writes are refused. Both entry
   points (`POST /api/phase3/run` and the scheduler) take the fence. The cursor advance
   (`turn_evidence_repository.py:592-606`) stops being an unconditional MERGE.
4. Read-side kill switch (F6, R3-14). `ScalarStateService` gets `counting_enabled` like
   `scalar_history_enabled`; off -> the fold ignores occurrence rows. Recall reads `scalar_basis`
   (already a node property: `scalar_state_service.py:246-248` -> `view_write_repository.py:409`) and
   hides basis=counted Views when off. `scalar_basis` is written for every state ("stated" too), so a
   counted->stated change does not leave a stale value (R3-11 minor).
5. Flag-flip sweep (R3-14). Each namespace stores the last-applied counting flag state; at the start
   of a phase-3 pass, a mismatch rebuilds that namespace's counted slots (retire when off).
6. API (F7). `Phase3RunResponse` + handler carry `counting_namespaces_processed`, `counting_batches`,
   `counting_occurrences_stored`, `counting_advisory`, `counting_llm_calls`, `counting_namespaces_failed`,
   `counting_dirty_after`.
7. Order and isolation (F8, F13). Event history runs before counting on the shared budget; each
   counting namespace runs in try/except, failure leaves its cursor unadvanced.
8. Small (F14, F16). Warn once at startup when counting is on and scalars are off; the counting
   perceiver_version becomes a setting.
9. Extraction v2 (own flag; F11, F12, F17). Negation/modal guard anchored near the span's verb (no bare
   not/can/will); one sample's proposals split by attribute before overlap grouping; the ALREADY
   CAPTURED block fenced with a delimiter turns cannot contain.

Accepted as is: F15 (budget may overshoot by k-1 calls per page, same as scalars).

Proofs: same turn, different spans -> 1; same turn, different scope -> 1; cursor reset + different span
-> 1; re-version keeps the total, and a v2 miss drops it with an audit row; stale generation -> writes
refused, cursor not advanced; delete during a page -> runner aborts, deleted turns re-read; API and
scheduler overlap -> one processes; flag off -> fold ignores occurrences, sweep retires counted Views,
scalar Views byte-identical; route test for counting keys; event history gets its page under a tight
budget; failing namespace leaves others done; guard keep-cases; two-attribute vote -> two decisions.

## Unit R + unit 6: counted Views stop crowding recall and say what they are (own flags, ship together)

Menhir (`personal_memory_counting_recall_authority_only`):
- `recall_pipeline.py` authority loop (~1220-1288): read `scalar_basis`; add `basis` to
  `ScalarAuthorityVerdict` (`domain/recall.py:230-247`) and the API response model
  (`api/routes_support.py:333`); skip the ranked `candidate_inputs.append` (~1269-1270) for counted Views.
- Order (R3-11): observations are appended at ~892, before the authority layer is built at ~1052 from
  those same hits. New order: collect slots from all hits -> build the authority layer -> then remove
  occurrence observations whose slot has a counted verdict.
- Observation lane (R3-10): `search_assertion_embeddings` excludes occurrences not counted (repeat,
  same_event, uncertain under 4R) and keeps at most one occurrence per slot.

Harness (archolith-bench `archolith_bench/harness/menhir_client.py`, option `counted_authority_only`;
R3-13): only basis=counted advisory records are affected: they do not set `has_authority` (:555) and
render after raw memories, inside a fixed authority cap separate from `limit` (:617). Other advisory
records keep today's behaviour, so the OFF arm is unchanged; re-run the OFF arm anyway after the PR.

Wording (`personal_memory_counting_wording_enabled`; F9, R3-12, owner decision 3): basis=counted renders
"at least N you mentioned" with per-action counts, item names and up to 8 quotes, plus "M more that may
be repeats" when uncertain > 0. Covers both renderers (`services/context_builder.py:376-399`,
`menhir_client.py:114-134`) and the MCP payload. Counted Views stay advisory and never lead (F10).

Proofs: 10 entity candidates + 2 counted slots -> all 10 entities in results, counted slots only in
authority_layer; 6 retellings + 1 stated scalar -> the scalar authority still present; harness: a
counted advisory record does not relabel, other advisory records unchanged; scalar-only recall
byte-identical; wording golden tests for both renderers.

## Prerequisite for 4R: one user subject

All 5 bench namespaces log `SELF_FORKS_REQUIRE_MIGRATION ... forks=1`; with `canonical_self` off the
user's own happenings split across two subjects (R3-9). Before 4R: run the self-fork migration (or
resolve the self-fork set and pull across it), and test that related pulls see both forks.

Resolved 2026-10-17, no code needed. No self-fork migration exists (canonical-self runbook: "not
available"). On the counted bench snapshot all 20 bound occurrences across the 5 namespaces have
subject_uuid = self_uuid_for_namespace(ns); none sit on the fork. The fork is the Graphiti
extractor's "user" :Entity (is_self false), and the typed lane never binds to it: `_resolve_subject`
takes the self seam first and refuses the lexical twin (`test_cf131_self_token_guard.py`). So 4R
pulls on the canonical self only; its tests add a fork-present case (a "user" fork entity in the
namespace, pull still sees every user occurrence). Separate finding: 0a995998's two unbound
occurrences are pronoun items ("I got them"), binding_pending, not a self split.

## Unit 4R: related-count matching (`personal_memory_counting_match_enabled`)

Decision state (R3-3): a separate `:CountDecision` node per (assertion_id, counting perceiver_version):
`count_status` (counted | repeat | same_event | uncertain), `target_id`, vote, reason, decided_at,
fence generation. Written by an explicit MERGE ... SET outside the assertion MERGE, so it can change.
Only the counting runner and the re-decide step write it. (Named `count_status`, not "absorbed", which
the fold already uses for the delta guard; R3-17.)

Fold with the match flag on: sum only `counted`; `repeat`/`same_event` excluded; `uncertain` or no
decision yet -> the uncertain count. A `repeat`/`same_event` whose target is not a current `counted`
row -> uncertain and queued for re-decide (R3-2). With the match flag off: unit 3 behaviour (sum all
occurrences after 3a.1), stated as the off state.

Flow per page (under the 3a.3 fence):
1. Find. Counting extraction plus `object` (family, e.g. "model kit"), `item` (the specific thing, e.g.
   "Tamiya 1/48 Spitfire Mk.V", owner decision 2) and `action` (finished, bought, started, visited).
   Subject is the user for the user's own happenings; places go in `scope`-like detail, not subject.
2. Bind. Subjects are bound before deciding (split of the persist loop at
   `typed_scalar_persistence.py:105`); rows are recorded with no decision (so a rebuild meanwhile counts
   them as uncertain, never counted).
3. Pull related (R3-9). New query: occurrences with operation = 'occurrence', subject in the user's
   subject set, `count_status = counted`, current, current counting version, via `tenant_scope_cypher`.
   Match on object family by token overlap (not exact set equality; drop action verbs), or cosine on a
   new write-time `count_key_embedding` of "object action item" (own property, vector index). Also
   relate the page's own candidates to each other, earlier (valid_at, turn_id) first (R3-1). Rank:
   signature hits, then cosine, then assertion_id. Bounds: 10 per candidate, 40 per page; a candidate
   whose set was cut by a bound is marked truncated (R3-4).
4. Decide. One call per page for candidates with related counts, k=3, 2/3 vote. Output per candidate:
   `counted` | `repeat:<id>` | `same_event:<id>`, plus the family name to use (an existing related family
   when one fits). Targets may be page-local candidate ids.
5. Code rules after the vote: target must be a current `counted` row (or a page candidate decided
   counted) in the same namespace and subject set, resolved to its root (no chains); else uncertain. A
   truncated candidate cannot be `counted`: it becomes uncertain. No 2/3 agreement -> uncertain. On a
   repeat where the candidate's valid_at is earlier than the target's, swap: the earlier one is counted
   (R3-19). Candidates with no related counts and no truncation -> counted.
6. Record decisions and rebuild, conditional on the fence generation.

Re-decide (owner decision 3, R3-16, R3-2, R3-8): each pass re-decides `uncertain` rows and rows with a
missing or dead target, oldest first, inside the counting budget, at most 3 attempts per row.
Occurrences repaired out of binding_pending get no decision, so they are uncertain until the next pass
decides them.

Slot and View shape (R3-17): counted slot key = (subject, object family, value_kind, unit); scope and
action are details, not key parts. `FoldedScalarState` and the View audit gain per-action counts, item
list and the uncertain count.

Skip work (R3-20): a candidate whose assertion key and current-version decision already exist is not
re-decided (re-reads after a watermark wipe cost extraction only).

Proofs (fake LLM): two retellings in one page -> 1 counted; retelling in a later page -> repeat; true
match at rank 11 -> uncertain, not counted; candidate past the page cap -> uncertain; target deleted ->
row becomes uncertain then re-decided, happening not lost; re-version with a repeat -> total 1; repeat
of an uncertain target -> uncertain; earlier-dated retelling found later -> swap, View date is the
happening's; unbound retelling later bound -> uncertain, not counted; foreign-namespace or
foreign-subject target -> uncertain; finished + bought kit on different days -> one family, counted 2,
per-action 1+1, item listed once; same item, different subject -> separate; no related counts -> no
decide call; match flag off -> unit 3 behaviour.

## Order and gates

1. 3a (counting flag fixes) and 3a.9 (own flag).
2. R + 6 (Menhir PR + archolith-bench PR; three flags). Re-run the OFF arm after the harness PR.
3. Self-fork prerequisite.
4. 4R.
5. Paid A/B per flag on the same 5 items plus a wider multi-session slice (ask first). Each flag stays
   off unless ON is equal or better than OFF.
6. Unit 5 (counts onto stated totals, own flag), as in rev 2.
7. Later goal: distinct items (combine Views by item; owner decision 2).
8. Unit 7 counter-lane retirement: only after the distinct-items goal, since the counter lane keeps
   distinct-item counts and category spend sums until then.

## Units kept from rev 2

- Unit 1 (occurrence groundwork) and unit 3 (counting lane) shipped in PR #293. Design notes for unit 3
  remain true except where 3a changes them.
- Unit 5: occurrences after a stated total are added to it (own flag); rebuild trigger = the 3a.5 sweep.
- Unit 7: counter-lane retirement, now gated as above.
- Importance (owner 2026-10-15): tracked / notable / routine per occurrence; Views only for slots with a
  tracked or notable occurrence; no numeric mention floor.

## Deferred

- Distinct items (owner decision 2): later goal.
- Obligations ("need to pick up / return X"): a separate flagged lane, after measuring raw recall once
  unit R lands.
- View embeddings: only after unit R, otherwise counted Views crowd recall more.
- Why only 2 of 5 kits were extracted: rerun the count pass with the consolidation audit on (paid; ask
  first).
- Slot matching for scalars; the event-history lane stays separate.

## Cost

Counting: k extraction calls per page of new turns. 4R adds k decide calls per page that has related
counts, one embed call per candidate for `count_key_embedding`, an indexed vector lookup per candidate,
and bounded re-decide calls for uncertain rows. Re-reads after a delete cost extraction only (skip rule).
Retiring the counter lane later removes its k extract + cross-check, coref and verify calls on up to
500 turns every pass.

## Open questions

1. Related-match thresholds (token overlap, cosine): tune on fake-LLM fixtures before the paid run.
2. k=3 split rate on same-or-different calls: measure the uncertain share in the paid run; if high,
   consider k=5 for decide only.
3. Lease/fence timing vs Flex latency on a 500-turn page (check the server log timings);
   consider smaller counting pages.
4. Do any View readers besides recall (explorer, MCP get-entity, list-views) show counted Views? Gate
   them with the same basis check if so.
5. Delete counter-lane code one release after it goes off (assumed) or keep it?

## Size

About 6 PRs before the A/B: 3a, 3a.9, R + 6 (Menhir), R (archolith-bench), self-fork prerequisite, 4R.
Then unit 5, the distinct-items goal and unit 7.

## Appendix: rev 2 design notes (unchanged, 2026-10-15)

### What scalars already give us (verified)

- Cursor over new turns, paged, version-reset (`scalar_consolidation.py:178-250`,
  `turn_evidence_repository.py:385-421`).
- One stored `:TypedAssertion` per claim, operation absolute | delta | expire
  (`typed_assertion.py:59, 266-290`); replay of the same span is a no-op (`:374-397`).
- Fold from the stored log: latest absolute is the anchor, later deltas summed
  (`scalar_state_fold.py:157-250`). "Have 10, bought 3 more" already gives 13.
- Activity totals ("worn them six times") already land as `_count` slots
  (`typed_scalar_rules.py:296-309`).
- The scalar loader reading `agent_quoted` turns is INTENDED: agent-supplied user words may feed scalar
  perception but never count as user foundation (`menhir-scalar-default-enablement-plan.md:46-56`;
  pinned by `test_agent_quoted_evidence.py::test_scalar_lane_keeps_agent_quoted`).

### Why a separate lane, not a bigger scalar prompt (checked)

- The shared scalar prompt says twice to emit nothing for a single happening; reversing that
  touches every scalar read.
- Vote: a sample that reads one span two ways casts a conflicted vote (`typed_scalar_rules.py:1621`);
  the shipped scalar threshold is 2/3 (`settings_model.py:371`), so one such sample is survivable,
  two block a value that works today.
- A shared cursor would tie resets together: re-versioning counting would re-read and re-pay every
  scalar page (version-mismatched watermark is ignored, `turn_evidence_repository.py:405`).
- Store: one current assertion per source span head; scalar always uses claim_ordinal 0
  (`typed_scalar_rules.py:1332`). Counting uses its own ordinal so it never shares a head with a
  scalar claim on the same span.
- Precedent: the event-history lane already runs as its own phase with its own watermark,
  version and flag (`turn_evidence_repository.py:432-513`, `event_consolidation.py`).

### How counting builds on scalars

1. Order. Counting runs after the scalar phase in the same scheduler pass and shares its call budget
   (rev 4: event history now runs before counting, 3a.7).
2. Never ahead of scalars. Counting has its own watermark but only reads turns at or before the scalar
   cursor for the scalar perceiver_version in effect. If scalars reset (version bump), counting pauses
   until scalars catch up.
3. Input per page: the scalar claims already stored for these turns ("already captured, do not count
   again"). (The rev 2 "known counters, bounded 50" input is replaced by unit 4R's related pull.)
4. Output: `occurrence` claims only (value 1 for counts, the amount for spend). The counting lane
   cannot write absolute, delta or expire; the scalar lane cannot write occurrence (per-lane allowed
   operations; parser check at `typed_scalar_rules.py:1161`).
5. Double-count guard in the fold: an occurrence from a turn that already gave the same slot a delta
   is absorbed. "Visited three more times" stays +3, not +3 and +1.
6. If scalars are off, counting does nothing.

### Unit 3 design (as shipped in PR #293)

- Flag `personal_memory_counting_enabled` (default off), plumbed like event history:
  `config/feature_flags.py`, `config/settings_model.py`, `core/runtime.py` (also the LLM build
  condition), `services/maintenance_scheduler.py` (also the job-registration OR at :187-191),
  `api/routes_handlers.py`, `api/routes_support.py`. Counting runs only when scalars are also on.
- Cursor: `:CountingConsolidationWatermark` with list/load/advance queries copied from the event lane.
  The load query only returns turns at or before the scalar watermark for the scalar perceiver_version
  in effect; no scalar watermark or a version mismatch returns nothing (pause). Same turn filter as
  scalars (agent-quoted included). The label is in every watermark wipe site.
- Runner `services/counting_consolidation.py`, after the scalar phase in
  `scheduler_tasks.consolidate_personal_memory`, sharing the call counter and budget; result keys
  `counting_*` only when on.
- Extraction: `extract_typed_scalars_once` and `parse_scalar_row` take `prompt` and
  `allowed_operations` (defaults = scalar behaviour). `COUNTING_SYSTEM_PROMPT`. Occurrence rows: a count
  with no number in the span is 1; one positive number is taken; money must be one positive amount;
  `importance` parsed onto the proposal. A deterministic `occurrence_not_happened` drop for planned,
  hypothetical or negated clauses. Occurrences are dated by the turn.
- Identity: counting claims use claim ordinal 1; numeric counting perceiver_version ("v1").
- Gate: the scalar k-vote, unchanged. Importance is not in the vote label; the committed claim takes
  the most common importance among agreeing samples, ties to the higher level.
- Persistence: the existing bind-and-persist path, with `importance` kept on the pending-repair path.
- Views: counted Views get provenance edges (contributing occurrences as CONTRIBUTED_TO, absorbed as
  SUPERSEDED_ANCHOR). They stay advisory in recall (no user foundation, never lead).
- Not changed: the dormant projection path (`scalar_projection_definition.py`).

### Rev 2 tests (still required; 3a/R/4R proofs above add to these)

- Counting never reads past the scalar cursor; a scalar version reset pauses counting.
- Turn 501+ is counted; a replayed page does not double count.
- Same turn gives a scalar delta and a counting occurrence: counted once.
- Same span claimed by both lanes: two heads, neither lost.
- Two subjects with the same activity stay separate.
- Counting off: scalar Views byte-identical; no counting watermark written.
- Budget exhausted after scalars: counting skipped, namespace stays dirty for counting only.

## Note (2026-10-16)

The rev 3 review is part of the combined counting-lane review.
Owner approved rev 4 for implementation (orchestrated delegation), 2026-10-16.
