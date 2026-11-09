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

## 2026-11-08 - v0.3.5: Client policy v3: tool profiles, a tier ceiling and deployment-owned product roles

- Client policy v3 grants tools by profile (`core`, `curate`, `operate`, `admin`) with a tier
  ceiling, and product roles belong to the deployment (#435). v1 and v2 policies still load;
  the next release drops them.
- Profile catalog 1: the first catalog. A v3 policy sets `"profile_catalog": 1`. Under v3,
  `list_clients` is an `admin` tool, so product clients no longer receive it.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.5. No dependency changes in this commit.

## 2026-11-08 - Client policy v3: tool profiles and deployment-owned product roles (#435)

- A version 3 client policy grants tools by profile: `core`, `curate`, `operate` and `admin`
  (`src/menhir/mcp/tool_profiles.py`), plus optional `allow` and `deny` lists. It resolves at load
  time to the same allowlist versions 1 and 2 spell out, so runtime enforcement is unchanged.
- Profile tools above a client's `maximum_tier` are left out and logged. An explicit `allow`
  above the tier fails the load. A v3 client's scopes must equal its tier's scopes.
- `profile_catalog` pins the catalog a policy was written for. Tools a later catalog adds are
  withheld and logged as a warning; a policy newer than the build fails the load. Catalog 1 is
  this release.
- `access_contract.products` is now the deployment's: any product names, roles `readonly`, `agent`
  or `operator` (v2: `agent` or `operator`). The fixed ChatGPT/Codex/Claude/OpenCode role table is
  gone. In v3 a product client never resolves an `admin` tool.
- Agent Smith client-metadata documents advertise the scopes the policy gives that client.
- Tier ranking lives in one place, `src/menhir/tiers.py`.
- `.agent/mcp-tools.yaml` lists the profiles, checked against the code by a test.
- Versions 1 and 2 still load. The next release drops them.
- New fixture `tests/fixtures/client-policy.v3.synthetic.json`; `.agent/endpoints.md` documents
  the policy.

## 2026-11-08 - v0.3.4: MCP initialize names the build; releases publish what they serve; policy refusals are MCP errors

- `serverInfo.version` carries the build commit as SemVer build metadata, and each GitHub release
  attaches `served-metadata.json` for a deploy to compare with `/readyz` (#498).
- A tool refused by the client policy or `MENHIR_CLIENT_TOOLS` returns an MCP error result (#503).
- A transient 402 stays transient after it is stored, so the retry poller requeues it.
- Compact recall over the stdio bridge leaves the source-memory limit to the server, and keeps
  the closest five source memories.
- A release re-run attaches `served-metadata.json` to an existing release that lacks it.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`, `docs/model-governance.md`: version 0.3.4. No dependency changes in this commit.

## 2026-11-08 - v0.3.4 audit fixes: bridge recall keeps the closest sources; release record on re-runs

- Compact `recall_memories` over the stdio bridge trimmed the server's source memories to the first
  five, which are the oldest, since the server sends them oldest-first. It now keeps the five with
  the highest cosine, still in the server's order. (Regression from the bridge-limit change.)
- A release run that finds the GitHub release already there attaches `served-metadata.json` when it
  is missing, and fails if it cannot, instead of finishing green without the record a deploy checks.

## 2026-11-08 - MCP `initialize` names the build; releases publish what they serve

- A release image reports `serverInfo.version` as the version plus the first 12 characters of
  `MENHIR_RELEASE_COMMIT`, as SemVer build metadata (`0.3.3+43e693f6b821`), so a long-lived
  connection shows which build it was opened against (#498). It still parses as the plain version.
  Without a valid commit the version is unchanged.
- `docs/agent-usage.md`: compare it with `/readyz`'s `build.commit` to spot a pre-deploy connection.
- The release workflow runs the sealed image with no network to compute `served-metadata.json`
  (version, commit, and the MCP metadata digest for every snapshot mode), checks it against the tag,
  and attaches it to the GitHub release. `python -m menhir.mcp.served_metadata` prints the record.
  A deploy compares `/readyz` with it; the GitHub release is not created without it.

## 2026-11-08 - The served-metadata check reaches a Cloudflare-fronted server

- `scripts/release/check_served_metadata.py` sends `User-Agent: menhir-release-check`. Cloudflare
  answered 403 to urllib's default `Python-urllib/x.y`, so the check could not read production's
  `/readyz`.

## 2026-11-08 - Policy refusals are MCP errors; v0.3.3 audit follow-ups

- A tool refused by the client policy or `MENHIR_CLIENT_TOOLS` now returns an MCP error result
  (`isError=true`) with the same text as before, so a client reading only `isError` no longer
  takes it for a result (#503). Other gate refusals (tier, identity, query-string auth) are unchanged.
- A transient 402 is stored with a `provider_payment_throttled:` prefix, so the retry poller,
  which sees only the stored text, requeues it as the worker did. Before, a 402 whose only sign
  was a Retry-After hint was parked for manual review.
- Compact `recall_memories` over the stdio bridge no longer sends a source-memory limit read from
  the bridge's own environment. The server applies its own default, and the bridge trims to 5.
- Upgrade note for v0.3.3: startup requires the production client policy to decide
  `review_inbox` and `acknowledge_warning` for every client, and refuses a policy that does not,
  so update the policy first. A deploy check that matched the old refusal text ("restricted by
  MENHIR_CLIENT_TOOLS allowlist") must also accept "restricted by the server's client policy",
  or, from this change, `isError=true`.

## 2026-11-07 - v0.3.3: review inbox, token-gated /readyz build details, transient-402 backoff

- New read-only MCP tool `review_inbox`: what is waiting on a decision, in one call.
- `/readyz` shows the build block only to callers with a valid token; the release check script
  reads it with `MENHIR_READYZ_TOKEN` and refuses served text that is not plain.
- A 402 with a retry hint (OpenRouter's in-flight budget) backs off like a 429 instead of
  hard-opening the LLM breaker for an hour.
- Recall: `relevance_basis` names the tier as a rank score; source memories can be filtered by
  ingest source. Conflict detection drops dissimilar pairs before review.
- Tool refusals name the control that refused; `uv.lock` refreshed for Python 3.15 wheels.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`, `docs/post-install.md`,
  `docs/model-governance.md`: version 0.3.3. No dependency changes in this commit.

## 2026-11-07 - Release gaps: refusals name their control, review_inbox labels, readyz check output

- A refused tool call names the control that refused it: the server's client policy, or
  `MENHIR_CLIENT_TOOLS`. A bound client policy now decides alone, and an empty one denies every
  tool in both the invocation gate and `tools/list` (the policy loader already rejects an empty
  one) (#503).
- `review_inbox` over the stdio bridge no longer heads an unscoped read "all namespaces": a pin
  there exists only server-side (#507). Its candidates line says the explorer is not served by
  production (#504).
- `scripts/release/check_served_metadata.py` refuses a served build field that is not plain text,
  with exit 2 and without printing it, and reports an output encoding error as exit 2 (#501).
- New test pins the auth middleware's 403 `client_policy_denied` response (#508).
- `docs/usability/README.md`: the usage row is done (#480); the served-metadata row points at #498 (#505).
- Still open: a denied call returns a text result with `isError=False` (#503). Changing that
  changes an existing test's assertion, so it waits for a decision.

## 2026-11-07 - A 402 with a retry hint backs off instead of opening the breaker for an hour

- Every HTTP 402 counted as billing (#482), so OpenRouter's `in_flight_budget_exhausted` 402, which
  carries a Retry-After and clears in seconds, hard-opened the LLM breaker for an hour and parked
  the enrichment. A 402 with a Retry-After (HTTP header, or OpenRouter's `metadata.headers`) or
  that reason is now throttling: backed off like a 429, counted toward the normal breaker
  threshold, and requeued. A bare 402, or one with a billing code, is still billing.
- `retry_after_seconds` also reads the hint from the error body, so the backoff waits as asked.
- No operator reset for a hard-opened breaker yet (#492): it needs a new operator tool.

