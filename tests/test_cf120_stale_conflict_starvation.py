"""CF-120 -- the stale-conflict resolver asked for the newest rows and then discarded
everything young.

``LifecycleConflictService.auto_resolve_stale_conflicts`` existed to clear conflict
groups that had gone unresolved past an age threshold. It fetched the NEWEST ``limit``
unresolved groups (``list_conflict_groups`` orders ``created_at DESC``) and then kept
only the OLDEST in Python. So when the newest rows are all young, the job returns 0
forever while the backlog it was built to drain grows unbounded -- the head keeps
refreshing with fresh conflicts and the stale tail is never fetched.

The fix pushes the age cutoff into Cypher (``created_before``) and orders ascending
(``oldest_first=True``) for this caller only. The other ``list_conflict_groups`` callers
keep newest-first because both parameters default to the old behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.services.lifecycle_conflicts import LifecycleConflictMixin


class _StaleService(LifecycleConflictMixin):
    """Minimal mixin holder so the service method needs no real graph/LLM deps."""

    def __init__(self, graph_adapter):
        self.graph_adapter = graph_adapter


class _HonoringAdapter:
    """Fake adapter that honors created_before/oldest_first like the real query."""

    def __init__(self, groups):
        self.groups = list(groups)
        self.resolved = []

    def list_conflict_groups(self, *, status=None, limit=25, namespace=None,
                             created_before=None, oldest_first=False):
        candidates = [g for g in self.groups
                      if status is None or g["status"] == status]
        if created_before is not None:
            candidates = [g for g in candidates if g["created_at"] < created_before]
        candidates = sorted(candidates, key=lambda g: g["created_at"],
                            reverse=not oldest_first)
        return candidates[:limit]

    def resolve_conflict_group(self, conflict_group_id, action, *,
                               resolution_status="resolved", **kwargs):
        self.resolved.append(conflict_group_id)
        return {"member_uuids": ["m1", "m2"]}


class _IgnoringAdapter:
    """Fake adapter with the OLD fetch shape: newest-first, no cutoff support."""

    def __init__(self, groups):
        self.groups = list(groups)
        self.resolved = []

    def list_conflict_groups(self, *, status=None, limit=25, namespace=None, **kwargs):
        candidates = [g for g in self.groups
                      if status is None or g["status"] == status]
        candidates = sorted(candidates, key=lambda g: g["created_at"], reverse=True)
        return candidates[:limit]

    def resolve_conflict_group(self, conflict_group_id, action, *,
                               resolution_status="resolved", **kwargs):
        self.resolved.append(conflict_group_id)
        return {"member_uuids": ["m1", "m2"]}


class _CapturingAdapter:
    """Fake adapter that records the kwargs the service handed over."""

    def __init__(self):
        self.kwargs = None

    def list_conflict_groups(self, *, status=None, limit=25, namespace=None,
                             created_before=None, oldest_first=False, **kwargs):
        self.kwargs = {
            "status": status,
            "limit": limit,
            "created_before": created_before,
            "oldest_first": oldest_first,
        }
        return []

    def resolve_conflict_group(self, *args, **kwargs):
        return {"member_uuids": []}


def _starvation_backlog():
    """60 unresolved groups: newest 50 are 1 day old, oldest 10 are 90 days old."""
    now = datetime.now(timezone.utc)
    old = [
        {"group_id": f"old-{i}", "status": "unresolved",
         "created_at": now - timedelta(days=90)}
        for i in range(10)
    ]
    new = [
        {"group_id": f"new-{i}", "status": "unresolved",
         "created_at": now - timedelta(days=1)}
        for i in range(50)
    ]
    return old + new


@pytest.mark.unit
def test_stale_resolver_drains_the_oldest_backlog_not_the_fresh_head() -> None:
    """End-to-end: with an honoring adapter the 10 old groups are resolved."""
    adapter = _HonoringAdapter(_starvation_backlog())
    svc = _StaleService(adapter)

    result = svc.auto_resolve_stale_conflicts(max_age_days=14, limit=50)

    assert result == 10
    assert len(adapter.resolved) == 10
    assert all(gid.startswith("old-") for gid in adapter.resolved)


@pytest.mark.unit
def test_old_fetch_shape_starves_the_backlog() -> None:
    """The defect as a second fake: newest-first + no cutoff returns 0 for the same data."""
    adapter = _IgnoringAdapter(_starvation_backlog())
    svc = _StaleService(adapter)

    result = svc.auto_resolve_stale_conflicts(max_age_days=14, limit=50)

    assert result == 0
    assert adapter.resolved == []


@pytest.mark.unit
def test_service_passes_cutoff_and_oldest_first_to_the_adapter() -> None:
    """Structural pin: the fix must actually be handed to list_conflict_groups."""
    adapter = _CapturingAdapter()
    svc = _StaleService(adapter)

    svc.auto_resolve_stale_conflicts(max_age_days=14, limit=50)

    assert adapter.kwargs is not None
    assert adapter.kwargs["oldest_first"] is True
    assert adapter.kwargs["limit"] == 50
    created_before = adapter.kwargs["created_before"]
    assert isinstance(created_before, datetime)
    age = datetime.now(timezone.utc) - created_before
    assert timedelta(days=13) <= age <= timedelta(days=15)


class _StubNeo4j:
    def __init__(self):
        self.query = None
        self.params = None

    def execute(self, query, params=None, **kwargs):
        self.query = query
        self.params = params or {}
        return []


@pytest.mark.unit
def test_query_is_unchanged_for_callers_without_new_args() -> None:
    """Regression guard: default callers keep newest-first and no cutoff."""
    stub = _StubNeo4j()
    repo = ConsolidationRepository(neo4j=stub)

    repo.list_conflict_groups()

    assert "ORDER BY created_at DESC" in stub.query
    assert "created_before" not in stub.params
    assert "$created_before" not in stub.query


@pytest.mark.unit
def test_query_emits_asc_and_cutoff_when_new_args_are_given() -> None:
    stub = _StubNeo4j()
    repo = ConsolidationRepository(neo4j=stub)
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)

    repo.list_conflict_groups(created_before=cutoff, oldest_first=True)

    assert "ORDER BY created_at ASC" in stub.query
    assert "created_at < $created_before" in stub.query
    assert stub.params["created_before"] is cutoff
    assert "created_before" in stub.params


@pytest.mark.unit
def test_empty_backlog_returns_zero_and_calls_nothing() -> None:
    """POSITIVE CONTROL: nothing to do -> 0, no resolution attempted."""
    class _EmptyAdapter:
        def list_conflict_groups(self, **kwargs):
            return []

        def resolve_conflict_group(self, *args, **kwargs):
            raise AssertionError("must not resolve an empty backlog")

    svc = _StaleService(_EmptyAdapter())

    result = svc.auto_resolve_stale_conflicts(max_age_days=14, limit=50)

    assert result == 0


@pytest.mark.unit
def test_young_group_is_still_skipped_by_the_retained_python_filter() -> None:
    """POSITIVE CONTROL: a group younger than the cutoff that reaches the loop is skipped,
    proving the redundant guard against an adapter that ignores the cutoff is live."""
    now = datetime.now(timezone.utc)
    adapter = _IgnoringAdapter([
        {"group_id": "young", "status": "unresolved",
         "created_at": now - timedelta(days=1)},
    ])
    svc = _StaleService(adapter)

    result = svc.auto_resolve_stale_conflicts(max_age_days=14, limit=50)

    assert result == 0
    assert adapter.resolved == []
