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

## 2026-11-05 - Filter recall's source memories by ingest source

For agent-written memories (#168): a caller can ask for just the agent-writer items, ranked among themselves, instead of fetching 50 and filtering client-side.

- `POST /api/recall` takes `source_memory_sources` (1-20 exact `source` strings, stripped and de-duplicated; anything else is 422). The source-memory search keeps only episodes with one of those sources, before `source_memory_limit` is applied. It does not turn the section on: that is still `source_memory_limit` or the deployment default. Without the field, recall is unchanged and the kwarg is not passed down.
- `domain/recall.py`: `normalize_source_memory_sources`, shared by the request model and the backend runtime, since the internal backend route skips `RecallRequest`. Threaded through `backend_client_ops`, `backend_protocol`, `backend_runtime_data_ops`, `RecallService`, `run_recall`, `_build_source_memories`, `MemoryGraphAdapter` and `MemoryQueryRepository.search_episode_embeddings` (`n.source IN $sources`; an empty sequence matches nothing).
- Not on the `recall_memories` MCP tool.
- Tests (new): `tests/test_source_memory_source_filter.py`, including an online test showing the filter applies before LIMIT.

## 2026-11-05 - Conflict detection drops dissimilar pairs before review

- `classify_pair` now reads the pair's real name cosine and drops conflict and merge candidates
  below `CORRELATION_SIMILARITY_FLOOR` (0.40). The floor can only turn a route into `none`; a
  missing vector, a read error or a PROMOTED member keeps today's route. Each drop is recorded as
  `correlation_similarity_floor`. Routing above the floor still uses the RRF rank score, because
  cosine cannot tell a contradiction from a duplicate.
- The shared-episode veto is always on. A pair that one episode asserted together, with no shared
  attribute word, is dropped instead of queued. Known limit: a same-episode correction with no
  shared word ("Lisbon residence" / "Porto home") is dropped too.
- A floor or veto drop ends that node's contradiction scan, as a queued conflict does, so a drop
  never reaches hits the scan would not have classified. A dropped pair writes nothing to the
  graph: the node stays scannable and merge-eligible, and a rescan records the drop again.
- `list_conflicts` shows each member's scope and a note on how the group may have been created.
- `tests/fixtures/conflict_pairs.json` pins what 42 hand-written pairs do. The floor is fitted to
  those pairs, so the fixture records behaviour and does not measure accuracy.
  `scripts/build_conflict_fixture.py` refreshes its cosines.

## 2026-11-06 - Recall's `relevance_basis` says the tier is a rank score

`relevance_basis` changes from `legacy_rrf_threshold_unvalidated` to
`rank_score_threshold_not_similarity` on MCP and REST recall results. The tier has now been tested:
at least one `high` result came back for 98.5% of real LongMemEval questions and for 92.5% of null
questions (unanswerable, or swapped onto an unrelated namespace). The `relevance` key, its values, scores, ranking and the 0.15 floor are
unchanged. A client matching the old string must update it.
