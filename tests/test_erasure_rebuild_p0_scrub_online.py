"""P0 counterexamples for the erasure rebuild, lane C (plan v2, section 6): closure, scrub, scoping
and copiers. Online: every test needs the live throwaway Neo4j (``--run-online``).

Discipline (plan section 6): the xfail tests are ``xfail(strict=True, raises=AssertionError)``, drive
public entry points only (the coordinator / backend ``erase_memory`` and ``delete_memory``, merge
coordinator, raw capture, TypedAssertion and projection writers, ``ensure_self_entity``), fake the
LLM and Graphiti (here: no LLM at all, the graph is seeded in Graphiti's shape), and assert only
observable outcomes: a Cypher read of every node and relationship property for the canary phrase,
sidecar rows and reply fields. A symbol that does not exist at e574abdf is looked up with
``getattr`` and asserted before use, so today's failure is an AssertionError, never an ImportError.

ASSUMPTIONS the later PRs must honour for these tests to go green (stated, not hidden):
* seeded ids are canonical uuid4, so the strict entry validator (P2) does not reject them;
* the READ_CONTEXT / menhir_memory_uuid schema of plan section 2.1 is seeded by hand for T20, because
  no fork hook exists yet to write it;
* canaries are never placed in identity fields (names, uuids), which R-names says are not scrubbed.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sqlite3
import uuid as uuidlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import ErasureCoordinator

pytestmark = pytest.mark.online

P0 = pytest.mark.xfail(strict=True, raises=AssertionError)


# ------------------------------------------------------------------ helpers
def _u() -> str:
    return str(uuidlib.uuid4())


def _canary(tag: str) -> str:
    return f"CANARY-{tag}-{uuidlib.uuid4().hex}"


def _adapter_and_coordinator(repo: Any, tmp_path: Path, wrap: Any = None):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(neo4j=repo)
    graph = wrap(adapter) if wrap else adapter
    db = tmp_path / "p0-scrub.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    coord = ErasureCoordinator(
        graph_adapter=graph,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    return adapter, coord, db


def _hits(repo: Any, phrase: str) -> list[str]:
    """Every node and relationship whose full property map mentions the phrase (list- and
    JSON-valued holders included), as readable descriptors."""
    found: list[str] = []
    for row in repo.execute("MATCH (n) RETURN labels(n) AS labels, properties(n) AS p"):
        if phrase in json.dumps(row["p"], default=str):
            found.append(f"node{row['labels']} uuid={row['p'].get('uuid')}")
    for row in repo.execute("MATCH ()-[r]->() RETURN type(r) AS t, properties(r) AS p"):
        if phrase in json.dumps(row["p"], default=str):
            found.append(f"rel[{row['t']}] uuid={row['p'].get('uuid')}")
    return found


def _sidecar_hits(db: Path, phrase: str) -> list[str]:
    out: list[str] = []
    with contextlib.closing(sqlite3.connect(db)) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                if phrase in json.dumps(list(row), default=str):
                    out.append(table)
                    break
    return out


def _seed_memory(repo: Any, ns: str, canary: str, *, live_text: str = "a surviving memory") -> dict:
    """Queue node Q -> twin T (both hold the canary) + live episode L.
    Shared entity S (MENTIONS from T and L; summary holds the canary) and a fact S->O over [T, L]
    whose text holds the canary. All ids are canonical uuid4."""
    ids = {k: _u() for k in ("queue", "twin", "live", "shared", "other", "edge", "m1", "m2")}
    ids["ns"] = ns
    repo.execute(
        """
        CREATE (t:Episodic {uuid:$twin, group_id:$ns, valid_at:datetime(), content:$canary})
        CREATE (:Episodic {uuid:$queue, namespace:$ns, resolved_episode_uuid:$twin,
                           processing_state:'READY', content:$canary})
        CREATE (l:Episodic {uuid:$live, group_id:$ns, valid_at:datetime(), content:$live_text})
        CREATE (s:Entity {uuid:$shared, name:'shared probe', group_id:$ns, namespace:$ns,
                          summary:$canary, created_at:datetime()})
        CREATE (o:Entity {uuid:$other, name:'other probe', group_id:$ns, namespace:$ns,
                          summary:'clean', created_at:datetime()})
        CREATE (t)-[:MENTIONS {uuid:$m1, group_id:$ns}]->(s)
        CREATE (l)-[:MENTIONS {uuid:$m2, group_id:$ns}]->(s)
        CREATE (s)-[:RELATES_TO {uuid:$edge, name:'PROBE', fact:$canary, episodes:[$twin, $live],
                                 group_id:$ns, valid_at:datetime(), created_at:datetime()}]->(o)
        """,
        params={**ids, "canary": canary, "live_text": live_text},
    )
    return ids


# ------------------------------------------------------------------ T27a
@P0
def test_t27a_edge_invalidation_conflict_text_is_gone_after_erase(test_neo4j_repo, tmp_path):
    """T27 (Adv F2a). An :EdgeInvalidationConflict written by the production writer copies a
    closure edge's fact into ``kept_fact``. Erasing the memory deletes the edge but today leaves the
    conflict node (and its text) behind. Fails if the cascade does not delete or scrub conflict
    nodes whose kept/cause edge uuid is in the deleted or flagged edge set."""
    from menhir.infrastructure.graphiti_edge_invalidation import KeptEdge, record_kept_conflicts

    repo = test_neo4j_repo
    ns, canary = f"t27a-{_u()}", _canary("T27A")
    ids = _seed_memory(repo, ns, canary)
    # Make the closure the edge's ONLY source so the cascade deletes it outright.
    repo.execute("MATCH ()-[r:RELATES_TO {uuid:$e}]->() SET r.episodes = [$twin]",
                 params={"e": ids["edge"], "twin": ids["twin"]})
    repo.execute("MATCH (l:Episodic {uuid:$l}) DETACH DELETE l", params={"l": ids["live"]})

    class _Driver:
        async def execute_query(self, cypher: str, **params: Any) -> None:
            repo.execute(cypher, params=params)

    asyncio.run(record_kept_conflicts(_Driver(), [KeptEdge(
        reason="newer_candidate", kept_edge_uuid=ids["edge"], kept_group_id=ns,
        kept_fact=canary, cause_edge_uuid=_u(), cause_fact="unrelated cause", cause_source="agent")]))
    assert repo.execute("MATCH (c:EdgeInvalidationConflict) RETURN count(c) AS c")[0]["c"] == 1

    _, coord, _ = _adapter_and_coordinator(repo, tmp_path)
    coord.erase_memory(ids["queue"])

    assert _hits(repo, canary) == []


# ------------------------------------------------------------------ T27b
# R2 CF-330 made this pass (the scrub removes every non-structural Entity key).
def test_t27b_original_content_backup_is_scrubbed_on_shared_entity(test_neo4j_repo, tmp_path):
    """T27 (Adv F2b). Lifecycle compression (production ``compress_node``) copies ``content`` into
    ``original_content``. A shared entity flagged by an erase has summary/content scrubbed, but the
    backup copy keeps the text. Fails if the scrub does not cover ``original_content``."""
    repo = test_neo4j_repo
    ns, canary = f"t27b-{_u()}", _canary("T27B")
    ids = _seed_memory(repo, ns, canary)
    repo.execute(
        "MATCH (s:Entity {uuid:$s}) SET s.content = $c, s.scope = 'PERSISTENT', "
        "s.freshness = 'ACTIVE'", params={"s": ids["shared"], "c": canary})
    adapter, coord, _ = _adapter_and_coordinator(repo, tmp_path)
    assert adapter.compress_node(ids["shared"], "compressed gist")  # production writer
    assert repo.execute("MATCH (s:Entity {uuid:$s}) RETURN s.original_content AS o",
                        params={"s": ids["shared"]})[0]["o"] == canary

    coord.erase_memory(ids["queue"])

    survivors = repo.execute("MATCH (s:Entity {uuid:$s}) RETURN s.rederive_pending AS p",
                             params={"s": ids["shared"]})
    assert survivors and survivors[0]["p"] is True, "shared entity must survive, flagged"
    assert _hits(repo, canary) == []


# ------------------------------------------------------------------ T13 (regression, NOT xfail)
def test_t13_raw_capture_entity_of_exhausted_queue_node_is_erased(test_neo4j_repo, tmp_path):
    """T13 regression: an exhausted queue node gets a raw-capture entity (real
    ``create_raw_capture_entity``); erasing the queue node must delete the entity. Breaks if the
    cascade stops selecting ``raw_capture_for`` entities (or their MENTIONS) as closure."""
    repo = test_neo4j_repo
    ns, canary = f"t13-{_u()}", _canary("T13")
    queue = _u()
    repo.execute(
        "CREATE (:Episodic {uuid:$q, namespace:$ns, processing_state:'FAILED', content:$c, "
        "processing_attempts: 5})", params={"q": queue, "ns": ns, "c": canary})
    adapter, coord, _ = _adapter_and_coordinator(repo, tmp_path)
    entity = adapter.create_raw_capture_entity(queue, "raw capture name", canary, ns, "s1", "u1", "test")
    assert entity
    assert _hits(repo, canary), "precondition: the raw capture holds the canary"

    coord.erase_memory(queue)

    assert _hits(repo, canary) == []
    assert repo.execute("MATCH (n:Entity {raw_capture_for:$q}) RETURN count(n) AS c",
                        params={"q": queue})[0]["c"] == 0


# ------------------------------------------------------------------ T20
def _seed_context_holder(repo: Any, ns: str, canary: str) -> dict:
    """P (published, holds the canary) and E (published later, with P in its recorded context).
    E's own episode text is clean; E's derived items restate P (the second-hand carry the one-hop
    scrub must catch). READ_CONTEXT / menhir_memory_uuid are seeded per plan section 2.1."""
    ids = {k: _u() for k in ("qp", "tp", "qe", "te", "x", "y", "edge", "m")}
    ids["ns"] = ns
    repo.execute(
        """
        CREATE (tp:Episodic {uuid:$tp, group_id:$ns, valid_at:datetime() - duration('PT2H'),
                             content:$canary, menhir_memory_uuid:$qp})
        CREATE (:Episodic {uuid:$qp, namespace:$ns, resolved_episode_uuid:$tp,
                           processing_state:'READY', content:$canary})
        CREATE (te:Episodic {uuid:$te, group_id:$ns, valid_at:datetime() - duration('PT1H'),
                             content:'E says something unrelated and clean',
                             menhir_memory_uuid:$qe})
        CREATE (:Episodic {uuid:$qe, namespace:$ns, resolved_episode_uuid:$te,
                           processing_state:'READY', content:'E says something unrelated and clean'})
        CREATE (te)-[:READ_CONTEXT {kind:'previous_episode', inferred:false}]->(tp)
        CREATE (x:Entity {uuid:$x, name:'holder subject', group_id:$ns, namespace:$ns,
                          summary:$canary, created_at:datetime()})
        CREATE (y:Entity {uuid:$y, name:'holder object', group_id:$ns, namespace:$ns,
                          summary:'clean', created_at:datetime()})
        CREATE (te)-[:MENTIONS {uuid:$m, group_id:$ns}]->(x)
        CREATE (x)-[:RELATES_TO {uuid:$edge, name:'RESTATES', fact:$canary, episodes:[$te],
                                 group_id:$ns, valid_at:datetime(), created_at:datetime()}]->(y)
        """,
        params={**ids, "canary": canary},
    )
    return ids


@P0
def test_t20_context_holder_derived_items_are_scrubbed_and_flagged(test_neo4j_repo, tmp_path):
    """T20 (Adv F1). Erasing P must scrub (and flag for rederive) the derived items of every twin
    that had P in its recorded context, while leaving that twin itself intact. Fails today because
    the cascade follows only MENTIONS/episodes provenance of the erased memory, never READ_CONTEXT."""
    repo = test_neo4j_repo
    ns, canary = f"t20-{_u()}", _canary("T20")
    ids = _seed_context_holder(repo, ns, canary)
    _, coord, _ = _adapter_and_coordinator(repo, tmp_path)

    coord.erase_memory(ids["qp"])

    assert _hits(repo, canary) == []
    # Holder twin E is intact; its derived items survive, flagged for rederive.
    te = repo.execute("MATCH (e:Episodic {uuid:$u}) RETURN e.content AS c", params={"u": ids["te"]})
    assert te and te[0]["c"] == "E says something unrelated and clean"
    edge = repo.execute("MATCH ()-[r:RELATES_TO {uuid:$u}]->() RETURN r.rederive_pending AS p",
                        params={"u": ids["edge"]})
    assert edge and edge[0]["p"] is True
    x = repo.execute("MATCH (e:Entity {uuid:$u}) RETURN e.rederive_pending AS p",
                     params={"u": ids["x"]})
    assert x and x[0]["p"] is True


@P0
def test_t20_reply_names_second_hand_context_residue(test_neo4j_repo, tmp_path):
    """T20 reply half (S4): every non-failure reply carries a ``residue`` field listing
    ``second_hand_context``. Fails today because the reply has no ``residue`` field."""
    repo = test_neo4j_repo
    ns, canary = f"t20r-{_u()}", _canary("T20R")
    ids = _seed_context_holder(repo, ns, canary)
    _, coord, _ = _adapter_and_coordinator(repo, tmp_path)

    outcome = coord.erase_memory(ids["qp"])

    assert "residue" in outcome, f"reply has no residue field: {sorted(outcome)}"
    assert "second_hand_context" in outcome["residue"]


# ------------------------------------------------------------------ T8
def _pinned_runtime(adapter: Any, coord: ErasureCoordinator, ns: str):
    from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin

    class _Runtime(RuntimeProviderDataOpsMixin):
        def __init__(self) -> None:
            self.built = SimpleNamespace(graph_adapter=adapter)
            self._erasure_coordinator = coord

        async def _off_loop(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

    runtime = _Runtime()

    class _Pin:
        def __enter__(self_inner):
            self_inner.patches = [
                patch("menhir.core.tenancy.pinned_namespace", return_value=ns),
                patch("menhir.core.backend_runtime_data_ops.pinned_namespace", return_value=ns),
            ]
            for p in self_inner.patches:
                p.start()

        def __exit__(self_inner, *exc):
            for p in self_inner.patches:
                p.stop()

    return runtime, _Pin()


def _seed_foreign_merge_audit(repo: Any, ns_x: str) -> tuple[str, list[str]]:
    survivor = _u()
    entry = json.dumps({"survivor_uuid": survivor, "absorbed_uuid": _u(),
                        "snapshot": {"note": "a merge recorded in namespace X"}})
    repo.execute(
        "CREATE (:Entity {uuid:$s, name:'x survivor', group_id:$ns, namespace:$ns, "
        "merge_audit:[$entry], created_at:datetime()})",
        params={"s": survivor, "ns": ns_x, "entry": entry})
    return survivor, [entry]


def test_t8_pinned_short_id_does_not_touch_other_namespaces_merge_audit(test_neo4j_repo, tmp_path):
    """T8 (A-7). A caller pinned to namespace Y erases the one-letter id ``"e"``. The merge_audit
    scrub matches by substring over ALL entities, so namespace X's merge_audit (whose JSON contains
    an ``e``) is tombstoned. Fails if the scrub is not scoped to the caller's namespace with exact
    canonical-uuid matches (or the id is not rejected up front)."""
    repo = test_neo4j_repo
    ns_x, ns_y = f"t8x-{_u()}", f"t8y-{_u()}"
    survivor, before = _seed_foreign_merge_audit(repo, ns_x)
    adapter, coord, _ = _adapter_and_coordinator(repo, tmp_path)
    runtime, pin = _pinned_runtime(adapter, coord, ns_y)

    with pin:
        try:
            asyncio.run(runtime.erase_memory("e"))
        except Exception:  # noqa: BLE001 - a rejection is a legitimate outcome after the fix
            pass

    row = repo.execute(
        "MATCH (s:Entity {uuid:$s}) RETURN s.merge_audit AS a, s.merge_irreversible AS irr",
        params={"s": survivor})[0]
    assert row["a"] == before, "namespace X's merge_audit was rewritten by namespace Y's erase"
    assert row["irr"] is None


def test_t8_pinned_short_id_is_invalid_target_and_purges_no_sidecar_rows(test_neo4j_repo, tmp_path):
    """T8 reply half (bypass 8). The same erase of ``"e"`` must answer ``invalid_target`` and purge
    nothing. Today any string proceeds to PREPARE and the sidecar purge (reply ``graph_already_absent``).
    The foreign sidecar row's survival is asserted too, but it already holds today (the purge does not
    reach it); the failing line is the reply reason. Fails if the strict canonical-uuid validator is absent."""
    repo = test_neo4j_repo
    ns_x, ns_y = f"t8x-{_u()}", f"t8y-{_u()}"
    adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)
    McpTelemetryStore(db_path=db).record_merge(
        survivor_uuid=_u(), absorbed_uuid="e", similarity=0.9,
        snapshot_json=json.dumps({"properties": {"namespace": ns_x}}),
        survivor_namespace=ns_x, absorbed_namespace=ns_x)
    runtime, pin = _pinned_runtime(adapter, coord, ns_y)

    with pin:
        try:
            outcome = asyncio.run(runtime.erase_memory("e"))
        except Exception:  # noqa: BLE001
            outcome = {}

    with contextlib.closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM merge_audit WHERE absorbed_uuid = 'e'").fetchone()[0]
    assert rows == 1, "namespace X's sidecar merge_audit row was purged by namespace Y's erase"
    assert outcome.get("reason") == "invalid_target", outcome


# ------------------------------------------------------------------ T15
class _EraseAfterSnapshot:
    """Merge adapter that lets the erase run between the merge's snapshot read and its write."""

    def __init__(self, inner: Any, erase: Any) -> None:
        self._inner = inner
        self._erase = erase
        self.erased = False

    def capture_merge_snapshot(self, *args: Any, **kwargs: Any) -> Any:
        snapshot = self._inner.capture_merge_snapshot(*args, **kwargs)
        self._erase()
        self.erased = True
        return snapshot

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@P0
def test_t15_merge_snapshot_taken_before_erase_does_not_resurrect_text(test_neo4j_repo, tmp_path):
    """T15 (F3). The merge reads the absorbed entity (summary holds the canary), the erase of the
    memory that produced it then scrubs that shared entity, and the merge writes its pre-scrub
    snapshot into ``survivor.merge_audit`` and the sidecar ``merge_audit`` / journal rows. Fails
    if the merge write is not fenced (G-copy) against an erase that ran inside its read window."""
    from menhir.infrastructure.correlation_queries import CorrelationRepository
    from menhir.services.merge_coordinator import MergeCoordinator

    repo = test_neo4j_repo
    ns, canary = f"t15-{_u()}", _canary("T15")
    ids = _seed_memory(repo, ns, canary)  # `shared` is the absorbed entity (T and L mention it)
    survivor = _u()
    repo.execute(
        "MATCH (a:Entity {uuid:$a}) SET a.content = $c, a.scope = 'PERSISTENT', "
        "a.freshness = 'ACTIVE', a.type = 'ENTITY'", params={"a": ids["shared"], "c": canary})
    repo.execute(
        "CREATE (:Entity {uuid:$s, name:'survivor probe', group_id:$ns, namespace:$ns, "
        "summary:'short', scope:'PERSISTENT', freshness:'ACTIVE', type:'ENTITY', "
        "created_at:datetime()})", params={"s": survivor, "ns": ns})
    _, erase_coord, db = _adapter_and_coordinator(repo, tmp_path)
    merge_coord = MergeCoordinator(
        graph_adapter=_EraseAfterSnapshot(
            CorrelationRepository(repo), lambda: erase_coord.erase_memory(ids["queue"])),
        journal=GraphOperationsJournal(db_path=db),
    )

    try:
        result = merge_coord.merge(survivor_uuid=survivor, absorbed_uuid=ids["shared"], similarity=0.97)
    except Exception as exc:  # noqa: BLE001 - after the fix the stale write may abort
        result = {"aborted": type(exc).__name__}
    assert merge_coord.graph_adapter.erased, f"harness: erase hook never ran: {result}"

    leaked_graph = [h for h in _hits(repo, canary)]
    leaked_sidecar = _sidecar_hits(db, canary)
    assert leaked_graph == [] and leaked_sidecar == [], (leaked_graph, leaked_sidecar, result)


# ------------------------------------------------------------------ T22
@P0
def test_t22_raw_capture_after_erase_does_not_recreate_the_erased_text(test_neo4j_repo, tmp_path):
    """T22 (G-create). The erase completes for queue node Q, then a stale failure handler for the
    same episode calls ``create_raw_capture_entity`` with Q's text (the exhausted-retry path, which
    holds the content in memory). Today the MERGE on ``raw_capture_for`` creates a brand-new Entity
    holding the canary that nothing will ever erase. Fails if the raw-capture writer is not gated on
    the namespace's erasure epoch / erased-uuid marker (G-create)."""
    repo = test_neo4j_repo
    ns, canary = f"t22-{_u()}", _canary("T22")
    queue = _u()
    repo.execute(
        "CREATE (:Episodic {uuid:$q, namespace:$ns, group_id:$ns, processing_state:'FAILED', "
        "content:$c})", params={"q": queue, "ns": ns, "c": canary})
    adapter, coord, _db = _adapter_and_coordinator(repo, tmp_path)
    coord.erase_memory(queue)
    assert _hits(repo, canary) == [], "precondition: erase itself removed the text"

    try:
        adapter.create_raw_capture_entity(
            episode_uuid=queue, name="raw", content=canary, namespace=ns,
            session_id="s", user_id="u", source="test")
    except Exception:  # noqa: BLE001 - a refusal after the fix is fine; only the residue matters
        pass
    assert _hits(repo, canary) == []


# ------------------------------------------------------------------ T23
def _turn_seed(repo: Any, ns: str, canary: str) -> dict:
    ids = {"turn": _u(), "queue": _u()}
    repo.execute(
        """
        CREATE (t:TurnEvidence {turn_id:$turn, turn_key:$turn, namespace:$ns, role:'user',
                                declarant:'user', text:$c, recorded_at:datetime()})
        CREATE (q:Episodic {uuid:$queue, namespace:$ns, processing_state:'READY',
                            content:'User said something'})
        CREATE (q)-[:ADMITTED_ON]->(t)
        """, params={**ids, "ns": ns, "c": canary})
    return ids


def test_t23a_typed_assertion_written_after_erase_does_not_keep_the_text(test_neo4j_repo, tmp_path):
    """T23 (typed-assertion half). Perception read the turn, the erase ran, then the stale
    perceiver persists a TypedAssertion whose ``stated_span`` is the turn's words. Fails if
    ``record_typed_assertion`` is not gated against the erased turn/episode (G-publish)."""
    from menhir.domain.typed_assertion import TypedAssertion

    repo = test_neo4j_repo
    ns, canary = f"t23a-{_u()}", _canary("T23A")
    ids = _turn_seed(repo, ns, canary)
    adapter, coord, _db = _adapter_and_coordinator(repo, tmp_path)
    coord.erase_memory(ids["queue"])
    assert _hits(repo, canary) == [], "precondition: erase removed the turn text"

    assertion = TypedAssertion(
        subject_uuid=_u(), subject_display="user", attribute="owned", scope="", value_kind="count",
        unit="", operation="absolute", value=3, stated_span=canary, episode_uuid=ids["queue"],
        valid_at="2026-01-01T00:00:00Z", learned_at="2026-01-02T00:00:00Z",
        span_start=0, span_end=len(canary), namespace=ns)
    try:
        adapter.record_typed_assertion(assertion)
    except Exception:  # noqa: BLE001
        pass
    assert _hits(repo, canary) == []


class _ProjectAfterClosure:
    """Delegating adapter: the evidence projection is minted after the erase resolved its closure
    (turn + existing projections) but before the delete runs."""

    def __init__(self, inner: Any, ns: str) -> None:
        self._inner, self._ns = inner, ns
        self.projected: Any = "never"

    def erasure_evidence_uuids(self, node_uuid: str) -> Any:
        out = self._inner.erasure_evidence_uuids(node_uuid)
        turn = out[0] if out else None
        self.projected = self._inner.create_evidence_projection(
            turn_evidence_uuid=turn, projection_uuid=_u(), name="late projection",
            session_id="s", user_id="u", namespace=self._ns)
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_t23b_projection_minted_inside_erase_window_does_not_survive(test_neo4j_repo, tmp_path):
    """T23 (projection half). Pass 3 projects the turn between the erase's closure read and its
    delete. The projection copies the turn text, is not in the closure, and survives as an orphan.
    Fails if the projection writer is not fenced (G-publish) or the delete does not re-resolve the
    closure under the fence."""
    repo = test_neo4j_repo
    ns, canary = f"t23b-{_u()}", _canary("T23B")
    ids = _turn_seed(repo, ns, canary)
    holder: dict = {}
    _adapter, coord, _db = _adapter_and_coordinator(
        repo, tmp_path, wrap=lambda a: holder.setdefault("w", _ProjectAfterClosure(a, ns)))
    coord.erase_memory(ids["queue"])
    assert holder["w"].projected not in ("never",), "harness: hook never ran"
    assert _hits(repo, canary) == []


# ------------------------------------------------------------------ T6
def test_t6_stale_summary_save_after_erase_scrub_is_refused(test_neo4j_repo, tmp_path):
    """T6 (A-1). M1 and M2 share entity E. Worker W2 reads E (summary holds M1's words), erase of M1
    scrubs E, then W2 saves the summary it read. Drives the real ``GraphitiClient.add_episode``
    wrapper around a fake native Graphiti (no LLM) that does: read E -> erase M1 -> save the stale
    summary. ASSUMPTION (fork contract, plan 3.1, not implemented at e574abdf): when the wrapper
    passes a ``publish_fence`` callable to the native client, the native client calls it in its
    save transaction and a sealed namespace makes it raise. Today the wrapper passes nothing, so
    the stale save lands. Fails if the publish path does not hand the native client a fence that
    refuses a save whose read window overlapped an erase (G-publish)."""
    import asyncio
    from datetime import datetime, timezone

    from menhir.infrastructure.graphiti_client import GraphitiClient
    from menhir.infrastructure.publish_fence import (
        ClaimFence, MenhirPublishFence, read_gate_fence)

    repo = test_neo4j_repo
    ns, canary = f"t6-{_u()}", _canary("T6")
    ids = _seed_memory(repo, ns, canary)
    _adapter, coord, _db = _adapter_and_coordinator(repo, tmp_path)

    # W2's own claim on a second queue node, as the worker holds it after gate entry. Seeded
    # directly (P4 setup realignment: the fence is the fork's before_write/after_write protocol,
    # driven from a claim and a gate epoch, not a zero-arg callable).
    q2, twin2, worker = _u(), _u(), "t6-w2"
    started = repo.execute(
        """
        CREATE (q:Episodic {uuid:$q, namespace:$ns, processing_state:'ENRICHING',
                            processing_owner:$w, processing_started_at:datetime(),
                            reserved_twin_uuids:[$t], publish_protocol:2,
                            content:'a surviving memory'})
        RETURN toString(q.processing_started_at) AS started
        """, params={"q": q2, "ns": ns, "w": worker, "t": twin2})[0]["started"]
    claim = ClaimFence(q_uuid=q2, worker_id=worker, claim_started_at=started,
                       namespace_key=ns, reserved_twin_uuid=twin2)
    gate = read_gate_fence(repo, ns)  # W2 entered the gate before the erase

    class _NativeGraphiti:
        driver = None

        async def add_episode(self, **kwargs: Any) -> Any:
            stale = repo.execute(
                "MATCH (e:Entity {uuid:$e}) RETURN e.summary AS s", params={"e": ids["shared"]})[0]["s"]
            assert canary in stale, "harness: W2 must read the pre-erase summary"
            coord.erase_memory(ids["queue"])  # M1 erased between W2's read and W2's save
            fence = kwargs.get("publish_fence")
            loop = asyncio.get_running_loop()

            def work(tx: Any) -> None:
                if fence is not None:
                    asyncio.run_coroutine_threadsafe(
                        fence.before_write(tx, episode_uuid=kwargs.get("new_episode_uuid") or twin2,
                                           previous_episode_uuids=[]), loop).result(30)
                tx.execute("MATCH (e:Entity {uuid:$e}) SET e.summary = $s",
                           {"e": ids["shared"], "s": stale})

            await asyncio.to_thread(repo.execute_write, work)
            return SimpleNamespace(episode=None, nodes=[], edges=[])

    client = GraphitiClient(client=_NativeGraphiti())
    try:
        asyncio.run(client.add_episode(
            name="w2", episode_body="a surviving memory", source_description="test",
            reference_time=datetime.now(timezone.utc), episode_uuid=q2, group_id=ns,
            new_episode_uuid=twin2, publish_fence=MenhirPublishFence(claim, gate)))
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 - a fenced abort after the fix is the expected outcome
        pass
    assert _hits(repo, canary) == []


# ------------------------------------------------------------------ T21
#: GUESS: the plan (3.7) names a "scheduled verifier job" but no module or function at e574abdf.
#: Change this constant to the real module once P5 names it.
_VERIFIER_MODULE_GUESS = "menhir.services.erasure_verifier"


@P0
def test_t21_namespace_recreate_self_entity_is_stamped_and_verifier_exists(test_neo4j_repo, tmp_path):
    """T21 (Adv F8). Namespace erase, namespace recreated, ``ensure_self_entity`` re-mints the
    deterministic-uuid self node. The resurrection rule (plan 2.2) can only classify it as a
    legitimate re-creation if it carries ``created_epoch``; and the verifier that applies the rule
    must exist. Fails if ensure_self_entity does not stamp ``created_epoch`` (G-create) or the
    verifier module is missing. NOT EXPRESSED: 'a pre-erase self entity surviving with an old
    created_epoch is flagged' needs the verifier's API, unknown today."""
    repo = test_neo4j_repo
    ns, canary = f"t21-{_u()}", _canary("T21")
    adapter, coord, _db = _adapter_and_coordinator(repo, tmp_path)
    ids = _seed_memory(repo, ns, canary)
    self_uuid = adapter.ensure_self_entity(ns)
    assert repo.execute("MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c",
                        params={"u": self_uuid})[0]["c"] == 1

    reply = coord.erase_namespace(ns)
    assert repo.execute("MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c",
                        params={"u": self_uuid})[0]["c"] == 0, f"precondition: {reply}"

    recreated = adapter.ensure_self_entity(ns)
    assert recreated == self_uuid  # deterministic uuid, as the plan relies on
    row = repo.execute("MATCH (n:Entity {uuid:$u}) RETURN n.created_epoch AS ce",
                       params={"u": recreated})[0]
    assert row["ce"] is not None, "re-created self entity carries no created_epoch"
    assert importlib.util.find_spec(_VERIFIER_MODULE_GUESS) is not None, (
        f"verifier module {_VERIFIER_MODULE_GUESS} does not exist")
