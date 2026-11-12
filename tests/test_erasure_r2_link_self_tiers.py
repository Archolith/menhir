"""R2 CF-337 follow-up: the best-effort self-tier link never swallows a lost fence.

``stamp_and_finalize`` treats a ``link_self_tiers`` failure as best effort, but ``FenceLost``
means an erase sealed the queue: the worker must requeue, not go on to mark the episode READY.
The online half (the real fence refusing the INFERRED_VIEW_OF write) is in
test_erasure_r2_post_publish_online.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.infrastructure.publish_fence import FenceLost
from menhir.services import enrichment_steps
from menhir.services.enrichment_steps import stamp_and_finalize
from tests.test_attempt_ownership_fences import _adapter, _ctx, _graphiti_result

pytestmark = pytest.mark.unit

_EXECUTOR = object()  # stands in for the claim-fence transaction


def _run(monkeypatch: pytest.MonkeyPatch, link_error: Exception | None) -> Any:
    receipt = SimpleNamespace(
        episode_key="ep-site",
        raw_entity_count=0,
        raw_edge_count=0,
        self_bind_result=SimpleNamespace(bound=True),
        inferred_bind_result=None,
    )
    monkeypatch.setattr(enrichment_steps, "get_extraction_receipt", lambda: receipt)
    monkeypatch.setattr(enrichment_steps, "clear_extraction_receipt", lambda: None)
    monkeypatch.setattr(enrichment_steps, "_fence_check", lambda ctx: None)
    monkeypatch.setattr(enrichment_steps, "_fenced_write", lambda ctx, call: call(_EXECUTOR))
    adapter = _adapter()
    adapter.link_self_tiers.side_effect = link_error
    ctx = _ctx(adapter, {"namespace": "ns-self"}, inferred_self_mode="enforce")
    node = SimpleNamespace(uuid="n1", name="", content="")
    return adapter, ctx, _graphiti_result([node])


def test_fence_lost_in_the_self_tier_link_reaches_the_worker(monkeypatch) -> None:
    """Fails if the best-effort ``except Exception`` swallows FenceLost (the episode would be
    marked READY after an erase sealed it)."""
    adapter, ctx, result = _run(monkeypatch, FenceLost("erase_marker"))

    with pytest.raises(FenceLost):
        asyncio.run(stamp_and_finalize(ctx, result))

    adapter.link_self_tiers.assert_called_once_with("ns-self", executor=_EXECUTOR)
    adapter.mark_episode_ready.assert_not_called()


def test_the_self_tier_link_runs_inside_the_fenced_write(monkeypatch) -> None:
    """Fails if stamp_and_finalize calls link_self_tiers outside ``_fenced_write`` (no executor,
    so the write would not share the claim re-check's transaction)."""
    adapter, ctx, result = _run(monkeypatch, None)
    adapter.link_self_tiers.side_effect = None

    asyncio.run(stamp_and_finalize(ctx, result))

    adapter.link_self_tiers.assert_called_once_with("ns-self", executor=_EXECUTOR)


def test_other_self_tier_link_errors_stay_best_effort(monkeypatch) -> None:
    """Control: an ordinary link failure is still swallowed and the episode finishes."""
    adapter, ctx, result = _run(monkeypatch, RuntimeError("neo4j hiccup"))

    asyncio.run(stamp_and_finalize(ctx, result))

    assert adapter.link_self_tiers.call_count == 1
    assert adapter.mark_episode_ready.call_count == 1
