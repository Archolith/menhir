# Agent operating contract

Menhir is most useful when an agent treats it as two related systems: durable semantic memory and a
structural code graph. The client must supply stable workspace, namespace, project, reader, and file
context instead of asking Menhir to guess them.

## Recommended session flow

1. Bootstrap in two phases. Call `read_flagged_memories` first, then
   `recall_context_memories`, using the same stable `reader_id` and registered `workspace` key.
2. Before exploring code, call `query_structure(query_type="projects")`. If the target is absent, call
   `ingest_project` before trusting an empty structural answer.
3. Use `query_structure` for files, imports, symbols, endpoints, tests, context, and dependencies. Before
   editing a file, call `blast_radius` once for that file; use `affected_tests` to choose focused checks.
4. Use targeted `recall_memories` when a decision, failure, preference, or prior implementation fact could
   change the work. Pass `file_context` and `file_context_project` for code-related questions.
5. Verify stale anchors against the current file. An incomplete or stale project index makes an empty result
   inconclusive, not proof that no dependency exists.
6. After using recall output, call `rate_recall` honestly. This records retrieval quality; it does not alter
   memory ranking.
7. At the end of meaningful work, store durable lessons with `add_memory`. Attach a bounded Git diff when
   code paths matter. Record remaining work as a todo with a repository-relative code reference.

## Write discipline

- Store durable facts, decisions, corrections, verified failures, and reusable operator knowledge.
- Do not store secrets, raw credentials, transient progress narration, or facts that have not been checked.
- For a recall-critical semantic write in the local-stdio MVP, choose `add_memory_and_track` as the
  original write. Plain `add_memory` is fire-and-forget and provides no immediate-recall guarantee.
- `add_memory` returning `PENDING` means the write was accepted, not that it will certainly finish.
  Keep the returned episode ID. Never call `add_memory_and_track` afterward to wait on that write:
  it takes new text, not an existing episode ID, and would queue another write.
- Use `TEMPORAL` with `valid_at` for time-bound reminders. Use workspace/namespace fields explicitly; a
  workspace bootstrap key, a semantic namespace, and a structural project key are different identifiers.
- Destructive and administrative tools require stronger authority than recall. Use the least-privileged
  client tier that can perform the task.

## Local-stdio tracked-write workflow

Make one original call, with the intended authorized namespace:

```text
add_memory_and_track(
    text="The billing service uses PostgreSQL 16.",
    namespace="billing",
    timeout_s=60.0,
    poll_interval_s=1.0
)
```

The tool returns a text summary and observed transitions; it is not a streaming subscription.
Its supported options are `text`, `source`, `timeout_s`, `poll_interval_s`, `diff`,
`turn_evidence_uuid`, and `namespace`. It does not accept an existing episode ID, `type`,
`valid_at`, `flagged`, or `bootstrap_scope`. Do not invent those parameters.

Preserve the returned `episode_id`. The status tools call that argument `episode_uuid`:

```text
get_enrichment_status(
    episode_uuid="<returned episode_id>",
    namespace="billing",
    wait=True,
    timeout_s=60.0,
    poll_interval_s=1.0
)
```

Use the same authorized namespace and client. This is an observation of the existing write,
not a second write. `watch_enrichment` is another observation tool when available. A restricted
client may not expose either tool: follow its actual tool list, scope, and allowlist, report that
completion is unverified, and do not switch identities or expand permissions to work around it.

Interpret the observed result, not an assumed outcome:

| Result | Meaning and next action |
| --- | --- |
| `PENDING` | Queued work was observed; completion and worker health are not established. |
| `ENRICHING` | The stored state records a claimed job; this is not proof a worker is alive. |
| `READY` | Enrichment reports completion; verify the needed fact through recall/context separately. A query is not guaranteed to retrieve it. |
| `FAILED` | Enrichment reports a failed attempt/state, not deletion or necessarily permanent failure. Inspect the existing episode and use authorized repair/retry, not duplicate ingestion. |
| `timed_out: True` | The observation window expired; it did not cancel the queued write or prove it failed. Continue observing the same episode. |
| `not_found`, `UNKNOWN`, or tracking unavailable | Completion is unverified. A missing/failed status read does not prove the accepted write failed. Preserve its receipt; do not resubmit based only on this observation. |

An empty immediate recall after plain `add_memory` is not evidence that the write failed or that
no memory exists. Likewise, a status read is not a relevance test. Do not claim recall succeeded
until the needed fact actually appears in an authorized recall/context result.

When the write requires options supported only by `add_memory`, use that original write followed
by status observation of its returned episode ID, subject to the supported feature contract and
client permissions. `add_memory(type="TEMPORAL", valid_at="YYYY-MM-DD")` is different: it writes
a time-bound memory directly and bypasses graph enrichment. Do not require a queued episode or
an enrichment `READY` transition for that path. Retention/flag semantics remain governed by the
separate MVP retention work; this workflow does not certify them.

## Beacon generation (issue #120)

`menhir beacon generate PROJECT --repo ABSOLUTE_ROOT --beacon-python PATH` generates
`beacon.generated.yaml` in the repository root from Menhir-held indexed project knowledge.
Generation is conservative and fails closed: it requires an intact, complete index whose
recorded root matches the requested repository, a scan fingerprint, an indexed project
description, and at least one indexed canonical document. It never invents purpose, commands,
guardrails, or concepts to fill schema fields.

Generated evidence is labeled `experimental`, not `current`. Menhir bookends its graph reads with
the structure-writer revision and rechecks both graph and filesystem immediately before publication,
but arbitrary repository editors do not share that lock. The artifact is therefore a verified
point-in-time projection of its cited scan fingerprint, not a claim that the checkout remains current
after publication.

- The output is always the sidecar `beacon.generated.yaml`, never a hand-authored `beacon.yaml`.
- Initial generation refuses an existing output. Refresh requires `--refresh` plus
  `--expected-sha256` matching the existing generated file; foreign or hand-edited outputs are refused.
- The Beacon package lives in a separate interpreter (`--beacon-python`) because Menhir and Beacon
  require incompatible `archolith-mcp-framework` versions. Install the same contract revision CI
  uses: `pip install "git+https://github.com/Archolith/beacon.git@447abd00a08b3082188da63f342ffdeff7a2dd5e"`.
  That revision provides `beacon build --menhir-evidence`; the PyPI `0.1.0` package does not.
  Menhir never imports Beacon directly; every artifact is serialized and validated by Beacon's own
  parser and validator before publication.
- The published manifest passes `beacon validate` with zero errors and serves through Beacon's stdio
  server (`BEACON_MANIFEST_PATH=<path> beacon`).

## Failure behavior

- If Menhir is unavailable, do not pretend its history or structural graph was checked. Continue only when
  the task can safely rely on current local evidence, and state the limitation.
- If a project is missing from `query_structure(query_type="projects")`, ingest or re-ingest it before
  relying on project-scoped queries.
- If recall marks a file anchor stale, inspect the current file before acting on the memory.
- If tool discovery is incomplete, use the MCP client's tool discovery mechanism instead of inventing a
  tool name or argument.

## Ready-to-copy default

The maintained, paste-ready instruction block is
[`templates/AGENTS.menhir.md`](templates/AGENTS.menhir.md). Keep it short in consumer repositories and link
back here for explanation instead of duplicating the full Menhir reference documentation.
