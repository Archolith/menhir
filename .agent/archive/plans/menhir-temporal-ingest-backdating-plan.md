# menhir — backdated ingest (`reference_time` passthrough) for faithful LongMemEval temporal scoring

> **ARCHIVED 2026-07-11 (ctharvey-approved).** menhir product change shipped and verified
> (back-compat): `occurred_at` on `MemoryRequest` (`routes.py:204`) -> `_parse_occurred_at`
> (`ingest_service.py:58,458`) -> persisted `reference_time` on the Episodic node
> (`episode_lifecycle.py:57,88,110`; cypher return fields `cypher.py:258,316,342`) ->
> `coalesce(reference_time, queued_at)` at Graphiti (`enrichment_steps.py:481`); tests
> `tests/test_occurred_at.py`. Integration test PASSED (2023 `reference_time` distinct from
> `queued_at`). The two remaining DoD items (A/B accuracy lift + full oracle-500 build) are bench
> runs, tracked in `deferred-verification.md`. Archived per owner rule (a).

**Date:** 2026-06-29
**Projects in scope:** `menhir` (product change — the load-bearing work) + `archolith-bench` (benchmark client + ingest script, the consumer). One project per session for writes; do menhir first, then archolith-bench.
**Driver:** LongMemEval Mode B (oracle variant) standalone graph build. ~42% of the 500 questions are time-dependent (temporal-reasoning 133, knowledge-update 78); the current ingest discards all event times, so menhir's headline temporal-KG capability is untested / understated.

## Problem (verified this session)

The HTTP ingest path is built for **live** ingest ("store what's happening now"), not **historical replay** of a dated transcript:

- `MemoryRequest` (`api/routes.py` ~line 152) has fields `{episode, source, session_id, user_id, diff, namespace}` — **no timestamp**.
- `queue_episode(...)` (`core/backend_impl.py` ~165 and ~1049) and `queue_episode_for_enrichment(...)` (`services/ingest_service.py` ~376) have **no `reference_time` parameter**.
- The Episodic node is created with `queued_at: datetime()` (Neo4j now) in `infrastructure/episode_lifecycle.py:85`.
- The enrichment worker passes Graphiti `reference_time=coerce_reference_time(ctx.claimed.get("queued_at"))` (`services/enrichment_steps.py:451`). `coerce_reference_time` (`enrichment_steps.py:108`) falls back to `now()` when the value is missing.

Net: **every episode's `valid_at` = ingestion time.** The LongMemEval haystack carries real dates (`item["haystack_dates"]`, one per session; `item["question_date"]` = "now"), and the sessions are **not** in chronological order (verified: 12/20 items have non-chronological session order), so:

| Failure | Question types hit | Count |
|---|---|---|
| Absolute event dates gone | temporal-reasoning | 133 (27%) |
| "Latest fact wins" broken (last-ingested ≠ newest) | knowledge-update | 78 (16%) |
| Session boundaries collapse (shared default `session_id`) | multi-session | 133 (27%) |

## Key design decision — DO NOT overload `queued_at`

`queued_at` is used for three things: (1) Graphiti `reference_time`, (2) FIFO enrichment **queue ordering** (`episode_lifecycle.py:122` `order_by coalesce(n.queued_at,...)`), (3) **staleness / orphan-lease recovery** (`episode_lifecycle.py:136,522`, `episode_maintenance.py:174,200`). Backdating `queued_at` to a 2023 date would (a) jam the episode to the front of the queue and (b) make stale-lease recovery treat it as orphaned. So **add a separate `reference_time` field** on the Episodic node, default null, and have the worker prefer it: `coalesce(reference_time, queued_at)`. Queue ordering / staleness keep using `queued_at` (real now); only Graphiti's temporal reference changes.

The temporal pipeline otherwise already exists end-to-end — this is a passthrough, not new machinery.

---

## Phase 1 — menhir (product change)

Thread an optional `occurred_at` (ISO-8601 string) from the HTTP boundary to a persisted `reference_time` on the Episodic node, and have the enrichment worker use it.

1. **`api/routes.py`** — `MemoryRequest`: add `occurred_at: str | None = None`. `ingest_memory(...)`: pass `occurred_at=body.occurred_at` into `backend.queue_episode(...)`.
2. **`core/backend_protocol.py`** — `queue_episode(...)` protocol: add `occurred_at: str | None = None`.
3. **`core/backend_impl.py`** (BOTH definitions) — add `occurred_at` param + thread through.
4. **`services/ingest_service.py`** — `queue_episode_for_enrichment(...)`: add `occurred_at`, `_parse_occurred_at` helper, pass `reference_time=<dt>` to `create_pending_episode(...)`.
5. **`infrastructure/episode_lifecycle.py`** + `memory_graph_adapter.py` — `create_pending_episode(...)`: add `reference_time: datetime | None = None`; persist in CREATE cypher.
6. **`infrastructure/cypher.py`** — add `n.reference_time AS reference_time` to MEMORY_RETURN_FIELDS, EPISODE_CLAIM_FIELDS, EPISODE_PROCESSING_FIELDS.
7. **`services/enrichment_steps.py:451`** — `reference_time=coerce_reference_time(ctx.claimed.get("reference_time") or ctx.claimed.get("queued_at"))`.
8. **Tests** — `tests/test_occurred_at.py`: 8 tests covering `_parse_occurred_at` + full threading + back-compat.

## Phase 2 — archolith-bench (consumer) + two efficiency wins

9. **`archolith_bench/harness/menhir_client.py`** — `HttpMenhirClient.ingest` + `StubMenhirClient.ingest`: add `occurred_at`, `session_id`, `wait` kwargs.
10. **`scripts/_ingest_lme.py`** — `_parse_lme_date()` helper; `_ingest_turn` passes `occurred_at`/`session_id`/`wait=False`; main loop zips sessions with `haystack_dates`+`haystack_session_ids`.
11. **Efficiency win:** `wait=False` per turn (removes ~11k blocking round-trips; per-item drain is the completeness guarantee).
12. **Efficiency win:** `_drain` default `poll_s=2.0` (was 5.0; saves ~33 min across 500 items).

## Phase 3 (OPTIONAL) — sharded parallel build (only if ~1 day is too slow)

K menhir+Neo4j pairs each building 500/K items → ~K× throughput. Needs namespace→shard routing in the recall runner. Defer unless wall-clock demands it.

## Verification

- **menhir unit:** `tests/test_occurred_at.py` 8/8 green. Full suite 875 passed (3 pre-existing failures).
- **archolith-bench:** 33/33 harness tests green.
- **Integration (oracle, 1 dated item):** ingest one temporal-reasoning item, cypher-check:
  `MATCH (e:Episodic {namespace:'lme-<qid>'}) RETURN e.reference_time LIMIT 5;`
- **End-to-end A/B:** run `_run_lme_variant.sh main` on an oracle subset; confirm temporal-reasoning + knowledge-update accuracy is materially higher than the timestamp-less baseline.

## Risks / mitigations

- **Overloading `queued_at`** → handled by the separate `reference_time` field.
- **Graphiti edge validity:** confirm `add_episode(reference_time=...)` propagates to extracted edge `valid_at`.
- **Existing episodes lack `reference_time`** → `coalesce(reference_time, queued_at)` keeps them working.
- **Date-parse failures** → fall back to `None`/`now()` (no crash).
- **Back-compat:** `occurred_at` defaults None everywhere → live-ingest path unchanged.

## Definition of done

- [x] menhir: `occurred_at` → persisted `reference_time` → Graphiti `reference_time`; unit tests + back-compat green. **DONE** — commits `e28523b` (partial: protocol/impl/cypher/adapter) + `6416302` (routes/ingest_service/episode_lifecycle/enrichment_steps + 8 tests). Branch: `claude/menhir-chain-handoff-doc-7iuat2`.
- [x] archolith-bench: client + `_ingest_lme.py` pass per-session `occurred_at` + `session_id`. **DONE** — commit `96bc29e`. 33/33 harness tests green.
- [x] Integration: a dated oracle item shows 2023 `reference_time` on its Episodic nodes (not today). **DONE** — `scripts/_integ_reference_time.py` exercised `EpisodeRepository.create_pending_episode(reference_time=datetime(2023,7,14,8,30,utc))` against `menhir-neo4j-dummy`; cypher read confirmed `e.reference_time = 2023-07-14T08:30:00Z`, `e.queued_at = 2026-06-30T04:23:01Z` (separation preserved). **PASS.**
- [ ] A/B on an oracle subset shows lifted temporal-reasoning / knowledge-update accuracy.
- [ ] Then: clean full oracle 500 build on the corrected ingest.
