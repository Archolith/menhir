"""CF-29 -- mcp_events records who called, under what tier, and where a failure landed.

`track_mcp_call` recorded operation, duration, sizes, preview and error text. An operator with a
failed call could tell which tool ran, but not which client invoked it, at which tier, or whether
the failure hit before the work, during it, or at the deadline.

OWNER RULING 2026-08-21, both parts:

1. **Full identity**, not the minimum: `client_name`, `client_id`, `session_id`, `tier`, plus
   `stage`. `session_id` is the consequential one -- it makes an MCP row addressable by the session
   that made the call, which is a NEW subject linkage, so `erasure_inventory` had to be widened to
   match. That obligation is discharged here and asserted, not just noted.
2. **Sentinel backfill** over the 218,925 pre-existing rows rather than leaving them NULL.

THE HONEST LIMIT ON `stage`, and why there is no committed/rolled-back pair: this wrapper sees only
the boundary of `runner()`. It cannot know whether a mutation inside the runner reached the graph.
`timeout` is precisely the case where that is unknown. So the stage stamp NARROWS CF-28 -- an
operator can now tell "rejected at the gate" from "broke while running" from "cancelled mid-flight"
-- but it does not resolve it, and a label claiming otherwise would be a stage that lies.

Identity is read from the request context, which is the same source the namespace pin keys on. It
is therefore the SERVER-resolved identity, never a value the caller put in its own arguments -- the
distinction CF-118 already had to make for the payload preview.
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.core.request_context import (
    bind_request_session,
    bind_request_tier,
    reset_request_session,
    reset_request_tier,
)
from menhir.domain.session import MemorySession
from menhir.infrastructure.telemetry.erasure_inventory import CONTENT_COLUMNS
from menhir.infrastructure.telemetry.schema_migrations import _PRE_CF29_SENTINEL
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.mcp.telemetry.tracker import (
    STAGE_COMPLETED,
    STAGE_DENIED,
    STAGE_FAILED,
    STAGE_TIMEOUT,
    track_mcp_call,
)

pytestmark = pytest.mark.unit

IDENTITY_COLUMNS = ("client_name", "client_id", "session_id", "tier", "stage")


@pytest.fixture
def store(tmp_path) -> McpTelemetryStore:
    s = McpTelemetryStore(db_path=tmp_path / "t.db")
    s._ensure_ready()
    return s


def _rows(store: McpTelemetryStore) -> list[dict]:
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM mcp_events ORDER BY id")]
    finally:
        conn.close()


async def _call(store, *, runner, tier="agent", client="claude-code", effective=lambda: {"a": 1}):
    session = MemorySession(
        session_id="sess-1", user_id="u", started_at="2026-08-21T00:00:00+00:00",
        client_id="cid-1", client_name=client,
    )
    st, tt = bind_request_session(session), bind_request_tier(tier)
    try:
        return await track_mcp_call(
            kind="tool", operation="add_memory", payload={"a": 1},
            runner=runner, store=store, timeout=1, effective_payload=effective,
        )
    finally:
        reset_request_tier(tt)
        reset_request_session(st)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_successful_call_records_the_caller_and_tier(store) -> None:
    async def ok():
        return "fine"

    await _call(store, runner=ok)
    row = _rows(store)[0]

    assert row["client_name"] == "claude-code"
    assert row["client_id"] == "cid-1"
    assert row["session_id"] == "sess-1"
    assert row["tier"] == "agent"


@pytest.mark.asyncio
async def test_identity_is_recorded_on_the_failure_path_too(store) -> None:
    """The path that matters. A successful call is the one an operator never has to triage; the
    entry is about what a FAILED row can tell you."""
    async def boom():
        raise RuntimeError("nope")

    await _call(store, runner=boom, tier="readonly")
    row = _rows(store)[0]

    assert row["success"] == 0
    assert row["client_name"] == "claude-code"
    assert row["tier"] == "readonly"


@pytest.mark.asyncio
async def test_a_call_with_no_session_records_nulls_not_a_guess(store) -> None:
    """Background and internal work has no caller. Nulls are the truthful answer; inventing
    'system' would put a fake client in the audit trail."""
    async def ok():
        return "fine"

    await track_mcp_call(
        kind="background", operation="enrich", payload=None,
        runner=ok, store=store, timeout=1,
    )
    row = _rows(store)[0]

    assert row["client_name"] is None
    assert row["session_id"] is None


@pytest.mark.asyncio
async def test_the_recorded_client_is_server_resolved_not_caller_supplied(store) -> None:
    """CF-118's distinction, applied to identity. A caller putting `client_name` in its own
    arguments must not be able to write that into the operator's audit trail -- identity comes
    from the bound session, which the caller does not control."""
    async def ok():
        return "fine"

    await _call(store, runner=ok, effective=lambda: {"client_name": "impersonated"})
    row = _rows(store)[0]

    assert row["client_name"] == "claude-code"


# ---------------------------------------------------------------------------
# stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_completed_call_is_stamped_completed(store) -> None:
    async def ok():
        return "fine"

    await _call(store, runner=ok)
    assert _rows(store)[0]["stage"] == STAGE_COMPLETED


@pytest.mark.asyncio
async def test_a_runner_failure_is_stamped_failed(store) -> None:
    async def boom():
        raise RuntimeError("nope")

    await _call(store, runner=boom)
    assert _rows(store)[0]["stage"] == STAGE_FAILED


@pytest.mark.asyncio
async def test_a_timeout_is_its_own_stage(store) -> None:
    """Distinct from `failed` on purpose: this is the one case where the wrapper cannot know
    whether a mutation inside the runner committed. Collapsing it into `failed` would hide exactly
    the ambiguity CF-28 is about."""
    import asyncio

    async def hangs():
        await asyncio.sleep(5)

    await _call(store, runner=hangs)
    assert _rows(store)[0]["stage"] == STAGE_TIMEOUT


@pytest.mark.asyncio
async def test_a_refused_call_is_stamped_denied_not_failed(store) -> None:
    """THE TRIAGE DISTINCTION. A call rejected at the tier or allowlist gate raises inside the
    runner like any other error, so the exception path alone cannot tell "your request was
    rejected" from "the work broke" -- which is the first question an operator asks.

    Keyed on the same signal CF-118 already uses for the preview: arguments never published."""
    async def denied():
        raise PermissionError("tier")

    await _call(store, runner=denied, effective=lambda: None)
    row = _rows(store)[0]

    assert row["stage"] == STAGE_DENIED
    assert row["stage"] != STAGE_FAILED


@pytest.mark.asyncio
async def test_denied_wins_over_the_outcome_even_on_timeout(store) -> None:
    """POSITIVE CONTROL on the precedence. If the outcome won, a slow rejected call would be
    stamped `timeout` and read as "we may have written something" when nothing ran."""
    import asyncio

    async def hangs():
        await asyncio.sleep(5)

    await _call(store, runner=hangs, effective=lambda: None)
    assert _rows(store)[0]["stage"] == STAGE_DENIED


# ---------------------------------------------------------------------------
# the erasure obligation the ruling's fuller field set creates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column", ("payload_preview", "error"))
def test_session_id_is_a_key_column_for_mcp_events_content(column: str) -> None:
    """THE COST THE RULING ACCEPTED, discharged. Recording `session_id` makes these rows
    addressable by session, which is a new subject linkage -- so the erasure inventory has to key
    on it, or a session-scoped purge would walk past content it can now reach."""
    entry = next(e for e in CONTENT_COLUMNS if e.table == "mcp_events" and e.column == column)
    assert "session_id" in entry.key_columns


@pytest.mark.parametrize("column", ("payload_preview", "error"))
def test_the_older_keys_are_not_dropped(column: str) -> None:
    """POSITIVE CONTROL. ANY_SUBJECT ORs its keys, so adding one must widen reach and never
    replace the node-uuid or namespace paths that already worked."""
    entry = next(e for e in CONTENT_COLUMNS if e.table == "mcp_events" and e.column == column)
    assert {"node_uuid", "namespace"} <= set(entry.key_columns)


def test_a_session_scoped_purge_reaches_an_mcp_row(tmp_path) -> None:
    """Executed, not declared (T17). The inventory naming `session_id` proves nothing about the
    purge using it.

    The positive control is the point here: BOTH rows share namespace='default', so a purge that
    fell back to the namespace key would erase them both and still pass a test that only checked
    the target row."""
    from menhir.infrastructure.telemetry.erasure_purge import (
        ErasureSubjects,
        purge_content,
    )

    db = tmp_path / "t.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    conn = sqlite3.connect(db)
    # Both rows carry namespace='default'. Required, and it is also the realistic case: the CF-165
    # insert trigger stamps 'default' on any mcp_events row written without a namespace AND
    # redacts its content, so a row with no namespace cannot exist to be tested. Which is exactly
    # why session_id earns its place -- nearly all real traffic sits in one silo, where a
    # namespace-keyed purge is far too wide to be the only way in.
    conn.execute(
        "INSERT INTO mcp_events (started_at, completed_at, duration_ms, operation, kind, success, "
        "payload_preview, namespace, session_id) "
        "VALUES ('t','t',1,'add_memory','tool',1,'SECRET','default','sess-9')"
    )
    conn.execute(
        "INSERT INTO mcp_events (started_at, completed_at, duration_ms, operation, kind, success, "
        "payload_preview, namespace, session_id) "
        "VALUES ('t','t',1,'add_memory','tool',1,'KEEPME','default','sess-other')"
    )
    conn.commit()

    purge_content(conn, ErasureSubjects(session_ids=frozenset({"sess-9"})), dry_run=False)
    conn.commit()

    surviving = {sid: prev for sid, prev in conn.execute(
        "SELECT session_id, payload_preview FROM mcp_events"
    )}
    conn.close()

    assert "SECRET" not in (surviving["sess-9"] or "")
    assert surviving["sess-other"] == "KEEPME", "another session's row must be untouched"


# ---------------------------------------------------------------------------
# the backfill
# ---------------------------------------------------------------------------


def test_pre_existing_rows_get_the_sentinel(tmp_path) -> None:
    """The ruling's second half. A row written before these columns existed carries an explicit
    marker so a consumer never branches on NULL."""
    from menhir.infrastructure.telemetry.schema_migrations import ensure_lineage_columns

    db = tmp_path / "t.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    conn = sqlite3.connect(db)
    for column in IDENTITY_COLUMNS:
        conn.execute(f"UPDATE mcp_events SET {column} = NULL")
    conn.execute(
        "INSERT INTO mcp_events (started_at, completed_at, duration_ms, operation, kind, success) "
        "VALUES ('t','t',1,'old','tool',1)"
    )
    conn.commit()

    ensure_lineage_columns(conn)
    conn.commit()

    row = dict(zip([c[0] for c in conn.execute("SELECT * FROM mcp_events LIMIT 0").description],
                   conn.execute("SELECT * FROM mcp_events").fetchone()))
    conn.close()
    for column in IDENTITY_COLUMNS:
        assert row[column] == _PRE_CF29_SENTINEL


def test_the_backfill_is_idempotent_and_does_not_rewrite_real_values(tmp_path) -> None:
    """THE HALF THAT WOULD BE DESTRUCTIVE IF WRONG. It runs on every startup against the
    operator's live sidecar, so a second pass must not overwrite identity a real call recorded."""
    from menhir.infrastructure.telemetry.schema_migrations import ensure_lineage_columns

    db = tmp_path / "t.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO mcp_events (started_at, completed_at, duration_ms, operation, kind, success, "
        "client_name, tier) VALUES ('t','t',1,'new','tool',1,'claude-code','operator')"
    )
    conn.commit()

    ensure_lineage_columns(conn)
    ensure_lineage_columns(conn)
    conn.commit()

    name, tier = conn.execute("SELECT client_name, tier FROM mcp_events").fetchone()
    conn.close()
    assert (name, tier) == ("claude-code", "operator")


def test_the_sentinel_cannot_be_mistaken_for_a_real_client() -> None:
    """It collapses "predates the column" and "no identity available" into one value -- that is the
    cost of the ruling. Choosing an implausible marker is what keeps the collapse visible instead
    of letting it read as a real caller."""
    assert _PRE_CF29_SENTINEL == "pre-cf29"
    assert _PRE_CF29_SENTINEL not in ("", "unknown", "system", "default")
