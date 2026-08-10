# Phase 3 real-data validation — hook -> :TurnEvidence -> consolidation -> Views -> recall

**Date:** 2026-07-07
**Scope:** Freeze the selective-capture producer and MEASURE the end-to-end Phase 3 pipeline on the
real Neo4j graph and the real personal-memory LLM. No producer expansion (no assistant/tool turns,
no full-transcript mode, no new triage categories).
**Harness:** `scripts/validate_phase3_realdata.py` (re-runnable; isolated to namespace
`phase3-validation`; mirrors the prod scheduler wiring — `make_sync_chat(model=personal_memory
_chat_model)` + `make_view_embedder`, k=3, all bias guards pinned on).
**Verdict:** the pipeline SKELETON is validated and working; the consumer (perception/fold) had two
real defects that made 2 of the 4 minimum cases only partially work. **Do not expand the producer
yet** — the defects are consumer-side, and more capture would multiply the misses, not fix them.

> **RESOLUTION ADDENDUM (2026-07-07) — F1 and F2 fixed and re-validated.** Both consumer-side defects
> below are now fixed; the producer stays frozen.
>
> - **F1 (commit `8cddd9b`)** — the self-consistency gate now votes on a semantic measure FAMILY
>   (`subject` + singularized-noun signature + reducer) instead of the literal key, so
>   `bike_spend`/`bikes_spend`/`bikes_purchased` count as one cluster. Only scattered families (≥2
>   labels) are rewritten to a deterministic canonical key; single-label families are untouched.
>   `record_abstentions=True` is now pinned in the Phase 3 task so misses leave per-veto receipts.
>   Re-measured live (bike prompt ×10): canonical key `bike_spend` every run, value always 125,
>   **commit 8/10** (was ~2/5); the 2 misses are the Lever-C4 verifier failing closed (a separate
>   precision lever, now observable via a receipt), **zero** `self_consistency` scatter.
> - **F2 (this commit)** — a new deterministic `correction_resolver` binds a bare numeric correction
>   ("actually it is 20, not 25") to the UNIQUE current View whose value == the old number, in the
>   same namespace, superseding it (ambiguous / no-match → abstain, nothing touched). The superseding
>   write reuses the fold sink with the correction turn's later `recorded_at`, so it wins LWW and
>   survives batch re-folds. Re-validated live: `corrections_applied=1`, **movies 25 → 20 current**,
>   prior 25 superseded (`current:false`, `expired_at` set), bike View untouched, idempotent on re-run.
>
> Case scorecard after the fixes: (1) stated measure ✅ · (2) fold SUM ✅ (stable key; commits subject
> to the verifier lever) · (3) supersession ✅ · (4) junk ✅ dropped. Full suite: **2003 passed, 31
> skipped**. The original findings are preserved verbatim below for the record.

---

## Scorecard (the questions asked)

| Question | Result |
|---|---|
| Prompts passing triage | **3 / 4** (junk `write the handoff` dropped) |
| Stored as `:TurnEvidence` | **3 / 3** candidates (via live `POST /api/turn-evidence`, all 200/created) |
| Phase 3 selected the namespace | **Yes** (dirty query picked it up) |
| Views written | **2 current** (`movies=25`, `bike_spend=125`) |
| Abstained (run 1) | **3** measures (all veto-1 self-consistency; `llm_calls=3` = extraction only, no guard ever reached) |
| Re-run duplicate/divergent writes | **0** — no existing View was overwritten with a divergent value |
| Re-run is deterministic | **No** — a borderline SUM flips abstain<->commit across identical runs (see F1) |
| New evidence re-dirties namespace | **Yes** |
| Watermark debounce (0 re-LLM) | **Yes** — namespace drops out of the dirty set after a pass |
| Recall improvement | **Yes** — 0 durable Views before, 2 after (aggregate answers now materialized) |
| Supersession / currentness | **No** — the correction never supersedes (see F2) |

### Per-case verdict (the 4 minimum cases)

| # | Prompt | Expected | Actual |
|---|---|---|---|
| 1 | `I have 25 movies on my watch list.` | stated-measure View | **PASS** — `movies=25`, unanimous 3/3, stable |
| 2 | `I bought one bike for $50 and another for $75.` | fold-derived SUM View | **PARTIAL** — value always 125, but commits only ~40% of runs and under an unstable key (F1) |
| 3 | `Actually it is 20, not 25.` | supersede to 20 | **FAIL** — never linked to `movies`; stays 25 (F2) |
| 4 | `write the handoff` | not stored / not processed | **PASS** — dropped by triage, never left the machine |

---

## What is validated (works on real data)

1. **Deterministic triage is correct.** Cases 1/2/3 matched evidence signals
   (`i_have`+`number`+`possession_state`; `i_bought`+`money`+`number`; `correction`+`number`);
   case 4 matched nothing and was dropped before any network call. No LLM ran in triage.
2. **Live capture path works end to end.** Each candidate posted through the real
   `POST /api/turn-evidence` (agent-tier auth + `routes.py` + `TurnEvidenceRepository` + Neo4j)
   returned `200 created=true`; the nodes were present and readable by an independently-built adapter.
3. **Phase 3 selection + watermark debounce work.** The namespace was dirty before a pass and
   NOT dirty after (`ConsolidationWatermark` read/write are consistent). A new turn re-dirtied it.
   In prod this means the scheduler (`namespaces=None`) consolidates a namespace once per
   new-evidence arrival and then skips it — **0 re-LLM** on unchanged namespaces.
4. **Recall gains a durable aggregate.** Before consolidation the only answer to "how many movies
   on my watch list" was the raw episode; after, a `counter` View holds the value directly. The
   raw evidence never entered normal recall (invariant held).
5. **No wrong View was ever written.** Every failure below is a MISS (safe under the precision-first
   FP>>FN design: the raw episode is the fallback), never a confidently-wrong committed value.

---

## Finding F1 — measure-key instability defeats the self-consistency gate (fold-derived SUM unreliable)

**Severity: medium (recall/stability).** The bike SUM is *arithmetically* never wrong — the value is
`125.0` in every sample that perceives it. The problem is the **measure KEY scatters across the k
samples**: the extractor keys the same quantity `bike_spend` / `bikes_spend` / `bikes_purchased`
non-deterministically. The self-consistency gate groups by `(subject, measure)` and requires the
modal *key* to hold unanimously (threshold 1.0) across k=3, counting samples that used a different
key as ABSENT. So when the key scatters, every key is sub-unanimous and **all of them abstain**.

Observed over 5 identical passes (real `perceive_and_fold`, cases 1+2, guards on):

```
pass 0: bike_spend      COMMIT  125  dist={'125.00': 3}                 # all 3 keyed bike_spend
pass 1: bikes_spend     COMMIT  125  dist={'125.00': 3}                 # all 3 keyed bikes_spend (different key!)
pass 2: bike_spend      ABSTAIN[self_consistency] dist={'125.00':2,'__absent__':1}
        bikes_spend     ABSTAIN[self_consistency] dist={'__absent__':2,'125.00':1}
pass 3: bike_spend      COMMIT  125
pass 4: bikes_purchased ABSTAIN[self_consistency] dist={'125.00':1,'__absent__':2}
        bike_spend      ABSTAIN[self_consistency] dist={'__absent__':1,'125.00':2}
-> bike_spend committed 2/5 passes; committed KEY varies (bike_spend vs bikes_spend)
```

`movies=25` (a clean STATED total) is stable because its key is a single obvious noun. The failure is
specific to fold-derived measures whose key the model names inconsistently.

**Why the canonicalization layer doesn't save it:** `_MEASURE_ALIASES` (perception.py) collapses the
cycling/watch-list synonym families it was seeded from, but does **not** cover
`bikes_spend`/`bike_spend`/`bikes_purchased`. The layer is a hand-seeded alias table, so it only
catches scatter it has already seen.

**Prod consequence (important):** the watermark debounce (correct on its own) means each namespace is
consolidated **once** per new-evidence arrival. Whatever that single pass draws is frozen until new
evidence arrives — so a correct, useful `bike_spend=125` is silently missed ~60% of the time, and
when it lands the stored key is non-deterministic.

**Recommended fixes (consumer-side; do before expanding capture):**
- **Vote on value within a reducer class, not on the exact key.** Compute self-consistency agreement
  over `(subject, reducer, quantized_value)` and then pick the modal key as the label. A unanimous
  `sum -> 125` should commit regardless of whether the label was `bike_spend` or `bikes_spend`.
  This is the highest-leverage fix and generalizes past the alias whack-a-mole.
- **Or** extend `_MEASURE_ALIASES` for the observed bike trio (stopgap, does not generalize).
- **Turn on abstention telemetry.** `consolidate_personal_memory` calls `perceive_and_fold` without
  `record_abstentions=True`, so prod records **no** reason for a miss. Flipping it on writes
  `perception_abstained_self_consistency` counters, making F1 observable in prod instead of invisible.

## Finding F2 — a bare numeric correction never supersedes (currentness fails)

**Severity: medium (currentness).** `Actually it is 20, not 25.` does **not** update `movies`. Batch
re-fold loads all namespace evidence together, yet across 4 extraction samples the correction
produced **zero** events/assertions keyed to `movies` (or anything):

```
sample 0..3 (episodes: "25 movies on watch list", "bike $50/$75", "Actually it is 20, not 25"):
   movies      reducer=stated stated_total=25.0 nevents=0
   bike_spend  reducer=sum    nevents=2
   # "Actually it is 20, not 25." -> NO group emitted in any sample
```

**Root cause:** it is an extractor-**linkage** failure, not a fold/LWW-tie. The correction has no
measure noun ("movies", "watch list"), so the extractor cannot key `20` to the `movies` measure and
emits nothing. The LWW-by-latest-assertion machinery in the fold never gets a second assertion to
choose between — so there is no supersession to perform. (Even if it *were* linked, both episodes
carry the same capture date, so date-only LWW would tie — a secondary risk, but not the cause here.)

**Recommended fixes (consumer-side):**
- Give the extractor **context to resolve anaphoric corrections**: when a turn is a bare correction
  ("actually it's N, not M"), bind it to the most recent stated measure in the batch whose current
  value is `M`. The `correction` triage reason is already on the evidence node — the extractor prompt
  could be told which measure `M` matches.
- Add a **recency tiebreaker** to the assertion LWW so same-date corrections still win by capture
  order (`recorded_at`), not just `when`-date.

---

## Overall recommendation

**The hook idea is validated at the plumbing level and should be frozen, not expanded.** Triage,
capture, idempotent persistence, dirty-selection, watermark debounce, and recall materialization all
work on real data. But the **consumer** (perception/fold) reliably handles only the clean stated-total
case (case 1); the fold-derived SUM (case 2) is unstable and the correction/supersession (case 3)
does not work at all.

Expanding the producer now (more turn kinds, more triage categories, transcript mode) would feed the
same consumer more borderline material and multiply F1's misses and F2's stale values — exactly the
"expansion multiplies noise" risk. **Sequence:** (1) fix F1 key-stability + turn on abstention
telemetry; (2) fix F2 correction linkage + recency tiebreaker; (3) re-run this harness and confirm
cases 2 and 3 go green; (4) only then consider producer expansion; (5) then the fresh-Neo4j benchmark
harness for launch-grade recall/cost evidence.

---

## Reproduce

```
cd projects/archolith/menhir
.venv/Scripts/python.exe scripts/validate_phase3_realdata.py --keep --json out.json
```

Isolated to namespace `phase3-validation`; purges that namespace on start and (without `--keep`) on
exit. Verified to leave **zero** residue in the dev graph. Server must be live on :8090 with the
personal-memory chat model configured. Cost: ~4 gpt-4o-mini consolidation passes (tens of calls; no
rate-limit exposure).
```
