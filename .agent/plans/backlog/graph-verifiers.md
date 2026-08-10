# Graph-native verifiers — keep derivable beliefs fresh against their source of truth

Status: **PARTIAL / ACTIVE** (kept — not archivable). Core mechanism + scheduler wiring + seeding
are shipped and live; the recall-side payoff is not yet wired.

> **Status note 2026-07-11 (code-reconciled).** Verified against `src/menhir`:
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
4. **Recall integration** — teach recall to surface "value from register X, verified Ns ago" and to
   down-rank `needs_review` prose, so flagged beliefs stop being asserted as current truth.
5. **Seeding** — a small bootstrap that upserts the standard config/status verifiers on startup.
