"""ScalarState merge/unmerge lifecycle rebinding (ScalarStateView Piece C.3).

Offline, with STATEFUL fakes that model the durable per-op :AssertionRebind lineage and the
:ScalarReconcile receipts, so chain reversal, scoped head restoration, and partial-failure repair
are exercised end to end (not just query shape). Invariant: entity identity remains correct across
ALL lifecycle changes. The riskiest cross-system piece — its own file.
"""

from __future__ import annotations

import pytest

from menhir.services.scalar_state_service import ScalarStateService


def _row(assertion_id, subject_uuid, attribute, value, valid_at, *, scope="", value_kind="count",
         unit="", operation="absolute", tier="user", episode="ep", claim_key=None, source_key=None):
    return {
        "assertion_id": assertion_id, "subject_uuid": subject_uuid, "subject_display": subject_uuid,
        "attribute": attribute, "scope": scope, "value_kind": value_kind, "unit": unit,
        "operation": operation, "value": value, "valid_at": valid_at,
        "learned_at": valid_at, "evidence_tier": tier, "episode_uuid": episode,
        "binding_pending": False, "superseded": False,
        "claim_key": claim_key or f"ck-{assertion_id}",
        "source_key": source_key or f"src-{assertion_id}",
    }


class _StatefulAssertions:
    """In-memory :TypedAssertion + :AssertionRebind + :ScalarReconcile store."""

    def __init__(self, rows, *, existing_entities=None):
        self.rows = [dict(r) for r in rows]
        self.rebinds = []      # AssertionRebind records: {op, assertion_id, from, to, claim_key}
        self.receipts = set()  # (op, kind, ns) with a COMPLETE reconcile receipt
        self.intents = set()   # (op, kind, ns) markers written BEFORE the work (status='pending')
        # None => every subject_uuid is treated as a live Entity (no orphans). A set models exactly
        # which Entity nodes still exist, so orphaned_assertions can find dead subjects.
        self._existing = existing_entities
        self._orphan_attempted: dict = {}    # subject_uuid -> fairness stamp

    def materializable_assertions_for_entity(self, subject_uuid, *, namespace=None):
        return [dict(r) for r in self.rows
                if r["subject_uuid"] == subject_uuid
                and not r.get("binding_pending") and not r.get("superseded")
                and (namespace is None or r.get("namespace") == namespace)]

    def rebind_assertions(self, *, absorbed_uuid, survivor_uuid, merge_op_id, namespace=None):
        if absorbed_uuid == survivor_uuid:
            return {"rebound": 0, "source_keys": []}
        moved, sks = 0, []
        for r in self.rows:
            if r["subject_uuid"] == absorbed_uuid and (namespace is None or r.get("namespace") == namespace):
                # journal the per-op lineage entry (DB-unique on rebind_key = op::aid), then move.
                rebind_key = f"{merge_op_id}::{r['assertion_id']}"
                if not any(rb["rebind_key"] == rebind_key for rb in self.rebinds):
                    self.rebinds.append({"rebind_key": rebind_key, "op": merge_op_id,
                                         "assertion_id": r["assertion_id"],
                                         "from": absorbed_uuid, "to": survivor_uuid,
                                         "source_key": r["source_key"], "namespace": r.get("namespace")})
                r["subject_uuid"] = survivor_uuid
                sks.append(r["source_key"])
                moved += 1
        return {"rebound": moved, "source_keys": sks}

    def restore_rebound_assertions(self, *, merge_op_id, namespace=None):
        recs = [rb for rb in self.rebinds if rb["op"] == merge_op_id
                and (namespace is None or rb.get("namespace") == namespace)]
        sks, from_uuids = [], set()
        for rb in recs:
            for r in self.rows:
                if r["assertion_id"] == rb["assertion_id"]:
                    r["subject_uuid"] = rb["from"]      # back to the owner recorded at THIS op
                    sks.append(rb["source_key"])
                    from_uuids.add(rb["from"])
        consumed = {rb["rebind_key"] for rb in recs}
        self.rebinds = [rb for rb in self.rebinds if rb["rebind_key"] not in consumed]
        return {"restored": len(recs), "source_keys": sks, "from_uuids": sorted(from_uuids)}

    def record_reconcile_receipt(self, *, operation_id, operation_kind, merge_op_id=None,
                                 namespace=None):
        # receipts are NAMESPACE-KEYED: (op, kind, namespace). The completion write PROMOTES the
        # pending marker on the same key (same node in the real store), so the marker survives.
        self.receipts.add((operation_id, operation_kind, namespace))

    def record_reconcile_intent(self, *, operation_id, operation_kind, merge_op_id=None,
                                namespace=None):
        # pending marker on the same receipt key; never downgrades a complete receipt.
        self.intents.add((operation_id, operation_kind, namespace))

    def reconcile_complete(self, operation_id, *, operation_kind=None, namespace=None,
                           any_namespace=False):
        for (o, k, ns) in self.receipts:
            if o != operation_id:
                continue
            if operation_kind is not None and k != operation_kind:
                continue
            if any_namespace or ns == namespace:
                return True
        return False

    def entity_exists(self, uuid):
        # None => treat every subject as a live entity (no orphans in that fixture)
        return True if self._existing is None else (uuid in self._existing)

    def namespaces_for_operation(self, *, merge_op_id, absorbed_uuid=None):
        # the op's OWN affected assertions: its rebind records + rows still on the absorbed uuid.
        out: list = []
        for rb in self.rebinds:
            if rb["op"] == merge_op_id and rb.get("namespace") not in out:
                out.append(rb.get("namespace"))
        if absorbed_uuid:
            for r in self.rows:
                if (r["subject_uuid"] == absorbed_uuid and not r.get("superseded")
                        and r.get("namespace") not in out):
                    out.append(r.get("namespace"))
        return out

    def namespaces_for_unmerge(self, *, unmerge_op_id, merge_op_id):
        # forward merge's SURVIVING rebind records UNION this unmerge's own reconcile markers
        # (pending or complete). Restore deletes the rebind records, so the marker is what keeps a
        # receiptless unmerge discoverable after a restore-ok/rebuild-failed crash.
        out: list = []
        for rb in self.rebinds:
            if rb["op"] == merge_op_id and rb.get("namespace") not in out:
                out.append(rb.get("namespace"))
        for (o, k, ns) in sorted(self.intents | self.receipts, key=lambda t: (t[0], t[1], str(t[2]))):
            if o == unmerge_op_id and k == "ENTITY_UNMERGE" and ns not in out:
                out.append(ns)
        return out

    def orphaned_assertions(self, *, namespaces=None, limit=200):
        if self._existing is None:
            return []                       # every subject is a live entity in this fixture
        seen: list[dict] = []
        seen_keys: set = set()
        for r in self.rows:
            u = r["subject_uuid"]
            ns = r.get("namespace")
            if (not r.get("superseded") and not r.get("binding_pending")
                    and u not in self._existing and (u, ns) not in seen_keys
                    and (namespaces is None or ns in namespaces)):
                # fairness: unattempted first, then least-recently attempted, keyed by (uuid, ns)
                seen.append({"subject_uuid": u, "namespace": ns,
                             "_attempted": self._orphan_attempted.get((u, ns))})
                seen_keys.add((u, ns))
        seen.sort(key=lambda w: (w["_attempted"] is not None, w["_attempted"] or "", w["subject_uuid"]))
        return [{"subject_uuid": w["subject_uuid"], "namespace": w["namespace"]}
                for w in seen[:limit]]

    def mark_orphan_repair_attempted(self, work_items, *, at):
        for w in work_items:
            key = (w["subject_uuid"], w.get("namespace"))
            self._orphan_attempted[key] = at + f"#{w['subject_uuid']}"
        return len(work_items)


class _StatefulViews:
    def __init__(self):
        self.current = {}

    def record_scalar_state(self, **kw):
        slot = (kw["subject_uuid"], kw["attribute"], kw["scope"], kw["value_kind"], kw["unit"])
        vk = "::".join(slot)
        self.current[slot] = {"subject_uuid": kw["subject_uuid"], "view_key": vk,
                              "attribute": kw["attribute"], "scope": kw["scope"],
                              "value_kind": kw["value_kind"], "unit": kw["unit"], "value": kw["value"]}
        return {"view_key": vk}

    def list_scalar_state_views(self, *, subject_uuid, namespace=None):
        return [dict(v) for v in self.current.values() if v["subject_uuid"] == subject_uuid]

    def retire_scalar_state(self, *, view_key):
        for slot, v in list(self.current.items()):
            if v["view_key"] == view_key:
                del self.current[slot]
                return True
        return False

    def value_for(self, subject_uuid, attribute):
        for (su, at, *_r), v in self.current.items():
            if su == subject_uuid and at == attribute:
                return v["value"]
        return None


def _svc(rows):
    a, vw = _StatefulAssertions(rows), _StatefulViews()
    return ScalarStateService(a, vw), a, vw


def _svc_with_entities(rows, existing):
    a, vw = _StatefulAssertions(rows, existing_entities=set(existing)), _StatefulViews()
    return ScalarStateService(a, vw), a, vw


@pytest.mark.unit
def test_merge_rebinds_and_rebuilds_onto_survivor():
    rows = [
        _row("aA", "entA", "owned", 5, "2026-07-01T00:00:00+00:00"),
        _row("aB", "entB", "owned", 8, "2026-08-01T00:00:00+00:00"),
    ]
    svc, assertions, views = _svc(rows)
    svc.rebuild_scalar_state("entA")
    svc.rebuild_scalar_state("entB")
    out = svc.handle_merge(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    assert out["rebound"] == 1
    assert views.value_for("entA", "owned") is None            # absorbed View retired
    assert views.value_for("entB", "owned") == 8               # survivor folds union, latest wins
    assert assertions.reconcile_complete("op1", operation_kind="ENTITY_MERGE") is True        # receipt written after all stages


@pytest.mark.unit
def test_duplicate_slot_resolves_by_latest_no_collision():
    rows = [
        _row("aA", "entA", "owned", 37, "2026-07-01T00:00:00+00:00"),
        _row("aB", "entB", "owned", 40, "2026-09-01T00:00:00+00:00"),
    ]
    svc, _a, views = _svc(rows)
    svc.handle_merge(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    assert views.value_for("entB", "owned") == 40             # latest anchor across the union


@pytest.mark.unit
def test_merge_chain_and_unmerge_returns_both_origins_to_b():
    # REGRESSION 1: A->B (op1), B->C (op2). Unmerge op2 (C->B) must return A-origin AND B-native
    # assertions to B.
    rows = [
        _row("aA", "entA", "owned", 1, "2026-06-01T00:00:00+00:00"),
        _row("aB", "entB", "sold", 2, "2026-07-01T00:00:00+00:00"),  # distinct slot on B
        _row("aC", "entC", "owned", 3, "2026-08-01T00:00:00+00:00"),
    ]
    svc, assertions, views = _svc(rows)
    svc.handle_merge(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    svc.handle_merge(absorbed_uuid="entB", survivor_uuid="entC", merge_op_id="op2")
    assert all(r["subject_uuid"] == "entC" for r in assertions.rows)   # all on C

    svc.handle_unmerge(survivor_uuid="entC", absorbed_uuid="entB", merge_op_id="op2", unmerge_op_id="u-op2")
    owner = {r["assertion_id"]: r["subject_uuid"] for r in assertions.rows}
    assert owner["aA"] == "entB" and owner["aB"] == "entB"  # both A-origin AND B-native back to B
    assert owner["aC"] == "entC"                            # C-native stays
    assert views.value_for("entB", "owned") == 1 and views.value_for("entB", "sold") == 2
    assert views.value_for("entC", "owned") == 3


@pytest.mark.unit
def test_chain_second_unmerge_returns_only_a_origin_to_a():
    # REGRESSION 2: after unmerging op2, unmerge op1 (B->A) returns ONLY A-origin to A.
    rows = [
        _row("aA", "entA", "owned", 1, "2026-06-01T00:00:00+00:00"),
        _row("aB", "entB", "sold", 2, "2026-07-01T00:00:00+00:00"),
        _row("aC", "entC", "owned", 3, "2026-08-01T00:00:00+00:00"),
    ]
    svc, assertions, _v = _svc(rows)
    svc.handle_merge(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    svc.handle_merge(absorbed_uuid="entB", survivor_uuid="entC", merge_op_id="op2")
    svc.handle_unmerge(survivor_uuid="entC", absorbed_uuid="entB", merge_op_id="op2", unmerge_op_id="u-op2")
    svc.handle_unmerge(survivor_uuid="entB", absorbed_uuid="entA", merge_op_id="op1", unmerge_op_id="u-op1")
    owner = {r["assertion_id"]: r["subject_uuid"] for r in assertions.rows}
    assert owner["aA"] == "entA"    # A-origin restored to A
    assert owner["aB"] == "entB"    # B-native stayed on B (op1 never moved it)
    assert owner["aC"] == "entC"


@pytest.mark.unit
def test_survivor_native_heads_stay_on_survivor_after_unmerge():
    # REGRESSION 3: a simple A->B unmerge must not drag B-native claims to A. (Modeled by
    # subject_uuid ownership: B's own assertion must remain on B.)
    rows = [
        _row("aA", "entA", "owned", 5, "2026-07-01T00:00:00+00:00"),
        _row("aB", "entB", "owned", 8, "2026-08-01T00:00:00+00:00"),  # B-native, same slot
    ]
    svc, assertions, views = _svc(rows)
    svc.handle_merge(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    svc.handle_unmerge(survivor_uuid="entB", absorbed_uuid="entA", merge_op_id="op1", unmerge_op_id="u-op1")
    owner = {r["assertion_id"]: r["subject_uuid"] for r in assertions.rows}
    assert owner["aA"] == "entA" and owner["aB"] == "entB"   # B-native NOT dragged to A
    assert views.value_for("entA", "owned") == 5 and views.value_for("entB", "owned") == 8


@pytest.mark.unit
def test_merge_is_idempotent_on_replay():
    # REGRESSION 5 (partial): replay of a committed merge rebinds nothing new (lineage already
    # recorded) and re-folds to the same Views.
    rows = [
        _row("aA", "entA", "owned", 5, "2026-07-01T00:00:00+00:00"),
        _row("aB", "entB", "owned", 8, "2026-08-01T00:00:00+00:00"),
    ]
    svc, _a, views = _svc(rows)
    first = svc.handle_merge(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    second = svc.handle_merge(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    assert first["rebound"] == 1 and second["rebound"] == 0
    assert views.value_for("entB", "owned") == 8


@pytest.mark.unit
def test_repair_finds_partial_failure_rebind_ok_rebuild_failed():
    # REGRESSION 6: a committed merge whose rebind succeeded but rebuild crashed leaves NO dead
    # subject_uuid (assertions are on the live survivor) — only the missing receipt reveals it. The
    # repair pass re-runs reconciliation for committed merges lacking a receipt.
    rows = [
        _row("aA", "entA", "owned", 5, "2026-07-01T00:00:00+00:00"),
        _row("aB", "entB", "owned", 8, "2026-08-01T00:00:00+00:00"),
    ]
    svc, assertions, views = _svc(rows)
    # Simulate the partial failure: rebind ran, receipt NOT written, View stale/absent.
    assertions.rebind_assertions(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    assert assertions.reconcile_complete("op1", operation_kind="ENTITY_MERGE") is False
    assert views.value_for("entB", "owned") is None   # rebuild never happened

    out = svc.repair_incomplete_reconciliations(
        committed_merges=[{"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"}])
    assert out["repaired"] == 1 and out["merge_op_ids"] == ["op1"]
    assert views.value_for("entB", "owned") == 8       # repaired
    assert assertions.reconcile_complete("op1", operation_kind="ENTITY_MERGE") is True
    # a second repair is a no-op (receipt now present)
    assert svc.repair_incomplete_reconciliations(
        committed_merges=[{"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"}])["repaired"] == 0


@pytest.mark.unit
def test_transitive_repair_replays_downstream_chain_even_with_receipt():
    # C.4.4.3: A->B (op1) FAILED (no receipt) but B->C (op2) SUCCEEDED (receipt present). Repairing
    # op1 rebinds A's assertions onto B; op2's receipt is now stale because those assertions must
    # travel B->C. The repair must REPLAY op2 (despite its receipt) so aA reaches the final survivor C.
    rows = [
        _row("aA", "entA", "owned", 1, "2026-06-01T00:00:00+00:00"),
        _row("aC", "entC", "sold", 3, "2026-08-01T00:00:00+00:00"),
    ]
    svc, assertions, views = _svc(rows)
    # op2 (B->C) already completed with a receipt; op1 (A->B) committed but its scalar hook failed.
    assertions.receipts.add(("op2", "ENTITY_MERGE", None))
    merges = [
        {"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"},
        {"op_id": "op2", "absorbed_uuid": "entB", "survivor_uuid": "entC"},
    ]
    out = svc.repair_incomplete_reconciliations(committed_merges=merges)
    assert out["merge_op_ids"] == ["op1"]                       # op1 was the one missing a receipt
    assert "op2" in out["downstream_replayed_op_ids"]          # op2 replayed despite its receipt
    assert all(r["subject_uuid"] == "entC" for r in assertions.rows)   # aA traveled A->B->C
    assert views.value_for("entC", "owned") == 1 and views.value_for("entC", "sold") == 3


@pytest.mark.unit
def test_unmerge_restore_ok_rebuild_failed_stays_discoverable_and_repairs_once():
    # C.4.4.3 crash window: restore succeeds -> rebuild raises -> no completion receipt. Restore has
    # ALREADY deleted the forward op's AssertionRebind records, which were the only durable carrier of
    # this unmerge's namespace. Without the pending marker written BEFORE restore, namespace discovery
    # finds nothing and the receiptless unmerge is skipped FOREVER.
    rows = [
        {**_row("aA", "entA", "owned", 5, "2026-07-01T00:00:00+00:00"), "namespace": "ns-a"},
        {**_row("aB", "entB", "sold", 8, "2026-08-01T00:00:00+00:00"), "namespace": "ns-a"},
    ]
    svc, assertions, views = _svc(rows)
    svc.handle_merge(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1", namespace="ns-a")
    assert [rb["namespace"] for rb in assertions.rebinds] == ["ns-a"]   # namespace-scoped rebind

    # first unmerge attempt: restore lands, rebuild raises.
    real_record = views.record_scalar_state
    def _boom(**_kw):
        raise RuntimeError("rebuild failed")
    views.record_scalar_state = _boom
    with pytest.raises(RuntimeError):
        svc.handle_unmerge(survivor_uuid="entB", absorbed_uuid="entA", merge_op_id="op1",
                           unmerge_op_id="u-op1", namespace="ns-a")
    views.record_scalar_state = real_record

    owner = {r["assertion_id"]: r["subject_uuid"] for r in assertions.rows}
    assert owner["aA"] == "entA"                     # restore DID happen
    assert assertions.rebinds == []                  # ...and consumed the rebind records
    assert assertions.reconcile_complete(
        "u-op1", operation_kind="ENTITY_UNMERGE", namespace="ns-a") is False   # no completion receipt

    unmerges = [{"op_id": "u-op1", "merge_op_id": "op1",
                 "absorbed_uuid": "entA", "survivor_uuid": "entB"}]
    # the namespace is STILL discoverable — from the unmerge's own pending marker, not the rebinds.
    assert svc._resolve_unmerge_namespaces(
        unmerge_op_id="u-op1", merge_op_id="op1", allowed={"ns-a"}) == ["ns-a"]

    out = svc.repair_incomplete_reconciliations(
        committed_unmerges=unmerges, allowed_namespaces={"ns-a"})
    assert out["repaired"] == 1 and out["unmerge_op_ids"] == ["u-op1"]
    # rebuild succeeded WITHOUT restoring a second time (no double-move corruption)
    owner2 = {r["assertion_id"]: r["subject_uuid"] for r in assertions.rows}
    assert owner2 == {"aA": "entA", "aB": "entB"}
    assert views.value_for("entA", "owned") == 5 and views.value_for("entB", "sold") == 8
    assert assertions.reconcile_complete(
        "u-op1", operation_kind="ENTITY_UNMERGE", namespace="ns-a") is True
    # third run: receipt present -> no-op.
    out3 = svc.repair_incomplete_reconciliations(
        committed_unmerges=unmerges, allowed_namespaces={"ns-a"})
    assert out3["repaired"] == 0 and out3["unmerge_op_ids"] == []


@pytest.mark.unit
def test_unmerge_pending_marker_does_not_count_as_completion():
    # the marker must not satisfy the receipt check — otherwise writing intent would itself mask the
    # very failure it exists to make discoverable.
    rows = [{**_row("aA", "entA", "owned", 5, "2026-07-01T00:00:00+00:00"), "namespace": "ns-a"}]
    _svc_unused, assertions, _v = _svc(rows)
    assertions.record_reconcile_intent(
        operation_id="u-op1", operation_kind="ENTITY_UNMERGE", merge_op_id="op1", namespace="ns-a")
    assert assertions.reconcile_complete(
        "u-op1", operation_kind="ENTITY_UNMERGE", namespace="ns-a") is False


@pytest.mark.unit
def test_transitive_repair_is_idempotent_on_second_run():
    rows = [
        _row("aA", "entA", "owned", 1, "2026-06-01T00:00:00+00:00"),
        _row("aC", "entC", "sold", 3, "2026-08-01T00:00:00+00:00"),
    ]
    svc, assertions, _v = _svc(rows)
    assertions.receipts.add(("op2", "ENTITY_MERGE", None))
    merges = [
        {"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"},
        {"op_id": "op2", "absorbed_uuid": "entB", "survivor_uuid": "entC"},
    ]
    svc.repair_incomplete_reconciliations(committed_merges=merges)
    # both receipts now present -> a second pass repairs nothing and replays nothing.
    out2 = svc.repair_incomplete_reconciliations(committed_merges=merges)
    assert out2["repaired"] == 0 and out2["downstream_replayed_op_ids"] == []


@pytest.mark.unit
def test_orphan_pass_resolves_dead_subject_to_survivor_via_lineage():
    # A merge rebind never ran: aA still carries the DEAD subject entA (its Entity was DETACH DELETEd
    # by the merge). Only entC exists. The scheduled orphan pass resolves entA -> its survivor via the
    # A->B->C lineage and rebinds forward.
    rows = [
        _row("aA", "entA", "owned", 1, "2026-06-01T00:00:00+00:00"),   # orphaned on dead entA
        _row("aC", "entC", "sold", 3, "2026-08-01T00:00:00+00:00"),
    ]
    svc, assertions, views = _svc_with_entities(rows, existing={"entC"})
    assert [w["subject_uuid"] for w in assertions.orphaned_assertions()] == ["entA"]
    merges = [
        {"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"},
        {"op_id": "op2", "absorbed_uuid": "entB", "survivor_uuid": "entC"},
    ]
    out = svc.repair_orphaned_assertions(committed_merges=merges)
    assert out["orphans"] == 1 and out["repaired"] == ["entA"] and out["unresolved"] == []
    assert all(r["subject_uuid"] == "entC" for r in assertions.rows)   # rebound to the final survivor
    assert views.value_for("entC", "owned") == 1


@pytest.mark.unit
def test_orphan_pass_surfaces_unresolvable_orphan_without_guessing():
    # an orphan with NO merge lineage cannot be resolved to a survivor -> surfaced, never guessed.
    rows = [_row("aX", "entX", "owned", 1, "2026-06-01T00:00:00+00:00")]
    svc, assertions, _v = _svc_with_entities(rows, existing=set())   # entX is dead, no lineage
    out = svc.repair_orphaned_assertions(committed_merges=[])
    assert out["orphans"] == 1 and out["repaired"] == [] and out["unresolved"] == ["entX"]
    assert assertions.rows[0]["subject_uuid"] == "entX"             # left untouched for an operator


@pytest.mark.unit
def test_orphan_pass_noops_when_no_orphans():
    rows = [_row("aA", "entA", "owned", 1, "2026-06-01T00:00:00+00:00")]
    svc, _a, _v = _svc_with_entities(rows, existing={"entA"})       # entA still lives -> no orphans
    out = svc.repair_orphaned_assertions(committed_merges=[])
    assert out == {"orphans": 0, "repaired": [], "unresolved": [], "errored": [],
                   "replayed_op_ids": []}


@pytest.mark.unit
def test_orphan_pass_fail_closed_when_terminal_survivor_is_dead():
    # the chain A->B->C exists, but C was ALSO deleted (not in existing). The orphan must stay
    # unresolved, never reported repaired/projected against a dead terminal uuid.
    rows = [_row("aA", "entA", "owned", 1, "2026-06-01T00:00:00+00:00")]
    svc, assertions, views = _svc_with_entities(rows, existing=set())   # neither entA nor entC live
    merges = [
        {"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"},
        {"op_id": "op2", "absorbed_uuid": "entB", "survivor_uuid": "entC"},
    ]
    out = svc.repair_orphaned_assertions(committed_merges=merges)
    assert out["repaired"] == [] and out["unresolved"] == ["entA"]
    assert assertions.rows[0]["subject_uuid"] == "entA"            # untouched
    assert views.value_for("entC", "owned") is None               # no View against a dead terminal


@pytest.mark.unit
def test_orphan_pass_is_eventually_complete_past_unresolvable_first_page():
    # fairness: limit=2, orphans 1-2 have NO lineage, orphan 3 has A->B->C. Run 1 examines 1-2 (stamps
    # them). Run 2 reaches orphan 3 (unattempted-first) and repairs it.
    rows = [
        _row("a1", "dead1", "owned", 1, "2026-06-01T00:00:00+00:00"),
        _row("a2", "dead2", "owned", 2, "2026-06-02T00:00:00+00:00"),
        _row("a3", "entA", "sold", 3, "2026-06-03T00:00:00+00:00"),
    ]
    svc, assertions, _v = _svc_with_entities(rows, existing={"entC"})
    merges = [
        {"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"},
        {"op_id": "op2", "absorbed_uuid": "entB", "survivor_uuid": "entC"},
    ]
    out1 = svc.repair_orphaned_assertions(committed_merges=merges, limit=2)
    assert out1["repaired"] == []                                  # only dead1/dead2 seen, no lineage
    out2 = svc.repair_orphaned_assertions(committed_merges=merges, limit=2)
    assert out2["repaired"] == ["entA"]                            # orphan 3 reached on the next run
    assert any(r["subject_uuid"] == "entC" for r in assertions.rows)


@pytest.mark.unit
def test_orphan_pass_allowlist_scopes_and_stamps_examined():
    # allowlist restricts the work set; every examined orphan is stamped (fairness frontier advances).
    rows = [
        _row("aA", "entA", "owned", 1, "2026-06-01T00:00:00+00:00", claim_key=None),
    ]
    rows[0]["namespace"] = "tenant-a"
    svc, assertions, _v = _svc_with_entities(rows, existing={"entB"})
    merges = [{"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"}]
    out = svc.repair_orphaned_assertions(
        committed_merges=merges, allowed_namespaces={"tenant-a"}, limit=10)
    assert out["repaired"] == ["entA"]
    assert assertions._orphan_attempted.get(("entA", "tenant-a")) is not None  # stamped by (uuid, ns)
    # a namespace outside the allowlist yields no work
    out2 = svc.repair_orphaned_assertions(
        committed_merges=merges, allowed_namespaces={"tenant-z"}, limit=10)
    assert out2["orphans"] == 0


@pytest.mark.unit
def test_recon_op_namespace_ignores_survivor_native_assertions():
    # regression 1: a tenant-A merge whose SURVIVOR holds unrelated tenant-B-native assertions is a
    # tenant-A operation — the op's namespace comes from its AFFECTED rows (absorbed/rebind records),
    # never from survivor-native rows, so this must repair as tenant-a and NOT be a multi violation.
    rows = [
        _row("aA", "entA", "owned", 1, "2026-06-01T00:00:00+00:00"),      # absorbed, tenant-a
        _row("aBn", "entB", "sold", 9, "2026-06-02T00:00:00+00:00"),      # survivor-native, tenant-b
    ]
    rows[0]["namespace"] = "tenant-a"
    rows[1]["namespace"] = "tenant-b"
    svc, assertions, _v = _svc(rows)
    merges = [{"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"}]
    out = svc.repair_incomplete_reconciliations(committed_merges=merges)
    assert out["merge_op_ids"] == ["op1"] and out["errored_merge_op_ids"] == []
    assert assertions.reconcile_complete(
        "op1", operation_kind="ENTITY_MERGE", namespace="tenant-a")     # receipted for tenant-a only
    assert not assertions.reconcile_complete(
        "op1", operation_kind="ENTITY_MERGE", namespace="tenant-b")


@pytest.mark.unit
def test_recon_two_namespaces_one_failure_does_not_certify_the_other():
    # regression 2: ONE merge op spanning two silos. tenant-b's rebuild fails, tenant-a's succeeds.
    # tenant-a's success must NOT write a receipt that masks tenant-b's failure.
    rows = [
        _row("aA", "entA", "owned", 1, "2026-06-01T00:00:00+00:00"),
        _row("aB", "entA", "sold", 2, "2026-06-02T00:00:00+00:00"),
    ]
    rows[0]["namespace"] = "tenant-a"
    rows[1]["namespace"] = "tenant-b"
    svc, assertions, _v = _svc(rows)
    real_rebuild = svc.rebuild_scalar_state

    def _boom(subject_uuid, **kw):
        if kw.get("namespace") == "tenant-b":
            raise RuntimeError("tenant-b rebuild boom")
        return real_rebuild(subject_uuid, **kw)
    svc.rebuild_scalar_state = _boom

    merges = [{"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"}]
    out = svc.repair_incomplete_reconciliations(committed_merges=merges)
    assert "op1" in out["merge_op_ids"] and "op1" in out["errored_merge_op_ids"]
    assert assertions.reconcile_complete("op1", operation_kind="ENTITY_MERGE", namespace="tenant-a")
    assert not assertions.reconcile_complete(
        "op1", operation_kind="ENTITY_MERGE", namespace="tenant-b")   # failure NOT certified

    # regression 3: a later run repairs the failed namespace without duplicating anything.
    svc.rebuild_scalar_state = real_rebuild
    out2 = svc.repair_incomplete_reconciliations(committed_merges=merges)
    assert out2["merge_op_ids"] == ["op1"] and out2["errored_merge_op_ids"] == []
    assert assertions.reconcile_complete("op1", operation_kind="ENTITY_MERGE", namespace="tenant-b")
    assert len(assertions.rows) == 2                                  # no duplicated assertions
    assert len([rb for rb in assertions.rebinds if rb["op"] == "op1"]) == 2   # one rebind per row


@pytest.mark.unit
def test_recon_per_op_isolation_one_failure_does_not_block_others():
    # op1 always raises on rebuild; op2 is repairable. op1 is errored (no receipt); op2 still repaired.
    rows = [
        _row("a1", "entA", "owned", 1, "2026-06-01T00:00:00+00:00"),
        _row("a2", "entC", "owned", 3, "2026-06-02T00:00:00+00:00"),
    ]
    svc, assertions, _v = _svc(rows)

    real_rebuild = svc.rebuild_scalar_state

    def _boom(subject_uuid, **kw):
        if subject_uuid == "entB":                 # op1's survivor rebuild
            raise RuntimeError("rebuild boom")
        return real_rebuild(subject_uuid, **kw)
    svc.rebuild_scalar_state = _boom

    merges = [
        {"op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"},
        {"op_id": "op2", "absorbed_uuid": "entC", "survivor_uuid": "entD"},
    ]
    out = svc.repair_incomplete_reconciliations(committed_merges=merges)
    assert out["errored_merge_op_ids"] == ["op1"]
    assert out["merge_op_ids"] == ["op2"]
    assert not assertions.reconcile_complete("op1", operation_kind="ENTITY_MERGE")  # errored -> no receipt
    assert assertions.reconcile_complete("op2", operation_kind="ENTITY_MERGE")      # unrelated proceeded


@pytest.mark.unit
def test_unmerge_coordinator_fires_scalar_hook_on_commit():
    # REGRESSION 4: the REAL UnmergeCoordinator committed path invokes scalar reconciliation via its
    # post-COMMIT hook. Drive the replay-committed branch (observed == expected_after) so the commit
    # path runs without a live graph.
    from menhir.services.merge_coordinator import merge_state_fingerprint
    from menhir.services.unmerge_coordinator import UnmergeCoordinator

    state = {"survivor_present": True, "absorbed_present": True, "lineage_recorded": False}
    op_id = "unmerge-op"
    expected_after = merge_state_fingerprint(state, op_id=op_id)

    class _Adapter:
        def fetch_merge_state(self, survivor_uuid, absorbed_uuid):
            return dict(state)

    class _Journal:
        def _ensure_ready(self):
            pass

        def get(self, oid):
            return {"expected_after_sha256": expected_after}

        def mark_committed(self, oid):
            pass

        def mark_reversed(self, oid):
            pass

    seen = []

    def _hook(*, survivor_uuid, absorbed_uuid, merge_op_id, unmerge_op_id):
        seen.append({"survivor": survivor_uuid, "absorbed": absorbed_uuid,
                     "merge_op": merge_op_id, "unmerge_op": unmerge_op_id})

    coord = UnmergeCoordinator(graph_adapter=_Adapter(), journal=_Journal(),
                               on_unmerge_committed=_hook)
    request = {"op_id": op_id, "survivor_uuid": "entB", "absorbed_uuid": "entA",
               "merge_op_id": "merge-op-1"}
    out = coord._apply(request, {})
    assert out["restored"] == 1 and out.get("replayed") is True
    # the hook receives BOTH the forward merge id AND the unmerge's OWN op id (for its receipt).
    assert seen == [{"survivor": "entB", "absorbed": "entA",
                     "merge_op": "merge-op-1", "unmerge_op": op_id}]


@pytest.mark.unit
def test_unmerge_coordinator_hook_failure_does_not_break_commit():
    from menhir.services.merge_coordinator import merge_state_fingerprint
    from menhir.services.unmerge_coordinator import UnmergeCoordinator

    state = {"survivor_present": True, "absorbed_present": True, "lineage_recorded": False}
    op_id = "unmerge-op2"
    expected_after = merge_state_fingerprint(state, op_id=op_id)

    class _Adapter:
        def fetch_merge_state(self, s, a):
            return dict(state)

    class _Journal:
        def _ensure_ready(self): pass
        def get(self, oid): return {"expected_after_sha256": expected_after}
        def mark_committed(self, oid): pass
        def mark_reversed(self, oid): pass

    def _boom(*, survivor_uuid, absorbed_uuid, merge_op_id, unmerge_op_id):
        raise RuntimeError("restore blew up")

    coord = UnmergeCoordinator(graph_adapter=_Adapter(), journal=_Journal(),
                               on_unmerge_committed=_boom)
    out = coord._apply({"op_id": op_id, "survivor_uuid": "entB", "absorbed_uuid": "entA",
                        "merge_op_id": "m1"}, {})
    assert out["restored"] == 1   # unmerge still succeeds; hook error swallowed


@pytest.mark.unit
def test_unmerge_restores_and_writes_receipt():
    # REGRESSION 4-adjacent: the service unmerge restores assertions and rebuilds both sides. (The
    # real UnmergeCoordinator wiring is covered in test_unmerge_coordinator via its hook.)
    rows = [
        _row("aA", "entA", "owned", 5, "2026-07-01T00:00:00+00:00"),
        _row("aB", "entB", "owned", 8, "2026-08-01T00:00:00+00:00"),
    ]
    svc, assertions, views = _svc(rows)
    svc.handle_merge(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    out = svc.handle_unmerge(survivor_uuid="entB", absorbed_uuid="entA", merge_op_id="op1", unmerge_op_id="u-op1")
    assert out["restored"] == 1
    assert views.value_for("entA", "owned") == 5 and views.value_for("entB", "owned") == 8
    assert assertions.reconcile_complete("u-op1", operation_kind="ENTITY_UNMERGE") is True


@pytest.mark.unit
def test_concurrent_rebind_produces_one_lineage_record_per_op_assertion():
    # REGRESSION 3: the merge hook and a repair pass can both call rebind for the same op before a
    # receipt exists. The rebind_key (op::assertion_id) unique boundary makes it idempotent — exactly
    # one AssertionRebind per (op, assertion), and the second rebind moves nothing new.
    rows = [_row("aA", "entA", "owned", 5, "2026-07-01T00:00:00+00:00")]
    svc, assertions, _v = _svc(rows)
    first = assertions.rebind_assertions(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    second = assertions.rebind_assertions(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    assert first["rebound"] == 1 and second["rebound"] == 0
    recs = [rb for rb in assertions.rebinds if rb["op"] == "op1"]
    assert len(recs) == 1                                   # exactly one lineage record for (op, aid)


@pytest.mark.unit
def test_unmerge_hook_failure_is_repaired_by_unmerge_reconciliation():
    # REGRESSION 4 (the real gap): graph unmerge COMMITS, its forward merge is marked REVERSED, then
    # the scalar hook FAILS. A merge-only repair scan cannot see it (merge is reversed). The
    # unmerge-direction repair, keyed by the unmerge op's OWN id, restores and rebuilds both.
    rows = [
        _row("aA", "entA", "owned", 5, "2026-07-01T00:00:00+00:00"),
        _row("aB", "entB", "owned", 8, "2026-08-01T00:00:00+00:00"),
    ]
    svc, assertions, views = _svc(rows)
    svc.handle_merge(absorbed_uuid="entA", survivor_uuid="entB", merge_op_id="op1")
    assert views.value_for("entB", "owned") == 8
    # Simulate the committed-unmerge whose scalar hook failed: graph reversed, but scalar NOT
    # restored (assertions still on survivor, no unmerge receipt).
    assert assertions.reconcile_complete("u-op1", operation_kind="ENTITY_UNMERGE") is False

    out = svc.repair_incomplete_reconciliations(committed_unmerges=[
        {"op_id": "u-op1", "merge_op_id": "op1", "absorbed_uuid": "entA", "survivor_uuid": "entB"}])
    assert out["repaired"] == 1 and out["unmerge_op_ids"] == ["u-op1"]
    assert views.value_for("entA", "owned") == 5 and views.value_for("entB", "owned") == 8
    assert assertions.reconcile_complete("u-op1", operation_kind="ENTITY_UNMERGE") is True
