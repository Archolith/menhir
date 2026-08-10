# Lifecycle F5 — consolidation middle rung: demote-with-TTL (implementation plan)

**Design authority:** @ctharvey | **Status:** IMPLEMENTED 07-10 (P1-P7, 171 tests green) — re-enables H2 | **Parent:** `plans/lifecycle-remediation.md` §F5 | **Todo:** `0b86a37f`

> **Implementation note (07-10):** built by an Opus subagent, reviewed + corrected by Claude. Two
> review fixes applied post-agent: (1) the TTL-expiry delete was running BEFORE promotion (would
> delete a now-promotable node before rescue) — moved to run AFTER promotion so promotion wins;
> (2) audit used the generic `record_mcp_event` — switched to `record_lifecycle_action` (the canonical
> deletion audit trail forensics query). Added the missing P7 scheduler test. H3 untouched.

**What this is:** F5 replaces the H2 hotfix's `else: pass` (unpromoted SESSION nodes linger forever)
with a **deliberate middle rung** — a demoted node gets a time-to-live; if nothing promotes it within
the grace window, it is deleted as unkept. This **re-enables H2** as a principled demote-with-TTL, not
a scale-artifact delete. **Prerequisite met:** F2 (lawful sharpness) landed + calibrated (`ba43ab9`,
`02f88fe`), so the "low sharpness" that routes a node into the demote branch is now a real cosine
uniqueness measure, not an RRF artifact.

> **Scope boundary:** F5 re-enables **H2 only** (SESSION consolidation delete). It does **not** touch
> **H3** (`should_delete` → `False`, decay GONE on PERSISTENT nodes), which stays disarmed and is
> low-yield per the corrected causality. The bare sharpness-threshold delete is **not** restored — the
> only path to deletion here is TTL expiry after a full grace window with no corroboration.

---

## Problem (grounded)

`_run_consolidation` (`lifecycle_service.py:176-221`) routes each SESSION Entity node:
1. `user_flagged` → promote (`:182`)
2. `count_persistent_edges >= PERSISTENT_EDGE_PROMOTE_THRESHOLD` → promote candidate (`:190`)
3. `sharpness >= SHARPNESS_PROMOTE_THRESHOLD` → promote candidate (`:210`)
4. **else → `pass`** (`:212-218`, the H2 disarm) — node stays SESSION forever.

`delete_uuids` is never populated, so `delete_session_nodes([])` (`:241`) is a no-op. The unpromoted
SESSION nodes accumulate: default recall excludes SESSION, so they are invisible but they dilute the
graph and never resolve. The design intent (§F5) is a recoverable middle rung: promote on any positive
signal, otherwise start a TTL; delete only when the TTL expires with the node still unkept.

No memory-node TTL field exists today (all `ttl`/`ttl_expires` in the tree are OAuth/scheduler).

## Design decision (locked)

- New Entity node property **`ttl_expires`** (a Neo4j `datetime`). Absent = not demoted.
- **`DEMOTE_TTL_DAYS = 14`** — a single global constant, retention-favoring (upper end of the §F5
  7-14 range). Deletion is irreversible; the wider window is the conservative default. (Future: may
  move to a per-type `MemoryTypePolicy` field; out of scope now.)
- **All time math is DB-side** (`datetime()`, `duration({days:$days})`) to avoid Python/Neo4j clock
  skew — mirrors how `promote_to_persistent` sets `promoted_at = datetime()` (`consolidation_queries.py:349`).
- **TTL does not reset on mere re-access.** Set-once via `coalesce` (`SET n.ttl_expires =
  coalesce(n.ttl_expires, datetime() + duration(...))`). A node nobody promotes in 14 days is unkept
  even if recall happened to touch it; only a *promotion signal* rescues it (and clears the TTL).
- **Promotion clears the TTL** (`n.ttl_expires = null` in `promote_to_persistent`) — a rescued node is
  no longer demoted.

Reversibility justification (governance: reversibility monotone in corroboration): a node is deleted
only after a full 14-day window in which it accrued **no** corroboration — unflagged, below the
persistent-edge threshold, and low *lawful-cosine* uniqueness (post-F2). SESSION scope is excluded
from default recall, so the pre-deletion state is already low-cost. This is the lawful terminal rung,
not a scale casualty.

---

## Parts

| Item | What | File(s) | Status |
|------|------|---------|--------|
| P1 | `ttl_expires` field + queries (set/clear/fetch-expired) | `consolidation_queries.py`, `memory_graph_adapter.py` | THIS PASS |
| P2 | Routing: replace `else: pass` with demote; add TTL-expiry delete phase | `lifecycle_service.py` | THIS PASS |
| P3 | Telemetry: `ConsolidationResult.demoted` + lifecycle-action records | `lifecycle_service.py` | THIS PASS |
| P4 | Constant `DEMOTE_TTL_DAYS` | `lifecycle_service.py` | THIS PASS |
| P5 | Tests | `tests/` | THIS PASS |
| P6 | Docs: H2 re-enable note; replace disarm comment | tracker, plan | THIS PASS |
| P7 | Scheduled daily lifecycle-consolidation job (resolves D3) | `maintenance_scheduler.py` | THIS PASS |

### P1 — field + queries (`consolidation_queries.py`, adapter passthrough)

- **`fetch_session_entities`** (`:286`): add `n.ttl_expires AS ttl_expires` to the RETURN so routing
  sees the current TTL. (Kept for observability; the expiry *decision* is a separate DB-side query so
  we never compare times in Python.)
- **`promote_to_persistent`** (`:339`): add `"n.ttl_expires = null"` to the SET clause — promotion
  clears any pending demotion.
- **New `set_demote_ttl(node_uuids: list[str], ttl_days: int) -> int`**: `MATCH (n:Entity) WHERE
  n.uuid IN $uuids AND n.scope='SESSION' SET n.ttl_expires = coalesce(n.ttl_expires, datetime() +
  duration({days:$days})) RETURN count(...)`. `coalesce` = set-once (no reset). Returns the number of
  nodes that had NO prior TTL (i.e., *newly* demoted) — count via a companion `WHERE n.ttl_expires IS
  NULL` pre-count, or return both totals; the routing needs the newly-demoted count for telemetry.
- **New `fetch_ttl_expired_session_uuids(session_id: str | None) -> list[dict]`**: `MATCH (n:Entity)
  WHERE n.scope='SESSION' AND n.ttl_expires IS NOT NULL AND n.ttl_expires < datetime() [AND
  n.session_id=$session_id] RETURN n.uuid AS uuid, n.name AS name, n.session_id AS session_id`. This is
  the expiry set to delete (returns names/session for audit records before deletion).
- **Reuse `delete_session_nodes`** (`:355`, DETACH DELETE, scope-guarded) for the actual deletion —
  no new delete path.
- Expose all new methods through `MemoryGraphAdapter` (mirror the existing
  `fetch_session_entities`/`delete_session_nodes` passthroughs at `memory_graph_adapter.py:498,511`).

### P2 — routing (`lifecycle_service._run_consolidation`)

Replace the H2 disarm `else: pass` (`:212-218`) with demote:

```python
else:
    # F5 demote-with-TTL: low lawful-cosine sharpness, unflagged, below edge threshold.
    # Start (do not reset) a grace TTL; the node stays SESSION until it expires.
    demote_uuids.append(uuid)
```

Collect `demote_uuids`, then in Phase 4 (transitions) call `set_demote_ttl(demote_uuids,
DEMOTE_TTL_DAYS)`; `demoted = <newly-demoted count>`.

Add a **TTL-expiry delete phase** (new, runs each consolidation pass, respecting the pass's
`session_id` filter — so `recover_orphans` (session_id=None) sweeps globally). Note: consolidation is
**restart-only today** (`recover_orphans` runs at `runtime.py:262` init; it is NOT on the scheduler),
so the sweep needs a real cadence — see **P7**, which puts consolidation on the maintenance scheduler:

```python
expired = await asyncio.to_thread(self.graph_adapter.fetch_ttl_expired_session_uuids, session_id)
for row in expired:
    record_lifecycle_action(action="delete", node_uuid=row["uuid"],
                            session_id=row.get("session_id"), trigger="demote_ttl_expiry",
                            before_freshness=None, after_freshness=None)
deleted = await asyncio.to_thread(self.graph_adapter.delete_session_nodes,
                                  [r["uuid"] for r in expired])
```

Ordering: promotion checks (flagged/edges/sharpness) run first and win — a node that now qualifies is
promoted and its TTL cleared, regardless of a pending expiry. The expiry phase only deletes nodes that
routed to demote AND whose window already lapsed.

### P3 — telemetry

- `ConsolidationResult` (`:59`): add trailing field `demoted: int = 0` (default keeps the five
  existing positional constructors at `:110,:167` valid). Populate it in the returned result and log
  it in the `Consolidation complete` line.
- Audit records: `record_lifecycle_action(action="demote", trigger="consolidation_demote", ...)` for
  each newly-demoted uuid, and `action="delete", trigger="demote_ttl_expiry"` for each expiry
  deletion (shown above). Deletions must be recorded **before** the DETACH DELETE (the record is the
  only surviving evidence — governance chain-of-custody).

### P4 — constant

`DEMOTE_TTL_DAYS = 14` near the lifecycle thresholds block (`lifecycle_service.py:34`), with a comment
citing this plan and the retention-favoring rationale.

### P5 — tests

- **Demote:** low-sharpness, unflagged, below-edge SESSION node → NOT promoted, NOT deleted this pass,
  `set_demote_ttl` called with its uuid; `result.demoted == 1`.
- **No reset:** a node that already has `ttl_expires` and routes to demote again → `set_demote_ttl`
  uses `coalesce`, TTL unchanged (assert the query is coalesce-guarded / stub records no overwrite).
- **Expiry delete:** a SESSION node whose `ttl_expires < now` → appears in
  `fetch_ttl_expired_session_uuids` → deleted; a `record_lifecycle_action(trigger="demote_ttl_expiry")`
  emitted before deletion; `result.deleted == 1`.
- **Rescue clears TTL:** a demoted node that gains edges / flag / high sharpness → promoted, and
  `promote_to_persistent` SET includes `n.ttl_expires = null`.
- **Update the H2 regression test** `tests/test_regression_state_machines.py::TestSessionLowValueDelete.
  test_low_value_retained_not_deleted`: today it asserts a low-value node is *retained* (H2 disarm).
  Under F5 it is retained **this pass** (still not deleted) but now demoted — keep the not-deleted
  assertion and add that a TTL was set. Add a sibling test for the expiry-delete pass. This is an
  intentional behavior change (H2 re-enable), documented; do not contort the impl to keep the old
  meaning.
- Stub `StubGraphitiClient`/`stub_memory_graph_adapter` gain `set_demote_ttl` /
  `fetch_ttl_expired_session_uuids` support (record calls + programmable return), mirroring the F2
  `count_similar_by_cosine` stub pattern in `tests/conftest.py`.

### P6 — docs

- Tracker §4 F5 → `[IMPLEMENTED]`; §1 H2 row re-enable-condition satisfied (F2+F5). Replace the
  `else: pass` HOTFIX-2026-07-03 comment with a one-line pointer to this plan.
- Parent `lifecycle-remediation.md` §F5 + Parts table row updated.

### P7 — scheduled daily lifecycle consolidation (resolves D3)

Without this, F5 (and SESSION→PERSISTENT promotion) only fires at process restart
(`runtime.py:262`), so a rarely-restarted menhir never demotes/expires/promotes on schedule. Put
lifecycle consolidation on the maintenance scheduler, matching the existing job pattern
(`maintenance_scheduler.py`):

- **Config fields** (dataclass, near `:78`): `lifecycle_consolidation_interval_s: float = 86400.0`
  (daily) and `lifecycle_consolidation_enabled: bool = True`.
- **Register** in `__post_init__` (`:105-121`), gated like the others:
  ```python
  if self.lifecycle_service is not None and self.lifecycle_consolidation_enabled:
      self._jobs["consolidate_lifecycle"] = _JobState(interval_s=self.lifecycle_consolidation_interval_s)
  ```
  (`lifecycle_service` is already a scheduler field, `:58`.)
- **Dispatch** in the tick loop (`:331-344` elif chain):
  `elif name == "consolidate_lifecycle": await self._run_job(job, "scheduler_consolidate_lifecycle", self._make_consolidate_lifecycle())`.
- **Runner** `_make_consolidate_lifecycle` (near `:447`): returns
  `self.lifecycle_service.recover_orphans()` — consolidates aged SESSION nodes (promote / demote-set /
  TTL-expiry) on the daily tick.
- **Protocol**: ensure `SchedulerLifecycleService` (`scheduler_protocols.py`) exposes `recover_orphans`;
  extend it if not. Confirm `runtime.py` passes the real `lifecycle_service` into the maintenance
  scheduler (it holds the field; verify it is wired at construction).
- **Interval rationale**: daily is ample for a 14-day TTL and keeps promotion latency ≤ ~1 day. Do NOT
  recommend a daily *restart* as the mechanism — restarts churn leases and cold caches; the scheduler
  is the deliberate cadence (closes D3 for consolidation; `apply_decay`/D2 remains separate).

---

## Verification gates

1. Routing: unflagged/low-sharpness/below-edge → demote (TTL set, not deleted); promote signals still
   win and clear TTL; expired TTL → deleted with an audit record emitted first.
2. `set_demote_ttl` is set-once (coalesce) — re-demote does not extend the window.
3. All TTL time math is DB-side (`datetime()`/`duration`), no Python time comparison.
4. `ConsolidationResult.demoted` populated; deletions recorded before DETACH DELETE.
5. H3 untouched (`should_delete` still `return False`); no bare sharpness-delete reintroduced.
6. P7: a `consolidate_lifecycle` job is registered (when `lifecycle_service` present + enabled), runs
   `recover_orphans` on the daily tick, and is covered by a scheduler test (job registered + dispatch
   invokes the runner). Confirm the maintenance scheduler is constructed with the real
   `lifecycle_service`.
7. Suite green: `.venv/Scripts/python.exe -m pytest tests/test_lifecycle_service.py tests/test_regression_state_machines.py tests/test_decay_logic.py tests/test_edge_cases.py -q -p no:cacheprovider` (plus any `tests/test_maintenance_scheduler*` for P7).

## Risks / notes

- **This re-enables H2** (a real deletion path). Mitigations: 14-day grace, set-once TTL, promotion
  rescues + clears, deletion only for uncorroborated unkept SESSION nodes, audit record before delete.
  Choosing to build F5 is the H2 re-enable sign-off (parent plan P5).
- **Cadence (D3) — the load-bearing dependency.** Lifecycle consolidation is restart-only today;
  **P7** puts it on the scheduler (daily), which is what makes F5 actually fire. Without P7, F5 is
  latent until the next restart. P7 closes D3 for consolidation (`apply_decay`/D2 stays separate).
- **Backfill.** Existing lingering SESSION nodes have no `ttl_expires`; they get one on their next
  consolidation pass and delete 14 days later — a gradual, safe drain, not a mass delete.
- **V5 frontier parity.** Port to `menhir-frontier` if the consolidation path diverges; `diff -rq` the
  two `src/menhir` trees for this path before closing.

## Cross-reference

- Parent: `plans/lifecycle-remediation.md` §F5 · Tracker: `.agent/memory-review-tracker.md` §4 (F5), §1 (H2)
- Prerequisite: F2 lawful sharpness (`plans/lifecycle-f2-lawful-sharpness-implementation.md`, landed + calibrated)
- Governance: reversibility monotone in corroboration (`.agent/memory-governance.md`)
- Code anchors: `lifecycle_service.py:34,59,176-221,232,241,533` ·
  `consolidation_queries.py:286,339,355` · `memory_graph_adapter.py:498,511` ·
  `telemetry/recorders.py:192` (record_lifecycle_action) · `domain/models.py:21` (NodeScope)
