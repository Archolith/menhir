# Enrichment Stall Troubleshooting

Use this when an episode stays `ENRICHING` for too long or keeps consuming attempts without reaching `READY`.

## Known Failure Pattern

Current high-value pattern:

- episode remains `ENRICHING`
- `processing_stage = graphiti_extracting`
- `processing_substage = llm_request_started`
- heartbeat keeps advancing
- `processing_llm_last_task_at` stays stale
- scheduler proxy request can remain open for hours

Interpretation:

- the worker loop is alive
- the failure is below the episode layer
- treat it as a stuck Graphiti/llama request path first, not a simple retry-budget issue

Do **not** start by blaming:

- bootstrap `acquire` spam
- stale `PENDING` rows alone
- memory-side timeout length alone

Those can exist, but the decisive signal is the lower-level request lifecycle.

## First Checks

1. Inspect the episode row directly.

PowerShell:

```powershell
@'
from menhir.core import build_memory_services
EPISODE_UUID = 'replace-me'
built = build_memory_services()
row = built.graph_adapter.fetch_episode_processing(EPISODE_UUID)
print({
    'uuid': row.get('uuid'),
    'state': row.get('processing_state'),
    'stage': row.get('processing_stage'),
    'substage': row.get('processing_substage'),
    'attempts': row.get('processing_attempts'),
    'error': row.get('processing_error'),
    'heartbeat': str(row.get('processing_heartbeat_at')),
    'last_task': str(row.get('processing_llm_last_task_at')),
    'active_task': row.get('processing_llm_active_task'),
})
'@ | .\.venv\Scripts\python.exe -u -
```

2. If heartbeat is moving but `substage` stays `llm_request_started`, inspect scheduler and llama before changing timeouts.

Also pull the compact trace bundle first:

- MCP tool: `get_episode_trace(episode_uuid=...)`
- MCP resource: `memory://system/lifecycle-trace`

`get_episode_trace` is the fastest way to see:

- current durable queue row
- per-episode LLM task events
- per-episode failure history
- per-episode lifecycle/debug events

Check:

- scheduler `/status`
- scheduler `/llama/slots`
- how long the current proxy request has been open

If the proxy request has been open for minutes or hours, that is the primary bug.

3. Check GPU state.

```powershell
nvidia-smi
nvidia-smi dmon -s pucm -c 5
```

If GPU `sm` is pinned near `100%` while the episode stays at `llm_request_started`, treat that as active or wedged inference, not a dead GPU.

## Proven Live Repro

This repro proved that attempts are consumed by Graphiti extraction timing out:

```powershell
@'
import asyncio
from menhir.core import build_memory_services

EPISODE_UUID = 'e153c57b-35f9-44c4-9190-d55362db00a0'

async def main():
    built = build_memory_services()
    reset = built.graph_adapter.force_reset_failed_episode(EPISODE_UUID)
    print({'reset': reset})
    await built.ingest_service._process_pending_episode(EPISODE_UUID)
    row = built.graph_adapter.fetch_episode_processing(EPISODE_UUID)
    print({
        'uuid': row.get('uuid'),
        'state': row.get('processing_state'),
        'stage': row.get('processing_stage'),
        'substage': row.get('processing_substage'),
        'attempts': row.get('processing_attempts'),
        'error': row.get('processing_error'),
    })

asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -u -
```

Observed result:

- `state = FAILED`
- `attempts = 1`
- `error = graphiti add_episode timed out after 300.0s`

Meaning:

- the thing consuming attempts is the Graphiti extraction call hitting the timeout boundary
- not bootstrap `acquire` events by themselves

## Secondary Bug: Dead PENDING Rows

There is also a queue hygiene bug:

- stale lease recovery can reset `ENRICHING -> PENDING`
- but leave `processing_attempts` at retry ceiling
- `claim_pending_episode(...)` then refuses to claim that row

Symptom:

- episode sits in `PENDING`
- attempts already at max
- no progress is possible until manual reset or code fix

This is real, but it is secondary to the stuck request issue above.

## What To Challenge

Challenge any fix that only:

- raises timeout from `300s` to `900s`
- retries more aggressively
- focuses on `acquire: memory: graphiti bootstrap`

Those are weak unless backed by evidence that the request is actually making progress and eventually succeeds.

An open proxy request for hours is not a valid reason to inflate the enrichment timeout.

## Better Next Steps

1. Instrument the Graphiti extraction request boundary.
2. Correlate one active episode with scheduler `/status` and `/llama/slots`.
3. Add scheduler-side stuck-request timeout/cancellation if proxy requests can remain open indefinitely.
4. Fix stale-lease reset so exhausted rows do not return to unrunnable `PENDING`.

## Current Recovery Behavior

- MCP startup now performs an orphaned-row reconciliation pass before stale-lease recovery:
  - `ENRICHING` + no `processing_owner` -> `PENDING`
  - `processing_error = orphaned_enriching_reset`
- MCP shutdown now releases rows owned by the current worker:
  - `ENRICHING` + matching `processing_owner` -> `PENDING`
  - `processing_error = worker_shutdown_release`
- This is intended to reduce post-crash/manual-restart queue jams, but it does not replace root-cause debugging for hangs inside `GraphitiClient.add_episode()`.

## TODO - Root Cause Follow-up

- The scheduler-idle watchdog is only a containment fix.
- The actual root issue is still unresolved if:
  - llama task completes
  - scheduler child task completes
  - but `GraphitiClient.add_episode()` never returns
  - and `IngestService` never reaches `graphiti_response_received` / `mark_episode_ready`
- Do not treat the watchdog as the final fix. Keep tracing the post-request Graphiti path until the parent episode transitions correctly without watchdog intervention.

## Related Files

- `src/menhir/services/ingest_service.py`
- `src/menhir/infrastructure/graphiti_client.py`
- `src/menhir/infrastructure/observability.py`
- `src/menhir/infrastructure/memory_graph_adapter.py`
