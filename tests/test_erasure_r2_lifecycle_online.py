"""R2 CF-338 online counterexamples: lifecycle compression and rehydration cannot write erased
text back.

Both writers copy text read earlier (the decay sweep's content, the archived original merged with
new context) into a node. Graph seeded in Graphiti's shape; no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.graph_text_inventory import ABSORBED_BLOCK_SEPARATOR
from tests._erasure_canary import all_hits, new_canary
from tests.test_erasure_rebuild_p0_scrub_online import _adapter_and_coordinator, _u

pytestmark = pytest.mark.online


def _props(repo: Any, uuid: str) -> dict[str, Any]:
    rows = repo.execute("MATCH (e:Entity {uuid:$u}) RETURN properties(e) AS p", params={"u": uuid})
    return dict(rows[0]["p"]) if rows else {}


def _epoch(repo: Any, ns: str) -> int:
    rows = repo.execute(
        "MATCH (f:EvidenceNamespaceFence {namespace_key:$k}) RETURN coalesce(f.erasure_epoch,0) AS e",
        params={"k": ns},
    )
    return int(rows[0]["e"]) if rows else 0


def _seed_holder(repo: Any, ns: str, canary: str, *, freshness: str) -> dict[str, str]:
    """X holds an absorbed block of Y (whose text is the canary)."""
    ids = {"x": _u(), "y": _u(), "ns": ns}
    content = f"X text{ABSORBED_BLOCK_SEPARATOR}{ids['y']}) on 2026-01-01 ---\nY said {canary}"
    repo.execute(
        """
        CREATE (:Entity {uuid:$x, name:'holder', group_id:$ns, namespace:$ns, scope:'PERSISTENT',
                         freshness:$fresh, content:$content, summary:'x summary',
                         absorbed_from:[$y], created_at:datetime()})
        CREATE (:Entity {uuid:$y, name:'absorbed', group_id:$ns, namespace:$ns,
                         scope:'PERSISTENT', content:$y_text, created_at:datetime()})
        """,
        params={**ids, "fresh": freshness, "content": content, "y_text": f"Y said {canary}"},
    )
    if freshness == "COMPRESSED":
        repo.execute(
            "MATCH (e:Entity {uuid:$x}) SET e.original_content = e.content, e.content = 'gist'",
            params=ids,
        )
    return ids


def test_cf338_compress_from_a_pre_erase_read_is_refused(test_neo4j_repo, tmp_path) -> None:
    """Decay reads X (epoch E), Y is erased (X's Y block cut), then the summary the LLM made from
    the stale read -- quoting Y -- would land on X. Fails if compress_node bypasses G-copy and
    drops its ``rederive_pending`` guard."""
    repo = test_neo4j_repo
    ns, canary = f"r2-338c-{_u()}", new_canary("CF338C")
    ids = _seed_holder(repo, ns, canary, freshness="ACTIVE")
    adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)
    read_epoch = _epoch(repo, ns)

    assert coord.erase_memory(ids["y"])["reason"] == "erased"
    ok = adapter.compress_node(ids["x"], f"summary: Y said {canary}", read_epoch=read_epoch,
                               fence_key=ns)

    assert ok is False
    assert all_hits(repo, db, canary) == []
    assert _props(repo, ids["x"])["freshness"] == "ACTIVE"


def test_cf338_compress_epoch_check_alone_refuses_a_stale_summary(test_neo4j_repo, tmp_path) -> None:
    """An erase in X's namespace that does not touch X still moves the epoch: the summary from the
    pre-erase read is refused. Fails if the epoch read by the sweep is not compared at write."""
    repo = test_neo4j_repo
    ns = f"r2-338e-{_u()}"
    x, other = _u(), _u()
    repo.execute(
        """
        CREATE (:Entity {uuid:$x, name:'x', group_id:$ns, namespace:$ns, scope:'PERSISTENT',
                         freshness:'ACTIVE', content:'x body', created_at:datetime()})
        CREATE (:Entity {uuid:$o, name:'o', group_id:$ns, namespace:$ns, scope:'PERSISTENT',
                         content:'o body', created_at:datetime()})
        """,
        params={"x": x, "o": other, "ns": ns},
    )
    adapter, coord, _ = _adapter_and_coordinator(repo, tmp_path)
    read_epoch = _epoch(repo, ns)
    coord.erase_memory(other)

    assert adapter.compress_node(x, "x gist", read_epoch=read_epoch, fence_key=ns) is False
    # The positive control: a fresh read compresses.
    assert adapter.compress_node(x, "x gist", read_epoch=_epoch(repo, ns), fence_key=ns) is True
    assert _props(repo, x)["freshness"] == "COMPRESSED"


def test_cf338_decay_sweep_carries_the_epoch_and_skips_scrubbed_nodes(test_neo4j_repo) -> None:
    """Fails if fetch_decay_candidates stops returning the fence key/epoch read with the content,
    or selects a node waiting for rederive."""
    repo = test_neo4j_repo
    ns = f"r2-338s-{_u()}"
    live, pending = _u(), _u()
    old = "datetime() - duration({days: 400})"
    repo.execute(
        f"""
        CREATE (:Entity {{uuid:$l, name:'l', group_id:$ns, namespace:$ns, scope:'PERSISTENT',
                         freshness:'ACTIVE', content:'l', created_at:{old},
                         last_accessed_at:{old}}})
        CREATE (:Entity {{uuid:$p, name:'p', group_id:$ns, namespace:$ns, scope:'PERSISTENT',
                         freshness:'ACTIVE', content:'p', created_at:{old},
                         last_accessed_at:{old}, rederive_pending:true}})
        """,
        params={"l": live, "p": pending, "ns": ns},
    )
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    rows = MemoryGraphAdapter(neo4j=repo).fetch_decay_candidates(
        "ACTIVE", min_days_since_accessed=30, max_edge_count=1000
    )
    by_uuid = {r["uuid"]: r for r in rows}
    assert pending not in by_uuid
    # No fence node yet: the epoch reads as None, which the sweep treats as 0.
    assert by_uuid[live]["fence_key"] == ns and "fence_epoch" in by_uuid[live]


def test_cf338_rehydration_of_a_scrubbed_node_is_refused(test_neo4j_repo, tmp_path) -> None:
    """X was compressed holding Y's block in original_content; Y is erased (cut, rederive pending);
    a rehydration merge made from the stale archive would restore Y's text. Fails if
    complete_rehydration with content drops the ``rederive_pending`` guard."""
    repo = test_neo4j_repo
    ns, canary = f"r2-338r-{_u()}", new_canary("CF338R")
    ids = _seed_holder(repo, ns, canary, freshness="COMPRESSED")
    adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)

    coord.erase_memory(ids["y"])
    ok = adapter.complete_rehydration(ids["x"], f"merged: Y said {canary}")

    assert ok is False
    assert all_hits(repo, db, canary) == []


def test_cf338_rehydration_merging_an_erased_episode_is_refused(test_neo4j_repo, tmp_path) -> None:
    """The merge's new context came from episode E; E is erased before the write. Fails if
    complete_rehydration does not pass the context's source to G-copy (the source marker)."""
    repo = test_neo4j_repo
    ns, canary = f"r2-338m-{_u()}", new_canary("CF338M")
    x, ep = _u(), _u()
    repo.execute(
        """
        CREATE (:Entity {uuid:$x, name:'x', group_id:$ns, namespace:$ns, scope:'PERSISTENT',
                         freshness:'COMPRESSED', content:'gist', original_content:'x body',
                         created_at:datetime()})
        CREATE (:Episodic {uuid:$ep, name:'e', group_id:$ns, namespace:$ns, content:$c,
                           source_description:'t', created_at:datetime(), valid_at:datetime()})
        """,
        params={"x": x, "ep": ep, "ns": ns, "c": f"episode {canary}"},
    )
    adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)

    assert coord.erase_memory(ep)["reason"] == "erased"
    ok = adapter.complete_rehydration(x, f"x body + {canary}", source_uuids=(ep,))

    assert ok is False
    assert all_hits(repo, db, canary) == []
    # A state-only rehydration (no copied text) still works.
    assert adapter.complete_rehydration(x) is True
    assert _props(repo, x)["freshness"] == "ACTIVE"
