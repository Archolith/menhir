"""R2 CF-332 online counterexamples: text absorbed by conflict resolution is erased with its source.

Production writer: ``resolve_conflict_group(action='replace')`` appends the removed member's
content to the kept entity. Graph seeded in Graphiti's shape; no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.graph_text_inventory import ABSORBED_BLOCK_SEPARATOR
from tests._erasure_canary import all_hits, new_canary
from tests.test_erasure_rebuild_p0_scrub_online import _adapter_and_coordinator, _seed_memory, _u

pytestmark = pytest.mark.online


def _seed_group(repo: Any, ns: str, keep_text: str, remove_text: str) -> dict[str, str]:
    ids = {"a": _u(), "b": _u(), "g": _u(), "ns": ns}
    repo.execute(
        """
        CREATE (:Entity {uuid:$a, name:'keeper', group_id:$ns, namespace:$ns, scope:'PERSISTENT',
                         content:$keep, summary:'keeper summary', conflict_group_id:$g,
                         created_at:datetime()})
        CREATE (:Entity {uuid:$b, name:'superseded', group_id:$ns, namespace:$ns,
                         scope:'PERSISTENT', content:$remove, summary:$remove,
                         conflict_group_id:$g, created_at:datetime()})
        """,
        params={**ids, "keep": keep_text, "remove": remove_text},
    )
    return ids


def _props(repo: Any, uuid: str) -> dict[str, Any]:
    rows = repo.execute("MATCH (e:Entity {uuid:$u}) RETURN properties(e) AS p", params={"u": uuid})
    return dict(rows[0]["p"]) if rows else {}


def test_cf332_absorbed_text_is_cut_from_the_holder_when_its_source_is_erased(
    test_neo4j_repo, tmp_path
) -> None:
    """Absorb B into A, erase B: A keeps its own text, loses B's block, is flagged, and no store
    holds the canary. Fails if absorption stops recording ``absorbed_from`` and the marker cut is
    removed, or if the cascade stops scrubbing holders."""
    repo = test_neo4j_repo
    ns, canary = f"r2-332-{_u()}", new_canary("CF332")
    ids = _seed_group(repo, ns, "A's own memory text", f"B says {canary}")
    adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)

    adapter.resolve_conflict_group(ids["g"], "replace", keep_uuid=ids["a"], remove_uuid=ids["b"],
                                   namespace=ns)
    a = _props(repo, ids["a"])
    assert canary in a["content"] and a["absorbed_from"] == [ids["b"]], a

    out = coord.erase_memory(ids["b"])

    assert out["reason"] == "erased", out
    assert all_hits(repo, db, canary) == []
    a = _props(repo, ids["a"])
    assert a["content"] == "A's own memory text", a
    assert a["rederive_pending"] is True and a.get("absorbed_from") == [], a
    assert out["derived"]["entities_flagged"] >= 1, out["derived"]


def test_cf332_legacy_absorbed_block_without_absorbed_from_is_cut(test_neo4j_repo, tmp_path) -> None:
    """A holder written before ``absorbed_from`` existed is found by its marker text; a second block
    from a live memory stays. Fails if the namespace-scoped marker fallback is removed, or if the cut
    drops blocks of memories that were not erased."""
    repo = test_neo4j_repo
    ns, canary = f"r2-332l-{_u()}", new_canary("CF332L")
    b, live = _u(), _u()
    content = (
        "own text"
        f"{ABSORBED_BLOCK_SEPARATOR}{b}) on 2026-01-01 ---\nerased {canary}"
        f"{ABSORBED_BLOCK_SEPARATOR}{live}) on 2026-01-02 ---\nlive block"
    )
    a, compressed = _u(), _u()
    other_ns_holder = _u()
    repo.execute(
        """
        CREATE (:Entity {uuid:$b, name:'gone', group_id:$ns, namespace:$ns, content:'b body'})
        CREATE (:Entity {uuid:$a, name:'holder', group_id:$ns, namespace:$ns, content:$content,
                         summary:'mixes both'})
        CREATE (:Entity {uuid:$o, name:'foreign', group_id:$ons, namespace:$ons, content:$content})
        CREATE (:Entity {uuid:$k, name:'compressed', group_id:$ns, namespace:$ns,
                         content:'gist', original_content:$content})
        """,
        params={"a": a, "b": b, "o": other_ns_holder, "k": compressed, "ns": ns,
                "ons": f"{ns}-other", "content": content},
    )
    _, coord, _ = _adapter_and_coordinator(repo, tmp_path)

    coord.erase_memory(b)

    holder = _props(repo, a)
    assert holder["content"] == (
        f"own text{ABSORBED_BLOCK_SEPARATOR}{live}) on 2026-01-02 ---\nlive block"
    ), holder
    assert holder["rederive_pending"] is True and holder["summary"] == "", holder
    # Compressed after absorbing: the block survives only in original_content (compress_node).
    assert canary not in (_props(repo, compressed).get("original_content") or "")
    # Another namespace's copy is out of this erase's scope (the plan's namespace-scoped fallback).
    assert _props(repo, other_ns_holder)["content"] == content


def test_cf332_absorption_refused_when_an_erase_lands_between_read_and_write(
    test_neo4j_repo, tmp_path, monkeypatch
) -> None:
    """G-copy: A holds C's absorbed block. Resolution reads A, then C is erased (A's C block cut),
    then the absorption write would put A's stale content -- with C's text -- back. The epoch read
    before A was read refuses that write. Fails if the epoch is captured at write time, or if the
    absorption write bypasses ``execute_copy_fenced``."""
    repo = test_neo4j_repo
    ns, canary = f"r2-332r-{_u()}", new_canary("CF332R")
    c = _u()
    ids = _seed_group(
        repo, ns,
        f"A text{ABSORBED_BLOCK_SEPARATOR}{c}) on 2026-01-01 ---\nC said {canary}",
        "B body",
    )
    repo.execute(
        "CREATE (:Entity {uuid:$c, name:'c', group_id:$ns, namespace:$ns, content:'c body'})",
        params={"c": c, "ns": ns},
    )
    adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)
    real_execute = repo.execute
    erased: list[dict[str, Any]] = []

    def execute(cypher: str, *args: Any, **kwargs: Any) -> Any:
        rows = real_execute(cypher, *args, **kwargs)
        params = kwargs.get("params") or (args[0] if args else {}) or {}
        # Right after resolution reads B's content (A was read before it): erase C.
        if not erased and "n.content AS content" in cypher and params.get("uuid") == ids["b"]:
            erased.append(coord.erase_memory(c))
        return rows

    monkeypatch.setattr(repo, "execute", execute)

    adapter.resolve_conflict_group(ids["g"], "replace", keep_uuid=ids["a"], remove_uuid=ids["b"],
                                   namespace=ns)

    assert erased and erased[0]["reason"] == "erased", erased
    assert all_hits(repo, db, canary) == []
    a = _props(repo, ids["a"])
    assert "B body" not in (a.get("content") or ""), a  # the refused copy wrote nothing
    assert _props(repo, ids["b"]).get("freshness") == "GONE"  # resolution itself still ran


def _block(source: str, text: str) -> str:
    return f"{ABSORBED_BLOCK_SEPARATOR}{source}) on 2026-01-01 ---\n{text}"


def _repair_keys(repo: Any, holder: str) -> list[str]:
    return [r["k"] for r in repo.execute(
        "MATCH (rr:DerivedRederiveRepair {item_uuid:$h}) RETURN rr.repair_key AS k",
        params={"h": holder})]


def test_cf332_holder_is_cut_when_the_erased_target_is_already_gone(test_neo4j_repo, tmp_path):
    """The absorbed source no longer exists (its node was removed earlier), so the erase takes the
    target-absent path. The holder in the caller's namespace still loses the block, is flagged with
    a recoverable repair key, and a foreign holder is out of the scoped erase. Fails if the
    target-absent statement stops running the holder scrub."""
    repo = test_neo4j_repo
    ns, canary = f"r2-332a-{_u()}", new_canary("CF332A")
    gone, holder, foreign = _u(), _u(), _u()
    content = "holder text" + _block(gone, f"gone said {canary}")
    repo.execute(
        """
        CREATE (:Entity {uuid:$h, name:'holder', group_id:$ns, namespace:$ns, content:$c,
                         summary:'restates it', absorbed_from:[$g]})
        CREATE (:Entity {uuid:$f, name:'foreign', group_id:$ons, namespace:$ons, content:$c,
                         absorbed_from:[$g]})
        """,
        params={"h": holder, "f": foreign, "g": gone, "ns": ns, "ons": f"{ns}-other", "c": content},
    )
    _, coord, db = _adapter_and_coordinator(repo, tmp_path)

    out = coord.erase_memory(gone, caller_namespace=ns)

    h = _props(repo, holder)
    assert h["content"] == "holder text" and h["summary"] == "", h
    assert h["rederive_pending"] is True and h["absorbed_from"] == [], h
    assert _repair_keys(repo, holder) == [f"{gone}\u001f{holder}"]
    assert out["derived"]["entities_flagged"] == 1, out
    assert _props(repo, foreign)["content"] == content  # another namespace: out of scope
    assert canary not in str(_props(repo, holder))


@pytest.mark.parametrize("sole_source", [True, False], ids=["source_deleted", "source_scrubbed"])
def test_cf332_block_of_an_entity_the_cascade_deleted_or_scrubbed_is_cut(
    test_neo4j_repo, tmp_path, sole_source
):
    """Erasing an episode deletes (sole source) or scrubs (shared) a derived entity S. A holder that
    absorbed S's text keeps a copy unless the cut covers the cascade's own deleted/flagged
    entities, not just the erase targets. Fails if the holder cut keys on $erased_uuids alone."""
    repo = test_neo4j_repo
    ns, canary = f"r2-332c-{_u()}", new_canary("CF332C")
    ids = _seed_memory(repo, ns, canary)
    if sole_source:
        repo.execute("MATCH ()-[r:RELATES_TO {uuid:$e}]->() SET r.episodes = [$t]",
                     params={"e": ids["edge"], "t": ids["twin"]})
        repo.execute("MATCH (l:Episodic {uuid:$l}) DETACH DELETE l", params={"l": ids["live"]})
    holder = _u()
    repo.execute(
        "CREATE (:Entity {uuid:$h, name:'holder', group_id:$ns, namespace:$ns, content:$c, "
        "absorbed_from:[$s]})",
        params={"h": holder, "ns": ns, "s": ids["shared"],
                "c": "holder text" + _block(ids["shared"], f"S said {canary}")},
    )
    _, coord, db = _adapter_and_coordinator(repo, tmp_path)

    out = coord.erase_memory(ids["queue"])

    assert out["reason"] == "erased", out
    s_left = _props(repo, ids["shared"])
    assert (s_left == {}) is sole_source, s_left  # the cascade deleted / scrubbed S as expected
    h = _props(repo, holder)
    assert h["content"] == "holder text" and h["rederive_pending"] is True, h
    assert all_hits(repo, db, canary) == []
