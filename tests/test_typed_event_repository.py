"""Durable :TypedEventAssertion event log (Event History Phase 2B.1) — repository.

No live Neo4j: a FakeNeo4j routes cypher to canned rows and records every call. Proves the log is
durable and idempotent (single atomic statement, head locked on the binding-stable source_key), with
strict-rank supersession, monotonic evidence-tier upgrade, pending->bound adoption, binding-mismatch
guard + provenance gating, optional object link, raw-invalid-time retention with a nullable parsed
param, exact lane filters, include_superseded/materializable filters, row reconstruction,
valid-time ordering (invalid last, never by learned_at), key/source reads, and idempotent schema
activation. Generic synthetic data only — no benchmark IDs/answers/phrases.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from menhir.domain.event_history import EventLane, TypedEventAssertion
from menhir.domain.typed_assertion import evidence_rank, perceiver_rank
from menhir.infrastructure.typed_event_repository import (
    EVENT_IDENTITY_VERSION,
    TypedEventAssertionRepository,
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


def make(subject_uuid="subj-a", predicate="purchased", object_key="obj-x", domain=None,
         namespace=None, valid_at="2026-01-05T00:00:00Z", learned_at="2026-01-06T00:00:00Z",
         episode_uuid="ep-1", **kw) -> TypedEventAssertion:
    base = dict(
        subject_uuid=subject_uuid,
        subject_display="Alpha",
        predicate=predicate,
        object_key=object_key,
        object_display="Object X",
        domain=domain,
        namespace=namespace,
        valid_at=valid_at,
        learned_at=learned_at,
        stated_span="an exact stated span",
        episode_uuid=episode_uuid,
        span_start=10,
        span_end=30,
        claim_ordinal=0,
        turn_evidence_uuid=None,
    )
    base.update(kw)
    return TypedEventAssertion(**base)


def _record_fake():
    return FakeNeo4j(default=[{"assertion_id": "aid-1", "created": True,
                               "superseded_prior": False, "binding_pending": False,
                               "binding_mismatch": False}])


LANE = EventLane(subject_uuid="subj-a", predicate="purchased", namespace=None, domain=None)


# ---------------------------------------------------------------------------------- write

@pytest.mark.unit
def test_record_write_is_a_single_atomic_statement_head_locked_on_source_key():
    # ONE execute(): head lock + mismatch guard + assertion + supersede + provenance + binding.
    # The head is MERGEd on the BINDING-STABLE source_key (its DB-unique identity) — no read-before-
    # write preflight to race.
    fake = _record_fake()
    out = TypedEventAssertionRepository(fake).record_event_assertion(make())
    assert len(fake.executed) == 1
    q, _ = fake.executed[0]
    assert "MERGE (h:TypedEventAssertionHead {source_key: $source_key})" in q
    assert "MERGE (a:TypedEventAssertion {assertion_key: $assertion_key})" in q
    assert "MERGE (h)-[:CURRENT]->(a)" in q
    assert "MERGE (h)-[:HAS_VERSION]->(a)" in q
    assert "MERGE (a)-[:SUPERSEDES]->(c)" in q
    # provenance is atomic with the write: episode GROUNDS, turn FOUNDS, entity HAS_EVENT_ASSERTION,
    # object EVENT_OBJECT.
    for edge in ("GROUNDS", "FOUNDS", "HAS_EVENT_ASSERTION", "EVENT_OBJECT"):
        assert edge in q
    # result shape is exactly the documented five fields.
    assert set(out) == {"assertion_id", "created", "superseded_prior", "binding_pending",
                        "binding_mismatch"}
    assert out["assertion_id"] == "aid-1" and out["created"] is True


@pytest.mark.unit
def test_record_maps_keys_ranks_and_normalized_components_into_params():
    fake = _record_fake()
    a = make(predicate="Purchased", object_key="  Obj-X ", domain="D1", namespace="NS",
             perceiver_version="v3", evidence_tier="user")
    TypedEventAssertionRepository(fake).record_event_assertion(a)
    _, params = fake.executed[0]
    assert params["source_key"] == a.source_key
    assert params["assertion_key"] == a.assertion_key
    assert params["identity_version"] == EVENT_IDENTITY_VERSION
    assert params["perceiver_rank"] == perceiver_rank("v3")
    assert params["evidence_rank"] == evidence_rank("user")
    assert params["span_start"] == 10 and params["span_end"] == 30 and params["claim_ordinal"] == 0
    # predicate/object_key/domain/namespace are normalized for lane matching; display stays verbatim.
    assert params["predicate_norm"] == "purchased"
    assert params["object_key_norm"] == "obj-x"
    assert params["domain_norm"] == "d1"
    assert params["namespace_norm"] == "ns"
    assert params["object_display"] == "Object X"


@pytest.mark.unit
def test_record_keeps_raw_times_and_passes_nullable_parsed_never_datetime_raw():
    # Invalid nonblank valid_at/learned_at must be retained verbatim for audit WITHOUT making Cypher
    # datetime() throw: the raw string goes to *_raw, and the canonical prop gets a nullable parsed
    # value (None here) parsed in Python.
    fake = _record_fake()
    TypedEventAssertionRepository(fake).record_event_assertion(
        make(valid_at="not-a-date", learned_at="also-bad"))
    q, params = fake.executed[0]
    assert params["valid_at_raw"] == "not-a-date"
    assert params["learned_at_raw"] == "also-bad"
    assert params["valid_at_parsed"] is None
    assert params["learned_at_parsed"] is None
    # never hand the raw string to datetime() in Cypher.
    assert "datetime($valid_at_raw)" not in q
    assert "datetime($learned_at_raw)" not in q
    assert "a.valid_at_raw = $valid_at_raw" in q and "a.valid_at = $valid_at_parsed" in q


@pytest.mark.unit
def test_record_parses_valid_times_into_canonical_params():
    # A non-UTC offset must be normalized to UTC in the canonical param (the raw string stays
    # verbatim on *_raw), proving ordering/comparison is offset-independent.
    fake = _record_fake()
    TypedEventAssertionRepository(fake).record_event_assertion(
        make(valid_at="2026-01-05T02:00:00+02:00", learned_at="2026-01-06T03:30:00+05:30"))
    _, params = fake.executed[0]
    assert params["valid_at_parsed"] == datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert params["learned_at_parsed"] == datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)
    # the raw strings are preserved exactly, untouched by normalization.
    assert params["valid_at_raw"] == "2026-01-05T02:00:00+02:00"
    assert params["learned_at_raw"] == "2026-01-06T03:30:00+05:30"


@pytest.mark.unit
def test_supersession_is_strict_rank_and_same_version_does_not_replace():
    # Contract: become current only if no prior current, OR it strictly out-ranks it, OR it IS the
    # current (idempotent). A same-version different-value write (equal rank) does NOT satisfy
    # will_supersede, so it stays superseded=true (non-current audit).
    fake = _record_fake()
    TypedEventAssertionRepository(fake).record_event_assertion(make())
    q = fake.executed[0][0]
    assert "$perceiver_rank > coalesce(cur.perceiver_rank, -1)" in q
    assert "cur IS NULL OR will_supersede" in q
    assert "cur.assertion_key = $assertion_key" in q


@pytest.mark.unit
def test_monotonic_tier_upgrade_in_write_and_on_match_never_restamps_identity():
    # An idempotent re-write (same assertion_key) may UPGRADE the evidence tier, never downgrade;
    # identity_version is stamped only on ON CREATE so a replay never rewrites the version label.
    fake = _record_fake()
    TypedEventAssertionRepository(fake).record_event_assertion(make())
    q, params = fake.executed[0]
    assert "evidence_rank > coalesce(a.evidence_rank, -1)" in q
    assert "ON MATCH SET" in q
    assert params["identity_version"] == EVENT_IDENTITY_VERSION
    assert "h.identity_version = $identity_version" in q
    assert "a.identity_version = $identity_version" in q
    on_match = q.partition("ON MATCH SET")[2]
    assert "identity_version" not in on_match


@pytest.mark.unit
def test_binding_mismatch_guard_precedes_node_and_gates_all_mutations():
    # Mismatch is decided from the head's EXISTING current owner (a bound owner that differs) BEFORE
    # any node create / CURRENT move / supersession; every mutation is gated on NOT binding_mismatch.
    fake = _record_fake()
    TypedEventAssertionRepository(fake).record_event_assertion(make())
    q = fake.executed[0][0]
    normalized = " ".join(q.split())
    assert "cur IS NOT NULL AND NOT coalesce(cur.binding_pending, true)" in normalized
    assert "cur.subject_uuid <> $subject_uuid) AS binding_mismatch" in normalized
    assert normalized.index("AS binding_mismatch") < normalized.index("MERGE (a:TypedEventAssertion")
    assert "(NOT binding_mismatch) AND (cur IS NULL OR will_supersede" in normalized
    assert "(NOT binding_mismatch) AND cur IS NOT NULL AND cur.assertion_key <> $assertion_key" in normalized
    assert "(NOT binding_mismatch) AND fully_bound AND was_pending" in normalized
    assert "(NOT binding_mismatch) AND $evidence_rank > coalesce(a.evidence_rank, -1)" in normalized
    assert "WHEN binding_mismatch THEN [] ELSE [1] END | SET a.binding_pending" in normalized
    # provenance links gated: never draw GROUNDS/FOUNDS/HAS_EVENT_ASSERTION/EVENT_OBJECT on a mismatch.
    for edge in ("GROUNDS", "FOUNDS", "HAS_EVENT_ASSERTION", "EVENT_OBJECT"):
        assert "(NOT binding_mismatch)" in normalized and edge in normalized


@pytest.mark.unit
def test_optional_object_link_is_gated_on_resolved_object_entity():
    # object_uuid resolving to an Entity draws assertion-[:EVENT_OBJECT]->object; a missing object
    # Entity alone does NOT make binding_pending (obj is optional, unlike the subject/foundation).
    fake = _record_fake()
    TypedEventAssertionRepository(fake).record_event_assertion(make(object_uuid="obj-uu"))
    q = fake.executed[0][0]
    assert "MERGE (a)-[:EVENT_OBJECT]->(obj)" in q
    assert "WHEN (NOT binding_mismatch) AND obj IS NOT NULL THEN [1] ELSE [] END" in q
    # binding_pending depends only on foundation (e/te) and subject (n), never on obj.
    assert "SET a.binding_pending = ((e IS NULL AND te IS NULL) OR n IS NULL)" in q


@pytest.mark.unit
def test_pending_to_bound_adoption_updates_subject_on_replay():
    # Because assertion_key omits subject_uuid, an idempotent replay of a still-pending sentinel
    # claim with a now-resolved real subject lands on the SAME node; it must adopt the resolved
    # subject onto the assertion and head when NOT mismatch AND fully_bound AND was_pending. No
    # projection_pending marker (that belongs to later orchestration).
    fake = _record_fake()
    TypedEventAssertionRepository(fake).record_event_assertion(make())
    q = fake.executed[0][0]
    assert "fully_bound" in q and "was_pending" in q
    assert "SET a.subject_uuid = $subject_uuid, a.subject_display = $subject_display" in q
    assert "h.subject_uuid = $subject_uuid" in q
    # no projection_pending write token — that belongs to later orchestration.
    assert "SET a.projection_pending" not in q


@pytest.mark.unit
def test_record_reports_mismatch_and_pending_from_return():
    fake = FakeNeo4j(default=[{"assertion_id": "aid-3", "created": False, "superseded_prior": True,
                               "binding_pending": True, "binding_mismatch": True}])
    out = TypedEventAssertionRepository(fake).record_event_assertion(make())
    assert out == {"assertion_id": "aid-3", "created": False, "superseded_prior": True,
                   "binding_pending": True, "binding_mismatch": True}


# ---------------------------------------------------------------------------------- read

@pytest.mark.unit
def test_lane_filter_is_exact_normalized_namespace_domain_subject_predicate():
    fake = FakeNeo4j(default=[])
    lane = EventLane(subject_uuid="subj-a", predicate="purchased", namespace="NS", domain="D1")
    TypedEventAssertionRepository(fake).assertions_for_lane(lane)
    q, params = fake.executed[-1]
    for comp in ("a.namespace", "a.subject_uuid", "a.predicate", "a.domain"):
        assert f"toLower(trim(coalesce({comp}, ''))) = $" in q
    assert params == {"namespace": "ns", "subject_uuid": "subj-a",
                      "predicate": "purchased", "domain": "d1"}
    # default excludes superseded but KEEPS binding-pending audit rows.
    assert "NOT coalesce(a.superseded, false)" in q
    assert "NOT coalesce(a.binding_pending, false)" not in q


@pytest.mark.unit
def test_include_superseded_and_materializable_change_the_filters():
    fake = FakeNeo4j(default=[])
    repo = TypedEventAssertionRepository(fake)
    repo.assertions_for_lane(LANE, include_superseded=True)
    q_inc = fake.executed[-1][0]
    assert "NOT coalesce(a.superseded, false)" not in q_inc
    repo.assertions_for_lane(LANE, materializable_only=True)
    q_mat = fake.executed[-1][0]
    assert "NOT coalesce(a.binding_pending, false)" in q_mat
    assert "NOT coalesce(a.superseded, false)" in q_mat
    repo.assertions_for_lane(LANE)
    q_default = fake.executed[-1][0]
    assert "NOT coalesce(a.binding_pending, false)" not in q_default


def _row(**over):
    base = dict(
        assertion_id="a1", source_key="sk1", assertion_key="ak1",
        subject_uuid="subj-a", subject_display="Alpha", predicate="purchased",
        object_key="obj-x", object_display="Object X", object_uuid="obj-uu",
        domain=None, namespace=None,
        valid_at_raw="2026-01-05T00:00:00Z",
        learned_at_raw="2026-01-06T00:00:00Z",
        valid_at="2026-01-05T00:00:00Z", learned_at="2026-01-06T00:00:00Z",
        stated_span="an exact stated span", span_start=10, span_end=30, claim_ordinal=0,
        episode_uuid="ep-1", turn_evidence_uuid="turn-9", time_basis="explicit",
        evidence_tier="user", perceiver_version="v2",
        metadata=json.dumps({"k": "v"}),
    )
    base.update(over)
    return base


@pytest.mark.unit
def test_row_reconstruction_hydrates_all_fields():
    fake = FakeNeo4j(default=[_row()])
    ev = TypedEventAssertionRepository(fake).assertions_for_lane(LANE)[0]
    assert ev.subject_uuid == "subj-a" and ev.subject_display == "Alpha"
    assert ev.predicate == "purchased" and ev.object_key == "obj-x" and ev.object_display == "Object X"
    assert ev.object_uuid == "obj-uu" and ev.turn_evidence_uuid == "turn-9"
    assert ev.valid_at == "2026-01-05T00:00:00Z" and ev.learned_at == "2026-01-06T00:00:00Z"
    assert ev.stated_span == "an exact stated span"
    assert ev.span_start == 10 and ev.span_end == 30 and ev.claim_ordinal == 0
    assert ev.episode_uuid == "ep-1" and ev.time_basis == "explicit"
    assert ev.evidence_tier == "user" and ev.perceiver_version == "v2"
    assert ev.metadata == {"k": "v"}


@pytest.mark.unit
def test_reconstruction_prefers_raw_when_canonical_parsed_absent():
    # Hydration prefers the raw source string; if it is absent it falls back to toString(parsed).
    raw = _row(valid_at_raw="2026-01-05T00:00:00Z", valid_at=None,
               learned_at_raw="2026-01-06T00:00:00Z", learned_at=None)
    fake = FakeNeo4j(default=[raw])
    ev = TypedEventAssertionRepository(fake).assertions_for_lane(LANE)[0]
    assert ev.valid_at == "2026-01-05T00:00:00Z" and ev.learned_at == "2026-01-06T00:00:00Z"
    # and the parsed-only fallback path when raw is empty.
    only_parsed = _row(valid_at_raw="", valid_at="2026-02-01T00:00:00Z",
                       learned_at_raw="", learned_at="2026-02-02T00:00:00Z")
    fake2 = FakeNeo4j(default=[only_parsed])
    ev2 = TypedEventAssertionRepository(fake2).assertions_for_lane(LANE)[0]
    assert ev2.valid_at == "2026-02-01T00:00:00Z" and ev2.learned_at == "2026-02-02T00:00:00Z"


@pytest.mark.unit
def test_lane_ordering_is_valid_ascending_with_invalid_last_independent_of_learned_at():
    # Deterministic: parseable valid world times first ascending (then assertion_key); unparseable
    # valid times LAST — and never ordered by learned_at/input order. Objects are identified by
    # object_key (TypedEventAssertion has no assertion_id field).
    rows = [
        _row(object_key="obj-later", valid_at_raw="2026-03-01T00:00:00Z",
             learned_at_raw="2026-01-01T00:00:00Z"),
        _row(object_key="obj-bogus", valid_at_raw="not-a-date",
             learned_at_raw="2026-01-01T00:00:00Z"),
        _row(object_key="obj-earlier", valid_at_raw="2026-01-05T00:00:00Z",
             learned_at_raw="2026-06-01T00:00:00Z"),
    ]
    fake = FakeNeo4j(default=rows)
    events = TypedEventAssertionRepository(fake).assertions_for_lane(LANE)
    assert [e.object_key for e in events] == ["obj-earlier", "obj-later", "obj-bogus"]
    # the invalid row is last even though it has an EARLIER learned_at than the valid rows.
    assert events[-1].object_key == "obj-bogus"


@pytest.mark.unit
def test_valid_ties_order_by_assertion_key():
    # Two assertions tie on valid_at but differ in interpretation (object_key), so their
    # assertion_keys differ; ordering must be by assertion_key, never input order. A tie's
    # assertion_key sequence must be sorted.
    rows = [
        _row(object_key="zebra", valid_at_raw="2026-01-05T00:00:00Z"),
        _row(object_key="alpha", valid_at_raw="2026-01-05T00:00:00Z"),
    ]
    fake = FakeNeo4j(default=rows)
    events = TypedEventAssertionRepository(fake).assertions_for_lane(LANE)
    keys = [e.assertion_key for e in events]
    assert keys == sorted(keys)


@pytest.mark.unit
def test_hydration_keeps_a_real_zero_span_offset():
    # A known span offset of 0 must survive hydration (not be defaulted to -1 as if absent). Both
    # offsets present and equal -> a valid, reconstruction-safe zero-width span.
    fake = FakeNeo4j(default=[_row(span_start=0, span_end=0)])
    ev = TypedEventAssertionRepository(fake).assertions_for_lane(LANE)[0]
    assert ev.span_start == 0 and ev.span_end == 0


@pytest.mark.unit
def test_assertion_by_key_returns_hydrated_or_none():
    # Assert hydrated DOMAIN fields (TypedEventAssertion derives assertion_key, so we don't require a
    # canned assertion_key string to match) plus the query param that scopes the lookup.
    fake = FakeNeo4j(default=[_row()])
    ev = TypedEventAssertionRepository(fake).assertion_by_key("ak1")
    assert ev is not None and ev.evidence_tier == "user" and ev.predicate == "purchased"
    assert ev.object_key == "obj-x" and ev.episode_uuid == "ep-1"
    q, params = fake.executed[-1]
    assert "MATCH (a:TypedEventAssertion {assertion_key: $assertion_key})" in q
    assert params["assertion_key"] == "ak1"
    fake2 = FakeNeo4j(default=[])
    assert TypedEventAssertionRepository(fake2).assertion_by_key("missing") is None


@pytest.mark.unit
def test_current_for_source_reads_through_the_head():
    fake = FakeNeo4j(default=[_row(source_key="sk1")])
    ev = TypedEventAssertionRepository(fake).current_for_source("sk1")
    assert ev is not None and ev.episode_uuid == "ep-1"
    q, params = fake.executed[-1]
    assert "TypedEventAssertionHead {source_key: $source_key})-[:CURRENT]->" in q
    assert params["source_key"] == "sk1"
    fake2 = FakeNeo4j(default=[])
    assert TypedEventAssertionRepository(fake2).current_for_source("missing") is None


# ------------------------------------------------------------------ subject/predicate read

def _subject_predicate_rows():
    return [
        _row(object_key="obj-later", valid_at_raw="2026-03-01T00:00:00Z",
             learned_at_raw="2026-01-01T00:00:00Z"),
        _row(object_key="obj-bogus", valid_at_raw="not-a-date",
             learned_at_raw="2026-01-01T00:00:00Z"),
        _row(object_key="obj-earlier", valid_at_raw="2026-01-05T00:00:00Z",
             learned_at_raw="2026-06-01T00:00:00Z"),
    ]


@pytest.mark.unit
def test_subject_predicate_read_is_exact_normalized_no_domain_override():
    fake = FakeNeo4j(default=[])
    TypedEventAssertionRepository(fake).assertions_for_subject_predicate(
        "subj-a", "Purchased", namespace="NS")
    q, params = fake.executed[-1]
    assert "toLower(trim(coalesce(a.subject_uuid, ''))) = $subject_uuid" in q
    assert "toLower(trim(coalesce(a.predicate, ''))) = $predicate" in q
    assert "toLower(trim(coalesce(a.namespace, ''))) = $namespace" in q
    # no domain predicate in the WHERE, and no domain override param.
    assert "coalesce(a.domain" not in q
    assert params == {"subject_uuid": "subj-a", "predicate": "purchased", "namespace": "ns"}


@pytest.mark.unit
def test_subject_predicate_without_namespace_omits_namespace_clause():
    fake = FakeNeo4j(default=[])
    TypedEventAssertionRepository(fake).assertions_for_subject_predicate("subj-a", "purchased")
    q, params = fake.executed[-1]
    assert "toLower(trim(coalesce(a.namespace, ''))) = $namespace" not in q
    assert params == {"subject_uuid": "subj-a", "predicate": "purchased"}


@pytest.mark.unit
def test_subject_predicate_defaults_exclude_superseded_and_binding_pending():
    fake = FakeNeo4j(default=[])
    TypedEventAssertionRepository(fake).assertions_for_subject_predicate("subj-a", "purchased")
    q = fake.executed[-1][0]
    assert "NOT coalesce(a.superseded, false)" in q
    assert "NOT coalesce(a.binding_pending, false)" in q


@pytest.mark.unit
def test_subject_predicate_opt_ins_remove_exclusion_clauses():
    fake = FakeNeo4j(default=[])
    repo = TypedEventAssertionRepository(fake)
    repo.assertions_for_subject_predicate(
        "subj-a", "purchased", include_superseded=True, materializable_only=False)
    q = fake.executed[-1][0]
    assert "NOT coalesce(a.superseded, false)" not in q
    assert "NOT coalesce(a.binding_pending, false)" not in q


@pytest.mark.unit
def test_subject_predicate_hydrates_and_sorts_by_valid_invalid_last():
    fake = FakeNeo4j(default=_subject_predicate_rows())
    events = TypedEventAssertionRepository(fake).assertions_for_subject_predicate(
        "subj-a", "purchased")
    assert [e.object_key for e in events] == ["obj-earlier", "obj-later", "obj-bogus"]
    assert events[0].predicate == "purchased" and events[0].evidence_tier == "user"


@pytest.mark.parametrize(
    "subject,predicate",
    [("", "purchased"), ("subj-a", ""), ("   ", "purchased"), ("subj-a", "   ")],
)
def test_subject_predicate_blank_required_inputs_reject_before_execute(subject, predicate):
    fake = FakeNeo4j(default=[])
    with pytest.raises(ValueError):
        TypedEventAssertionRepository(fake).assertions_for_subject_predicate(subject, predicate)
    assert fake.executed == []


# ---------------------------------------------------------------------------------- schema

@pytest.mark.unit
def test_activate_runs_idempotent_ddl():
    fake = FakeNeo4j(default=[])
    out = TypedEventAssertionRepository(fake).activate()
    executed = [q for q, _ in fake.executed]
    assert out["queries_executed"] == len(executed)
    assert any("typed_event_assertion_head_source_key_unique" in q for q in executed)
    assert any("typed_event_assertion_key_unique" in q for q in executed)
    assert any("typed_event_assertion_id_unique" in q for q in executed)
    assert any("event_consolidation_watermark_group_id_unique" in q for q in executed)
    assert any(
        "FOR (w:EventConsolidationWatermark) REQUIRE w.group_id IS UNIQUE" in q
        for q in executed
    )
    # every statement is idempotent and non-destructive — no DROP, no DETACH DELETE, no purge.
    for q in executed:
        assert "IF NOT EXISTS" in q
        assert "DROP" not in q and "DELETE" not in q and "PURGE" not in q.upper()
    # useful lane/source indexes.
    assert any("typed_event_assertion_lane_idx" in q for q in executed)
    assert any("typed_event_assertion_source_idx" in q for q in executed)
    assert any("typed_event_assertion_valid_at_idx" in q for q in executed)
