# Lifecycle F2 — lawful sharpness recomputation (implementation plan)

**Design authority:** @ctharvey | **Status:** P1-P4 IMPLEMENTED + P3 CALIBRATED (`SHARPNESS_COSINE_FLOOR = 0.80`); P5 re-enable PENDING | **Parent:** `plans/lifecycle-remediation.md` §F2 | **Todo:** `1a9eb1f2`

> **Implementation note (07-10):** code landed on `main` in `ba43ab9`. **P3 CALIBRATED 07-10** against
> the live LME `default` namespace (n=150): `SHARPNESS_COSINE_FLOOR = 0.80` (was provisional 0.75,
> which the probe showed was a 63%-compress cliff). The probe is now a real read-only tool
> (`scripts/probe/probe_sharpness_cosine_floor.py`, bootstraps the live graphiti client); result in
> `.agent/reviews/f2-cosine-floor-probe.md`. P5 gate re-enable is untouched and stays sign-off-gated.

**Chosen approach (2026-07-10):** Option 1 — *true cosine*. Confirmed feasible: graphiti's
node vector search already computes a real cosine similarity and exposes a genuine cosine cutoff
(`NodeSearchConfig.sim_min_score`). We stop deriving the similar-neighbor count from a rank artifact
and derive it from a real cosine distance instead. Options 2 (recalibrate RRF) and 3 (hybrid) are
rejected — they keep calibrating against a rank score whose value depends on result-set composition.

**Revised 2026-07-10 after review** — corrected the causality (see §Problem), fixed count semantics,
degraded-startup parity, probe reproducibility, Graphiti-contract tests, and stale references.

---

## Problem (grounded — corrected per the 2026-07-04 CORRECTION)

Sharpness (a memory's uniqueness score) is the sole gate on the lifecycle arms. The scale-probe
(`.agent/archive/reviews/menhir-lifecycle-scale-probe-2026-07-03.md`) and its **CORRECTION
(2026-07-04)** split the original "59% collapsed" finding into two *distinct* defects. F1 fixed one;
F2 fixes the other. Getting this attribution right is what makes the regression fixture meaningful:

- **Defect A — stamp-time 0.0 default (the <0.1 population). ALREADY FIXED BY F1.** The ~18,990
  persistent nodes at sharpness `<0.1` were **predominantly stamp-time defaults**: stamping set
  `n.sharpness = coalesce(toFloat(n.sharpness), 0.0)` at `episode_stamping.py:56,102`, so every
  memory initialized *maximally forgettable* until recomputed. This — not RRF — was the primary
  feeder of the GONE gate (`<0.1`) and its 115 recorded deletions. **F1 removed that default and
  made missing sharpness protective. F2 does not target this band.**

- **Defect B — RRF-corrupted computed sharpness (the 0.2–0.5 band). THIS IS F2.** On a two-method
  (bm25 + cosine) `1/(rank+1)` RRF ladder, `compute_sharpness` **cannot produce values below ~0.2**
  (at most ~4 hits can score ≥0.7). The RRF miscalibration therefore corrupts the **0.2–0.5 band** —
  exactly where the consolidation **promote bar (≥0.5)** and the **compress gate (<0.3)** live. A
  genuinely-unique node whose neighbors merely *rank* near the top gets a mid-band RRF sharpness,
  fails the promote bar, and (in the disarmed-today path) was treated as session-deletable. That is
  the H2 session-deletion damage. F2 makes the computed value a true-cosine measure so the 0.2–0.5
  band reflects real uniqueness.

Mechanism of Defect B in code:
1. `_count_similar_nodes` (`lifecycle_service.py:252`) calls `graphiti_client.search_scored(...)`,
   configured `reranker=NodeReranker.rrf` (`graphiti_client.py:915`) → `score` is an RRF rank-fusion
   value (max = 1/2+1/3 = 0.8333 fingerprint), not a cosine similarity.
2. It counts results with `score >= 0.7` (`:274`) — a cosine-shaped cutoff on a rank score.
3. `compute_sharpness(count) = 1/(1+count)` (`:87`) turns that into sharpness, corrupted across
   0.2–0.5.
4. Gates `MemoryTypePolicy.should_compress`/`should_delete` (`memory_types.py:52,82`) compare against
   `compress_sharpness ≈ 0.3` and `gone_sharpness = 0.1` — calibrated for a real [0,1] measure.

Disarm state in the tree: H3 GONE (`lifecycle_service.should_delete` → `return False`, `:558`) and
H2 session deletion (the `else: pass` at `:205–211`). F1 made disarming *safe*; F2 makes re-enabling
*correct* — for the compress/promote bands and the H2 path specifically.

## Feasibility finding (why Option 1 is clean)

- `NodeSearchConfig.sim_min_score` (`graphiti_core/search/search_config.py:91`) is a genuine cosine
  minimum applied **inside the vector search** (`search.py:340` → `node_similarity_search`
  `min_score`), *before* any reranking.
- The Neo4j node vector query filters `WHERE score > $min_score` (**strict `>`**, default
  `min_score=0.6`) in `graphiti_core/driver/neo4j/operations/search_ops.py:158`. So a cosine-only
  search with `sim_min_score = X` returns only nodes whose real cosine similarity is **strictly
  greater than X**. We set an honest floor and **count** the neighbors that clear it; the reranker
  score on the returned set is irrelevant and ignored.
- Boundary note: because the filter is strict `>`, the floor `X` is an *exclusive* lower bound — a
  neighbor at exactly cosine `X` is not counted. This is acceptable (documented, not compensated);
  the calibrated floor (P3) is chosen against the strict-`>` behavior it will actually run under.

---

## Parts

| Item | What | File(s) | Status |
|------|------|---------|--------|
| P1 | New cosine-floored similarity count on the client | `graphiti_client.py` | THIS PASS |
| P1b | Sentinel parity for degraded startup | `core/bootstrap.py` | THIS PASS |
| P2 | Rewire sharpness feeder to P1 | `lifecycle_service.py` | THIS PASS |
| P3 | Calibrate the cosine floor empirically | new probe script + constant | THIS PASS |
| P4 | Tests (counting + Graphiti contract) | `tests/` | THIS PASS |
| P5 | Gate re-enable (H3, then H2) | separate, sign-off-gated | DEFERRED |

### P1 — `count_similar_by_cosine` on `GraphitiClient`

Add a method alongside `search_scored` (`graphiti_client.py:887`):

```python
async def count_similar_by_cosine(
    self,
    query: str,
    *,
    exclude_uuid: str,
    min_cosine: float,
    limit: int = 50,
    group_ids: list[str] | None = None,
) -> int:
    """Count DISTINCT entity nodes whose true cosine similarity to `query` is > min_cosine.

    Cosine-only search with sim_min_score as a genuine cosine floor (applied in the vector
    search, strict `>`, before reranking). Returns the count of qualifying neighbors — distinct
    by uuid, excluding `exclude_uuid` and Episodic nodes. Returns -1 if the similarity search is
    unavailable (advisory, not critical — mirrors the -1 contract _count_similar_nodes has today).
    """
```

Mechanism:
- `SearchConfig(node_config=NodeSearchConfig(search_methods=[NodeSearchMethod.cosine_similarity],
  reranker=NodeReranker.rrf, sim_min_score=min_cosine), limit=limit + 1)`. Reranker is moot — the
  `sim_min_score` floor selects the set; we count, we don't read the score.
- **Cap + self-exclusion (fixes the undercount):** request `limit + 1` results, then drop
  `exclude_uuid` and de-duplicate by uuid, so the queried node consuming a slot never costs a real
  neighbor. The count is still bounded by `limit`; **document the cap:** `compute_sharpness =
  1/(1+count)` saturates below 0.1 by ~9 neighbors and below 0.2 by ~4, so any cap ≥ ~10 cannot
  change a decision — the cap is intentional and immaterial to the gates. `limit=50` is chosen as
  comfortably past saturation.
- **De-dup is required, not incidental:** count DISTINCT uuids. This closes the known duplicate-
  inflation gap that `test_edge_cases.py::TestDuplicateUUIDSharpness` (`:771`) currently documents
  (see P4).
- Run `self.client.search_(query, config, group_ids=group_ids)` behind the existing
  `_ensure_graphiti_endpoints_alive` guard. **The endpoint guard is inside the method's exception
  contract:** wrap guard + search in one try; any exception (including
  `_is_vector_dimension_mismatch_error`) returns `-1`, because a dimension mismatch means the cosine
  index is unusable and there is no lawful similarity signal. Do **not** fall back to BM25 — a
  lexical count is not a uniqueness measure and would reintroduce a scale mismatch.

### P1b — `UnavailableGraphitiClient` sentinel parity

`LifecycleService` accepts the degraded-startup sentinel `UnavailableGraphitiClient`
(`core/bootstrap.py:44`), which today stubs `search_scored` by raising. Add:

```python
async def count_similar_by_cosine(self, query, *, exclude_uuid, min_cosine,
                                  limit=50, group_ids=None) -> int:
    self._raise()
```

`LifecycleService`'s own try/except around the call converts that raise into the `-1` advisory-
unavailable contract, so degraded startup skips sharpness (unchanged behavior). Without this the
sharpness path would `AttributeError` under degraded startup instead of degrading gracefully.

### P2 — Point sharpness at the lawful count

In `lifecycle_service.py`, `_count_similar_nodes` (`:252`) becomes a thin wrapper over
`count_similar_by_cosine` (both call sites keep calling `_count_similar_nodes`):

- Call sites: consolidation `:190` and decay `:750`. Both already handle `similar_count < 0`
  (skip + warn) — that contract is preserved by P1/P1b returning `-1`.
- Replace the RRF-scale `threshold: float = 0.7` with `min_cosine` sourced from the calibrated
  constant (P3). Keep `namespace` scoping (`namespace_to_group_ids`).
- `compute_sharpness` (`:87`) is **unchanged** — `1/(1+count)` is lawful once `count` is lawful.

### P3 — Calibrate the cosine floor (reproducible)

The old `0.7` was an RRF-scale number; it does not transfer to real cosine. Pick `min_cosine` from
data with a committed, re-runnable probe (the earlier `probe_telemetry.py` / `probe_graph.py` were
job-tmp scratch and are not in the repo; there is no `probe_rrf_scale.py`).

- **Script (new, committed):** `scripts/probe/probe_sharpness_cosine_floor.py`. **Read-only** — no
  writes, no lifecycle actions; asserts read-only at the top and uses only search + node reads.
- **Namespace / corpus:** run against a real production namespace (default `agent-experience`, the
  largest genuine one; parameterize `--namespace`). **Sample:** up to 500 PERSISTENT entity nodes
  sampled deterministically (`--sample 500 --seed 0`); if fewer exist, use all.
- **Command:**
  `.venv/Scripts/python.exe scripts/probe/probe_sharpness_cosine_floor.py --namespace agent-experience --sample 500 --seed 0 --floors 0.60,0.70,0.80,0.85 --out .agent/reviews/f2-cosine-floor-probe.md`
- **Output artifact:** `.agent/reviews/f2-cosine-floor-probe.md` — for each candidate floor, the
  resulting sharpness histogram (buckets: `<0.2`, `0.2–<0.3`, `0.3–<0.5`, `0.5–<1.0`, `=1.0`),
  median, and the fraction landing in each gate band (compress `<0.3`, promote `≥0.5`).
- **Selection criterion for "discriminating"** (numeric, decided from the artifact): choose the
  smallest floor such that (a) `=1.0` (zero-neighbor) share ≤ 60% — not everything is "unique", and
  (b) `<0.3` (compress-eligible) share ≤ 25% — not a mass-compress cliff, and (c) the 0.2–0.5 band
  is populated (not empty) so promote/compress actually discriminate. Record the chosen value and
  the winning row inline at the constant's definition site.
- Define the floor as a named constant `SHARPNESS_COSINE_FLOOR` near the other lifecycle thresholds
  (`lifecycle_service.py:34`), not a literal.
- **Do not** touch `compress_sharpness` / `gone_sharpness` in `memory_types.py`. If P3's distribution
  shows they need re-tuning, that is a *recorded follow-up decision*, not a silent edit in this pass.

### P4 — Tests

**Counting semantics** (fake scored set proves the count math):
- distinct neighbors above floor counted; `exclude_uuid` dropped; Episodic dropped; **duplicate uuid
  counted once**; dimension-mismatch → `-1`; any other exception → `-1`.
- **Resolve `test_edge_cases.py::TestDuplicateUUIDSharpness` (`:771`, currently `assert count == 3`,
  "known gap"):** F2 closes the gap by design. Update the test to drive the new
  `count_similar_by_cosine` path and assert the **deduped** count (`2`), and reword the docstring
  from "documents the gap" to "dedups by uuid". This is an intentional behavior fix, not a refactor —
  the test is repointed to the new contract, not contorted to preserve the old one.

**Graphiti contract** (a fake scored set cannot prove this — capture the `SearchConfig`):
- Inject a fake graphiti client that records the `SearchConfig` passed to `search_`. Assert:
  node `search_methods == [NodeSearchMethod.cosine_similarity]` (cosine-only, **no BM25**),
  `node_config.sim_min_score == min_cosine`, `limit == limit + 1`, and `group_ids` equals
  `namespace_to_group_ids(namespace)` (namespace scoping preserved).
- Assert the dimension-mismatch path returns `-1` **without** issuing a BM25-only fallback search.

**Regression fixture (corrected band):**
- Build a fixture in the **RRF-corrupted 0.2–0.5 band**, NOT `<0.1`: a node that under the old
  RRF+0.7 path had enough rank-neighbors to score mid-band (e.g. RRF sharpness ≈ 0.33, failing the
  ≥0.5 promote bar) but whose *true* cosine neighbor count above the calibrated floor is 0 → new
  `compute_sharpness` → `1.0` (protective, promotes). This is the exact H2 session-deletion
  regression the CORRECTION identified; assert the flip from mid-band-corrupt to protective.

**Existing suites:** `test_decay_logic.py` / `test_lifecycle_service.py` stay green (the
`similar_count < 0` skip contract is unchanged). Repoint only the mocks that stubbed `search_scored`
for the sharpness path at `count_similar_by_cosine`; do not preserve old patch paths by contorting
behavior.

### P5 — Gate re-enable (SEPARATE, sign-off-gated — not this pass)

F2 fixes the *measure*; turning the irreversible arms back on is a distinct, explicitly gated step:

- **H3 (decay GONE):** re-enable = `LifecycleService.should_delete` (`:546`) delegates to
  `policy.should_delete(node)` instead of `return False`. F1 removes the primary stamp-default
  source of `<0.1` values; true-cosine counts of 10+ still yield `<0.1`, so H3 eligibility is not
  zero by construction — whether it is *rare* must come from the P3 probe, not be assumed. After F2,
  **measure actual H3 eligibility empirically**; H3 may be low-yield, but remains disabled unless
  validation demonstrates safe and worthwhile behavior. Gated on F2 landed + P3 reviewed + a
  validation run.
- **H2 (session deletion):** the `else: pass` at `:205–211` must NOT become a bare delete — its
  principled replacement is **F5 (demote-with-TTL)**. This is the band F2 actually repairs, so H2
  re-enable is the higher-value one, gated on F2 **and** F5.
- Both re-enables land as their own reviewed change with the disarm comments removed and replaced by
  a pointer to this plan + validation evidence. **Not** folded into the F2 commit.

---

## Verification gates

1. `count_similar_by_cosine` counts DISTINCT true-cosine neighbors above the floor, excludes self +
   Episodic, returns `-1` on cosine-index failure and on any exception (unit tests).
2. Graphiti-contract test proves cosine-only config, `sim_min_score` passed, `group_ids` preserved,
   no BM25 fallback (config-capture test).
3. `TestDuplicateUUIDSharpness` updated to assert deduped count (`2`) on the new path.
4. Regression fixture in the **0.2–0.5 corrupted band** flips to protective (not a `<0.1` fixture).
5. `compute_sharpness` unchanged; `memory_types.py` gate thresholds unchanged.
6. P3 probe committed + run; `.agent/reviews/f2-cosine-floor-probe.md` produced; chosen floor +
   winning histogram row recorded at the `SHARPNESS_COSINE_FLOOR` definition site.
7. Suite green: `.venv/Scripts/python.exe -m pytest tests/test_decay_logic.py tests/test_lifecycle_service.py tests/test_edge_cases.py tests/test_regression_state_machines.py -q -p no:cacheprovider`.
8. No re-enable of H2/H3 in this pass — disarm sites (`:205–211`, `:558`) left intact, now
   cross-referencing this plan.

## Risks / notes

- **Compression is already active** (`should_compress`, `:762`) and is NOT disarmed — once sharpness
  is lawful, compression eligibility shifts in-place on the next decay sweep. Intended correction,
  low-risk (F3 archive-first rehydrate makes compression reversible). Call out in the wrapup: the
  first post-F2 sweep may compress a different (correct) set.
- **Cosine floor is load-bearing.** Too low → nothing unique (over-compress); too high → everything
  unique (never trims). P3 is not optional polish. Strict `>` boundary factored into calibration.
- **Cosine-index availability.** Dimension mismatch → sharpness unavailable (`-1` → skip), same
  failure mode as today; no silent BM25 substitution.
- **Prod vs frontier parity (V5).** Land in both `menhir` and `menhir-frontier` checkouts if the
  sharpness path diverges; `diff -rq` the two `src/menhir` trees for this path before closing.
- **D2 (`apply_decay` has no prod invoker).** F2 makes decay lawful but does not wire it — decay
  invocation stays a separate decision (`memory-review-tracker.md` §5 D2).

## Cross-reference

- Parent: `plans/lifecycle-remediation.md` §F2 · Tracker: `.agent/memory-review-tracker.md` §4 (F2)
- Evidence: `.agent/archive/reviews/menhir-lifecycle-scale-probe-2026-07-03.md` — CORRECTION
  (2026-07-04) §"the sub-0.1 sharpness population has a different primary writer" (Defect A vs B)
- Scale law: `.agent/memory-lifecycle-under-uncertainty.md` §5 (signals must be scale-lawful)
- Partner fix: F5 demote-with-TTL (todo `0b86a37f`) — required with F2 for H2 re-enable
- Code anchors: `lifecycle_service.py:34,87,190,205,252,546,558,750` ·
  `graphiti_client.py:887,915,1043` · `core/bootstrap.py:44` · `memory_types.py:52,82` ·
  `tests/test_edge_cases.py:771` · `graphiti_core/search/search_config.py:91` ·
  `graphiti_core/driver/neo4j/operations/search_ops.py:158`
