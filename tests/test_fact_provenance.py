"""Durable fact provenance (Plan 2 step 5 / Part D).

A fact's evidence used to live ONLY in (:Episodic)-[:MENTIONS]->(fact) edges, so when an episode was
reaped the fact silently lost its provenance while its summary kept claiming the supporting events
(prod has facts asserting "2 supporting events" with zero surviving MENTIONS). A fact version now
stores a durable, sorted, deduplicated `episode_uuids` list plus an exact `supporting_event_count`,
and an unchanged-value rewrite UNIONS the incoming UUIDs into it inside a single Cypher statement.

See workspace-root .agent/plans/menhir-metric-provenance-redesign.md (Part D).
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from concurrent.futures import ThreadPoolExecutor

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.view_repository import (
    _COUNT_TOKEN,
    CounterKind,
    _checked_template,
    _normalize_episode_uuids,
)


# ------------------------------------------------------------------------------- unit: normal form


@pytest.mark.unit
def test_episode_uuids_normalized_sorted_deduped_and_blank_free():
    assert _normalize_episode_uuids(["b", "a", "b", "", "  ", None, " c "]) == ["a", "b", "c"]
    assert _normalize_episode_uuids(None) == []


@pytest.mark.unit
def test_counter_summary_template_carries_the_count_token():
    """The template is what Cypher fills in on the unchanged-value refresh. It must differ from the
    rendered summary ONLY at the count -- otherwise the refresh would rewrite the value or date."""
    kind = CounterKind()
    payload = {"counter": "movies", "value": 20.0, "valid_at": "2026-07-01T00:00:00+00:00",
               "episode_uuids": ["e1", "e2"]}
    _, summary = kind.surface("user", payload)
    tpl = kind.summary_template("user", payload)

    assert "2 supporting event(s)" in summary
    assert _COUNT_TOKEN in tpl
    assert tpl.replace(_COUNT_TOKEN, "2") == summary


@pytest.mark.unit
def test_template_is_discarded_when_it_cannot_reconstruct_the_summary():
    """The count token must appear ONLY where the count goes. A counter whose text contains the
    token would let Cypher rewrite the fact's own summary text -- so the template is verified against
    the rendered summary and dropped if it does not reproduce it exactly."""
    kind = CounterKind()
    payload = {"counter": _COUNT_TOKEN, "value": 1.0, "valid_at": "2026-07-01T00:00:00+00:00",
               "episode_uuids": ["e1"]}
    _, summary = kind.surface("user", payload)

    assert _checked_template(kind, "user", payload, summary, 1) is None   # poisoned -> discarded

    clean = {"counter": "movies", "value": 1.0, "valid_at": "2026-07-01T00:00:00+00:00",
             "episode_uuids": ["e1"]}
    _, clean_summary = kind.surface("user", clean)
    assert _checked_template(kind, "user", clean, clean_summary, 1) is not None


# --------------------------------------------------------------------------- online: durable + union


def _make_episodes(repo, ns: str, uuids: list[str]) -> None:
    repo.execute(
        "UNWIND $eps AS eid CREATE (e:Episodic {uuid: eid, group_id: $ns, content: 'x'})",
        {"eps": uuids, "ns": ns},
    )


def _fact(repo, key: str) -> dict:
    rows = repo.execute(
        "MATCH (n:Entity {view_key:$k}) WHERE coalesce(n.view_current, true) "
        "RETURN n.uuid AS uuid, n.episode_uuids AS eps, "
        "n.supporting_event_count AS count, n.summary AS summary, n.view_value AS value, "
        "n.view_sig AS sig, toString(n.valid_at) AS valid_at, "
        "COUNT { (:Episodic)-[:MENTIONS]->(n) } AS mentions",
        {"k": key},
    )
    return dict(rows[0]) if rows else {}


@pytest.mark.online
def test_fact_stores_durable_episode_uuids_and_exact_count(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"prov-{uuidlib.uuid4().hex[:8]}"
    eps = [f"{ns}-e2", f"{ns}-e1"]
    _make_episodes(test_neo4j_repo, ns, eps)

    adapter.record_counter(subject="user", counter="movies", value=20.0, namespace=ns,
                           episode_uuids=eps + [eps[0]])  # duplicate must collapse

    got = _fact(test_neo4j_repo, f"{ns}::user::movies")
    assert got["eps"] == sorted(eps)          # stored sorted + deduplicated
    assert got["count"] == 2                  # exact length of the stored list
    assert "2 supporting event(s)" in got["summary"]
    assert got["mentions"] == 2


@pytest.mark.online
def test_unchanged_value_rewrite_unions_provenance_and_keeps_the_version(test_neo4j_repo):
    """The D2 contract: a same-value rewrite with NEW evidence must union the UUIDs, refresh the
    count and summary, and leave the value, signature, valid_at, and node identity untouched."""
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"prov-{uuidlib.uuid4().hex[:8]}"
    first, second = [f"{ns}-a"], [f"{ns}-b"]
    _make_episodes(test_neo4j_repo, ns, first + second)

    adapter.record_counter(subject="user", counter="movies", value=20.0, namespace=ns,
                           valid_at="2026-07-01T00:00:00+00:00", episode_uuids=first)
    before = _fact(test_neo4j_repo, f"{ns}::user::movies")

    adapter.record_counter(subject="user", counter="movies", value=20.0, namespace=ns,
                           valid_at="2026-07-01T00:00:00+00:00", episode_uuids=second)
    after = _fact(test_neo4j_repo, f"{ns}::user::movies")

    assert after["uuid"] == before["uuid"]                 # no new version
    assert after["value"] == before["value"]
    assert after["sig"] == before["sig"]
    assert after["valid_at"] == before["valid_at"]
    assert after["eps"] == sorted(first + second)          # unioned, sorted, deduped
    assert after["count"] == 2
    assert "2 supporting event(s)" in after["summary"]     # summary regenerated with the new count
    assert after["mentions"] == 2                          # MENTIONS merged for the new episode

    # Idempotent: replaying the same evidence changes nothing.
    adapter.record_counter(subject="user", counter="movies", value=20.0, namespace=ns,
                           valid_at="2026-07-01T00:00:00+00:00", episode_uuids=second)
    again = _fact(test_neo4j_repo, f"{ns}::user::movies")
    assert again["eps"] == after["eps"] and again["count"] == 2 and again["mentions"] == 2


@pytest.mark.online
def test_missing_episodes_stay_in_the_durable_list_and_keep_counting(test_neo4j_repo):
    """D3: a reaped episode does NOT silently drain the fact's evidence. The UUID stays in the list
    and keeps counting; MENTIONS is repaired if the episode ever comes back."""
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"prov-{uuidlib.uuid4().hex[:8]}"
    present, reaped = f"{ns}-here", f"{ns}-gone"
    _make_episodes(test_neo4j_repo, ns, [present])        # `reaped` never exists

    res = adapter.record_counter(subject="user", counter="bike_spend", value=500.0, namespace=ns,
                                 episode_uuids=[present, reaped])

    assert res["episodes_present"] == 1
    assert res["episodes_missing"] == 1                    # reported, not silently dropped
    got = _fact(test_neo4j_repo, f"{ns}::user::bike_spend")
    assert got["eps"] == sorted([present, reaped])         # missing UUID retained
    assert got["count"] == 2                               # and still counted
    assert got["mentions"] == 1                            # only the surviving episode is linked

    # The episode returns -> the next refresh repairs MENTIONS without touching the version.
    _make_episodes(test_neo4j_repo, ns, [reaped])
    adapter.record_counter(subject="user", counter="bike_spend", value=500.0, namespace=ns,
                           episode_uuids=[reaped])
    repaired = _fact(test_neo4j_repo, f"{ns}::user::bike_spend")
    assert repaired["uuid"] == got["uuid"]
    assert repaired["mentions"] == 2
    assert repaired["count"] == 2


@pytest.mark.online
def test_concurrent_refreshes_do_not_lose_episode_uuids(test_neo4j_repo):
    """The D2 atomicity claim, actually exercised.

    Neo4j is read-committed and does NOT prevent lost updates: being a single statement is not
    enough, because the property read in the UNWIND happens before the SET takes the write lock.
    Without the leading lock-taking SET, N concurrent same-value refreshes each read the same base
    list and the last writer wins -- silently dropping the others' evidence. Every UUID must survive.
    """
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"prov-{uuidlib.uuid4().hex[:8]}"
    eps = [f"{ns}-e{i}" for i in range(12)]
    _make_episodes(test_neo4j_repo, ns, eps)

    # Seed the fact, then refresh it concurrently, each writer contributing ONE distinct episode.
    adapter.record_counter(subject="user", counter="movies", value=20.0, namespace=ns,
                           valid_at="2026-07-01T00:00:00+00:00", episode_uuids=[])

    def _refresh(ep: str) -> None:
        adapter.record_counter(subject="user", counter="movies", value=20.0, namespace=ns,
                               valid_at="2026-07-01T00:00:00+00:00", episode_uuids=[ep])

    with ThreadPoolExecutor(max_workers=len(eps)) as pool:
        list(pool.map(_refresh, eps))

    got = _fact(test_neo4j_repo, f"{ns}::user::movies")
    assert got["eps"] == sorted(eps)        # no writer's UUID was clobbered by another
    assert got["count"] == len(eps)
    assert f"{len(eps)} supporting event(s)" in got["summary"]
    assert got["mentions"] == len(eps)


@pytest.mark.online
def test_superseding_value_gets_version_local_evidence(test_neo4j_repo):
    """D1: evidence is version-local. A new value does NOT inherit the old value's episode UUIDs."""
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"prov-{uuidlib.uuid4().hex[:8]}"
    old_ep, new_ep = f"{ns}-old", f"{ns}-new"
    _make_episodes(test_neo4j_repo, ns, [old_ep, new_ep])

    adapter.record_counter(subject="user", counter="movies", value=25.0, namespace=ns,
                           valid_at="2026-07-01T00:00:00+00:00", episode_uuids=[old_ep])
    adapter.record_counter(subject="user", counter="movies", value=20.0, namespace=ns,
                           valid_at="2026-07-02T00:00:00+00:00", episode_uuids=[new_ep])

    current = _fact(test_neo4j_repo, f"{ns}::user::movies")
    assert current["value"] == 20.0
    assert current["eps"] == [new_ep]      # NOT unioned across values
    assert current["count"] == 1
