---
artifact_schema: 1
artifact_uuid: b2b70db2-6674-49ed-8231-29c710d26c2a
artifact_type: plan
artifact_status: IMPLEMENTED
---

# Menhir: anchored-time resolver, P1 implementation plan

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical. Line references to the design doc use its numbering before the move.

Disposition: IMPLEMENTED (was: DRAFT - 2026-10-06)
Last verified: 2026-10-21 - DONE (high); anchors checked: PR 244 (merged 2026-10-08), PR 274
Parent design: [menhir-anchored-time-resolver-plan.md](menhir-anchored-time-resolver-plan.md)
(P0 verdict: GO to P1).
Code baseline: menhir 29bcbd9 (smoke/gpt6-flex). P1 must branch from a commit that contains
29bcbd9's `graphiti_extraction_policy.py`, including the containment prune.
graphiti_core = archolith fork 0.30.2.post2.
Prototype being ported: `reich_lib.py` sha256 a7797891 (prompt, detector,
calendar), `clause_guard.py` sha256 ec4472e9, `check_guard.py`, `run_replay.py`.

**The one decision for the user: the integration point (section 3). Recommended: A-hook.**
**Decided (user, 2026-10-07): A-hook**, as implemented.

## 1. Goal

Ship the P0 resolver inside Menhir ingest, behind a default-off flag, so that with the flag on
a cue-bearing user turn gets correct event times on its extracted edges before Graphiti's
dedupe, contradiction and expiry logic runs. With the flag off, ingest is byte-identical to today.

P1 contents:
- deterministic cue detector (gate),
- one resolver LLM call per gated episode, prompt frozen at a7797891,
- deterministic calendar (`compute`) and the clause guard,
- an overlay that writes `valid_at` on the in-memory edges, only under the rules in section 6,
- an in-memory data contract for P2 (not persisted),
- focused tests and an offline replay harness against the P0 gold (no paid calls in CI).

## 2. Non-goals (deferred)

- P2: new edge properties (`time_expression`, `time_basis`, ...), recall rendering by basis,
  timeline ordering by window. P1 defines the contract (section 7) but persists none of it.
- P3: the expiry rule change (`invalid_at` on point events / bounded states is not supersession).
  P1 never sets `invalid_at` and never touches `expired_at`.
- P4: cross-turn and cross-session anchors resolved at consolidation.
- Any prompt wording change. The prompt stays at a7797891; only mechanical changes (section 5).
- Replacing Graphiti's batch timestamp call. P1 overlays it; the batch call still runs (design
  doc line 35 says "replacing"; see section 13).
- LME re-ingest. Not until P1-P3 land.
- Backfill of existing graphs.
- The open P0 items, listed in section 15: model-year guard, attribute-facts rule, detector
  early/mid/late month and age forms, fact-text precedence, second non-LME eval corpus.

## 3. Integration point (USER DECISION)

### Recommendation: A-hook

Run the resolver at the end of `_run_graphiti_combined_extraction`, immediately before
`return nodes, edges, index_map` (graphiti_extraction_policy.py:1573), after the containment
prune (1553-1572). This is a variant of the design doc's option A: the same moment in the
pipeline, but at Menhir's own extraction-hook seam, not inside the LLM proxy.

Evidence:
- Graphiti's batch timestamp call runs inside `extract_nodes_and_edges`
  (graphiti_core combined_extraction.py:233-278), which Menhir calls at
  graphiti_extraction_policy.py:1444. So at 1573 every final edge already carries Graphiti's
  `valid_at`/`invalid_at`. The resolver sees the final edge list, after the relationless repair,
  author correction, self binding (1540) and the containment prune. It runs once, on the facts
  that will actually be resolved.
- The hook result goes to the fork as `SingleEpisodeExtractionResult` (1607-1611). The fork
  returns it unchanged (graphiti.py:728-729) and uses the edges as `precomputed_edges`
  (graphiti.py:835-836), which then enter `resolve_extracted_edges` (graphiti.py:851). So the
  resolver's times are in place before:
  - the per-edge fallback timestamp call (edge_operations.py:576-620, called at 680 and 813;
    it is skipped when `valid_at` or `invalid_at` is already set, 587-588),
  - contradiction invalidation of older edges (`resolve_edge_contradictions`,
    edge_operations.py:538-573, which compares `valid_at`/`invalid_at`),
  - expiry of the new edge against later candidates (edge_operations.py:822-839).
- Everything the resolver needs is in scope: the source text (`receipt.episode_text`), the speech
  time (`episode.valid_at`, which Graphiti sets from `reference_time` at graphiti.py:1363 and
  which Menhir passes as the turn's `occurred_at` or now, enrichment_steps.py:179-196 and 628),
  and typed `EntityEdge` objects with `.fact`.
- No coupling to fork prompt names or response-model names.

### Alternatives

- A-proxy (the design doc's literal A): intercept
  `prompt_name='extract_edges.extract_timestamps_batch'` / `response_model=BatchEdgeTimestamps`
  in `_PayloadSanitizingLLMClient.generate_response` (graphiti_extraction_policy.py:1374-1384,
  which today only acts on `CombinedExtraction`). It is feasible, since the receipt carries the
  text. But it couples to fork prompt and model names. It fires once per extraction pass,
  including the relationless repair pass, so the resolver would also pay for edges that are later
  discarded. And it sees only the fact strings the batch prompt sends, not the `EntityEdge`
  objects. Choose it only if P1 later wants to replace (suppress) the batch call.
- B, a post-episode rewrite after `add_episode` returns: decoupled, but contradiction and expiry
  have already run on the wrong times (edge_operations.py:538-573, 822-839). It also needs a
  second graph write per edge, and it would have to undo `invalid_at`/`expired_at` decisions that
  were made on wrong times. Not recommended.

### Hook coverage limits (all of these keep today's behavior)

- No active receipt: the hook returns None (graphiti_extraction_policy.py:1592-1594), so callers
  that do not go through `add_episode_with_timeout` are not resolved.
- Custom edge types: the SEPARATE route (1595-1596).
- Bulk/multi-episode extraction paths that do not use the single-episode hook.

## 4. Files and symbols to add or change

New:
- `src/menhir/infrastructure/anchored_time.py`. Ported from the prototype, pure and synchronous:
  - `CUES`, `detect(text) -> list[str]` (reich_lib),
  - `SYSTEM`, `INSTRUCTIONS`, `build_messages(turn, speech_date, facts)`,
  - calendar helpers `_add_months`, `_month_window`, `_shift`, `_season`, `_calendar`, `_explicit`,
    `compute`, `_TOL`, `_TOL_APPROX`,
  - `parse_output`,
  - `clause_guard` and its helpers (clause_guard.py),
  - `PROMPT_VERSION = "a7797891"`.
  - New pure functions:
    - `parse_items(content) -> tuple[dict[int, dict], list]`, mirroring run_replay.py:90-103,
    - `window_midpoint(window)`, from `_mid`,
    - `granularity_of(item)`,
    - `plan_overlay(edges, items, speech_date) -> list[AnchoredTimeResult]`, the section 6 rules
      as a pure function of its inputs.
  - Dataclasses `AnchoredTimeResult` (the section 7 contract) and `AnchoredTimeReport`
    (per-episode telemetry).
- `src/menhir/infrastructure/anchored_time_resolver.py`. The async I/O part:
  - `AnchoredTimeResolver(client, base_url, model, timeout_s)`,
  - `async def resolve(turn, speech_date, facts) -> ResolverOutcome`, which never raises except
    on cancellation,
  - a bounded in-process result cache (section 9).
- Tests and fixtures: section 11.
- `scripts/eval_anchored_time.py`. Scoring and the live eval CLI (section 12). Not product code.
  The offline test imports its scoring functions, following the precedent of
  `tests/test_analyze_recall_lab_scores.py` with `scripts/analyze_recall_lab_scores.py`.

Changed:
- `src/menhir/config/settings_model.py`: three fields on `MemorySettings` (next to
  `canonical_self_binding_mode`, line 219) and three `from_env` reads (next to line 845). See
  section 5.
- `src/menhir/config/feature_flags.py`: one `_SETTINGS` row for the bool only (registry rule:
  bools and `*_mode` fields must be registered; tests/test_feature_flag_registry.py:165-170).
  Category `runtime`, `recall_affecting=True`, because it changes stored `valid_at`.
- `.env.example`: a `# MENHIR_ANCHORED_TIME_RESOLVER=false` line. Required by
  `test_every_control_is_documented_with_no_exemptions` (test_feature_flag_registry.py:97-99).
  Also add commented lines for the model and timeout envs.
- `src/menhir/infrastructure/graphiti_client.py`, `from_settings_with_capabilities`:
  - inside `if llm_enabled:` (line 251), when `settings.anchored_time_resolver_enabled`, build
    `AnchoredTimeResolver(client=_ProviderExtrasAsyncClient(raw_llm_client, llama_base_url),
    base_url=llama_base_url, model=settings.anchored_time_resolver_model or
    llm_provider.chat_model, timeout_s=settings.anchored_time_resolver_timeout_s)`;
  - at line 343, pass `MenhirExtractionHook(anchored_time=resolver_or_None)`.
  - Flag off: `MenhirExtractionHook()` is constructed exactly as today.
- `src/menhir/infrastructure/graphiti_extraction_policy.py`:
  - `MenhirExtractionHook.__init__(self, anchored_time=None)`, which stores it. The class has
    no `__init__` today.
  - `extract_single_episode` passes it to `_run_graphiti_combined_extraction(...,
    anchored_time=self._anchored_time)`, a new keyword-only parameter defaulting to None.
  - Before line 1573:
    `if anchored_time is not None: await _apply_anchored_time(anchored_time, receipt, episode, edges)`.
  - `CombinedExtractionReceipt` gets one field: `anchored_time: AnchoredTimeReport | None = None`.
    The receipt is never serialized as a whole (only named counters are read: enrichment_steps.py
    1084-1089, graphiti_resolution_policy.py:67-97), so the new default-None field changes nothing
    when the flag is off.
- `.agent/default-off-features.md` (Menhir repo): a new ledger row with Feature, Flag/env, Gate
  location (graphiti_client.py hook construction, plus graphiti_extraction_policy.py before 1573),
  Bench status ("offline replay only; no LME ingest"), and Source plan (this file).

Not changed:
- graphiti_core.
- `bootstrap.py`, `ingest_queue.py`, `ingest_service.py`, `ingest_worker.py`, `enrichment_steps.py`.
  The hook is built from settings at client construction, so no per-episode threading is needed.
  This is unlike `canonical_self_binding_mode`, which runs through bootstrap.py:213 ->
  ingest_queue.py:72-90 -> ingest_worker.py:168 -> enrichment_steps.py:129/636/1613.
- No existing test file is modified.

## 5. Flag, settings and resolver client

| Setting | Env | Default | Notes |
|---|---|---|---|
| `anchored_time_resolver_enabled: bool` | `MENHIR_ANCHORED_TIME_RESOLVER` | False | `parse_bool_env(_getenv(...))`; registered in feature_flags |
| `anchored_time_resolver_model: str` | `MENHIR_ANCHORED_TIME_MODEL` | "" | empty = Graphiti chat model; follows `personal_memory_consolidation_chat_model` (settings_model.py:276/867) |
| `anchored_time_resolver_timeout_s: float` | `MENHIR_ANCHORED_TIME_TIMEOUT_S` | 120.0 (60.0 until L1, see section 18) | validated > 0 in the existing validation block (near 569) |

The flag is read once, at Graphiti client construction. Toggling it needs a restart, like every
other client-construction setting.

Resolver call path:
- It uses the same traced client object Graphiti uses. Calls go through
  `_InstrumentedEndpoint.create` (observability.py:405-430), so they are counted by
  `start_llm_usage_call`, and they get the DeepSeek/OpenRouter provider extras
  (graphiti_llm_adapter.py:318-360).
- It calls `acreate_chat_completion(client.chat.completions.create, request, base_url=...,
  label="anchored_time.resolve")` (openai_calls.py:42). This applies model-profile shaping (gpt-6:
  `max_tokens` -> `max_completion_tokens`, temperature removed, `service_tier` from
  `MENHIR_OPENAI_SERVICE_TIER`) and 429 backoff.
- The request is `{"model": m, "messages": build_messages(...), "max_tokens": 3000,
  "response_format": {"type": "json_object"}}`, with no temperature. After shaping for
  gpt-6-luna it must equal the P0 body (run_replay.py:77-79: messages,
  `max_completion_tokens: 3000`, json_object). A unit test pins this.
- It does NOT go through `MenhirOpenAIGenericClient.generate_response`. That method appends
  `get_extraction_language_instruction(group_id)` to the system message
  (graphiti_llm_adapter.py:514), which would be a prompt change.
- The whole call is wrapped in `asyncio.wait_for(..., timeout_s)`, a bound separate from the flex
  900 s profile timeout. It runs inside the existing `add_episode` bound
  (`graphiti_add_episode_timeout_seconds`, 300 s), so 60 s leaves the extraction most of its
  budget.
  Superseded by f04185b (section 18): the call is `acreate_chat_completion` on the unwrapped
  client under `await_with_backoff_aware_timeout`, and a nested helper now forwards its 429
  sleeps to the enclosing `add_episode` deadline.

Mechanical changes allowed under the prompt freeze (anything else means a new dev/held-out round):
- module layout and type hints;
- stripping the ingest role prefix (`user:`) from the turn before `build_messages`, because P0
  turns had no prefix;
- `speech_date` rendered from `episode.valid_at`, exactly as P0 rendered it (`YYYY-MM-DD (Weekday)`).

A test asserts `build_messages` output equals a golden JSON produced from reich_lib a7797891 for
fixed inputs.

## 6. Per-episode data flow (flag on)

Inputs:
- `receipt.episode_text`,
- `episode.valid_at`, the speech time; its date is taken in the datetime's own tz, as stored,
- final `edges` (`EntityEdge`, with `.fact`, `.valid_at`, `.invalid_at`, `.reference_time`).

1. **Gate** (no LLM call unless all of these hold):
   - `edges` is non-empty;
   - `_episode_role(receipt.episode_text) == "user"` (graphiti_extraction_policy.py:449). P0
     validated user turns only. Assistant and unprefixed notes are deferred;
   - `episode.valid_at` is set;
   - `detect(turn)` is non-empty;
   - `len(edges) <= 40` and `len(turn) <= 4000` chars. These guard against document-sized
     episodes; P0 turns had about 3-4 facts.

   A skipped episode records `AnchoredTimeReport(status="skipped", reason=...)`.
2. **Call**: `facts = [e.fact for e in edges]` in edge order. Then
   `resolver.resolve(turn, speech_date, facts)`. On a cache hit (section 9), no call is made.
3. **Parse**: `parse_items(content)` keeps items whose `i` is an int in range. Unknown or missing
   `i` means no item, which means keep Graphiti's value. `missing_events` is counted only.
4. **Guard**: `clause_guard(turn, facts, items)`. It can only drop a time; it records
   `guard="clause"`.
5. **Compute**: `compute(speech_date, items, i)` for every i gives `(start, end)`, or None, or an
   open interval.
6. **Overlay**, the only mutation. For edge i, set `valid_at = midpoint(window)` at 00:00 UTC
   only if all of these hold:
   - basis is `explicit_date`, `speech_relative` or `event_anchored`;
   - the window is closed (both bounds set);
   - `kind != "plan"`;
   - Graphiti's `valid_at` is None or lies outside the window;
   - Graphiti's `invalid_at` is None or later than the new `valid_at`.

   Otherwise the edge keeps Graphiti's value. Never touch `invalid_at`, `expired_at`,
   `attributes`, `fact` or `episodes`.
7. **Record** an `AnchoredTimeResult` for every fact (written or not) on
   `receipt.anchored_time.results`, keyed by edge uuid. Log one INFO line per episode:
   - episode key, prompt version, model,
   - facts, overridden, kept-inside-window, plan-not-written, guard drops,
   - latency, cost if reported.

Written in P1: `EntityEdge.valid_at` on overridden edges only. Graphiti persists it through its
normal save (edges.py:360-385).
Deferred: everything else in the contract (section 7), the plan window, open intervals,
`missing_events`.

Why midpoint and why these rules (offline rescoring of the cached P0 outputs, guard on, frozen
prompt; dated facts in window / false dates on no-date facts):

| set (run) | baseline | resolver window (P0 metric) | write window start | write midpoint, keep Graphiti if inside, skip plans (P1) |
|---|---|---|---|---|
| held-out 1 (fix) | 18/32, 0/73 | 31/32, 0/73 | 29/32, 0/73 | 26/32, 0/73 |
| held-out 2 (frozen) | 12/23, 0/76 | 22/23, 4/76 | 19/23, 4/76 | 19/23, 2/76 |
| held-out 2 (frozen_b) | 12/23, 0/76 | 21/23, 2/76 | 19/23, 2/76 | 19/23, 0/76 |
| held-out 3 (frozen) | 18/28, 0/93 | 26/28, 0/93 | 24/28, 0/93 | 24/28, 0/93 |

- Writing the window start, as the design doc says (line 62), loses 2-4 dated facts per set
  against the midpoint, because tolerance windows (`approx`) start a week or a month early.
  P2 stores the full window, so `valid_at` semantics can be revisited there.
- Keeping Graphiti's value when it is already inside the window changes no score and leaves
  11-19 edges per set untouched (less churn).
- Skipping plans costs 2-5 dated facts per set. It removes the only stable false dates (G15.1/2,
  plan time spread onto attribute facts, recurring in all P0 runs), and it matches the design's
  intent that a plan's time is `planned_time`, not `valid_at` (design doc lines 21, 51). The plan
  window is kept in the contract for P2.
- Caveat: the skip-plans rule was chosen after seeing held-out 2. It must be confirmed on fresh
  data (section 12, step L3).

## 7. Data contract for P2 (in memory only in P1)

`AnchoredTimeResult` (frozen dataclass), one per fact:
- `edge_uuid`, `fact_index`;
- `expression: str | None`, which maps to P2 `time_expression`;
- `basis: str`, one of `explicit_date|speech_relative|event_anchored|vague|none`, which maps to
  `time_basis`. These are the prototype's values; the design doc's line 46 enum differs (section 13);
- `kind: str`, one of `point_event|state|plan`, which maps to `event_kind`;
- `granularity: str | None`, derived from `offset.unit`, `calendar.unit` or the explicit date
  format (day/week/month/quarter/season/year), which maps to `time_granularity`;
- `window_start`, `window_end` (`date | None`), which map to `time_window_start/end` (open
  intervals allowed);
- `anchor_ref: str | None`: the anchor edge uuid when `anchor.fact` is in range, else
  `anchor.event` text. Maps to `time_anchor_ref`;
- `anchor_offset: dict | None`: the raw `offset` or `calendar`. Maps to `time_anchor_offset`;
- `planned_window: tuple | None`: the window when `kind == "plan"`. Maps to `planned_time`;
- `guard: str | None`;
- `written: bool`, `graphiti_valid_at` (the pre-overlay value, for audit), `prompt_version`,
  `model`.

P1 must not put any of this in `EntityEdge.attributes`. `save()` flattens attributes into Neo4j
properties (edges.py:360-385), so that would persist it.

## 8. Failure handling (never blocks ingest)

Every failure means: no overlay for that episode, Graphiti's values stand, ingest continues.
`AnchoredTimeReport.status` records which case it was:

| Case | Behavior |
|---|---|
| `asyncio.TimeoutError` (own bound) | `status="timeout"`, WARNING log |
| provider/network error, 429 after backoff | `status="error"`, WARNING with exception class only (no payload) |
| `LlmUsageControlSignal` (per-job budget refusal) | `status="budget"`, swallowed. A refusal means the job is over budget; the next Graphiti call enforces it. Swallowing keeps the invariant that flag-on never fails an episode by itself. |
| empty choices / empty content | `status="empty"` |
| JSON or shape parse failure | `status="parse_error"` |
| exception in guard/compute/overlay (bug) | `status="internal_error"`, logged with traceback; the edges are left as they were before the overlay. The overlay computes all new values first and assigns only at the end. |
| `asyncio.CancelledError` | re-raised (ingest timeout/shutdown semantics unchanged) |

The resolver call is counted by the per-job LLM budget (default 100,
settings_model.py:171): +1 per gated episode.

## 9. Idempotency on retry and replay

- An `add_episode` retry (timeout or failure) begins a fresh receipt
  (enrichment_steps.py:1606, 1641, 1657), so the hook runs again.
  - A process-local LRU cache (256 entries) avoids a second paid call and a different answer.
  - Key: `sha256(PROMPT_VERSION | model | speech_date | turn | facts)`.
  - Only successful parses are cached.
- If the episode's edges differ on retry (extraction is an LLM call too), the key differs and the
  resolver re-runs. That is correct: the facts changed.
- The overlay is a pure function of (edges, items, speech_date). The same resolver output always
  gives the same writes (unit test).
- Re-ingest of an already-ingested turn: P1 only changes the in-memory extracted edges. Whether
  an extracted edge merges into an existing edge is Graphiti's dedupe decision, unchanged.
  Existing edges are never rewritten by P1.
- A restart with a different flag value affects only episodes processed after the restart. There
  is no persisted marker in P1. P2 adds `time_basis`, which makes resolved edges identifiable.

## 10. When Graphiti and the resolver disagree

| Graphiti `valid_at` | Resolver | P1 result |
|---|---|---|
| any | basis `none`/`vague`, no item, guard-dropped, or open window | keep Graphiti (incl. null, so the per-edge fallback still runs as today) |
| None | closed window, non-plan | write midpoint |
| inside the window | closed window, non-plan | keep Graphiti (consistent, possibly more precise) |
| outside the window | closed window, non-plan | write midpoint; log `graphiti_valid_at` in the result (the P0 baseline missed the gold window on 14/32, 11/23 and 10/28 dated facts, null or outside; the speech-date default comes from extract_edges.py:287) |
| any | `kind == "plan"` | keep Graphiti; plan window to contract only |
| `invalid_at` set and <= the new `valid_at` | closed window | keep Graphiti (no inverted intervals) |

Downstream effect to measure, not change: an earlier `valid_at` on a new edge changes which edge
wins in `resolve_edge_contradictions` (edge_operations.py:563-571) and can make the new edge
expire against a later candidate (826-839). This is the intended time-correct behavior, but it is
a behavior change. P1's integration test covers both branches with a fake LLM. P3 owns the
expiry semantics.

Known gap (deferred, fact-text precedence): when the fact text carries a date and the turn
carries a different one, the prompt rule says the fact text wins, and the model sometimes does
not follow it (held-out 3 K19.0). P1 does not add a code rule for it.

## 11. Tests (new files only, offline, no network)

Fixtures in `tests/fixtures/anchored_time/`, copied byte-for-byte from the prototype:
- `dev_set.json`, `dev_set2.json`, `dev_set3.json`;
- `heldout{,2,3}_turns.json`, `heldout{,2,3}_gold.json`;
- frozen-prompt checkpoints: `ckpt_dev_fix2.jsonl`, `ckpt_dev2_it2.jsonl`, `ckpt_dev3_it1.jsonl`,
  `ckpt_heldout_fix.jsonl`, `ckpt_heldout2_frozen.jsonl`, `ckpt_heldout2_frozen_b.jsonl`,
  `ckpt_heldout3_frozen.jsonl`;
- `prototype_expected.json`: per-fact `compute` windows and guard drops, generated once by
  running the prototype on these checkpoints at port time;
- `prompt_golden.json`;
- a `README` naming the source shas.

Before copying, confirm which `dev_fix*` checkpoint was produced under a7797891 (the pt token
counts differ between runs).

| File | Covers |
|---|---|
| `tests/test_anchored_time_calendar.py` | ported calendar: `_calendar` last/this/next/past/upcoming x day/week/weekend/weekday/month/quarter/year/season with count; `_explicit` YYYY-MM-DD, YYYY-MM, YYYY, `--MM`, `---DD`, any-dash MM-DD, plan direction; `compute` offsets with/without approx, event_anchored recursion, depth > 3 -> None, `calendar.later`, open intervals; month-end and leap-day edges |
| `tests/test_anchored_time_detect_parse.py` | `detect` per cue class plus non-firing controls (causal "since"); `parse_output` code fences / bad JSON; `parse_items` bad `i`; `build_messages` equals `prompt_golden.json`; shaped request for gpt-6-luna equals the P0 body |
| `tests/test_anchored_time_guard.py` | every assert from `check_guard.py` (dev3 F1, F2, F6, F8, F7, F5, F3; inline: fact text keeps time, forward elaboration, anchor inside expression, "Dr." abbreviation, relative-clause antecedent) |
| `tests/test_anchored_time_overlay.py` | section 6/10 rules as a table: each row of the disagreement table, plan skip, invalid_at inversion, midpoint, untouched fields, determinism |
| `tests/test_anchored_time_hook.py` | integration with fake LLM, reusing the pattern of tests/test_graphiti_combined_extraction_patch.py:195-250: fake Graphiti clients for extraction plus a fake resolver client returning canned JSON; asserts edges written, receipt report, every failure row of section 8 (timeout, raise, budget signal, empty, bad JSON), gate skips (assistant turn, no cue, too many edges), cache hit on second run, and that `CancelledError` propagates |
| `tests/test_anchored_time_flag_off.py` | flag-off byte-identity: (a) `MemorySettings()` default is False and `GraphitiClient.from_settings` builds `MenhirExtractionHook` with `_anchored_time is None`; (b) the hook run with `anchored_time=None` on canned extraction output returns edges whose `model_dump()` (and save payload) equals the run on unmodified code, and the resolver module is never called (spy); (c) receipt `anchored_time` stays None |
| `tests/test_anchored_time_replay.py` | offline replay: fixtures through the PORTED Menhir functions; asserts per-fact equality with `prototype_expected.json` (port parity) and the pinned scores in section 12 O2 |

Existing tests that must still pass, unmodified:
- `tests/test_feature_flag_registry.py`,
- `tests/test_graphiti_combined_extraction_patch.py`,
- `tests/test_graphiti_combined_extraction_closure.py`,
- `tests/infrastructure/test_edge_containment.py`,
- `tests/config/test_settings_minimums.py`.

## 12. P1 acceptance

Offline (CI, free):
- O1 Port parity. For every fixture fact, the ported `compute` window and guard drops equal
  `prototype_expected.json`.
- O2 Pinned scores. These are the rescoring numbers from section 6; a change means a behavior
  change.
  - P0-parity resolver metric, guard on: held-out 1 (fix) 31/32 and 0/73; held-out 2 (frozen)
    22/23 and 4/76; held-out 3 26/28 and 0/93. These match design doc lines 212-214.
  - P1 written-value metric: 26/32 0/73, 19/23 2/76, 19/23 0/76 (frozen_b), 24/28 0/93.
  - Current pins (ad693ce, `test_anchored_time_replay.py`): resolver held-out 1 30/32 (H19.6,
    week-end rule), held-out 3 26/28; written held-out 3 25/28 (keep-Graphiti's-dates rule).
    The rest are unchanged.
- O3 All section 11 tests pass. The flag-off identity test passes.
- O4 Scoped ruff and the registry tests pass.

Live (operator-run, paid, never in CI). Run with `scripts/eval_anchored_time.py --live`, through
`AnchoredTimeResolver`, so through Menhir's request path and not the bench `run_flex`. Use
gpt-6-luna, the only model the prompt was evaluated on. Budget cap: $0.10.
- L1 Request-path parity: one run of held-out 1, 2 and 3 through Menhir's path (about $0.021).
- L2 Recurrence: a second run of held-out 1 and held-out 3 (each has one frozen run today), and a
  third of held-out 2 (about $0.021). That gives 2-3 runs per set.
- L3 Fresh check of the plan rule: one fresh held-out set of at least 25 cue turns, gold labeled
  blind before the run. Two runs. Use a non-LME corpus if one is ready, else the remaining b10
  pool (about 47 unused cue turns, design doc line 254).

Go/no-go criteria. They replace "false dates <= baseline", which is unmeasurable here because the
baseline's speech-date default is never counted as false (design doc lines 131-133).
- C1 Dates. In every run, the written-value dated-in-window count is above the baseline count for
  every set. The mean over runs is at least baseline + 5 facts per set. Today the margins are
  +8, +7 and +6.
- C2 False dates by recurrence. A no-date gold fact counts as false only if it is false in at
  least 2 runs of the same prompt. Allowed: at most 1 recurring false date per set, and at most 2
  across all sets. Non-recurring false dates are reported, not gated. Today it is 0 on every set
  under the P1 overlay.
- C3 Robustness: 0 ingest-blocking failures. Parse plus call failures at most 1 per 30 turns per
  run.
- C4 Reported, not gated: basis accuracy, dev-vs-held-out gap, latency p50/p95, cost per gated
  turn, override rate, kept-inside-window rate, detector fire rate.
- These criteria judged L1-L3. Section 19 replaces C1 for the next fresh set.

Verification commands (in the Menhir checkout; use the project venv):

```bash
python -m pytest tests/test_anchored_time_calendar.py tests/test_anchored_time_detect_parse.py tests/test_anchored_time_guard.py tests/test_anchored_time_overlay.py tests/test_anchored_time_hook.py tests/test_anchored_time_flag_off.py tests/test_anchored_time_replay.py -q
python -m pytest tests/test_feature_flag_registry.py tests/config/test_settings_minimums.py tests/test_graphiti_combined_extraction_patch.py tests/test_graphiti_combined_extraction_closure.py tests/infrastructure/test_edge_containment.py -q
ruff check --select F811,F821,ASYNC src/menhir/infrastructure/anchored_time.py src/menhir/infrastructure/anchored_time_resolver.py src/menhir/infrastructure/graphiti_extraction_policy.py src/menhir/infrastructure/graphiti_client.py src/menhir/config/settings_model.py src/menhir/config/feature_flags.py scripts/eval_anchored_time.py tests/test_anchored_time_*.py
python scripts/eval_anchored_time.py --offline          # prints the O2 table from fixtures
# operator only, paid, explicit:
python scripts/eval_anchored_time.py --live --sets heldout,heldout2,heldout3 --runs 1 --tag p1path --max-usd 0.05
```

The full suite runs once, in CI, on the final SHA (run_and_test.md policy).

## 13. Cost and latency

- Measured on gpt-6-luna flex, frozen prompt: $0.00018-0.00025 per call, from the cached P0
  checkpoints. Prompt is about 1.8-1.9k tokens, completion about 340-600. Use $0.00025 per gated
  turn: about $0.25 per 1,000 gated user turns on flex.
- Gate rate: the detector fired on about 60% (24/40) and about 30% (12/40) of the two 40-turn
  samples (design doc lines 122-123, 167). Budget for up to 60% of user turns.
- Standard (non-flex) tier pricing was not measured. Assume about 2x, unverified.
- Latency was not measured in P0 (run_flex checkpoints carry no timings). P1 logs it per call.
  On flex the call can queue, so the 60 s own timeout may convert slow calls into "timeout" (no
  overlay). L1 reports p50/p95 and the timeout rate. If the timeout rate is above 5%, revisit the
  default or the tier before enabling. Measured in L1 (section 18): 5.6% timeouts at 60 s,
  1.1% at 120 s, so the default is now 120 s.
- The extra call is sequential inside `add_episode`, so it adds directly to per-episode
  wall-clock.

## 14. Risks

- Prompt validated on one model (gpt-6-luna). With `MENHIR_ANCHORED_TIME_MODEL` unset, the
  resolver uses the Graphiti chat model. On any other model, results are unvalidated. Log the
  model in the INFO line; the ledger row says "gpt-6-luna only".
- Small n (23-32 dated facts per set). C1/C2 use recurrence and margins for that reason.
  Single-run differences of about 4 facts are noise (design doc lines 249-254).
- Behavior change in contradiction and expiry outcomes (section 10), measurable only on a real
  ingest. That is deferred with the LME re-ingest until P1-P3. P1 flags it in the ledger.
- The skip-plans rule and the midpoint choice were chosen on non-blind data. L3 confirms them.
- Per-job LLM budget: +1 call can push an episode near the limit over it one call earlier
  (section 8).
- Timezone: the speech date is the stored `valid_at` date in its own tz. For non-UTC users near
  midnight, "yesterday" can shift by a day. Unmeasured; LME dates are date-only.
- Episode text may carry appended diffs (enrichment_steps.py:164-176). The role gate excludes
  most such episodes, and the size gate covers the rest.
- Same-turn anchors only. Cross-turn anchors stay unresolved (P4), by design.

## 15. Open questions and deferred items

Open questions (each has a default, so none blocks P1):
1. Integration A-hook vs A-proxy vs B. **Decided 2026-10-07: A-hook** (user).
2. `valid_at` = window midpoint (P1 default, evidence in section 6) vs window start (design doc
   line 62). Revisit in P2 when the window is stored.
3. Gate on user turns only (default) vs also assistant turns. Widening needs its own dev/held-out
   round.
4. Committing LME-derived held-out turns and gold as test fixtures. Default: commit; check the
   corpus licence first. If not allowed, the replay test reads them from an env path and skips
   when the path is absent. CI coverage then falls back to dev sets only.
5. Resolver timeout default (60 s) and tier (flex vs default), after L1 latency. **Decided
   2026-10-07:** keep flex, raise the default to 120 s (6ceae9b); section 18.
6. Whether to later suppress Graphiti's batch timestamp call on resolved episodes (saves a call;
   that needs A-proxy).

Deferred from P0, not in P1:
- model-year guard ("owns a 2018 <car>" read as the fact's time);
- attribute-facts rule (attribute facts of a planned/dated thing inherit no time). The P1 plan
  skip covers the observed plan case only;
- detector forms: early/mid/late month ("back in mid-February") and age ("around 5 years old");
- fact-text precedence over a turn date, as a code rule (held-out 3 K19.0);
- a second, non-LME eval corpus (b10's unused cue pool is nearly exhausted).

## 16. Phase checklist

- [x] User decision on the integration point recorded in this file (A-hook, 2026-10-07).
- [x] Branch from a commit containing 29bcbd9's extraction policy. Run blast_radius on
      `graphiti_extraction_policy.py` and `graphiti_client.py`.
- [x] Copy fixtures and generate `prototype_expected.json` / `prompt_golden.json` from the
      prototype (record the shas).
- [x] `anchored_time.py` (pure port and overlay) plus calendar/detect/guard/overlay tests.
- [x] `anchored_time_resolver.py` plus cache, and the shaped-request test.
- [x] Settings fields, feature_flags row, `.env.example` lines.
- [x] Hook constructor and policy insertion before 1573; receipt field.
- [x] graphiti_client construction behind the flag.
- [x] Hook integration test, flag-off identity test, replay test.
- [x] `scripts/eval_anchored_time.py` (offline and live).
- [x] Focused tests and scoped ruff (section 12). fa64e1d: 278 anchored-time tests pass, scoped
      ruff clean. The registry test fails on 4 env reads from the gpt-6/Flex base commits
      (MENHIR_OPENAI_FLEX_TIMEOUT_S, MENHIR_OPENAI_SERVICE_TIER, openai_rate_limit `_env_*`),
      not from anchored-time; it must be fixed before CI can be green.
- [x] Ledger row in Menhir `.agent/default-off-features.md`.
- [x] Operator: L1, L2 live runs; record results here (section 18, 2026-10-07).
- [x] Operator: L3 fresh held-out (blind gold first); go/no-go against C1-C3 (section 18:
      C1 not met, C2/C3 met).
- [x] CI green on the final SHA: 1d81bc6, all 4 checks pass (2026-10-07). Draft PR
      Archolith/menhir#244 (stacked on the gpt-6/Flex base; a200e7a registers the 4 base env reads;
      1d81bc6 renames the structural predicate's comprehension variables, which shadowed the
      subject timeline's `s` once both branches combined). The flag stays OFF; no LME re-ingest until P1-P3.

## 17. Orchestrator review (2026-10-06)

Re-checked at 29bcbd9:
- The insertion point at 1573 is the last statement of `_run_graphiti_combined_extraction`.
  The hook wraps its result in `SingleEpisodeExtractionResult` (1607-1611).
- The fork returns that result unchanged (graphiti.py:728-729). It is used as
  `precomputed_edges` (835-836) and goes into `resolve_extracted_edges` (851).
- The per-edge fallback skips edges whose times are already set (edge_operations.py:587-588).
- `_episode_role` is at 449.
- No anchored-time code exists yet.

Notes for the implementer:
1. The case for the midpoint is partly the P0 metric itself, which scores the midpoint against
   the gold window. The reason that does not depend on the metric is the tolerance: an `approx`
   window starts early. L3 must also report how often the window start falls inside the gold
   window, so the choice is not fitted to the scorer.
2. 29bcbd9 currently exists only in a local clone. Before P1 starts, confirm the commit is pushed
   to `smoke/gpt6-flex` on the remote. Otherwise the branch base can be lost.
3. Open question 4 (LME-derived fixtures) must be settled before fixtures are committed. Until
   then, the replay test uses the env-path-and-skip fallback.

## 18. Live gate results (2026-10-07)

Branch `smoke/gpt6-flex`, pushed to origin at f04185b. gpt-6-luna on flex,
one run per row. Cells are written dated-in-window / total, with the margin over the baseline
(heldout 18, heldout2 12, heldout3 18).

| Run (checkpoint tag) | Timeout | heldout | heldout2 | heldout3 | Fail (per 30 turns) | p50 / p95 | Cost |
|---|---|---|---|---|---|---|---|
| L1 `p1path` | 60 s | 23/32 (+5) | 19/23 (+7) | 24/28 (+6) | 2, 2, 1 (all timeouts) | - / 60 s | $0.0196 |
| L1 `p1path120` | 120 s | 26/32 (+8) | 19/23 (+7) | 23/28 (+5) | 1, 0, 0 (1 timeout) | 15-24 s / 89 s | $0.0203 |
| L2 `p1l2` | 120 s (default) | 26/32 (+8) | 19/23 (+7) | 24/28 (+6) | 0, 0, 0 | 6-7 s / 19 s | $0.0209 |

- L1 at 60 s failed C3 (2/30 on heldout and heldout2; 5/89 = 5.6% timeouts). The 60 s bound
  also counted 429 backoff sleeps. Rerun at 120 s passed C1-C3 (1/89 = 1.1% timeouts).
- L2 at the committed default passed C1-C3. Overridden 10/11/9, kept 19/12/16.
- C2 recurrence (frozen runs plus the three live tags): heldout2 G28.0 is false in the frozen
  run and in `p1l2`, so heldout2 has 1 recurring false date. That is at the per-set limit (1)
  and within the total (2). Non-recurring: G17.1 (frozen), K04.3 (`p1path`). heldout and
  heldout3 have none.
- Checkpoints: `ckpt_heldout{,2,3}_{p1path,p1path120,p1l2}_r1.jsonl` in the local eval dir
  (git-ignored, LME-derived).
- Commits: 6ceae9b raises the default to 120 s. f04185b routes the call through
  `acreate_chat_completion` under `await_with_backoff_aware_timeout` on the unwrapped client,
  and makes nested backoff clocks forward their sleeps to the enclosing `add_episode`
  deadline. That fixes the two call-discipline test failures. Full suite after f04185b: 10672
  passed; 1 failure (feature-flag registry) and 14 beacon errors, both pre-existing.
- Trade-off: in a 429 storm the resolver can now wait up to 480 s (4 x 120 s), with the
  `add_episode` deadline paused rather than expiring.
- Live check of the f04185b helper call path (tag `p1helper`, 120 s default): written 26/32
  (+8), 19/23 (+7), 24/28 (+6); 0 false dates written; failures 0, 0, 0; p50 13-15 s, p95
  30-40 s; overridden 10/9/9, kept 19/13/16; $0.0212. Passes C1-C3. C2 recurrence unchanged
  (heldout2 G28.0 only).

L3 (2026-10-07): held-out 4 = 30 unused b10 cue turns (101 facts, 27 gold-dated), read-only
query, gold labeled blind at f04185b before any run (`build_heldout4.py`, `heldout4_gold.json`).
Cue mix: no durations and only 5 speech-relative turns left in the pool. Several turns restate
events already in held-outs 1-3 in other words (Coastal Cleanup, Dance for a Cause, mid-February
bike repair), so the set is less fresh than its text-level dedup suggests.

| Run (tag) | baseline | written | false (written) | Fail | p50 / p95 | Cost |
|---|---|---|---|---|---|---|
| `l3` r1 | 21/27 | 23/27 (+2) | 0/70 | 0/30 | 17.7 s / 58.8 s | $0.0066 |
| `l3` r2 | 21/27 | 22/27 (+1) | 0/70 | 0/30 | 10.7 s / 73.4 s | $0.0067 |

- **C1 fails as written:** above baseline in both runs, but the mean is +1.5, not +5. The
  baseline already dates 21/27 here (mostly explicit dates), so +5 needs 26/27.
- C2 passes (no false dates). C3 passes (0 failures).
- Misses: the plan skip costs 2 (L09.1, L21.0; no plan-spread false dates appeared). Two are
  unplaceable (L04.3, L20.2). r2 overwrote a correct baseline on L18.0: the model returned
  `"date": "---10"` for "on February 10th", and the overlay accepted the malformed date
  (r1 returned `--02-10`). That is a parser robustness gap.
- Section 17 note 1: window start inside the gold window in 20/22 and 20/23 resolver windows,
  against 22/22 and 22/23 for the midpoint.
- Go/no-go: **no-go on C1 as written**. Whether C1's fixed +5 margin should scale with
  baseline headroom is the operator's call.

## 19. Overlay fixes and revised gate (2026-10-07)

Overlay fixes (6e8e44e, pushed to `smoke/gpt6-flex`):
- Keep Graphiti's own dates: `valid_at` is overridden only when Graphiti left it missing or at
  its speech-date default (date in UTC or in the speech tz). New reason `graphiti_resolved`.
- Reject an explicit date that drops or changes the one month its expression names
  (`"on February 10th"` -> `---10`). The fact becomes unplaceable and keeps Graphiti's value.
  `---DD` stays valid when no month is named ("on the 20th").
- Offline over all 18 frozen-prompt runs (4 frozen, 12 live on held-outs 1-3, 2 L3), 492 dated
  facts: written 405 -> 412, harmful overrides 11 -> 4, false dates 4 -> 4. No run got worse.
  The rules were chosen after seeing these runs, so this is not a blind result; the next fresh
  set confirms it. The pinned O2 row for held-out 3 (frozen) is now 25/28, 8 overridden.
- Tests: anchored-time, nested-backoff, call-discipline and all extraction-policy tests with
  the eval dir set: 735 passed, 2 skipped.

Revised gate. Fixed now, before any further data is seen; amended the same day with the Codex
review (harm over all facts, fixed sampling). It applies to the next fresh sets only; L3 stays
a no-go under the section 12 gate. C2 and C3 are unchanged.
- Sets. Two, both sampled and gold-labeled blind before any run. Sizes, pool and seeds are
  written into this plan before sampling.
  - R (representative): at least 30 cue turns drawn uniformly at random from the pool.
  - X (challenge): at least 30 cue turns, at least half speech-relative, event-anchored or
    duration.
  - At most one turn per conversation in a set, and no turn in both sets.
  - No set is replaced, resampled or relabeled after a run, whatever the result.
- Runs: exactly 2 independent runs per set, each in a fresh process (the resolver's result
  cache is in-process), no reused outputs.
- Gold marks plan facts (`"plan": true`) at labeling time, so "plan" is not defined by model
  output.
- C1' Dates, non-plan dated facts. Recovery = (written hits - baseline hits) / (dated facts -
  baseline hits), mean over the 2 runs.
  - Every run, both sets: written hits >= baseline hits.
  - X: recovery at least 50%. R: recovery reported, no minimum beyond the line above.
  - No set is excluded for low headroom. If X has fewer than 8 baseline misses, the gate is
    inconclusive, not passed.
- C5 Harm, over all gold facts (plan, non-plan and no-date), per run and set.
  - Harmful override: the baseline value was right (inside the gold window, or not a false
    date on a no-date fact) and the written value is wrong.
  - Wrong write: any written value outside the gold window or false, whatever the baseline had.
  - Caps: at most 1 harmful override per run; 0 harmful overrides on the same fact in both runs;
    wrong writes at most max(1, 10% of writes) per run.
- C6 Plans, reported only (P2 work): planned-window accuracy on gold plan facts.
- Reported, not gated: results per cue category (explicit, speech-relative, event-anchored,
  duration) and a 90% bootstrap interval over turns for recovery.
- Data: the b10 pool cannot supply R and X; it needs a new extraction pass or a non-LME corpus.

Gate run 1 setup (fixed 2026-10-07, before the pool is built or seen):
- Code: 02b5200 (count and speech-tz fixes, pushed). Resolver prompt unchanged.
- Pool: bench arm b11, longmemeval_oracle items at file positions
  25-64 (40 items, none in b10, which holds positions 0-24). Fixture sha256 `9fa654b4...c948`.
  Extraction gpt-6-luna on Flex, anchored-time resolver off, so `valid_at` is Graphiti's own.
- Eligible turns: user turns with at least one edge on which the cue detector (`reich_lib.detect`)
  fires; duplicate turn text within a conversation counted once.
- R: seed 20261011. Shuffle all eligible turns; walk them and take a turn when its conversation
  has none in R yet; stop at 30.
- X: seed 20261012. Shuffle the eligible turns not in R; first take speech-relative,
  event-anchored or duration turns (one per conversation) up to 30, then top up with other cue
  turns (one per conversation) to 30. If X gets fewer than 15 such turns or fewer than 30 turns,
  stop and report; the pool is not grown or resampled for this run.
- Gold: labeled blind from a dump with the turn and its facts only (no baseline, no model
  output), plan facts marked `"plan": true`, written before any resolver call.
- Runs: R and X twice each, every run its own process, gpt-6-luna Flex, $0.05 cap per run.

Amendment (2026-10-07, during the build, before sampling; user-approved):
- Flex capacity 429s exhausted all 6 backoff attempts on some Graphiti timestamp-batch calls
  ("Failed to extract batch timestamps for N edges"); those edges keep no `valid_at`, which
  would inflate recovery. A user turn whose episode had such a failure is ineligible.
- Failed episodes are found from the build log: each failure's Graphiti request begin line
  (same `estimated_tokens`, latest before the failure) carries `correlation_id` = episode uuid.
  The list is fixed after the build and before sampling; seeds and sizes are unchanged.
- Other exhausted calls (e.g. one `identity_judgment` dedup call) are reported, not excluded.
- While diagnosing, 4 b11 fact snippets (2 conversations) were seen in a timestamp query; no
  turn text, baseline dates or set membership.
- The first build attempt hit the bench's 7200 s window timeout under Flex throttling (no item
  manifested). A resume was refused: 2 episodes stayed ENRICHING with no worker (benchmark mode
  disables orphan recovery). The unmanifested container and volume were deleted and the pool
  rebuilt fresh on the same fixture and code, Flex only (arm b11f: LME_ROLLING_WINDOW=1,
  LME_DRAIN_TIMEOUT=86400, 12 conversations at a time, results dir oracle-b11f). The exclusion
  list is built from the b11f log only.

Amendment 2 (2026-10-07, during the build, before sampling; user-approved): the gate scores the
review fix d16485d (branch fix/anchored-time-review on 02b5200), not 02b5200. Resolver runs use
d16485d; the b11 pool stays the resolver-off build of 02b5200 (the fix touches only the resolver
overlay, which is off in the build). d16485d writes no date from ambiguous or invalid resolver
input: dependents of a two-candidate anchor ("ambiguous_anchor"), indexes or anchors outside the
fact list, duplicated indexes, units other than day/week/month/year, unknown directions, bad
amounts or counts, and "later that week/month" with no days left. Offline on the four frozen
runs, P1 written is unchanged (26/32, 19/23, 19/23, 25/28); P0 resolver windows lose one heldout
hit (31 -> 30: H19 "later that week" after a week-end anchor, which the old next-week fallback
happened to cover).

Build and sampling (2026-10-07, before any resolver call):
- b11f stopped at 13/40 manifested: an OpenAI 500 in the Phase 3 event lane of
  lme-gpt4_d9af6064 aborted the whole build. Bench fix 226224c: Phase 3 retries 429/5xx and
  transport errors (3 attempts; config errors and 4xx are not retried), and a failed item is left
  unmanifested instead of stopping the build (up to 3). Resume arm b11fr kept the 13 items (same
  container, fixture and server code 02b5200, resolver off; noncanonical because the bench commit
  changed) and built 27 more: 40/40 manifested, 0 failed episodes, 0 retries needed, 0 timestamp
  batch failures. One benign embedding timeout warning (source-memory embedding, not edges).
- validate: 4 FAILs, none a data defect. commit_immutability / fresh_clean_provenance /
  no_interrupted_phases come from the two-attempt noncanonical resume (the interrupted phase is
  attempt 1). provenance_chain: lme-dcfa8644 counts were -1 (unreadable at manifest time); a
  read-only recompute gives 12/12 user turns admitted, 0 unprojected.
- Exclusion: 1 failed timestamp batch (b11f, 11:08, episode 0dc6551a, lme-gpt4_9a159967), none
  in b11fr. That uuid is a user memory episode with no RELATES_TO edges, so it never reached the
  pool; its turn ("I'm planning a future trip...") is not in either set (X16 is a different turn
  of that conversation). Caveat: the exclusion keys on the memory episode uuid while the sampler
  keys on turn episodes; it held here only because that turn was not drawn.
- Pool: 238 eligible cue turns in 40 conversations, 161 with a hard cue. R 30 turns / 122 facts,
  X 30 turns (30 hard) / 118 facts, seeds as fixed above.
- Gold (`b11{R,X}_gold.json`, blind from the label dumps, before any run): R 43 dated (31 non-plan,
  14 plan facts, 7 skip), X 33 dated (27 non-plan, 15 plan facts, 5 skip). Past-tense dates that
  fall after the speech date (year ambiguous) are skip.

Gate run 1 result (2026-10-07, d16485d, gpt-6-luna Flex, 4 fresh processes, $0.033 at the
Standard rate the bench reports; Flex bills half). Verdict: FAIL (R fails C5; X is
inconclusive on C1'). Sets and gold are not changed after this.

| set | run | dated non-plan | baseline | written | recovery | writes | wrong | harmful |
|-----|-----|---------------:|---------:|--------:|---------:|-------:|------:|---------|
| R | g1a | 31 | 26 | 27 | 20% | 6 | 2 | R17.1, R29.2 (FAIL) |
| R | g1b | 31 | 26 | 30 | 80% | 8 | 1 | R29.2 |
| X | g1a | 27 | 26 | 26 | 0% | 0 | 0 | - |
| X | g1b | 27 | 26 | 26 | 0% | 0 | 0 | - |

- C1': written >= baseline in every run. R mean recovery 50% (90% bootstrap 0..100%, 5 misses).
  X has 1 baseline miss (< 8), so inconclusive, not passed.
- C5: R g1a has 2 harmful overrides (cap 1) and 2 wrong writes (cap 1); R29.2 is harmful in both
  runs (recurring). X: no writes.
  - R29.2 "The shoe brand has black stilettos..." (no time in the fact): dated to the office
    party "on February 5th" and to the prior year, 2022-02-05; the baseline had the speech date.
  - R17.1 "...watch for his birthday in June" (speech 2022-05-15): written 2021-06-15, a past
    year for an upcoming month.
- C6 (plans): R 8/12, 9/12; X 5/6, 5/6.
- Per category (base -> written, g1a | g1b): R speech-relative 18->19 | 18->22 of 22, explicit
  7->7 of 8, event-anchored 6->6, duration 3->3. X speech-relative 22->22 of 23, event-anchored
  5->5 of 6, explicit 4/4, duration 9/9.
- Headroom: on this luna-extracted pool Graphiti already dates 26/31 (R) and 26/27 (X) non-plan
  dated facts, far above the b10 baseline (section 18), so X cannot reach the 8-miss floor.
- The X g1b run hit Flex 429s (6 backoffs, max attempt 5); no call failed. X g1a had 1/30 turn
  with no resolver output (counts as no write).
- Failure class for the next round (not fixed here): a stated month/day is placed in the wrong
  year (past for an upcoming month, prior year for a date after the speech date), and a date is
  copied onto a co-mentioned fact that does not carry it.

## 20. Pinned implementation after gate run 1 (2026-10-07)

Decision (user, 2026-10-07): pin the current implementation and move on. No further gate run is
planned; a full 40-turn blind set was declined. This is an acceptance on non-blind evidence and
is NOT a gate pass under section 19.

Pinned: branch `fix/anchored-time-review`, d16485d -> efdbfd4 -> ad693ce (local, unpushed).
- efdbfd4 year rule: a yearless explicit date or holiday whose other-year occurrence is nearer
  the speech date and within 31 days is `year_ambiguous` and not written; dependents inherit
  `ambiguous_anchor`. Threshold from data: every wrong or skip cross-year case had the other
  occurrence 1-17 days away, every correct one 150+ days. A plain nearest-occurrence rule was
  rejected (it lost G08/L13 "on May 1st" said Nov 29).
- ad693ce: replay pins refreshed for the approved week-end rule (H19.6, resolver 30/32).
- Tests: 278/278 anchored-time tests pass; 7 new overlay tests fail on d16485d.

Evidence and its limits:

| check | result | blind? |
|-------|--------|--------|
| offline, 18 frozen runs | no change | yes (pre-existing) |
| offline, 22 runs total | ok 532->533, false 6->4, wrong writes 7->4, writes 147->144 | no |
| b11R re-score (g1a, g1b) | PASS: recovery 20%/80%, 0 wrong, R17.1 and R29.2 removed | no (rule chosen after the failures) |
| b11X re-score | INCONCLUSIVE (unchanged, 1 baseline miss) | no |
| live R17+R29 x5 (Flex) | R29.2 dated in 2/5, blocked as year_ambiguous both times | live |
| live R17 x20 (Flex) | "in June" labelled plan 20/20; R17.1 bug never recurred | live |

R17.1 is fixed only on a replay of the real g1a output; it did not recur live in 25 runs.

Open, not fixed:
- Cross-sentence date copy (K04.3, accepted earlier). Two guard variants were tried: one changed
  nothing, the other also dropped the correct R26.0 write. Neither was kept.
- R29.2 also needs the cross-sentence guard to get a date; today it is blocked, not dated.
- A real gate pass needs a new blind set; a reduced size would have to be approved here first.

## 21. State-start guard for the cross-sentence date copy (2026-10-12)

Decision (user, 2026-10-12): anchored time is on for the n=500 build, so the open copy class in
section 20 is fixed first. Menhir branch `fix/anchored-time-cross-sentence` a43c75e on main
db990a1 (local, unpushed, no PR yet).

The class, as found on main over all 22 runs: every remaining wrong write gave a time to a fact it
does not date. K04.3 read a causal "since" ("Since I started working with her on 2/15, I'm
hoping ...") as a start time; G17.1 ("the goats are safe" got the fence repair's "three weeks
ago"), G28.0 x2 ("owns a 2018 Ford Mustang GT" dated 2018) and R29.2 ("the brand has
stilettos" got the party's "February 5th") gave a state the time of a co-mentioned event.

Rule (`state_start_guard`, after `clause_guard`, production and offline eval):
- "since" is temporal with "ever since" or a perfect-aspect main clause; otherwise the item is
  dropped (guard `causal_since`), any kind. A date inside a "since <clause>" with an explicit
  or speech-relative basis dates that clause's own event and is not treated as the operator.
- A state takes a time only when the text marks its start: temporal "since", a duration up to
  now, or start/begin in the expression's clause. Otherwise guard `state_start`: the item keeps
  its basis so facts anchored on it still place (R20.2), and `plan_overlay` does not write it.
  Graphiti's own dates are untouched; rendering shows no window for this outcome.
- Known cost, by design: a state started by a change-of-state verb without start/begin/since
  ("I moved to Lisbon two weeks ago" -> "lives in Lisbon") keeps Graphiti's value. It is a
  missed recovery, never a wrong date; no such write occurs in the 22 runs.

Evidence:

| check | result | blind? |
|-------|--------|--------|
| offline, 22 runs | wrong writes 4 -> 0, harmful 4 -> 0, correct facts 2259 -> 2263, writes 144 -> 140, no correct write lost | no (rule chosen after seeing them) |
| R29.2 | blocked by `state_start` on any speech date, not only the year rule's 31 days | no |
| R17.1 | blocked by `state_start` (g1a) and the year rule | no |
| K04.3 | dropped as `causal_since` | no |
| tests | new test_anchored_time_state_start.py 18/18; 6 mutants all killed; anchored-time, extraction-policy, nested-backoff and call-discipline suites 554 passed with the eval dir | - |

Correction to section 20: R29.2 fact 2 has gold "none", so the right outcome is no date, which
it now gets from the guard; it does not need a date.

Still open:
- `test_heldout_replay_matches_the_prototype[heldout3:frozen]` (K09 window, compute() parity)
  already fails on main db990a1 with the eval dir set; untouched by this change, not fixed here.
- Not a gate pass. A blind check needs a new set; size and cost to be approved here first.

### 21.1 Review round (PR #274, 2026-10-12)

PR #274 = a43c75e + f8bf270. Review: `.agent/reviews/menhir-pr274-anchored-time-reviews-combined.md`
(0 High, 4 Medium, 8 Low). Fixed in f8bf270:
- M1: `plan_overlay` checks `state_start` before `graphiti_inside_window`, so a borrowed window
  is never stored as a window outcome and rendered (G19.2, K08.2; no write change).
- M2: start/begin does not date a state when the fact's words are only in another coordinate
  clause ("I started fixing the fence ... and the goats are safe"), unless the start's subject
  refers back ("it all started", R26.0).
- M3: perfect aspect = have/has/had + participle, read outside a leading since-clause.
- M4: "ever" before an expression that starts at "since".
- L3-L6, L8: every occurrence of a repeated expression is judged; case and curly apostrophes
  fold without shifting positions; "for"/"now" exempt only a span to now.

Evidence: offline 22 runs 2263 ok, 140 writes, 0 wrong, no correct write lost vs a43c75e;
29 tests, 16 mutants all killed; related suites 661 passed (only the heldout3:frozen failure
above); CI green on f8bf270. PINNED_O2 kept-inside: heldout2:frozen 13 -> 12, heldout3:frozen
16 -> 15.

Deferred: L1/L2 (some temporal "since" not recognized; no write, fail safe); L7 (timeline sort
uses the stored window for every outcome, pre-existing on main); the same find/lower pattern
in `clause_guard`.

Merge: CI green and MERGEABLE CLEAN; `gh pr merge` was refused by the session's permission
check, so the merge is left to the user. (Superseded: merged after rounds 2-5, see 21.2.)

### 21.2 Review rounds 2-5 and merge (PR #274, 2026-10-12)

Reviews: rounds 2-5 in `.agent/reviews/menhir-pr274-anchored-time-reviews-combined.md`. Commits 2d5a311
(round 2), c0dbdb0 (round 3), d81901a (round 4), 608c9a6 (round 5).

Lesson: every loosening (prepositional "since", "which started", subject-only "and" split)
reopened a wrong-date path, and each round found a new nearby shape. The cause is design: the
guard infers time-to-fact attachment by parsing English clauses with regexes. From round 4 on,
the rule was "never looser than 2d5a311 (reviewed in round 3)". Round 5 was the last review, by
user decision, and its findings were closed only by cutting writes:
- R5-1: a fronted time takes the next clause only when it opens its comma clause.
- R5-2, RS5-4: `plan_overlay` never writes a state whose window reaches past the speech date.
- RS5-1: the main clause also ends at or/yet/plus/dash; its perfect must follow only the subject.
- RS5-2: a temporal "since" dates only the clause it attaches to (shared word with the fact,
  else `state_start`); a trailing since's clause starts after its last coordinator/subordinator.
- RS5-3: no back-reference start ("it all started" alone).

Evidence at 608c9a6: offline 22 runs 2263 ok, 138 writes, 0 wrong (R26.0's back-reference write
dropped; Graphiti's date already scores it correct). 0 lines looser than 2d5a311 over every
probe and fuzzer from rounds 2-5 (302 + 108,268). 118 tests; 51 mutants, 47 killed (3 redundant
second defenses, 1 speed-only cache). Related suites 794 passed, only the known heldout3:frozen
K09 failure. CI green.

Merged: PR #274 merged at a0a9814 (head 608c9a6, `--match-head-commit`).

Known residuals: RS5-5 (Low, "since" at a line end); round-4 RS-1 (a start word about another
event sharing a generic word with the fact, "Last week, I started PT for my back" on "back is
sore"; 2d5a311 writes it too). Follow-up after the 500 run: move time-to-fact attachment into
the extractor's structured output and keep the deterministic guard as a veto only.
