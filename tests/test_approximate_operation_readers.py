"""The `approximate` operation (a hedged value, "I think I'm close to 1300") is read, never written.

Readers ship before any writer, so every reader that meets a stored approximate row must: keep it out
of the scalar fold (it never sets or changes a value), list it in history as a value statement, render
it as "about N", and not report it as an unaccounted projection. No parse path may emit one. Each
counterexample is paired with a positive control showing the same path is live.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from menhir.domain.assertion_operations import OPERATIONS
from menhir.domain.projection_coverage import EligibilityRole
from menhir.domain.projection_coverage_types import AuditFailureClassification
from menhir.domain.scalar_history import build_history
from menhir.domain.scalar_identity import CompositionalScalarIdentity, ScalarIdentityProvenance
from menhir.domain.scalar_state_fold import fold_assertions
from menhir.domain.typed_assertion import TypedAssertion, build_source_key, validate_value
from menhir.infrastructure import scalar_view_repository
from menhir.services.projection_coverage_service import ProjectionCoverageService
from menhir.services.recall_policies import (
    _render_scalar_history_content,
    _scalar_history_records_change,
)
from menhir.services.scalar_state_service import ScalarStateService
from menhir.services.typed_scalar_rules import parse_scalar_row

pytestmark = pytest.mark.unit

OLD = "2026-07-01T00:00:00+00:00"
NEW = "2026-09-01T00:00:00+00:00"


def _row(**over):
    base = dict(
        assertion_id="a?", subject_uuid="ent-me", subject_display="my followers",
        attribute="followers", scope="", value_kind="count", unit="", operation="absolute",
        value=1250, valid_at=OLD, learned_at=OLD, evidence_tier="user",
        episode_uuid="ep-1", turn_id="t1", stated_span="1250 followers", binding_pending=False,
    )
    base.update(over)
    return base


def _approx(**over):
    base = dict(assertion_id="apx", operation="approximate", value=1300, valid_at=NEW,
                stated_span="close to 1300")
    base.update(over)
    return _row(**base)


# ------------------------------------------------------------------------------ 1. scalar fold

def test_fold_newer_approximate_does_not_move_or_contribute_to_the_value():
    old_abs = _row(assertion_id="abs", value=1250)
    result = fold_assertions([old_abs, _approx()])
    assert [s.value for s in result.states] == [1250]
    assert "apx" not in repr(result)
    assert "apx" not in result.states[0].contributor_ids

    # positive control: the same newer row as an absolute DOES move the value.
    moved = fold_assertions([old_abs, _approx(operation="absolute")])
    assert [s.value for s in moved.states] == [1300]


def test_fold_approximate_alone_makes_no_state_and_no_abstention():
    result = fold_assertions([_approx()])
    assert (result.states, result.abstentions, result.expiries) == ([], [], [])

    # positive control: a lone known non-anchor row (delta) does create its slot as an abstention.
    assert fold_assertions([_approx(operation="delta", value=5)]).abstentions


# ------------------------------------------------------------------------------ 2. validator

@pytest.mark.parametrize("kind,value", [("count", 1300), ("money", 1300.5)])
def test_validate_value_accepts_a_single_number_for_approximate(kind, value):
    validate_value(kind, "approximate", value)  # must not raise


@pytest.mark.parametrize("kind,value", [
    ("count", [1200, 1400]),
    ("money", [1200, 1400]),
    ("status", "x"),
    ("count", 12.5),
    ("count", float("nan")),
    ("count", True),
])
def test_validate_value_rejects_a_non_single_or_non_numeric_approximate(kind, value):
    with pytest.raises(ValueError):
        validate_value(kind, "approximate", value)


def test_validate_value_controls_show_the_rejected_shapes_differ_by_operation():
    # a range is legal for an absolute count and a status string for an absolute status, so the
    # approximate rejections above are specific to the approximate branch.
    validate_value("count", "absolute", [1200, 1400])
    validate_value("status", "absolute", "x")


def test_typed_assertion_with_approximate_operation_can_be_constructed():
    ta = TypedAssertion(
        subject_uuid="s-1", subject_display="User", attribute="followers", scope="",
        value_kind="count", unit="", operation="approximate", value=1300,
        stated_span="close to 1300", episode_uuid="ep-1", valid_at=OLD, learned_at=OLD,
        span_start=8, span_end=20,
    )
    assert ta.operation == "approximate"
    # control: an unknown operation is refused by the same constructor.
    with pytest.raises(ValueError):
        TypedAssertion(
            subject_uuid="s-1", subject_display="User", attribute="followers", scope="",
            value_kind="count", unit="", operation="zz_future", value=1300,
            stated_span="close to 1300", episode_uuid="ep-1", valid_at=OLD, learned_at=OLD,
            span_start=8, span_end=20,
        )


# ------------------------------------------------------------------------------ 3. no writer

@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _parse(operation):
    ep = _Ep(uuid="ep-1", content="I have 1300 followers now.")
    row = dict(episode=0, subject="user", attribute="followers", scope="", value_kind="count",
               unit="", operation=operation, value=1300, stated_span="I have 1300 followers")
    drops: list[str] = []
    return parse_scalar_row(row, [ep], drops.append), drops


def test_approximate_is_not_a_writer_operation_and_parse_scalar_row_refuses_it():
    assert "approximate" not in OPERATIONS
    proposal, drops = _parse("approximate")
    assert proposal is None and drops == ["bad_attribute_or_operation"]

    # positive control: the identical row as "absolute" parses.
    ok, ok_drops = _parse("absolute")
    assert ok is not None and ok_drops == []


def _identity(operation):
    key = build_source_key("ep-1", 0, 10, 0)
    prov = ScalarIdentityProvenance(
        episode_uuid="ep-1", span_start=0, span_end=10, claim_ordinal=0, source_key=key)
    return CompositionalScalarIdentity(
        subject="user", relation_type="quantity", target_or_scope=("followers", ""),
        value_kind="count", value="1300", unit="", operation=operation,
        effective_time=None, provenance=prov)


def test_compositional_identity_refuses_approximate():
    assert _identity("absolute").operation == "absolute"  # control: constructs
    with pytest.raises(ValueError):
        _identity("approximate")


# ------------------------------------------------------------------------------ 4. history

@pytest.mark.parametrize("count_match", [False, True])
def test_history_lists_an_approximate_entry_beside_the_absolute(count_match):
    rows = [_row(assertion_id="abs", value=1250), _approx()]
    result = build_history(rows, count_match=count_match)
    assert [e.assertion_id for e in result.projections[0].all_entries] == ["abs", "apx"]
    assert result.projections[0].operation_counts == {"absolute": 1, "approximate": 1}

    # positive control: an unknown operation in the same position is not listed.
    unknown = build_history(rows + [_approx(assertion_id="unk", operation="zz_future")],
                            count_match=count_match)
    assert [e.assertion_id for e in unknown.projections[0].all_entries] == ["abs", "apx"]


# ------------------------------------------------------------------------------ 5. change check

def _entry(op, value):
    return {"operation": op, "value": value}


def test_scalar_history_change_treats_approximate_as_a_value_statement():
    assert _scalar_history_records_change(
        {"entries": [_entry("absolute", 1300), _entry("approximate", 1300)]}) is False
    assert _scalar_history_records_change(
        {"entries": [_entry("absolute", 1250), _entry("approximate", 1300)]}) is True
    assert _scalar_history_records_change({"entries": [_entry("approximate", 1300)]}) is False
    # positive control: a delta is a change.
    assert _scalar_history_records_change(
        {"entries": [_entry("absolute", 1250), _entry("delta", 50)]}) is True


# ------------------------------------------------------------------------------ 6. rendering

def test_render_history_shows_an_approximate_as_about_n_including_latest_line():
    view = {
        "attribute": "followers", "scope": "", "subject": "user",
        "operation_counts": {"absolute": 1, "approximate": 1},
        "entries": [
            {"operation": "absolute", "value": 1250, "valid_at": OLD, "stated_span": "1250"},
            {"operation": "approximate", "value": 1300, "valid_at": NEW,
             "stated_span": "close to 1300"},
        ],
    }
    lines = _render_scalar_history_content(view).splitlines()
    assert any("about 1300 (approximate)" in line for line in lines)
    assert lines[-1].startswith("Latest: about 1300")
    # positive control: the absolute entry is rendered bare, not as "about".
    assert any("1250 (absolute)" in line and "about" not in line for line in lines)


# ------------------------------------------------------------------------------ 7. coverage

NS = "tenant-a"
SUBJECT = "entity-1"


def _cov_row(assertion_id, operation):
    return {
        "assertion_id": assertion_id, "assertion_key": f"ak-{assertion_id}",
        "source_key": f"sk-{assertion_id}", "subject_uuid": SUBJECT,
        "subject_display": "Entity One", "namespace": NS, "attribute": "followers", "scope": "",
        "value_kind": "count", "unit": "", "operation": operation, "value": 1300,
        "valid_at": OLD, "learned_at": OLD, "episode_uuid": f"ep-{assertion_id}",
        "evidence_tier": "user", "perceiver_version": "v1", "absolute_semantics": "ordinary",
        "superseded": False, "binding_pending": False, "projection_pending": False,
        "head_subject_uuid": SUBJECT, "head_current_subject_uuid": SUBJECT,
        "head_current_assertion_id": assertion_id,
    }


class _Source:
    def __init__(self, assertions):
        self.assertions = assertions

    def assertions_for_projection_audit(self, subject_uuid, *, namespace=None):
        return [dict(r) for r in self.assertions]

    def list_scalar_state_views_for_audit(self, *, subject_uuid, namespace=None):
        return []


def _audit(operation):
    service = ProjectionCoverageService(
        _Source([_cov_row("x1", operation)]), counting_enabled=True, count_match=True)
    return service.audit_entity(SUBJECT, namespace=NS)


def test_lone_approximate_is_history_only_not_unaccounted():
    report = _audit("approximate")
    assert AuditFailureClassification.UNACCOUNTED not in {
        v.classification for v in report.coverage_violations}
    assert [r.eligibility_role for r in report.accounting] == [EligibilityRole.HISTORY_ONLY]

    # positive control: an unknown operation in the same position IS reported unaccounted.
    unknown = _audit("zz_future")
    assert AuditFailureClassification.UNACCOUNTED in {
        v.classification for v in unknown.coverage_violations}


# ------------------------------------------------------------------------------ 8. rebuild history

class _Source8:
    def __init__(self, rows):
        self._rows = rows

    def materializable_assertions_for_entity(self, subject_uuid, *, namespace=None):
        return [r for r in self._rows if r["subject_uuid"] == subject_uuid]


class _Sink:
    def __init__(self):
        self.scalar_histories = []
        self.drawn = []
        self._n = 0

    def record_scalar_history(self, **kw):
        self._n += 1
        self.scalar_histories.append(kw)
        return {"uuid": f"v{self._n}", "view_key": f"h{self._n}", "created": True,
                "superseded": False}

    def list_scalar_history_views(self, *, subject_uuid, namespace=None):
        return []

    def retire_scalar_history(self, *, view_key):
        return True

    def draw_scalar_history_entries(self, *, view_uuid, entries):
        self.drawn.append(list(entries))
        return {"history_entries": len(entries)}


def _rebuild(rows, *, counting_enabled):
    sink = _Sink()
    svc = ScalarStateService(_Source8(rows), sink, counting_enabled=counting_enabled)
    result = svc.rebuild_scalar_history("ent-me", namespace="test")
    return result, sink


def _occ(aid):
    return _row(assertion_id=aid, operation="occurrence", value=1, attribute="followers",
                importance="notable", count_status="counted", grounding_id=f"turn-{aid}",
                valid_at="2026-08-01T00:00:00+00:00")


def test_rebuild_history_with_counting_off_keeps_approximate_and_drops_occurrence():
    rows = [_row(assertion_id="abs"), _approx(), _occ("occ")]
    result, sink = _rebuild(rows, counting_enabled=False)
    assert result["written"] == 1
    ops = json.loads(sink.scalar_histories[0]["audit"]["scalar_history_ops"])
    assert ops == {"absolute": 1, "approximate": 1}
    assert [e.assertion_id for e in sink.drawn[0]] == ["abs", "apx"]

    # positive control: with counting on the same occurrence row IS listed.
    _, sink_on = _rebuild(rows, counting_enabled=True)
    assert "occ" in [e.assertion_id for e in sink_on.drawn[0]]


# ------------------------------------------------------------------------------ 9. Cypher filters

def test_history_filter_lists_approximate_and_provenance_filter_does_not():
    assert "'approximate'" in scalar_view_repository._HISTORY_OPERATION_FILTER
    assert "'approximate'" not in scalar_view_repository._PROVENANCE_OPERATION_FILTER
    # positive control: both filters list a real value claim.
    assert "'absolute'" in scalar_view_repository._HISTORY_OPERATION_FILTER
    assert "'absolute'" in scalar_view_repository._PROVENANCE_OPERATION_FILTER
