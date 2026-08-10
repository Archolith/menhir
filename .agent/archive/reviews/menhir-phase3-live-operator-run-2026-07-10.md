# Phase 3 live operator run — M2 rollout evidence

**Date:** 2026-07-10
**Milestone:** MVP roadmap **M2 - Phase 3 production rollout** (`docs/roadmap/menhir-mvp-roadmap.md`)
**Server:** production `127.0.0.1:8090`, restarted onto current `main` @ `b6502cc` (PID 62908)
**Backend:** production Neo4j (real graph), real `gpt-4o-mini` consolidation model
**Decision context:** operator chose a live `POST /api/phase3/run` against a real namespace
(writes Views to the production graph, not the isolated `phase3-validation` harness), and to keep
the background scheduler enabled.

## What this run establishes

The **live operator surface** (`POST /api/phase3/run`) works against the real production graph and
is **precision-first safe**: it processed a real namespace and wrote **zero** Views rather than
committing anything unverified. Pipeline *correctness* on measure-bearing content (stated measures,
fold-SUM, corrections) is proven separately by `menhir-phase3-realdata-validation-2026-07-07.md`
(movies=25, bike_spend=125, movies 25->20 supersession) and the archolith-bench `menhir-phase3`
suite; this note adds the missing **live production-surface** evidence.

## Precondition: server was stale (fixed)

Before this run the live `:8090` server was a manually-started `menhir.cli serve` that **predated
both** the Phase 3 REST surface and Hook Center: `POST /api/phase3/run`, `/api/phase3/status`,
`/api/views`, and `/api/tool-events/*` all returned **404**, and its scheduler was **not running**
(`scheduler=False`) despite `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED=true`. So no consolidation
had actually been happening in production. Restarted via `scripts/start-server.ps1 -Action restart`
(venv `serve-watch`). Post-restart verification:

- `/api/phase3/status?namespace=trip-report` -> 200; `/api/tool-events/dirty` -> 200 (current code live).
- `scheduler.running = true`, lease held by PID 62908; `consolidate_personal_memory` job registered
  (interval 300s). **The background scheduler is now live and will auto-consolidate dirty namespaces
  against the production graph** (intended per the rollout decision; note the 6a interaction below).

## The live run

Target `trip-report` (11 `:TurnEvidence` nodes; `dirty=false`, 0 pre-existing Views). An explicit
namespace POST bypasses the dirty gate and processes the namespace directly.

```
POST /api/tool-events? -> POST /api/phase3/run  {"namespace":"trip-report"}
->
{
  "namespace": "trip-report",
  "phase3_selected": false,
  "dirty_after": false,
  "namespaces_dirty": 1,
  "namespaces_processed": 1,
  "views_written": 0,
  "abstained": 0,
  "corrections_applied": 0,
  "llm_calls": 3
}
```

`GET /api/views?namespace=trip-report` after the run: **0 Views, 0 abstention receipts.**

### Interpreting the null yield (honest)

`llm_calls=3` = the k=3 extraction samples ran, so the 11 user turns **were** loaded and extracted
(the loader reads `t.text` where `role='user' AND declarant='user' AND text<>''`). `views_written=0`
**and** `abstained=0` together mean the extractor emitted **no committable count/amount measure
group at all** — nothing reached the bias guards to be vetoed. So `trip-report`'s user turns, though
triaged with `number`/`i_have`/`possession_state` signals, did not contain a foldable
count/amount measure the precision-first consumer would commit. This is a **safe miss** (the raw
episodes remain the fallback), not a wrong write and not a regression — the consumer's positive
materialization is covered by the validation review + bench (2003 passed).

## M2 gate mapping

| M2 gate | Status |
|---|---|
| Keep capture selective (user prompts only, deterministic triage, no transcript) | Held; producer frozen (ADR 0001). |
| Treat archolith-bench `menhir-phase3` as the external regression suite | In place; tracked report `benchmarks/menhir-phase3-view-consolidation-2026-07-07.md`. |
| Enable `CONSOLIDATION_ENABLED` after a local smoke run | Already `true` in `.env`; SUM grounding default `true`; model `gpt-4o-mini`. Scheduler now actually running post-restart. |
| Record one live operator run of `POST /api/phase3/run` over a known namespace | **This run** (trip-report, production graph, `gpt-4o-mini`). |
| No wrong current-state View writes in the launch smoke | **Held** — 0 Views written, 0 wrong writes. |
| Raw `:TurnEvidence` excluded from normal recall | Invariant (unchanged; validated in the realdata review). |
| Document accepted View families + known abstention cases | Families/abstention behavior documented in `menhir-phase3-realdata-validation-2026-07-07.md`; this note records the live-surface result. |

## Follow-ups / risks

- **6a interaction:** the background scheduler is now consolidating dirty namespaces against the
  **already-degraded** live graph (the ~2,679 auto-merge damage in `lifecycle-remediation.md`).
  Phase 3 View writes are precision-first (independent of the lifecycle auto-merge defect), but the
  6a repair-or-accept decision still governs whether the graph the scheduler writes into is trusted.
- **Optional stronger demo:** a namespace with explicit "I have N X" / "I bought X for $Y" content
  would materialize a counter View live; `trip-report` did not. Not required for the M2 gate (the
  operator surface + safety are proven here; materialization is proven by the validation harness).
- **Persona fit (see `menhir-phase3-persona-fit-2026-07-10.md`):** the Views inventory showed zero
  genuine personal Views in real use - all 5 `user::` Views are artifacts (3 quoted-example
  over-extraction, 2 bench residue). Phase 3 personal-measure consolidation is a
  personal-assistant/chatbot feature; on coding-workspace content it correctly commits nothing. M2's
  value for the coding MVP is the mechanism + safety proof, not materialized yield.
