"""Related-count matching (unit 4R): the match-mode counted fold, offline.

Only `counted` rows add to a View's value; every other live occurrence is reported in
`count_uncertain`, so no happening is counted twice and none silently disappears. A counted slot is
keyed by the object family, not the extracted attribute. Match off must be byte-identical to before.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from menhir.domain.count_match import status_of
from menhir.domain.scalar_state_fold import BASIS_COUNTED, fold_assertions
from menhir.services.scalar_state_service import ScalarStateService, _count_match_audit


def _row(i, day, **over):
    base = dict(
        assertion_id=f"o{i}", subject_uuid="ent-self", subject_display="me",
        attribute="model_kit_count", scope="", value_kind="count", unit="", operation="occurrence",
        value=1, importance="notable", valid_at=f"2026-07-{day:02d}T00:00:00+00:00",
        learned_at=f"2026-07-{day:02d}T00:00:00+00:00", evidence_tier="user",
        episode_uuid=f"ep-{i}", binding_pending=False,
        count_object="model kit", count_item="", count_action="", count_status="",
        count_target_id="", count_family="",
    )
    base.update(over)
    return base


def _only(result):
    assert not result.abstentions, result.abstentions
    state, = result.states
    return state


# ------------------------------------------------------------------ A. fold semantics

@pytest.mark.unit
def test_counted_plus_uncertain_retelling_reports_both_but_adds_once():
    # Fails if an uncertain row is added to the value (double count) or dropped from count_uncertain.
    a = _row(1, 1, count_status="counted")
    b = _row(2, 5, count_status="uncertain")
    s = _only(fold_assertions([a, b], count_match=True))
    assert s.value == 1 and s.count_uncertain == 1 and s.basis == BASIS_COUNTED
    assert s.contributor_ids == ["o1"]
    assert s.uncertain_occurrence_ids == ["o2"]


@pytest.mark.unit
def test_repeat_of_a_counted_row_is_excluded_and_absorbed():
    # Fails if a resolved repeat still counts, or lands in count_uncertain, or in the contributors.
    a = _row(1, 1, count_status="counted")
    b = _row(2, 5, count_status="repeat", count_target_id="o1")
    s = _only(fold_assertions([a, b], count_match=True))
    assert s.value == 1 and s.count_uncertain == 0
    assert "o2" in s.absorbed_occurrence_ids
    assert "o2" not in s.contributor_ids and "o2" not in s.contributed_occurrence_ids


@pytest.mark.unit
def test_same_event_of_a_counted_row_is_also_excluded():
    a = _row(1, 1, count_status="counted")
    b = _row(2, 5, count_status="same_event", count_target_id="o1")
    s = _only(fold_assertions([a, b], count_match=True))
    assert s.value == 1 and s.count_uncertain == 0 and "o2" in s.absorbed_occurrence_ids


@pytest.mark.unit
def test_repeat_with_a_dead_target_falls_back_to_uncertain():
    # Fails if a repeat is trusted when its target is not a current counted row (it would vanish).
    a = _row(1, 1, count_status="counted")
    b = _row(2, 5, count_status="repeat", count_target_id="ghost")
    s = _only(fold_assertions([a, b], count_match=True))
    assert s.value == 1 and s.count_uncertain == 1
    assert "o2" not in s.absorbed_occurrence_ids


@pytest.mark.unit
def test_repeat_pointing_at_itself_is_uncertain():
    b = _row(1, 5, count_status="repeat", count_target_id="o1")
    s = _only(fold_assertions([b], count_match=True))
    assert s.value == 0 and s.count_uncertain == 1


@pytest.mark.unit
def test_repeat_of_an_uncertain_target_counts_neither_but_reports_both():
    # Nothing counted: the View is still produced (value 0) so the uncertain total is visible.
    a = _row(1, 1, count_status="uncertain")
    b = _row(2, 5, count_status="repeat", count_target_id="o1")
    s = _only(fold_assertions([a, b], count_match=True))
    assert s.value == 0 and s.count_uncertain == 2
    assert s.contributor_ids == [] and sorted(s.uncertain_occurrence_ids) == ["o1", "o2"]


@pytest.mark.unit
@pytest.mark.parametrize("missing", [True, False])
def test_no_decision_is_never_counted(missing):
    # Fails if an undecided row (status '' or key absent) is counted instead of uncertain.
    rows = [_row(1, 1), _row(2, 2), _row(3, 3)]
    if missing:
        for r in rows:
            del r["count_status"]
    s = _only(fold_assertions(rows, count_match=True))
    assert s.value == 0 and s.count_uncertain == 3 and s.contributor_ids == []


@pytest.mark.unit
def test_finished_and_bought_the_same_kit_merge_into_one_family_view():
    # Fails if the slot is still keyed by the extracted attribute (two Views) or per-action counts
    # or the item list are wrong.
    fin = _row(1, 1, attribute="model_kit_finished_count", count_status="counted",
               count_action="finished", count_item="Spitfire")
    buy = _row(2, 9, attribute="kit_bought_count", count_status="counted",
               count_action="bought", count_item="Spitfire")
    s = _only(fold_assertions([fin, buy], count_match=True))
    assert s.attribute == "model_kit_count" and s.value == 2
    assert s.count_actions == {"finished": 1, "bought": 1}
    assert s.count_items == ["Spitfire"]
    assert s.scope == ""


@pytest.mark.unit
def test_money_family_sums_counted_spend():
    a = _row(1, 1, attribute="concert_spend", value_kind="money", unit="usd", value=80,
             count_object="concert ticket", count_status="counted")
    b = _row(2, 9, attribute="tickets_spend", value_kind="money", unit="usd", value=40,
             count_object="concert ticket", count_status="counted")
    s = _only(fold_assertions([a, b], count_match=True))
    assert s.attribute == "concert_ticket_spend" and s.value_kind == "money"
    assert Decimal(str(s.value)) == Decimal("120")


@pytest.mark.unit
def test_same_turn_dedup_keys_on_item_under_match():
    # Fails if two different kits bought in one turn collapse to one, or the same kit stays two.
    def rows(item_b):
        return [
            _row(1, 1, grounding_id="turn-1", episode_uuid="ep-1", count_status="counted",
                 count_item="Spitfire"),
            _row(2, 1, grounding_id="turn-1", episode_uuid="ep-1", count_status="counted",
                 count_item=item_b),
        ]
    assert _only(fold_assertions(rows("Hurricane"), count_match=True)).value == 2
    assert _only(fold_assertions(rows("spitfire"), count_match=True)).value == 1


@pytest.mark.unit
def test_same_turn_dedup_prefers_the_counted_row():
    # Fails if the dedup survivor is chosen by id/value alone and discards the counted row.
    rows = [
        _row(1, 1, grounding_id="turn-1", count_status="uncertain", count_item="Spitfire"),
        _row(2, 1, grounding_id="turn-1", count_status="counted", count_item="Spitfire"),
    ]
    s = _only(fold_assertions(rows, count_match=True))
    assert s.value == 1 and s.contributor_ids == ["o2"]


@pytest.mark.unit
def test_match_off_ignores_count_fields_entirely():
    # Fails if any count_* field changes the off-mode fold (value, keys, contributors, audit fields).
    stripped_keys = ("count_object", "count_item", "count_action", "count_status",
                     "count_target_id", "count_family")
    with_fields = [
        _row(1, 1, count_status="uncertain", count_item="A"),
        _row(2, 2, attribute="kit_bought_count", count_status="repeat", count_target_id="o1",
             count_action="bought"),
        _row(3, 3, count_status="counted", count_family="something else"),
    ]
    plain = [{k: v for k, v in r.items() if k not in stripped_keys} for r in with_fields]
    a = fold_assertions(with_fields, count_match=False)
    b = fold_assertions(plain, count_match=False)
    assert a == b
    # two attributes -> two slots, uncounted statuses all counted: proves no family merge, no filtering
    assert sorted((s.attribute, s.value) for s in a.states) == [
        ("kit_bought_count", 1), ("model_kit_count", 2)]
    assert all(s.count_uncertain is None and s.count_actions is None and s.count_items is None
               for s in a.states)


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("COUNTED", "counted"), (" Repeat ", "repeat"), ("Same_Event", "same_event"),
    ("UNCERTAIN", "uncertain"), ("", "uncertain"), (None, "uncertain"),
    ("maybe", "uncertain"), ("counted!", "uncertain"),
])
def test_status_words_are_case_insensitive_and_unknown_is_uncertain(raw, expected):
    assert status_of({"count_status": raw}) == expected
    assert status_of({}) == "uncertain"


@pytest.mark.unit
def test_upper_case_status_counts_in_the_fold():
    s = _only(fold_assertions([_row(1, 1, count_status="COUNTED"),
                               _row(2, 2, count_status="bogus")], count_match=True))
    assert s.value == 1 and s.count_uncertain == 1


# ------------------------------------------------------------------ D. service audit

class _Assertions:
    def __init__(self, rows):
        self._rows = rows

    def materializable_assertions_for_entity(self, subject_uuid, *, namespace=None):
        return [r for r in self._rows if r["subject_uuid"] == subject_uuid]


class _Views:
    def __init__(self):
        self.records: list[dict] = []

    def record_scalar_state(self, **kw):
        self.records.append(kw)
        return {"view_key": f"vk-{kw['attribute']}", "uuid": f"view-{kw['attribute']}"}

    def list_scalar_state_views(self, *, subject_uuid, namespace=None):
        return []

    def retire_scalar_state(self, *, view_key):
        return True

    def draw_scalar_state_provenance_edges(self, **kw):
        pass


def _audit_rows():
    return [
        _row(1, 1, count_status="counted", count_action="finished", count_item="Spitfire"),
        _row(2, 2, count_status="counted", count_action="bought", count_item="Hurricane"),
        _row(3, 3, count_status="uncertain"),
    ]


@pytest.mark.unit
def test_service_match_on_writes_audit_keys_on_the_counted_view():
    # Fails if the service does not thread count_match to the fold or drops the audit keys.
    views = _Views()
    ScalarStateService(_Assertions(_audit_rows()), views, count_match=True).rebuild_scalar_state(
        "ent-self")
    rec, = views.records
    assert rec["attribute"] == "model_kit_count" and rec["value"] == 2
    audit = rec["audit"]
    assert audit["scalar_basis"] == "counted"
    assert audit["count_uncertain"] == 1
    assert json.loads(audit["count_actions"]) == {"bought": 1, "finished": 1}
    assert audit["count_actions"] == json.dumps({"bought": 1, "finished": 1})  # sorted keys
    assert json.loads(audit["count_items"]) == ["Spitfire", "Hurricane"]


@pytest.mark.unit
def test_service_match_off_leaves_count_audit_keys_none():
    # Fails if match-off Views carry (or invent) the 4R audit fields.
    views = _Views()
    ScalarStateService(_Assertions(_audit_rows()), views).rebuild_scalar_state("ent-self")
    rec, = views.records
    audit = rec["audit"]
    assert rec["value"] == 3  # off: every occurrence counts, as before
    assert audit["scalar_basis"] == "counted"
    assert audit["count_uncertain"] is None
    assert audit["count_actions"] is None and audit["count_items"] is None


@pytest.mark.unit
def test_service_match_ignored_when_counting_disabled():
    svc = ScalarStateService(_Assertions(_audit_rows()), _Views(), counting_enabled=False,
                             count_match=True)
    assert svc.count_match is False


@pytest.mark.unit
def test_count_match_audit_serializes_decimals_and_none():
    class _S:
        count_uncertain = Decimal("2.5")
        count_actions = {"b": Decimal("1.5"), "a": 2}
        count_items = ["x"]

    out = _count_match_audit(_S())
    assert out["count_uncertain"] == 2.5
    assert out["count_actions"] == json.dumps({"a": 2, "b": "1.5"})
    assert out["count_items"] == '["x"]'

    class _Off:
        count_uncertain = None

    assert _count_match_audit(_Off()) == {
        "count_uncertain": None, "count_actions": None, "count_items": None}
