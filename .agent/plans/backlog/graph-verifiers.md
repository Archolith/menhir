# Graph-native verifiers — keep derivable beliefs fresh against their source of truth

Status: **PARTIAL / ACTIVE** (kept — not archivable). Core mechanism + scheduler wiring + seeding
are implemented; ranked recall/context now consume review flags as explicit source-verification advisories.

> **2026-10-05 reconciliation (#173):** the historical unwired-consumer finding below was reproduced
> and fixed. Review flags survive ranked recall, compact/full MCP, REST and context with an atomic
> warning. Namespace-scoped bindings and write fences prevent cross-silo drift flags. Invalid source
> values are unavailable. Scores/content and default settings remain unchanged. This does not certify
> prose as current, measure useful answers retained, or qualify deployment/default activation.
> Linked register evidence now exposes the last successful value/time and distinct latest-probe status. Additional executor kinds and string registers are optional
> product decisions, not prerequisites for the bounded `env_key` path.

> **Historical status note 2026-07-11 (superseded by the reconciliation above).** Verified then against `src/menhir`:
> - DONE & live: `verifier_sync.py` + `verifier_repository.py`; scheduler wiring
>   (`verifier_sync_interval_s`/`MENHIR_VERIFIER_SYNC_ENABLED` in `settings.py:193,394`,
>   `runtime.py:216`); standard-verifier **seeding** (`verifier_sync.py:203-220`) — so the top-line
>   "scheduler wiring is the next step" and tail item **#5 (seeding)** are both stale/DONE.
> - REMAINING (why this stays active): only the `env_key` executor exists (#2 `http_status_field`/
>   `file_fingerprint` and #3 string-valued registers absent); and **#4 recall integration is
>   NOT wired** — `needs_review`/`review_reason` are written on drift but not consumed in
>   `recall_service`/`scoring_service`, so flagged prose is not yet down-ranked. #4 is the
>   load-bearing gap: the flag is produced but nothing reads it.

## Problem
### Recorded-conflict guard handoff (#173, 2026-10-05)

Why: the offline panel found that persisted unresolved conflicts reach scoring/context but not
the optional contradiction guard. Pass a bounded `recorded_conflict` signal into WardenContext
from candidate metadata when a conflict group exists and its status is exactly `unresolved`.
ContradictionWarden consumes it alongside existing oracle/belief signals: current intent refuses;
historical intent retains with a conflict label. Resolved, absent and unknown status do not invent
a contradiction. Other guards still apply and may independently refuse historical memories.
Keep master-gate and optional-guard dependencies, ranking, default labels and settings unchanged.
Alternative: add a default conflict oracle (rejected here because it also changes ranking and the
base admission profile). No new graph fields, migration, write path, ingest or paid qualification.
Validate the real assertion producer with guard on/off and current/historical queries; expand the
offline recall panel with resolved and historical conflict controls. Run focused guard/recall/context
neighbors and exact-head CI. Remaining default/corpus/usefulness gates stay open.

### Offline Warden safety controls (#173, 2026-10-05)

`tests/test_warden_safety_matrix.py` runs the actual recall/guard path against existing stub
adapters. Nine authored controls cover anchored and conversation-only valid memories, unknown
provenance, wrong project, expired current and historical claims, and unresolved/resolved/historical conflicts.
Gold means useful or harmful **for the query's assertion context**; unknown evidence is scored
separately. Each single-candidate pool and score stays fixed across Warden-off, strict (anchor on),
and conversational (anchor off) profiles. Ranking/facet lanes and shadow execution stay off.
Currentness and contradiction options are tested independently and together: 12 runs / 108 decisions.
Retained candidates keep the Warden-off final score.

The report counts useful refusals, harmful refusals, harmful returns with/without warnings, and
unknown returns/refusals. Negative controls prove both error counters detect injected failures.
Warnings include actual Warden labels, the existing context conflict marker, and dated expired
facts; a warning is never counted as a refusal. Execution receipts must match the bounded run.
Run `pytest tests/test_warden_safety_matrix.py -o junit_family=legacy --junitxml=warden.xml`;
the `warden_safety_report` JUnit property contains JSON after pytest removes scratch files.

Observed controls: strict refuses one useful conversation-only memory; conversational preserves
all five useful fixtures. Both gated profiles refuse the wrong-project fixture. Currentness alone
returns the strongly anchored expired-current fixture labeled historical; enabling both optional
guards refuses it while preserving historical recall. The initial seven-control panel in #228 exposed
the recorded-conflict producer gap. The handoff fix above now refuses unresolved current conflicts
when master and contradiction guards are on, preserves historical conflicts with a label, and leaves
resolved conflicts and guard-off behavior intact. These are bounded controls, not zero-error
qualification. Default activation, answer usefulness, corpus accuracy and deployed enforcement
remain unqualified. No new production policy, ingest, model calls or paid bench is part of this panel.

### Optional Warden execution status (#173, 2026-10-05)

Why: configured guards must not imply applied enforcement after an exception or on pending fallback
results. Detailed diagnostic output should not add tokens to ordinary successful recall.
Scope: ranked Entity recall, pending fallback, provenance/temporal/staleness metadata degradation,
canonical runtime/client/API/MCP and context, plus Recall Lab. No guard policy/ranking/default changes,
new store, deployed probe, ingest or paid qualification.
Design: per-call `include_warden_status=False`; a structured execution receipt reports configured
chain options, actual state/counts, metadata gaps and explicitly excluded result types. Produce the
receipt from the current request's application path, never inferred from settings alone. Retain only
a brief `warden_notice` by default for configured-but-unapplied, failed or incomplete coverage.
Context packs that notice before ranked memories; insufficient budget suppresses those memories.
Requested context diagnostics share the token budget. Recall Lab requests diagnostics and shows them
in its existing details panel. Source/authority/TODO layers retain their existing independent rules.
Alternative: always include diagnostics (rejected for token cost); change fallback policy (deferred
because it changes admissions and requires usefulness evidence).
Risks: status must survive all early returns; metadata absence must not be confused with successful
checking; optional flags must reach remote and direct callers without altering older default calls.
Invariants: visibility does not affect admissions, scores, labels or graph writes; failures never claim
applied enforcement; no shared mutable per-request status. No data migration/atomic store is involved.
Validation: direct positive/negative controls for applied/disabled/computed-not-applied/error/pending,
metadata failure and missing rows; API/MCP/runtime/client flag-off omission and opt-in round trips;
context budgets and Recall Lab diagnostics/redaction; focused neighbors and exact-head full CI.
Docs: existing owner/index, data/API/default-off docs and changelog. Live enforcement remains unproven;
this bounded receipt is diagnostic evidence, not a claim of universal enforcement.

### Freshness evidence follow-up (#173, 2026-10-05)

Expose the existing last successfully verified register value/time beside linked ranked memories,
and persist a separate latest-probe time/status. Begin each probe as pending; only a completed
register/link/success-stamp sequence records success. Unavailable, unknown-kind and failed probes
must retain the previous successful value/time. A crash leaves an explicitly incomplete probe.
Read same-silo `REFERENCES` → counter register → trusted binding evidence in the existing candidate
metadata query, deduplicate bindings, and retain typed evidence through MCP/REST/context. Missing
legacy probe stamps are unknown, not inferred successes. Use absolute UTC timestamps; no arbitrary
freshness cutoff, score/rank change, review-marker clearing, source secrets, or default activation.

Test changed/unchanged, failed/unavailable/unknown, first-failure without any success, restart, token
budget, namespaces/legacy spellings, duplicate superseded register links and actual disposable graph
queries. Preserve flag-before-write ordering. Concurrency/mixed-version writers and external graph
mutation remain outside this bounded proof; successful register observations do not validate prose.
Update data/API/default-off docs, the existing backlog owner/index, changelog and strict domain
contract. No ingest, paid runs or deployed probes.

### Bounded #173 implementation plan (2026-10-05)

Trace the existing `env_key` observation → counter refresh → `REFERENCES` review flag →
ranked recall/context path. Surface `needs_review`, `review_reason`, and `review_flagged_at`
with an explicit instruction to verify the source before asserting current truth. Preserve
content and ranking: a review flag indicates uncertainty, not proven contradiction; an arbitrary
score penalty or refusal would need separate usefulness evidence. Keep sync and all defaults opt-in.

Scope verifier identity and register writes to the binding's persisted namespace. Refuse cross-silo
reference/verification edges and flag only same-silo non-derived beliefs. Preserve existing scoped
bindings; legacy rows without namespace use the explicit sync namespace. Invalid boolean/type
observations must be unavailable rather than freshly recorded false. Retain flag-before-write retry
ordering. Test changed/unchanged/unavailable/unknown-kind, restart, namespace separation, derived
register exclusion and actual recall/context plus compact/REST output. Disposable graph behavior
belongs to CI; no production graph, ingest, paid run, new executor, or default promotion in this pass.

Risks: persisted cross-silo edges require read-side fences; review markers remain until explicit
correction (an unchanged probe cannot validate prose). Raw episode sources and independent authority
layers are outside the linked-Entity contract. Freshness evidence is now implemented below; measured down-ranking remains
separate acceptance work. Update this owner, the routing index, data/API docs and changelog.

The sync write sequence is not a transaction. Existing flag-before-write tests prove retry ordering
for already-linked beliefs; concurrent reference creation or competing register writers remain an
assumption, not a system-wide freshness guarantee. External/manual graph writers are outside this
repository census. No stored data is backfilled or deleted; reverting the reader change restores the
previous display behavior while persisted review markers remain available.

Supersession / contradiction detection only fire when a *new* memory is written. If the world
changes and nothing writes a correcting memory, a belief silently stays "current" and wrong
(observed this session: an "experience-counter job is paused" belief survived after the job was
re-enabled). The only real cure is scheduled **re-observation** of the source of truth, wired to
exactly the nodes it governs.

## Design (chosen: separate verifier node + fan-in edges)
- **Verifier node** — `(:Entity {is_verifier:true})` holding a *binding*, not code:
  `verifier_kind`, `verifier_params` (JSON), and the register coordinates it maintains
  (`register_subject`, `register_counter`). Executable logic lives in a **trusted in-code registry**
  (`verifier_sync.DEFAULT_EXECUTORS`); an unknown kind is **skipped, never executed** — the graph
  never carries runnable code (no eval/injection surface).
- **Register** — the value it confirms is a supersedable counter View:
  `(register)-[:VERIFIED_BY]->(verifier)`. Re-deriving just re-records the counter, which
  self-supersedes on value change (deterministic — same key).
- **Fan-in** — free-text beliefs `(belief)-[:REFERENCES]->(register)`. Many nodes rely on one
  verifier. On a value change they are flagged (`needs_review`, `review_reason`) so recall can
  down-rank prose that now restates a stale value, instead of asserting it as current truth.

## Sync loop (`sync_verifiers`, graph-driven)
```
for each (v:Verifier):
    executor = registry[v.kind]            # unknown -> skip (never execute)
    res = executor(v.params, context)      # read live source of truth
    if not res.ok: continue                # unreadable -> leave register untouched
    record_counter(v.register_subject, v.register_counter, res.value)   # self-supersedes on change
    ensure (register)-[:VERIFIED_BY]->(v); stamp v.last_verified_at
    if changed: flag beliefs that REFERENCE the register  (needs_review)
```

## Files
- `src/menhir/services/verifier_sync.py` — `VerifierResult`, `VerifierContext`, executor registry
  (`env_key` built-in), `sync_verifiers` core.
- `src/menhir/infrastructure/verifier_repository.py` — Neo4j: `upsert_verifier`, `link_reference`,
  `list_verifiers`, `ensure_verified_edge`, `stamp_verifier`, `flag_referencing_beliefs`.
- `tests/test_verifier_sync.py` — 8 unit tests (executor coercion, changed/unchanged, unknown-kind
  skip, unreadable-source no-op, one-broken-probe isolation).

## Live proof (this session, then cleaned up)
Seeded an `env_key` verifier for `MENHIR_EXPERIENCE_COUNTER_ENABLED` -> register
`menhir-config.experience_counter_enabled`. First sync derived `true` (1.0), recorded the register,
linked `VERIFIED_BY`. Simulated a silent drift (toggle -> false) with **no memory write**: re-sync
superseded the register to 0.0 and flagged the referencing belief
(`review_reason="verifier value changed to false"`).

## Scheduler wiring — DONE (2026-07-06, `1ccc230`), LIVE on this box
`sync_verifiers` runs on `MaintenanceScheduler` every `verifier_sync_interval_s` (300s) behind
`MENHIR_VERIFIER_SYNC_ENABLED` (default off; **true** in this box's `.env`). `runtime._start_scheduler`
builds `VerifierRepository` + `VerifierContext(settings)` and seeds the standard verifiers on startup.
Verified live after restart: 3 verifiers seeded (experience_counter_enabled, structure_watcher_enabled,
api_port), first run `{verifiers:3, refreshed:3, changed:3}`, registers hold 1.0/1.0/8090. Job registers
only when enabled AND a repo is present, so default behavior is unchanged.

## Next steps (not done)
2. **More executor kinds** — `http_status_field` (e.g. scheduler running from `/api/stats`),
   `file_fingerprint` (reuse the structure-scan pattern). Keep each in the trusted code registry.
3. **String-valued registers** — current registers are numeric (bool->1/0, int). A typed/string
   register View kind would let verifiers maintain non-numeric config (URIs, model names).
4. **Recall integration — partial:** review advisories and linked-register freshness evidence are
   consumed. Measured usefulness/down-ranking remains separate work; neither evidence nor a review
   marker verifies prose. Freshness validation: 381 focused tests pass, with 7 graph tests reserved
   for disposable CI; no ingest or paid qualification.
5. **Seeding — implemented:** idempotent bootstrap upserts standard config/status bindings.
