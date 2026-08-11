# Lifecycle remediation — protective sharpness & archive-first rehydration

**Design authority:** @ctharvey | **Status:** CLOSED — COMPLETE (2026-07-10). F1+F3 DONE (07-04); F2 DONE+calibrated (`ba43ab9`/`02f88fe`); F5 DONE — H2 re-enabled (`dd1a398`); F6 DECIDED → Option B; D2 RESOLVED — decay sweep wired (`2b229be`); merge-eligibility guardrail landed (`bf7756c`); F4 CLOSED as won't-do (measure-only, no forward value). H3/GONE deletion stays disarmed by choice (its own future gated review). All impl plans archived to `.agent/archive/plans/`. This index is now archivable.

> **Wrap note (2026-07-10):** the acute lifecycle remediation is COMPLETE — protective sharpness (F1),
> lawful cosine sharpness (F2), archive-first rehydrate (F3), demote-with-TTL / H2 re-enable (F5), a
> forward merge-eligibility guardrail (structural + path-shaped nodes never merge), and the decay
> sweep now wired to run daily (D2) all landed on `main`. Both consolidation and decay lifecycle
> halves now execute on a deliberate cadence. Only **F4** remains (forensic measurement of historical
> merge damage — deprioritized). H3/GONE deletion stays disarmed by choice (its own gated review).

> **MVP 6a closed as ACCEPT + DOCUMENT (2026-07-10).** The acute bleed is stopped (H1 judge-gated
> merging; H2/H3 disarmed) and the protective fixes (F1, F3) landed. The ~2,679 historical merges are
> unrecoverable (no snapshot), the disarmed gates fail safe toward retention (correct at personal
> scale), and M1's launch benchmark uses a fresh Neo4j so launch evidence is unaffected by the degraded
> dev graph. F2 (lawful sharpness recomputation) and F5 (demote-with-TTL) — required to *re-enable*
> H2/H3 correctly — are explicitly **post-MVP**; until they land the gates stay disarmed. The degraded
> dev graph is an accepted, documented local-only risk. See `.agent/memory-review-tracker.md` §4.

## Why: core asymmetry enforcement

The memory system's irreversibility property (deletion ≠ repairable; retention is) mandates that unknown value defaults to protection. Two defects compound:

1. **Stamp-time default 0.0 (F1):** New ingested memories default to `sharpness = 0.0` (maximally forgettable) until a decay sweep recomputes. This inverts the asymmetry: unknown uniqueness becomes high-delete eligibility. Fix: null-protective gates (unknown never justifies decay) + remove the default coalesce.

2. **Compression is one-way lossy (F3):** Content compresses to a summary, and rehydration reconstructs from the summary alone, ratcheting detail away (photocopy-loss loop). The revision sidecar archives the original `old_value` on every compression; rehydration must read it instead of LLM-merging the summary with new context. Fix: archive-first recovery path + best-effort fallback to today's node content if archive unavailable.

See `.agent/memory-lifecycle-under-uncertainty.md` §2–4 for the asymmetry principle; `§3 proxies must fail safe` and `§4 compression keeps its receipt` are the operative clauses.

## Governance obligation

[Reversibility must be monotone in corroboration](../../memory-governance.md) — each rung of the ladder (ACTIVE → COMPRESSED → GONE) demands more evidence. F1 enforces this at the stamp boundary (unknown is not evidence); F3 closes the archive-path feedback loop so compression ceases to be irreversible in practice.

## Design summary

### F1: Protective sharpness default (remove 0.0 coalesce)

**Scope:** `episode_stamping.py:56,102` + `memory_types.py` gates + `consolidation_queries.py` Cypher

**Mechanism:**
- Delete the `n.sharpness = coalesce(toFloat(n.sharpness), 0.0)` SET clause from both stamped Entity and Episodic queries (lines 56, 102). A new memory makes no uniqueness claim; leave it null.
- In `memory_types.py` `should_compress` and `should_delete`: if `sharpness = node.get("sharpness"); sharpness is None: return False`. Missing sharpness is protective, not eligibility. TEMPORAL target-date paths are exempt.
- In `consolidation_queries.py` `fetch_decay_candidates`: 
  - **Compress phase** prefiltering may keep nulls (the service recomputes sharpness inline for ACTIVE candidates before deciding).
  - **Delete phase** Cypher must exclude nulls: `n.sharpness IS NOT NULL AND n.sharpness < ...` instead of `coalesce(n.sharpness, 0) < ...`.
- **Rationale:** Scale-lawful (see F2) and protective by design. The stamp is a trust point; null is epistemic honesty.

**Test impact:** Existing tests that construct nodes without explicit sharpness will fail the old default-0.0 assertions. Repair them by:
1. Either passing explicit `sharpness` in the mock node, or
2. Updating the test expectation to match new null-protective behavior (e.g., `should_compress(node)` returns False when sharpness is missing).

**Verification:** `test_decay_logic.py` + `test_lifecycle_service.py` + regression suite green; stamp queries confirmed to have no `coalesce(..., 0.0)` on sharpness in the SET clause.

### F2: Lawful sharpness recomputation (designed, NOT implemented this pass)

> **Implementation plan (2026-07-10):** approach chosen — Option 1 (true cosine via
> `NodeSearchConfig.sim_min_score`), feasibility confirmed. Implementation-ready spec:
> `plans/lifecycle-f2-lawful-sharpness-implementation.md`.

**Scope:** `lifecycle_service.py` `_count_similar_nodes` + `lifecycle_service.compute_sharpness` (staticmethod, `:87`)

**Problem:** Current sharpness uses RRF rank-sum (from `search_scored`), which is not cosine distance. A cosine-calibrated threshold applied to rank-sum is cross-scale mis-application (see `memory-lifecycle-under-uncertainty.md` §5 + probe CORRECTION). **This is the H2/H3 re-enable blocker.**

**Design options (chosen but not yet implemented):**
1. **True cosine path:** Compute cosine distance directly in consolidation + decay sweeps. Requires graphiti API change or local embedding.
2. **Threshold recalibration:** Pin the RRF scale empirically and re-tune consolidation/decay thresholds accordingly.
3. **Hybrid:** Use cosine where available (Episodic paths); RRF where it's the only option (Entity scale-free consolidation).

**Re-enable condition for H2/H3:** This must land before session deletion or decay GONE gate re-enable. F1 (protective default) makes them safe to disarm now; F2 makes them correct to re-enable.

**Not in scope for this pass:** Design decisions finalized; implementation deferred to post-F1 validation.

### F3: Rehydrate-from-archive (implement this pass)

**Scope:** `telemetry/store.py` + `lifecycle_service.py` `rehydrate_node` region only

**Mechanism:**
- Add `get_original_content(node_uuid: str) -> str | None` to `McpTelemetryStore`:
  - Query `memory_revisions` for the EARLIEST row where `node_uuid = ?` and `field = 'content'`.
  - Return the `old_value` from that row (the content before the first lossy rewrite).
  - Return None if no such row exists (archive miss).
  - Read-only, follows existing method style (no transaction complications).

- In `lifecycle_service.rehydrate_node` (line 663–665, `existing_content` region):
  - Before constructing `existing_content` from node, attempt:
    ```python
    archived_content = await asyncio.to_thread(telemetry_store.get_original_content, node_uuid)
    existing_content = (archived_content or str(node.get("content") or node.get("summary") or node.get("name") or "")).strip()
    ```
  - When archive has content, use it as LLM merge input (archive-first recovery). Record which source was used in the `record_lifecycle_action` details (or a debug log).
  - If archive raises, catch and fall back to today's behavior (try/except → None).

- **Rationale:** Compression stops being a one-way door. The revision sidecar already carries the full pre-compression content; rehydration now consults it. Summary-merge becomes the enhancement path (better context synthesis) rather than the recovery path (only option).

**Test impact:**
- Fake store returns archived original → `merge_content` called with archive text.
- Store returns None → today's byte-identical behavior.
- Store raises → today's behavior.
- New test: `test_rehydrate_from_archive_prefers_original` or similar.

**Verification:** Confirm archive-first recovery works end-to-end; test suite green.

### F4: One-off merge audit — CLOSED (won't-do, 2026-07-10)

**Scope:** `merged_from` receipt audit against episode MENTIONS.

**CLOSED as won't-do (@ctharvey, 2026-07-10).** F4 was forensic *measurement only* — it would
estimate the legit-dedup vs false-merge fraction of the ~2,910 historical absorptions. It does not
fix anything (no unmerge exists; the merges are unrecoverable). Decision: the value is in *stopping
recurrence*, which the merge-eligibility guardrail (structural + path-shaped nodes never merge, commit
`bf7756c`) now delivers — that guardrail alone would have blocked ~51% of the historical damage, and
H1 judge-gating covers the semantic remainder. A one-off backward-looking number adds no forward value,
so F4 is closed unexecuted. A read-only probe (`merged_from` vs MENTIONS) remains trivially runnable
from `git log` if a number is ever wanted; the receipts are preserved (never cleaned).

### F5: Consolidation middle rung — demote-with-TTL (designed, NOT implemented)

**Scope:** Session consolidation policy (replaces promote-or-delete).

**Problem:** Current H2 hotfix keeps low-sharpness SESSION nodes in SESSION scope (do no harm). But permanently unkept SESSION nodes dilute recall. Better: **demote-with-TTL** — promote if strong signals (edges, uniqueness), but mark demoted nodes with a time-to-live instead of deleting. They sink back to SESSION (or orphan) after TTL expires, reachable via staleness-based orphan cleanup.

**Design:** `demote-with-ttl_days` policy field (default 7–14 days) + consolidation route logic:
- Unique/connected → promote (today).
- Below threshold + flagged → promote (override).
- Below threshold + unflagged → **set ttl_expires = now + demote_ttl_days, keep scope=SESSION** (new).
- Cleanup orphans: if `ttl_expires < now and scope = SESSION`, delete (no loss: already unkept).

**Re-enable condition for H2:** With F5 in place, the "delete when low sharpness" branch becomes a deliberate demote-with-TTL, not a scale-artifact casualty.

**Not in scope for this pass:** Design sketched, implementation deferred to post-F1/F3 validation.

### F6: Compression severity — decide whether to pause until F3 lands

**Scope:** Runtime compression decision (≤200 char summary target).

**Current state:** Compression is active in both checkouts. The ≤200 char constraint plus the current summary-only rehydration means that repeated compress/rehydrate cycles can lose detail. F3 (archive recovery) makes rehydration lossless in practice (retrieves the original, merges with new context).

**DECISION (2026-07-10, @ctharvey): Option B — keep compression active.**
- **Option A:** Pause compression until F3 proves out in testing (conservative, maximizes retention; data cost is minimal for personal scale; one feature gate).
- **Option B (CHOSEN):** Keep compression active; F3 is the recovery path. F3 (rehydrate-from-archive) has landed, so compress/rehydrate is lossless in practice — rehydration reads the pre-compression original from the revision sidecar before any summary merge. The archive sidecar is in place; no pause needed.

---

## Explicitly not in scope

- H2/H3 re-enable gate logic (blocked on F2, not F1).
- Bench harness telemetry isolation (Q2, separate plan).
- Lawful scale recalibration or true cosine (F2 design phase only).
- Session demote-with-TTL mechanics (F5 design phase only).
- Merge audit or merged_from cleanup (F4, gated on H1 landing).
- Compression pause/resume gating (F6, awaiting decision).

---

## Parts

| Item | Design | Implement | Verify | Status |
|------|--------|-----------|--------|--------|
| F1: Protective default | ✓ | THIS PASS | `test_decay_logic` + `test_lifecycle_service` | IN PROGRESS |
| F2: Lawful sharpness | ✓ | DONE (`ba43ab9`, calibrated `02f88fe`) | `test_edge_cases` + live probe | IMPLEMENTED (impl plan archived) |
| F3: Archive-first rehydrate | ✓ | THIS PASS | `test_lifecycle_service` archive cases | IN PROGRESS |
| F4: Merge audit | — | GATED | H1 landing + receipts data | QUEUED |
| F5: Demote-with-TTL | ✓ | DONE (`dd1a398`) | `test_regression_state_machines` + `test_lifecycle_consolidation_job` | IMPLEMENTED — re-enables H2 (impl plan archived) |
| F6: Compression severity | — | AWAITING DECISION | Decision rationale log | DECIDE |

---

## Verification gates

1. **F1 query assertions:** Confirm `episode_stamping.py` SET clauses have no `coalesce(toFloat(n.sharpness), 0.0)`.
2. **F1 gate logic:** `should_compress` and `should_delete` return False when `node.sharpness is None`.
3. **F3 archive read:** `telemetry_store.get_original_content(uuid)` returns the EARLIEST content value or None.
4. **F3 rehydrate:** `rehydrate_node` prefers archived content; records source in details/log.
5. **Test suite green:** `.venv/Scripts/python.exe -m pytest tests/test_decay_logic.py tests/test_lifecycle_service.py tests/test_regression_state_machines.py -q -p no:cacheprovider`
6. **No commits:** Changes staged but not committed; tracked in WRAPUP.

---

## Cross-reference

- **Governance:** [Reversibility obligation](../../memory-governance.md#reversibility-must-be-monotone-in-corroboration)
- **Scale law:** [memory-lifecycle-under-uncertainty.md §5](../../memory-lifecycle-under-uncertainty.md) — signals must be scale-lawful
- **Asymmetry:** [memory-lifecycle-under-uncertainty.md §2](../../memory-lifecycle-under-uncertainty.md) — irreversibility principle
- **Archive receipt:** [memory-lifecycle-under-uncertainty.md §4](../../memory-lifecycle-under-uncertainty.md) — compression keeps its receipt
- **Hotfix context:** [menhir-lifecycle-scale-probe-2026-07-03.md](../reviews/menhir-lifecycle-scale-probe-2026-07-03.md) CORRECTION section — stamp-default finding
- **Tracker:** [memory-review-tracker.md §4](../../memory-review-tracker.md) — F1–F6 definitions and dependencies
