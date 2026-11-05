# Personal-memory lanes: readiness, restart and opt-out

The scalar and counting lanes are asynchronous. A user turn is captured
immediately, but the scalar value or count it implies appears in recall only after a
consolidation pass has read it. This page says when that happens, how to tell that a namespace
has caught up, what a restart does, and how to turn a lane off and back on. The scalar lane,
scalar history and counting ship on. For the agreement settings and the perceiver-version rules,
see `agreement-settings.md`.

## From turn to recall

1. **Capture.** A host hook records the user's words as `:TurnEvidence`
   (`POST /api/turn-evidence`), and the agent admits a memory on that turn (`add_memory` with
   `turn_evidence_uuid`). The raw memory is recallable straight away, through the ranked items
   and `source_memories`.
2. **Pass.** A consolidation pass reads each lane's turns past that lane's durable cursor. It
   writes assertions and rebuilds the affected Views. Lanes run in this order: scalar, counting.
3. **Recall.** Once the View exists, `recall_memories` returns a verdict in `authority_layer`,
   and `build_context` renders it as `[Scalar authority: ...]`. A lane's verdicts appear only while its lane flag is on;
   scalar authority has no separate flag (see "Opting out").

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
- `dirty_after`: scalar user turns left to read, from the scalar lane's cursor at the configured
  perceiver version. False when the scalar lane is off (nothing would consume that work).
- `counting_dirty_after`: counting turns left (null when counting is off).
- `scalar_repair_pending`: scalar subjects still waiting for a View rebuild.

Gaps:
- `GET /api/phase3/status` `dirty` has the same meaning as `dirty_after` (scalar turns left to
  read); it does not cover counting turns.

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

To turn a lane off, set its flag to `false`, then restart the backend and the stdio bridge. Its
authority stops too.

| Lane | Lane flag | Authority |
|---|---|---|
| Scalar | `MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED`; history Views: `MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED` | always on while the lane is on. `MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED` was retired 2026-10-27 (#168 freeze) and no longer turns it off |
| Counting | `MENHIR_PERSONAL_MEMORY_COUNTING_ENABLED` (needs scalar) | counted Views are hidden from recall when counting is off |

What happens (E2E-18):
- **The lane stops.** A pass makes no model calls for it, and its stored Views and assertions
  stay as they were. Turning a lane off is not a rollback.
- **Authority stops with its lane.** Authority is applied only while its lane flag is on
  (`config/authority_gates.py`). The stored Views no longer update, so serving them as current
  would give a changed value its old number. Recall serves ordinary memories instead.
- **Scalar authority default.** Scalar authority is on whenever the scalar lane is on, and the
  scalar lane ships on, so a default install serves scalar authority.
- **With both off,** recall returns ordinary memories and `source_memories`, with no verdicts.
- **Turning the lane back on.** It reads the turns captured while it was off, from its cursor,
  and does not re-read turns it had already finished. The Views catch up in that pass.

The consolidation counter lane (`MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED`) was removed
2026-10-28. Menhir ignores the setting; the bench launchers refuse it.

Removing data is a separate step: see the deletion and erasure tools, and the erasure limitation
below.

## Known limitation: erasure keeps extracted knowledge

`delete_memory` and the erasure saga remove the memory node and its `:Episodic` twin, the captured
evidence turns and their projections, the scalar assertions bound to or grounded on the memory,
and its sidecar rows. They do not remove the `:Entity` nodes or `RELATES_TO` fact edges that
enrichment extracted from the memory, and an entity summary or fact can restate the erased
content (#417).

Entities are shared: one entity can be supported by many memories, so deleting every entity a
memory mentions would destroy other memories' knowledge. Until the post-MVP fix removes what only
the erased memory supports, the way to remove everything is to delete the whole namespace
(`DELETE /api/namespace/{namespace}`, operator tier).

## Known limitation: source grounding by scalar kind

A scalar value becomes a current View when at least 2 of 3 model samples agree on it and it
passes a check against the user's own words:

| Kind | Checked against the source |
|---|---|
| money, measurement | the value must appear in the quoted span |
| count | corrected from the number in the span |
| clock_time | a contradicted time is corrected; an hour-only or ranged time abstains |
| duration | the amount and unit must be in the span, compared in seconds |
| frequency | the count and period must be in the span, or read directly from wording like "every other week" |
| weekday | the named day must be in the span |
| status | every word of the value must be in the span |
| boolean | the attribute must be named in the span; false needs a stated negative or ended state |

For duration, frequency, weekday, status and boolean (since 0.3.1, #411), the value is also
dropped when the clause before it negates or hedges it, or when the sentence is a question. The
clause is read from the source, so a quote cannot leave the negation out. Status and true
booleans are dropped in the past tense ("I was married"), and when a "who" or "whose" clause
puts the value on someone else.

**What these checks do not do:**

- They are English patterns. A value they cannot read is dropped, so a true fact can be missed.
  Replayed on the 44 values in these kinds from the #169 `gpt-6-luna` run, all judged right by
  one rater, 38 pass and 6 are dropped.
- They confirm the words are present, not who they are about. "My wife is retired" stored as the
  user's status passes.
- An `expire` row is exempt, because it ends a value the quote need not restate ("I quit my job").
- Values stored before 0.3.1 are not rechecked.

Measured at 0.3.1 with scripted model values: 0 unsafe admissions on the 64-case #169 grounding
matrix (13 at 44933985) and 0 of 160 rows on the held-out LongMemEval panel (33 at 44933985). The
panel found no first-person status or boolean statements, so those two kinds rest on the matrix
and hand-written counterexamples. The local default chat model has not been measured.

Scalar authority has no separate switch. The only way to stop serving these values as current
is to turn the scalar lane off (`MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED=false`), which
stops every scalar View.
