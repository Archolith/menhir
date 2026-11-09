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
