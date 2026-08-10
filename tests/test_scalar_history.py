"""Scalar history builder + ScalarHistoryKind (Slice 1).

Pure/offline. Proves that materializable assertions produce correct ordered history projections
with deterministic signatures, operation counts, source-time ordering, and bounded payloads.
Also tests the ViewKind's key, signature, surface, write_props, and parse methods.

See `.agent/plans/menhir-scalar-history-projection-plan.md`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from menhir.domain.scalar_history import (
    NO_VALID_ENTRIES,
    HistoryEntry,
    HistoryResult,
    ScalarHistoryProjection,
    build_history,
)
from menhir.infrastructure.view_models import ScalarHistoryKind


# ---- helpers -----------------------------------------------------------------

def _row(**over):
    base = dict(
        assertion_id="a?", subject_uuid="ent-postcards", subject_display="my postcards",
        attribute="postcard_count", scope="collection", value_kind="count", unit="postcards",
        operation="delta", value=17, valid_at="2023-08-11T00:00:00+00:00",
        learned_at="2023-08-11T00:00:00+00:00", evidence_tier="user",
        episode_uuid="ep-1", turn_id="t1", stated_span="17 postcards",
        binding_pending=False,
    )
    base.update(over)
    return base


def _one(rows, **kwargs):
    r = build_history(rows, **kwargs)
    assert len(r.projections) == 1 and not r.abstentions, (r.projections, r.abstentions)
    return r.projections[0]


# ---- ordering ----------------------------------------------------------------

@pytest.mark.unit
def test_entries_ordered_by_valid_at():
    """Two assertions produce entries in chronological source-time order."""
    p = _one([
        _row(assertion_id="a2", value=25, valid_at="2023-11-30T00:00:00+00:00", episode_uuid="ep-2"),
        _row(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00", episode_uuid="ep-1"),
    ])
    assert [e.assertion_id for e in p.entries] == ["a1", "a2"]
    assert [e.value_json for e in p.entries] == [17, 25]


@pytest.mark.unit
def test_tie_break_by_assertion_id():
    """Same valid_at ties are broken by assertion_id."""
    p = _one([
        _row(assertion_id="b", valid_at="2023-08-11T00:00:00+00:00"),
        _row(assertion_id="a", valid_at="2023-08-11T00:00:00+00:00"),
    ])
    assert [e.assertion_id for e in p.entries] == ["a", "b"]


@pytest.mark.unit
def test_neo4j_zone_suffix_orders_correctly():
    """Regression: valid_at with [UTC] suffix must parse correctly for ordering."""
    p = _one([
        _row(assertion_id="old", value=17, valid_at="2023-08-11T00:00:00Z[UTC]"),
        _row(assertion_id="new", value=25, valid_at="2023-11-30T00:00:00Z[UTC]"),
    ])
    assert [e.assertion_id for e in p.entries] == ["old", "new"]


# ---- postcard regression (the motivating case) --------------------------------

@pytest.mark.unit
def test_postcard_delta_only_history():
    """The postcard case: two deltas, no absolute. History is materialized; no computed total."""
    p = _one([
        _row(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00",
             episode_uuid="ep-1", operation="delta"),
        _row(assertion_id="a2", value=25, valid_at="2023-11-30T00:00:00+00:00",
             episode_uuid="ep-2", operation="delta"),
    ])
    assert p.entry_count == 2
    assert p.operation_counts == {"delta": 2}
    assert p.first_valid_at == "2023-08-11T00:00:00+00:00"
    assert p.last_valid_at == "2023-11-30T00:00:00+00:00"
    assert p.entries[-1].value_json == 25  # latest delta, not 42


@pytest.mark.unit
def test_postcard_does_not_sum_deltas():
    """Advisory history must never add deltas merely because they share a slot."""
    p = _one([
        _row(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00", operation="delta"),
        _row(assertion_id="a2", value=25, valid_at="2023-11-30T00:00:00+00:00", operation="delta"),
    ])
    # No entry has value 42
    assert all(e.value_json != 42 for e in p.entries)


@pytest.mark.unit
def test_5k_elapsed_duration_history_keeps_both_source_timed_values():
    """LME 6a1eabeb regression: the newer, faster 25:50 follows 27:12 in source time."""
    p = _one([
        _row(
            assertion_id="5k-old",
            subject_uuid="ent-user",
            subject_display="user",
            attribute="personal_best_5k_time",
            scope="charity_5k",
            value_kind="duration",
            unit="seconds",
            operation="absolute",
            value=1632,
            valid_at="2023-05-25T20:21:00+00:00",
            episode_uuid="ep-5k-old",
            stated_span="personal best time in a charity 5K run with a time of 27:12",
        ),
        _row(
            assertion_id="5k-new",
            subject_uuid="ent-user",
            subject_display="user",
            attribute="personal_best_5k_time",
            scope="charity_5k",
            value_kind="duration",
            unit="seconds",
            operation="absolute",
            value=1550,
            valid_at="2023-05-27T10:20:00+00:00",
            episode_uuid="ep-5k-new",
            stated_span="personal best time of 25:50",
        ),
    ])

    assert [entry.assertion_id for entry in p.entries] == ["5k-old", "5k-new"]
    assert [entry.value_json for entry in p.entries] == [1632, 1550]
    assert [entry.episode_uuid for entry in p.entries] == ["ep-5k-old", "ep-5k-new"]
    assert p.operation_counts == {"absolute": 2}
    assert p.first_valid_at == "2023-05-25T20:21:00+00:00"
    assert p.last_valid_at == "2023-05-27T10:20:00+00:00"


# ---- operation preservation ---------------------------------------------------

@pytest.mark.unit
def test_preserves_all_operation_types():
    """Absolute, delta, correction, expiry operations are all preserved."""
    p = _one([
        _row(assertion_id="a1", operation="absolute", value=20, valid_at="2023-01-01T00:00:00+00:00"),
        _row(assertion_id="a2", operation="delta", value=5, valid_at="2023-06-01T00:00:00+00:00"),
        _row(assertion_id="a3", operation="expire", value=20, valid_at="2023-12-01T00:00:00+00:00"),
    ])
    assert p.operation_counts == {"absolute": 1, "delta": 1, "expire": 1}
    assert [e.operation for e in p.entries] == ["absolute", "delta", "expire"]


# ---- slot keying & namespace isolation ----------------------------------------

@pytest.mark.unit
def test_distinct_slots_produce_separate_projections():
    """Two slots under the same entity produce two projections."""
    r = build_history([
        _row(assertion_id="a1", attribute="postcard_count", value=17),
        _row(assertion_id="a2", attribute="coin_count", value=50),
    ])
    assert len(r.projections) == 2
    attrs = {p.attribute for p in r.projections}
    assert attrs == {"postcard_count", "coin_count"}


@pytest.mark.unit
def test_canonical_slot_key():
    """Slot key is (subject_uuid, attribute, scope, value_kind, unit)."""
    p = _one([_row()])
    assert p.slot_key == ("ent-postcards", "postcard_count", "collection", "count", "postcards")


# ---- signature ---------------------------------------------------------------

@pytest.mark.unit
def test_signature_deterministic():
    """Same assertions in any input order produce the same signature."""
    rows = [
        _row(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00"),
        _row(assertion_id="a2", value=25, valid_at="2023-11-30T00:00:00+00:00"),
    ]
    p1 = _one(rows)
    p2 = _one(list(reversed(rows)))
    assert p1.history_signature == p2.history_signature


@pytest.mark.unit
def test_signature_changes_on_new_assertion():
    """Adding an assertion changes the signature."""
    rows_2 = [
        _row(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00"),
        _row(assertion_id="a2", value=25, valid_at="2023-11-30T00:00:00+00:00"),
    ]
    rows_3 = rows_2 + [
        _row(assertion_id="a3", value=30, valid_at="2024-01-15T00:00:00+00:00"),
    ]
    p2 = _one(rows_2)
    p3 = _one(rows_3)
    assert p2.history_signature != p3.history_signature


# ---- as-of filter -------------------------------------------------------------

@pytest.mark.unit
def test_as_of_excludes_future():
    """Assertions with valid_at > as_of are excluded."""
    rows = [
        _row(assertion_id="a1", value=17, valid_at="2023-08-11T00:00:00+00:00"),
        _row(assertion_id="a2", value=25, valid_at="2023-11-30T00:00:00+00:00"),
    ]
    p = _one(rows, as_of=datetime(2023, 10, 1, tzinfo=timezone.utc))
    assert p.entry_count == 1
    assert p.entries[0].assertion_id == "a1"


# ---- bounded payload ----------------------------------------------------------

@pytest.mark.unit
def test_bounded_entries_keeps_latest():
    """When entry count exceeds max_entries, the latest N are kept."""
    rows = [
        _row(assertion_id=f"a{i}", value=i, valid_at=f"2023-{i+1:02d}-01T00:00:00+00:00")
        for i in range(5)
    ]
    p = _one(rows, max_entries=3)
    assert len(p.entries) == 3
    # Should keep the latest 3
    assert [e.assertion_id for e in p.entries] == ["a2", "a3", "a4"]
    # But signature covers the full set (not bounded)
    p_full = _one(rows, max_entries=100)
    assert p.history_signature == p_full.history_signature


@pytest.mark.unit
def test_twenty_entries_keep_full_count_and_latest_sixteen_payload():
    rows = [
        _row(
            assertion_id=f"a{i:02d}", value=i,
            valid_at=f"2023-{(i // 2) + 1:02d}-{(i % 2) + 1:02d}T00:00:00+00:00",
        )
        for i in range(20)
    ]
    p = _one(rows)
    assert p.entry_count == 20
    assert p.payload_entry_count == 16
    assert p.omitted_entry_count == 4
    assert [e.assertion_id for e in p.entries] == [f"a{i:02d}" for i in range(4, 20)]
    assert [e.assertion_id for e in p.all_entries] == [f"a{i:02d}" for i in range(20)]


# ---- episode provenance -------------------------------------------------------

@pytest.mark.unit
def test_episode_uuids_deduped_order_stable():
    """Episode UUIDs are deduped and order-stable by first appearance."""
    p = _one([
        _row(assertion_id="a1", episode_uuid="ep-1", valid_at="2023-01-01T00:00:00+00:00"),
        _row(assertion_id="a2", episode_uuid="ep-2", valid_at="2023-06-01T00:00:00+00:00"),
        _row(assertion_id="a3", episode_uuid="ep-1", valid_at="2023-12-01T00:00:00+00:00"),
    ])
    assert p.episode_uuids == ["ep-1", "ep-2"]


# ---- abstention ---------------------------------------------------------------

@pytest.mark.unit
def test_malformed_valid_at_produces_abstention():
    """A slot where ALL assertions have malformed valid_at abstains."""
    r = build_history([
        _row(assertion_id="a1", valid_at=None),
        _row(assertion_id="a2", valid_at=""),
    ])
    assert len(r.abstentions) == 1
    assert r.abstentions[0].reason == "malformed_valid_at"
    assert r.abstentions[0].malformed_assertion_ids == ("a1", "a2")
    assert len(r.projections) == 0


@pytest.mark.unit
def test_partial_malformed_valid_at_abstains_whole_slot_with_offending_ids():
    """One malformed current member prevents a silently partial projection."""
    r = build_history([
        _row(assertion_id="a1", valid_at="2023-08-11T00:00:00+00:00"),
        _row(assertion_id="a2", valid_at=None),
    ])
    assert r.projections == []
    assert len(r.abstentions) == 1
    assert r.abstentions[0].reason == "malformed_valid_at"
    assert r.abstentions[0].malformed_assertion_ids == ("a2",)


# ---- empty input --------------------------------------------------------------

@pytest.mark.unit
def test_empty_rows_produces_empty_result():
    r = build_history([])
    assert r.projections == [] and r.abstentions == []


# ---- entry fields preserved ---------------------------------------------------

@pytest.mark.unit
def test_entry_preserves_all_fields():
    """HistoryEntry carries assertion_id, operation, value, value_json, valid_at,
    episode_uuid, evidence_tier, stated_span."""
    p = _one([_row(
        assertion_id="a1", operation="delta", value=17,
        valid_at="2023-08-11T00:00:00+00:00", episode_uuid="ep-1",
        evidence_tier="user", stated_span="17 postcards since I started collecting",
    )])
    e = p.entries[0]
    assert e.assertion_id == "a1"
    assert e.operation == "delta"
    assert e.value_json == 17
    assert e.valid_at == "2023-08-11T00:00:00+00:00"
    assert e.episode_uuid == "ep-1"
    assert e.evidence_tier == "user"
    assert e.stated_span == "17 postcards since I started collecting"


# ========================================================================
# ScalarHistoryKind ViewKind tests
# ========================================================================

_KIND = ScalarHistoryKind()


@pytest.mark.unit
def test_kind_name():
    assert _KIND.name == "scalar_history"


@pytest.mark.unit
def test_kind_not_lww():
    assert _KIND.lww_register is False


@pytest.mark.unit
def test_kind_key_discriminator_prefix():
    disc = _KIND.key_discriminator({
        "attribute": "postcard_count", "scope": "collection",
        "value_kind": "count", "unit": "postcards",
    })
    assert disc.startswith("sh_")


@pytest.mark.unit
def test_kind_key_distinct_from_scalar_state():
    """scalar_history and scalar_state keys must not collide for the same slot."""
    from menhir.infrastructure.view_models import ScalarStateKind
    payload = {"attribute": "postcard_count", "scope": "collection",
               "value_kind": "count", "unit": "postcards"}
    sh_key = ScalarHistoryKind().key_discriminator(payload)
    ss_key = ScalarStateKind().key_discriminator(payload)
    assert sh_key != ss_key
    assert sh_key.startswith("sh_")
    assert ss_key.startswith("ss_")


@pytest.mark.unit
def test_kind_rejects_empty_attribute():
    with pytest.raises(ValueError, match="non-empty attribute"):
        _KIND.key_discriminator({"attribute": "", "value_kind": "count"})


@pytest.mark.unit
def test_kind_rejects_unknown_value_kind():
    with pytest.raises(ValueError, match="not in"):
        _KIND.key_discriminator({"attribute": "x", "value_kind": "banana"})


@pytest.mark.unit
def test_kind_signature_from_history_signature():
    payload = {"history_signature": "abc123"}
    assert _KIND.signature(payload) == "abc123"


@pytest.mark.unit
def test_kind_surface_advisory_label():
    """The surface name must include the advisory disclaimer."""
    name, summary = _KIND.surface("my postcards", {
        "attribute": "postcard_count", "scope": "collection",
        "entry_count": 2, "operation_counts": {"delta": 2},
        "first_valid_at": "2023-08-11T00:00:00+00:00",
        "last_valid_at": "2023-11-30T00:00:00+00:00",
    })
    assert "advisory" in name.lower()
    assert "not an absolute current total" in name.lower()
    assert "history" in summary.lower()


@pytest.mark.unit
def test_kind_write_props_slot_fields():
    """write_props stores ss_attribute/scope/kind/unit and sh_* props."""
    props = _KIND.write_props("my postcards", "test-key", {
        "attribute": "postcard_count", "scope": "collection",
        "value_kind": "count", "unit": "postcards",
        "entries": [], "entry_count": 2, "history_entry_count": 2,
        "payload_entry_count": 0, "omitted_entry_count": 2,
        "history_signature": "abc123",
        "operation_counts": {"delta": 2},
        "first_valid_at": "2023-08-11", "last_valid_at": "2023-11-30",
    })
    assert props["ss_attribute"] == "postcard_count"
    assert props["ss_scope"] == "collection"
    assert props["ss_kind"] == "count"
    assert props["ss_unit"] == "postcards"
    assert props["sh_entry_count"] == 2
    assert props["sh_payload_entry_count"] == 0
    assert props["sh_omitted_entry_count"] == 2
    assert props["sh_signature"] == "abc123"
    assert props["sh_first_valid_at"] == "2023-08-11"
    assert props["sh_last_valid_at"] == "2023-11-30"


@pytest.mark.unit
def test_kind_parse_roundtrip():
    """parse() correctly maps read_fields output back to a dict."""
    row = {
        "uuid": "u1", "subject": "my postcards", "subject_uuid": "ent-1",
        "attribute": "postcard_count", "scope": "collection",
        "value_kind": "count", "unit": "postcards",
        "entry_count": 2, "history_signature": "abc123",
        "operation_counts": '{"delta": 2}',
        "first_valid_at": "2023-08-11", "last_valid_at": "2023-11-30",
        "payload": '[{"assertion_id": "a1", "operation": "delta", "value": "17"}]',
        "valid_at": "2023-11-30",
    }
    parsed = _KIND.parse(row)
    assert parsed["uuid"] == "u1"
    assert parsed["operation_counts"] == {"delta": 2}
    assert len(parsed["entries"]) == 1
    assert parsed["entries"][0]["assertion_id"] == "a1"


@pytest.mark.unit
def test_kind_valid_at_uses_last():
    """valid_at returns last_valid_at for View ordering."""
    assert _KIND.valid_at({"last_valid_at": "2023-11-30"}) == "2023-11-30"
    assert _KIND.valid_at({"valid_at": "2023-08-11"}) == "2023-08-11"
    assert _KIND.valid_at({"last_valid_at": "2023-11-30", "valid_at": "2023-08-11"}) == "2023-11-30"
