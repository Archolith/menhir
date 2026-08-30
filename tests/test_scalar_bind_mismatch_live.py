"""Live proof of the C.4.4.1 already-bound identity-mismatch invariant against a REAL Neo4j.

The offline suite (test_typed_assertion_repository.py) proves the mismatch clause is PRESENT and
correctly gated in the record cypher, but it routes cypher through a fake and cannot execute MERGE.
These online tests execute the real write and assert the DURABLE outcome the fake cannot: after a
mismatched record, the CURRENT pointer still points at A, the head owner is still A, the assertion
carries exactly ONE HAS_ASSERTION owner (A, never a second B edge), and A's bound assertion stays
non-pending and materializable.

The invariant belongs to the SOURCE CLAIM (head), across every assertion VERSION and interpretation:
once a claim is durably bound to A, a record presenting a different subject — a newer
perceiver_version, a changed value/slot, or an unresolved `unbound:` sentinel — must NOT rebind it.
Rebinding is the merge path's job. Runs only with `--run-online` against the stood-up TEST instance
(:7688); the autouse `force_all_tests_onto_test_neo4j` fixture guarantees the target is never prod.
"""

from __future__ import annotations

import threading
import uuid as uuidlib

import pytest

from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.infrastructure.view_repository import ViewClass


@pytest.fixture
def live_repo(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.activate_scalar_state()
    return TypedAssertionRepository(test_neo4j_repo)


def _mk_entities(repo, *uuids):
    for u in uuids:
        repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": u})


def _mk_episode(repo, uuid):
    repo.execute(
        "MERGE (e:Episodic {uuid:$u}) "
        "SET e.evidence_finalized = true, e.evidence_generation = 0",
        {"u": uuid},
    )


def _assertion(*, subject_uuid, episode_uuid, span_start, span_end, value=37,
               perceiver_version="v1", evidence_tier="agent", attribute="owned", value_kind="count"):
    return TypedAssertion(
        subject_uuid=subject_uuid, subject_display=subject_uuid, attribute=attribute, scope="",
        value_kind=value_kind, unit="", operation="absolute", value=value,
        stated_span="a total", span_start=span_start, span_end=span_end,
        episode_uuid=episode_uuid, valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", evidence_tier=evidence_tier,
        perceiver_version=perceiver_version)


def _current(repo, source_key):
    rows = repo.execute(
        """
        MATCH (h:TypedAssertionHead {source_key:$sk})-[:CURRENT]->(a:TypedAssertion)
        RETURN h.subject_uuid AS head_owner, a.subject_uuid AS cur_owner,
               a.assertion_key AS cur_key, coalesce(a.binding_pending, false) AS pending,
               a.perceiver_version AS pv, a.evidence_tier AS tier
        """,
        {"sk": source_key},
    )
    return rows[0] if rows else None


def _owner_edges(repo, source_key):
    # every HAS_ASSERTION owner of the CURRENT assertion for this claim
    rows = repo.execute(
        """
        MATCH (h:TypedAssertionHead {source_key:$sk})-[:CURRENT]->(a:TypedAssertion)
        MATCH (n:Entity)-[:HAS_ASSERTION]->(a)
        RETURN collect(n.uuid) AS owners
        """,
        {"sk": source_key},
    )
    return sorted(rows[0]["owners"]) if rows else []


@pytest.mark.online
def test_newer_version_different_subject_does_not_rebind_A(live_repo, test_neo4j_repo):
    # regression 1: v1/A current, then v2/B for the SAME source claim. A must stay current + sole
    # owner; B gets no ownership edge; the head owner stays A.
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    a_uuid, b_uuid = f"A-{ep}", f"B-{ep}"
    _mk_entities(test_neo4j_repo, a_uuid, b_uuid)
    _mk_episode(test_neo4j_repo, ep)

    a1 = _assertion(subject_uuid=a_uuid, episode_uuid=ep, span_start=0, span_end=7,
                    perceiver_version="v1")
    r1 = live_repo.record_assertion(a1)
    assert not r1["binding_pending"] and not r1["binding_mismatch"]

    a2 = _assertion(subject_uuid=b_uuid, episode_uuid=ep, span_start=0, span_end=7,
                    perceiver_version="v2", value=99)
    r2 = live_repo.record_assertion(a2)
    assert r2["binding_mismatch"] is True

    cur = _current(test_neo4j_repo, a1.source_key)
    assert cur["cur_owner"] == a_uuid and cur["head_owner"] == a_uuid   # A still current + head owner
    assert cur["pv"] == "v1"                                            # v2 did NOT become current
    assert _owner_edges(test_neo4j_repo, a1.source_key) == [a_uuid]     # exactly one owner, no B edge


@pytest.mark.online
def test_changed_interpretation_different_subject_does_not_move_current(live_repo, test_neo4j_repo):
    # regression 2: same source claim + same version, changed value/slot AND a different subject.
    # No current movement; A remains the sole owner.
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    a_uuid, b_uuid = f"A-{ep}", f"B-{ep}"
    _mk_entities(test_neo4j_repo, a_uuid, b_uuid)
    _mk_episode(test_neo4j_repo, ep)

    a1 = _assertion(subject_uuid=a_uuid, episode_uuid=ep, span_start=0, span_end=7, value=37)
    live_repo.record_assertion(a1)
    a2 = _assertion(subject_uuid=b_uuid, episode_uuid=ep, span_start=0, span_end=7,
                    value=40, attribute="sold")     # changed slot + subject, same version
    r2 = live_repo.record_assertion(a2)
    assert r2["binding_mismatch"] is True

    cur = _current(test_neo4j_repo, a1.source_key)
    assert cur["cur_key"] == a1.assertion_key and cur["cur_owner"] == a_uuid
    assert _owner_edges(test_neo4j_repo, a1.source_key) == [a_uuid]


@pytest.mark.online
def test_reperceived_as_unbound_sentinel_keeps_A_materializable(live_repo, test_neo4j_repo):
    # regression 3: an A-bound claim re-perceived when resolution now yields zero/multiple, so the
    # caller presents an `unbound:` sentinel. A must stay non-pending and materializable (the sentinel
    # cannot de-authorize A) — mismatch is NOT gated on the presented entity existing.
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    a_uuid = f"A-{ep}"
    _mk_entities(test_neo4j_repo, a_uuid)
    _mk_episode(test_neo4j_repo, ep)

    a1 = _assertion(subject_uuid=a_uuid, episode_uuid=ep, span_start=0, span_end=7)
    live_repo.record_assertion(a1)
    sentinel = _assertion(subject_uuid=f"unbound:{a1.source_key}", episode_uuid=ep,
                          span_start=0, span_end=7)
    r2 = live_repo.record_assertion(sentinel)
    assert r2["binding_mismatch"] is True

    cur = _current(test_neo4j_repo, a1.source_key)
    assert cur["cur_owner"] == a_uuid and cur["pending"] is False       # A stays bound, non-pending
    mat = live_repo.materializable_assertions_for_entity(a_uuid)
    assert any(m["subject_uuid"] == a_uuid for m in mat)                # still materializable


@pytest.mark.online
def test_mismatched_higher_tier_rewrite_does_not_upgrade_A(live_repo, test_neo4j_repo):
    # regression 4: a mismatched higher-tier (user) rewrite for a different subject must NOT upgrade
    # A's current evidence tier.
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    a_uuid, b_uuid = f"A-{ep}", f"B-{ep}"
    _mk_entities(test_neo4j_repo, a_uuid, b_uuid)
    _mk_episode(test_neo4j_repo, ep)

    a1 = _assertion(subject_uuid=a_uuid, episode_uuid=ep, span_start=0, span_end=7,
                    evidence_tier="agent")
    live_repo.record_assertion(a1)
    a2 = _assertion(subject_uuid=b_uuid, episode_uuid=ep, span_start=0, span_end=7,
                    evidence_tier="user")           # higher tier, different subject
    r2 = live_repo.record_assertion(a2)
    assert r2["binding_mismatch"] is True

    cur = _current(test_neo4j_repo, a1.source_key)
    assert cur["cur_owner"] == a_uuid and cur["tier"] == "agent"        # tier NOT upgraded by B


@pytest.mark.online
def test_pending_advisory_adopts_resolved_subject_on_reperception(live_repo, test_neo4j_repo):
    # C.4.4 pending-binding repair (live adoption): an advisory (unbound sentinel, binding_pending)
    # re-recorded with a now-resolvable real subject must land on the SAME node (assertion_key omits
    # subject_uuid), adopt the identity onto the assertion + head, clear binding_pending, and become
    # materializable — exactly ONE node, real owner, no duplicate.
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    a_uuid = f"A-{ep}"
    _mk_episode(test_neo4j_repo, ep)
    # advisory first: the entity does not exist yet, so binding cannot resolve -> sentinel subject.
    advisory = _assertion(subject_uuid="unbound:sentinel", episode_uuid=ep, span_start=0, span_end=7)
    r1 = live_repo.record_assertion(advisory)
    assert r1["binding_pending"] is True

    # the entity now exists; re-record with the resolved subject (same interpretation => same key).
    _mk_entities(test_neo4j_repo, a_uuid)
    resolved = _assertion(subject_uuid=a_uuid, episode_uuid=ep, span_start=0, span_end=7)
    assert resolved.assertion_key == advisory.assertion_key       # omits subject_uuid -> same node
    r2 = live_repo.record_assertion(resolved)
    assert r2["binding_pending"] is False and not r2["binding_mismatch"]

    cur = _current(test_neo4j_repo, advisory.source_key)
    assert cur["cur_owner"] == a_uuid and cur["head_owner"] == a_uuid   # adopted onto assertion + head
    assert cur["pending"] is False
    assert _owner_edges(test_neo4j_repo, advisory.source_key) == [a_uuid]  # exactly one owner
    # exactly one assertion node exists for this claim (adoption, never a fork)
    n = test_neo4j_repo.execute(
        "MATCH (a:TypedAssertion {source_key:$sk}) RETURN count(a) AS c", {"sk": advisory.source_key})
    assert n[0]["c"] == 1
    assert any(m["subject_uuid"] == a_uuid
               for m in live_repo.materializable_assertions_for_entity(a_uuid))


@pytest.mark.online
def test_full_repair_path_binds_advisory_end_to_end(test_neo4j_repo):
    # C.4.4.2 full path through the coordinator: pending_advisory_assertions -> repair -> entity
    # lookup -> reconstruction -> re-record -> rebuild, against a real Neo4j. Proves the repair
    # actually resolves + materializes an advisory once its entity appears, not just record_assertion.
    from menhir.services.typed_scalar_perception import TypedScalarPerceptionService

    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.activate_scalar_state()
    repo = TypedAssertionRepository(test_neo4j_repo)

    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    name = f"user-{uuidlib.uuid4().hex[:6]}"
    _mk_episode(test_neo4j_repo, ep)
    # advisory: entity absent, and the linked Entity that repair will match carries `name`.
    advisory = TypedAssertion(
        subject_uuid="unbound:x", subject_display=name, attribute="wake", scope="",
        value_kind="clock_time", unit="", operation="absolute", value="07:30",
        stated_span="a total", span_start=0, span_end=7, episode_uuid=ep,
        valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        evidence_tier="agent", perceiver_version="v1")
    assert repo.record_assertion(advisory)["binding_pending"] is True

    # the entity now exists and is linked to the episode so fetch_linked_entities_for_episode finds it.
    ent = f"E-{ep}"
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$name", {"u": ent, "name": name})
    test_neo4j_repo.execute(
        "MATCH (e:Episodic {uuid:$ep}),(n:Entity {uuid:$u}) MERGE (e)-[:MENTIONS]->(n)",
        {"ep": ep, "u": ent})

    coord = TypedScalarPerceptionService(adapter, adapter.scalar_state_service())
    out = coord.repair_pending_bindings()
    assert out["repaired"] >= 1

    cur = _current(test_neo4j_repo, advisory.source_key)
    assert cur["cur_owner"] == ent and cur["pending"] is False
    assert any(m["subject_uuid"] == ent
               for m in repo.materializable_assertions_for_entity(ent))


@pytest.mark.online
def test_global_repair_rebuilds_view_in_the_rows_namespace(test_neo4j_repo):
    # C.4.4.2 blocker 1: the ordinary GLOBAL repair sweep (no namespace arg) must rebuild a tenant-a
    # advisory's View into tenant-a, NOT the default silo. Proves the View node carries the row's
    # namespace and no default-namespace duplicate is created.
    from menhir.services.typed_scalar_perception import TypedScalarPerceptionService

    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.activate_scalar_state()
    repo = TypedAssertionRepository(test_neo4j_repo)

    ns = f"tenant-{uuidlib.uuid4().hex[:6]}"
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    name = f"user-{uuidlib.uuid4().hex[:6]}"
    _mk_episode(test_neo4j_repo, ep)
    advisory = TypedAssertion(
        subject_uuid="unbound:x", subject_display=name, attribute="wake", scope="",
        value_kind="clock_time", unit="", operation="absolute", value="07:30",
        stated_span="a total", span_start=0, span_end=7, episode_uuid=ep,
        valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        evidence_tier="agent", perceiver_version="v1", namespace=ns)
    assert repo.record_assertion(advisory)["binding_pending"] is True

    ent = f"E-{ep}"
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$name", {"u": ent, "name": name})
    test_neo4j_repo.execute(
        "MATCH (e:Episodic {uuid:$ep}),(n:Entity {uuid:$u}) MERGE (e)-[:MENTIONS]->(n)",
        {"ep": ep, "u": ent})

    # GLOBAL sweep (no namespaces arg) — the bug would rebuild into the default namespace.
    coord = TypedScalarPerceptionService(adapter, adapter.scalar_state_service())
    assert coord.repair_pending_bindings()["repaired"] >= 1

    views = test_neo4j_repo.execute(
        "MATCH (v:Entity {view_kind:'scalar_state', view_subject_uuid:$u}) "
        "RETURN v.namespace AS namespace, v.view_key AS view_key", {"u": ent})
    assert views and all(v["namespace"] == ns for v in views)          # rebuilt in the row namespace
    assert not any((v["namespace"] or "default") == "default" for v in views)  # no default silo View


@pytest.mark.online
def test_orphan_repair_is_namespace_isolated(test_neo4j_repo):
    # C.4.4.3 blocker 1: a tenant-a orphan repaired under namespaces=["tenant-a"] must move + rebuild
    # ONLY tenant-a state, in tenant-a's silo — never tenant-b, never the default namespace.
    from menhir.services.scalar_state_service import ScalarStateService

    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.activate_scalar_state()
    repo = TypedAssertionRepository(test_neo4j_repo)
    svc = ScalarStateService(repo, adapter)

    tag = uuidlib.uuid4().hex[:6]
    # THE SAME absorbed uuid carries BOTH tenants' assertions — this is the dangerous case an
    # unscoped `MATCH (a:TypedAssertion {subject_uuid: $absorbed})` would get wrong. A distinct
    # tenant-b entity would NOT exercise the namespace predicate at all.
    dead_shared, surv_a = f"dead-{tag}", f"survA-{tag}"
    ep_a, ep_b = f"epA-{tag}", f"epB-{tag}"
    ns_a, ns_b = f"tenant-a-{tag}", f"tenant-b-{tag}"
    _mk_episode(test_neo4j_repo, ep_a)
    _mk_episode(test_neo4j_repo, ep_b)
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u})", {"u": surv_a})   # survivor exists

    def _assn(subject, ep, ns, span):
        return TypedAssertion(
            subject_uuid=subject, subject_display=subject, attribute="owned", scope="",
            value_kind="count", unit="", operation="absolute", value=5,
            stated_span="a total", span_start=span, span_end=span + 3, episode_uuid=ep,
            valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
            evidence_tier="agent", perceiver_version="v1", namespace=ns)

    # Build a REAL orphan: the absorbed Entity must EXIST when the assertions are recorded (so they
    # bind, binding_pending=false, NOT advisories), then be DELETED to simulate a merge whose
    # post-commit rebind never ran. Recording against a missing Entity would only make an advisory,
    # which orphaned_assertions excludes. BOTH tenants bind to the SAME absorbed uuid.
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u})", {"u": dead_shared})
    rec_a = repo.record_assertion(_assn(dead_shared, ep_a, ns_a, 0))
    rec_b = repo.record_assertion(_assn(dead_shared, ep_b, ns_b, 10))
    assert rec_a["binding_pending"] is False and rec_b["binding_pending"] is False
    # now delete the shared Entity -> both assertions are true orphans on one dead subject_uuid
    test_neo4j_repo.execute("MATCH (n:Entity {uuid:$u}) DETACH DELETE n", {"u": dead_shared})
    assert any(w["subject_uuid"] == dead_shared
               for w in repo.orphaned_assertions(namespaces=[ns_a]))

    merges = [{"op_id": f"op-{tag}", "absorbed_uuid": dead_shared, "survivor_uuid": surv_a}]
    out = svc.repair_orphaned_assertions(
        committed_merges=merges, allowed_namespaces={ns_a}, limit=50)
    assert out["repaired"] == [dead_shared]

    # ONLY tenant-a's assertion moved to the survivor; tenant-b's stayed on the dead uuid.
    owners = {r["namespace"]: r["subject_uuid"] for r in test_neo4j_repo.execute(
        "MATCH (a:TypedAssertion) WHERE a.namespace IN $ns "
        "RETURN a.namespace AS namespace, a.subject_uuid AS subject_uuid", {"ns": [ns_a, ns_b]})}
    assert owners[ns_a] == surv_a                      # tenant-a rebound
    assert owners[ns_b] == dead_shared                 # tenant-b UNTOUCHED on the dead uuid

    # tenant-a View rebuilt in tenant-a's silo; tenant-b got NO survivor View; no default-silo View.
    a_views = adapter.list_scalar_state_views(subject_uuid=surv_a, namespace=ns_a)
    assert a_views and all(v.get("namespace") == ns_a for v in a_views)
    assert adapter.list_scalar_state_views(subject_uuid=surv_a, namespace=ns_b) == []
    default_views = test_neo4j_repo.execute(
        "MATCH (v:Entity {view_kind:'scalar_state', view_subject_uuid:$u}) "
        "WHERE coalesce(v.namespace,'default')='default' RETURN count(v) AS c", {"u": surv_a})
    assert default_views[0]["c"] == 0
    # retry stamps affect ONLY tenant-a's assertion.
    stamped = test_neo4j_repo.execute(
        "MATCH (a:TypedAssertion) WHERE a.namespace IN $ns "
        "RETURN a.namespace AS namespace, a.orphan_repair_attempted_at IS NOT NULL AS stamped",
        {"ns": [ns_a, ns_b]})
    by_ns = {r["namespace"]: r["stamped"] for r in stamped}
    assert by_ns[ns_a] is True and by_ns[ns_b] is False


@pytest.mark.online
def test_idempotent_same_subject_rewrite_is_not_a_mismatch(live_repo, test_neo4j_repo):
    # control: re-recording the SAME claim for the SAME subject is a normal idempotent write, not a
    # mismatch, and remains bound + materializable.
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    a_uuid = f"A-{ep}"
    _mk_entities(test_neo4j_repo, a_uuid)
    _mk_episode(test_neo4j_repo, ep)

    a1 = _assertion(subject_uuid=a_uuid, episode_uuid=ep, span_start=0, span_end=7)
    live_repo.record_assertion(a1)
    r2 = live_repo.record_assertion(a1)
    assert r2["binding_mismatch"] is False and r2["binding_pending"] is False
    assert _owner_edges(test_neo4j_repo, a1.source_key) == [a_uuid]


@pytest.mark.online
def test_two_worker_unmerge_repair_is_idempotent_under_concurrency(test_neo4j_repo):
    # FINAL LIVE GATE (2-worker concurrency): two independent scheduler workers -- each on its OWN
    # driver -- repair the SAME receiptless unmerge at the SAME instant. The convergence guarantee is
    # exactly-once: the head keeps ONE current assertion with ONE owner, at most ONE completion receipt
    # exists, and the system settles deterministically. Idempotency rests only on MERGE-keyed identity
    # (rebind_key, receipt_key), fail-closed restore (a second restore matches the already-deleted
    # records and no-ops), per-op exception isolation in the repair loop, and the namespace-keyed
    # pending marker written before restore consumes the AssertionRebind evidence.
    from menhir.services.scalar_state_service import ScalarStateService

    MemoryGraphAdapter(neo4j=test_neo4j_repo).bootstrap_phase_one()
    MemoryGraphAdapter(neo4j=test_neo4j_repo).activate_scalar_state()

    tag = uuidlib.uuid4().hex[:6]
    a_uuid, b_uuid, ep = f"A-{tag}", f"B-{tag}", f"ep-{tag}"
    ns, op1, u_op = f"tenant-{tag}", f"op1-{tag}", f"uop-{tag}"
    _mk_entities(test_neo4j_repo, a_uuid, b_uuid)
    _mk_episode(test_neo4j_repo, ep)

    setup_repo = TypedAssertionRepository(test_neo4j_repo)
    a1 = TypedAssertion(
        subject_uuid=a_uuid, subject_display=a_uuid, attribute="owned", scope="",
        value_kind="count", unit="", operation="absolute", value=5, stated_span="a total",
        span_start=0, span_end=7, episode_uuid=ep, valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", evidence_tier="agent", perceiver_version="v1",
        namespace=ns)
    rec = setup_repo.record_assertion(a1)
    assert rec["binding_pending"] is False
    # forward merge A->B (op1): moves the assertion onto B, journals a namespace-scoped AssertionRebind,
    # writes the merge receipt. This is the state a committed-but-scalar-unfinished unmerge reverses.
    ScalarStateService(setup_repo, MemoryGraphAdapter(neo4j=test_neo4j_repo)).handle_merge(
        absorbed_uuid=a_uuid, survivor_uuid=b_uuid, merge_op_id=op1, namespace=ns)
    assert _current(test_neo4j_repo, a1.source_key)["cur_owner"] == b_uuid   # on B now

    unmerges = [{"op_id": u_op, "merge_op_id": op1, "absorbed_uuid": a_uuid, "survivor_uuid": b_uuid}]

    workers, errors, barrier = [], [], threading.Barrier(2)

    def _worker():
        repo = Neo4jRepository(uri=test_neo4j_repo.uri, database=test_neo4j_repo.database,
                               user=test_neo4j_repo.user, password=test_neo4j_repo.password)
        try:
            svc = ScalarStateService(TypedAssertionRepository(repo), MemoryGraphAdapter(neo4j=repo))
            barrier.wait(timeout=10)          # release both workers into the repair at once
            svc.repair_incomplete_reconciliations(
                committed_unmerges=unmerges, allowed_namespaces={ns})
        except Exception as exc:  # noqa: BLE001 - surface any thread failure to the main assertions
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            repo.close()

    for _ in range(2):
        t = threading.Thread(target=_worker)
        t.start()
        workers.append(t)
    for t in workers:
        t.join(timeout=30)

    assert not errors, f"worker(s) raised: {errors}"        # per-op isolation means no thread throws
    assert not any(t.is_alive() for t in workers)           # neither deadlocked

    # ---- exactly-once invariants that CANNOT hold if concurrency double-applied ------------------
    def _q(cy, **p):
        return test_neo4j_repo.execute(cy, p)

    current_ct = _q("MATCH (h:TypedAssertionHead {source_key:$sk})-[:CURRENT]->(a) RETURN count(a) AS c",
                    sk=a1.source_key)[0]["c"]
    assert current_ct == 1                                   # never two CURRENT edges
    assert _owner_edges(test_neo4j_repo, a1.source_key) == [a_uuid]   # restored to A, single owner
    assert _current(test_neo4j_repo, a1.source_key)["cur_owner"] == a_uuid
    rebinds_left = _q("MATCH (r:AssertionRebind {merge_op_id:$op}) RETURN count(r) AS c", op=op1)[0]["c"]
    assert rebinds_left == 0                                 # restore consumed the op's rebind records
    receipt_ct = _q("MATCH (rc:ScalarReconcile {operation_id:$u, operation_kind:'ENTITY_UNMERGE'}) "
                    "WHERE rc.status='complete' RETURN count(rc) AS c", u=u_op)[0]["c"]
    assert receipt_ct == 1                                   # one completion receipt, not two

    # ---- convergence: a third, sequential repair is a clean no-op ---------------------------------
    final = ScalarStateService(setup_repo, MemoryGraphAdapter(neo4j=test_neo4j_repo)
                               ).repair_incomplete_reconciliations(
        committed_unmerges=unmerges, allowed_namespaces={ns})
    assert final["repaired"] == 0 and final["unmerge_op_ids"] == []
    assert _q("MATCH (rc:ScalarReconcile {operation_id:$u, operation_kind:'ENTITY_UNMERGE'}) "
              "WHERE rc.status='complete' RETURN count(rc) AS c", u=u_op)[0]["c"] == 1


@pytest.mark.online
def test_two_worker_projection_pending_repair_writes_one_current_view(test_neo4j_repo):
    # FINAL LIVE GATE (2-worker projection-pending race): the View writer reads the current version in
    # one query, then CREATEs a new node with a random uuid -- so two workers rebuilding the SAME
    # projection_pending assertion can each observe "no current" and each create a view_current=true
    # node for the slot (view_key had only a plain index, no uniqueness boundary). This test drives that
    # exact path and SYNCHRONIZES the two workers at the projection-write boundary -- inside
    # _current_by_key, AFTER the read and BEFORE the CREATE -- so both provably see no current before
    # either writes. The DB-level `ss_view_key_current` uniqueness constraint (C.4.4.4) is what makes
    # the losing CREATE fail and converge on the winner instead of forking a duplicate current View.
    from menhir.services.scalar_state_service import ScalarStateService
    from menhir.services.typed_scalar_perception import TypedScalarPerceptionService

    setup_adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    setup_adapter.bootstrap_phase_one()
    setup_adapter.activate_scalar_state()                    # brings the new uniqueness constraint online
    setup_repo = TypedAssertionRepository(test_neo4j_repo)

    tag = uuidlib.uuid4().hex[:6]
    a_uuid, ep, ns = f"A-{tag}", f"ep-{tag}", f"tenant-{tag}"
    _mk_entities(test_neo4j_repo, a_uuid)
    _mk_episode(test_neo4j_repo, ep)

    a1 = TypedAssertion(
        subject_uuid=a_uuid, subject_display=a_uuid, attribute="owned", scope="",
        value_kind="count", unit="", operation="absolute", value=5, stated_span="a total",
        span_start=0, span_end=7, episode_uuid=ep, valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", evidence_tier="agent", perceiver_version="v1",
        namespace=ns)
    rec = setup_repo.record_assertion(a1)
    assert rec["binding_pending"] is False
    # crash state: assertion is BOUND but its View projection never ran. Mark it projection_pending so
    # the repair pass selects it; record_assertion does NOT build a View, so there is no current View
    # yet -- both workers will race to create the first one.
    test_neo4j_repo.execute(
        "MATCH (a:TypedAssertion {source_key:$sk}) WHERE NOT coalesce(a.superseded,false) "
        "SET a.projection_pending = true", {"sk": a1.source_key})
    assert test_neo4j_repo.execute(
        "MATCH (v:Entity {view_kind:'scalar_state', view_subject_uuid:$u}) RETURN count(v) AS c",
        {"u": a_uuid})[0]["c"] == 0                           # no View exists pre-race

    barrier, errors = threading.Barrier(2), []

    def _worker():
        repo = Neo4jRepository(uri=test_neo4j_repo.uri, database=test_neo4j_repo.database,
                               user=test_neo4j_repo.user, password=test_neo4j_repo.password)
        try:
            adapter = MemoryGraphAdapter(neo4j=repo)
            svc = TypedScalarPerceptionService(adapter, ScalarStateService(
                TypedAssertionRepository(repo), adapter))
            # Interpose at the EXACT projection-write boundary: run the real current-read, then block
            # BOTH workers until each has read (both see None) before either is allowed to CREATE.
            orig_current = adapter._views._current_by_key
            state = {"synced": False}

            def _synced_current(key, *, view_class=ViewClass.FACT):
                res = orig_current(key, view_class=view_class)
                if not state["synced"]:
                    state["synced"] = True
                    barrier.wait(timeout=15)
                return res

            adapter._views._current_by_key = _synced_current
            svc.repair_pending_bindings(namespaces=[ns], limit=10)
        except Exception as exc:  # noqa: BLE001 - surface any thread failure to the assertions
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            repo.close()

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"worker(s) raised: {errors}"
    assert not any(t.is_alive() for t in threads)

    def _q(cy, **p):
        return test_neo4j_repo.execute(cy, p)

    # exactly one TypedAssertion (one CURRENT, one owner) for the claim
    assert _q("MATCH (a:TypedAssertion {source_key:$sk}) WHERE NOT coalesce(a.superseded,false) "
              "RETURN count(a) AS c", sk=a1.source_key)[0]["c"] == 1
    assert _q("MATCH (h:TypedAssertionHead {source_key:$sk})-[:CURRENT]->(a) RETURN count(a) AS c",
              sk=a1.source_key)[0]["c"] == 1
    assert _owner_edges(test_neo4j_repo, a1.source_key) == [a_uuid]

    # exactly ONE current scalar_state View for the slot, in the assertion's namespace
    cur = _q("MATCH (v:Entity {view_kind:'scalar_state', view_subject_uuid:$u}) "
             "WHERE coalesce(v.view_current,true) "
             "RETURN count(v) AS c, collect(DISTINCT v.group_id) AS ns, "
             "collect(DISTINCT v.view_key) AS keys", u=a_uuid)[0]
    assert cur["c"] == 1                                      # NOT two current Views (the race outcome)
    assert cur["ns"] == [ns]                                  # correct silo
    view_key = cur["keys"][0]
    # no default-silo View leaked
    assert _q("MATCH (v:Entity {view_kind:'scalar_state', view_subject_uuid:$u}) "
              "WHERE coalesce(v.view_current,true) AND coalesce(v.group_id,'')='' "
              "RETURN count(v) AS c", u=a_uuid)[0]["c"] == 0
    # no duplicate versions for the key: an unchanged projection built ONCE -> exactly one node total
    assert _q("MATCH (v:Entity {view_kind:'scalar_state', view_key:$k}) RETURN count(v) AS c",
              k=view_key)[0]["c"] == 1
    # the DB uniqueness boundary holds: exactly one node owns the current-key marker
    assert _q("MATCH (v:Entity {ss_view_key_current:$k}) RETURN count(v) AS c", k=view_key)[0]["c"] == 1
    # projection marker cleared on the assertion
    assert _q("MATCH (a:TypedAssertion {source_key:$sk}) WHERE NOT coalesce(a.superseded,false) "
              "RETURN coalesce(a.projection_pending,false) AS p", sk=a1.source_key)[0]["p"] is False

    # third repair pass is a clean no-op (nothing left projection_pending)
    final = TypedScalarPerceptionService(setup_adapter, ScalarStateService(setup_repo, setup_adapter)
                                         ).repair_pending_bindings(namespaces=[ns], limit=10)
    assert final["repaired"] == 0 and final["scanned"] == 0
    assert _q("MATCH (v:Entity {view_kind:'scalar_state', view_key:$k}) "
              "WHERE coalesce(v.view_current,true) RETURN count(v) AS c", k=view_key)[0]["c"] == 1
