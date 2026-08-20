"""CF-26 / CF-95: `backend_invoke` must not log the request body.

The handler logs at ERROR under the default INFO threshold, so anything it writes lands in
`server.log` with no debug mode and no redaction -- `privacy.py` does not sit on this boundary
(CF-96). The body carries raw caller content: `queue_episode(text=...)` puts episode text
straight into it.

Every test here drives the REAL `backend_invoke_impl`. Each absence assertion is paired with a
positive control, because "the canary is not in the log" also passes against a handler that
logs nothing at all.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from menhir.api.routes_handlers import backend_invoke_impl

CANARY = "CANARY-CF95-7f3a2b-raw-episode-text"


class _Session:
    session_id = "sess-cf95"


class _Backend:
    """Backend whose only operation raises, which is the condition that triggers the log."""

    async def queue_episode(self, **kwargs: Any) -> Any:
        raise RuntimeError("backend exploded")


def _invoke(body: dict[str, Any] | None, logger: logging.Logger):
    return backend_invoke_impl(
        object(),
        "queue_episode",
        body,
        backend_methods={"queue_episode"},
        required_tier_for_operation=lambda _op: "agent",
        require_tier=lambda _tier: None,
        try_record_destructive_op_rest=lambda _op: None,
        resolve_caller_session=lambda _req: _Session(),
        get_backend=lambda _req: _Backend(),
        drain_background_errors=lambda _sid: [],
        logger=logger,
    )


def _all_text(caplog: pytest.LogCaptureFixture) -> str:
    """Every rendered record plus its traceback -- not just `record.msg`.

    Checking `msg` alone would miss the canary when it arrives through a %-arg or through the
    exception text, which is exactly how it used to leak.
    """
    parts: list[str] = []
    for rec in caplog.records:
        parts.append(rec.getMessage())
        if rec.exc_text:
            parts.append(rec.exc_text)
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_raw_body_never_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("menhir.test.cf95")
    caplog.set_level(logging.DEBUG, logger=logger.name)

    with pytest.raises(RuntimeError):
        await _invoke({"text": CANARY, "namespace": "tenant-a"}, logger)

    text = _all_text(caplog)

    # POSITIVE CONTROL: the handler must actually have logged, or the assertion below
    # would pass against a handler that logs nothing.
    assert "backend_invoke failed" in text
    assert "queue_episode" in text

    # The defect: caller content in an operational log.
    assert CANARY not in text


@pytest.mark.asyncio
async def test_key_names_are_reported_but_values_are_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Argument NAMES are not caller content and are what makes the line diagnosable."""
    logger = logging.getLogger("menhir.test.cf95.keys")
    caplog.set_level(logging.DEBUG, logger=logger.name)

    with pytest.raises(RuntimeError):
        await _invoke({"text": CANARY, "namespace": "tenant-a"}, logger)

    text = _all_text(caplog)
    assert "text" in text and "namespace" in text
    assert CANARY not in text
    assert "tenant-a" not in text


@pytest.mark.asyncio
async def test_non_dict_and_empty_bodies_do_not_break_the_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The body is typed `dict | None` but the log path must not itself raise on anything."""
    logger = logging.getLogger("menhir.test.cf95.shapes")
    caplog.set_level(logging.DEBUG, logger=logger.name)

    for body in (None, {}):
        caplog.clear()
        with pytest.raises(RuntimeError):
            await _invoke(body, logger)
        assert "backend_invoke failed" in _all_text(caplog)
