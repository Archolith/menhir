"""#925 live: the retry candidate query skips a row parked for its current failure only.

Online because the exclusion compares a stored string against `toString(coalesce(...))` of a
Neo4j datetime, and the park write is fenced on that same value.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository

pytestmark = pytest.mark.online


@pytest.fixture
def graph(test_neo4j_repo):
    tag = f"i925-{uuidlib.uuid4().hex[:10]}"
    u = {k: f"{tag}-{k}" for k in ("parked", "open")}
    test_neo4j_repo.execute(
        """
        UNWIND $rows AS row
        CREATE (n:Episodic {uuid: row.uuid, name: row.uuid, namespace: $ns, content: 'x',
                            processing_state: 'FAILED', processing_attempts: 1,
                            processing_error: 'Failed to extract JSON from LLM response',
                            created_at: datetime(row.at), processing_completed_at: datetime(row.at),
                            test_tag: $tag})
        """,
        params={
            "ns": f"{tag}-ns",
            "tag": tag,
            "rows": [
                {"uuid": u["parked"], "at": "1990-01-01T00:00:00Z"},
                {"uuid": u["open"], "at": "1990-01-02T00:00:00Z"},
            ],
        },
    )
    repo = EpisodeLifecycleRepository()
    repo.neo4j = test_neo4j_repo
    try:
        yield repo, test_neo4j_repo, u
    finally:
        test_neo4j_repo.execute("MATCH (n {test_tag: $t}) DETACH DELETE n", params={"t": tag})


def _candidates(repo: EpisodeLifecycleRepository, u: dict) -> dict[str, dict]:
    wanted = set(u.values())
    return {
        r["uuid"]: r
        for r in repo.fetch_failed_episode_retry_candidates(limit=500)
        if r["uuid"] in wanted
    }


def test_parked_row_is_skipped_until_it_fails_again(graph) -> None:
    repo, _neo4j, u = graph

    before = _candidates(repo, u)
    assert set(before) == {u["parked"], u["open"]}

    failed_at = str(before[u["parked"]]["processing_completed_at"])
    assert repo.park_failed_episode_retry(u["parked"], failed_at=failed_at, reason="terminal")
    assert set(_candidates(repo, u)) == {u["open"]}

    # A fresh failure stamps a new processing_completed_at; the stale marker no longer matches.
    assert repo.mark_episode_failed(u["parked"], "Failed to load model")
    assert set(_candidates(repo, u)) == {u["parked"], u["open"]}


def test_park_is_a_noop_for_a_failure_it_was_not_judged_for(graph) -> None:
    repo, _neo4j, u = graph

    assert not repo.park_failed_episode_retry(
        u["parked"], failed_at="1970-01-01T00:00:00Z", reason="terminal"
    )
    assert u["parked"] in _candidates(repo, u)


def test_park_is_a_noop_once_the_row_left_failed(graph) -> None:
    repo, neo4j, u = graph
    failed_at = str(_candidates(repo, u)[u["parked"]]["processing_completed_at"])
    neo4j.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.processing_state = 'PENDING'",
        params={"u": u["parked"]},
    )

    assert not repo.park_failed_episode_retry(u["parked"], failed_at=failed_at, reason="terminal")


def test_park_expires_after_the_recheck_window(graph) -> None:
    repo, neo4j, u = graph
    failed_at = str(_candidates(repo, u)[u["parked"]]["processing_completed_at"])
    assert repo.park_failed_episode_retry(u["parked"], failed_at=failed_at, reason="exhausted")
    assert u["parked"] not in _candidates(repo, u)

    neo4j.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.retry_parked_at = datetime() - duration({days: 2})",
        params={"u": u["parked"]},
    )

    assert u["parked"] in _candidates(repo, u)


def test_parked_row_stays_failed_for_other_readers(graph) -> None:
    repo, _neo4j, u = graph
    failed_at = str(_candidates(repo, u)[u["parked"]]["processing_completed_at"])
    assert repo.park_failed_episode_retry(u["parked"], failed_at=failed_at, reason="terminal")

    row = repo.fetch_episode_processing(u["parked"])
    assert row is not None and row["processing_state"] == "FAILED"


def _park(repo: EpisodeLifecycleRepository, u: dict) -> None:
    failed_at = str(_candidates(repo, u)[u["parked"]]["processing_completed_at"])
    assert repo.park_failed_episode_retry(u["parked"], failed_at=failed_at, reason="terminal")
    assert u["parked"] not in _candidates(repo, u)


def test_1007_due_parked_row_sorts_after_an_unparked_newer_failure(graph) -> None:
    repo, neo4j, u = graph
    _park(repo, u)
    neo4j.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.retry_parked_at = datetime() - duration({days: 2})",
        params={"u": u["parked"]},
    )

    # The parked row failed first, so failure-time order alone would put it ahead.
    order = [
        r["uuid"]
        for r in repo.fetch_failed_episode_retry_candidates(limit=500)
        if r["uuid"] in set(u.values())
    ]
    assert order == [u["open"], u["parked"]]


def test_1007_parked_row_with_a_twin_link_is_readmitted(graph) -> None:
    repo, neo4j, u = graph
    _park(repo, u)
    twin = f"{u['parked']}-twin"
    neo4j.execute(
        """
        MATCH (q:Episodic {uuid: $q})
        CREATE (g:Episodic {uuid: $g, name: $g, namespace: q.namespace, content: 'x',
                            test_tag: q.test_tag})
        // A finished publish (#905): the link records a completing extraction outcome.
        SET q.resolved_episode_uuid = $g, q.resolved_extraction_outcome = 'extracted'
        """,
        params={"q": u["parked"], "g": twin},
    )

    assert u["parked"] in _candidates(repo, u)
    assert repo.linked_twin_artifact(u["parked"]) is not None


def test_1007_parked_row_linked_to_a_missing_twin_stays_parked(graph) -> None:
    repo, neo4j, u = graph
    _park(repo, u)
    neo4j.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.resolved_episode_uuid = $g",
        params={"u": u["parked"], "g": f"{u['parked']}-missing"},
    )

    assert u["parked"] not in _candidates(repo, u)
    assert repo.linked_twin_artifact(u["parked"]) is None


def test_1007_completed_link_readmits_a_terminal_park_but_not_an_exhausted_one(graph) -> None:
    """Tier 1 is for a row the worker can still claim; an exhausted park waits for its recheck."""
    repo, neo4j, u = graph
    candidates = _candidates(repo, u)
    for key, reason in (("parked", "terminal"), ("open", "exhausted")):
        failed_at = str(candidates[u[key]]["processing_completed_at"])
        assert repo.park_failed_episode_retry(u[key], failed_at=failed_at, reason=reason)
        neo4j.execute(
            """
            MATCH (q:Episodic {uuid: $q})
            CREATE (g:Episodic {uuid: $g, name: $g, namespace: q.namespace, content: 'x',
                                test_tag: q.test_tag})
            SET q.resolved_episode_uuid = $g, q.resolved_extraction_outcome = 'extracted'
            """,
            params={"q": u[key], "g": f"{u[key]}-twin"},
        )

    assert set(_candidates(repo, u)) == {u["parked"]}

    # Once its recheck is due, the exhausted row is a tier-2 candidate like any other park.
    neo4j.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.retry_parked_at = datetime() - duration({days: 2})",
        params={"u": u["open"]},
    )
    assert set(_candidates(repo, u)) == {u["parked"], u["open"]}
