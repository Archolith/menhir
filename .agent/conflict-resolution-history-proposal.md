# Proposal: Conflict Resolution History

## Review notes (`2026-03-22`)

Two rounds of review. All findings addressed below.

### Pair recording rule

Only record suppression rows for pairs that were actually reviewed. Never infer pair coverage from group membership.

| Resolution path | Who reviews | What gets recorded |
|---|---|---|
| `confirm_pending_conflicts` (LLM) | LLM evaluates `members[0]` vs `members[1]` | That single pair |
| Manual `replace` / `discard_new` | User picks `keep_uuid` + `remove_uuid` | `(keep_uuid, remove_uuid)` |
| Manual `keep_both` | User reviews the 2-member group presented by the MCP tool | `(members[0], members[1])` from prefetched member list |
| `auto_resolve_stale_conflicts` | Nobody — blanket age-based resolution | All `n*(n-1)/2` pairs (justified: if nobody reviewed it in 14 days, suppress the whole group) |

**Manual `keep_both` contract:** The MCP tool presents a conflict group (almost always 2 members). The user decides to keep both. The system records that specific pair from the prefetched member list. For N-member groups (rare, only from transitive merges), only record `(members[0], members[1])` — the same pair shown to the user. Unrecorded pairs in the group re-enter the scan pool and get reviewed individually. No API change needed.

Unreviewed pairs in merged N-way groups are NOT suppressed. They get re-detected on the next scan, reviewed properly, then suppressed. This is correct.

```python
# In confirm_pending_conflicts, after LLM clears as false_positive:
reviewed_pair = (members[0]["uuid"], members[1]["uuid"])
self.telemetry_store.record_conflict_resolution(
    uuid_a=reviewed_pair[0], uuid_b=reviewed_pair[1],
    status="false_positive", group_id=group_id,
    action="keep_both", reviewed_by="llm",
)

# In auto_resolve_stale_conflicts, blanket resolution:
for a, b in itertools.combinations([m["uuid"] for m in members], 2):
    self.telemetry_store.record_conflict_resolution(
        uuid_a=a, uuid_b=b, status="auto-resolved",
        group_id=group_id, action="keep_both", reviewed_by="auto",
    )
```

### Suppression state, not audit history

This feature is **pair suppression state only**. One row per pair, latest outcome wins. `INSERT OR REPLACE` on `UNIQUE (uuid_a, uuid_b)`. Re-resolving the same pair overwrites the row.

This is not a full audit trail. `lifecycle_events` captures general event logs but doesn't have pair identity or reviewer identity. If pair-level audit is ever needed, drop the unique constraint and add a `SELECT ... ORDER BY resolved_at DESC LIMIT 1` query for suppression. That's a future migration.

### Member prefetch for `keep_both`

`resolve_conflict_group` currently fetches members only in the `replace`/`discard_new` path (line 606–612). Move that fetch above the action dispatch so all paths have the member list. Return `member_uuids` in the result dict.

```python
# Before any action dispatch — fetch member UUIDs
members = self.neo4j.execute(
    "MATCH (n:Entity) WHERE n.conflict_group_id = $group_id "
    "RETURN n.uuid AS uuid",
    params={"group_id": conflict_group_id},
)
member_uuids = [str(r["uuid"]) for r in members]

# ... existing action dispatch (keep_both / replace / discard_new) ...

result["member_uuids"] = member_uuids
```

### Telemetry store dependency path

**Use the module-level singleton.** `telemetry_store` is a global instance at the bottom of `infrastructure/telemetry/store.py`, already imported as `from menhir.mcp.telemetry import telemetry_store` throughout the codebase (MCP tools, explorer, tracker). This is the established pattern.

`LifecycleService` imports and uses the singleton directly — no constructor injection, no new wiring in `core/bootstrap.py`. Same pattern as every other telemetry consumer in the project.

```python
# services/lifecycle_service.py
from menhir.infrastructure.telemetry.store import telemetry_store

# In _check_contradictions_batch:
if telemetry_store.is_pair_resolved(uuid, other_uuid, cooldown_days=self.settings.conflict_cooldown_days):
    continue
```

For `ConsolidationRepository`: no change. SQLite writes happen in the service layer after the Cypher completes.

### Cooldown threading

`conflict_cooldown_days` gets added to `MemorySettings` (in `config/settings.py`). `LifecycleService` already receives `settings` — wait, it doesn't. Looking at `core/bootstrap.py`:

```python
lifecycle_service = LifecycleService(
    graph_adapter=graph_adapter,
    graphiti_client=graphiti_client,
    llm=llm,
)
```

**Fix: add `settings` to `LifecycleService.__init__`.** One new param. The cooldown value flows through as `self.settings.conflict_cooldown_days` at the scan-time `is_pair_resolved` call site.

```python
# core/bootstrap.py
lifecycle_service = LifecycleService(
    graph_adapter=graph_adapter,
    graphiti_client=graphiti_client,
    llm=llm,
    settings=settings,  # new
)
```

## Cursor-based scan pagination

The current `scan_for_conflicts` has no pagination. It loads up to `limit` candidates, but there's no way to continue where you left off. At `limit=50` it works, but you can't iterate through all memories without re-scanning the same nodes (or timing out at higher limits).

**Fix: add a `cursor` param (UUID-based).**

### Service layer — `lifecycle_service.py`

```python
async def scan_for_conflicts(
    self, *, limit: int = 500, cursor: str | None = None,
) -> dict[str, Any]:
```

Cypher changes:

```python
query = (
    Cypher()
    .match("(n:Entity)")
    .where(
        "n.scope = 'PERSISTENT'",
        "n.freshness <> 'GONE'",
        "n.conflict_group_id IS NULL",
    )
    .where_if(cursor is not None, "n.uuid > $cursor")
    .return_raw("n.uuid AS uuid, n.name AS name, coalesce(n.summary, n.content, '') AS content")
    .order_by("n.uuid")
    .limit()
    .build()
)
params = {"limit": max(1, min(limit, 2000))}
if cursor is not None:
    params["cursor"] = cursor
```

Return value adds `next_cursor`:

```python
next_cursor = candidates[-1]["uuid"] if candidates else None
return {
    "scanned": len(candidates),
    "new_conflicts": new_conflicts,
    "next_cursor": next_cursor,
    "done": len(candidates) < limit,
}
```

`done: true` means there are no more nodes to scan. The caller can stop.

### MCP tool — `scan_conflicts.py`

Add `cursor: str = ""` param:

```python
async def scan_for_conflicts(limit: int = 500, cursor: str = "") -> str:
    """...
    Args:
        limit: Max nodes to scan per batch (default 500).
        cursor: Resume token from previous scan. Pass the `next_cursor` value
            from the last result to continue where you left off. Empty string
            starts from the beginning.
    """
```

Pass through: `lifecycle.scan_for_conflicts(limit=limit, cursor=cursor or None)`

### Usage

```
# First batch
scan_for_conflicts(limit=50)
→ {"scanned": 50, "new_conflicts": 3, "next_cursor": "abc-123", "done": false}

# Continue
scan_for_conflicts(limit=50, cursor="abc-123")
→ {"scanned": 50, "new_conflicts": 1, "next_cursor": "def-456", "done": false}

# Last batch
scan_for_conflicts(limit=50, cursor="def-456")
→ {"scanned": 12, "new_conflicts": 0, "next_cursor": "ghi-789", "done": true}
```

### Files changed (additional)

| File | Change | Lines |
|------|--------|-------|
| `services/lifecycle_service.py` | Add `cursor` param, `ORDER BY n.uuid`, `WHERE n.uuid > $cursor`, return `next_cursor` + `done` | +8 |
| `mcp/tools/conflict/scan_conflicts.py` | Add `cursor` param, pass through | +5 |
| `tests/test_conflict_history.py` | Cursor pagination tests: first batch, continuation, done flag, empty cursor | +20 |

### Test plan (additional)

| Test | What |
|------|------|
| `test_scan_cursor_returns_next` | First scan returns `next_cursor` matching last candidate UUID |
| `test_scan_cursor_continues` | Second scan with cursor skips already-scanned nodes |
| `test_scan_cursor_done_flag` | `done=true` when fewer candidates than limit |
| `test_scan_cursor_none_starts_fresh` | `cursor=None` starts from beginning |

---

## Problem

When `scan_for_conflicts` detects a high-similarity pair and LLM review clears it as `false_positive`, the resolution doesn't stick. `resolve_conflict_group` sets `conflict_group_id = null` on both nodes, which puts them right back into the scan pool (`WHERE conflict_group_id IS NULL`). Next scan finds the same similarity match, creates a new group, LLM clears it again. Loop forever.

## Why not just keep the group_id?

Tempting, but it breaks down:

- A node can only be in one group. If A resolves with B and later genuinely conflicts with C, A is stuck in the old group. The merge logic would try to fold C into a resolved group — mixed state with no clean semantics.
- `conflict_status` is per-node, not per-pair. You can't represent "resolved with B, unresolved with C" on the same node.
- Overloading one field for both "active conflict tracking" and "historical audit" gets worse as the system grows (N-way conflicts, re-detection after content changes, etc.).

## Design: SQLite resolution history

Separate concerns. Keep `conflict_group_id` + `conflict_status` for active conflicts (current behavior, null after resolution). Add a `conflict_resolutions` table in the SQLite sidecar that records settled pairs.

### New table

```sql
CREATE TABLE IF NOT EXISTS conflict_resolutions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    resolved_at     TEXT NOT NULL,
    uuid_a          TEXT NOT NULL,   -- sorted: smaller uuid first
    uuid_b          TEXT NOT NULL,
    status          TEXT NOT NULL,   -- false_positive, resolved, auto-resolved
    group_id        TEXT NOT NULL,   -- original conflict group for traceability
    action          TEXT NOT NULL,   -- keep_both, replace, discard_new
    reviewed_by     TEXT NOT NULL    -- llm, user, auto
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_conflict_resolutions_pair
ON conflict_resolutions (uuid_a, uuid_b);

CREATE INDEX IF NOT EXISTS idx_conflict_resolutions_resolved
ON conflict_resolutions (resolved_at);
```

The `UNIQUE` index on `(uuid_a, uuid_b)` means a pair can only have one active resolution. If you want to re-review a pair, you delete or update the row.

UUIDs are sorted at insert time (`uuid_a < uuid_b`) so the lookup is order-independent.

### Where it plugs in

**1. Recording resolutions — `lifecycle_service.py`**

After `resolve_conflict_group` returns, the caller writes suppression rows for the reviewed pairs only (see pair recording rule above). Uses the module-level `telemetry_store` singleton.

New method on `McpTelemetryStore`:

```python
def record_conflict_resolution(
    self,
    *,
    uuid_a: str,
    uuid_b: str,
    status: str,
    group_id: str,
    action: str,
    reviewed_by: str,
) -> None:
    pair = tuple(sorted([uuid_a, uuid_b]))
    self._ensure_ready()
    with sqlite3.connect(self.db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO conflict_resolutions
                (resolved_at, uuid_a, uuid_b, status, group_id, action, reviewed_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_utc_now_iso(), pair[0], pair[1], status, group_id, action, reviewed_by),
        )
```

`INSERT OR REPLACE` means re-resolving the same pair just updates the row.

**2. Checking history during scan — `lifecycle_service.py`**

`_check_contradictions_batch` currently calls `set_conflict` immediately when it finds a high-similarity match. Add a check before that: has this pair already been resolved?

New method on `McpTelemetryStore`:

```python
def is_pair_resolved(self, uuid_a: str, uuid_b: str) -> bool:
    pair = tuple(sorted([uuid_a, uuid_b]))
    self._ensure_ready()
    with sqlite3.connect(self.db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM conflict_resolutions WHERE uuid_a = ? AND uuid_b = ?",
            (pair[0], pair[1]),
        ).fetchone()
    return row is not None
```

In `_check_contradictions_batch`, after the similarity check passes and before calling `set_conflict`:

```python
if score < SIMILARITY_CONFLICT_THRESHOLD:
    continue

# skip pairs that were already reviewed
if self.telemetry_store.is_pair_resolved(uuid, other_uuid):
    continue

new_group_id = str(uuid4())
# ... existing set_conflict call
```

**3. Temporal cooldown (optional, config-driven)**

If content changes, a previously-cleared pair might genuinely conflict now. Add an optional `cooldown_days` param (default: 0 = permanent suppression):

```python
def is_pair_resolved(self, uuid_a: str, uuid_b: str, *, cooldown_days: int = 0) -> bool:
    pair = tuple(sorted([uuid_a, uuid_b]))
    self._ensure_ready()
    with sqlite3.connect(self.db_path) as conn:
        if cooldown_days > 0:
            row = conn.execute(
                """
                SELECT 1 FROM conflict_resolutions
                WHERE uuid_a = ? AND uuid_b = ?
                  AND resolved_at > datetime('now', ?)
                """,
                (pair[0], pair[1], f"-{cooldown_days} days"),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM conflict_resolutions WHERE uuid_a = ? AND uuid_b = ?",
                (pair[0], pair[1]),
            ).fetchone()
    return row is not None
```

Default 0 means "once cleared, never re-detect." Set to 30 or 90 if you want periodic re-checks after content may have drifted.

**4. Member prefetch in `resolve_conflict_group`**

Before the action dispatch, fetch member UUIDs from the group. Return them in the result dict so the caller knows which nodes were in the group. The Cypher mutations stay identical — `conflict_group_id` still gets nulled, status still gets set. The only structural change is that member lookup moves above the `if action == "keep_both"` branch instead of only happening in `replace`/`discard_new`.

**5. Recording after resolution — `lifecycle_service.py`**

After `resolve_conflict_group` returns, the caller writes suppression rows for the reviewed pairs only (not all member combinations). Which pairs were reviewed depends on the resolution path — see the pair recording rule in the review notes. The repository layer stays pure Neo4j.

## Files changed

| File | Change | Lines |
|------|--------|-------|
| `infrastructure/telemetry/store.py` | Add `conflict_resolutions` table schema, `record_conflict_resolution()`, `is_pair_resolved()` | +45 |
| `infrastructure/consolidation_queries.py` | Prefetch member UUIDs before action dispatch, return `member_uuids` in result dict | +10 |
| `services/lifecycle_service.py` | Add `settings` param, import `telemetry_store` singleton, record reviewed pairs after resolve, check `is_pair_resolved` in `_check_contradictions_batch`, cursor pagination | +33 |
| `mcp/tools/conflict/scan_conflicts.py` | Add `cursor` param, pass through | +5 |
| `core/bootstrap.py` | Pass `settings` to `LifecycleService` constructor | +1 |
| `config/settings.py` | Add `conflict_cooldown_days` setting (default: 0) | +3 |
| `tests/test_conflict_history.py` | New: pair recording, lookup, cooldown, sorted uuid, re-resolution, scan skip, member prefetch, reviewed-pair-only recording, cursor pagination | +90 |

**Total:** ~190 lines

## What this doesn't change

- Active conflict flow (detect → pending_llm_review → confirm/clear → resolve) — untouched
- `conflict_group_id` / `conflict_status` semantics on Entity nodes — untouched
- `resolve_conflict_group` Cypher mutations — untouched (only adds a member prefetch before the existing dispatch)
- `list_conflict_groups`, `requeue_conflicts_for_llm_review` — untouched
- Scoring (`has_conflict` flag in recall) — untouched
- Scheduler jobs — untouched

## What this fixes

- Same pair never gets re-flagged after LLM clears it (or user resolves it)
- Nodes stay free to participate in NEW conflict detection with other nodes
- N-way groups are safe: only reviewed pairs get suppression rows, unreviewed pairs re-enter the scan pool
- Pair-level suppression state in SQLite
- Optional temporal cooldown for long-lived systems where content drifts

## Test plan

| Test | What |
|------|------|
| `test_record_and_lookup` | Write a resolution, verify `is_pair_resolved` returns True |
| `test_uuid_order_independent` | `is_pair_resolved(A, B)` == `is_pair_resolved(B, A)` |
| `test_unresolved_pair_not_found` | Pair not in table → False |
| `test_cooldown_active` | Resolution within cooldown → True |
| `test_cooldown_expired` | Resolution older than cooldown → False |
| `test_cooldown_zero_permanent` | Default cooldown=0 → always True regardless of age |
| `test_re_resolution_updates` | Second resolution for same pair overwrites the row |
| `test_scan_skips_resolved_pair` | `_check_contradictions_batch` skips pair found in history |
| `test_scan_flags_new_pair` | Pair NOT in history still gets flagged normally |
| `test_auto_resolve_records_all_pairs` | `auto_resolve_stale_conflicts` on 3-member group writes 3 rows (AB, AC, BC) |
| `test_llm_review_records_reviewed_pair_only` | `confirm_pending_conflicts` on 3-member group writes 1 row (members[0], members[1]) |
| `test_manual_keep_both_records_presented_pair` | Manual `keep_both` records `(members[0], members[1])` from prefetch |

## Implementation order

1. `store.py` — table schema + `record_conflict_resolution` + `is_pair_resolved`
2. `consolidation_queries.py` — prefetch member UUIDs, return in result dict
3. `lifecycle_service.py` — add `settings` param, import singleton, wire recording after resolve, wire pair check in scan
4. `core/bootstrap.py` — pass `settings` to `LifecycleService`
5. `config/settings.py` — `conflict_cooldown_days` setting
6. `tests/test_conflict_history.py` — full test suite for new behavior
7. Run full suite
