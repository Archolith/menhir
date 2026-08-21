"""CF-120b, against a live Neo4j: the end-to-end proof that closes the entry's open question.

The offline sibling (`test_cf120b_conflict_created_at_driver_type.py`) pins the behavior using a
`neo4j.time.DateTime` constructed in-process, which is what keeps the regression covered in the
default lane. This file is the part that could not be written before: it writes real conflict
members with Cypher's own `datetime()`, reads them back through the real repository, and asserts
the type the driver actually hands over and that the job now drains its backlog.

Run with: pytest --run-online tests/test_cf120b_conflict_created_at_live.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.services.lifecycle_conflicts import LifecycleConflictMixin

pytestmark = [pytest.mark.online, pytest.mark.timing]


def _write_group(repo, group_id: str, *, days_old: int, members: int = 2) -> None:
    for i in range(members):
        repo.execute(
            """
            CREATE (n:Entity {
                uuid: $uuid, name: $name, namespace: 'default', scope: 'PERSISTENT',
                content: $name, conflict_group_id: $gid, conflict_status: 'unresolved',
                conflict_created_at: datetime() - duration({days: $days})
            })
            """,
            params={"uuid": f"{group_id}-{i}", "name": f"{group_id}-{i}",
                    "gid": group_id, "days": days_old},
        )


@pytest.fixture
def conflict_repo(test_neo4j_repo):
    test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
    yield ConsolidationRepository(neo4j=test_neo4j_repo)


class _Service(LifecycleConflictMixin):
    def __init__(self, adapter) -> None:
        self.graph_adapter = adapter


class _RepoAdapter:
    """The real repository behind the adapter surface the service calls."""

    def __init__(self, repo: ConsolidationRepository) -> None:
        self._repo = repo
        self.resolved: list[str] = []

    def list_conflict_groups(self, **kwargs):
        return self._repo.list_conflict_groups(**kwargs)

    def resolve_conflict_group(self, group_id, action, **kwargs):
        self.resolved.append(group_id)
        return {"member_uuids": []}


def test_the_driver_returns_a_type_that_breaks_naive_datetime_arithmetic(
    test_neo4j_repo, conflict_repo
) -> None:
    """The entry's open question, answered against the real driver rather than assumed."""
    _write_group(test_neo4j_repo, "g-old", days_old=90)

    rows = conflict_repo.list_conflict_groups(status="unresolved", limit=50)
    created_at = rows[0]["created_at"]

    assert type(created_at).__module__.startswith("neo4j.time")
    # Both halves of the trap: it HAS tzinfo (so the old guard skipped parsing) and it is NOT a
    # stdlib datetime (so the subtraction that followed raised).
    assert hasattr(created_at, "tzinfo")
    assert not isinstance(created_at, datetime)
    with pytest.raises(TypeError):
        datetime.now(timezone.utc) - created_at


def test_the_job_drains_the_stale_backlog_on_the_live_graph(
    test_neo4j_repo, conflict_repo
) -> None:
    """End to end: both CF-120 defects at once. The fetch must reach past the fresh head, and the
    age computation must survive the driver type. Before either fix this returned 0."""
    for i in range(3):
        _write_group(test_neo4j_repo, f"g-stale-{i}", days_old=90)
    for i in range(5):
        _write_group(test_neo4j_repo, f"g-fresh-{i}", days_old=1)

    adapter = _RepoAdapter(conflict_repo)

    resolved = _Service(adapter).auto_resolve_stale_conflicts(max_age_days=14, limit=50)

    assert resolved == 3
    assert sorted(adapter.resolved) == ["g-stale-0", "g-stale-1", "g-stale-2"]


def test_the_cutoff_and_sort_reach_past_a_full_page_of_fresh_groups(
    test_neo4j_repo, conflict_repo
) -> None:
    """The starvation scenario as the entry describes it, on real rows: more fresh groups than the
    page size, with the stale ones behind them. Newest-first would return only fresh groups."""
    for i in range(12):
        _write_group(test_neo4j_repo, f"g-fresh-{i:02d}", days_old=1, members=1)
    for i in range(2):
        _write_group(test_neo4j_repo, f"g-stale-{i}", days_old=200, members=1)

    adapter = _RepoAdapter(conflict_repo)

    resolved = _Service(adapter).auto_resolve_stale_conflicts(max_age_days=14, limit=10)

    assert resolved == 2
    assert sorted(adapter.resolved) == ["g-stale-0", "g-stale-1"]


def test_the_cutoff_is_applied_in_the_database_not_only_in_python(
    test_neo4j_repo, conflict_repo
) -> None:
    """The push-down itself: the query must return only rows past the cutoff."""
    _write_group(test_neo4j_repo, "g-old", days_old=90)
    _write_group(test_neo4j_repo, "g-new", days_old=1)

    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    rows = conflict_repo.list_conflict_groups(
        status="unresolved", limit=50, created_before=cutoff, oldest_first=True,
    )

    assert [r["group_id"] for r in rows] == ["g-old"]


def test_default_callers_still_get_every_group_newest_first(
    test_neo4j_repo, conflict_repo
) -> None:
    """POSITIVE CONTROL on the live graph: the MCP/explorer listing is untouched."""
    _write_group(test_neo4j_repo, "g-old", days_old=90)
    _write_group(test_neo4j_repo, "g-new", days_old=1)

    rows = conflict_repo.list_conflict_groups(status="unresolved", limit=50)

    assert [r["group_id"] for r in rows] == ["g-new", "g-old"]
