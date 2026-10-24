# Personal-memory agreement settings and replay rules

This page freezes the agreement profile that Menhir ships for the personal-memory lanes. It
compares that profile with the earlier strict profile and the LME bench profile, and it defines
what a perceiver-version change does to stored readings. `tests/test_shipped_agreement_profile.py`
pins every value below and checks that the runtime scheduler and `POST /api/phase3/run` both pass
them through.

Shipping a value is not the same as turning a lane on. Every lane and authority flag ships off.

## Shipped profile

| Setting | Value | Env var / how to change |
|---|---|---|
| Samples per turn (k), scalar and counting extraction | 3 | `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_K`; `phase3/run` also takes `k` (1-9) per request |
| Scalar agreement threshold | 2/3 (two of three samples) | `MENHIR_PERSONAL_MEMORY_SCALAR_THRESHOLD`; write `2/3`, never `0.67` |
| Scalar reconcile attribute / scope / subject | on / on / on | `..._SCALAR_RECONCILE_ATTRIBUTE`, `_SCOPE`, `_SUBJECT` |
| Scalar canonical self | off | `..._SCALAR_CANONICAL_SELF` |
| Scalar span alignment | always on | fixed in code |
| Scalar verdict min similarity | 0.0 | `..._SCALAR_VERDICT_MIN_SIMILARITY` |
| Counter lane threshold | 1.0 (unanimous) | fixed; no caller passes it |
| Counter coref / verify samples | 3 / 3, unanimous | fixed |
| Counter verify retries / sum grounding | 0 / on | `..._VERIFY_RETRIES`, `..._SUM_GROUNDING` |
| Event history | one call per batch of turns, no vote | fixed |
| Counting match-decide vote | k=3, threshold 2/3 | fixed (`domain/count_decide.py`) |
| Sampling temperature | 0.7 | fixed (`make_sync_chat`) |
| Max tokens per call | 8192 | `..._CONSOLIDATION_MAX_TOKENS` |
| Reasoning disabled | no | `..._CONSOLIDATION_DISABLE_REASONING` |
| Chat model | the global chat model | `..._CHAT_MODEL` |
| Model calls per scheduled pass | 300 | `..._CONSOLIDATION_CALL_BUDGET` |
| Pass interval | 300 s | `..._CONSOLIDATION_INTERVAL_S` |
| Perceiver version: scalar / event / counting | `v2` / `v2` / `v1` | `..._SCALAR_STATE_PERCEIVER_VERSION`, `..._EVENT_HISTORY_PERCEIVER_VERSION`, `..._COUNTING_PERCEIVER_VERSION` |
| Counting namespace lease | 900 s | `..._COUNTING_LEASE_SECONDS` |

All env vars start with `MENHIR_PERSONAL_MEMORY_`.

`POST /api/phase3/run` applies no call budget unless the request body sets `call_budget`. The
scheduled pass uses the 300-call budget.

## Comparison

| | Strict (before 2026-09-29) | Shipped | LME bench (`serve.sh`) |
|---|---|---|---|
| Scalar threshold | 1.0 | 2/3 | 2/3 |
| Reconcile attribute / scope / subject | off | on | on |
| Canonical self | off | off | off |
| Scalar lane | off | off | on (env) |
| Scalar View authority (effective only while the lane is on) | off | on | on (env) |
| Chat model | global | global | set by env |

The 2/3 profile was not imported from the bench silently. The owner chose it on 2026-09-28
(`.agent/plans/menhir-scalar-default-enablement-plan.md`), and #197 shipped it on 2026-09-29.
Measurements on the frozen LME panel:
- **Correct current Views:** 23 of 100 under the strict profile, 72 of 100 under the shipped
  flags (the settings comments).
- **Cost of 2/3** (the `gate_typed_scalars` docstring): commits rose from 64 to 298, and slots
  holding two contradictory values in one pass rose from 22 to 68.

The bench reaches its profile only through env vars. No code path changes agreement settings in
benchmark mode.

To go back to the strict profile, set `..._SCALAR_THRESHOLD=1` and the three reconcile flags to
`false`. On a namespace that already has data, also bump the scalar perceiver version (see below).

## Perceiver versions and replay

### Identity

- **Head** (`source_key`): the episode, the span offsets and the claim ordinal. It has no subject
  and no version.
- **Slot**: subject, attribute, scope, value kind and unit. Text fields are lowercased and
  stripped, and nothing else is normalized.
- **Assertion key**: the source key plus the perceiver version, the slot fields, the operation and
  the normalized value. Event assertion keys also include the predicate, the object key, `valid_at`
  and the time basis.

### What a version change does today

1. **Re-read.** Each lane's cursor treats a watermark from a different version string as reset, so
   every turn is read again. The test is string inequality, so a downgrade also re-reads.
2. **Supersede (scalar and event).** A new reading replaces the CURRENT assertion on its head only
   when its `perceiver_rank` is strictly higher. A reading at the same or a lower rank is stored as
   a superseded audit node and changes nothing. The rank is SemVer (`v2.1` > `v2` > `v1.3`). A
   version that does not parse ranks 0 and can never supersede, so scalar and event versions must
   stay plain SemVer.
3. **Re-fold.** The rebuild folds every non-superseded assertion for the subject. It retires Views
   whose slot is no longer produced, so a higher-rank reading that moves a fact to a new slot on
   the same head moves the View with it.
4. **Retire (scalar and event, #170 decision 1).** After a page is stored at a higher rank, the
   older CURRENT agent-tier readings on its turns that the new version did not reproduce are
   retired (`retired_reason` `scalar_reversion` / `event_reversion`). The keep rule is counting's
   rule B:
   - **Replaced:** a stored new reading overlaps the old one's span.
   - **Kept:** the new samples proposed something over it but nothing was stored (a gate veto), or
     a sample could not read the turn in full. For events, any parse drop on the page counts as
     not read in full.
   - **Retired:** everything else.

   The same statement raises the turn's read marker (`:TypedReadMarker`) to the new rank. Records
   lock that marker, and an agent-tier reading ranked below it is stored non-current, so a slower
   lower-version runner cannot make a reading current on that turn again. A downgrade and a
   rank-0 version retire nothing. If a retired reading's View fails to rebuild, the cursor does
   not move past the page, and the next read rebuilds it.
5. **Counting.** The counting lane compares versions for equality, so suffixed versions (`v1+x2`)
   are fine there. Prior-version occurrences stay counted until the new reading of their turn is
   stored, and are then retired. Counting reads only turns the scalar lane has finished at the
   scalar version in effect, so a scalar bump pauses counting until the scalar re-read catches up.
6. **Same version, different behavior.** Changing reconciliation, canonicalization or the threshold
   without a bump leaves the old readings current. The settings comments say to bump. Nothing
   enforces it.

### Rules for changing slot identity

Any change that can move a stored fact to a different slot needs these rules. That includes
attribute, scope, subject, unit or value-kind normalization, the reconcile flags and canonical
self.

1. Bump the lane's perceiver version to a strictly higher SemVer, such as `v2` to `v2.1`.
2. Never roll back by lowering the version. Ship the old behavior under a higher version instead.
3. Do not change span selection or span alignment with only a bump. A different span is a
   different head, so nothing supersedes the old reading.
4. The prior-version retire (step 4 above) is what makes a bump safe when the new version abstains
   on a turn or quotes a different span. Without it the old reading would stay CURRENT and in the
   fold, because `materializable_assertions_for_entity` has no version filter. It needs strictly
   higher SemVer ranks, which is rule 1.

A version bump costs a full re-read of every namespace with the lane on: k calls per turn,
limited by the per-pass budget. #334 measures backlog recovery.

## Known divergences (not changed)

- The `getattr` fallbacks in `core/runtime.py` and `api/routes_handlers.py` still use the strict
  values (threshold 1.0, reconcile off). The `MaintenanceScheduler` dataclass also defaults the
  event and scalar perceiver versions to `v1`. None of these apply when the settings object is
  `MemorySettings`. The freeze test checks both entry points with real settings.
- `gate_typed_scalars` itself defaults to threshold 1.0 with reconcile off. Every production caller
  passes the settings values.
- `.env.example` has no entry for the scalar perceiver version.
