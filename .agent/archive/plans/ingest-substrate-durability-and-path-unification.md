# Plan: substrate durability + ingest path unification

> **ARCHIVED 2026-07-11 (ctharvey-approved).** Parts 1-4 verified live on the ingest path (no
> flag gating): `ingest_episode` unified onto the queue (`ingest_service.py:827,860`) with
> READY->INGESTED mapping (:838,893), bounded `wait_for_episode_processing` (:497), degraded
> raw-capture via `create_raw_capture_entity` (`episode_maintenance.py:272`,
> `memory_graph_adapter.py:236`, `enrichment_steps.py:284`), and per-job LLM budget backpressure
> (`_check_session_budget`, :316,614). Archived per owner rule (a) fully implemented/shipped.

**Status: IMPLEMENTED + TESTED 2026-07-05.**
Parts 1-4 landed: zero-extraction is a success (READY + empty-extraction receipt, no failure
event); terminal breakage (oversize preflight rejection, exhausted retries) writes a degraded
raw-capture entity through the stamp choke point; `ingest_episode` is unified onto the queue path
(`queue_episode_for_enrichment` + bounded `wait_for_episode_processing`, mapping READY->INGESTED /
FAILED->FAILED / timeout->QUEUED); per-job LLM budget backpressure added. The ~10 ingest tests were
migrated from the old synchronous contract to the queue/worker layer where the behavior now lives
(the stub adapter gained `create_raw_capture_entity`); the WS2 test cluster is green.
The durability/hygiene slice of the 2026-07-03 ingest gap review. Design authority:
`.agent/memory-ingest-under-uncertainty.md` §3 (capture is the commitment), §4c (substrate loss),
§4f (path divergence), §6 (terminal failure needs a recallability story). The other two anchor
docs both *assume* what this plan enforces: raw episodes are always reachable by recall.

## Step 0 — the one-liner (do immediately, independent of the rest)
`ingest_service.py:882`: `project_name = project or session.project or ""` references an
undefined `project` → NameError on every direct ingest, silently swallowed by the blanket
`except` below → document linking has never run on the direct path. Fix to
`session.project or ""` now; Part 3 later deletes the whole block by unification. Add the
regression test the blanket handler prevented anyone from noticing was missing.

## Part 1 — zero-extraction is a success, not a failure
`stamp_and_finalize` marks a zero-node/zero-edge extraction terminally FAILED
(`enrichment_steps.py:687-753`). An "ok thanks" episode with nothing memorable is a *successful
empty determination*: conflating it with breakage inflates failure telemetry and mislabels the
episode anchor.
1. Zero extraction → `mark_episode_ready` with `nodes_touched=0` and a reason receipt
   (`empty_extraction`) instead of `mark_episode_failed`; lifecycle/telemetry events say
   `episode_empty`, not `failed`.
2. `get_failed_enrichment_count` and the failure-event stream stop counting these; the QuantState
   failure counters (agent-experience folds) inherit the correction for free.
3. Keep a guard against systematic emptiness: if a namespace's empty rate spikes, that IS a
   breakage signal — surface via the receipt stream, not by mislabeling single episodes.

## Part 2 — terminal failure keeps the prose recallable (the substrate guarantee)
Episodes that fail terminally with **memorable content** — oversize preflight rejections and
exhausted-retry failures — currently strand their raw text: recall's pending fallback shows only
PENDING/ENRICHING, and no entities exist to carry the content. The write-side doc's entire
abstention argument ("raw episodes always answer") assumes this cannot happen.
1. On terminal *breakage* (not Part 1's empty case — nothing memorable was lost there), write a
   **degraded raw-capture node**: one Entity whose content is the episode text (oversize:
   truncated surface for embedding, full text in content), passed through the standard
   `stamp_ingest_metadata` choke point (namespace, scope, source confidence) with a
   `raw_capture: true` marker and the episode anchor linked for provenance.
2. Recallable by construction (stamped + embedded), rank-competitive only on real similarity —
   no prior, no boost; it exists so the floor has no holes, not to win rankings.
3. Requeue-on-repair: an operator `requeue_failed_episode` that later succeeds supersedes the
   raw-capture node (mark GONE or link-and-expire) so the degraded copy never shadows the
   enriched result. Idempotent: re-failure does not duplicate the capture node.

## Part 3 — one pipeline: direct ingest delegates to the queue
`ingest_episode` duplicates a poorer pipeline (no revision records, no correlation, no structural
anchoring, no fact repair, no rehydration — and the Step-0 dead code). Unify:
1. `ingest_episode` becomes: `queue_episode_for_enrichment(...)` +
   `wait_for_episode_processing(timeout_s=...)` (both exist). Synchronous callers keep their
   semantics — READY within the wait returns INGESTED with real counts; timeout returns QUEUED
   (caller-visible, honest).
2. Delete the divergent enrichment body, including the Step-0 block.
3. Contract note in `endpoints.md`/`tasks-ingest.md`: direct ingest is now queue-backed; the only
   behavioral difference is bounded wait instead of unbounded inline processing.
4. Sequencing: after Part 1 (so a zero-extraction direct ingest reports success-empty, not
   FAILED, through the unified path).

## Part 4 — enforce the per-job LLM budget
`_record_episode_llm_usage` warns at `_budget_settings_max_per_job` and enforces nothing (the
session-window budget does enforce). Match shapes: on breach, mark the episode pending with a
retry_after (the same backpressure-not-failure move), so one pathological episode cannot burn the
window budget for its whole session. Enforcement point is the check before extraction dispatch —
mid-call aborts are not attempted.

## Part 5 (optional, time-boxed) — lifecycle-event noise
`run_graphiti_extraction` emits ~8 lifecycle events around one call (before/dispatch/entered/
wrapper × started/completed/finally) — forensic residue from the stall incidents. Collapse to
started/completed/failed once stall confidence is established. Skip if anything above slips.

## Explicitly NOT in scope (decided, not forgotten)
- Merge/identity gating — `ingest-identity-merge-gating.md`.
- Making raw-capture nodes rank-privileged or lens-routed — they are a floor, not a feature.
- Retry-policy retuning (attempts/lease durations unchanged).

## Verification
1. Unit: zero-extraction marks ready-empty (not failed) and skips failure counters; oversize and
   exhausted failures produce exactly one stamped raw-capture node (idempotent on re-failure);
   successful requeue supersedes the capture node; unified direct path returns INGESTED on fast
   enrichment and QUEUED on timeout; per-job breach requeues with retry_after.
2. Substrate assertion (the point of the plan): for an invented oversize episode, a recall query
   quoting its content returns the raw-capture node — the floor has no hole.
3. Full ingest test suite green; direct-path tests updated deliberately for unification
   (behavior change, called out in the commit).
