"""Durable :TypedAssertion store (ScalarStateView Piece C, commit 1) — domain model + repository.

No live Neo4j: a FakeNeo4j routes cypher to canned rows and records every call. Proves the event
log is durable and idempotent (invariant 1), with claim-versioned identity (distinct claims in one
episode stay distinct), atomic single-statement supersession via a per-claim head, monotonic tier
upgrade, kind/tier value validation, atomic provenance + binding_pending, and the corrected
include_superseded query. No View writes here (commit 2 adds the fold)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from menhir.domain.typed_assertion import (
    IDENTITY_VERSION,
    TypedAssertion,
    evidence_rank,
    normalize_scalar,
    perceiver_rank,
    validate_value,
)
from menhir.infrastructure.typed_assertion_repository import (
    ScalarStateActivationError,
    TypedAssertionRepository,
)


class FakeNeo4j:
    """Routes an executed cypher string to canned rows by substring match; records every call."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or []
        self.default = [] if default is None else default
        self.executed: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.executed.append((query, params or {}))
        for sub, rows in self.routes:
            if sub in query:
                return rows
        return self.default


@pytest.mark.unit
def test_pending_projection_repairs_are_bounded_and_scoped() -> None:
    fake = FakeNeo4j(default=[{
        "repair_key": "r1", "operation_id": "op1", "operation_kind": "MEMORY_DELETE",
        "subject_uuid": "ent-1", "namespace": "tenant-a", "started_at": "2026-07-22T00:00:00Z",
    }])

    rows = TypedAssertionRepository(fake).pending_projection_repairs(
        operation_id="op1", namespaces=["tenant-a", "tenant-a"], limit=7)

    assert rows[0]["repair_key"] == "r1"
    query, params = fake.executed[0]
    assert "rr.status = 'pending'" in query
    assert params == {"limit": 7, "operation_id": "op1", "namespaces": ["tenant-a"]}


@pytest.mark.unit
def test_projection_repair_completes_only_a_pending_receipt() -> None:
    fake = FakeNeo4j(default=[{"completed": 1}])

    assert TypedAssertionRepository(fake).mark_projection_repair_complete("r1") is True
    query, params = fake.executed[0]
    assert "rr.status = 'pending'" in query and "rr.status = 'complete'" in query
    assert params == {"repair_key": "r1"}


@pytest.mark.unit
def test_due_activation_claim_is_bounded_scoped_and_crash_repairable() -> None:
    fake = FakeNeo4j(default=[{
        "repair_key": "TIME_ACTIVATION\u001fa1", "operation_id": "a1",
        "operation_kind": "TIME_ACTIVATION", "subject_uuid": "ent-1",
        "namespace": "tenant-a", "started_at": "2026-07-22T00:00:00Z",
    }])

    rows = TypedAssertionRepository(fake).claim_due_scalar_activations(
        as_of="2026-07-22T12:00:00+00:00", namespaces=["tenant-a"], limit=9)

    assert rows[0]["operation_kind"] == "TIME_ACTIVATION"
    query, params = fake.executed[0]
    assert "a.valid_at <= datetime($as_of)" in query
    assert "a.activation_pending = false" in query
    assert "ScalarProjectionRepair" in query
    assert params["namespaces"] == ["tenant-a"] and params["limit"] == 9


def _assertion(**over):
    base = dict(
        subject_uuid="ent-coins", subject_display="my coins", attribute="owned", scope="pre-1920",
        value_kind="count", unit="", operation="absolute", value=37,
        stated_span="I have a total of 37 coins", span_start=10, span_end=36,
        episode_uuid="ep-1", valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", evidence_tier="user", perceiver_version="v1",
    )
    base.update(over)
    return TypedAssertion(**base)


# ----------------------------------------------------------------------------- domain model

@pytest.mark.unit
def test_normalize_scalar_shapes():
    assert normalize_scalar(38) == "38"
    assert normalize_scalar(38.0) == "38"
    assert normalize_scalar([10, 12]) == "10-12"
    assert normalize_scalar(True) == "true"
    assert normalize_scalar(" 07:30 ") == "07:30"


@pytest.mark.unit
def test_money_decimal_serialization_and_hydration_preserve_exact_scale():
    fake = FakeNeo4j(default=[{"assertion_id": "money-1", "created": True}])
    assertion = _assertion(value_kind="money", unit="usd", value=Decimal("1200.50"))

    TypedAssertionRepository(fake).record_assertion(assertion)

    params = fake.executed[0][1]
    assert params["value_norm"] == "1200.50"
    assert params["value_json"] == "1200.50"
    hydrated = TypedAssertionRepository._hydrate({
        "value_kind": "money", "value_json": "1200.50",
    })
    assert hydrated["value"] == Decimal("1200.50")
    assert isinstance(hydrated["value"], Decimal)


@pytest.mark.unit
def test_normalize_scalar_collapses_degenerate_range_only():
    """A degenerate range [N, N] IS the point N and must normalize identically to the scalar, so the
    same reading of the same span cannot split the k-sample vote. Genuine ranges stay ranges."""
    # degenerate -> collapses, and is INDISTINGUISHABLE from the bare scalar (the property that
    # actually makes the two k-samples agree).
    assert normalize_scalar([1300, 1300]) == "1300" == normalize_scalar(1300)
    assert normalize_scalar([38.0, 38]) == "38" == normalize_scalar(38)
    # endpoints are compared AFTER normalization, so a mixed-typed pair collapses too.
    assert normalize_scalar([1300, "1300"]) == "1300"
    # genuine ranges are untouched -- interval semantics survive where they carry information.
    assert normalize_scalar([10, 12]) == "10-12"
    assert normalize_scalar([-5, 5]) == "-5-5"
    # non-degenerate float noise is NOT collapsed (0.1+0.2 != 0.3).
    assert normalize_scalar([0.1 + 0.2, 0.3]) == "0.30000000000000004-0.3"
    # only 2-element sequences are range-shaped; anything else joins as before.
    assert normalize_scalar([7, 7, 7]) == "7-7-7"


@pytest.mark.unit
def test_fail_closed_validation():
    with pytest.raises(ValueError):
        _assertion(subject_uuid="  ")           # blank identity
    with pytest.raises(ValueError):
        _assertion(attribute="")                # empty attribute
    with pytest.raises(ValueError):
        _assertion(value_kind="bogus")          # unknown value_kind
    with pytest.raises(ValueError):
        _assertion(operation="multiply")        # bad operation
    with pytest.raises(ValueError):
        _assertion(stated_span="")              # missing grounding span
    with pytest.raises(ValueError):
        _assertion(episode_uuid="")             # missing provenance
    with pytest.raises(ValueError):
        _assertion(evidence_tier="root")        # non-allowlisted tier
    with pytest.raises(ValueError):
        _assertion(time_basis="guessed")        # non-allowlisted time_basis
    with pytest.raises(ValueError):
        _assertion(span_start=40, span_end=10)  # inverted span
    with pytest.raises(ValueError):
        _assertion(span_start=5, span_end=-1)   # half-known span (one offset absent)
    with pytest.raises(ValueError):
        _assertion(claim_ordinal=-1)            # negative ordinal


@pytest.mark.unit
def test_kind_specific_value_validation():
    validate_value("count", "absolute", 5)
    validate_value("duration", "absolute", [10, 12])
    validate_value("clock_time", "absolute", "07:30")
    validate_value("weekday", "absolute", "Saturday")
    validate_value("boolean", "absolute", True)
    validate_value("count", "delta", -5)             # a decrement
    with pytest.raises(ValueError):
        validate_value("clock_time", "absolute", "not-a-time")
    with pytest.raises(ValueError):
        validate_value("weekday", "absolute", "funday")
    with pytest.raises(ValueError):
        validate_value("boolean", "absolute", 1)     # int is not a bool
    with pytest.raises(ValueError):
        validate_value("clock_time", "delta", "07:30")  # non-numeric kind cannot delta


@pytest.mark.unit
def test_numeric_values_must_be_finite():
    # NaN / +-inf are not real quantities; they must fail closed at the durable validator (they would
    # otherwise reach normalize_scalar -> int() and crash the key builder).
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            validate_value("count", "absolute", bad)
        with pytest.raises(ValueError):
            validate_value("count", "delta", bad)
    with pytest.raises(ValueError):
        validate_value("measurement", "absolute", [1, float("inf")])  # a non-finite range endpoint
    validate_value("measurement", "absolute", [1, 2])                  # a finite range still passes


@pytest.mark.unit
def test_clock_time_must_be_a_real_time():
    validate_value("clock_time", "absolute", "23:59")     # boundary ok
    validate_value("clock_time", "absolute", "00:00")
    for bad in ("24:00", "12:60", "29:99"):
        with pytest.raises(ValueError):
            validate_value("clock_time", "absolute", bad)  # shape passes the regex but time is impossible


@pytest.mark.unit
def test_normalize_scalar_is_defensive_on_non_finite():
    # never crash the key builder even if a non-finite slips in (validate_value rejects these upstream).
    assert normalize_scalar(float("nan")) == "nan"
    assert normalize_scalar([1, float("inf")]) == "1-inf"


@pytest.mark.unit
def test_oversized_integers_fail_closed_with_valueerror():
    # an int too large to represent as a float (10**1000) must raise ValueError, NOT the OverflowError
    # that math.isfinite(float(x)) throws — the boundary only catches ValueError, so an unhandled
    # OverflowError would abort the whole extraction pass instead of dropping one row.
    huge = 10 ** 1000
    with pytest.raises(ValueError):
        validate_value("count", "absolute", huge)
    with pytest.raises(ValueError):
        validate_value("count", "delta", huge)
    with pytest.raises(ValueError):
        validate_value("measurement", "absolute", [1, huge])   # oversized range endpoint
    # and normalizing a huge int must not crash (stringified directly, never routed through float()).
    assert normalize_scalar(huge) == str(huge)
    assert normalize_scalar([1, huge]) == f"1-{huge}"


@pytest.mark.unit
def test_distinct_claims_in_one_episode_do_not_collapse():
    # "I used to wake at 7:30, but now I wake at 8:30" — two absolutes, same slot, same episode.
    old = _assertion(attribute="wake", scope="", value_kind="clock_time", value="07:30",
                     stated_span="used to wake at 7:30", span_start=0, span_end=19)
    new = _assertion(attribute="wake", scope="", value_kind="clock_time", value="08:30",
                     stated_span="now I wake at 8:30", span_start=25, span_end=43)
    assert old.claim_key != new.claim_key       # different source spans -> different claims
    assert old.assertion_key != new.assertion_key


@pytest.mark.unit
def test_ordinal_disambiguates_when_offsets_absent():
    a0 = _assertion(span_start=-1, span_end=-1, claim_ordinal=0)
    a1 = _assertion(span_start=-1, span_end=-1, claim_ordinal=1)
    assert a0.claim_key != a1.claim_key


@pytest.mark.unit
def test_source_key_is_binding_stable_across_subject_uuid():
    # source_key omits subject_uuid, so the SAME episode/span resolves identically whether the
    # assertion is bound to the absorbed or the survivor entity (blocker 5: no fork after merge).
    on_a = _assertion(subject_uuid="entA", span_start=3, span_end=20)
    on_b = _assertion(subject_uuid="entB", span_start=3, span_end=20)
    assert on_a.source_key == on_b.source_key         # stable under rebinding
    assert on_a.claim_key != on_b.claim_key           # claim_key still includes subject
    # a different span is a different source claim
    assert _assertion(span_start=30, span_end=40).source_key != on_a.source_key


@pytest.mark.unit
def test_claim_key_is_source_only_so_semantic_correction_supersedes():
    # A newer perceiver correcting a MISCLASSIFICATION (owned -> sold) on the SAME source span must
    # share claim_key (so it supersedes the wrong prior interpretation via the head), yet differ in
    # assertion_key (the interpretation changed). Blocker-2 regression guard.
    wrong = _assertion(attribute="owned", value=5, perceiver_version="v1", span_start=3, span_end=20)
    fixed = _assertion(attribute="sold", value=5, perceiver_version="v2", span_start=3, span_end=20)
    assert wrong.claim_key == fixed.claim_key          # source-grounded: same span -> same claim
    assert wrong.assertion_key != fixed.assertion_key  # attribute is part of the interpretation
    # value_kind / scope / unit corrections likewise supersede rather than fork a new claim.
    assert _assertion(value_kind="count", span_start=3, span_end=20).claim_key == \
           _assertion(value_kind="money", unit="usd", value=5, span_start=3, span_end=20).claim_key


@pytest.mark.unit
def test_assertion_key_versions_and_values():
    base = _assertion(value=37, perceiver_version="v1")
    assert base.assertion_key == _assertion(value=37, perceiver_version="v1").assertion_key  # idempotent
    assert base.assertion_key != _assertion(value=38, perceiver_version="v1").assertion_key  # value
    assert base.assertion_key != _assertion(value=37, perceiver_version="v2").assertion_key  # version
    # a newer version of the SAME claim shares claim_key (so it can supersede via the head)
    assert base.claim_key == _assertion(value=38, perceiver_version="v2").claim_key


@pytest.mark.unit
def test_perceiver_rank_fixed_width_semver_cross_major():
    # Blocker-1 regression guard: a variable-length accumulator ranked v1.3 (10003) above v2 (2).
    assert perceiver_rank("v2") > perceiver_rank("v1.3")
    assert perceiver_rank("v1.3") > perceiver_rank("v1.2")
    assert perceiver_rank("v2") > perceiver_rank("v1.99")
    assert perceiver_rank("v10") > perceiver_rank("v9.9.9")
    assert perceiver_rank("v1") == perceiver_rank("v1.0.0")   # padding
    assert perceiver_rank("garbage") == 0
    assert perceiver_rank("v1.2.3.4") == 0                     # too many components -> reject
    assert perceiver_rank("v1000000") == 0                     # out-of-range component -> reject


@pytest.mark.unit
def test_rank_helpers():
    assert evidence_rank("user") > evidence_rank("manual") > evidence_rank("agent")
    assert evidence_rank("unknown") == -1


# ----------------------------------------------------------------------------- repository write

def _record_fake():
    return FakeNeo4j(default=[{"assertion_id": "aid-1", "created": True,
                               "superseded_prior": False, "binding_pending": False}])


@pytest.mark.unit
def test_record_write_is_a_single_atomic_statement_head_locked_on_source_key():
    # ONE execute(): head lock + assertion + supersede + provenance. The head is MERGEd on the
    # BINDING-STABLE source_key (its DB-unique identity), so there is no read-before-write preflight
    # to race, and two concurrent first writes for one source claim converge on one head.
    fake = _record_fake()
    out = TypedAssertionRepository(fake).record_assertion(_assertion())
    assert len(fake.executed) == 1                          # single atomic statement, no preflight
    q, _ = fake.executed[0]
    assert "MERGE (h:TypedAssertionHead {source_key: $source_key})" in q  # atomic lock on source_key
    assert "MERGE (h)-[:CURRENT]->(a)" in q                 # current pointer moved in-statement
    assert "MERGE (a)-[:SUPERSEDES]->(c)" in q              # supersede in the same statement
    assert "GROUNDS" in q and "HAS_ASSERTION" in q          # provenance atomic with the write
    assert out["assertion_id"] == "aid-1" and out["created"] is True


@pytest.mark.unit
def test_record_cypher_adopts_resolved_binding_for_a_pending_row():
    # ScalarStateView C.4.3 pending->bound: a claim first written as an unbindable advisory (sentinel
    # subject_uuid, binding_pending=true) can later become uniquely bindable and be re-perceived.
    # assertion_key omits subject_uuid, so the re-write lands on the SAME node via ON MATCH; the write
    # MUST then adopt the resolved subject_uuid/subject_display/claim_key onto BOTH the assertion and
    # its head, or the subject_uuid-keyed fold would never retrieve it. (Live MERGE semantics are the
    # C.4.4 Neo4j gate; this asserts the adoption clause is present and correctly gated.)
    fake = _record_fake()
    TypedAssertionRepository(fake).record_assertion(_assertion())
    q, params = fake.executed[0]
    assert "fully_bound" in q and "was_pending" in q                 # gated on real entity + prior pending
    assert "SET a.subject_uuid = $subject_uuid" in q                 # adopt onto the assertion
    assert "h.subject_uuid = $subject_uuid" in q and "h.claim_key = $claim_key" in q  # and the head
    assert params["subject_display"] == _assertion().subject_display  # display is available to adopt


@pytest.mark.unit
def test_record_cypher_rejects_already_bound_identity_mismatch():
    # ScalarStateView C.4.4: the durable invariant belongs to the SOURCE CLAIM (head), across every
    # assertion VERSION and interpretation. Mismatch is decided from the head's EXISTING current owner
    # BEFORE any node create / CURRENT move / supersession — NOT from the freshly-created incoming node
    # (a new higher-version node has no binding_pending yet, so a late check would wrongly pass). It is
    # NOT gated on the presented entity existing, so an unresolved `unbound:` sentinel also counts as a
    # different identity and cannot de-authorize A. (Live MERGE is the C.4.4 Neo4j gate.)
    fake = _record_fake()
    out = TypedAssertionRepository(fake).record_assertion(_assertion())
    q, _ = fake.executed[0]
    normalized = " ".join(q.split())
    # computed from the pre-existing current `cur`, gated on cur being a bound owner that differs.
    assert "cur IS NOT NULL AND NOT coalesce(cur.binding_pending, true)" in normalized
    assert "cur.subject_uuid <> $subject_uuid) AS binding_mismatch" in normalized
    # the mismatch decision precedes the assertion MERGE (so a new node cannot escape the guard).
    assert normalized.index("AS binding_mismatch") < normalized.index("MERGE (a:TypedAssertion")
    # EVERY mutation is gated on NOT binding_mismatch: CURRENT move, supersession (via will_supersede),
    # adoption, binding_pending recompute, tier upgrade, and the provenance edges.
    assert "(NOT binding_mismatch) AND (cur IS NULL OR will_supersede" in normalized  # CURRENT move
    assert "(NOT binding_mismatch) AND cur IS NOT NULL AND cur.assertion_key <> $assertion_key" in normalized
    assert "(NOT binding_mismatch) AND fully_bound AND was_pending" in normalized     # adoption
    assert "(NOT binding_mismatch) AND $evidence_rank > coalesce(a.evidence_rank, -1)" in normalized  # tier
    assert "WHEN binding_mismatch THEN [] ELSE [1] END | SET a.binding_pending" in normalized
    assert "(NOT binding_mismatch) AND n IS NOT NULL THEN [1] ELSE [] END | MERGE (n)-[:HAS_ASSERTION]->(a)" in normalized
    assert "binding_mismatch" in out                                 # surfaced in the return dict


@pytest.mark.unit
def test_record_reports_binding_mismatch_from_return():
    fake = FakeNeo4j(default=[{"assertion_id": "aid-3", "created": False, "superseded_prior": False,
                               "binding_pending": False, "binding_mismatch": True}])
    out = TypedAssertionRepository(fake).record_assertion(_assertion())
    assert out["binding_mismatch"] is True and out["binding_pending"] is False


@pytest.mark.unit
def test_record_maps_keys_and_ranks_into_params():
    fake = _record_fake()
    a = _assertion(perceiver_version="v3", evidence_tier="user")
    TypedAssertionRepository(fake).record_assertion(a)
    _, params = fake.executed[0]
    assert params["claim_key"] == a.claim_key and params["assertion_key"] == a.assertion_key
    assert params["source_key"] == a.source_key
    assert params["perceiver_rank"] == perceiver_rank("v3")
    assert params["evidence_rank"] == evidence_rank("user")
    assert params["span_start"] == 10 and params["span_end"] == 36


@pytest.mark.unit
def test_record_reports_supersede_and_binding_pending_from_return():
    fake = FakeNeo4j(default=[{"assertion_id": "aid-2", "created": True,
                               "superseded_prior": True, "binding_pending": True}])
    out = TypedAssertionRepository(fake).record_assertion(_assertion())
    assert out["superseded_prior"] is True     # the query decided a prior current was out-ranked
    assert out["binding_pending"] is True       # entity/episode not yet present -> flagged, not lost


@pytest.mark.unit
def test_assertion_key_omits_subject_uuid_so_re_record_after_rebind_is_idempotent():
    # Blocker 2: assertion_key is built on source_key, NOT claim_key, so the SAME source span + same
    # interpretation + same version yields the SAME assertion_key regardless of current binding —
    # a user re-confirmation lands on the current node (upgrades tier) instead of forking a sibling.
    on_a = _assertion(subject_uuid="entA", span_start=3, span_end=20, value=5, perceiver_version="v1")
    on_b = _assertion(subject_uuid="entB", span_start=3, span_end=20, value=5, perceiver_version="v1")
    assert on_a.assertion_key == on_b.assertion_key   # binding-independent exact identity


@pytest.mark.unit
def test_supersession_is_strict_rank_and_same_version_does_not_replace():
    # Contract (documented): a claim becomes current only if there is no prior current, OR it
    # strictly out-ranks it, OR it IS the current (idempotent). A same-version different-value write
    # (equal rank) does NOT satisfy will_supersede, so it stays superseded=true (non-current audit).
    fake = _record_fake()
    TypedAssertionRepository(fake).record_assertion(_assertion())
    q = fake.executed[0][0]
    # supersede only on a STRICT rank win
    assert "$perceiver_rank > coalesce(cur.perceiver_rank, -1)" in q
    # become-current requires: no cur, OR will_supersede, OR it is already the current assertion_key
    assert "cur IS NULL OR will_supersede" in q
    assert "cur.assertion_key = $assertion_key" in q


@pytest.mark.unit
def test_monotonic_tier_upgrade_is_in_the_write():
    # An idempotent re-write (same assertion_key) must be able to UPGRADE the tier, never downgrade.
    fake = _record_fake()
    TypedAssertionRepository(fake).record_assertion(_assertion())
    q = fake.executed[0][0]
    assert "evidence_rank > coalesce(a.evidence_rank, -1)" in q  # guarded upgrade
    assert "ON MATCH SET" in q


# ----------------------------------------------------------------------------- repository read

@pytest.mark.unit
def test_assertions_for_entity_hydrates_value_json():
    fake = FakeNeo4j(routes=[("MATCH (a:TypedAssertion {subject_uuid:",
                              [{"assertion_id": "a1", "value": "10-12", "value_json": "[10, 12]",
                                "attribute": "spent", "value_kind": "duration"}])])
    rows = TypedAssertionRepository(fake).assertions_for_entity("ent-x")
    assert rows[0]["value"] == [10, 12]  # decoded back to the typed range, not the norm string


@pytest.mark.unit
def test_include_superseded_actually_changes_the_filter():
    # Default keeps only current (superseded=false); include_superseded=True drops the WHERE.
    fake = FakeNeo4j(default=[])
    TypedAssertionRepository(fake).assertions_for_entity("ent-x")
    assert "NOT coalesce(a.superseded, false)" in fake.executed[-1][0]
    fake2 = FakeNeo4j(default=[])
    TypedAssertionRepository(fake2).assertions_for_entity("ent-x", include_superseded=True)
    q2 = fake2.executed[-1][0]
    assert "NOT coalesce(a.superseded, false)" not in q2
    assert "WHERE" not in q2  # the filter is genuinely gone, not just missing one predicate


@pytest.mark.unit
def test_materializable_only_excludes_binding_pending():
    fake = FakeNeo4j(default=[])
    TypedAssertionRepository(fake).assertions_for_entity("ent-x")
    q_default = fake.executed[-1][0]
    assert "NOT coalesce(a.binding_pending, false)" not in q_default  # default keeps pending rows
    fake2 = FakeNeo4j(default=[])
    TypedAssertionRepository(fake2).materializable_assertions_for_entity("ent-x")
    q_mat = fake2.executed[-1][0]
    assert "NOT coalesce(a.binding_pending, false)" in q_mat        # materializable drops them
    assert "NOT coalesce(a.superseded, false)" in q_mat            # and still only current


@pytest.mark.unit
def test_materializable_default_namespace_matches_legacy_aliases_and_hydrates_canonical_name():
    fake = FakeNeo4j(default=[{
        "assertion_id": "a1",
        "namespace": "",
        "value": "10",
        "value_json": "10",
    }])

    rows = TypedAssertionRepository(fake).materializable_assertions_for_entity(
        "ent-x", namespace="default"
    )

    query, params = fake.executed[-1]
    assert "tenant_namespaces" in query
    assert params["tenant_namespaces"] == ["default", ""]
    assert "a.namespace AS namespace" in query
    assert rows[0]["namespace"] == "default"


@pytest.mark.unit
def test_materializable_reads_separate_turn_and_admitted_episode_provenance():
    fake = FakeNeo4j(default=[])
    TypedAssertionRepository(fake).materializable_assertions_for_entity("ent-x")
    query = fake.executed[-1][0]
    assert "TurnEvidence {turn_id: a.episode_uuid}" in query
    assert "Episodic)-[:ADMITTED_ON]->(te)" in query
    assert "AS turn_id" in query
    assert "AS source_episode_uuid" in query
    assert "AS grounding_id" in query


@pytest.mark.unit
def test_rebind_journals_per_op_uniquely_and_scopes_head_moves_by_source_key():
    fake = FakeNeo4j(default=[{"rebound": 2, "source_keys": ["sk1", "sk2"]}])
    out = TypedAssertionRepository(fake).rebind_assertions(
        absorbed_uuid="ent-absorbed", survivor_uuid="ent-survivor", merge_op_id="op-1")
    assert out == {"rebound": 2, "source_keys": ["sk1", "sk2"]}
    q, p = fake.executed[-1]
    assert "MERGE (r:AssertionRebind {rebind_key: $op + '::' + a.assertion_id})" in q  # DB-unique key
    assert "r.from_uuid = $absorbed, r.to_uuid = $survivor" in q     # lineage stack entry
    assert "SET a.subject_uuid = $survivor" in q
    assert "MATCH (h:TypedAssertionHead {source_key: sk})" in q      # head moves scoped by source_key
    assert p["op"] == "op-1"


@pytest.mark.unit
def test_rebind_requires_op_and_noops_on_self():
    fake = FakeNeo4j(default=[{"rebound": 9, "source_keys": []}])
    assert TypedAssertionRepository(fake).rebind_assertions(
        absorbed_uuid="x", survivor_uuid="x", merge_op_id="op") == {"rebound": 0, "source_keys": []}
    assert fake.executed == []
    with pytest.raises(ValueError):  # missing op_id -> lineage would be lost
        TypedAssertionRepository(fake).rebind_assertions(
            absorbed_uuid="a", survivor_uuid="b", merge_op_id="  ")


@pytest.mark.unit
def test_restore_is_op_scoped_and_moves_to_recorded_from_uuid():
    fake = FakeNeo4j(default=[{"restored": 1, "source_keys": ["sk1"], "from_uuids": ["ent-b"]}])
    out = TypedAssertionRepository(fake).restore_rebound_assertions(merge_op_id="op-2")
    assert out["restored"] == 1 and out["from_uuids"] == ["ent-b"]
    q, p = fake.executed[-1]
    assert "MATCH (r:AssertionRebind {merge_op_id: $op})" in q       # scoped to THIS op only
    assert "SET a.subject_uuid = from_uuid" in q                     # back to the recorded owner
    assert "MATCH (h:TypedAssertionHead {source_key: sk})" in q      # head moves scoped by source_key
    assert "FOREACH (r IN records | DELETE r)" in q                  # op's lineage records consumed
    assert p["op"] == "op-2"


@pytest.mark.unit
def test_reconcile_receipt_is_keyed_by_operation_kind_and_namespace():
    # C.4.4: one lifecycle op may span two assertion silos and is repaired INDEPENDENTLY per silo, so
    # receipt identity is (operation_id, kind, namespace) — a global key would let tenant-A's success
    # certify tenant-B's failure.
    fake = FakeNeo4j(default=[{"ok": 1}])
    repo = TypedAssertionRepository(fake)
    repo.record_reconcile_receipt(
        operation_id="op-1", operation_kind="ENTITY_MERGE", namespace="tenant-a")
    q, p = fake.executed[-1]
    assert "MERGE (rc:ScalarReconcile {receipt_key: $receipt_key})" in q
    assert "rc.status = 'complete'" in q and "rc.namespace = $namespace" in q
    assert p["namespace"] == "tenant-a"
    # the null silo gets a sentinel so it cannot collide with a real namespace named 'null'
    assert repo._receipt_key("op-1", "ENTITY_MERGE", None) != repo._receipt_key(
        "op-1", "ENTITY_MERGE", "null")
    # the completeness check is namespace-scoped by default; any_namespace is the explicit opt-out
    repo.reconcile_complete("op-1", operation_kind="ENTITY_MERGE", namespace="tenant-a")
    q2, p2 = fake.executed[-1]
    assert "rc.namespace = $namespace" in q2 and p2["namespace"] == "tenant-a"
    repo.reconcile_complete("op-1", operation_kind="ENTITY_MERGE", any_namespace=True)
    q3, _ = fake.executed[-1]
    assert "rc.namespace = $namespace" not in q3
    with pytest.raises(ValueError):
        repo.record_reconcile_receipt(operation_id="op", operation_kind="bogus")


@pytest.mark.unit
def test_reconcile_intent_is_pending_on_the_receipt_key_and_never_downgrades():
    # C.4.4.3 unmerge crash window: the pending marker shares receipt_key with the completion write,
    # so completing PROMOTES this node rather than creating a second one. ON MATCH must be absent —
    # a replay must never downgrade an already-complete receipt back to pending.
    fake = FakeNeo4j(default=[{"ok": 1}])
    repo = TypedAssertionRepository(fake)
    repo.record_reconcile_intent(
        operation_id="u-1", operation_kind="ENTITY_UNMERGE", merge_op_id="op-1", namespace="tenant-a")
    q, p = fake.executed[-1]
    assert "MERGE (rc:ScalarReconcile {receipt_key: $receipt_key})" in q
    assert "rc.status = 'pending'" in q
    assert "ON MATCH" not in q                       # never downgrade a complete receipt
    assert p["receipt_key"] == repo._receipt_key("u-1", "ENTITY_UNMERGE", "tenant-a")
    assert p["namespace"] == "tenant-a" and p["merge_op"] == "op-1"
    # a pending marker is NOT a completion: the repair pass must still pick the op up.
    repo.reconcile_complete("u-1", operation_kind="ENTITY_UNMERGE", namespace="tenant-a")
    q2, _ = fake.executed[-1]
    assert "rc.status = 'complete'" in q2
    with pytest.raises(ValueError):
        repo.record_reconcile_intent(operation_id="u-1", operation_kind="bogus")


@pytest.mark.unit
def test_namespaces_for_unmerge_unions_rebinds_with_its_own_reconcile_markers():
    # restore DELETES the forward op's rebind records, so after a restore-ok/rebuild-failed crash the
    # rebind source is empty. The unmerge's own marker must still surface the namespace.
    fake = FakeNeo4j(routes=[("AssertionRebind", []),                    # restore consumed them
                             ("ScalarReconcile", [{"namespace": "tenant-a"}])],  # marker survives
                     default=[])
    out = TypedAssertionRepository(fake).namespaces_for_unmerge(
        unmerge_op_id="u-1", merge_op_id="op-1")
    assert out == ["tenant-a"]
    q_rebind, p_rebind = fake.executed[0]
    q_marker, p_marker = fake.executed[1]
    assert "MATCH (r:AssertionRebind {merge_op_id: $op})" in q_rebind and p_rebind["op"] == "op-1"
    # the marker lookup is keyed by the UNMERGE's own op id + kind, and accepts pending OR complete
    assert "ScalarReconcile {operation_id: $op, operation_kind: 'ENTITY_UNMERGE'}" in q_marker
    assert "status" not in q_marker and p_marker["op"] == "u-1"


@pytest.mark.unit
def test_orphaned_subject_uuids_excludes_pending_and_needs_no_entity():
    fake = FakeNeo4j(default=[{"uuid": "dead-1"}])
    out = TypedAssertionRepository(fake).orphaned_subject_uuids(limit=50)
    assert out == ["dead-1"]
    q, _ = fake.executed[-1]
    assert "NOT EXISTS { MATCH (e:Entity {uuid: uuid}) }" in q          # no surviving Entity
    assert "NOT coalesce(a.superseded, false)" in q                     # current only
    assert "NOT coalesce(a.binding_pending, false)" in q               # pending advisory != orphan


@pytest.mark.unit
def test_pending_advisory_assertions_returns_current_pending_with_reconstruct_fields():
    fake = FakeNeo4j(default=[{
        "assertion_id": "a1", "source_key": "sk1", "subject_uuid": "unbound:sk1",
        "subject_display": "user", "binding_pending": True, "projection_pending": False,
        "attribute": "wake", "scope": "", "value_kind": "clock_time", "unit": "",
        "operation": "absolute", "value_json": "\"07:30\"", "stated_span": "wake at 07:30",
        "episode_uuid": "ep-1", "span_start": 0, "span_end": 13, "claim_ordinal": 0,
        "valid_at": "2026-07-01T00:00:00Z", "learned_at": "2026-07-01T00:00:00Z",
        "time_basis": "explicit", "perceiver_version": "v1", "namespace": "ns"}])
    out = TypedAssertionRepository(fake).pending_advisory_assertions(namespaces=["ns"], limit=50)
    assert len(out) == 1 and out[0]["value"] == "07:30"               # value_json hydrated
    q, p = fake.executed[-1]
    # selects rows needing binding OR projection repair, current only, restricted to the allowlist
    assert "coalesce(a.binding_pending, false) OR coalesce(a.projection_pending, false)" in q
    assert "NOT coalesce(a.superseded, false)" in q                   # current only, never revived
    assert "a.namespace IN $namespaces" in q and p["namespaces"] == ["ns"]
    # returns binding_pending + subject_uuid so repair can distinguish advisory from projection-only
    assert "coalesce(a.binding_pending, false) AS binding_pending" in q
    assert "a.subject_uuid AS subject_uuid" in q
    # carries the fields needed to reconstruct + re-record on the SAME node (assertion_key inputs)
    for field in ("subject_display", "attribute", "scope", "value_kind", "unit", "operation",
                  "value_json", "stated_span", "episode_uuid", "span_start", "span_end",
                  "claim_ordinal", "perceiver_version"):
        assert f"a.{field} AS {field}" in q or f"a.{field}" in q


@pytest.mark.unit
def test_mark_projection_complete_clears_marker():
    fake = FakeNeo4j(default=[{"cleared": 1}])
    n = TypedAssertionRepository(fake).mark_projection_complete(["a1"])
    assert n == 1
    q, p = fake.executed[-1]
    assert "a.assertion_id IN $ids" in q
    assert "SET a.projection_pending = false" in q
    assert p["ids"] == ["a1"]


@pytest.mark.unit
def test_mark_projection_complete_noops_on_empty():
    fake = FakeNeo4j(default=[])
    assert TypedAssertionRepository(fake).mark_projection_complete([]) == 0
    assert fake.executed == []


@pytest.mark.unit
def test_pending_advisory_ordering_is_fairness_not_oldest_first():
    # eventual completeness: unattempted rows first, then least-recently-attempted, so a bounded scan
    # advances the frontier instead of rescanning the same starved first page forever.
    fake = FakeNeo4j(default=[])
    TypedAssertionRepository(fake).pending_advisory_assertions(limit=2)
    q, _ = fake.executed[-1]
    normalized = " ".join(q.split())
    assert "CASE WHEN a.binding_repair_attempted_at IS NULL THEN 0 ELSE 1 END" in normalized
    assert "a.binding_repair_attempted_at ASC" in normalized
    assert "a.learned_at ASC, a.assertion_id ASC" in normalized     # stable tiebreak, NOT the primary


@pytest.mark.unit
def test_mark_binding_repair_attempted_stamps_frontier():
    fake = FakeNeo4j(default=[{"stamped": 2}])
    n = TypedAssertionRepository(fake).mark_binding_repair_attempted(
        ["a1", "a2"], at="2026-07-10T00:00:00Z")
    assert n == 2
    q, p = fake.executed[-1]
    assert "a.assertion_id IN $ids" in q
    assert "SET a.binding_repair_attempted_at = datetime($at)" in q
    assert p["ids"] == ["a1", "a2"] and p["at"] == "2026-07-10T00:00:00Z"


@pytest.mark.unit
def test_mark_binding_repair_attempted_noops_on_empty():
    fake = FakeNeo4j(default=[{"stamped": 0}])
    assert TypedAssertionRepository(fake).mark_binding_repair_attempted([], at="t") == 0
    assert fake.executed == []                                       # no query on an empty list


@pytest.mark.unit
def test_head_lookup_by_source_key_is_binding_stable():
    fake = FakeNeo4j(default=[{"claim_key": "ck1", "subject_uuid": "ent-survivor"}])
    out = TypedAssertionRepository(fake).head_uuids_for_source_key("src-1")
    assert out == [{"claim_key": "ck1", "subject_uuid": "ent-survivor"}]
    q, p = fake.executed[-1]
    assert "MATCH (h:TypedAssertionHead {source_key: $sk})" in q       # by source_key, not subject
    assert p["sk"] == "src-1"


@pytest.mark.unit
def test_current_for_claim_reads_through_the_head():
    fake = FakeNeo4j(routes=[("(h:TypedAssertionHead {claim_key: $ck})-[:CURRENT]->",
                              [{"assertion_id": "cur", "assertion_key": "k",
                                "value": "38", "value_json": "38", "evidence_tier": "user",
                                "perceiver_version": "v2"}])])
    cur = TypedAssertionRepository(fake).current_for_claim("ck-1")
    assert cur["assertion_id"] == "cur" and cur["value"] == 38


# --------------------------------------------------------------- fresh-only activation gate (C.3)

@pytest.mark.unit
def test_write_stamps_identity_version_on_create_only():
    # Every v2 head + assertion is stamped identity_version = IDENTITY_VERSION at write time, and
    # ONLY on ON CREATE — so the stamp always reflects the contract the node was created under and a
    # later idempotent re-write can never silently "upgrade" a legacy node's version label.
    fake = _record_fake()
    TypedAssertionRepository(fake).record_assertion(_assertion())
    q, params = fake.executed[0]
    assert params["identity_version"] == IDENTITY_VERSION
    assert "h.identity_version = $identity_version" in q
    assert "a.identity_version = $identity_version" in q
    # the stamp lives under ON CREATE, not ON MATCH (no version rewrite on a re-record)
    head_create, _sep, after_head = q.partition("MERGE (a:TypedAssertion")
    assert "h.identity_version = $identity_version" in head_create   # head stamp before assertion MERGE
    on_match = q.partition("ON MATCH SET")[2]
    assert "identity_version" not in on_match                        # never restamped on match


@pytest.mark.unit
def test_incompatible_identity_detection_uses_exact_match():
    fake = FakeNeo4j(default=[{"incompatible_heads": 2, "incompatible_assertions": 5}])
    out = TypedAssertionRepository(fake).incompatible_identity_nodes_exist()
    assert out == {"incompatible_heads": 2, "incompatible_assertions": 5}
    q, p = fake.executed[-1]
    # EXACT-MATCH gate: incompatible == unstamped OR != current (older OR newer), not merely "below".
    assert "h.identity_version IS NULL OR h.identity_version <> $v" in q
    assert "a.identity_version IS NULL OR a.identity_version <> $v" in q
    assert "< $v" not in q  # must NOT use a below-current comparison (would accept a newer contract)
    assert p["v"] == IDENTITY_VERSION


@pytest.mark.unit
def test_activation_refused_over_a_legacy_head_and_assertion():
    # DECISIVE regression (older/unstamped): a legacy store holds a head+assertion from the v1
    # (claim_key-anchored) contract — neither carries source_key nor a v2 assertion_key, so
    # identity_version is absent. Fresh-only activation MUST refuse rather than silently create the
    # source_key constraint over mixed identity spaces, and MUST NOT execute any DDL when it refuses.
    fake = FakeNeo4j(routes=[("identity_version IS NULL",
                              [{"incompatible_heads": 1, "incompatible_assertions": 1}])])
    repo = TypedAssertionRepository(fake)
    with pytest.raises(ScalarStateActivationError) as exc:
        repo.assert_scalar_state_activatable()
    assert exc.value.incompatible_heads == 1 and exc.value.incompatible_assertions == 1
    # activate_scalar_state gates first and bails before any CREATE CONSTRAINT runs.
    fake2 = FakeNeo4j(routes=[("identity_version IS NULL",
                               [{"incompatible_heads": 1, "incompatible_assertions": 0}])])
    repo2 = TypedAssertionRepository(fake2)
    with pytest.raises(ScalarStateActivationError):
        repo2.activate_scalar_state()
    assert not any("CREATE CONSTRAINT" in q for q, _ in fake2.executed)  # no DDL over legacy state


@pytest.mark.unit
def test_activation_refused_over_newer_v3_stamped_nodes():
    # DECISIVE regression (newer): a v2 binary rolled back onto a store whose nodes were stamped by a
    # future v3 identity algorithm. The exact-match gate must fail closed just as it does for legacy
    # nodes — a "< current" gate would wrongly accept these — and NO DROP / CREATE INDEX / CREATE
    # CONSTRAINT may execute. (The count query is version-agnostic: it counts anything != current, so
    # v3 nodes surface here exactly like unstamped ones.)
    fake = FakeNeo4j(routes=[("identity_version IS NULL",
                              [{"incompatible_heads": 3, "incompatible_assertions": 7}])])
    repo = TypedAssertionRepository(fake)
    with pytest.raises(ScalarStateActivationError) as exc:
        repo.activate_scalar_state()
    assert exc.value.incompatible_heads == 3 and exc.value.incompatible_assertions == 7
    executed = [q for q, _ in fake.executed]
    assert not any("DROP" in q for q in executed)
    assert not any("CREATE INDEX" in q for q in executed)
    assert not any("CREATE CONSTRAINT" in q for q in executed)


@pytest.mark.unit
def test_activation_runs_gated_ddl_on_a_fresh_store():
    # A fresh (or fully-v2) store reports zero legacy nodes (default empty rows -> 0/0), so activation
    # proceeds and brings the source_key identity DDL online, retiring the superseded v1 constraint.
    fake = FakeNeo4j(default=[])
    out = TypedAssertionRepository(fake).activate_scalar_state()
    executed = [q for q, _ in fake.executed]
    assert out["queries_executed"] >= 1
    assert any("DROP CONSTRAINT typed_assertion_head_claim_key_unique IF EXISTS" in q for q in executed)
    assert any("typed_assertion_head_source_key_unique" in q for q in executed)
    assert any("scalar_reconcile_op_unique" in q for q in executed)
    assert any("assertion_rebind_key_unique" in q for q in executed)
    assert any("scalar_projection_repair_key_unique" in q for q in executed)


@pytest.mark.unit
def test_purge_scalar_state_nodes_deletes_log_views_and_watermarks():
    fake = FakeNeo4j(default=[{"assertions": 4, "heads": 2, "rebinds": 1, "receipts": 3,
                               "projection_repairs": 2,
                               "views": 6, "watermarks": 5}])
    out = TypedAssertionRepository(fake).purge_scalar_state_nodes()
    assert out == {"assertions": 4, "heads": 2, "rebinds": 1, "receipts": 3,
                   "projection_repairs": 2,
                   "views": 6, "watermarks": 5}
    q, _ = fake.executed[-1]
    assert "MATCH (a:TypedAssertion) DETACH DELETE a" in q
    assert "MATCH (h:TypedAssertionHead) DETACH DELETE h" in q
    assert "MATCH (r:AssertionRebind) DETACH DELETE r" in q
    assert "MATCH (rc:ScalarReconcile) DETACH DELETE rc" in q
    assert "MATCH (rr:ScalarProjectionRepair) DETACH DELETE rr" in q
    # the materialized projections go too — ALL versions, not only current — so no stale scalar_state
    # View can survive a destructive reset and remain recallable after its assertion log is gone.
    assert "MATCH (v:Entity {view_kind: 'scalar_state'}) DETACH DELETE v" in q
    # ...and the scheduling cursor, or a purged namespace would look already-consolidated and never
    # backfill against the fresh store.
    assert "MATCH (w:ScalarConsolidationWatermark) DETACH DELETE w" in q
