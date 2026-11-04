## 2026-11-04 - FalkorDBLite compatibility spike (#109)

- `.agent/memory-backlog.md`: links the phased workspace roadmap and conditional LLM token budget.
- `.agent/memory-backlog.md`: future adapter gates, rollback evidence and the separate Windows fork investigation.
- `scripts/falkordblite_compatibility.py`: opt-in isolated storage probe for the checkout's Graphiti
  fork, entity/edge replay, vector/fulltext search, rollback failures and orderly restart persistence.
- `tests/test_falkordblite_compatibility.py`: offline refusal and result-reporting checks.
- `.agent/reference/menhir-falkordblite-compatibility.md`: measured transaction blockers, Windows
  limitation and remaining acceptance gates; indexed in `.agent/reference/README.md` and
  `.agent/scripts-index.md`, with the investigation boundary recorded in `.agent/architecture.md`.
  Runtime storage and dependencies remain unchanged. Older changelog entries remain in Git history.

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

## 2026-11-02 - One command prepares a version or fork bump

`scripts/bump.py` replaces the by-hand edits before a release. No runtime code changes.

- `scripts/bump.py`: `version` and `fork` subcommands update every file that states the version or
  pin, or write nothing if an expected line has moved. `uv lock --check` must pass, and `--pr` opens
  the pull request. Tagging stays in `scripts/release.py`.
- `tests/test_bump.py`: covers partial writes, restores, and CRLF files. It also fails if a new file
  names the current version or fork pin and the bump would miss it.
- `docs/runbooks/release.md`: step 2 and the fork pin procedure.

## 2026-11-02 - One tag publishes the whole release

A `v*` tag now publishes PyPI, the release image, the image's version tag and the GitHub release
in one run. Nothing is typed in by hand, and a stopped release can be finished by a re-run.

- `.github/workflows/publish-pypi.yml`: calls `release-image.yml`, verifies the attestation with
  the identity `menhirctl.ps1 upgrade` uses, tags the image `X.Y.Z`, then creates the GitHub
  release. The test gate is unchanged.
- `deploy/release_pipeline.py`, `deploy/release-python-base.txt`: derive the release inputs, skip
  finished stages, and refuse to move an image tag or upload over a different PyPI file set.
- `scripts/release.py`: wait up to three hours for the longer run.
- `docs/runbooks/release.md`: the new flow and how to finish a stopped release.

## 2026-11-02 - v0.3.2: retry replies cut off at the output limit

- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`,
  `docs/model-governance.md`, `docs/post-install.md`: version 0.3.2. No dependency changes in this
  commit.
- Includes the truncation retry and fork 0.31.2.post2 (#424), the parallel `MENTIONS` retention fix
  (#422) and the release command error report (#420).

## 2026-11-02 - Retry LLM replies cut off at the output limit; Graphiti fork 0.31.2.post2

A reply cut off at the output token limit could be parsed into a nested fragment, so the retry with
a higher limit never ran, or the fragment was accepted. The fork now raises `OutputTruncatedError`
when the provider reports `finish_reason=length`, and Menhir treats that as truncation.

- `infrastructure/graphiti_llm_adapter.py`: `_looks_like_truncation` returns true for
  `OutputTruncatedError` at any reply length. The retry limits are unchanged: 4096, 8192, 16384,
  then 16384 with a request for a smaller extraction.
- `tests/test_graphiti_truncation_retry.py`: real-client tests for the escalation.
- `pyproject.toml`, `uv.lock`: `archolith-graphiti-core==0.31.2.post2`; pins, docs and release
  checks updated.

A prose-wrapped reply that is cut off, when the provider does not report `length`, can still parse
to a fragment.

## 2026-11-01 - v0.3.1: scalar values checked against their source (#411)

Hotfix on 0.3.0. A wrong value in five scalar kinds could become a current View if 2 of 3 model
samples agreed on it, because nothing compared it with what the user said.

- `services/typed_scalar_rules.py`: `parse_scalar_row` now drops a duration, frequency, weekday,
  status or boolean value the source does not state, or that the source negates, hedges or asks
  about. The clause is read from the source, so a short quote cannot leave a negation out. Status
  and true booleans are also dropped in the past tense or in a "who" clause. `expire` rows are
  exempt.
- `uv.lock`: `joserfc` 1.7.3 -> 1.7.7 (GHSA-jpxq-7h8p-2q76, GHSA-9pg5-8c7x-5hfx).
- `tests/test_scalar_source_grounding_411.py` (new). `tests/test_boolean_polarity_consistency.py`:
  three cases asserted `has_passport` from unrelated sentences and now name their own attribute.
- `docs/personal-memory-readiness.md`, `docs/post-install.md`, `README.md`: what the checks do and
  do not cover.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `docs/governance.md`: version 0.3.1.

Values stored before 0.3.1 are not rechecked. The benchmark was not rerun for this hotfix, so the
Gate E results in #123 describe 0.3.0.

## 2026-11-01 - v0.3.0: release version for candidate 44933985 (#123)

The frozen MVP release candidate `44933985` (#123) gets its own version: 0.2.3 is already on PyPI.

- `pyproject.toml`, `uv.lock`: version 0.2.3 -> 0.3.0. No dependency changes.
- No source changes since `44933985`. Outside `src/`: offline tests also run on Windows/py3.12 and Ubuntu/py3.13, test fixes for those platforms, and community files (CONTRIBUTING, Code of Conduct, issue and PR templates). `docs/personal-memory-readiness.md` gained the scalar grounding known limitation (#412; post-MVP follow-up #411).
- `CHANGELOG-archive.md`: the oldest former current entry moved there (10-entry limit).
- Docs accuracy pass for the release: known limitations (#169, #417) and the Beacon deferral (#120) are stated; defaults, ports, retired settings, `menhir sync` upload behavior and runbook commands are corrected; `docs/roadmap/menhir-mvp-roadmap.md` is now a pointer to the release plan.

## 2026-10-31 - LLM clients resolve each host once (#409)

Under 500-way ingest every default-executor thread can be busy with sync Neo4j calls. asyncio resolves hostnames on that same pool, so each new OpenAI connection waited behind graph I/O and hit the SDK's 5 s connect timeout. Graphiti embeds failed with `APITimeoutError`, and source-memory embeds were skipped.

- `infrastructure/dns_cache.py` (new): an httpcore network backend that resolves each host once per 5 minutes on its own 2-thread pool. Concurrent lookups for a host share one call. It connects to the cached IPs in order, re-resolves after every address fails, and leaves IP literals alone. TLS still verifies the hostname, because httpcore passes it as `server_hostname`.
- Follow-up: each lookup runs on its own short-lived thread, in a copy of the caller's context, instead of a `ThreadPoolExecutor`, which the CF-79 ratchet bans. The thread is listed in `_KNOWN_RAW_THREADS`, and the lookup's `wait_for` is listed in `_TIMEOUTS_ALLOWED`. Neither makes a model call.
- `infrastructure/observability.py`: every client `build_async_openai_client` returns uses it. Remote clients get `openai.DefaultAsyncHttpxClient`, keeping the SDK's timeout, connection limits and redirects; the local-llama client keeps httpx defaults.
- Not fixed here: graph I/O itself still shares the default executor, and a plain `TimeoutError` from the 300 s episode deadline still parks the episode as `manual_review` (#409).
- Tests: new `tests/infrastructure/test_dns_cache.py`. Changed: `tests/test_observability.py` now expects remote clients to get an `http_client`.
- `CHANGELOG-archive.md`: the oldest former current entry moved there (10-entry limit).

## 2026-10-30 - Delta-audit remediation (CF-259..CF-306)

Fixes from the 2026-10-29 delta audit (`ca3f762f..0e078983`). The root findings register ("Delta audit 2026-10-29") carries each CF's status and commit, and lists the residuals. CF-277 is held for an owner decision, CF-285 is closed by design (dates stay UTC), and a new High finding, CF-307, is fixed (see LLM retry below).

Operator-visible changes:

- **Telemetry:** Graphiti fork PostHog telemetry is off by default. Menhir sets `GRAPHITI_TELEMETRY_ENABLED=false` at client construction unless the operator set it, and `GRAPHITI_TELEMETRY_ENABLED=true` turns it back on (CF-272).
- **Install:** the documented Neo4j floor is 5.26 (README, `docs/post-install.md`). `python-multipart` is declared. A missing or incomplete `graphiti_core` now fails with a message naming `archolith-graphiti-core==0.31.2.post1` (CF-297, CF-303..306).
- **Embedding model:** the episode embedding stamp now records the configured embedding model instead of `unknown`, so the source-memory model filter applies. Legacy `unknown` stamps still match; blank or missing stamps stay excluded. Changing the embedding model on an existing graph is not supported: recall skips vectors made by another model, and the scheduled repair job and the `--dry-run --require-complete` check do not see them. A model change needs a full re-embed or a fresh graph (CF-260).
- **Settings:** the flex request timeout is validated at settings load. A non-numeric, non-finite or non-positive value fails startup and names the env var, even when the flex tier is unused (CF-286). A retired flag explicitly set to false logs a startup warning (CF-301).
- **Redaction:** JWKS failure logs redact URLs fail-closed (CF-300). Recall Lab redaction is an allowlist on every route (CF-269).
- **LLM retry:** the outer LLM retry no longer retries provider 429s; the rate-limit layer owns them (CF-262). One bad anchored-time item drops only itself, not the episode (CF-263). A provider 429 that Graphiti rewraps as its own `RateLimitError` ("Rate limit exceeded", no "429" in the text) now requeues the episode like a raw 429, instead of parking it in `manual_review` for good. An `insufficient_quota` 429 still parks (CF-307). Errors that cannot succeed on retry now get one attempt: a 4xx other than 408/409 (bad request, auth, 402) and an unimplemented backend. Billing 429s (`billing_hard_limit_reached`, `credit_balance_*`, "exceeded your current quota") are treated as quota like `insufficient_quota`. Conflict review stops under a standing chat quota and leaves the remaining groups pending. 5xx, timeouts and throttle 429s still retry (CF-262).
- **Extraction on gpt-6:** the contained-edge prune is off for the gpt-6 profile, the only one that enabled it, so no shipped profile prunes. Token containment cannot tell whose fact an edge states, so the prune could delete a user's own fact and its node. gpt-6 extractions now keep restated sub-facts as redundant edges, and `contained_edges_pruned` stays 0. The code is kept; re-enabling it needs a meaning-based check (#402, CF-259).

Behaviour fixes:

- **Extraction:**
  - The contained-edge prune keeps a fact when the keeper cannot see its endpoint (CF-259, before it was switched off; see above).
  - Status reporters follow the verbatim-skip projection everywhere, including `list_enrichment_queue`, the explorer and READY counts (CF-261).
- **Counting and scalar state:**
  - The related pool is fetched per candidate (CF-264). The decide step now sends up to 50 related rows per candidate and 200 per page, up from 10 and 40, so a recurring happening counts up to 51 times instead of stopping at about 11. Decide prompts can be up to 5x longer. Known issue: past 50 related rows a candidate is still never counted (#405 mitigated; removing the cap is post-MVP Archolith/menhir#407).
  - An entity merge re-arms moved counted rows for re-decision (CF-265).
  - Rebuild failures stay owed, and rebuilds and coverage audits fold with the writer's parameters (CF-266, CF-267, CF-270, CF-290).
  - The LME repair script uses the runtime counting and history flags (CF-289).
  - A zero-row counted View clears stale `CONTRIBUTED_TO` edges (CF-291).
  - `purge_scalar_state_nodes` also removes history Views and `scalar:` read markers (CF-292).
  - Phase3 dirty signals read the live scalar cursor (CF-273).
- **Recall:**
  - `recall_timeline` query mode anchors past unanchorable hits (CF-268).
  - CLI hook recall honours scalar history like the backend (CF-278).
  - `fetch_temporal_facts` drops its unused `namespace` parameter (CF-279).
  - A failed recall cancels its source-memory task (CF-281).
  - Receipts name scalar Views by `view_name` (CF-282).
  - Typed-history `prev_offset` is clamped at 0 (CF-283).
  - `agent_quoted` matching ignores case, surrounding whitespace and `-`/`_` in Python and in the two scalar foundation queries (CF-287).
- **Graph-op journal:**
  - Transitions are owner-checked compare-and-set (CF-271).
  - A refused `mark_committed` now logs at ERROR and writes a failure event (`failure_stage="commit_refused_after_graph_work"`). Every caller commits only after its own graph work, so the graph may hold the after-state while the journal says FAILED. The journal state is not changed; escalating to NEEDS_REVIEW is #404 (CF-271 F2). That event now keeps the op id, kind and journal state; they had been redacted as unscoped telemetry. A writer whose op another owner already committed now gets `StaleTransitionError` instead of an idempotent success, which is tracked by a new nullable `resolved_by_token` column added on startup. A repeat commit by the same owner is still idempotent (CF-271 review F-1, F-2). Upgrade every process that shares a journal file: a commit made by an older process leaves the column empty, so it gets no different-owner check.
  - The committed-pairs scan pages by keyset on `(created_at, rowid)` (CF-275).
  - Telemetry SQLite connections close when their `with` block exits (CF-276).
- **Eval script:** `scripts/eval_anchored_time.py` routes the resolver like production, reserves budget before each call, and stops before scoring a set the cap cut short (CF-288).
- **By design, documented:** CF-274, CF-280, CF-294, CF-299. CF-284's premise was false: nothing calls `_belief_commit_context`.

Tests: each unit added counterexample tests; online tests ran against a throwaway Neo4j 5.26. Full offline suite: 12,234 passed, 914 skipped, 1 failed, 14 errors in 10m48s. The failure was `test_large_module_boundaries` (`telemetry/store.py` at 426 lines against its 425 cap, from the CF-269 comment), fixed in ee0e88d0 and rerun green. The 14 errors are `test_beacon_generation`, which requires `MENHIR_TEST_BEACON_PYTHON` and fails loudly when it is unset (an environment gate).

`CHANGELOG-archive.md`: the oldest dated entry moved there (10-entry limit).
