# Changelog

Older entries: [`CHANGELOG-archive.md`](CHANGELOG-archive.md).

## 2026-11-06 - Research routing and changelog trim

- `workflows/artifact_authoring.md`: added where research results go.
- `README.md`: routes recall questions to the experiment record.
- `maintenance.md`: older changelog entries now move to `CHANGELOG-archive.md`.
- `CHANGELOG.md`, `CHANGELOG-archive.md`: moved 114 older entries to the archive, unchanged.
- `../docs/research/README.md` and the cluster READMEs: added "Start here" routing.

## 2026-10-27 - Bucket 1 default-off features removed; kept on an archive branch

- Removed nine default-off features that the #168 freeze did not ship (`be6e3658`..`155d34be`):
  - the oracle subset;
  - the similarity scale;
  - the fact-edge, content-vector and BM25 recall lanes;
  - scalar collapse;
  - scalar canonical self;
  - the deterministic scalar extractor and router;
  - shadow context composition.
- The code is kept on `archive/bucket1-features-2026-10-27` (base `d1105b25`).
  [`archive/removed-features-2026-10-27.md`](archive/removed-features-2026-10-27.md) maps each commit
  to its files and gives restore steps.
- Their settings are in `RETIRED`. A stale env export is ignored, and the bench launchers refuse it.
- Recall Lab arms a, b and c are gone. A request that sets a removed knob gets a 422.
- Equivalence check: for each unit, an empty env resolves to the same settings, recall tuning, hooks
  and constructor values as the base, with no differences beyond the removed keys.

## 2026-10-27 - #168 freeze: tested behavior becomes the default; 23 flags retired

- **Always on, flags retired:**
  - Warden gate, contradiction interrupt, fact said-time, source memories, source-memory spread,
    count anchor hint and stale-lead demotion (`370fd94e`);
  - edge-invalidation statement order (`8ec03d26`);
  - scalar reconcile attribute, scope and subject, scalar View authority, the View freshness gate
    and sum grounding (`d022a7ed`);
  - counting recall authority-only, counting wording, counting extraction v2 and related-count
    matching (`9ec4d08c`, fix `7b26834f`);
  - assistant turns stored only (`bdad118c`).
- **Defaults on, switches kept** (`cc931186`). These three cost LLM calls and remain the way to turn
  the work off:
  - `MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED` and `MENHIR_PERSONAL_MEMORY_COUNTING_ENABLED`: the
    scheduler runs the scalar and counting lanes.
  - `MENHIR_ANCHORED_TIME_RESOLVER`: now also drives the blind attach check and world-end expiry.
    `MENHIR_ANCHORED_TIME_ATTACH` and `_EXPIRY` are retired.
- `MENHIR_FRONTIER_SOURCE_MEMORY_MAX_CHARS` defaults to 2000.
- **Retired keys** are recorded in `RETIRED` (`config/feature_flags.py`). Menhir ignores a retired env
  var, so a stale export cannot turn a feature off.
- **Component knobs** (`RetrievalTuningConfig`, service, adapter and scheduler fields) default on and
  stay as per-request or test seams. No alternate code path was deleted.
- **Scalar View authority** now follows the scalar lane alone (`config/authority_gates.py`).
  Features that required it now require scalar state.
- **Counting stamp:** writers and the API dirty check stamp `<configured>+x2+m`. Namespaces counted
  without v2 or matching are re-counted once.
- **Equivalence check:** an empty env on the new code resolves to the same settings, recall tuning,
  Graphiti hooks and production-constructor values as the old code (`466a8d39`) with the approved
  values set. The same holds with each switch off.
- Plan: workspace `menhir-freeze-retire-always-on-flags-plan-2026-10-27.md`.

## 2026-10-25 - Record statement-order keeps as edge conflicts (#168 item 9, deferred link)

- Each pair the statement-order veto keeps (NEWER_CANDIDATE and CONTRADICTION) is now written as an
  `EdgeInvalidationConflict` record with `kept_by = 'statement_order'`, the fork reason, and the
  cause's source (stored, or this call's). Authority keeps are written exactly as before; a record
  without `kept_by` is an authority keep. One record per ordered (kept, cause) pair, as before.
- Nothing reads these records yet. Linking them to `list_conflicts`, recall and resolution needs
  edge-level conflict code: Menhir's conflict groups live on `:Entity` nodes, so linking through a
  fact's endpoints would flag the user node on every recall. That link is deferred to post-MVP.
- Written only when `MENHIR_EDGE_INVALIDATION_STATEMENT_ORDER` is on.
- `tests/test_edge_invalidation_statement_order_records.py`, `tests/test_edge_invalidation_statement_order_records_online.py`.

## 2026-10-25 - Statement-order veto on edge invalidation (#168 2a)

- Graphiti orders supersession by event date (`valid_at`), so an older statement whose event dates
  later ("had it for 3 months") ended a newer one ("switched yesterday"). The invalidation hook now
  answers KEEP on NEWER_CANDIDATE and CONTRADICTION when the cause was said before the target, and
  both facts stay live. "Said" is the episode time: this call's episode for its own mention, the
  stored `r.reference_time` (read by uuid and `group_id`, cached per call) for any other edge.
- Veto only: it never ends a fact today's code keeps. The #377 authority check still answers
  first. A missing or unparseable time keeps today's answer; a failed lookup answers KEEP. OWN_END
  is unchanged.
- `MENHIR_EDGE_INVALIDATION_STATEMENT_ORDER`, default on; `false` restores the old behaviour.
  Applies at ingest, so existing graphs change only for facts written after it is on.
- `tests/test_edge_invalidation_statement_order.py`, `tests/test_edge_invalidation_statement_order_online.py`.

## 2026-10-25 - Backfill `--require-complete` build gate

- `scripts/backfill_episode_embeddings.py --require-complete`: after the run, count READY
  episodes that still have no content embedding (the listing's own predicates, missing only) and
  exit 3 if any remain. With `--dry-run` it only checks. For benchmark builds, where the
  scheduler (and so the repair job) is off.

## 2026-10-25 - Store-only ingest (`store_only` on POST /api/memory)

- `store_only: true` stores and embeds the episode for the source-memory lane and never extracts
  it: no Graphiti extraction and no evidence projection for its turn. The turn link
  (`ADMITTED_ON`) is still drawn. For turns with nothing to extract, such as a fact-free question,
  whose extraction collapses to zero edges and fails the episode. Default off; existing callers
  are unchanged.
- The mode is a durable `:Episodic.extraction_mode = 'store_only'` set at creation and returned by
  every claim, so lease recovery, retries and requeues never extract it. The worker embeds, then
  finishes READY (substage `store_only`) through a write fenced on the claim's owner and start
  instant; a lost fence never falls through to extraction.
- `reset_zero_extraction_episodes` exempts store-only rows (it would otherwise loop them).
  `/api/episode-admission` still links a store-only episode but does not mint its projection.
- `store_only` is in the idempotency digest only when set, so existing stored digests still match.

## 2026-10-25 - Source-memory embedding repair job

- The ingest step's episode embed (10 s timeout) logs and swallows failures, so an episode could
  stay READY with no `content_embedding` and never reach the source-memory lane. In the o500
  build, 91 such timeouts left 91 READY episodes unembedded, 34 of them evidence turns.
- New scheduler job `repair_source_embeddings` (`services/source_embedding_repair.py`), on when
  `MENHIR_FRONTIER_SOURCE_MEMORIES` is on (the default) and the embedder is wired: every 300 s it embeds up to
  200 READY episodes missing an embedding (30 s timeout each), skips past failures, and stops a
  run after 5 consecutive failures. It fills missing embeddings only; re-embedding for a model
  change stays with `scripts/backfill_episode_embeddings.py`.
- `set_episode_content_embedding(..., expected_content=)` writes only if the content still matches
  the text that was embedded; `list_episodes_missing_content_embedding(..., ready_only=)` limits
  the listing to READY episodes so the job never races the ingest step.
- Benchmark mode disables the scheduler, so LME builds still need the backfill script after drain.

## 2026-10-25 - Source-memory session spread (default on)

- `MENHIR_FRONTIER_SOURCE_MEMORY_SPREAD` (default on; `=false` opts out): the source-memory lane fetches a pool
  of 3x the limit (capped at 50), drops exact content repeats (whitespace and case folded), then
  takes each session's best match in cosine order before a second turn from any session. Rows
  without a session count as their own session; rows without a uuid never take a slot. The
  section is still sorted oldest first. Off: the lane fetches and returns exactly as before.
  `recall_history` is unchanged.
- Why: in the o500 src-sm10 run, the slots often crowded into one session (e.g. 6 of 10 from a
  single session, with an evidence session absent) and exact repeats took slots.
- o500 recall-only A/B vs src-sm10: 0.810 vs 0.804 (noise). The spread changed 18/500 sections;
  those flipped fixed 3 / broke 0. o500 has a median of 2 sessions per item, so it rarely fires.
- New `domain/source_memory_spread.py` and `tests/test_source_memory_spread.py`; the flag is
  added to the release-config campaign list and its default-on set. `RetrievalTuningConfig` and
  Recall Lab keep `source_memory_spread=False`, as they do for `enable_source_memories`.

## 2026-10-12 - Anchored-time blind attach check (default off) and its labeled sets

- `MENHIR_ANCHORED_TIME_ATTACH` (default off, needs the resolver): after the regex guards and
  `plan_overlay`, the facts it would write are candidates. One blind model call per episode with
  candidates (`anchored_time_attach_client.AnchoredTimeAttacher`; the prompt never sees the
  resolver's items) quotes, per fact, the phrase that dates its start or occurrence. A candidate
  is written only when both phrases overlap at the same place in the turn (V1) and the relation
  matches the kind (V2). A failed call or missing answer writes nothing (V3). New outcomes
  `attach_rejected`, `attach_unverified` and, with the check on, `state_future`; all render like
  `state_start`. `MENHIR_ANCHORED_TIME_ATTACH_MODEL` (empty = the resolver's model).
- `time_contract` is `at2|<resolver version>|<model>|<attach version>|<attach model>` for
  episodes the check decided; `at1` otherwise. Flag off: the offline replay of the 22 runs is
  identical to a0a9814 for all 2374 facts.
- Labeled sets (plan P1): `scripts/build_attach_set.py` (set 1, offline from the frozen runs)
  and `tests/fixtures/anchored_time/attach_adversarial.json` (set 2, 270 review reproducers).
- New tests: `tests/test_anchored_time_attach.py`, `tests/test_anchored_time_attach_hook.py`,
  `tests/test_anchored_time_attach_sets.py`. No existing test changed.
- P3 (paid gate, `scripts/eval_attach.py`): the attach prompt was tuned twice and frozen, then run
  once blind with the regex guards off. G1 and G2 failed, so P5 (retiring the regex attachment
  inference) is not supported. The prompt now also answers "none" when the turn does not say the
  fact is true (prompt e6ecd4d1): no loss on the tune sets, and it fixes the negation failures.
  It also dates something done on a dated occasion in the next clause, unless a word moves it
  (prompt 3aec9649): it fixes H19.5 (1 tune call reused over 5 runs, plus 5 of 5 probe calls),
  with no change with the guards on; with the guards off it adds 2 wrong writes on set 2.
