"""CF-36 -- raw tracebacks require operator tier; the rest of the trace stays at readonly.

**The entry is mostly answered by what `readonly` already means.** It reports these three tools as
returning "global memory content and operational traces" at readonly. But readonly is the READ tier:
it already grants `recall_memories`, `build_context` and `read_flagged_memories`, so a readonly
caller reading memory content is the design, not the defect. And the *global* half was closed by
CF-33 step 4 -- all three tools now declare `namespace`, are `ToolScope.NAMESPACED`, and the two
that address an object by uuid carry `foreign_object_refusal`, covered by
`tests/test_object_ownership_at_load.py`.

OWNER RULING 2026-08-21: gate tracebacks only. A raw Python traceback exposes deployment internals
-- absolute paths, module layout, endpoint URLs -- and its exception prose can carry the text being
processed. Everything a monitoring client is actually for (state, stage, attempts, owner, lease,
heartbeat, error_type, the exception message) stays at readonly.

THE GATE HAS TWO PATHS AND ONE IS EASY TO MISS. `traceback_preview` is a named field, but `details`
returns the *whole* details dict -- which carries the FULL `traceback` next to the preview, and does
so in `lifecycle_events` as well as `failure_events`. Gating the named field alone would leave the
same content one key over, in two places. That sibling-path shape is what these tests are mostly
about.
"""

from __future__ import annotations

import json

import pytest

from menhir.core.request_context import bind_request_tier, reset_request_tier
from menhir.mcp.tools.ops.get_episode_trace import (
    _TRACEBACK_WITHHELD,
    GetEpisodeTraceTool,
    _strip_tracebacks,
)

pytestmark = pytest.mark.unit

_TRACE = 'Traceback (most recent call last):\n  File "C:/srv/menhir/x.py", line 9\nValueError: boom'
_DETAILS = {"traceback": _TRACE, "traceback_preview": _TRACE[:500], "error_type": "ValueError"}


class _Backend:
    """Returns one failure row and one lifecycle row, both carrying a traceback."""

    async def fetch_memory_by_uuid(self, uuid, **kw):
        return None

    async def fetch_episode_processing(self, *a, **kw):
        return None

    async def fetch_episode_task_events(self, *a, **kw):
        return []

    async def fetch_recent_failures(self, *a, **kw):
        return [{"recorded_at": "t", "operation": "enrich", "error": "boom",
                 "details_json": json.dumps(_DETAILS)}]

    async def fetch_recent_lifecycle_events(self, *a, **kw):
        return [{"recorded_at": "t", "component": "c", "event": "e", "state": "s",
                 "details_json": json.dumps(_DETAILS)}]


async def _trace(tier: str) -> dict:
    """Render the trace with *tier* bound.

    The bind happens INSIDE the coroutine on purpose. `bind_request_tier` returns a ContextVar
    token, and a token minted in a sync fixture cannot be reset from the asyncio task the test body
    runs in -- ContextVar raises "created in a different Context". Binding and resetting in the same
    context is the only arrangement that both takes effect and cleans up.
    """
    token = bind_request_tier(tier)
    try:
        tool = GetEpisodeTraceTool()
        tool.get_backend = lambda: _Backend()  # type: ignore[method-assign]
        return json.loads(
            await tool.endpoint(episode_uuid="11111111-1111-1111-1111-111111111111")
        )
    finally:
        reset_request_tier(token)


# ---------------------------------------------------------------------------
# the helper, in isolation
# ---------------------------------------------------------------------------


def test_both_traceback_keys_are_replaced_not_just_the_preview() -> None:
    """THE SIBLING PATH. `details` carries the full `traceback` alongside `traceback_preview`;
    masking only the named field leaves the whole stack one key over."""
    out = _strip_tracebacks(dict(_DETAILS), allowed=False)
    assert out["traceback"] == _TRACEBACK_WITHHELD
    assert out["traceback_preview"] == _TRACEBACK_WITHHELD


def test_non_traceback_fields_survive_the_strip() -> None:
    """POSITIVE CONTROL. A helper that blanked the dict would pass the test above while
    destroying exactly the diagnostics the ruling keeps at readonly."""
    assert _strip_tracebacks(dict(_DETAILS), allowed=False)["error_type"] == "ValueError"


def test_an_operator_sees_the_traceback_unchanged() -> None:
    assert _strip_tracebacks(dict(_DETAILS), allowed=True) == _DETAILS


def test_a_withheld_marker_is_used_rather_than_a_silent_drop() -> None:
    """An operator debugging someone's report needs to distinguish "withheld" from "there was no
    traceback"."""
    out = _strip_tracebacks({"traceback": _TRACE}, allowed=False)
    assert out["traceback"] == _TRACEBACK_WITHHELD

    absent = _strip_tracebacks({"traceback": None}, allowed=False)
    assert absent["traceback"] is None


# ---------------------------------------------------------------------------
# the tool, end to end -- the helper being right proves nothing about it being called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_gets_no_traceback_on_either_event_list() -> None:
    """TRAP T17 IN ITS USUAL FORM. `_strip_tracebacks` passing its own tests says nothing about
    whether the render calls it -- and there are TWO render blocks, `failure_events` and
    `lifecycle_events`, each with its own `details` passthrough."""
    out = await _trace("readonly")

    for block in ("failure_events", "lifecycle_events"):
        rendered = json.dumps(out[block])
        assert "ValueError: boom" not in rendered, f"{block} leaked the traceback"
        assert "C:/srv/menhir" not in rendered, f"{block} leaked a deployment path"


@pytest.mark.asyncio
async def test_operator_still_gets_the_traceback() -> None:
    """The other half of the ruling. A gate that withheld from everyone would pass the test above
    and make the tool useless for the person it exists for."""
    out = await _trace("operator")

    assert "ValueError: boom" in json.dumps(out["failure_events"])
    assert "ValueError: boom" in json.dumps(out["lifecycle_events"])


@pytest.mark.asyncio
async def test_readonly_keeps_everything_the_ruling_left_alone() -> None:
    """The ruling is explicit that monitoring clients keep working. The exception MESSAGE and
    error_type stay -- only the raw stack goes."""
    out = await _trace("readonly")

    failure = out["failure_events"][0]
    assert failure["error"] == "boom"
    assert failure["details"]["error_type"] == "ValueError"


@pytest.mark.asyncio
async def test_an_unbound_tier_is_not_treated_as_operator() -> None:
    """`get_request_tier()` returns "" when nothing bound a tier -- which the contract layer reads
    as "no gate applied". Defaulting that to operator would open the field on exactly the requests
    that were never authorized."""
    out = await _trace("")

    assert "ValueError: boom" not in json.dumps(out["failure_events"])


@pytest.mark.asyncio
async def test_agent_tier_does_not_reach_it_either() -> None:
    """operator, not "above readonly". agent is the write tier, not the diagnostics tier."""
    out = await _trace("agent")

    assert "ValueError: boom" not in json.dumps(out["failure_events"])
