"""Live checks for the durable policy-empty label on READY :Episodic rows.

PROPERTY. A READY row's `policy_empty_reason` was written by the write that made it READY, so a
label from an earlier attempt never survives into a later READY. `reset_zero_extraction_episodes`
requeues unlabelled zero-extraction rows and leaves labelled (decided) ones READY.

WHY ONLINE. The label rides on the real fenced Cypher writes (claim instant round-tripped through
the driver) and the reset sweep's graph predicates; a fake cannot prove either.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.episode_maintenance import EpisodeMaintenanceRepository

TEXT = "What would you like to know about LG's 4K smart TVs?"


@pytest.fixture
def tag(test_neo4j_repo):
    t = f"pempty-{uuidlib.uuid4().hex[:10]}"
    try:
        yield t
    finally:
        test_neo4j_repo.execute("MATCH (n {test_tag: $t}) DETACH DELETE n", params={"t": t})


@pytest.fixture
def repo(test_neo4j_repo):
    r = EpisodeLifecycleRepository()
    r.neo4j = test_neo4j_repo
    return r


@pytest.fixture
def maintenance(test_neo4j_repo):
    m = EpisodeMaintenanceRepository()
    m.neo4j = test_neo4j_repo
    return m


def _uid(prefix):
    return f"{prefix}-{uuidlib.uuid4().hex[:10]}"


def _pending(raw, tag, text=TEXT, ns=None):
    uid = _uid("ep")
    raw.execute(
        "CREATE (:Episodic {uuid: $u, name: $u, content: $text, namespace: $ns, "
        "processing_state: 'PENDING', processing_attempts: 0, "
        "queued_at: datetime('2000-01-01T00:00:00Z'), test_tag: $tag})",
        params={"u": uid, "text": text, "ns": ns or _uid("ns"), "tag": tag})
    return uid


def _claim(repo, uuid, worker):
    claimed = repo.claim_pending_episode(uuid, max_attempts=3, worker_id=worker, lease_seconds=60)
    assert claimed is not None and claimed["processing_started_at"] is not None
    return claimed


def _ready_empty(repo, uuid, worker, **reason):
    claimed = _claim(repo, uuid, worker)
    assert repo.mark_episode_ready(
        uuid, worker_id=worker, resolved_episode_uuid=uuid, nodes_touched=0, edges_touched=0,
        claim_started_at=claimed["processing_started_at"], **reason)


def _node(raw, uuid):
    rows = raw.execute("MATCH (n:Episodic {uuid: $u}) RETURN properties(n) AS p", params={"u": uuid})
    return rows[0]["p"]


def _requeue(raw, uuid):
    # Any path that returns a READY row to PENDING (operator re-enrich, repair) without
    # touching the label: the stale value must not outlive the next READY write.
    raw.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.processing_state = 'PENDING', "
        "n.processing_attempts = 0, n.processing_owner = null, n.processing_started_at = null",
        params={"u": uuid})


@pytest.mark.online
def test_ready_write_stores_the_reason(test_neo4j_repo, repo, tag):
    ep = _pending(test_neo4j_repo, tag)
    _ready_empty(repo, ep, "worker-a", policy_empty_reason="claimless_question")

    n = _node(test_neo4j_repo, ep)
    assert n["processing_state"] == "READY"
    assert n["policy_empty_reason"] == "claimless_question"


@pytest.mark.online
def test_label_from_attempt_a_does_not_survive_attempt_b(test_neo4j_repo, repo, tag):
    """Attempt A decides policy-empty; the row is requeued; attempt B completes with no reason."""
    ep = _pending(test_neo4j_repo, tag)
    _ready_empty(repo, ep, "worker-a", policy_empty_reason="claimless_question")
    _requeue(test_neo4j_repo, ep)

    _ready_empty(repo, ep, "worker-b")  # no reason passed: the write must clear, not keep

    n = _node(test_neo4j_repo, ep)
    assert n["processing_state"] == "READY"
    assert n.get("policy_empty_reason") is None


@pytest.mark.online
def test_stale_attempt_cannot_label_a_newer_claim(test_neo4j_repo, repo, tag):
    """A's late READY write is fenced out, so B's claim never carries A's label."""
    ep = _pending(test_neo4j_repo, tag)
    claimed_a = _claim(repo, ep, "worker-a")
    _requeue(test_neo4j_repo, ep)
    _claim(repo, ep, "worker-b")

    assert not repo.mark_episode_ready(
        ep, worker_id="worker-a", resolved_episode_uuid=ep, nodes_touched=0, edges_touched=0,
        claim_started_at=claimed_a["processing_started_at"],
        policy_empty_reason="claimless_question")

    n = _node(test_neo4j_repo, ep)
    assert n["processing_state"] == "ENRICHING"
    assert n.get("policy_empty_reason") is None


@pytest.mark.online
def test_zero_extraction_reset_skips_labelled_rows(test_neo4j_repo, repo, maintenance, tag):
    labelled = _pending(test_neo4j_repo, tag)
    unlabelled = _pending(test_neo4j_repo, tag)
    _ready_empty(repo, labelled, "worker-a", policy_empty_reason="claimless_question")
    _ready_empty(repo, unlabelled, "worker-a")

    assert maintenance.reset_zero_extraction_episodes() >= 1

    assert _node(test_neo4j_repo, labelled)["processing_state"] == "READY"
    reset = _node(test_neo4j_repo, unlabelled)
    assert reset["processing_state"] == "PENDING"
    assert reset["processing_error"] == "zero_extraction_reset"


@pytest.mark.online
def test_verbatim_skip_ready_write_clears_a_stale_label(test_neo4j_repo, repo, tag):
    ns = _uid("ns")
    turn = _uid("turn")
    test_neo4j_repo.execute(
        "CREATE (:TurnEvidence {turn_id: $tid, role: 'user', declarant: 'user', text: $text, "
        "namespace: $ns, recorded_at: datetime(), test_tag: $tag})",
        params={"tid": turn, "text": TEXT, "ns": ns, "tag": tag})
    client = _pending(test_neo4j_repo, tag, ns=ns)
    assert repo.link_episode_admission(episode_uuid=client, turn_evidence_uuid=turn, namespace=ns)
    proj = repo.create_evidence_projection(
        turn_evidence_uuid=turn, projection_uuid=_uid("proj"), name=f"ep-{turn}",
        session_id="s", user_id="u", namespace=ns)
    test_neo4j_repo.execute(
        "MATCH (p:Episodic {uuid: $u}) SET p.test_tag = $tag", params={"u": proj, "tag": tag})
    test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.policy_empty_reason = 'claimless_question'",
        params={"u": client})
    claimed = _claim(repo, client, "worker-a")

    assert repo.mark_episode_verbatim_skipped(
        client, worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) == proj

    n = _node(test_neo4j_repo, client)
    assert n["processing_state"] == "READY"
    assert n.get("policy_empty_reason") is None


def _verbatim_skipped_client(raw, repo, tag):
    """A client completed READY by the real verbatim-skip write; returns (client, projection)."""
    ns = _uid("ns")
    turn = _uid("turn")
    raw.execute(
        "CREATE (:TurnEvidence {turn_id: $tid, role: 'user', declarant: 'user', text: $text, "
        "namespace: $ns, recorded_at: datetime(), test_tag: $tag})",
        params={"tid": turn, "text": TEXT, "ns": ns, "tag": tag})
    client = _pending(raw, tag, ns=ns)
    assert repo.link_episode_admission(episode_uuid=client, turn_evidence_uuid=turn, namespace=ns)
    proj = repo.create_evidence_projection(
        turn_evidence_uuid=turn, projection_uuid=_uid("proj"), name=f"ep-{turn}",
        session_id="s", user_id="u", namespace=ns)
    raw.execute("MATCH (p:Episodic {uuid: $u}) SET p.test_tag = $tag", params={"u": proj, "tag": tag})
    claimed = _claim(repo, client, "worker-a")
    assert repo.mark_episode_verbatim_skipped(
        client, worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) == proj
    return client, proj


@pytest.mark.online
def test_zero_extraction_reset_skips_verbatim_skipped_clients(
    test_neo4j_repo, repo, maintenance, tag,
):
    """A skipped client is READY 0/0 with no Entity by design: its projection carries the text."""
    client, proj = _verbatim_skipped_client(test_neo4j_repo, repo, tag)
    unlabelled = _pending(test_neo4j_repo, tag)
    _ready_empty(repo, unlabelled, "worker-a")

    assert maintenance.reset_zero_extraction_episodes() >= 1

    n = _node(test_neo4j_repo, client)
    assert n["processing_state"] == "READY"
    assert n["extraction_skipped_for"] == proj
    assert n.get("processing_error") is None
    assert _node(test_neo4j_repo, unlabelled)["processing_state"] == "PENDING"


@pytest.mark.online
def test_stale_skip_pointer_does_not_exempt_a_later_normal_ready(
    test_neo4j_repo, repo, maintenance, tag,
):
    """extraction_skipped_for left over from an earlier skip must not hide a real empty READY."""
    ep = _pending(test_neo4j_repo, tag)
    test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.extraction_skipped_for = 'old-projection'",
        params={"u": ep})
    _ready_empty(repo, ep, "worker-a")  # normal READY: substage 'complete', pointer still set

    assert maintenance.reset_zero_extraction_episodes() >= 1

    n = _node(test_neo4j_repo, ep)
    assert n["processing_state"] == "PENDING"
    assert n["processing_error"] == "zero_extraction_reset"
