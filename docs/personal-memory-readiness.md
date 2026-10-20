# Personal-memory lanes: readiness, restart and opt-out

The counter, scalar, event and counting lanes are asynchronous. A user turn is captured
immediately, but the scalar value, event or count it implies appears in recall only after a
consolidation pass has read it. This page says when that happens, how to tell that a namespace
has caught up, what a restart does, and how to turn a lane off and back on. Every lane ships
off. For the agreement settings and the perceiver-version rules, see `agreement-settings.md`.

## From turn to recall

1. **Capture.** A host hook records the user's words as `:TurnEvidence`
   (`POST /api/turn-evidence`), and the agent admits a memory on that turn (`add_memory` with
   `turn_evidence_uuid`). The raw memory is recallable straight away, through the ranked items
   and `source_memories`.
2. **Pass.** A consolidation pass reads each lane's turns past that lane's durable cursor. It
   writes assertions and rebuilds the affected Views. Lanes run in this order: counter, scalar,
   event, counting.
3. **Recall.** Once the View exists, `recall_memories` returns a verdict in `authority_layer` or
   `event_authority_layer`, and `build_context` renders it as `[Scalar authority: ...]` or
   `[Event authority: ...]`. A lane's verdicts appear only while its authority flag is on.

## When a pass runs

- **Scheduled.** The maintenance scheduler registers the consolidation job only if at least one
  lane flag is on and a chat provider is configured. The job runs every
  `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_INTERVAL_S` seconds (default 300), counted from the start
  of the previous run.
- **First run after start.** A job that has never run is due immediately. Jobs run one at a time
  in registration order, so consolidation starts after the earlier maintenance jobs finish:
  stale-lease recovery, enrichment retry, queue health, the conflict jobs and the structure
  refresh.
- **No scheduler.** With `MENHIR_BENCHMARK_MODE=1`, or when enrichment is not ready, no scheduler
  starts. Passes then run only through `POST /api/phase3/run` for one namespace.
- **Budget.** A scheduled pass may make up to 300 model calls
  (`MENHIR_PERSONAL_MEMORY_CONSOLIDATION_CALL_BUDGET`). A larger backlog drains over several
  passes. `POST /api/phase3/run` applies no budget unless the request sets `call_budget`.

Expected delay for a new turn: up to one interval plus the duration of the pass that reads it.
Counting reads only turns the scalar lane has finished, so a count can trail its scalar value by
one more pass. A perceiver-version change re-reads every turn in the namespace, so expect a
backlog after one.

## Knowing a namespace has caught up

`POST /api/phase3/run` reports what is left after its pass:
- `dirty_after`: counter turns left to read.
- `event_dirty_after`: event turns left (null when the event lane is off).
- `counting_dirty_after`: counting turns left (null when counting is off).
- `scalar_repair_pending`: scalar subjects still waiting for a View rebuild.

Gaps:
- No status field reports scalar turns left to read.
- `GET /api/phase3/status` reports counter dirtiness only.

## Restart

- **Cursors are durable.** Each lane's cursor lives in Neo4j. A restarted backend continues from
  where the last finished page left off and does not re-read turns already read at the same
  perceiver version. A pass killed mid-page re-reads only that page. E2E-15 and E2E-18 cover this.
- **Clean stop.** A stopped scheduler releases its lease, so the next process takes it
  immediately.
- **Unclean exit.** The scheduler lease lives in SQLite and lasts 90 s, renewed every 30 s. After
  a crash, the dead process's lease stays valid until it expires.
  - **Default deployment.** The PID-namespace assertion is not set, so a backend restarted within
    those 90 s cannot take the lease at first. Its scheduler stands by and retries every
    heartbeat (30 s). It takes the lease once the dead owner's lease expires, within about 90 s,
    with no operator action (#170 decision 4).
    - While it waits, `resume_scheduler` reports `lease_standby: True`.
    - `stop()` or pausing the scheduler withdraws the standby.
    - A process that loses the lease while running does not stand by. It stays stopped until an
      operator calls `resume_scheduler` or `force_scheduler_takeover`.
    - `POST /api/phase3/run` still works.
  - **With the PID-namespace assertion set,** a dead local owner's lease is reclaimed at once.
- **Scheduler state.** Job timing is held in memory, so after a restart every job counts as
  never run and is due at once.

## Opting out

To turn a lane off, set its flag to `false`. To stop it from answering recall as well, also set
its authority flag to `false`. Then restart the backend and the stdio bridge.

| Lane | Lane flag | Authority flag |
|---|---|---|
| Counter | `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED` | none; counter Views are ordinary memories |
| Scalar | `MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED` | `MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED`; history Views: `MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED` |
| Event | `MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED` | `MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED` |
| Counting | `MENHIR_PERSONAL_MEMORY_COUNTING_ENABLED` (needs scalar) | counted Views are hidden from recall when counting is off |

What happens (E2E-18):
- **The lane stops.** A pass makes no model calls for it, and its stored Views and assertions
  stay as they were. Turning a lane off is not a rollback.
- **Authority keeps reading stored state if left on.** The retained Views and events can still
  lead, but they no longer update. A value the user changed while the lane was off is served
  with its old number as current. This is by design ("retained Views can outlive the writer
  gate", `config/feature_flags.py`). For a full opt-out, turn off both flags.
- **With both off,** recall returns ordinary memories and `source_memories`, with no verdicts.
- **Turning the lane back on.**
  - **Scalar and event:** they read the turns captured while they were off, from their
    cursors, and do not re-read turns they had already finished.
  - **Counter:** the counter lane re-reads every user turn of the namespace on any pass that
    has new turns. Its cursor only marks the namespace dirty.
  - The Views catch up in that pass.

Removing data is a separate step: see the deletion and erasure tools.
