"""Live proof that verbatim-skip never leaves a client episode's text unextracted.

SAFETY PROPERTY. A client `:Episodic` whose text is exactly its admitted turn's text may skip
extraction ONLY while a sibling evidence projection with identical text in the same namespace
exists and is not FAILED. If that projection FAILS or is DELETED the client returns to PENDING
and never skips again (`verbatim_skip_revoked`).

WHY ONLINE. The offline sibling (`test_verbatim_skip_worker.py`) cannot prove the Cypher: the
claim fence compares a driver-returned `neo4j.time.DateTime` (stored `Z` round-trips as `[UTC]`)
against the stored property, the eligibility predicates are graph patterns, and the re-enable
query's OPTIONAL MATCH has to see a deleted node as absent. Every claim here uses the REAL
`claim_pending_episode` and feeds its `processing_started_at` straight back as the fence.
"""

from __future__ import annotations

import uuid as uuidlib
from datetime import timedelta

import pytest

from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository

TEXT = "I keep 25 postcards in the blue album"


@pytest.fixture
def tag(test_neo4j_repo):
    t = f"vskip-{uuidlib.uuid4().hex[:10]}"
    try:
        yield t
    finally:
        test_neo4j_repo.execute("MATCH (n {test_tag: $t}) DETACH DELETE n", params={"t": t})


@pytest.fixture
def repo(test_neo4j_repo):
    r = EpisodeLifecycleRepository()
    r.neo4j = test_neo4j_repo
    return r


def _uid(prefix):
    return f"{prefix}-{uuidlib.uuid4().hex[:10]}"


def _turn(raw, tag, ns, text=TEXT):
    tid = _uid("turn")
    raw.execute(
        "CREATE (:TurnEvidence {turn_id: $tid, role: 'user', declarant: 'user', text: $text, "
        "namespace: $ns, recorded_at: datetime(), test_tag: $tag})",
        params={"tid": tid, "text": text, "ns": ns, "tag": tag})
    return tid


def _client(raw, repo, tag, ns, turn_id, text=TEXT):
    cid = _uid("client")
    raw.execute(
        "CREATE (:Episodic {uuid: $u, name: $u, content: $text, namespace: $ns, "
        "processing_state: 'PENDING', processing_attempts: 0, "
        # sorts ahead of any stray PENDING rows so the list_pending (limit 500) check is reliable
        "queued_at: datetime('2000-01-01T00:00:00Z'), test_tag: $tag})",
        params={"u": cid, "text": text, "ns": ns, "tag": tag})
    assert repo.link_episode_admission(episode_uuid=cid, turn_evidence_uuid=turn_id, namespace=ns)
    return cid


def _projection(raw, repo, tag, ns, turn_id):
    puid = repo.create_evidence_projection(
        turn_evidence_uuid=turn_id, projection_uuid=_uid("proj"), name=f"ep-{turn_id}",
        session_id="s", user_id="u", namespace=ns)
    assert puid
    raw.execute("MATCH (p:Episodic {uuid: $u}) SET p.test_tag = $tag", params={"u": puid, "tag": tag})
    return puid


def _world(raw, repo, tag, text=TEXT, ns=None):
    ns = ns or _uid("ns")
    turn = _turn(raw, tag, ns, text)
    client = _client(raw, repo, tag, ns, turn, text)
    proj = _projection(raw, repo, tag, ns, turn)
    return {"ns": ns, "turn": turn, "client": client, "proj": proj}


def _claim(repo, uuid, worker="worker-a"):
    claimed = repo.claim_pending_episode(uuid, max_attempts=3, worker_id=worker, lease_seconds=60)
    assert claimed is not None, "claim_pending_episode must succeed for a PENDING episode"
    assert claimed["processing_started_at"] is not None
    return claimed


def _node(raw, uuid):
    rows = raw.execute("MATCH (n:Episodic {uuid: $u}) RETURN properties(n) AS p", params={"u": uuid})
    return rows[0]["p"] if rows else None


def _assert_still_claimed(raw, cid, worker):
    n = _node(raw, cid)
    assert n["processing_state"] == "ENRICHING"
    assert n["processing_owner"] == worker
    assert n.get("extraction_skipped_for") is None


# 1 -------------------------------------------------------------------------------------------
@pytest.mark.online
def test_happy_path_skips_and_records_projection(test_neo4j_repo, repo, tag):
    w = _world(test_neo4j_repo, repo, tag)
    claimed = _claim(repo, w["client"])

    got = repo.mark_episode_verbatim_skipped(
        w["client"], worker_id="worker-a", claim_started_at=claimed["processing_started_at"])

    assert got == w["proj"]
    n = _node(test_neo4j_repo, w["client"])
    assert n["processing_state"] == "READY"
    assert n["processing_substage"] == "verbatim_skipped"
    assert n["extraction_skipped_for"] == w["proj"]
    assert n.get("processing_owner") is None
    assert n.get("processing_lease_expires_at") is None
    assert n.get("resolved_episode_uuid") is None


# 2 -------------------------------------------------------------------------------------------
@pytest.mark.online
@pytest.mark.parametrize("case", ["wrong_worker", "started_at_off_by_one_second", "started_at_none"])
def test_claim_fence_rejects_skip(test_neo4j_repo, repo, tag, case):
    w = _world(test_neo4j_repo, repo, tag)
    claimed = _claim(repo, w["client"])
    started = claimed["processing_started_at"]
    worker, fence = "worker-a", started
    if case == "wrong_worker":
        worker = "worker-b"
    elif case == "started_at_off_by_one_second":
        fence = started.to_native() + timedelta(seconds=1)
    else:
        fence = None

    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id=worker, claim_started_at=fence) is None
    _assert_still_claimed(test_neo4j_repo, w["client"], "worker-a")


# 3 -------------------------------------------------------------------------------------------
@pytest.mark.online
@pytest.mark.parametrize("b_worker", ["worker-b", "worker-a"])
def test_stale_owner_cannot_skip_a_reclaimed_episode(test_neo4j_repo, repo, tag, b_worker):
    """A loses its lease, B (or A again, same id) re-claims. A's old started_at must not fence in."""
    w = _world(test_neo4j_repo, repo, tag)
    a = _claim(repo, w["client"], "worker-a")
    # What the stale-lease reset does: back to PENDING, owner cleared.
    test_neo4j_repo.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.processing_state = 'PENDING', "
        "n.processing_owner = null, n.processing_lease_expires_at = null",
        params={"u": w["client"]})
    b = _claim(repo, w["client"], b_worker)

    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id="worker-a", claim_started_at=a["processing_started_at"]) is None
    _assert_still_claimed(test_neo4j_repo, w["client"], b_worker)

    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id=b_worker, claim_started_at=b["processing_started_at"]) == w["proj"]
    assert _node(test_neo4j_repo, w["client"])["processing_state"] == "READY"


# 4 -------------------------------------------------------------------------------------------
def _m_trailing_space(raw, repo, tag, w):
    raw.execute("MATCH (n:Episodic {uuid: $u}) SET n.content = n.content + ' '", params={"u": w["client"]})


def _m_proj_failed(raw, repo, tag, w):
    raw.execute("MATCH (p:Episodic {uuid: $u}) SET p.processing_state = 'FAILED'", params={"u": w["proj"]})


def _m_proj_deleted(raw, repo, tag, w):
    raw.execute("MATCH (p:Episodic {uuid: $u}) DETACH DELETE p", params={"u": w["proj"]})


def _m_proj_other_ns(raw, repo, tag, w):
    raw.execute("MATCH (p:Episodic {uuid: $u}) SET p.namespace = $ns",
                params={"u": w["proj"], "ns": _uid("other-ns")})


def _m_two_turns(raw, repo, tag, w):
    t2 = _turn(raw, tag, w["ns"])
    assert repo.link_episode_admission(episode_uuid=w["client"], turn_evidence_uuid=t2, namespace=w["ns"])


def _m_diff(raw, repo, tag, w):
    raw.execute("MATCH (n:Episodic {uuid: $u}) SET n.diff = 'some diff'", params={"u": w["client"]})


def _m_revoked(raw, repo, tag, w):
    raw.execute("MATCH (n:Episodic {uuid: $u}) SET n.verbatim_skip_revoked = true", params={"u": w["client"]})


def _m_resolved(raw, repo, tag, w):
    raw.execute("MATCH (n:Episodic {uuid: $u}) SET n.resolved_episode_uuid = 'already-resolved'",
                params={"u": w["client"]})


def _m_second_projection(raw, repo, tag, w):
    # Ambiguity: two projections over the same turn text -> size(ps) <> 1 -> no skip.
    raw.execute(
        "MATCH (t:TurnEvidence {turn_id: $t}) MATCH (p:Episodic {uuid: $pu}) "
        "CREATE (q:Episodic {uuid: $qu, content: p.content, namespace: p.namespace, "
        "is_evidence_projection: true, evidence_projection_of: $t, "
        "processing_state: 'READY', test_tag: $tag}) CREATE (q)-[:ADMITTED_ON]->(t)",
        params={"t": w["turn"], "pu": w["proj"], "qu": _uid("proj2"), "tag": tag})


_REFUSALS = {
    "text_differs_by_trailing_space": _m_trailing_space,
    "projection_failed": _m_proj_failed,
    "projection_deleted": _m_proj_deleted,
    "projection_in_other_namespace": _m_proj_other_ns,
    "two_admitted_turns": _m_two_turns,
    "client_has_diff": _m_diff,
    "verbatim_skip_revoked": _m_revoked,
    "client_already_resolved": _m_resolved,
    "two_candidate_projections": _m_second_projection,
}


@pytest.mark.online
@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_ineligible_client_is_never_skipped(test_neo4j_repo, repo, tag, case):
    w = _world(test_neo4j_repo, repo, tag)
    claimed = _claim(repo, w["client"])
    _REFUSALS[case](test_neo4j_repo, repo, tag, w)

    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) is None
    _assert_still_claimed(test_neo4j_repo, w["client"], "worker-a")


@pytest.mark.online
def test_the_projection_itself_is_never_skipped(test_neo4j_repo, repo, tag):
    w = _world(test_neo4j_repo, repo, tag)
    claimed = _claim(repo, w["proj"])

    assert repo.mark_episode_verbatim_skipped(
        w["proj"], worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) is None
    _assert_still_claimed(test_neo4j_repo, w["proj"], "worker-a")


# 5 -------------------------------------------------------------------------------------------
@pytest.mark.online
@pytest.mark.parametrize("state", ["PENDING", "ENRICHING", "READY"])
def test_accepted_projection_states(test_neo4j_repo, repo, tag, state):
    w = _world(test_neo4j_repo, repo, tag)
    claimed = _claim(repo, w["client"])
    test_neo4j_repo.execute("MATCH (p:Episodic {uuid: $u}) SET p.processing_state = $s",
                            params={"u": w["proj"], "s": state})

    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) == w["proj"]
    assert _node(test_neo4j_repo, w["client"])["processing_state"] == "READY"


# 6 -------------------------------------------------------------------------------------------
@pytest.mark.online
def test_reenable_after_projection_fails_and_never_skips_again(test_neo4j_repo, repo, tag):
    w = _world(test_neo4j_repo, repo, tag)
    claimed = _claim(repo, w["client"])
    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) == w["proj"]

    assert repo.mark_episode_failed(w["proj"], "boom", worker_id=None) is True
    assert _node(test_neo4j_repo, w["proj"])["processing_state"] == "FAILED"

    revived = repo.reenable_verbatim_skips(limit=500)
    assert w["client"] in revived
    n = _node(test_neo4j_repo, w["client"])
    assert n["processing_state"] == "PENDING"
    assert n["processing_attempts"] == 0
    assert n.get("processing_owner") is None
    assert n.get("processing_started_at") is None
    assert n["verbatim_skip_revoked"] is True
    assert n["verbatim_skip_revoked_for"] == w["proj"]
    assert n.get("extraction_skipped_for") is None

    assert w["client"] not in repo.reenable_verbatim_skips(limit=500)

    # No oscillation: even with the projection healthy again, the client extracts for itself.
    test_neo4j_repo.execute("MATCH (p:Episodic {uuid: $u}) SET p.processing_state = 'PENDING'",
                            params={"u": w["proj"]})
    again = _claim(repo, w["client"], "worker-c")
    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id="worker-c", claim_started_at=again["processing_started_at"]) is None
    _assert_still_claimed(test_neo4j_repo, w["client"], "worker-c")


# 7 -------------------------------------------------------------------------------------------
@pytest.mark.online
def test_reenable_after_projection_is_deleted(test_neo4j_repo, repo, tag):
    w = _world(test_neo4j_repo, repo, tag)
    claimed = _claim(repo, w["client"])
    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) == w["proj"]

    test_neo4j_repo.execute("MATCH (p:Episodic {uuid: $u}) DETACH DELETE p", params={"u": w["proj"]})

    assert w["client"] in repo.reenable_verbatim_skips(limit=500)
    n = _node(test_neo4j_repo, w["client"])
    assert n["processing_state"] == "PENDING"
    assert n["verbatim_skip_revoked"] is True
    assert n["verbatim_skip_revoked_for"] == w["proj"]
    assert n.get("extraction_skipped_for") is None


# 8 -------------------------------------------------------------------------------------------
@pytest.mark.online
@pytest.mark.parametrize("state", ["PENDING", "ENRICHING", "READY"])
def test_no_reenable_while_projection_is_live(test_neo4j_repo, repo, tag, state):
    w = _world(test_neo4j_repo, repo, tag)
    claimed = _claim(repo, w["client"])
    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) == w["proj"]
    test_neo4j_repo.execute("MATCH (p:Episodic {uuid: $u}) SET p.processing_state = $s",
                            params={"u": w["proj"], "s": state})

    assert w["client"] not in repo.reenable_verbatim_skips(limit=500)
    n = _node(test_neo4j_repo, w["client"])
    assert n["processing_state"] == "READY"
    assert n["extraction_skipped_for"] == w["proj"]
    assert not n.get("verbatim_skip_revoked")


# 9 -------------------------------------------------------------------------------------------
@pytest.mark.online
def test_projection_uuid_filter_revokes_only_its_clients(test_neo4j_repo, repo, tag):
    ns = _uid("ns")
    w1 = _world(test_neo4j_repo, repo, tag, text=TEXT + " one", ns=ns)
    w2 = _world(test_neo4j_repo, repo, tag, text=TEXT + " two", ns=ns)
    for w in (w1, w2):
        claimed = _claim(repo, w["client"])
        assert repo.mark_episode_verbatim_skipped(
            w["client"], worker_id="worker-a",
            claim_started_at=claimed["processing_started_at"]) == w["proj"]
    for w in (w1, w2):
        assert repo.mark_episode_failed(w["proj"], "boom", worker_id=None) is True

    revived = repo.reenable_verbatim_skips(projection_uuid=w1["proj"], limit=500)

    assert w1["client"] in revived
    assert w2["client"] not in revived
    assert _node(test_neo4j_repo, w1["client"])["processing_state"] == "PENDING"
    other = _node(test_neo4j_repo, w2["client"])
    assert other["processing_state"] == "READY"
    assert other["extraction_skipped_for"] == w2["proj"]


# 10 ------------------------------------------------------------------------------------------
@pytest.mark.online
def test_revoked_client_is_listed_pending_for_the_worker(test_neo4j_repo, repo, tag):
    w = _world(test_neo4j_repo, repo, tag)
    claimed = _claim(repo, w["client"])
    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) == w["proj"]
    assert w["client"] not in repo.list_pending_episode_uuids(max_attempts=3, limit=500)

    test_neo4j_repo.execute("MATCH (p:Episodic {uuid: $u}) DETACH DELETE p", params={"u": w["proj"]})
    assert w["client"] in repo.reenable_verbatim_skips(limit=500)

    assert w["client"] in repo.list_pending_episode_uuids(max_attempts=3, limit=500)


# 10 ------------------------------------------------------------------------------------------
@pytest.mark.online
def test_skipped_client_status_follows_its_projection(test_neo4j_repo, repo, tag):
    from menhir.domain.verbatim_skip_status import (
        PROJECTION_LOST_SUBSTAGE,
        effective_processing_row,
    )

    w = _world(test_neo4j_repo, repo, tag)
    claimed = _claim(repo, w["client"])
    assert repo.mark_episode_verbatim_skipped(
        w["client"], worker_id="worker-a", claim_started_at=claimed["processing_started_at"]) == w["proj"]

    # Projection still PENDING: the raw client row is READY, the effective row is not.
    raw_row = repo.fetch_episode_processing(w["client"])
    assert raw_row["processing_state"] == "READY"
    assert raw_row["extraction_skipped_for"] == w["proj"]
    assert raw_row["skip_projection"]["processing_state"] == "PENDING"
    assert effective_processing_row(raw_row)["processing_state"] == "PENDING"

    # Projection READY with a resolved episode: effective row is READY and carries it.
    resolved = _uid("resolved")
    test_neo4j_repo.execute(
        "MATCH (p:Episodic {uuid: $u}) SET p.processing_state = 'READY', p.resolved_episode_uuid = $r",
        params={"u": w["proj"], "r": resolved})
    eff = effective_processing_row(repo.fetch_episode_processing(w["client"]))
    assert eff["processing_state"] == "READY"
    assert eff["resolved_episode_uuid"] == resolved

    # Projection FAILED: the client reports PENDING / projection lost.
    test_neo4j_repo.execute("MATCH (p:Episodic {uuid: $u}) SET p.processing_state = 'FAILED'",
                            params={"u": w["proj"]})
    eff = effective_processing_row(repo.fetch_episode_processing(w["client"]))
    assert eff["processing_state"] == "PENDING"
    assert eff["processing_substage"] == PROJECTION_LOST_SUBSTAGE
    # the raw row is untouched for work fences
    assert repo.fetch_episode_processing(w["client"])["processing_state"] == "READY"


# 11 ------------------------------------------------------------------------------------------
@pytest.mark.online
def test_normal_episode_has_no_skip_fields(test_neo4j_repo, repo, tag):
    w = _world(test_neo4j_repo, repo, tag)
    row = repo.fetch_episode_processing(w["client"])
    assert row["extraction_skipped_for"] is None
    assert row["skip_projection"] is None
