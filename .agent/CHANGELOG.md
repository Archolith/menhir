# Changelog

Older entries: [`CHANGELOG-archive.md`](CHANGELOG-archive.md).

The legacy `user` extraction prompt work in the archive is marked "Superseded by `automatic-memory-v1` on 2026-09-06". `tests/test_canonical_self_contract_docs.py` checks for that phrase in this file.

## 2026-11-07 - Design records moved in from the maintainer workspace

- Added 16 plans and reviews, trimmed of local paths, host details and live configuration: three current plans, four backlog plans, five archived plans plus the bucket-1 removal plan, the #168 off-flag triage, the agent prompt review and the combined PR #274 reviews.
- `plans/README.md`, `plans/backlog/README.md`: routed the new plans.
- `archive/removed-features-2026-10-27.md`: its evidence and plan links now resolve here.

## 2026-11-07 - Recall test records added to reference

- `reference/`: added the real-similarity, no-match and graph-value test records, trimmed of local paths and host names.
- `reference/menhir-projection-coverage-audit.md`, `reference/menhir-realization-coverage.md`: retyped as `investigation`.
- `reference/README.md`: the evidence-admission PDF is suggestions only, and its two validator findings are accepted.

## 2026-11-06 - Doc review: removed features marked, reference docs typed

- `architecture.md`, `data_models.md`, `endpoints.md`, `memory-backlog.md`: marked the event-history lane and deterministic shadow as removed.
- `artifacts-provenance-governance-status.md`, `scripts-index.md`, `workflows/scalar_state_measurement.md`, `audit/MODULE-MAP.md`: corrected claims about retired flags and removed scripts.
- `README.md`, `reference/README.md`, `maintenance.md`: fixed routing, the reference count and a duplicate heading.
- `reference/*.md`: declared an artifact type on 13 records, with no IDs or statuses added.
- Four plans: corrected invalid statuses to `IMPLEMENTED`.
- `../CHANGELOG.md`, `../CHANGELOG-archive.md`: moved the oldest 8 entries to meet the 10-entry limit.

## 2026-11-06 - Research routing and changelog trim

- `workflows/artifact_authoring.md`: added where research results go.
- `README.md`: routes recall questions to the experiment record.
- `maintenance.md`: older changelog entries now move to `CHANGELOG-archive.md`.
- `CHANGELOG.md`, `CHANGELOG-archive.md`: moved 114 older entries to the archive. One Windows home path became a repo-style path; the rest is unchanged.
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
