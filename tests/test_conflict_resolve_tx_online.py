"""#890 online counterexamples: a resolve is one locked transaction (real Neo4j).

Concurrency and lock interleaving cannot be shown against a fake: they need real write locks.
"""

from __future__ import annotations

import threading
import time
import uuid as uuidlib
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from menhir.infrastructure.consolidation_queries import ConflictResolveRefused, summary_sha256
from menhir.infrastructure.erasure_fence import fence_key_cypher
from menhir.infrastructure.graph_text_inventory import ABSORBED_BLOCK_SEPARATOR
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = pytest.mark.online


class _Group:
    def __init__(self, repo: Any, specs: dict[str, dict[str, Any]]) -> None:
        tag = uuidlib.uuid4().hex[:8]
        self.repo = repo
        self.ns = f"tx890-{tag}"
        self.gid = f"g-{tag}"
        self.ids = {name: f"{name}-{tag}" for name in specs}
        for name, props in specs.items():
            repo.execute(
                """
                CREATE (:Entity {uuid: $uuid, name: $uuid, namespace: $ns, group_id: $ns,
                                 scope: 'PERSISTENT', freshness: 'ACTIVE',
                                 conflict_group_id: $gid, conflict_status: 'unresolved',
                                 conflict_created_at: datetime(), created_at: datetime()})
                WITH $props AS props
                MATCH (n:Entity {uuid: $uuid}) SET n += props
                """,
                params={"uuid": self.ids[name], "ns": self.ns, "gid": self.gid, "props": props},
            )

    def props(self, name: str) -> dict[str, Any]:
        rows = self.repo.execute(
            "MATCH (n:Entity {uuid: $u}) RETURN properties(n) AS p", params={"u": self.ids[name]}
        )
        return dict(rows[0]["p"])

    def resolve(self, action: str = "replace", keep: str = "keep", **kw: Any) -> dict[str, Any]:
        return MemoryGraphAdapter(neo4j=self.repo).resolve_conflict_group(
            self.gid, action, keep_uuid=self.ids[keep], namespace=self.ns, **kw
        )

    def fence_key(self) -> str:
        rows = self.repo.execute(
            f"MATCH (n:Entity {{uuid: $u}}) RETURN {fence_key_cypher('n')} AS k",
            params={"u": self.ids["keep"]},
        )
        return str(rows[0]["k"])


@pytest.fixture
def group_factory(test_neo4j_repo) -> Iterator[Callable[..., _Group]]:
    made: list[_Group] = []

    def make(**specs: dict[str, Any]) -> _Group:
        group = _Group(test_neo4j_repo, specs)
        made.append(group)
        return group

    yield make
    for group in made:
        test_neo4j_repo.execute(
            "MATCH (n:Entity {namespace: $ns}) DETACH DELETE n", params={"ns": group.ns}
        )


def _blocks(content: str, target_id: str) -> int:
    return content.count(f"{ABSORBED_BLOCK_SEPARATOR}{target_id})")


def _patch_statements(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_when: Callable[[str], bool] | None = None,
    after_member_read: Callable[[Any], None] | None = None,
) -> None:
    """Inject an error into, or run a callback after, matching statements on every path."""
    from menhir.infrastructure import neo4j as n4

    real_repo, real_tx = n4.Neo4jRepository.execute, n4.Neo4jTransaction.execute
    fired: list[bool] = []

    def repo_execute(self: Any, query: str, *a: Any, **k: Any) -> Any:
        if fail_when is not None and fail_when(query):
            raise RuntimeError("injected failure")
        rows = real_repo(self, query, *a, **k)
        if after_member_read is not None and not fired and "fence_epoch" in query:
            fired.append(True)
            after_member_read(self)
        return rows

    def tx_execute(self: Any, query: str, *a: Any, **k: Any) -> Any:
        if fail_when is not None and fail_when(query):
            raise RuntimeError("injected failure")
        return real_tx(self, query, *a, **k)

    monkeypatch.setattr(n4.Neo4jRepository, "execute", repo_execute)
    monkeypatch.setattr(n4.Neo4jTransaction, "execute", tx_execute)


def _is_gone_write(query: str) -> bool:
    return "SET n.freshness = 'GONE'" in query


def test_gone_failure_leaves_nothing_changed(group_factory, monkeypatch) -> None:
    """Fails on main: the absorb is committed before the GONE statement raises."""
    g = group_factory(keep={"content": "keep text"}, t1={"content": "target one"})
    _patch_statements(monkeypatch, fail_when=_is_gone_write)

    with pytest.raises(RuntimeError, match="injected"):
        g.resolve()

    keep, target = g.props("keep"), g.props("t1")
    assert keep["content"] == "keep text" and "absorbed_from" not in keep
    assert target["freshness"] == "ACTIVE"
    assert keep["conflict_group_id"] == g.gid and target["conflict_group_id"] == g.gid


def test_retry_after_a_failed_resolve_absorbs_once(group_factory, monkeypatch) -> None:
    """Fails on main: the failed attempt's block is committed and the retry appends a second."""
    g = group_factory(keep={"content": "keep text"}, t1={"content": "target one"})
    with monkeypatch.context() as m:
        _patch_statements(m, fail_when=_is_gone_write)
        with pytest.raises(RuntimeError):
            g.resolve()

    result = g.resolve()

    assert result["removed_uuids"] == [g.ids["t1"]]
    assert _blocks(g.props("keep")["content"], g.ids["t1"]) == 1
    assert g.props("t1")["freshness"] == "GONE"


def test_retry_of_a_half_done_resolve_absorbs_nothing_more(group_factory) -> None:
    """Fails on main: state from a resolve that absorbed and died before GONE gets a second block."""
    tag = uuidlib.uuid4().hex[:8]
    t1 = f"t1-{tag}"
    block = f"{ABSORBED_BLOCK_SEPARATOR}{t1}) on 2026-01-01 ---\ntarget one"
    g = group_factory(keep={"content": f"keep text{block}", "absorbed_from": [t1]})
    # The target is created under the id the keep already records.
    g.repo.execute(
        """
        CREATE (:Entity {uuid: $u, name: 'half', namespace: $ns, group_id: $ns, scope: 'PERSISTENT',
                         freshness: 'ACTIVE', content: 'target one', conflict_group_id: $gid,
                         conflict_status: 'unresolved', created_at: datetime()})
        """,
        params={"u": t1, "ns": g.ns, "gid": g.gid},
    )
    g.ids["t1"] = t1

    g.resolve()

    assert g.props("keep")["content"] == f"keep text{block}"
    assert g.props("keep")["absorbed_from"] == [t1]
    assert g.props("t1")["freshness"] == "GONE"


def test_two_concurrent_resolves_of_one_group_absorb_once_and_one_succeeds(group_factory) -> None:
    """Fails on main: both resolves absorb and both report success."""
    groups = [
        group_factory(keep={"content": "keep text"}, t1={"content": "target one"}) for _ in range(4)
    ]
    outcomes: dict[tuple[int, int], Any] = {}
    start = threading.Barrier(len(groups) * 2)

    def run(i: int, attempt: int) -> None:
        start.wait()
        try:
            outcomes[(i, attempt)] = groups[i].resolve()
        except Exception as exc:  # noqa: BLE001
            outcomes[(i, attempt)] = exc

    threads = [
        threading.Thread(target=run, args=(i, attempt))
        for i in range(len(groups)) for attempt in (0, 1)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    for i, g in enumerate(groups):
        results = [outcomes[(i, 0)], outcomes[(i, 1)]]
        successes = [r for r in results if isinstance(r, dict)]
        refusals = [r for r in results if isinstance(r, ValueError)]
        assert len(successes) == 1 and len(refusals) == 1, results
        assert _blocks(g.props("keep")["content"], g.ids["t1"]) == 1
        assert g.props("t1")["freshness"] == "GONE"


def test_a_fenced_absorb_refuses_with_no_gone(group_factory, monkeypatch) -> None:
    """An erase lands after the epoch was read. Fails on main: it GONEs the member and succeeds."""
    g = group_factory(keep={"content": "keep text"}, t1={"content": "target one"})
    key = g.fence_key()

    def erase_lands(repo: Any) -> None:
        repo.execute(
            "MERGE (f:EvidenceNamespaceFence {namespace_key: $k}) "
            "SET f.erasure_epoch = coalesce(f.erasure_epoch, 0) + 1",
            params={"k": key},
        )

    _patch_statements(monkeypatch, after_member_read=erase_lands)

    with pytest.raises(ConflictResolveRefused, match="epoch_moved"):
        g.resolve()

    assert g.props("t1")["freshness"] == "ACTIVE"
    assert g.props("keep")["content"] == "keep text"
    assert g.props("t1")["conflict_group_id"] == g.gid


def test_a_member_promoted_after_the_read_is_not_gone(group_factory, monkeypatch) -> None:
    """Fails on main: PROMOTED is checked once, from the early read."""
    g = group_factory(keep={"content": "keep text"}, t1={"content": "target one"})

    def promote(repo: Any) -> None:
        repo.execute(
            "MATCH (n:Entity {uuid: $u}) SET n.scope = 'PROMOTED'", params={"u": g.ids["t1"]}
        )

    _patch_statements(monkeypatch, after_member_read=promote)

    with pytest.raises(ValueError, match="Refusing to GONE PROMOTED"):
        g.resolve()

    assert g.props("t1")["freshness"] == "ACTIVE"
    assert g.props("keep")["content"] == "keep text"


def test_a_member_that_joined_after_the_read_refuses_the_resolve(group_factory, monkeypatch) -> None:
    """Fails on main, which resolves the group without looking at the late member."""
    g = group_factory(keep={"content": "keep text"}, t1={"content": "target one"}, late={"content": "late"})
    g.repo.execute(
        "MATCH (n:Entity {uuid: $u}) SET n.conflict_group_id = null", params={"u": g.ids["late"]}
    )

    def joins(repo: Any) -> None:
        repo.execute(
            "MATCH (n:Entity {uuid: $u}) SET n.conflict_group_id = $g",
            params={"u": g.ids["late"], "g": g.gid},
        )

    _patch_statements(monkeypatch, after_member_read=joins)

    with pytest.raises(ConflictResolveRefused, match="member_changed"):
        g.resolve()

    assert g.props("keep")["content"] == "keep text"
    assert g.props("t1")["freshness"] == "ACTIVE" and g.props("t1")["conflict_group_id"] == g.gid
    assert g.props("late")["freshness"] == "ACTIVE" and g.props("late")["conflict_group_id"] == g.gid


def test_a_resolve_with_two_targets_keeps_both_blocks(group_factory) -> None:
    g = group_factory(keep={"content": "keep text"}, t1={"content": "one"}, t2={"content": "two"})

    g.resolve()

    content = g.props("keep")["content"]
    assert content.startswith("keep text")
    assert _blocks(content, g.ids["t1"]) == 1 and _blocks(content, g.ids["t2"]) == 1
    assert g.props("keep")["absorbed_from"] == sorted([g.ids["t1"], g.ids["t2"]])


def test_compressed_keep_with_a_trusted_marker_gets_the_blocks_in_original_content(group_factory) -> None:
    g = group_factory(
        keep={"content": "the summary", "freshness": "COMPRESSED", "original_content": "the full body",
              "compressed_summary_sha256": summary_sha256("the summary")},
        t1={"content": "one"}, t2={"content": "two"},
    )

    g.resolve()

    keep = g.props("keep")
    assert keep["original_content"].startswith("the full body")
    assert _blocks(keep["original_content"], g.ids["t1"]) == 1
    assert _blocks(keep["original_content"], g.ids["t2"]) == 1
    assert keep["compressed_summary_sha256"] == summary_sha256(keep["content"])


def test_compressed_keep_with_an_untrusted_marker_leaves_original_content_and_drops_the_marker(
    group_factory,
) -> None:
    g = group_factory(
        keep={"content": "the summary", "freshness": "COMPRESSED", "original_content": "the full body",
              "compressed_summary_sha256": summary_sha256("not the summary")},
        t1={"content": "one"},
    )

    g.resolve()

    keep = g.props("keep")
    assert keep["original_content"] == "the full body"
    assert "compressed_summary_sha256" not in keep
    assert _blocks(keep["content"], g.ids["t1"]) == 1


def test_a_keep_without_content_never_gets_an_empty_original_content(group_factory) -> None:
    """Fails on main, which stores original_content = ''."""
    g = group_factory(keep={}, t1={"content": "one"})

    g.resolve()

    assert "original_content" not in g.props("keep")
    assert "one" in g.props("keep")["content"]


def test_keep_both_reports_the_members_it_changed(group_factory) -> None:
    g = group_factory(a={"content": "x"}, b={"content": "y"}, c={"content": "z"})

    result = MemoryGraphAdapter(neo4j=g.repo).resolve_conflict_group(
        g.gid, "keep_both", namespace=g.ns
    )

    assert result["resolved"] == 3
    assert result["member_uuids"] == sorted(g.ids.values())


def test_set_conflict_counts_b_once(test_neo4j_repo) -> None:
    """a joins b's group while b's group is retired: a, b and b's other member change. Fails on
    main, which reports 4."""
    tag = uuidlib.uuid4().hex[:8]
    a, a2, b, b2 = (f"{n}-{tag}" for n in ("a", "a2", "b", "b2"))
    repo = test_neo4j_repo
    ns = f"tx890-set-{tag}"
    for uuid, gid in ((a, "ga"), (a2, "ga"), (b, "gb"), (b2, "gb")):
        repo.execute(
            """
            CREATE (:Entity {uuid: $u, name: $u, namespace: $ns, group_id: $ns, scope: 'PERSISTENT',
                             conflict_group_id: $gid, conflict_status: 'unresolved',
                             conflict_created_at: datetime()})
            """,
            params={"u": uuid, "ns": ns, "gid": f"{gid}-{tag}"},
        )
    try:
        canonical, updated = MemoryGraphAdapter(neo4j=repo).set_conflict(a, b, "unused")

        assert canonical == f"ga-{tag}"
        assert updated == 3
        rows = repo.execute(
            "MATCH (n:Entity {namespace: $ns}) RETURN n.uuid AS u, n.conflict_group_id AS g",
            params={"ns": ns},
        )
        assert {r["u"]: r["g"] for r in rows} == {
            a: f"ga-{tag}", a2: f"ga-{tag}", b: f"ga-{tag}", b2: f"ga-{tag}",
        }
    finally:
        repo.execute("MATCH (n:Entity {namespace: $ns}) DETACH DELETE n", params={"ns": ns})


def test_remove_conflict_member_returns_its_reason(group_factory) -> None:
    """Fails on main: a refusal carries no reason."""
    g = group_factory(a={}, b={}, c={})
    for name in g.ids:
        g.repo.execute(
            "MATCH (n:Entity {uuid: $u}) SET n.conflict_status = 'pending_llm_review'",
            params={"u": g.ids[name]},
        )

    out = MemoryGraphAdapter(neo4j=g.repo).remove_conflict_member(
        g.gid, g.ids["a"], from_status="pending_llm_review", reviewed_uuids=[g.ids["b"]]
    )

    assert out["removed"] is False
    assert out["reason"] == "unreviewed_members"
    assert out["unreviewed_uuids"] == [g.ids["c"]]


def test_remove_conflict_member_refuses_on_an_unreviewed_foreign_member(group_factory) -> None:
    """A legacy group with a member of another namespace: that pair was never reviewed, so the
    removal is refused (the design keeps main's true-member-count refusal), the foreign member
    is untouched and its uuid is not disclosed."""
    g = group_factory(a={}, b={})
    foreign = f"foreign-{g.gid}"
    g.repo.execute(
        """
        CREATE (:Entity {uuid: $u, name: $u, namespace: $other, group_id: $other, scope: 'PERSISTENT',
                         freshness: 'ACTIVE', conflict_group_id: $gid, conflict_status: 'pending_llm_review',
                         created_at: datetime()})
        """,
        params={"u": foreign, "other": f"{g.ns}-other", "gid": g.gid},
    )
    for name in g.ids:
        g.repo.execute(
            "MATCH (n:Entity {uuid: $u}) SET n.conflict_status = 'pending_llm_review'",
            params={"u": g.ids[name]},
        )
    try:
        out = MemoryGraphAdapter(neo4j=g.repo).remove_conflict_member(
            g.gid, g.ids["a"], from_status="pending_llm_review",
            reviewed_uuids=[g.ids["b"]], namespace=g.ns,
        )

        assert out["removed"] is False
        assert out["reason"] == "unreviewed_members"
        assert out["unreviewed_uuids"] == []
        rows = g.repo.execute(
            "MATCH (n:Entity) WHERE n.uuid IN $us RETURN n.uuid AS u, n.conflict_group_id AS g, "
            "n.conflict_status AS s",
            params={"us": [g.ids["a"], foreign]},
        )
        assert {r["u"]: (r["g"], r["s"]) for r in rows} == {
            g.ids["a"]: (g.gid, "pending_llm_review"),
            foreign: (g.gid, "pending_llm_review"),
        }
    finally:
        g.repo.execute(
            "MATCH (n:Entity {namespace: $ns}) DETACH DELETE n", params={"ns": f"{g.ns}-other"}
        )


# --- lock interleaving -------------------------------------------------------------------------
class _OtherSession:
    """A second session holding write locks in an open transaction."""

    def __init__(self, repo: Any) -> None:
        self._session = repo._get_driver().session(database=repo.database)
        self._tx = self._session.begin_transaction()

    def run(self, query: str, **params: Any) -> None:
        self._tx.run(query, **params).consume()

    def commit(self) -> None:
        try:
            self._tx.commit()
        finally:
            self._session.close()


def _resolve_in_thread(g: _Group) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = g.resolve()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def test_a_resolve_waits_for_a_members_write_lock_and_refuses_the_promotion(group_factory) -> None:
    """Another session promotes a target in an open transaction. The resolve blocks on the member
    lock, then sees the committed promotion and refuses. Without the lock statement it reads the
    old scope, and the GONE write (whose WHERE is not re-evaluated after the wait) retires it."""
    g = group_factory(keep={"content": "keep text"}, t1={"content": "target one"})
    other = _OtherSession(g.repo)
    other.run("MATCH (n:Entity {uuid: $u}) SET n.scope = 'PROMOTED'", u=g.ids["t1"])
    thread, box = _resolve_in_thread(g)
    try:
        time.sleep(3)
        assert thread.is_alive(), f"the resolve did not wait for the lock: {box}"
    finally:
        other.commit()
    thread.join(timeout=60)

    assert isinstance(box.get("error"), ValueError), box
    assert "Refusing to GONE PROMOTED" in str(box["error"])
    assert g.props("t1")["freshness"] == "ACTIVE"
    assert g.props("keep")["content"] == "keep text"


def test_a_resolve_waits_for_a_keep_edit_and_keeps_it(group_factory) -> None:
    """Another session edits the keep's text in an open transaction. The resolve waits and builds
    the merged text from the committed edit. Without the lock it merges the old text and its write
    replaces the edit."""
    g = group_factory(keep={"content": "keep text"}, t1={"content": "target one"})
    other = _OtherSession(g.repo)
    other.run("MATCH (n:Entity {uuid: $u}) SET n.content = 'keep text, edited'", u=g.ids["keep"])
    thread, box = _resolve_in_thread(g)
    try:
        time.sleep(3)
        assert thread.is_alive(), f"the resolve did not wait for the lock: {box}"
    finally:
        other.commit()
    thread.join(timeout=60)

    assert "error" not in box, box
    content = g.props("keep")["content"]
    assert content.startswith("keep text, edited")
    assert _blocks(content, g.ids["t1"]) == 1
