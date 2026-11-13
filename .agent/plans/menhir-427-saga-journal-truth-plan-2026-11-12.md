---
artifact_schema: 1
artifact_uuid: db650b02-fee6-4a5f-8cc1-1111bd99cad3
artifact_type: plan
artifact_status: PROPOSED
---

# Saga journal truth: close stranded rows, escalate late writes, enforce owner tokens (#427, #404, #406)

Date: 2026-11-12. Issues: Archolith/menhir#427, #404, #406. Base: `1b7641e7`.
Branch: `fix/427-saga-journal-truth`. One PR closes all three.

## Why

A journal state is a claim about the graph. The three issues are three ways the claim and the graph
part company, with no supported way to repair them:

- **#427:** PREPARED ("a writer may be executing") outlives a writer that finished with a server
  rollback. The row keeps its participant locks forever. In `live` startup mode it blocks boot,
  because OWNER_UNKNOWN fails `_assess_live_readiness`.
- **#404:** FAILED or COMMITTED is written while a stalled writer's statement can still land. The
  writer's commit is refused and logged, but the row is never re-fenced or quarantined.
  - The token is per-process, so a P -> R -> P takeover re-validates P's stale writer (F-3).
  - A stale outcome after dispatch does not block `write_ready` (F-4).
- **#406:** the owner-token compare-and-set fails open.
  - Tokens are optional (F-6) and the AST guard has holes (F-5).
  - `clear_needs_review` can create an ownerless PREPARED row (F-7, FR-4).
  - Tokenless moves leave `resolved_by_token` stale or NULL (FR-2, FR-3).
  - Evidence from a stale writer is dropped silently (F-9).
  - Test fakes ignore the token (F-10).

All three must be fixed before `live` reconcile can become the default.

## Constraints (from current code)

- **`classify_ownership` and `operation_owner.py`:** an expired lease is not death evidence. Only
  an attestation bound to the exact token, or a dead PID on a verifiable host, gives ABANDONED.
  This plan keeps that rule.
- **`Neo4jRepository.execute` (`neo4j.py:294-401`):**
  - Auto-commit, one statement per attempt.
  - `ClientError` (which includes `ConstraintError`) and `TransientError` propagate as the
    server's own exception. Both are `Neo4jError`, so the server rolled the statement back.
  - `ServiceUnavailable` and `SessionExpired` are `DriverError`: ambiguous, the server may have
    committed.
  - Driver version is 5.28.4.
- **Mutations per coordinator** (investigator trace, recorded in this session):
  - merge, unmerge, legacy unmerge, delete: one auto-commit mutation statement each.
  - metric write: one mutation statement (the count of 2 is a deliberate upper bound).
  - erasure: many statements plus irreversible SQLite purges.
- **Before-state proofs** that exist:
  - `merge_state_fingerprint` + `fetch_merge_state` (merge, unmerge, legacy unmerge).
  - `state_fingerprint` + `fetch_metric_state` (metric).
  - Delete has only per-target existence (`capture_node_state`).
  - Erasure has none.
- **`_transition` (`graph_operations_transitions.py:188-295`):** the compare-and-set skips the
  owner clause when the token is None. Locks are released only on COMMITTED, REVERSED and FAILED.
- **Production callers** pass `owner_token=oo.process_owner_token()` on every mark, 8
  `record_attempt` sites included.
  - 39 tokenless test calls in 9 test files will need a token.
  - `tests/test_cf271_stale_commit_after_takeover.py:126,144` deliberately pin the tokenless
    idempotent behaviour that this plan removes. Those two tests are rewritten for the new
    contract, and the PR says so.
- **Project policy:** the full suite runs in CI only. Locally we run focused tests plus
  disposable-Neo4j online tests for the destructive paths.

## Design

### 1. Per-claim owner tokens (#404 F-3)
- `oo.new_claim_token()` returns `<label>:<host>:<pid>:<process_nonce>-<claim_nonce>`.
  - It keeps 4 colon-separated parts, so `_owner_parts` and every older binary still parse
    host/pid. PID death evidence is unchanged.
- `oo.same_process(token)` compares everything up to and including `process_nonce`.
  - `is_own_claim` becomes process-level through it.
  - `claim_abandoned_operation`'s "not this process" conjunct uses it too.
- Each claim is minted once and threaded explicitly; no `oo.process_owner_token()` at a mark site:
  - **Forward path:** the coordinator mints a token and passes it to `prepare(owner_token=)`, to
    `owned_mutation(owner_token=)`, and to every mark.
  - **Replay path:** the dispatcher mints a token for `claim_abandoned_operation(owner_token=)`
    and passes it to `handler.replay_prepared_row(row, owner_token=)`.
- New append-only `graph_operation_claims(op_id, claim_token, claimed_at, kind)` table, written in
  the same transaction as PREPARE and as a successful claim. It is the audit chain, and it is what
  binds an escalation (section 4) to a writer that really held the row.

### 2. Fail-closed mark API (#406 F-6, F-5, FR-2, FR-3, FR-4, F-7, F-9)
- `owner_token` becomes a required keyword with no default on `mark_committed`, `mark_failed`,
  `mark_needs_review` and `record_attempt`.
  - `None` or an empty string raises `GraphOperationError`.
  - There is no bypass sentinel. The only tokenless transitions left are `mark_reversed`, which acts
    on COMMITTED rows where owner claims don't apply, and the operator's `clear_needs_review`, which
    stamps the operator's name. So no route into COMMITTED leaves `resolved_by_token` NULL (FR-2).
  - The existing exemption for ownerless legacy PREPARED rows (`owner_token IS NULL`) is unchanged.
- `clear_needs_review(op_id, *, to_state, operator)`:
  - Requires an operator name and refuses `PREPARED` (F-7).
  - Stamps `resolved_by_token = 'operator:<name>'` (FR-3 and FR-4).
- On the idempotent branch, a same-state move by a *different* tokened owner logs WARNING and
  records a failure event carrying the stale writer's evidence. `last_error` is not overwritten
  (F-9).
- `mark_committed(..., after_graph_work: bool = True)`:
  - The already-applied and skip branches pass `False`, so they produce no commit-refused record
    and no escalation (#404 F-8).
  - The record gets an allowlisted `refusal` enum: `committed_by_other_owner`, `owner_mismatch`
    or `illegal_transition` (FR-7 remainder).
- `attest_owner_death(op_id, *, attested_by, expected_owner_token)` attests only if the row still
  carries that token. This stops an operator who inspected owner A from attesting B after a
  transfer.
- The AST guard covers `record_attempt` and rejects a literal `None` value. It matches any
  receiver except the two unrelated journals (`migration_batches`, `view_projection_repair`),
  excluded by path.

### 3. Writer closes a provable rollback (#427 fix A)
New module `services/saga_writer_outcomes.py`:
- `server_rolled_back(exc) -> bool`: true only for `neo4j.exceptions.ClientError` and
  `TransientError`. `DatabaseError`, every `DriverError` and every non-Neo4j exception return
  False.
- `close_after_failed_mutation(journal, op_id, *, owner_token, exc, prove_before) -> bool`:
  1. Calls `record_attempt` first.
  2. Then, only if `server_rolled_back(exc)` **and** `prove_before()` is true, calls `mark_failed`
     with the reason `rolled back by server: <type>: <msg>`, which releases the fence.
  3. Returns whether it closed the row.
  - It never swallows `exc`: the caller still re-raises.
  - If `prove_before` itself raises, the row stays PREPARED.
- Wired into the mutation except-blocks of:
  - merge (`_apply_owned`): fingerprint == `expected_before`.
  - unmerge (`_apply_owned`): fingerprint == `expected_before` (`merged_fp`).
  - legacy unmerge: re-read the fingerprint == `merged_fp`. This kind is non-replayable, so today
    it is always quarantined.
  - metric (`_apply_owned`): `state_fingerprint` == `expected_before`.
  - delete (`_mutate_and_verify`): every target still exists. Delete is one `DETACH DELETE`
    statement, so a rollback leaves them all.
- **Erasure is excluded.** It spans several commits, its purge cannot be undone, and FAILED would
  be false. Its rows stay PREPARED and go through live replay or the operator (section 6).
- Replay shares `_apply_owned`, so a race that recurs during a `live` replay now closes FAILED
  instead of leaving a PREPARED own-claim that blocks boot twice.

### 4. Commit refused after graph work (#404 F2, FR-5, FR-6)
Re-verifying the after-state at refusal time can't tell the cases apart: every coordinator already
verifies before it commits, so the check passes by construction. What distinguishes them is
**whether both owners did graph work**:
- New column `commit_graph_work INTEGER` is written by `mark_committed(after_graph_work=...)`.
- "Did graph work" means this caller's own mutation statement reported an effect:
  - merge returned a row;
  - unmerge `restored > 0`;
  - delete `deleted_uuids` is non-empty;
  - metric write reported a write;
  - erasure deleted or purged anything.
  An already-applied, skip or abstain branch did no graph work.

New journal method `escalate_after_refused_commit(op_id, *, owner_token, observed_error)`. One
`BEGIN IMMEDIATE` that, in order:
1. Requires `owner_token` to appear in `graph_operation_claims` for `op_id`.
2. Requires the state to be FAILED or COMMITTED.
3. Re-inserts the participant locks and moves the row to NEEDS_REVIEW with `last_error` = the
   observed evidence.
4. Refuses (rolls back) if any participant is now locked by another op, **or** if the move
   violates `ux_graph_ops_unresolved_key` (`IntegrityError`, which means a newer write already
   holds the same `target_key`).
- It returns `escalated | refused_fence_taken | refused_not_claimant | refused_state`.
- Every refusal records a failure event.

Helper `settle_refused_commit(journal, op_id, *, owner_token, did_graph_work, observed)`, in
`saga_writer_outcomes`. It is called wherever a writer's `mark_committed` or `mark_needs_review`
raises `StaleTransitionError`. It reads the row and decides:

| Row | This writer did graph work | Committer did graph work | Action |
|---|---|---|---|
| FAILED | yes | n/a | escalate (FAILED says "no mutation", which this writer disproves), re-raise |
| FAILED | no | n/a | re-raise (FAILED is true) |
| COMMITTED by other | yes | yes or NULL (old binary) | escalate (possible double apply), re-raise |
| COMMITTED by other | yes | no | **success**: one application, verified by the committer; informational event |
| COMMITTED by other | no | any | **success** ("completed by recovery") |
| anything else | any | any | re-raise |

- "Success" returns normally, so the forward caller reports the true outcome (FR-6).
- For merge, this also covers the interleaving where the reconciler really replayed and the stale
  writer's statement no-oped into the abstain branch: that branch's refused `mark_needs_review`
  settles as success when `post_fp == expected_after`. `merge()` then returns `merged: 1`, and
  `correlation_service` fires `on_merge_committed` exactly once. Replay never fires it (FR-5).
- If the writer process died instead, the hook is covered by the existing
  `repair_orphaned_assertions` pass, as documented in FR-5.

### 5. Dispatcher readiness (#404 F-4)
- A new outcome `STALE` replaces `LIVE_OWNER, {"stale": True}` in the dispatcher and the three
  replay wrappers (merge, unmerge, metric).
- `_record` keeps the flag, and `_assess_live_readiness` blocks on STALE.
- The dispatcher's `NonReplayableKind` quarantine uses the claim token it minted.

### 6. Operator surface (#427 fix B, also the exit for #404 escalations)
`menhir saga` Typer group (`cli/saga_ops.py`), with the logic in `services/saga_operator.py`.
Every write names `--by <operator>`.
- **`list [--state PREPARED|NEEDS_REVIEW]`:** rows with kind, ownership classification, attempts
  and `last_error`. Read-only.
- **`show <op_id>`:** the row, claim history, classification, and the kind handler's pure
  `classify_prepared_row` verdict. Read-only.
- **`attest <op_id> --owner-token T --by NAME`:** calls `attest_owner_death(expected_owner_token=T)`.
- **`resolve <op_id> --by NAME [--apply]`** (dry run by default):
  1. Acquires `ReconciliationGate` and refuses if it is held.
  2. Requires the ownership classification to be ABANDONED, which means a dead PID or an
     attestation.
  3. Claims with a fresh token via `claim_abandoned_operation`.
  4. Classifies the graph with the handler and closes the row with the claim token:
     - **ALREADY_APPLIED:** COMMITTED with `after_graph_work=False`.
     - **Before-state proven** (WOULD_REPLAY for merge, unmerge or metric; all targets present
       for delete; legacy at `merged_fp`): FAILED, reason `operator <name> closed: graph at
       before-state`.
     - **Drift or unparseable:** NEEDS_REVIEW.
     - **Erasure:** never FAILED. `resolve` refuses before claiming and points to live replay,
       because a partial erasure has to be completed, not abandoned.
  5. Releases the gate.
- **`clear-review <op_id> --to FAILED|COMMITTED|REVERSED --by NAME`:** calls the hardened
  `clear_needs_review`.

**When an operator may attest.** This goes in the runbook and the `attest` help text.

Attestation states that the named writer process has stopped, so it is true only when that
process is gone: a container restarted since the row's `created_at`, or a host that was rebooted.

After fix A, the rows that still strand are the ambiguous-error ones. Their writer is often the
**running** server, so attesting them while it runs is false. The supported route is:
1. Restart the server. The old process is now dead, which makes the attestation true.
2. Run `menhir saga attest` against the old token, then `resolve`.

Alternatively, run with `MENHIR_HOST_PID_NAMESPACE_VERIFIABLE` on a single-namespace host, which
lets the dead PID prove the death by itself.

Ownerless legacy PREPARED rows (no token) stay out of scope. `attest` and `claim` refuse them
already, and `resolve` reports them as "needs direct repair".

## Steps (in order; each step ends with its focused tests green and a commit)

| # | Owner | Files | Acceptance |
|---|---|---|---|
| S1 | orchestrator | `operation_owner.py`, `graph_operations_{transitions,ownership,prepare,model}.py`, new `tests/test_saga_journal_truth.py` | Claim tokens, claims table and `commit_graph_work` column, required tokens, hardened `clear_needs_review` and `attest_owner_death`, F-9 event, refusal enum, `escalate_after_refused_commit`. New journal tests pass: F-3 R3 repro, F-7, FR-2, FR-3, FR-4, F-9, escalation including fence and unique-key refusal. |
| S2 | executor | tokenless test call sites (9 files) and the 9 fakes (F-10) | Every test call passes a real token. The fakes record the token and assert it equals the one PREPARE or the claim used. The two `test_cf271_stale_commit_after_takeover` tests are rewritten to pin the new contract. The S1-affected journal suites are green. |
| S3 | orchestrator | new `services/saga_writer_outcomes.py`, `saga_writer_heartbeat.py` (`owned_mutation(owner_token=)`), `saga_reconcile_dispatcher{,_routing}.py` (STALE, claim token, `replay_prepared_row(row, *, owner_token)`) | Helper unit tests, plus dispatcher tests for STALE blocking readiness and claim-token threading. |
| S4 | executors, one per file group, in parallel | merge; unmerge; legacy unmerge; delete; erasure; metric (saga + reconcile) | Mint a token, thread it through every mark, close on rollback (not erasure), settle refused commits, `after_graph_work=False` on no-work branches, STALE in replay wrappers. Each coordinator's existing suite is green. |
| S5 | executor | new `services/saga_operator.py`, `cli/saga_ops.py`, register in `cli/__init__.py` | list, show, attest, resolve, clear-review, as specified in section 6, with tests. |
| S6 | test-writer | new counterexample tests | Every "+" item under Validation. |
| S7 | orchestrator | AST guard, docs, CHANGELOG | Guard flags `record_attempt`, literal `None` and an aliased receiver. Online suites pass. `code-reviewer` findings are resolved. PR opened. |

## Alternatives considered

- **Mark FAILED on any mutation exception, with a before-state re-read:** rejected. After a
  `DriverError`, the statement may still commit after the re-read and after the fence is
  released.
- **An integer `owner_epoch` column instead of a per-claim token:** it gives the same safety but
  needs a second compare value threaded everywhere and a schema change on every mark. A per-claim
  token reuses the existing `owner_token` compare-and-set unchanged.
- **COMMITTED -> NEEDS_REVIEW on every refused commit:** rejected. When another owner committed
  and the graph is verified at the after-state, the claim is true, and quarantining it would
  refence a correct merge and hide the op from the merge hook.

## Risks

- **Mixed versions (Mitigable):** an older binary's writer uses the process token, while a new
  reconciler's claims use claim tokens.
  - An old writer can still only mark rows carrying its own token, as today.
  - The new token keeps 4 parts, so old classifiers parse it.
  - The claims table is created on `_ensure_ready`. Old binaries ignore it, so a claim made by an
    old binary leaves no history row, and an escalation by that claimant is refused, which fails
    closed.
- **Erasure rows still strand (Acceptable):** by design. The operator `show` and live replay
  cover them.
- **The race test is offline only (Acceptable):** the `DELETE` race happens inside one Cypher
  statement and cannot be produced deterministically against Neo4j. The test injects the real
  driver `ConstraintError` from the adapter. Online tests cover the before-state proof and FAILED
  on a real graph.
- **Test churn (Acceptable):** the required token touches 39 tokenless test calls and the 9
  fakes, and 2 tests that pin removed behaviour are rewritten. This is disclosed in the PR.

## Invariants (must hold after the change)

1. FAILED is written only when this op made no graph mutation. The proof is a server rollback
   plus a before-state re-read, or operator classification at the before-state.
2. No mark moves a PREPARED row without the current claim's token. A lost claim's token never
   matches again, even within the same process.
3. Locks are released only by COMMITTED, REVERSED or FAILED. Escalation re-takes them atomically
   or refuses.
4. A refused commit after graph work never leaves FAILED over a mutated graph unrecorded.
5. A `live` pass is not write-ready while any row is STALE, OWNER_UNKNOWN, FAILED-replay or an
   own-claim.

## Validation

- **Offline focused tests**, new ones marked (+):
  - + race: `ConstraintError` from `merge_entity` with the graph at before -> FAILED, locks
    released, pair mergeable again. The same for unmerge, metric, legacy and delete.
  - + an ambiguous `ServiceUnavailable` stays PREPARED with locks. `prove_before` raising also
    stays PREPARED.
  - + `live` boot with a stranded merge row: it refuses on main. After fix A, a fresh failure
    closes FAILED and the boot is write-ready.
  - + F-3 R3 repro: P -> R -> P, then P's stale `mark_failed` is refused and the locks are kept.
  - + #404: the writer stalls, the reconciler marks FAILED, the writer lands -> NEEDS_REVIEW
    with locks re-taken. A second op takes a participant between FAILED and escalation -> refused
    and recorded.
  - + COMMITTED by another and verified -> forward success and the merge hook fires. Not verified
    -> NEEDS_REVIEW.
  - + F-4: a STALE replay blocks `write_ready`.
  - + F-7, FR-2, FR-3, FR-4, F-9, F-8 and the refusal enum. A tokenless mark raises `TypeError`,
    and `None` raises `GraphOperationError`.
  - + operator: dry run makes no writes. It refuses a live owner, a held gate, erasure FAILED,
    drift (sends it to NEEDS_REVIEW), and a missing `--by`. Attestation is bound to the expected
    token.
  - + guard: `record_attempt`, the literal-None shape, and the aliased receiver are flagged.
  - Existing suites:
    - journal: `test_graph_operations_*`, `test_operation_ownership.py`,
      `test_claim_abandoned_operation.py`, `test_pid_namespace_assertion.py`,
      `test_cf271_*`, `test_metric_saga_sidecar.py`
    - coordinators: `test_*coordinator*`, `test_unmerge_precondition.py`,
      `test_cf232_legacy_unmerge_ownership.py`, `tests/services/test_cf210_*`,
      `test_scalar_state_merge.py`
    - dispatcher and startup: `test_saga_*`, `test_reconciliation_gate.py`
    - erasure and delete: `test_sidecar_physical_erasure.py`, `test_erasure*`, `test_delete*`
  - `affected_tests` from the structure index is used as one input.
- **Online:** disposable Neo4j on ubuntu-server. The `*_coordinator_live.py` and
  `test_saga_dispatcher_live.py` / `test_saga_reconcile_live_path.py` suites plus the new online
  FAILED-close test.
- **Static:** `ruff check --select F811,F821,ASYNC` on the changed files.
- **CI:** full offline and online suites on the PR SHA.

## Docs to update

- `CHANGELOG.md`
- `.agent/workflows/operations_runbook.md`: the saga operator commands.
- `.agent/data_models.md`: the `graph_operation_claims` table and the token format.
- `.agent/plans/README.md`: this plan.

## Coverage summary

- **Inspected:**
  - `graph_operations_{transitions,ownership,prepare,model}.py`, `operation_owner.py`,
    `saga_writer_heartbeat.py`, `neo4j.py` `execute`
  - `saga_reconcile_dispatcher.py` (replay and readiness), `saga_preflight.py`,
    `core/runtime.py` gate, `cli/__init__.py`
  - `merge_coordinator.py` `_apply_owned`, `correlation_queries.py` `fetch_merge_state` and the
    merge mutation tail
  - all coordinators, through an investigator trace
  - the test fake inventory, through an Explore pass
- **Not inspected:**
  - the erasure cascade statement counts, which don't matter because erasure is excluded;
  - the upper-level exception handling of forward callers, beyond `classify_pair` and the
    enrichment step that swallows exceptions.
- **Assumptions without code evidence:**
  - a re-proposal of a FAILED merge by a later correlation pass (not traced; it affects only
    whether a closed merge is retried, not safety);
  - that production runs the `menhir` console script inside the app container (the 11-02
    workaround ran from inside it).
