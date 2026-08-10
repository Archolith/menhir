# Cumulative Activity Scalars

**Status:** IMPLEMENTED / FOCUSED VERIFICATION COMPLETE
**Date:** 2026-08-08
**Owner:** Codex orchestrator with parallel Luna workers
**Repositories:** `menhir` (production behavior), `archolith-bench` (validation only)

## Problem

Statements such as "I've worn my black Converse six times" report a cumulative observation, not
six separately evidenced events. Today they can remain only as ordinary memory content, allowing a
stale earlier total to outrank the newer total at recall. Manufacturing six event records would be
equally wrong because the source supplies neither six timestamps nor six independently grounded
occurrences.

## Decision

Represent an explicit activity total through the existing typed-scalar pipeline:

- subject: the distinct activity object when one is grounded (`my black Converse`), otherwise the
  actor (`user`);
- attribute: stable activity family ending in `_count` (`wear_count`, `visit_count`);
- scope: only a grounded differentiator needed to separate lanes;
- value kind: `count`;
- unit: empty, following the existing count contract;
- operation: `absolute` for a reported running total, `delta` only for an explicit signed change;
- time and provenance: existing `valid_at`, `learned_at`, source span, episode, and evidence edges.

Do not add a node kind, View kind, event-count store, or new fold. Do not synthesize individual
events from an aggregate. Event History continues to represent only directly grounded occurrences.

## Invariants

1. "I've worn them six times" is an absolute cumulative count, not frequency and not six events.
2. "I wore them yesterday" is not a scalar merely because it is an activity occurrence.
3. "I wear them twice a week" is frequency, not a cumulative count.
4. Tentative, hypothetical, approximate, vague, and discrete-alternative totals do not become exact
   scalar authority.
5. A delta requires explicit additive/subtractive wording and a resolvable accumulator lane.
6. Earlier and later explicit totals are both retained; the existing scalar fold selects the latest
   valid absolute and preserves the earlier assertion in history.
7. An owned object's count stays on the object. It is never silently moved to the user.
8. Tests must cover non-LongMemEval wording, malformed grammar, and spelling noise so the design is
   not fitted only to `lme-618f13b2`.
9. Historical canonical run artifacts are immutable. New evaluation output uses a new result name.

## Implementation Waves

### Wave 1A — Menhir perception contract

Files:

- `src/menhir/services/typed_scalar_rules.py`
- a new focused test module under `tests/`

Work:

- teach the typed-scalar extraction contract the distinction among cumulative activity totals,
  individual occurrences, and recurring frequency;
- require stable activity-family naming and object ownership;
- add prompt-contract, parser/gate, and production-style examples including corrections, deltas,
  approximate/tentative negatives, grammar noise, and unrelated activities;
- reuse the existing `TypedScalarProposal` and `TypedAssertion` contracts unchanged.

### Wave 1B — Benchmark panel and focused regression

Files:

- existing `fixtures/scalar_identity_cumulative_v1.json` and
  `scripts/measure_scalar_identity_isolated_comparison.py` as the generic cumulative baseline;
- a new narrow activity panel only if the Menhir production slice cannot be measured by the
  existing instrument without changing its frozen cases.

Work:

- reuse the existing cumulative-completion panel rather than duplicating its runner or frozen cases;
- define a small production-general activity slice with exact expected classification and slot shape;
- include `lme-618f13b2` as one named regression, not as the source of the rules;
- keep deterministic/offline validation separate from optional model calls;
- never overwrite the canonical `scalar-canonical-ku78-v1-20260806` artifacts.

### Wave 2 — Central integration and verification

The orchestrator reviews every diff, resolves overlaps, and runs tests serially:

1. Menhir focused activity-aggregate tests.
2. Existing typed-scalar perception, gate, binding, fold, authority, and event-history tests.
3. Bench focused panel tests.
4. Menhir and Bench full suites when focused tests are green.
5. Ruff/static checks required by each repository.

No API calls are required for the offline gate. A live repeated panel is a separate, explicitly
named experiment after the deterministic contract is green.

## Implementation Record

Completed on 2026-08-08:

- extended the typed-scalar extraction prompt with a production-general distinction among explicit
  cumulative activity totals, individual occurrences, and recurring frequency;
- added a grounded, deterministic admission guard for modal/conditional counts, hypothetical
  deltas, single occurrences, and event-window-bound activity counts;
- kept the existing proposal, assertion, scalar slot, fold, View, Event History, persistence, and API
  contracts unchanged;
- added focused coverage for object-bound activity totals, actor-bound totals, latest-total folding,
  post-anchor deltas, corrections, frequency separation, tentative language, occurrence separation,
  spelling/grammar noise, and unrelated activity domains;
- added false-positive controls for calendar `May`, adjectival `must-read`, trailing capability
  clauses, and trailing activity qualifications;
- did not add a redundant Bench runner or mutate any canonical LongMemEval artifact. The existing
  cumulative instrument remains the generic baseline; live model extraction yield remains a
  separately named experiment rather than being conflated with deterministic parser/gate behavior.

Verification:

- `tests/test_cumulative_activity_scalars.py`: 31 passed;
- focused scalar perception/gate/binding/fold/authority plus Event History suite: 297 passed;
- Ruff passed for the changed source and test module;
- Python bytecode compilation passed for the changed source and test module;
- `git diff --check` passed for the three files in this work slice.

The repository-wide suite was attempted but could not collect in this local environment because
the optional `archolith_oauth` package and one Graphiti maintenance module are unavailable. Those
collection failures are outside this change; all directly affected and adjacent suites are green.

## Acceptance Criteria

- Explicit cumulative activity totals produce one grounded absolute count proposal.
- Two totals for the same bound object and activity share a scalar slot and fold to the newer value.
- A subsequent explicit delta contributes only after an absolute anchor.
- Individual occurrences, recurring rates, intents, approximations, and vague counts remain correctly
  separated or abstained.
- Event History behavior and event assertion counts are unchanged.
- Existing scalar identity, persistence, fold, authority, and wire contracts remain unchanged.
- The validation panel includes grammar/spelling variation and at least two activity domains unrelated
  to shoes or LongMemEval.

## Deferred Work

- Deriving advisory lower-bound counts from individually observed events.
- Reconciliation between stated aggregates and event-derived lower bounds.
- Window/epoch-aware totals such as "this month" or "since buying them" when the window must become
  part of durable slot identity.
- Promotion of any deterministic open-world activity parser; that requires separate shadow evidence.

## 2026-08-09 KU78 Follow-up Hardening

The first fresh candidate run scored 69/78 (0.885) versus the canonical 68/78 (0.872), but live
inspection found two general integration gaps behind the targeted behavior:

- object-bound activity assertions could remain `binding_pending` when the canonical object existed
  in the namespace but was not linked to the assertion's episode;
- Menhir's structured `event_authority_layer` was produced by recall and consumed by the production
  context builder, but the generic recall-only HTTP client discarded it.

The follow-up keeps the original anti-fitting invariants and adds:

1. episode-local exact binding first, followed only on failure by exact normalized-name lookup among
   non-View entities with the same `group_id`; the same unique-match binder still decides admission;
2. a constrained possessive spelling reduction (`my new X` -> `new X` -> `X`) with no fuzzy,
   substring, stem, synonym, or cross-namespace matching;
3. write-time and pending-repair use of the same optional namespace lookup seam;
4. authoritative rendering of grounded Event History leads in production context and the generic
   HTTP recall client;
5. fail-closed suppression of scalar authority, ranked memories, and timelines for unresolved event
   selection gates; and
6. an anchor-only guard for broader explicit `before I bought/purchased/got/acquired X` questions.
   The guard never chooses an answer and is silent when X resolves uniquely.

Focused verification after integration:

- Menhir changed and adjacent scalar/Event History suites: 643 passed, 11 skipped;
- focused Menhir subset: 205 passed;
- Bench Event History/client/harness suites: 78 passed, 22 skipped;
- `git diff --check`: passed.

Live narrow canaries and the next fresh KU78 comparison are recorded separately because they create
new paid/graph artifacts and must not mutate the completed v4 evidence.

## 2026-08-09 Final KU78 Result

The fresh follow-up run `scalar-event-activity-ku78-v6-20260809` completed at Menhir
`1fa57955b24f90d08550c911f26133e5b14cbb89` and Bench
`d5e97cc4fc322564c624a749e2cb25dccdf9c2ea`. It used a fresh non-resumed graph, a passing two-item
checkpoint, Event History and Event History authority enabled, deterministic scalar router/shadow
disabled, and the zero-failed-episodes-per-namespace policy.

Final integrity and score:

- 78/78 manifest rows;
- cumulative `failed_remaining=0`;
- final PENDING/ENRICHING/FAILED episode counts all zero;
- recall harness exit 0;
- Menhir recall 71/78 (0.910256, displayed 0.910), versus v4 at 69/78 (0.885) and the prior
  canonical baseline at 68/78 (0.872);
- scored Menhir arm: 117,933 input tokens, 1,376 output tokens, `$0.308592`;
- provider-reported combined usage: 17,516,332 tokens.

The seven misses were `f9e8c073`, `c4ea545c`, `e61a7584`, `a2f3aa27`, `26bdc477`,
`031748ae_abs`, and `07741c45`. Do not add broad rules for this list. Three were v4 passes with
relevant evidence still present, two reward treating approximate or planned statements as exact
current facts, and one is an unsupported synthetic role premise. The one clear deterministic defect
is `26bdc477`: both trip-count assertions were minted but remained `binding_pending` because
possessive `my camera` did not bind to the co-mentioned `Canon EOS 80D camera`. Keep that alias case
in backlog until an unrelated non-benchmark panel establishes the general pattern.

The cumulative-activity acceptance criteria in this plan are complete. Beacon integration and the
paid full-500 ingest are a separate milestone, not unfinished acceptance work here. Their canonical
follow-up is `archolith-bench/.agent/plans/beacon-view-contract-and-full500-gate-2026-08-09.md`.
That plan owns the generic View envelope, `MenhirBeaconProvider` vertical slice, project-memory
fixture, review gate, checkpointed full-500 run, and cost/provenance controls. Further KU78-specific
scalar tuning remains deferred.
