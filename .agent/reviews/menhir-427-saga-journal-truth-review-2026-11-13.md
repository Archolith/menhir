# Adversarial correctness review: `fix/427-saga-journal-truth` (#427, #404, #406)

Date: 2026-11-13. HEAD `035ecc81`, diff `git diff origin/main...HEAD`. Reviewer lane: recall-maximizing.
Nothing here has been through an orchestrator verification pass yet. No code was edited. The repro
script lives outside the repo at
`C:/Users/thron/AppData/Local/Temp/claude/0f5969dc40e57058/17d95120-6a4d-4f3f-8c7a-f868a95d358d/scratchpad/repro_427.py`.
It uses temp SQLite files only and stubs the telemetry recorders. `git status --short` at the end of
the review shows only `?? .agent/reviews/menhir-427-saga-review.md`.

## Executive summary

I read all 20 files in scope (8,141 lines by `wc -l`), plus the code they call into:
`neo4j.py` `execute`, `correlation_queries.merge_entity` / `restore_merge_snapshot`,
`consolidation_queries.delete_entities_returning_uuids` and `bridge_and_delete`,
`view_write_repository._write_version`, `metric_receipts`, the dispatcher routing, the gate,
preflight and `build_memory_services`.

The core mechanism holds on every interleaving I could build:

- Per-claim tokens, the required-token CAS and the `BEGIN IMMEDIATE` escalation behave as designed.
- `server_rolled_back` + `prove_before` is sound for the five single-statement mutations. A Grep-tool
  search for `safe_to_reexecute=True` in `src` returned no matches, so a retried `TransientError`
  always follows rolled-back attempts.
- The private `_reconcile_sweep` impersonation has no production caller.
- Metric FAILED rows do not corrupt the receipt chain (see property 2).

Found: 4 Medium and 15 Low. Three were reproduced by executing branch code (F1, F2, F3).

The most important is **F1**. The new merge abstain branch writes COMMITTED ("another owner already
applied this very op") using a fingerprint that deliberately excludes the op marker, and it does so
proactively under its own token. The plan only allows success after a *refused* `mark_needs_review`
finds the row COMMITTED by another owner. So a merge this op never performed can be journaled
COMMITTED, and `merge()` returns `merged: 1`. The code defect is reproduced. How reachable the
production trigger is remains unproven (OQ1).

## Findings

### F1 -- Medium -- merge abstain branch commits a merge this op never performed
- **Location:** `src/menhir/services/merge_coordinator.py:412-421`; fingerprint at `:127-135`.
- **Mechanism:**
  - The branch reads:
    ```
    if expected_after and post_fp == expected_after:
        # Another owner already applied this very op (the survivor carries its marker), so
        # this writer did no graph work.
        settled = commit_or_settle(self.journal, op_id, owner_token=owner_token, after_graph_work=False, ...)
    ```
  - `merge_state_fingerprint` hashes only `survivor_present`, `absorbed_present` and
    `lineage_recorded`. Its own docstring (`:118-123`) says "`last_merge_op_id` is deliberately NOT
    fingerprinted". So `post_fp == expected_after` means only "absorbed is absent and its uuid is in
    `survivor.merged_from`". It does not mean "this op applied", and the comment's "carries its
    marker" is never checked.
  - **Plan deviation:** plan lines 174-177 say that "that branch's refused `mark_needs_review`
    settles as success when `post_fp == expected_after`". Success is allowed only after the row has
    *already* been moved by another owner (the refusal proves a real replay committed it). The code
    instead calls `commit_or_settle` first. When the row is still PREPARED under this writer's token,
    `mark_committed` simply succeeds, and nothing else proves the work was done.
- **Trace:**
  1. **Stale lineage precondition, from code:**
     - `merge_entity` carries the absorbed node's own lineage forward:
       `survivor.merged_from = coalesce(survivor.merged_from, []) + [$absorbed_uuid] + coalesce(absorbed.merged_from, [])`
       (`correlation_queries.py:770-771`).
     - Restore removes only the one uuid:
       `SET s.merged_from = [x IN coalesce(s.merged_from, []) WHERE x <> $absorbed]` (`:426`).
     - Sequence: merge A into B (B.merged_from=[A]); merge B into S (S.merged_from=[B, A]); unmerge
       B from S (S.merged_from=[A]); unmerge A from B (A exists again). S still lists A, so the
       before-state for a new S <- A merge is `(T, T, T)`.
     - Eligibility does not reject this. `merge_eligibility.py:37-46` has no lineage reason code, and
       a Grep for `merged_from` in `domain/merge_eligibility.py` returned no matches.
     - Nothing forces unmerges to run in LIFO order. A Grep for `merged_from|LIFO|NOT_LATEST` in
       `unmerge_coordinator.py` returned 0 matches. I did not trace the restore's own Cypher
       preconditions beyond `:420-430`, so this chain is plausible, not executed.
  2. Op X PREPAREs S <- A.
  3. A is deleted by a path that does not consult `graph_operation_locks` while X is PREPARED.
     - The one candidate I found is decay: `lifecycle_decay.py:368` calls
       `graph_adapter.bridge_and_delete`, which runs `DETACH DELETE n` (`consolidation_queries.py:391-421`).
     - A Grep for `graph_operation_locks|participant` in `lifecycle_decay.py` and
       `consolidation_queries.py` returned only an unrelated docstring at
       `consolidation_queries.py:908`. Decay's only lock is the in-process `_decay_lock`
       (`lifecycle_decay.py:89-93`).
     - Its Cypher guard, `harmful_automatic_mutation_allowed_cypher` (`consolidation_queries.py:51-58`),
       composes `automatic_lifecycle_protection_cypher` (`:33-47`: non-derived, not retained by a live
       FACT View) and `destructive_retention_allowed_cypher` (`domain/retention.py:41-48`: not
       user-flagged, not source-retention-protected). Neither looks at in-flight saga operations.
     - This contradicts `delete_coordinator.py:66`: "The sanctioned entry point for physical deletion
       of Entity nodes."
     - Decay deletes only COMPRESSED candidates (`fetch_decay_candidates(FreshnessState.COMPRESSED, ...)`,
       `lifecycle_decay.py:352-358`), while eligibility needs A to be ACTIVE at PREPARE
       (`_MERGEABLE_FRESHNESS`, `merge_eligibility.py:57`). So
       A must be compressed *and* deleted inside X's PREPARED window. The erasure deletes of derived
       entities are fenced through `fence_uuids` participant locks (`erasure_coordinator.py:179,685-686`).
       Reachability is therefore narrow and unproven (OQ1).
  4. `merge_entity` abstains because A is missing.
  5. `post_fp` is `(T, F, T)`, which equals `expected_after`.
  6. `commit_or_settle(after_graph_work=False)` succeeds under X's own token, and `merge()` returns
     `{"merged": 1, "replayed": True}`.
  7. On `origin/main` this path went to `mark_needs_review` (the diff hunk at `:422`).
- **Executed repro (`scenario_merge_abstain_commits`, which fakes eligibility and the delete):**
  ```
  F1 merge result: {'merged': 1, 'replayed': True, 'op_id': '22da1b43962e4ee1b7af56af782b6b1d'}
  F1 row state: COMMITTED commit_graph_work: 0 graph last_merge_op_id: some-older-op != op 22da1b43...
  ```
- **Impact:**
  - The journal says op X merged A into S. X's snapshot becomes the "inverse" an unmerge would
    restore, which would recreate A from a snapshot even though A was deleted by decay.
  - `correlation_service.py:204-225` logs MERGED and fires `on_merge_committed` for a merge that
    never happened.
  - The same weak fingerprint drives `WOULD_MARK_ALREADY_APPLIED` (`:266-267`, existing code), and
    the new operator `resolve` COMMITTED path (`saga_operator.py:178-179`) inherits it.
- **Fix:** implement the plan as written. In the abstain branch, when `post_fp == expected_after`,
  call `mark_needs_review`; on `StaleTransitionError`, use `settle_refused_commit(..., did_graph_work=False)`,
  which returns success only when the row is COMMITTED by another owner. A row still PREPARED under
  this writer's token then goes to NEEDS_REVIEW.
  - Cheaper alternative: add the conjunct `post.get("last_merge_op_id") == op_id`. A later merge
    overwriting the stamp then fails closed (NEEDS_REVIEW).
  - Apply the same conjunct in `_classify_replay` before `WOULD_MARK_ALREADY_APPLIED`, at least for
    the operator `resolve` COMMITTED action.

### F2 -- Medium -- `resolve --apply` re-strands the row if classification or a mark raises after the claim
- **Location:** `src/menhir/services/saga_operator.py:171-187`.
- **Mechanism:** after `claim_abandoned_operation` succeeds (`:173`), nothing catches an exception
  from `handler.classify_prepared_row(...)` (`:175`) or from the mark calls (`:178-183`).
  - The claim has already cleared the attestation (`graph_operations_ownership.py:140-141`) and
    moved ownership to the CLI process's claim token, with a fresh lease.
  - The gate is released in `finally` (good), but the exception propagates through Typer with a
    traceback. The new claim token is never printed: the `report.update(... claim_token=claim)` at
    `:184` is skipped.
  - The handlers catch narrowly, so a Neo4j outage does raise here: merge
    (`merge_coordinator.py:310`), delete (`delete_coordinator.py:282-300`, with the comment "A broad
    `except Exception` would fold a transient graph outage into ...") and metric.
- **Executed repro (`scenario_resolve_raises_after_claim`):**
  ```
  F2 resolve raised: ConnectionError neo4j unavailable during re-classification
  F2 after: state PREPARED owner_token is CLI claim: True attested_by: None gate held: False ownership: LIVE_OWNER
  ```
- **Impact:**
  - The row is still stranded, now owned by an exited CLI process.
  - It reads LIVE_OWNER until the lease lapses, then OWNER_UNKNOWN. ABANDONED is reachable only
    with `MENHIR_HOST_PID_NAMESPACE_VERIFIABLE` (`operation_owner.py:250`), since the CLI's PID is
    dead.
  - In `live` startup mode that blocks boot again (#427's original symptom).
  - The operator must find the new token with `saga show` and attest again. The runbook does not
    cover this.
- **Fix:** wrap `:175-185` in `try/except Exception`. On failure:
  - Best-effort `journal.mark_needs_review(op_id, owner_token=claim, observed_error=f"operator {by}: resolve failed after claim: ...")`.
    The claim is still held, so this is legal.
  - Return a refusal carrying `claim_token`.

### F3 -- Medium -- `menhir saga clear-review --to FAILED` can close an erasure as "no mutation"
- **Location:** `src/menhir/services/saga_operator.py:203-207`;
  `src/menhir/infrastructure/graph_operations_transitions.py:219-247`.
- **Mechanism:**
  - The plan (section 6, and section 3: "Erasure is excluded ... FAILED would be false") says an
    erasure is never FAILED, and `resolve` enforces it (`saga_operator.py:137-142`).
  - `clear_review` / `clear_needs_review` has no kind check. Erasure rows do reach NEEDS_REVIEW:
    `erasure_coordinator.py:521-526` (forward RESIDUAL_CONTENT, `mark_needs_review`) and `:641-646`
    (replay of a WOULD_NEEDS_REVIEW row, `mark_needs_review`). Replay at `:704-709` only *reports*
    DRIFTED for a residual result; the transition there is the `:521` call inside `_erase_and_verify`.
  - FAILED is in `_LOCK_RELEASE_STATES`, so the transition also deletes the erasure's participant and
    `fence_uuids` locks (`graph_operations_transitions.py:519-522`).
- **Executed repro (`scenario_clear_review_erasure_failed`):**
  ```
  F3 clear_review erasure -> {'ok': True, ..., 'to_state': 'FAILED', 'operator': 'alice'} state now: FAILED
  ```
- **Impact:**
  - A partially executed erasure (graph nodes deleted, sidecar purge irreversible) is recorded as
    "made no graph mutation".
  - Its fence is released, so other ops can touch the fenced uuids again.
  - Before this branch the API had no CLI, so this is a new operator-reachable path in a
    compliance-critical lane.
- **Fix:**
  - In `saga_operator.clear_review`, read the row and refuse `to_state == "FAILED"` when
    `operation_kind == ERASURE_KIND`.
  - Optionally enforce the same in `clear_needs_review` with an explicit
    `allow_erasure_failed: bool = False`.

### F4 -- Medium -- a refused `mark_needs_review` after this writer's own graph work is never settled or escalated
- **Location** (each a `mark_needs_review` that runs after this writer's mutation returned success,
  with no `settle_refused_commit`):
  - `merge_coordinator.py:440-449`
  - `unmerge_coordinator.py:488-497`
  - `legacy_unmerge_coordinator.py:259-265`
  - `metric_write_coordinator_reconcile.py:268-277`
  - `delete_coordinator.py:236-243`
  - `erasure_coordinator.py:521-526`: the RESIDUAL_CONTENT `mark_needs_review` runs after this
    writer's graph delete and sidecar purge. If a replaying R has already committed the row, the
    refusal raises and the residual-content evidence is never journaled.
- **Mechanism:**
  - Plan lines 160-162: "`settle_refused_commit` ... is called wherever a writer's `mark_committed`
    or `mark_needs_review` raises `StaleTransitionError`". The helper's docstring
    (`saga_writer_outcomes.py:106-107`) says the same.
  - A Grep for `settle_refused_commit|escalate_after_refused_commit` in `src` finds only
    `saga_writer_outcomes.py:128,149,177` and the definition in `graph_operations_transitions.py`.
    So the helper is reached only through `commit_or_settle`, which wraps `mark_committed` alone.
  - When the after-state check fails and another owner has already FAILED the row, the refused
    `mark_needs_review` raises `StaleTransitionError` (`REFUSAL_ILLEGAL_TRANSITION`,
    `graph_operations_transitions.py:510-513`).
  - Nothing is escalated, and nothing durable is recorded: `_record_commit_lost` runs only inside
    `mark_committed` (`:118-126`).
- **Trace.** The premise is the #404 stalled writer. P's lease lapsed and P was attested (wrongly)
  or judged a dead PID while P was still alive. `saga_writer_heartbeat.py:20-23` says recovery
  "requires positive evidence that the writer process is dead", so this needs that evidence to be
  wrong.
  - **Variant A** (repo code plus read-committed visibility only):
    1. P dispatches `merge_entity`. The statement takes write locks on S/A, stays uncommitted, and
       stalls.
    2. The operator runs `resolve --apply`. `classify_prepared_row` reads the committed graph, which
       is still the before-state, so `_planned_close` picks FAILED and `saga_operator.py:180-181`
       marks FAILED. FAILED releases the locks.
    3. P's statement commits. P's after-state read differs from `expected_after`: that needs a
       third concurrent change, or for delete or metric another writer on the same key.
    4. P calls `mark_needs_review`. It is refused with `illegal_transition` (FAILED -> NEEDS_REVIEW)
       and raises `StaleTransitionError`.
    5. Result: FAILED over a mutated graph, recorded only as an exception to the forward caller.
  - **Variant B** (rests on assumed Neo4j behaviour, not verified):
    1. R claims the row and re-runs `_apply_owned`.
    2. R's `merge_entity` blocks on P's locks and fails with an error that `server_rolled_back`
       accepts (`saga_writer_outcomes.py:35-47`). I assume a lock-acquisition timeout surfaces as a
       `TransientError` or a transaction timeout as a `ClientError`; nothing in the repo confirms
       this.
    3. R's `close_after_failed_mutation` (`merge_coordinator.py:384-393`) proves the before-state
       and marks FAILED.
    4. P then proceeds as in steps 3-5 of Variant A.
  - The defect itself does not depend on either variant. The plan requires the settle, and the
    six sites do not call it.
  - This breaks plan invariant 4: "A refused commit after graph work never leaves FAILED over a
    mutated graph unrecorded". It needs two independent races plus a false or premature
    ABANDONED, so it is rare.
- **Fix:** route the six sites through a helper. On `StaleTransitionError`, call
  `settle_refused_commit(..., did_graph_work=True, observed=<mismatch text>)` and re-raise; the
  FAILED -> escalate branch then covers it. Otherwise, correct the plan and the docstring.

### F5 -- Low -- dispatcher reports LIVE_OWNER (non-blocking) for this process's own attested row
- **Location:** `src/menhir/services/saga_reconcile_dispatcher.py:302-308`;
  `src/menhir/infrastructure/graph_operations_ownership.py:116-120`.
- **Mechanism:**
  - `claim_abandoned_operation` now refuses any row whose token is the same *process* as the
    claimant (`same_process`).
  - `_replay_one` maps every refused claim to `LIVE_OWNER` without the `own_claim` flag. Only
    `own_claim` LIVE_OWNER rows block readiness (`:384-393`).
- **Trace:**
  1. Startup attempt 1 in process P claims row X with a P-minted claim token Tc. Its replay returns
     FAILED, so X stays PREPARED with Tc.
  2. The lease expires, and an operator (wrongly) attests Tc, so `classify_ownership` returns
     ABANDONED.
  3. Attempt 2 runs in the same PID. `_replay_one` sees ABANDONED, the claim is refused by
     `same_process`, and it returns `LIVE_OWNER, {observed_error: ...}`.
  4. `write_ready` is True while X is still PREPARED.
  - New with claim tokens: before, no P-minted claim token distinct from the process token existed.
  - It needs a false attestation, which the runbook forbids.
- **Fix:** on a refused claim, re-read the row. Return `own_claim=True` (blocking) when
  `oo.is_own_claim(row)`, or return STALE.

### F6 -- Low -- delete reports `after_graph_work=True` even when it deleted nothing
- **Location:** `src/menhir/services/delete_coordinator.py:245-248`.
- **Mechanism:**
  - The plan (section 4) defines graph work for delete as "`deleted_uuids` is non-empty".
  - The code passes `True` unconditionally. `deleted` can be `[]` when every target was filtered by
    scope or retention protection (`consolidation_queries.py:576-588`), with `skipped` equal to all
    targets.
- **Trace:**
  1. P's DETACH DELETE matches nothing (all targets were promoted out of SESSION scope).
  2. Meanwhile an attested-row operator `resolve` saw all targets present and marked FAILED.
  3. P's commit is refused, and `settle_refused_commit` takes FAILED + `did_graph_work` -> escalate.
     A correct FAILED is quarantined to NEEDS_REVIEW and its fence re-taken.
- **Fix:** `after_graph_work=bool(deleted)`.

### F7 -- Low -- erasure always reports `after_graph_work=True`, so a forward/replay overlap escalates a completed erasure
- **Location:** `src/menhir/services/erasure_coordinator.py:546-552`. This one call site serves the
  forward path (`:334-341`) and replay (`:688-698`).
- **Mechanism:**
  - The plan says an erasure did graph work only if it "deleted or purged anything".
  - The code passes `True` even when `graph_deleted == 0` and `result.rows_affected` sums to 0.
  - If P (forward) and R (replay) both run, R commits `commit_graph_work=1`. P's settle then takes
    "COMMITTED by other, both did work" -> escalate.
  - Result: a finished, idempotent erasure becomes NEEDS_REVIEW with re-taken locks (target_key
    fenced in `ux_graph_ops_unresolved_key`), and P's caller gets `StaleTransitionError`.
- **Fix:**
  - Compute `after_graph_work = bool(graph_deleted) or sum(result.rows_affected.values()) > 0`.
  - Consider treating an erasure "double apply" as benign in `settle_refused_commit`, since its
    deletes are idempotent.

### F8 -- Low -- the unmerge hook fires twice under COMPLETED_BY_OTHER_OWNER
- **Location:** `src/menhir/services/unmerge_coordinator.py:429-442` and `:499-506`.
- **Mechanism:** replay fires `_fire_unmerge_hook` too, through `WOULD_MARK_ALREADY_APPLIED` or the
  normal commit, both inside `_apply_owned`. The forward writer that settles
  COMPLETED_BY_OTHER_OWNER then fires it again.
- **Trace:**
  1. P's restore lands, then P stalls.
  2. R classifies ALREADY_APPLIED, commits with `graph_work=0`, marks REVERSED and fires the hook.
  3. P's commit is refused, and settle takes "committer did no graph work" -> success.
  4. P marks REVERSED (an idempotent no-op) and fires the hook again.
- **Impact:** latent. The hook is not wired in production: `git grep on_unmerge_committed` finds only
  the field and the call in `unmerge_coordinator.py:95-112`, and `saga_preflight.py:58` constructs
  `UnmergeCoordinator(graph_adapter=..., journal=...)`. Merge is fine: replay never fires
  `on_merge_committed`.
- **Fix:** skip `_fire_unmerge_hook` when `settled == COMPLETED_BY_OTHER_OWNER`, or document the
  hook as required to be idempotent.

### F9 -- Low -- operator `resolve` of an already-applied unmerge never marks the forward merge REVERSED
- **Location:** `src/menhir/services/saga_operator.py:178-179`, compared with
  `unmerge_coordinator.py:435`.
- **Mechanism:** `resolve` closes an ENTITY_UNMERGE `WOULD_MARK_ALREADY_APPLIED` with a bare
  `mark_committed`. The coordinator's own path also calls `_mark_merge_reversed` and the hook.
- **Impact:** the forward ENTITY_MERGE row stays COMMITTED while the graph is unmerged. A later
  `unmerge(merge_op_id)` returns `ALREADY_RESTORED`, but the journal still says the merge stands.
- **Fix:** for `ENTITY_UNMERGE`, after the COMMITTED close, call
  `journal.mark_reversed(request["merge_op_id"], owner_token=claim)` (best effort).

### F10 -- Low -- mixed versions: three old-binary behaviours escape the new guarantees
- **Location:** `graph_operations_transitions.py:491-502`, and the `origin/main` versions of
  `clear_needs_review` / `mark_reversed`.
- **(a)** `different_owner = resolved_by is not None and resolved_by != token`.
  - A COMMITTED row with `resolved_by_token IS NULL` therefore makes a new writer's `mark_committed`
    an idempotent success.
  - Such a row comes from a pre-CF-271 tokenless binary's commit, or from an old
    `clear_needs_review` -> COMMITTED.
  - The success comes with no settle, no escalation and no `commit_graph_work` record, even when the
    new writer did graph work.
- **(b)** On `origin/main`, `clear_needs_review` accepts `to_state="PREPARED"` and is tokenless.
  - Run by an older CLI against the shared DB, it still creates ownerless PREPARED rows.
  - The new `_transition` keeps the `owner_token IS NULL` exemption (`:473`), so any token can then
    move those rows.
- **(c)** An older dispatcher still maps `StaleTransitionError` to `LIVE_OWNER, stale` (the
  `origin/main` lines replaced in the diff).
- **What holds:**
  - An old reconciler's commit leaves `commit_graph_work` NULL, which the new settle treats as "did
    graph work" (fail closed).
  - An old claimant inserts no `graph_operation_claims` row, which only matters for that claimant's
    own escalation (refused, fail closed).
  - The claim token keeps 4 colon parts, so the old `_owner_parts` still parses host and PID.
- **Fix:** document (a)-(c) in the plan's mixed-versions risk. For (a), treat `resolved_by IS NULL`
  on COMMITTED as `REFUSAL_COMMITTED_BY_OTHER` when `after_graph_work` is True.

### F11 -- Low -- "read-only" and dry-run CLI commands write to the journal DB through `_ensure_ready`
- **Location:** `src/menhir/cli/saga_ops.py:19-36`;
  `src/menhir/infrastructure/graph_operations_prepare.py:95-247` (`_backfill_participant_locks`
  call at `:244`, defined at `:283-306`).
- **Mechanism:**
  - `list` / `show` / `resolve` (including dry run) construct `GraphOperationsJournal`. `show` and
    `resolve` also build the full `build_default_dispatcher`, and every coordinator's
    `__post_init__` calls `journal._ensure_ready()`.
  - On a process's first touch, that runs `ALTER TABLE ADD COLUMN`, `CREATE TABLE`, and an
    `INSERT OR IGNORE` lock backfill for every PREPARED/NEEDS_REVIEW row.
  - `show` and `resolve` also call `build_memory_services(MemorySettings.from_env())`
    (`core/bootstrap.py:145`). I found no Neo4j writes there: it builds Graphiti, the LLM and the
    reranker. But it is heavy, `preload_for_settings` can raise `RerankUnavailable`, and the CLI
    does not pass `read_only=True`. So a read-only `saga show` can fail on an unrelated reranker
    problem.
  - The plan's Validation section says "dry run makes no writes". Row transitions are indeed absent;
    schema and lock writes are not.
- **Fix:** document this as "no state transitions", or give the operator's read paths an
  `_ensure_ready`-free reader and a lighter wiring that builds only the graph adapter.

### F12 -- Low (latent) -- `_reconcile_sweep` impersonates the row's current owner token
- **Location:** `merge_coordinator.py:575`, `unmerge_coordinator.py:695`,
  `delete_coordinator.py:424`, `metric_write_coordinator_reconcile.py:427`.
- **Mechanism:** `owner_token=str(row.get("owner_token") or oo.process_owner_token())` passes the
  CAS for any row and sidesteps the claim mechanism (property 7).
- **Reachability:** a Grep for `_replay_prepared\(|_reconcile_sweep\(` finds only the definitions,
  the `dry_run=True` call in `reconcile()`, and tests. `reconcile(dry_run=False)` raises
  `NotImplementedError`. There is no production caller.
- **Fix:** mint `oo.new_claim_token()` and `claim_abandoned_operation` per row inside the sweep, or
  move the sweep into test helpers.

### F13 -- Low -- erasure keeps a tokenless journal write
- **Location:** `src/menhir/services/erasure_coordinator.py:477-479`:
  `UPDATE graph_operations SET last_error = NULL WHERE op_id = ?`.
- **Mechanism:** the statement has no state or owner predicate. A stale erasure writer, still in
  `_erase_and_verify` after a takeover, can null `last_error` on a row it no longer owns, including
  a NEEDS_REVIEW row whose `last_error` is escalation evidence. This is pre-existing, but it is the
  one remaining journal write that bypasses the new mechanism (property 9).
- **Fix:** add `AND state = 'PREPARED' AND owner_token = ?`, bound to the writer's token.

### F14 -- Low -- `close_after_failed_mutation` can replace the original Neo4j exception
- **Location:** `src/menhir/services/saga_writer_outcomes.py:64-66, 83-91`.
- **Mechanism:** only `StaleTransitionError` from `mark_failed` and exceptions from `prove_before`
  (`:71`) are contained.
  - A `GraphOperationError` or `sqlite3.OperationalError` (database locked) from `record_attempt` or
    `mark_failed` propagates out of the caller's `except` block. It masks the server error the
    caller meant to re-raise, which survives only as `__context__`.
  - "Never swallows `exc`" holds; "the caller still re-raises it" does not.
- **Fix:** wrap the journal calls in a `try/except Exception` that logs and returns False.

### F15 -- Low -- `resolve` holds the global PREPARE pause with no heartbeat or re-verification
- **Location:** `src/menhir/services/saga_operator.py:169-187`; `saga_reconcile_gate.py:40,66-87`.
- **Mechanism:** the CLI acquires the 120 s gate, which pauses every saga PREPARE in the running
  server. It then runs Neo4j classification with no `GateHeartbeat` and no `verify_still_held()`
  before the mark.
  - A SIGKILL leaves the pause in place until the lease expires.
  - A slow classification can outlive the gate. That is harmless to this row (it is claimed), but
    the "gate held" premise silently lapses.
- **Fix:** call `gate.verify_still_held()` before the mark, or use `GateHeartbeat`.

### F16 -- Low -- replay reports REPLAYED when the row was committed by someone else during the claim
- **Location:** `delete_coordinator.py:370-375`; `erasure_coordinator.py:688-711`;
  `merge_coordinator.py:346-351` (through `_apply`); `metric_write_coordinator_reconcile.py:185-196`.
- **Mechanism:**
  - During a replay that holds a fresh claim, `commit_or_settle` returning `COMPLETED_BY_OTHER_OWNER`
    means the row moved under the claim.
  - With claim tokens, only the ownerless-row exemption or an old tokenless binary can do that.
  - The wrappers return REPLAYED or SKIPPED, not STALE, so this does not block readiness (the spirit
    of property 4).
- **Fix:** in the replay wrappers, map `COMPLETED_BY_OTHER_OWNER` to STALE.

### F17 -- Low (plan deviation) -- legacy unmerge `resolve` never closes FAILED
- **Location:** `saga_operator.py:102-114`; `saga_reconcile_dispatcher_routing.py:55-57`.
- **Mechanism:**
  - The plan (section 6) says "legacy at `merged_fp`: FAILED".
  - The routing table maps `LEGACY_ENTITY_UNMERGE` to `NonReplayableKind`, whose
    `classify_prepared_row` always returns WOULD_NEEDS_REVIEW, so `resolve` always quarantines.
  - This fails closed, but it does not match the plan, and it leaves such rows fenced until
    `clear-review`.
- **Fix:** update the plan, or add a legacy-specific `merged_fp` re-read in `_is_before_state`.

### F18 -- Low (latent) -- ownership APIs still default a missing token to the process token
- **Location:**
  - `graph_operations_ownership.py:36` (`renew_owner_heartbeat`) and `:90`
    (`claim_abandoned_operation`).
  - `:159-212` (`attest_owner_death`, where `expected_owner_token=None` means unbound).
  - `graph_operations_prepare.py:349` (`prepare`).
  - `saga_writer_heartbeat.py:83`.
- **Evidence:** `git grep -n "process_owner_token()" -- 'src/*.py'` returns exactly those defaults,
  the four sweep fallbacks (F12) and `operation_owner.py:162,186,202`.
- **Mechanism:** a future caller that omits the token gets back the per-process token, or an
  unbound attestation. Today every production caller passes the token explicitly.
- **Fix:** make the token keyword required on `claim_abandoned_operation`, `prepare` and
  `attest_owner_death`, or extend the AST guard to them.

### F19 -- Low -- escalation of a COMMITTED merge can race an unmerge's `mark_reversed`
- **Location:** `graph_operations_transitions.py:281-391`; `unmerge_coordinator.py:513-519`.
- **Trace:**
  1. R commits the merge with `commit_graph_work=1`.
  2. A user runs `unmerge`. Its row COMMITTED releases the pair locks; its `_mark_merge_reversed`
     has not run yet.
  3. Stale writer P's escalation finds the merge row COMMITTED with no locks held, and moves it to
     NEEDS_REVIEW with re-taken locks.
  4. The unmerge's `mark_reversed` gets `illegal_transition`, which `except Exception` at `:516`
     swallows with a WARNING.
  - Result: the graph is unmerged, the merge row is NEEDS_REVIEW and fencing both uuids, and the
    unmerge row is COMMITTED. This fails closed but is confusing to adjudicate.
- **Fix:** none required. Note it in the runbook's escalation section.

## Properties checked (requested 1-9)

1. **Stale token cannot move a row: holds.**
   - Claim tokens are unique (`operation_owner.py:178-186`), and `_transition` compares tokens
     exactly (`:471-477`).
   - P -> R -> P: P's original token T1 matches neither R's claim nor P's new claim, and a renew
     with T1 fails (`graph_operations_ownership.py:46`).
   - Exceptions: ownerless rows (F10b) and the test-only sweep (F12).
2. **FAILED only with proof: holds for forward and replay, except F4.**
   - Every `close_after_failed_mutation` site wraps exactly one mutation statement, and nothing
     after the statement inside the `try` can raise a Neo4j error:
     - merge: `correlation_queries.py:697-908`, then the SQLite `record_merge`.
     - unmerge / legacy: `:323-452`.
     - delete: `consolidation_queries.py:576-588`.
     - metric: `view_write_repository.py:542-549`. The post-statement `ValueError` is non-Neo4j, so
       `server_rolled_back` is False and the row stays PREPARED.
   - `execute` never retries ServiceUnavailable or SessionExpired without `safe_to_reexecute`
     (`neo4j.py:384-401`). A Grep for `safe_to_reexecute=True` in `src` finds nothing.
   - **Metric FAILED is safe for the receipt chain:**
     - `metric_receipts.latest_for_key(committed_only=True)` joins on state COMMITTED, so a FAILED
       receipt is never chosen as the base.
     - `chain_for_key` has no `src` callers.
     - `state_fingerprint` includes `receipt_op_id`, so a FAILED op cannot be mistaken for the
       current version.
   - Residuals:
     - F4.
     - Operator `resolve` FAILED trusts a point-in-time classification (OQ2).
     - Erasure is correctly excluded from close-on-rollback (`erasure_coordinator.py:423-429`,
       `record_attempt` only), but not from clear-review (F3).
3. **Refused commit after graph work: holds for `mark_committed`, not for `mark_needs_review` (F4).**
   - Escalation runs in one `BEGIN IMMEDIATE` (`:309-340`) and refuses when a participant lock is
     held (`:369-374`) or `ux_graph_ops_unresolved_key` collides (`:381-390`).
   - It rolls back inserted locks on refusal, so no lock or `target_key` theft.
   - Silent success happens only when the committer recorded `commit_graph_work=0`, or for F10a.
   - "One application" rests on fingerprint strength, and for merge that is weak (F1).
4. **STALE blocks a live boot: holds.**
   - STALE is in `LIVE_REACHABLE_OUTCOMES`, counted and blocking (`saga_reconcile_dispatcher.py:372`).
   - Every `replay_prepared_row` either maps `StaleTransitionError` to STALE itself (merge, unmerge,
     metric at `metric_write_coordinator_reconcile.py:363`) or lets it reach the dispatcher's STALE
     mapping (delete, erasure).
   - Erasure's broad excepts at `:613` (subject read in classify) and `:703` (orphan release) do not
     wrap a transition.
   - Residuals: F5, F16.
5. **Operator resolve: mostly holds.**
   - It refuses non-ABANDONED rows (`:147-154`) and erasure (`:137-142`).
   - Dry run makes no row transition (`:166-167`), but it does make schema and lock writes (F11).
   - The gate is always released (`finally` at `:186-187`; the F2 repro shows `gate held: False`).
   - It fails badly when classification raises after the claim (F2), and `clear-review` can still
     close an erasure (F3).
6. **Mixed versions:** F10.
7. **`_reconcile_sweep`:** not reachable in production (F12).
8. **Hooks:**
   - The merge hook fires at most once: forward only, never on replay or escalation. Escalation
     leaves it to `repair_orphaned_assertions`.
   - The unmerge hook fires twice under COMPLETED_BY_OTHER_OWNER (F8).
   - Neither hook is wired in production.
9. **Bypasses:**
   - `process_owner_token()` into a mark: `git grep -n "process_owner_token()" -- 'src/*.py'` shows
     only API defaults (F18) and the sweep fallbacks (F12). No mark call site passes it.
   - `prepare` without `owner_token`: `git grep -n "\.prepare(" -- 'src/*.py'` finds 6 call sites,
     all in scope: `delete_coordinator.py:176`, `erasure_coordinator.py:316`,
     `legacy_unmerge_coordinator.py:203`, `merge_coordinator.py:218`,
     `metric_write_coordinator_saga.py:295` and `unmerge_coordinator.py:257`. Each passes
     `owner_token=token` within its next 12 lines (checked per site). There is no prepare caller
     outside the 20 scope files. Metric mints its token at `metric_write_coordinator_saga.py:294`, inside
     the same connection block as prepare and receipt.
   - `except Exception` swallowing a Stale: `git grep -n "except Exception"` over the 11 scope
     service files returns 28 lines. One is a comment (`delete_coordinator.py:282`), which leaves 27
     handlers. I checked each one:
     - PREPARE-failure abstentions, before any transition: `delete_coordinator.py:185`,
       `erasure_coordinator.py:327`, `merge_coordinator.py:228`, `unmerge_coordinator.py:268`.
     - Classification-only, with no transition inside: erasure `:613` and `:816` (namespace capture,
       abstains before any deletion), and dispatcher `:421` (handler classify -> WOULD_NEEDS_REVIEW).
     - Erasure orphan-release after the transition: `:703`.
     - Mutation excepts that call `close_after_failed_mutation` and re-raise: delete `:221`, merge
       `:384`, unmerge `:471`, legacy `:213/:243`, metric `:232`.
     - Erasure `record_attempt`-and-re-raise handlers: `:423` and `:488`. A Stale raised by
       `record_attempt` propagates; it is not swallowed.
     - Erasure non-transition handlers: `:436` (fence `release_memory` in `finally`), `:516` (WAL
       checkpoint) and `:538` (unaddressable-content census).
     - Per-row report wrappers, which map to STALE or FAILED and never to success: merge `:542`,
       unmerge `:658`, metric `:369`, dispatcher `:327`.
     - Heartbeat `:172`.
     - `prove_before`: `saga_writer_outcomes.py:71`.
     - Unmerge hook: `unmerge_coordinator.py:108`.
     - The only one that swallows a `StaleTransitionError` from a transition is
       `_mark_merge_reversed` (`unmerge_coordinator.py:516`). It is existing, audit-only code (F19).
   - The tokenless erasure `UPDATE` remains (F13).
   - Forward callers (`backend_runtime_data_ops.py:155`, `mcp/tools/ingest/delete_memories.py:110-140`,
     `delete_memory.py:195-205`, `failure_counter_bridge.py:40-80`) never convert a coordinator
     exception into success.

## Open questions (suspected, not proven)

- **OQ1 (F1 reachability):** is there a production path that deletes an ACTIVE, merge-fenced Entity
  without consulting `graph_operation_locks` inside a merge's PREPARED window?
  - Decay `bridge_and_delete` ignores the locks but deletes only COMPRESSED nodes, so it needs a
    compress plus delete inside the window.
  - Erasure derived deletes are fenced.
  - I did not audit every DETACH DELETE in `memory_queries.py`; `git grep` lists about 20 sites.
  - To settle it: for each Entity DETACH DELETE caller, check whether it takes or consults
    participant locks.
  - Independent of the trigger, the code's deviation from plan lines 174-177 is proven.
- **OQ2:** can a server-side auto-commit statement from a killed writer commit after the lease
  expires and an operator resolves FAILED?
  - `lease_seconds_for` sizes the lease above H + W (`operation_owner.py:75-119`), and the server
    timeout is `SAGA_MUTATION_TIMEOUT_S`, so it looks bounded.
  - Bolt behaviour on a closed connection in the middle of an auto-commit was not verified.
  - To settle it: a Neo4j 5.26 experiment.
- **OQ3 (resolved, folded into F11):** `build_memory_services` showed no graph writes in the code I
  read. Its remaining cost is weight and the `RerankUnavailable` failure mode.
- **OQ4:** `prove_before` reads run under the writer's revocation predicate
  (`saga_writer_heartbeat.py:267`).
  - If renewal stalls during a long failed mutation, the read raises `SagaOwnershipRevoked` and the
    row stays PREPARED. That is safe, but fix A silently does nothing.
  - It should not trigger while renewal is healthy.

## Per-file coverage (20 files in scope; 8,141 lines by `wc -l`)

| # | File | Lines | Depth | Findings |
|---|---|---|---|---|
| 1 | `src/menhir/infrastructure/operation_owner.py` | 432 | read-deep (whole) | 0 (supports F18) |
| 2 | `src/menhir/infrastructure/graph_operations_transitions.py` | 526 | read-deep (whole) | F4, F10, F19 |
| 3 | `src/menhir/infrastructure/graph_operations_ownership.py` | 212 | read-deep (whole) | F5, F18 |
| 4 | `src/menhir/infrastructure/graph_operations_prepare.py` | 539 | read-deep (whole) | F11, F18 |
| 5 | `src/menhir/infrastructure/telemetry/erasure_inventory.py` | 648 | trivial (diff only, +6) | 0 |
| 6 | `src/menhir/infrastructure/telemetry/helpers.py` | 295 | trivial (diff only, +3) | 0 |
| 7 | `src/menhir/services/saga_writer_outcomes.py` | 193 | read-deep (whole) | F4, F14 |
| 8 | `src/menhir/services/saga_writer_heartbeat.py` | 278 | read-deep (whole) | F18 |
| 9 | `src/menhir/services/saga_reconcile_dispatcher.py` | 486 | read-deep (whole) | F5 |
| 10 | `src/menhir/services/saga_reconcile_outcomes.py` | 179 | read-deep (whole) | 0 |
| 11 | `src/menhir/services/merge_coordinator.py` | 613 | read-deep (whole) | F1, F4, F12, F16 |
| 12 | `src/menhir/services/unmerge_coordinator.py` | 728 | read-deep (whole) | F4, F8, F12, F19 |
| 13 | `src/menhir/services/legacy_unmerge_coordinator.py` | 294 | read-deep (whole) | F4 |
| 14 | `src/menhir/services/delete_coordinator.py` | 445 | read-deep (whole) | F4, F6, F12, F16 |
| 15 | `src/menhir/services/erasure_coordinator.py` | 898 | read-deep (whole) | F3 (target), F4, F7, F13, F16 |
| 16 | `src/menhir/services/metric_write_coordinator_saga.py` | 325 | read-deep (whole) | 0 |
| 17 | `src/menhir/services/metric_write_coordinator_reconcile.py` | 443 | read-deep (whole) | F4, F12, F16 |
| 18 | `src/menhir/services/saga_operator.py` | 207 | read-deep (whole) | F1 (COMMITTED path), F2, F3, F9, F15, F17 |
| 19 | `src/menhir/cli/saga_ops.py` | 102 | read-deep (whole) | F11 |
| 20 | `src/menhir/cli/__init__.py` | 298 | trivial (diff only, +2) | 0 |

Row count: 20, matching the enumerated 20.

| Files | Lines |
|---|---|
| infrastructure (rows 1-6) | 432 + 526 + 212 + 539 + 648 + 295 = 2,652 |
| services (rows 7-18) | 193 + 278 + 486 + 179 + 613 + 728 + 294 + 445 + 898 + 325 + 443 + 207 = 5,089 |
| cli (rows 19-20) | 102 + 298 = 400 |
| **Total** | **2,652 + 5,089 + 400 = 8,141** |

Every row except 5, 6 and 20 was read in full. Rows 5, 6 and 20 are trivial diffs (+6, +3, +2)
and were read at the diff level only.

**Context files read (not in scope):**
- `infrastructure/neo4j.py` 150-455
- `correlation_queries.py` 165-951
- `consolidation_queries.py` 33-58, 391-421, 550-610
- `domain/retention.py` 41-48
- `lifecycle_decay.py` 330-372
- `domain/merge_eligibility.py` 35-60
- `view_write_repository.py` 320-588
- `view_query_repository.py` 100-200
- `metric_write_coordinator_common.py` 60-110
- `metric_receipts.py`
- `saga_reconcile_dispatcher_routing.py` (whole)
- `saga_preflight.py` (whole)
- `saga_reconcile_gate.py` 40-160
- `correlation_service.py` 170-245
- `graph_operations_model.py` 65-127
- `memory_graph_adapter.py` 2695-2800
- `core/bootstrap.py` 140-272
- `enrichment_steps.py` 1295-1334
- `memory_queries.py` 1625-1660, 2240-2302, 2640-2680
- the forward callers listed under property 9
- `tests/test_saga_writer_counterexamples.py` 1-200

**Not reviewed:** the test files in the diff (41 files) and the AST guard. Both are outside the
requested scope.

## Verification notes

- **Executed:** F1, F2 and F3, run with branch code (`.venv/Scripts/python.exe`, `PYTHONPATH=src`)
  on temp DBs, with `_record_journal_event` / `record_failure_event` stubbed. Output is quoted in
  each finding. The F1 repro fakes eligibility and the concurrent delete, so it proves the commit
  decision, not the production trigger.
- **Traced, not executed:** F4-F19. None was run against Neo4j.
- **Not run:** the repo's own test suites, per the review-only remit.
- **Searches cited:**
  - `git grep "attest_owner_death(\|claim_abandoned_operation(\|clear_needs_review(" -- ':!tests' ':!*.md'`
    finds 4 call sites (F18).
  - `git grep -n "process_owner_token()" -- 'src/*.py'` (F18, property 9).
  - `git grep -n "except Exception"` over the 11 scope service files: 28 lines (27 handlers plus 1 comment), classified under
    property 9.
  - `git grep -n "DETACH DELETE" -- 'src/*.py'` (OQ1).
  - Grep-tool searches:
    - `_replay_prepared\(|_reconcile_sweep\(` (F12)
    - `settle_refused_commit|escalate_after_refused_commit` (F4)
    - `safe_to_reexecute=True` in `src`, with no matches
    - `on_unmerge_committed|on_merge_committed` (F8)
    - `graph_operation_locks|participant` in the decay files (F1)
    - `merged_from` in `merge_eligibility.py`, with no matches (F1)

## Review confidence: 76/100

- **Raising it:**
  - Every scope file was read, and the safety-critical modules were read whole.
  - Three findings were executed.
  - The bypass and broad-except searches were enumerated and classified one by one.
- **Lowering it:**
  - Neo4j-side behaviour was not verified (OQ2): lock-timeout error classes and auto-commit after a
    disconnect.
  - F1's production trigger is unproven (OQ1).
  - F4 rests on Neo4j read-committed visibility of an in-flight writer, which I reasoned through but
    did not test.

## Disposition (orchestrator, 2026-11-13)

Every Medium is fixed with a test that fails when the fix is reverted (F1, F4 checked by
reverting). Lows are fixed where they touch the journal's truth; the rest are recorded as follow-ups.

| ID | Disposition |
|---|---|
| F1 | Fixed. The abstain branch claims "already applied" only when the survivor's `last_merge_op_id` is this op (exact while the op's fence is held). Test: `test_abstention_at_the_after_state_of_another_merge_is_not_reported_as_this_merge`. |
| F2 | Fixed. A failure after the operator's claim quarantines the row NEEDS_REVIEW under the claim and returns the claim token. Test: `test_a_failure_after_the_claim_quarantines_instead_of_stranding_the_row`. |
| F3 | Fixed in the journal (`clear_needs_review` refuses FAILED for `EXPLICIT_ERASURE`), so every caller is covered; `clear-review` reports it as a refusal. Test: `test_clear_review_refuses_failed_for_an_erasure`. |
| F4 | Fixed. New `quarantine_after_graph_work` at all six after-state-drift sites escalates when another owner closed the row. Tests: `test_writer_after_state_drift_on_a_row_another_owner_closed_escalates[failed,committed]`. |
| F6 | Fixed: delete commits `after_graph_work=bool(deleted)`. |
| F13 | Fixed: the erasure `last_error` scrub is bound to `state='PREPARED' AND owner_token=<claim>`. |
| F14 | Fixed: journal failures inside `close_after_failed_mutation` are logged and never replace the mutation's exception. Test: `test_bookkeeping_failure_never_replaces_the_mutation_error`. |
| F5 | Fixed (#404 F-4): a refused claim on this process's own row carries `own_claim` and blocks readiness. Test: `test_this_process_s_own_attested_row_blocks_a_live_boot`. |
| F16 | Fixed (#404 F-4): a replay whose row was completed by another owner under its claim reports STALE in every wrapper. Test: `test_a_replay_whose_row_was_completed_under_its_claim_is_stale_not_replayed`. |
| F10 | Mitigated by a CHANGELOG instruction to upgrade every process sharing a journal together; the old-binary behaviours themselves remain. |
| F7, F8, F9, F11, F12, F15, F17, F18, F19 | Deferred, listed in the PR. None lets a stale writer move a row or marks FAILED over a possible mutation. |
| Also | The AST guard now covers `quarantine_after_graph_work`; metric and legacy-unmerge escalations record the after-state fingerprint (#404 item 3); the #406 F-10 fakes assert the token PREPARE (or the test) set. |
| OQ2 | Open: whether a killed writer's statement can commit server-side after an operator resolve. Needs a Neo4j experiment; the runbook's "restart the server first" guidance bounds it. |
