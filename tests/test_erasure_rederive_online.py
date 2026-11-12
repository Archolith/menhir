"""Fail-closed read hiding and the re-derive job after an explicit erasure (#423 follow-up).

Seeds the graph directly (Graphiti shape, no LLM), erases the queue node through the real
ErasureCoordinator, then checks (a) every recall read path hides flagged text and (b) the
re-derive job repairs / deletes / fails closed with its CAS guards. All tests need a live Neo4j and
are modelled on tests/test_erasure_derived_cleanup.py.
"""

from __future__ import annotations

import asyncio
import uuid as uuidlib
from pathlib import Path
from typing import Any

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.derived_rederive import run_derived_rederive
from menhir.services.erasure_coordinator import ERASED, ErasureCoordinator

SECRET = "The rederive probe's favourite colour is teal-9921"
LIVE_TEXT = "A surviving memory says the probe likes green-5513"
CONTROL_FACT = "control fact that must stay visible"


# ------------------------------------------------------------------ helpers
def _ids(tag: str) -> dict[str, str]:
    return {
        "tag": tag,
        "ns": f"{tag}-ns",
        "queue": f"{tag}-queue",
        "twin": f"{tag}-graphiti",
        "live": f"{tag}-live",
        "entity": f"{tag}-entity",
        "entity2": f"{tag}-entity2",
        "entity3": f"{tag}-entity3",
        "edge": f"{tag}-edge",
        "control_edge": f"{tag}-control-edge",
    }


def _seed(repo: Any, tag: str, edge_episodes: list[str] | None = None) -> dict[str, str]:
    """Q + twin T + live episode L; E mentioned by T and L; fact E->E2 over [T, L];
    unflagged control fact E->E3 over [L]."""
    ids = _ids(tag)
    episodes = [ids["twin"], ids["live"]] if edge_episodes is None else edge_episodes
    repo.execute(
        """
        CREATE (t:Episodic {uuid:$twin, group_id:$ns, valid_at:datetime(), content:$secret,
                            test_tag:$tag})
        CREATE (:Episodic {uuid:$queue, namespace:$ns, resolved_episode_uuid:$twin,
                           processing_state:'READY', content:$secret, test_tag:$tag})
        CREATE (l:Episodic {uuid:$live, group_id:$ns, valid_at:datetime(), content:$live_text,
                            test_tag:$tag})
        CREATE (e1:Entity {uuid:$entity, name:'rederive probe', group_id:$ns,
                           summary:$secret, content:$secret, created_at:datetime(),
                           test_tag:$tag})
        CREATE (e2:Entity {uuid:$entity2, name:'teal-9921', group_id:$ns,
                           summary:'a colour', created_at:datetime(), test_tag:$tag})
        CREATE (e3:Entity {uuid:$entity3, name:'control target', group_id:$ns,
                           summary:'control summary', content:'control content',
                           created_at:datetime(), test_tag:$tag})
        CREATE (t)-[:MENTIONS {uuid:$m1, group_id:$ns, created_at:datetime()}]->(e1)
        CREATE (l)-[:MENTIONS {uuid:$m2, group_id:$ns, created_at:datetime()}]->(e1)
        CREATE (e1)-[:RELATES_TO {uuid:$edge, name:'HAS_FAVOURITE_COLOUR', fact:$secret,
                                  episodes:$episodes, group_id:$ns, valid_at:datetime(),
                                  created_at:datetime(), fact_embedding:[0.1, 0.2],
                                  test_tag:$tag}]->(e2)
        CREATE (e1)-[:RELATES_TO {uuid:$control_edge, name:'CONTROL', fact:$control_fact,
                                  episodes:[$live], group_id:$ns, valid_at:datetime(),
                                  created_at:datetime(), test_tag:$tag}]->(e3)
        """,
        params={
            **ids,
            "secret": SECRET,
            "live_text": LIVE_TEXT,
            "control_fact": CONTROL_FACT,
            "m1": f"{tag}-m1",
            "m2": f"{tag}-m2",
            "episodes": episodes,
        },
    )
    return ids


def _coordinator(repo: Any, tmp_path: Path):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(neo4j=repo)
    db = tmp_path / "rederive-online.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    journal = GraphOperationsJournal(db_path=db)
    coord = ErasureCoordinator(
        graph_adapter=adapter, journal=journal, subjects=ErasureSubjectStore(db_path=db)
    )
    return adapter, coord


def _cleanup(repo: Any, tag: str) -> None:
    repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})
    repo.execute(
        "MATCH (r:DerivedRederiveRepair) WHERE r.repair_key CONTAINS $t DETACH DELETE r",
        params={"t": tag},
    )


def _erase(repo: Any, tmp_path: Path, tag: str, **seed_kwargs: Any):
    ids = _seed(repo, tag, **seed_kwargs)
    adapter, coord = _coordinator(repo, tmp_path)
    outcome = coord.erase_memory(ids["queue"])
    assert outcome["reason"] == ERASED
    return ids, adapter


def _edge(repo: Any, edge_uuid: str) -> dict[str, Any] | None:
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN r.fact AS fact, "
        "r.episodes AS episodes, r.rederive_pending AS pending, "
        "r.fact_embedding AS embedding, toString(r.rederive_flagged_at) AS flagged_at",
        params={"u": edge_uuid},
    )
    return dict(rows[0]) if rows else None


def _entity(repo: Any, entity_uuid: str) -> dict[str, Any] | None:
    rows = repo.execute(
        "MATCH (e:Entity {uuid: $u}) RETURN e.summary AS summary, "
        "e.rederive_pending AS pending",
        params={"u": entity_uuid},
    )
    return dict(rows[0]) if rows else None


def _repairs(repo: Any, tag: str) -> dict[str, dict[str, Any]]:
    """Repair nodes by item kind (one fact and/or one entity repair per scenario)."""
    rows = repo.execute(
        "MATCH (r:DerivedRederiveRepair) WHERE r.repair_key CONTAINS $t "
        "RETURN r.item_kind AS kind, r.item_uuid AS item, r.status AS status, "
        "r.attempts AS attempts, r.last_error AS last_error, properties(r) AS props",
        params={"t": tag},
    )
    return {str(r["kind"]): dict(r) for r in rows}


class _FakeLLM:
    def __init__(self, verdicts: dict[str, Any] | None = None, error: Exception | None = None):
        self.verdicts = verdicts or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def rederive_from_sources(self, *, kind, name, current_text, sources):
        self.calls.append(
            {"kind": kind, "name": name, "current_text": current_text, "sources": list(sources)}
        )
        if self.error is not None:
            raise self.error
        return self.verdicts[kind]


class _ScopedAdapter:
    """Delegating adapter that (1) only lists this test's repairs so a shared DB's other pending
    repairs are never touched, and (2) optionally runs a hook just before a fact CAS apply."""

    def __init__(self, inner: Any, tag: str, before_fact_apply: Any = None):
        self._inner = inner
        self._tag = tag
        self._hook = before_fact_apply

    def list_pending_derived_rederive(self, limit: int):
        rows = self._inner.list_pending_derived_rederive(1000)
        return [r for r in rows if self._tag in str(r.get("repair_key"))][:limit]

    def apply_derived_rederive(self, **kwargs: Any) -> bool:
        if self._hook is not None and kwargs.get("item_kind") == "fact":
            self._hook()
        return self._inner.apply_derived_rederive(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _run(adapter: Any, llm: Any) -> dict:
    return asyncio.run(run_derived_rederive(adapter, llm))


_SUPPORTED = {"fact": ("supported", "rewritten fact"), "entity": ("supported", "rewritten summary")}


# ------------------------------------------------------------------ read hiding
@pytest.mark.online
def test_erased_source_flagged_items_are_hidden_on_every_recall_read(
    test_neo4j_repo, tmp_path
) -> None:
    """Breaks if ENTITY_METADATA_FIELDS stops nulling summary/content for rederive_pending, if
    FACT_NOT_REDERIVE_PENDING is dropped from fetch_temporal_facts / fetch_candidate_fact_edges /
    timeline_facts, or if the erasure Cypher stops flagging the shared entity/edge. The control
    edge proves the hiding is per flagged edge, not a blanket filter."""
    repo = test_neo4j_repo
    tag = f"test-rrd-hide-{uuidlib.uuid4()}"
    try:
        ids, adapter = _erase(repo, tmp_path, tag)
        # Precondition from the erasure itself (computed by the cascade, not seeded).
        assert _edge(repo, ids["edge"])["pending"] is True
        assert _entity(repo, ids["entity"])["pending"] is True

        meta = {r["uuid"]: r for r in adapter.fetch_candidate_metadata([ids["entity"]])}
        row = meta[ids["entity"]]
        assert row["summary"] is None
        assert row["content"] is None
        assert row["rederive_pending"] is True
        assert row["name"] == "rederive probe"

        temporal = adapter.fetch_temporal_facts([ids["entity"]])
        assert all(SECRET not in str(r.get("fact")) for r in temporal)
        assert CONTROL_FACT in [r.get("fact") for r in temporal]  # control still visible

        shadow = adapter.fetch_candidate_fact_edges([ids["entity"]])
        assert all(SECRET not in str(r.get("fact_text")) for r in shadow)
        assert CONTROL_FACT in [r.get("fact_text") for r in shadow]

        timeline = adapter.timeline_facts(episode_uuids=[ids["live"]], namespace=ids["ns"])
        live_facts = [f.get("fact") for f in timeline.get(ids["live"], [])]
        assert all(SECRET not in str(f) for f in live_facts)
        assert CONTROL_FACT in live_facts
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_memory_return_fields_hide_flagged_entity_text_only(test_neo4j_repo, tmp_path) -> None:
    """Breaks if MEMORY_RETURN_FIELDS' content/summary CASE on rederive_pending is removed (every
    fetch_memory_by_uuid / recent / flagged / scope / type read would leak the erased text), or if
    it nulls unflagged entities."""
    repo = test_neo4j_repo
    tag = f"test-rrd-memret-{uuidlib.uuid4()}"
    try:
        ids, adapter = _erase(repo, tmp_path, tag)

        flagged = adapter.fetch_memory_by_uuid(ids["entity"])
        assert flagged is not None
        assert flagged["summary"] is None
        assert flagged["content"] is None
        assert flagged["name"] == "rederive probe"

        control = adapter.fetch_memory_by_uuid(ids["entity3"])
        assert control is not None
        assert control["summary"] == "control summary"
        assert control["content"] == "control content"
    finally:
        _cleanup(repo, tag)


# ------------------------------------------------------------------ job behaviour
@pytest.mark.online
def test_job_rewrites_flagged_fact_and_entity_from_remaining_sources_only(
    test_neo4j_repo, tmp_path
) -> None:
    """Breaks if load_derived_rederive_item feeds the LLM the erased twin's text (or any source
    other than the live episode), if apply stops clearing rederive_pending / nulling the stale
    fact_embedding / finishing the repair as 'done', or if the entity summary is not rewritten."""
    repo = test_neo4j_repo
    tag = f"test-rrd-rewrite-{uuidlib.uuid4()}"
    try:
        ids, adapter = _erase(repo, tmp_path, tag)
        # Scrub at erase (owner ruling 2026-11-09): text and stale embedding are already gone.
        assert _edge(repo, ids["edge"])["embedding"] is None
        assert _edge(repo, ids["edge"])["fact"] == ""
        llm = _FakeLLM(_SUPPORTED)

        counts = _run(_ScopedAdapter(adapter, tag), llm)

        assert counts["rewritten"] == 2
        edge = _edge(repo, ids["edge"])
        assert edge["fact"] == "rewritten fact"
        assert edge["pending"] is None
        assert edge["embedding"] is None
        ent = _entity(repo, ids["entity"])
        assert ent["summary"] == "rewritten summary"
        assert ent["pending"] is None
        repairs = _repairs(repo, tag)
        assert repairs["fact"]["status"] == "done"
        assert repairs["entity"]["status"] == "done"
        assert {c["kind"] for c in llm.calls} == {"fact", "entity"}
        for call in llm.calls:
            assert call["sources"] == [LIVE_TEXT]
            assert all(SECRET not in s for s in call["sources"])
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_job_deletes_fact_the_llm_calls_unsupported(test_neo4j_repo, tmp_path) -> None:
    """Breaks if an 'unsupported' verdict on a fact stops deleting the edge, or the repair is not
    finished as 'deleted'."""
    repo = test_neo4j_repo
    tag = f"test-rrd-unsup-{uuidlib.uuid4()}"
    try:
        ids, adapter = _erase(repo, tmp_path, tag)
        llm = _FakeLLM({"fact": ("unsupported", None), "entity": ("supported", "kept summary")})

        counts = _run(_ScopedAdapter(adapter, tag), llm)

        assert counts["deleted"] == 1
        assert _edge(repo, ids["edge"]) is None
        assert _repairs(repo, tag)["fact"]["status"] == "deleted"
        # The control fact (unflagged, never queued) is untouched.
        assert _edge(repo, ids["control_edge"]) is not None
    finally:
        _cleanup(repo, tag)


# ------------------------------------------------------------------ CAS counterexamples
@pytest.mark.online
def test_counterexample_second_erasure_reflag_between_load_and_apply_refuses_write(
    test_neo4j_repo, tmp_path
) -> None:
    """Stale-state counterexample: a second erasure re-flags the edge (new rederive_flagged_at)
    after the job loaded it. Breaks if apply_derived_rederive's CAS drops the
    rederive_flagged_at comparison: the job would overwrite text derived from a now-stale source
    set and clear the newer flag."""
    repo = test_neo4j_repo
    tag = f"test-rrd-casflag-{uuidlib.uuid4()}"
    try:
        ids, adapter = _erase(repo, tmp_path, tag)

        def reflag() -> None:
            repo.execute(
                "MATCH ()-[r:RELATES_TO {uuid:$u}]->() "
                "SET r.rederive_flagged_at = datetime() + duration('PT1S')",
                params={"u": ids["edge"]},
            )

        counts = _run(_ScopedAdapter(adapter, tag, reflag), _FakeLLM(_SUPPORTED))

        assert counts["cas_failed"] == 1
        assert counts["rewritten"] == 1  # only the entity applied
        edge = _edge(repo, ids["edge"])
        assert edge["fact"] == ""  # still the erase-time scrub, not the refused rewrite
        assert edge["pending"] is True
        assert _repairs(repo, tag)["fact"]["status"] == "pending"
        # And the read path still hides it.
        assert all(
            SECRET not in str(r.get("fact"))
            for r in adapter.fetch_temporal_facts([ids["entity"]])
        )
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_counterexample_episode_set_change_between_load_and_apply_refuses_write(
    test_neo4j_repo, tmp_path
) -> None:
    """Stale-state counterexample: the edge's source-episode set changes after the job read it.
    Breaks if the CAS drops `coalesce(r.episodes, []) = $episodes` (a rewrite computed from the
    old source set would be applied)."""
    repo = test_neo4j_repo
    tag = f"test-rrd-casep-{uuidlib.uuid4()}"
    try:
        ids, adapter = _erase(repo, tmp_path, tag)

        def drop_live_episode() -> None:
            repo.execute(
                "MATCH ()-[r:RELATES_TO {uuid:$u}]->() "
                "SET r.episodes = [x IN r.episodes WHERE x <> $live]",
                params={"u": ids["edge"], "live": ids["live"]},
            )

        counts = _run(_ScopedAdapter(adapter, tag, drop_live_episode), _FakeLLM(_SUPPORTED))

        assert counts["cas_failed"] == 1
        edge = _edge(repo, ids["edge"])
        assert edge["fact"] == ""  # still the erase-time scrub, not the refused rewrite
        assert edge["pending"] is True
        assert _repairs(repo, tag)["fact"]["status"] == "pending"
    finally:
        _cleanup(repo, tag)


# ------------------------------------------------------------------ review fixes (PR #595)
FIXED_VECTOR = [0.25, 0.5, 0.75]


class _FakeEmbedder:
    """Fixed-vector embedder. Offers the surfaces Menhir's embedders expose (graphiti_client's
    ``embed_query(text)`` and Graphiti's ``create(input_data=[...])``), all async."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = list(vector)
        self.texts: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.texts.append(text)
        return list(self.vector)

    async def create(self, input_data: Any) -> list[float]:
        self.texts.append(str(input_data))
        return list(self.vector)


@pytest.mark.online
def test_rewritten_fact_gets_an_embedding_from_the_embedder(test_neo4j_repo, tmp_path) -> None:
    """R-1. A re-derived fact must be searchable again: apply writes the new text AND its
    embedding. Expected signature after the fix:
    ``run_derived_rederive(graph_adapter, llm, *, limit=25, embedder=None)`` where ``embedder``
    has ``async embed_query(text) -> list[float]`` (the graphiti_client surface), passed by the
    scheduler task. Fails today (TypeError on ``embedder=``; and apply writes fact_embedding =
    null unconditionally), or if the job ignores the embedder and leaves the embedding null."""
    repo = test_neo4j_repo
    tag = f"test-rrd-embed-{uuidlib.uuid4()}"
    try:
        ids, adapter = _erase(repo, tmp_path, tag)
        assert _edge(repo, ids["edge"])["embedding"] is None  # scrubbed at erase
        embedder = _FakeEmbedder(FIXED_VECTOR)

        counts = asyncio.run(
            run_derived_rederive(
                _ScopedAdapter(adapter, tag), _FakeLLM(_SUPPORTED), embedder=embedder
            )
        )

        assert counts["rewritten"] == 2
        edge = _edge(repo, ids["edge"])
        assert edge["fact"] == "rewritten fact"
        assert edge["pending"] is None
        assert edge["embedding"] == FIXED_VECTOR
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_fact_with_duplicate_episode_uuid_is_rederived_on_the_first_pass(
    test_neo4j_repo, tmp_path
) -> None:
    """R-2. A flagged shared fact whose stored ``episodes`` repeats a uuid ([live, live]). The job
    reads a de-duplicated list, and the apply CAS compares it with the raw stored property, so the
    write never matches and the fact stays hidden forever. Fails if apply_derived_rederive's
    episodes guard is not compared against the raw stored list (cas_failed==1, fact stays '')."""
    repo = test_neo4j_repo
    tag = f"test-rrd-dupep-{uuidlib.uuid4()}"
    try:
        ids, adapter = _erase(repo, tmp_path, tag)
        repo.execute(
            "MATCH ()-[r:RELATES_TO {uuid:$u}]->() SET r.episodes = [$live, $live]",
            params={"u": ids["edge"], "live": ids["live"]},
        )
        assert _edge(repo, ids["edge"])["episodes"] == [ids["live"], ids["live"]]
        assert _edge(repo, ids["edge"])["pending"] is True

        counts = _run(_ScopedAdapter(adapter, tag), _FakeLLM(_SUPPORTED))

        assert counts["cas_failed"] == 0
        edge = _edge(repo, ids["edge"])
        assert edge["fact"] == "rewritten fact"
        assert edge["pending"] is None
        assert _repairs(repo, tag)["fact"]["status"] == "done"
    finally:
        _cleanup(repo, tag)


# ------------------------------------------------------------------ no sources / no llm / failures
@pytest.mark.online
def test_no_remaining_sources_deletes_fact_without_calling_the_llm(
    test_neo4j_repo, tmp_path
) -> None:
    """Breaks if the empty-sources branch asks the LLM anyway (it would rewrite from nothing) or
    keeps a fact whose only remaining episode uuid has no :Episodic node."""
    repo = test_neo4j_repo
    tag = f"test-rrd-nosrc-{uuidlib.uuid4()}"
    try:
        ghost = f"{tag}-ghost-episode"  # no :Episodic node carries this uuid
        ids, adapter = _erase(repo, tmp_path, tag, edge_episodes=[f"{tag}-graphiti", ghost])
        assert _edge(repo, ids["edge"])["episodes"] == [ghost]  # flagged, stripped to the ghost
        llm = _FakeLLM(_SUPPORTED)

        counts = _run(_ScopedAdapter(adapter, tag), llm)

        assert counts["deleted"] == 1
        assert _edge(repo, ids["edge"]) is None
        assert _repairs(repo, tag)["fact"]["status"] == "deleted"
        assert all(c["kind"] != "fact" for c in llm.calls)
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_unavailable_llm_skips_and_leaves_repairs_pending(test_neo4j_repo, tmp_path) -> None:
    """Breaks if a degraded LLM adapter (no rederive_from_sources) burns attempts into 'failed' or
    clears flags instead of returning {'skipped': 'no_llm'} and leaving everything pending."""
    repo = test_neo4j_repo
    tag = f"test-rrd-nollm-{uuidlib.uuid4()}"
    try:
        ids, adapter = _erase(repo, tmp_path, tag)

        result = _run(_ScopedAdapter(adapter, tag), object())

        assert result == {"skipped": "no_llm"}
        repairs = _repairs(repo, tag)
        assert {k: v["status"] for k, v in repairs.items()} == {
            "fact": "pending",
            "entity": "pending",
        }
        assert all(not v["attempts"] for v in repairs.values())
        assert _edge(repo, ids["edge"])["pending"] is True
        assert _entity(repo, ids["entity"])["pending"] is True
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_repeated_llm_failure_records_class_only_and_stays_hidden(
    test_neo4j_repo, tmp_path
) -> None:
    """Breaks if failure accounting stores the exception message (which here echoes SECRET), does
    not reach 'failed' at MAX_ATTEMPTS, or if giving up clears the flag (the item must stay
    flagged so its text stays hidden)."""
    repo = test_neo4j_repo
    tag = f"test-rrd-fail-{uuidlib.uuid4()}"
    try:
        ids, adapter = _erase(repo, tmp_path, tag)
        llm = _FakeLLM(error=RuntimeError(f"contains {SECRET}"))
        scoped = _ScopedAdapter(adapter, tag)

        for _ in range(4):
            _run(scoped, llm)
        assert {v["status"] for v in _repairs(repo, tag).values()} == {"pending"}
        counts = _run(scoped, llm)

        assert counts["failed"] == 2
        repairs = _repairs(repo, tag)
        for repair in repairs.values():
            assert repair["status"] == "failed"
            assert repair["attempts"] == 5
            assert repair["last_error"] == "RuntimeError"
            assert SECRET not in str(repair["props"])
        assert _edge(repo, ids["edge"])["pending"] is True
        assert _entity(repo, ids["entity"])["pending"] is True
        row = {r["uuid"]: r for r in adapter.fetch_candidate_metadata([ids["entity"]])}[
            ids["entity"]
        ]
        assert row["summary"] is None
        assert row["content"] is None
        # A failed repair is no longer listed, so the next run does not touch it.
        assert _run(scoped, llm)["processed"] == 0
    finally:
        _cleanup(repo, tag)
