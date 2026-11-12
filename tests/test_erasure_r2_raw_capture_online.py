"""R2 CF-327 online counterexamples: a failed episode's raw capture never outlives its erase.

Production paths: ``fail_exhausted_pending_episodes`` (fetch, then one raw capture per row) and
``create_raw_capture_entity``. Graph seeded in Graphiti's shape; no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.erasure_fence import release_memory, seal_memory
from tests._erasure_canary import all_hits, graph_hits, new_canary
from tests.test_erasure_rebuild_p0_scrub_online import _adapter_and_coordinator, _u

pytestmark = pytest.mark.online


def _seed_exhausted(repo: Any, ns: str, canary: str) -> str:
    q = _u()
    repo.execute(
        "CREATE (:Episodic {uuid:$q, namespace:$ns, group_id:$ns, processing_state:'PENDING', "
        "processing_attempts:9, content:$c, session_id:'s', user_id:'u', source:'test', "
        "queued_at:datetime()})",
        params={"q": q, "ns": ns, "c": canary},
    )
    return q


def _raw_captures(repo: Any, q: str) -> int:
    return repo.execute(
        "MATCH (n:Entity {raw_capture_for:$q}) RETURN count(n) AS c", params={"q": q}
    )[0]["c"]


def test_cf327_erase_between_fetch_and_capture_leaves_no_entity(
    test_neo4j_repo, tmp_path, monkeypatch
) -> None:
    """The sweep fetched Q's content, then Q was erased, then the sweep's MERGE runs. Nothing may
    be created. Fails if ``create_raw_capture_entity`` stops running under ``execute_copy_fenced``
    (or stops requiring the episode to exist and not be a tombstone)."""
    repo = test_neo4j_repo
    ns, canary = f"r2-327-{_u()}", new_canary("CF327")
    q = _seed_exhausted(repo, ns, canary)
    adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)
    episodes = adapter._episodes
    real_fetch = episodes.fetch_exhausted_pending_episodes
    erased: list[dict[str, Any]] = []

    def fetch_then_erase(**kwargs: Any) -> list[dict[str, Any]]:
        rows = [r for r in real_fetch(**kwargs) if r["episode_uuid"] == q]
        assert rows and rows[0]["content"] == canary, rows
        erased.append(coord.erase_memory(q))
        return rows

    monkeypatch.setattr(episodes, "fetch_exhausted_pending_episodes", fetch_then_erase)

    adapter.fail_exhausted_pending_episodes(max_attempts=3)

    assert erased and erased[0]["reason"] in {"erased", "erased_incomplete"}, erased
    assert _raw_captures(repo, q) == 0
    assert all_hits(repo, db, canary) == []


def test_cf327_capture_while_an_erase_holds_the_fence_is_refused(test_neo4j_repo, tmp_path) -> None:
    """Q is sealed (an erase in flight, its marker written) but not yet cascaded; a capture now
    must be refused rather than land after the seal read its closure. Fails if the marker or
    erasing_ops check is bypassed."""
    repo = test_neo4j_repo
    ns, canary = f"r2-327s-{_u()}", new_canary("CF327S")
    q = _seed_exhausted(repo, ns, canary)
    adapter, _coord, _db = _adapter_and_coordinator(repo, tmp_path)
    op = _u()
    seal_memory(repo, op, [q])
    try:
        out = adapter.create_raw_capture_entity(
            episode_uuid=q, name="raw", content=canary, namespace=ns,
            session_id="s", user_id="u", source="test",
        )
        assert out is None
        assert _raw_captures(repo, q) == 0
    finally:
        release_memory(repo, op)


def test_cf327_seal_closure_and_cascade_select_the_raw_capture(test_neo4j_repo, tmp_path) -> None:
    """A raw capture written by the legacy two-statement writer has no MENTIONS link. The seal
    closure lists it and the erase deletes it. Fails if the closure or the cascade stops selecting
    ``raw_capture_for``."""
    repo = test_neo4j_repo
    ns, canary = f"r2-327l-{_u()}", new_canary("CF327L")
    q = _u()
    rc = _u()
    repo.execute(
        "CREATE (:Episodic {uuid:$q, namespace:$ns, group_id:$ns, processing_state:'FAILED', "
        "content:$c}) "
        "CREATE (:Entity {uuid:$rc, raw_capture_for:$q, raw_capture:true, name:'raw', "
        "content:$c, namespace:$ns, group_id:$ns, scope:'SESSION'})",
        params={"q": q, "rc": rc, "ns": ns, "c": canary},
    )
    op = _u()
    try:
        sealed = seal_memory(repo, op, [q])
        assert rc in sealed.closure_uuids, sealed
    finally:
        release_memory(repo, op)
    _adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)

    out = coord.erase_memory(q)

    assert out["reason"] == "erased", out
    assert _raw_captures(repo, q) == 0
    assert graph_hits(repo, canary) == [] and all_hits(repo, db, canary) == []


def test_cf327_capture_of_a_live_episode_still_links_mentions(test_neo4j_repo, tmp_path) -> None:
    """The fenced writer still works when nothing is being erased: one entity, linked by MENTIONS
    in the same statement. Fails if the gate refuses an unfenced namespace or drops the link."""
    repo = test_neo4j_repo
    ns, canary = f"r2-327ok-{_u()}", new_canary("CF327OK")
    q = _seed_exhausted(repo, ns, canary)
    adapter, _coord, _db = _adapter_and_coordinator(repo, tmp_path)
    out = adapter.create_raw_capture_entity(
        episode_uuid=q, name="raw", content=canary, namespace=ns,
        session_id="s", user_id="u", source="test",
    )
    assert out
    rows = repo.execute(
        "MATCH (:Episodic {uuid:$q})-[:MENTIONS]->(n:Entity {raw_capture_for:$q}) "
        "RETURN n.uuid AS u",
        params={"q": q},
    )
    assert [r["u"] for r in rows] == [out]


def test_cf327_cascade_alone_deletes_an_unlinked_raw_capture(test_neo4j_repo) -> None:
    """The cascade (without a seal closure) still deletes a raw capture that has no MENTIONS link.
    Fails if the cascade's ``raw_capture_for`` stage is removed."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    repo = test_neo4j_repo
    ns, q, rc = f"r2-327c-{_u()}", _u(), _u()
    repo.execute(
        "CREATE (:Episodic {uuid:$q, namespace:$ns, group_id:$ns, processing_state:'FAILED', "
        "content:'x'}) "
        "CREATE (:Entity {uuid:$rc, raw_capture_for:$q, raw_capture:true, name:'raw', "
        "content:'x', namespace:$ns, group_id:$ns, scope:'SESSION'})",
        params={"q": q, "rc": rc, "ns": ns},
    )
    result = MemoryGraphAdapter(neo4j=repo)._memory_queries.delete_memory_with_scalar_cascade(
        q, operation_id=_u(), erased_uuids=[q], deletable_entity_uuids=[]
    )
    assert rc in result["derived_entities_deleted_uuids"], result
    assert _raw_captures(repo, q) == 0
