"""CF-63: batch correlation must scope its search the way the single-entity path does.

`CorrelationService` runs the same semantic search two ways. `check_correlation` scopes it with
`group_ids=namespace_to_group_ids(namespace)`; `check_correlation_batch` did not, and structurally
could not -- it took no namespace parameter at all.

Results feed `classify_pair()` and merge proposal handling, so an unscoped hit is a
cross-namespace merge, and a merge is permanent.

The method has no production caller (the only callers in the corpus are in
`tests/test_correlation_service.py`), so this was latent rather than live. That is exactly why the
docstring mattered: it claimed the method was "used during consolidation/promotion", so anyone
wiring it up would reasonably have believed the unscoped search had already been exercised in that
role.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.domain.namespace import namespace_to_group_ids
from menhir.services.correlation_service import CorrelationService


def _service(results: list[tuple[str, str, float]] | None = None) -> tuple[CorrelationService, Any]:
    """A service whose graphiti client records exactly how the search was called."""
    client = MagicMock()
    client.search_scored = AsyncMock(return_value=results or [])
    repo = MagicMock()
    svc = CorrelationService(repo, client)
    # classify_pair reaches the repository and the judge; this test is about the SEARCH call,
    # so short-circuit everything downstream of it.
    svc.classify_pair = AsyncMock(return_value=("none", None))
    return svc, client


@pytest.mark.asyncio
async def test_explicit_namespace_scopes_the_search() -> None:
    svc, client = _service()

    await svc.check_correlation_batch(
        [{"uuid": "u-1", "name": "entity", "content": "text"}], namespace="tenant-a"
    )

    kwargs = client.search_scored.await_args.kwargs
    assert kwargs["group_ids"] == namespace_to_group_ids("tenant-a")


@pytest.mark.asyncio
async def test_a_candidates_own_namespace_scopes_its_own_search() -> None:
    """A batch can span namespaces, so scoping must resolve per candidate, not once per batch."""
    svc, client = _service()

    await svc.check_correlation_batch(
        [
            {"uuid": "u-1", "name": "a", "content": "text", "namespace": "tenant-a"},
            {"uuid": "u-2", "name": "b", "content": "text", "namespace": "tenant-b"},
        ]
    )

    calls = client.search_scored.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["group_ids"] == namespace_to_group_ids("tenant-a")
    assert calls[1].kwargs["group_ids"] == namespace_to_group_ids("tenant-b")
    assert calls[0].kwargs["group_ids"] != calls[1].kwargs["group_ids"], (
        "two namespaces must not collapse to one scope"
    )


@pytest.mark.asyncio
async def test_explicit_namespace_wins_over_a_candidates_own() -> None:
    """The caller pinning a namespace is a stronger statement than the row's own field."""
    svc, client = _service()

    await svc.check_correlation_batch(
        [{"uuid": "u-1", "name": "a", "content": "text", "namespace": "tenant-b"}],
        namespace="tenant-a",
    )

    kwargs = client.search_scored.await_args.kwargs
    assert kwargs["group_ids"] == namespace_to_group_ids("tenant-a")


@pytest.mark.asyncio
async def test_scoping_matches_the_single_entity_path_exactly() -> None:
    """PARITY: the two paths must scope identically, or the fix only moves the divergence."""
    svc_batch, client_batch = _service()
    svc_single, client_single = _service()

    await svc_batch.check_correlation_batch(
        [{"uuid": "u-1", "name": "a", "content": "query text"}], namespace="tenant-a"
    )
    await svc_single.check_correlation("u-1", "query text", namespace="tenant-a")

    assert (
        client_batch.search_scored.await_args.kwargs["group_ids"]
        == client_single.search_scored.await_args.kwargs["group_ids"]
    )


@pytest.mark.asyncio
async def test_no_namespace_anywhere_still_searches_unscoped() -> None:
    """DOCUMENTED RESIDUAL, asserted so it cannot change silently.

    This matches `check_correlation`'s existing behavior. Making the unresolvable case fail closed
    is a change to BOTH methods and to their existing tests, and is deliberately out of scope for
    CF-63 -- but if someone does make that change, this test should fail and force the decision to
    be explicit.
    """
    svc, client = _service()

    await svc.check_correlation_batch([{"uuid": "u-1", "name": "a", "content": "text"}])

    kwargs = client.search_scored.await_args.kwargs
    assert "group_ids" not in kwargs


@pytest.mark.asyncio
async def test_the_search_still_runs_and_still_carries_num_results() -> None:
    """POSITIVE CONTROL: without this, every assertion above would pass against a method that
    stopped searching entirely."""
    svc, client = _service()

    result = await svc.check_correlation_batch(
        [{"uuid": "u-1", "name": "a", "content": "text"}], namespace="tenant-a"
    )

    assert client.search_scored.await_count == 1
    assert client.search_scored.await_args.kwargs["num_results"] == 5
    assert result.checked == 1


@pytest.mark.asyncio
async def test_empty_query_still_skips_without_searching() -> None:
    """The new scoping code sits before the try/except; it must not have moved the empty-query
    skip, which returns before any search happens."""
    svc, client = _service()

    result = await svc.check_correlation_batch(
        [{"uuid": "u-1", "name": "", "content": "  "}], namespace="tenant-a"
    )

    assert result.skipped == 1
    client.search_scored.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_search_is_still_caught_per_candidate() -> None:
    """Scoping is computed OUTSIDE the try, so a bad namespace must not become an uncaught raise
    that aborts the whole batch."""
    svc, client = _service()
    client.search_scored = AsyncMock(side_effect=RuntimeError("search exploded"))

    result = await svc.check_correlation_batch(
        [
            {"uuid": "u-1", "name": "a", "content": "text"},
            {"uuid": "u-2", "name": "b", "content": "text"},
        ],
        namespace="tenant-a",
    )

    assert result.checked == 2
    assert result.skipped == 2
