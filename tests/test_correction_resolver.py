"""F2 — anaphoric numeric correction binding (see .agent/plans, perception-consolidation wiring).

A bare correction ("Actually it is 20, not 25.") must supersede the UNIQUE current View whose value
is the corrected-from number, in the same namespace, and touch nothing else. These use a fake graph
adapter (no live model / Neo4j); the write path is `fold_events_to_counter` -> `record_counter`.
"""

from __future__ import annotations

import pytest

from menhir.services.correction_resolver import detect_correction, resolve_corrections


class FakeGraph:
    """current counter Views + a record_counter that simulates LWW supersession (updates current)."""

    def __init__(self, views):
        self.views = [dict(v) for v in views]
        self.writes = []

    def list_counters(self, *, namespace=None, limit=200):
        return [dict(v) for v in self.views]

    def record_counter(self, *, subject, counter, value, namespace=None, valid_at=None,
                       episode_uuids=None, source="", name_embedding=None, audit=None):
        self.writes.append({"subject": subject, "counter": counter, "value": float(value),
                            "valid_at": valid_at, "audit": audit})
        for v in self.views:
            if v["subject"] == subject and v["counter"] == counter:
                v["value"] = float(value)
                break
        else:
            self.views.append({"subject": subject, "counter": counter, "value": float(value),
                               "valid_at": valid_at})
        return {"uuid": "u", "view_key": f"{subject}:{counter}", "created": True, "value": float(value)}


def _rows(*texts):
    return [{"uuid": f"c{i}", "valid_at": "2026-07-07T15:00:00Z", "content": t}
            for i, t in enumerate(texts)]


# ----------------------------------------------------------------------------- detection


@pytest.mark.unit
@pytest.mark.parametrize("text,expected", [
    ("Actually it is 20, not 25.", (25.0, 20.0)),
    ("it's 20 not 25", (25.0, 20.0)),
    ("not 25, it's 20", (25.0, 20.0)),
    ("changed from 25 to 20", (25.0, 20.0)),
    ("20 instead of 25", (25.0, 20.0)),
    ("Actually it is 19.5, not 25", (25.0, 19.5)),
    # negative-correction: cessation filler between OLD and the connective
    ("Not 25 anymore, it is 20.", (25.0, 20.0)),
    ("not 25 anymore, it's 20", (25.0, 20.0)),
    ("not 25 any longer, now it is 20", (25.0, 20.0)),
    # non-corrections
    ("I have 25 movies on my watch list.", None),
    ("I bought one bike for 50 and another for 75.", None),
    ("write the handoff", None),
    ("it is 25, not 25", None),                    # no-op: old == new
    ("I don't watch 25 movies anymore.", None),    # "anymore" without a NEW value -> not a correction
])
def test_detect_correction(text, expected):
    assert detect_correction(text) == expected


# ----------------------------------------------------------------------------- binding


@pytest.mark.unit
def test_binds_to_unique_value_matching_view_and_leaves_others_untouched():
    g = FakeGraph([
        {"subject": "user", "counter": "movies", "value": 25.0, "valid_at": "2026-07-07T00:00:00Z"},
        {"subject": "user", "counter": "bike_spend", "value": 125.0, "valid_at": "2026-07-07T00:00:00Z"},
    ])
    out = resolve_corrections(_rows("Actually it is 20, not 25."), g, namespace="ns")

    assert out["corrections_applied"] == 1 and out["corrections_ambiguous"] == 0
    assert any(w["counter"] == "movies" and w["value"] == 20.0 for w in g.writes)
    assert not any(w["counter"] == "bike_spend" for w in g.writes)          # unrelated View untouched
    movies = next(v for v in g.views if v["counter"] == "movies")
    assert movies["value"] == 20.0                                          # 25 -> 20 current


@pytest.mark.unit
def test_negative_correction_form_binds_and_supersedes():
    # "Not 25 anymore, it is 20" must supersede the unique 25-valued View, same as the bare form.
    g = FakeGraph([
        {"subject": "user", "counter": "movies", "value": 25.0, "valid_at": "2026-07-07T00:00:00Z"},
        {"subject": "user", "counter": "bike_spend", "value": 125.0, "valid_at": "2026-07-07T00:00:00Z"},
    ])
    out = resolve_corrections(_rows("Not 25 anymore, it is 20."), g, namespace="ns")

    assert out["corrections_applied"] == 1 and out["corrections_ambiguous"] == 0
    assert any(w["counter"] == "movies" and w["value"] == 20.0 for w in g.writes)
    assert not any(w["counter"] == "bike_spend" for w in g.writes)          # unrelated View untouched
    assert next(v for v in g.views if v["counter"] == "movies")["value"] == 20.0


@pytest.mark.unit
def test_negative_correction_ambiguous_still_abstains():
    # Two 25-valued Views -> even the negative form must abstain (precision-first, no wrong write).
    g = FakeGraph([
        {"subject": "user", "counter": "movies", "value": 25.0, "valid_at": "2026-07-07T00:00:00Z"},
        {"subject": "user", "counter": "books", "value": 25.0, "valid_at": "2026-07-07T00:00:00Z"},
    ])
    out = resolve_corrections(_rows("Not 25 anymore, it is 20."), g, namespace="ns")
    assert out["corrections_applied"] == 0 and out["corrections_ambiguous"] == 1
    assert all(v["value"] == 25.0 for v in g.views if v["counter"] in ("movies", "books"))


@pytest.mark.unit
def test_ambiguous_when_two_views_share_the_old_value_abstains():
    g = FakeGraph([
        {"subject": "user", "counter": "movies", "value": 25.0, "valid_at": "2026-07-07T00:00:00Z"},
        {"subject": "user", "counter": "books", "value": 25.0, "valid_at": "2026-07-07T00:00:00Z"},
    ])
    out = resolve_corrections(_rows("Actually it is 20, not 25."), g, namespace="ns")

    assert out["corrections_applied"] == 0 and out["corrections_ambiguous"] == 1
    assert g.writes == [] or all(w["subject"] == "perception" for w in g.writes)  # only receipts, no View change
    assert all(v["value"] == 25.0 for v in g.views if v["counter"] in ("movies", "books"))


@pytest.mark.unit
def test_no_target_when_no_view_matches_old_value():
    g = FakeGraph([{"subject": "user", "counter": "movies", "value": 30.0, "valid_at": "2026-07-07T00:00:00Z"}])
    out = resolve_corrections(_rows("Actually it is 20, not 25."), g, namespace="ns")

    assert out["corrections_applied"] == 0 and out["corrections_no_target"] == 1
    assert not any(w["counter"] == "movies" for w in g.writes)


@pytest.mark.unit
def test_idempotent_second_pass_is_a_noop():
    g = FakeGraph([{"subject": "user", "counter": "movies", "value": 25.0, "valid_at": "2026-07-07T00:00:00Z"}])
    rows = _rows("Actually it is 20, not 25.")

    first = resolve_corrections(rows, g, namespace="ns")
    assert first["corrections_applied"] == 1
    writes_after_first = len([w for w in g.writes if w["subject"] == "user"])

    second = resolve_corrections(rows, g, namespace="ns")  # movies is now 20; no 25-valued View
    assert second["corrections_applied"] == 0 and second["corrections_no_target"] == 1
    assert len([w for w in g.writes if w["subject"] == "user"]) == writes_after_first  # no new View write


@pytest.mark.unit
def test_perception_receipt_views_are_not_correction_targets():
    # a run-level receipt happens to hold value 25; a correction must never bind to it
    g = FakeGraph([
        {"subject": "perception", "counter": "perception_abstained", "value": 25.0, "valid_at": "x"},
        {"subject": "user", "counter": "movies", "value": 25.0, "valid_at": "2026-07-07T00:00:00Z"},
    ])
    out = resolve_corrections(_rows("Actually it is 20, not 25."), g, namespace="ns")
    assert out["corrections_applied"] == 1                                  # bound to user movies, not the receipt
    assert any(w["counter"] == "movies" and w["value"] == 20.0 for w in g.writes)
