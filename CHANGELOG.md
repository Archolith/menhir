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

## 2026-11-07 - Lock refresh for Python 3.15 wheels

- `uv.lock`: jiter 0.19.0, markupsafe 3.0.4, numpy 2.5.4, pydantic 2.14.3, pydantic-core 2.50.4
  and typing-inspection 0.4.5. These releases have Python 3.15 wheels; the locked ones did not.
- PyYAML 6.0.3 still has no 3.15 wheel. The README says how to install on 3.15 anyway, and
  `requires-python` stays `>=3.12`.

## 2026-11-04 - `/readyz` shows build details only to callers with a valid token

- The public `/readyz` body keeps its status fields but no longer reports the version, commit or
  metadata digest. A caller sending a bearer token the MCP endpoint would accept gets the `build`
  block. Both bodies are `Cache-Control: no-store`. A bad token gets the public body, never an
  error, and does not change the readiness answer.
- `scripts/release/check_served_metadata.py` reads the token from `MENHIR_READYZ_TOKEN`, sends it
  only over HTTPS or directly to loopback, and does not follow redirects while holding it. It now
  treats any status other than 200 or 503 as an error instead of reading the body.

## 2026-11-04 - Warning lines stop suggesting a silence; candidate text is marked untrusted

- Operator warning lines no longer tell the reader how to silence them. `acknowledge_warning` is
  unchanged and still listed for operator clients.
- `review_inbox` marks the candidates section as unreviewed, untrusted text.

## 2026-11-04 - One read-only inbox of what is waiting on a decision

New MCP tool `review_inbox` (readonly tier, #437). It lists unresolved conflicts, failed
enrichments, candidates, stale TODOs and open artifact questions in one call, with the tool that
acts on each. Plans stuck in a lifecycle state and a start-session inbox line are not included yet.

- `src/menhir/mcp/tools/ops/review_inbox.py`: the tool. One failed or hung read shows as
  `unavailable (<ExceptionType>)` and the other sections still render.
- `list_candidates` gains an optional `namespace` at every backend layer. The internal backend
  dispatch now forces a pinned client's silo onto it, as it already did for the other four reads.
  Each candidate row now also reports its `namespace`. An unscoped call is unchanged.
- Agent-tier clients do not get the tool. A production client policy must decide `review_inbox`
  for every client, or startup refuses the policy.

## 2026-11-02 - Usability charter and guardrails

Documentation only. Sets who Menhir must work for and the rules that keep new work usable.

- `docs/usability/README.md`: audiences, nine principles, and which guardrails are enforced in code.
- `docs/usability/tool-design.md`: rules for MCP tool names, descriptions, arguments, output, errors,
  safety and lifecycle.
- `docs/usability/agent-facing-text.md`: rules for server instructions, tool descriptions, the AGENTS
  template and hook output, including when a change needs an agent evaluation.
- `docs/usability/first-run.md`: standards for the first-run journeys and the gaps against them
  (#429-#441).
- `docs/usability/documentation.md`: rules for docs, and which document owns each fact.
- `AGENTS.md`, `CONTRIBUTING.md`, `.github/pull_request_template.md`: point contributors and agents at
  the guides; the PR template gains a usability checklist.
