# ADR 0006 — Recoverable Sagas for Cross-Store Mutations

- **Status:** ACCEPTED retrospectively (2026-09-21). This records the mutation protocol introduced
  after unrecoverable graph deletion and extended across current cross-store writers.
- **Date:** 2026-09-21
- **Deciders:** ctharvey
- **Related:** `.agent/data_models.md`, `.agent/memory-governance.md`,
  `src/menhir/infrastructure/graph_operations.py`

## Context

Menhir stores semantic state in Neo4j and operational receipts, snapshots, and telemetry in SQLite.
The two stores cannot participate in one transaction. Earlier destructive paths mutated the graph
and then wrote best-effort telemetry. A crash between those steps could delete nodes without a
durable snapshot; the 2026-07-12 degree-zero cleanup made roughly two dozen deletions unrecoverable.

The repository now uses durable coordinators for merge, unmerge, delete, erasure, metric writes,
and related repair. The shared architectural decision was never promoted from remediation plans and
model documentation into an ADR.

## Decision drivers

- Intent and recovery material must survive a crash before an irreversible side effect.
- Recovery must distinguish “not started,” “applied,” “safe to replay,” and “state drifted.”
- Retrying a coordinator must not apply the mutation twice.
- A pending erasure must stop reads before physical purge finishes.
- Operators need one bounded reconciliation path instead of coordinator-specific guesswork.

## Decision

Any operation that requires one logical mutation across Neo4j and the SQLite sidecar uses a
**recoverable saga** rather than pretending the stores share a transaction.

The protocol is:

```text
PREPARED  -> commit immutable intent and recovery material in SQLite
MUTATE    -> run an idempotent, preconditioned store mutation keyed by op_id
VERIFY    -> read and validate the expected after-state
COMMITTED -> mark the durable operation complete
RECONCILE -> classify and recover a row left PREPARED after interruption
```

Additional rules:

1. **Durable before destructive.** The PREPARED row contains the operation identity, immutable
   request, and sufficient snapshot or inverse material before graph mutation begins.
2. **Replay is explicit and idempotent.** Store writes carry operation identity and preconditions;
   a retry proves whether the intended state already exists instead of repeating blindly.
3. **Drift abstains.** Recovery that cannot prove a safe replay or commit transitions to
   `NEEDS_REVIEW`; it never guesses from partial state.
4. **Writers and reconcilers coordinate.** A reconciliation gate, ownership lease, and heartbeat
   prevent a live writer and recovery process from mutating the same saga concurrently.
5. **Erasure intent is an immediate read veto.** Once erasure is PREPARED, affected node/namespace
   reads are suppressed until purge completes. Veto lookup failures fail closed.
6. **Raw store primitives are not public mutation paths.** API, MCP, lifecycle, and correlation
   flows call the coordinator that owns the complete protocol.
7. **Receipts audit; they do not rank.** Saga records explain and recover mutation state but do not
   become semantic recall evidence.

## Considered alternatives

### Use a distributed transaction

Rejected. Neo4j and the SQLite sidecar do not share a transaction manager, and adding one would be
disproportionate to the deployment.

### Mutate Neo4j, then write telemetry best-effort

Rejected by observed data loss. The only recovery record can disappear in the same crash window as
the mutation.

### Write intent first but provide no replay classifier

Rejected. A PREPARED row after restart is useful only if the system can prove whether to commit,
replay, or escalate it.

### Allow reads until physical erasure finishes

Rejected. Durable eventual deletion does not prevent another process from exposing data during the
purge window.

### Put every operation in one generic coordinator

Rejected. The journal and recovery vocabulary are shared, while each domain coordinator retains
its own preconditions, snapshot, inverse, and after-state proof.

## Consequences

- Cross-store writers carry more state and explicit recovery logic.
- Destructive operations become inspectable, resumable, and reversible where the domain permits.
- Startup/preflight can report unresolved PREPARED work without mutating it blindly.
- Schema or coordinator changes must preserve old journal rows or provide a reviewed migration.
- Tests must cover interruption points, idempotent replay, drift refusal, and final-state proof.

## Evidence in the repository

- `src/menhir/infrastructure/graph_operations.py`
- `src/menhir/services/merge_coordinator.py`
- `src/menhir/services/unmerge_coordinator.py`
- `src/menhir/services/delete_coordinator.py`
- `src/menhir/services/erasure_coordinator.py`
- `src/menhir/services/erasure_veto.py`
- `src/menhir/services/metric_write_coordinator.py`
- `src/menhir/services/saga_reconcile_dispatcher.py`
- `tests/test_saga_recovery_matrix_live.py`

## Non-goals

This ADR does not promise that every mutation is reversible, authorize automatic recovery under
ambiguous drift, or make SQLite the semantic source of truth for graph state.
