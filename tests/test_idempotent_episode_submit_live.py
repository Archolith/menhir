"""Live proof that idempotent episode submit holds against a real Neo4j.

WHY ONLINE. The offline tests stub the adapter. What makes the feature correct under concurrency
is the `episodic_ingest_key_unique` constraint: the intake pre-check can race, so the database
must decide the winner and the losers must recover by re-reading. Only a real Neo4j can show
that, and that `is_constraint_violation` matches the error the repo's driver wrapper surfaces.

Enrichment is disabled on the test intake, so nothing is queued or extracted.
"""

from __future__ import annotations

import asyncio
import threading
import uuid as uuidlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from menhir.domain import IngestStatus, MemorySession
from menhir.domain.ingest import (
    IdempotencyKeyConflict,
    episode_ingest_key,
    episode_request_digest,
    is_constraint_violation,
)
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.ingest_intake import IngestIntakeMixin

pytestmark = pytest.mark.online


class _Intake(IngestIntakeMixin):
    def __init__(self, adapter):
        self.graph_adapter = adapter
        self._enrichment_enabled = False


def _session() -> MemorySession:
    return MemorySession(
        session_id=f"s-{uuidlib.uuid4().hex[:8]}", user_id="u",
        started_at=datetime.now(timezone.utc), client_name="live-test",
    )


@pytest.fixture
def env(test_neo4j_repo):
    raw = test_neo4j_repo
    adapter = MemoryGraphAdapter(neo4j=raw)
    adapter.bootstrap_phase_one()
    namespaces: list[str] = []

    def new_ns() -> str:
        ns = f"idem-{uuidlib.uuid4().hex[:10]}"
        namespaces.append(ns)
        return ns

    yield SimpleEnv(raw, adapter, new_ns)
    for ns in namespaces:
        raw.execute("MATCH (n:Episodic {namespace: $ns}) DETACH DELETE n", params={"ns": ns})


class SimpleEnv:
    def __init__(self, raw, adapter, new_ns):
        self.raw = raw
        self.adapter = adapter
        self.new_ns = new_ns

    def submit(self, *, ns, key, content="remembered fact"):
        return asyncio.run(
            _Intake(self.adapter).queue_episode_for_enrichment(
                episode=content, session=_session(), source="claude-code",
                namespace=ns, idempotency_key=key,
            )
        )

    def anchors(self, ns, key=None):
        if key is None:
            return self.raw.execute(
                "MATCH (n:Episodic {namespace: $ns}) RETURN n.uuid AS uuid, "
                "n.ingest_key AS ingest_key", params={"ns": ns})
        return self.raw.execute(
            "MATCH (n:Episodic {ingest_key: $k}) RETURN n.uuid AS uuid, n.namespace AS ns",
            params={"k": episode_ingest_key(key, ns)})


def test_sequential_resubmit_is_a_replay_with_one_anchor(env):
    ns, key = env.new_ns(), "k-seq"
    first = env.submit(ns=ns, key=key)
    second = env.submit(ns=ns, key=key)

    assert first.status == IngestStatus.QUEUED and first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert second.episode_id == first.episode_id
    assert second.processing_state == "PENDING"
    rows = env.anchors(ns, key)
    assert [r["uuid"] for r in rows] == [first.episode_id]
    stored = env.adapter.find_episode_by_ingest_key(episode_ingest_key(key, ns))
    assert stored["ingest_request_sha256"] == episode_request_digest(
        content="remembered fact", diff=None, source="claude-code", flagged=False,
        bootstrap_scope=None, occurred_at=None, role=None, turn_evidence_uuid=None,
        user_statement=None,
    )


def test_same_key_different_content_raises_conflict_and_creates_nothing(env):
    ns, key = env.new_ns(), "k-conflict"
    env.submit(ns=ns, key=key, content="first body")
    with pytest.raises(IdempotencyKeyConflict):
        env.submit(ns=ns, key=key, content="second body")
    assert len(env.anchors(ns)) == 1


def test_concurrent_submits_with_one_key_produce_one_anchor(env):
    ns, key, n = env.new_ns(), "k-race", 16
    barrier = threading.Barrier(n)

    def go(_):
        barrier.wait(timeout=30)
        return env.submit(ns=ns, key=key)

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(go, range(n)))

    assert len(env.anchors(ns, key)) == 1
    assert len({r.episode_id for r in results}) == 1
    assert all(r.episode_id for r in results)
    assert not [r for r in results if r.status == IngestStatus.FAILED]
    assert sum(1 for r in results if not r.idempotent_replay) == 1


def test_same_key_in_two_namespaces_makes_two_anchors(env):
    ns_a, ns_b = env.new_ns(), env.new_ns()
    a = env.submit(ns=ns_a, key="shared")
    b = env.submit(ns=ns_b, key="shared")
    assert a.episode_id != b.episode_id
    assert b.idempotent_replay is False
    assert len(env.anchors(ns_a)) == 1 and len(env.anchors(ns_b)) == 1


def test_no_key_twice_makes_two_anchors(env):
    ns = env.new_ns()
    a = env.submit(ns=ns, key=None)
    b = env.submit(ns=ns, key=None)
    assert a.episode_id != b.episode_id
    assert len(env.anchors(ns)) == 2
    assert all(r["ingest_key"] is None for r in env.anchors(ns))


def test_constraint_exists_and_raw_duplicate_is_recognised_as_violation(env):
    rows = env.raw.execute(
        "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties "
        "WHERE name = 'episodic_ingest_key_unique' RETURN type, labelsOrTypes, properties")
    assert len(rows) == 1
    assert rows[0]["type"] == "UNIQUENESS"
    assert list(rows[0]["labelsOrTypes"]) == ["Episodic"]
    assert list(rows[0]["properties"]) == ["ingest_key"]

    ns = env.new_ns()
    k = f"raw-{uuidlib.uuid4().hex}"
    create = ("CREATE (:Episodic {uuid: $u, namespace: $ns, ingest_key: $k})")
    env.raw.execute(create, params={"u": str(uuidlib.uuid4()), "ns": ns, "k": k})
    with pytest.raises(Exception) as excinfo:
        env.raw.execute(create, params={"u": str(uuidlib.uuid4()), "ns": ns, "k": k})
    assert "ConstraintValidationFailed" in str(getattr(excinfo.value, "code", ""))
    assert is_constraint_violation(excinfo.value)
    assert len(env.raw.execute(
        "MATCH (n:Episodic {ingest_key: $k}) RETURN n", params={"k": k})) == 1
