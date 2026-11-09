## 2026-11-08 - The off-loop telemetry writer skips the per-commit fsync

- Connections opened on the off-loop telemetry writer thread use `synchronous=NORMAL` in WAL mode,
  so each observability write no longer waits for a disk flush, and the writer falls behind less
  often (a read that outruns it logs a warning, see below). A power loss can drop only the latest
  of those rows, and never corrupts the file.
- Everything else that opens a connection through `connect_telemetry_db` keeps `FULL`: the
  scheduler lease, the operations journal, pending actions, migration batches, erasure subjects,
  metric receipts and the recovery rows (merge audit, memory revisions). So does any connection
  that is not in WAL mode.

## 2026-11-08 - Opt-in recall query log for recall research

- New setting `MENHIR_TELEMETRY_RECORD_RECALL_QUERIES` (default off). When on, each MCP recall
  (`recall_memories`, `recall_context_memories`) and `POST /api/recall` writes one row to a new
  telemetry table, `recall_query_log`: the query text and its SHA-256, the ordered result ids and
  scores, source memory ids, namespace, preset, limit, and the recall receipt token when there is
  one. `recall_receipts` stays content-free.
- With `MENHIR_PRIVACY_REDACT` on, only the hash is stored. Rows follow the 90-day diagnostic
  retention tier. Namespace erasure scrubs the query text; rows with no namespace only age out
  (`docs/your-data.md`).
- The write goes through the #527 off-loop writer, so it never opens SQLite on the event loop.

## 2026-11-08 - A telemetry read that may miss queued writes says so

- A telemetry read waits up to `MENHIR_TELEMETRY_BUSY_TIMEOUT_S` (5 s) for off-loop writes queued
  before it. When that wait times out, the read still runs but now logs a warning naming how many
  writes are still queued, instead of returning a possibly incomplete result silently.

## 2026-11-08 - v0.3.6: profile catalog 2, telemetry off the event loop, one operation table

- Profile catalog 2 puts `get_client_context` in `core`, so agent-tier clients receive it. A
  deployment opts in with `"profile_catalog": 2`; catalog 1 policies resolve as before.
- Telemetry writes no longer block the event loop (#527), and the token endpoint logs why it
  refused a request (#541).
- What each TypedAssertion operation means now lives in one table; an operation it does not know
  carries no value anywhere.
- #519: a span-aligned scalar no longer keeps a `current_`/`latest_` prefix its quote dropped.
  Readers understand an `approximate` operation; nothing writes one yet.
- v1 and v2 client policies still load. The v0.3.5 notes said this release would drop them; that
  moves to a later release.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.6. No dependency changes in this commit.

## 2026-11-08 - Readers for an approximate scalar operation (#519 phase 1)

- A hedged value ("I think I'm close to 1300") will be stored as `approximate`: it never sets or
  changes a View's value, is listed in scalar history as "about 1300", and keeps a View advisory
  when newer. Only readers ship here. The table entry has no writer lane, so a stored row loads
  but no parse path or scalar identity can produce one.
- `validate_value`: one finite number on a numeric kind (an integer for counts); ranges are
  refused.
- History lists it, also with counting off; the change check treats it as a value statement, so a
  lone hedge is not a change. The HISTORY_ENTRY listing includes it; provenance never does.
- Projection coverage: a known operation the fold never reads is `HISTORY_ONLY`, not an
  unaccounted write. An unknown operation is still reported.

## 2026-11-08 - Span alignment re-grounds a source-time attribute prefix (#519)

- Each k-sample proposal canonicalises its attribute against its own quote, so one quoting
  "current balance is 1300" kept `current_balance`. Span alignment then commits the common span
  "balance is 1300", and the kept prefix forked a `current_balance` slot beside `balance`. The
  gate now re-checks the name against the committed quote. Stored rows keep their names.

## 2026-11-08 - One table decides what each TypedAssertion operation means

- `menhir.domain.assertion_operations` holds every operation's meaning. The 61 places that
  compared operation names in Python and Cypher ask it instead. An operation it does not know
  sets, changes, ends and counts nothing, opens no fold slot or history entry, and is logged once.
- Known operations behave as before. The provenance and history listings now list the known
  operations instead of "everything except occurrences".
- `tests/test_assertion_operations.py` fails on any new literal operation comparison outside the
  table.

## 2026-11-08 - Profile catalog 2: `get_client_context` moves to core

- `get_client_context` is a readonly-tier, read-only tool that agents use at session start. Catalog
  1 filed it under `operate`, so agent-tier clients such as ChatGPT were refused it. Catalog 2 puts
  it in `core`.
- Policies pinned to `"profile_catalog": 1` resolve exactly as before. An agent policy still lacks
  it and logs it as withheld; an `operate` policy keeps it. A moved tool's earlier placements live
  in `EARLIER_PLACEMENTS` (`src/menhir/mcp/tool_profiles.py`), so a pinned policy never silently
  loses a tool.
- To pick it up, a deployment sets `"profile_catalog": 2` once it runs this build.

## 2026-11-08 - Token endpoint logs why it refused a request (#541)

- Every `/oauth/token` refusal now logs one WARNING (ERROR for a 500): the OAuth error code and
  description, grant type, the client's policy label (or the client ID as sent) and the requested
  scopes. Codes, refresh tokens and verifiers are never logged, and client-supplied fields are
  bounded and stripped of control characters. #533's opaque `-32603` was a `400` here that left
  only an access-log line.

## 2026-11-08 - Telemetry writes no longer block the event loop (#527)

- On an event-loop thread, `McpTelemetryStore` now hands its append-only observability writes
  (MCP, failure, episode-task, lifecycle event and action, LLM-usage rows) to one ordered writer
  thread (`src/menhir/infrastructure/telemetry/async_writer.py`). A locked telemetry database no
  longer stalls every coroutine for the busy timeout. A full queue writes synchronously; nothing
  is dropped.
- Memory revisions, merge audit and destructive-op records stay synchronous: recall and unmerge
  read them back.
- The store's connections wait for queued writes first, so reads see every earlier event in order.
- Erasure waits for queued writes before it purges and fails closed (stays PREPARED) if they don't
  land, so a row queued before an erase can't survive it.
- `track_mcp_call` awaits its row in a worker thread, so the row exists when the call returns.
