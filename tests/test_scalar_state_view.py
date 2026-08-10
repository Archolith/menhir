"""ScalarStateView (Pieces A + B): the entity-linked typed-scalar register ViewKind and the
`subject_uuid` keying contract. Offline unit tests on the kind + the `_key` static, mirroring
`test_view_repository_lww.py` (no Neo4j). Guards: registration, canonical/collision-safe key,
value-change supersession, display-not-UUID surface, heterogeneous value normalization, and the
backward-compatible + fail-closed `subject_uuid` keying."""

from __future__ import annotations

import pytest

from menhir.infrastructure.view_repository import (
    ScalarStateKind,
    ViewRepository,
    _scalar_norm,
)


# --- registration + register semantics ---------------------------------------------

@pytest.mark.unit
def test_scalar_state_kind_registered_and_is_register():
    assert "scalar_state" in ViewRepository.KINDS
    assert isinstance(ViewRepository.KINDS["scalar_state"], ScalarStateKind)
    # a current-value register -> newer valid_at wins (fold-algebra Law 1), like counter.
    assert ScalarStateKind.lww_register is True


# --- value normalization (heterogeneous ValueKinds) --------------------------------

@pytest.mark.unit
def test_scalar_norm_handles_all_value_shapes():
    assert _scalar_norm(True) == "true"
    assert _scalar_norm(False) == "false"
    assert _scalar_norm(38) == "38"
    assert _scalar_norm(38.0) == "38"          # integral float -> no decimal
    assert _scalar_norm(1.5) == "1.5"
    assert _scalar_norm([10, 12]) == "10-12"   # a duration/measurement range
    assert _scalar_norm("07:30") == "07:30"    # clock_time
    assert _scalar_norm("saturday") == "saturday"
    assert _scalar_norm("  finished ") == "finished"


def test_scalar_norm_collapses_degenerate_range_only():
    """Mirror of domain.typed_assertion.normalize_scalar: a degenerate range [N, N] is the point N.
    Both sides must agree or the assertion and the View disagree about the same value."""
    assert _scalar_norm([1300, 1300]) == "1300" == _scalar_norm(1300)
    assert _scalar_norm([38.0, 38]) == "38" == _scalar_norm(38)
    assert _scalar_norm([1300, "1300"]) == "1300"
    assert _scalar_norm([10, 12]) == "10-12"          # genuine range preserved
    assert _scalar_norm(["07:30", "07:30"]) == "07:30"  # degenerate clock_time pair
    assert _scalar_norm([7, 7, 7]) == "7-7-7"         # not range-shaped


# --- key discriminator: canonical, collision-safe, slot-isolating ------------------

@pytest.mark.unit
def test_key_discriminator_isolates_distinct_slots():
    kind = ScalarStateKind()
    mcu = {"attribute": "watched", "scope": "mcu", "value_kind": "count", "unit": "", "value": 5}
    allf = {"attribute": "watched", "scope": "", "value_kind": "count", "unit": "", "value": 12}
    owned = {"attribute": "owned", "scope": "", "value_kind": "count", "unit": "", "value": 38}
    # MCU vs all-films (scope), owned vs watched (attribute) must never share a slot.
    assert kind.key_discriminator(mcu) != kind.key_discriminator(allf)
    assert kind.key_discriminator(allf) != kind.key_discriminator(owned)


@pytest.mark.unit
def test_key_discriminator_is_stable_and_hashed():
    kind = ScalarStateKind()
    p = {"attribute": "wake", "scope": "saturday", "value_kind": "clock_time", "unit": "", "value": "07:30"}
    d1 = kind.key_discriminator(p)
    d2 = kind.key_discriminator({**p, "value": "08:30"})  # value not part of the slot
    assert d1 == d2                 # discriminator keys on the SLOT, not the value
    assert d1.startswith("ss_")     # hashed, not raw colon concat


@pytest.mark.unit
def test_key_discriminator_collision_safe_on_colon():
    # A scope containing ':' must not collide with a different (attribute, scope) via naive concat.
    kind = ScalarStateKind()
    a = {"attribute": "a", "scope": "b:c", "value_kind": "count", "unit": "", "value": 1}
    b = {"attribute": "a:b", "scope": "c", "value_kind": "count", "unit": "", "value": 1}
    assert kind.key_discriminator(a) != kind.key_discriminator(b)


# --- signature: supersede on value change ------------------------------------------

@pytest.mark.unit
def test_signature_changes_with_value_only():
    kind = ScalarStateKind()
    base = {"attribute": "owned", "scope": "", "value_kind": "count", "unit": "", "value": 37}
    assert kind.signature(base) == kind.signature({**base, "attribute": "x"})  # sig = value only
    assert kind.signature(base) != kind.signature({**base, "value": 38})       # value change -> supersede


# --- surface uses DISPLAY, never the UUID ------------------------------------------

@pytest.mark.unit
def test_surface_uses_display_subject_and_value_not_uuid():
    kind = ScalarStateKind()
    payload = {"attribute": "wake", "scope": "saturday", "value_kind": "clock_time",
               "unit": "", "value": "07:30", "display": "7:30 am",
               "valid_at": "2026-07-18T00:00:00+00:00"}
    name, summary = kind.surface("my wake-up", payload)
    assert "my wake-up" in name and "7:30 am" in name
    assert "wake" in summary and "saturday" in summary
    # a UUID (if it were ever passed as subject) is not what surface renders — display drives it.


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1550, "25:50"),
        (1550.5, "25:50.5"),
        (3723, "1:02:03"),
        (42, "0:42"),
        (-82, "-1:22"),
        ([1550, 1632], "25:50–27:12"),
        ([1550, 1550], "25:50"),
    ],
)
def test_duration_seconds_get_human_elapsed_display_without_changing_value(value, expected):
    kind = ScalarStateKind()
    payload = {
        "attribute": "personal_best_time",
        "scope": "",
        "value_kind": "duration",
        "unit": "seconds",
        "value": value,
        "valid_at": "2023-05-27T10:20:00Z",
    }

    props = kind.write_props("user", "key", payload)
    name, summary = kind.surface("user", payload)

    assert props["ss_value"] == _scalar_norm(value)
    assert props["ss_unit"] == "seconds"
    assert props["ss_display"] == expected
    assert expected in name and expected in summary


@pytest.mark.unit
def test_explicit_scalar_display_still_overrides_default_duration_format():
    kind = ScalarStateKind()
    payload = {
        "attribute": "personal_best_time",
        "scope": "",
        "value_kind": "duration",
        "unit": "seconds",
        "value": 1550,
        "display": "25 minutes, 50 seconds",
    }

    props = kind.write_props("user", "key", payload)
    name, _ = kind.surface("user", payload)

    assert props["ss_value"] == "1550"
    assert props["ss_display"] == "25 minutes, 50 seconds"
    assert "25 minutes, 50 seconds" in name


# --- write_props: slot components kept; numeric compat mirror ----------------------

@pytest.mark.unit
def test_write_props_keeps_slot_components_and_numeric_mirror():
    kind = ScalarStateKind()
    props = kind.write_props("s", "k", {
        "attribute": "Owned", "scope": "Pre-1920", "value_kind": "count", "unit": "",
        "value": 38, "display": "38 coins"})
    assert props["ss_value"] == "38" and props["ss_display"] == "38 coins"
    assert props["ss_attribute"] == "owned" and props["ss_scope"] == "pre-1920"
    assert props["ss_kind"] == "count"
    assert props["view_value"] == 38.0  # numeric compat mirror


@pytest.mark.unit
def test_write_props_non_numeric_value_mirror_is_zero():
    kind = ScalarStateKind()
    props = kind.write_props("s", "k", {
        "attribute": "wake", "scope": "sat", "value_kind": "clock_time", "unit": "",
        "value": "07:30"})
    assert props["ss_value"] == "07:30"
    assert props["view_value"] == 0.0  # non-numeric -> mirror 0.0, register content is ss_value


@pytest.mark.unit
def test_parse_roundtrip():
    kind = ScalarStateKind()
    row = {"uuid": "n-1", "subject": "coins", "subject_uuid": "ent-abc",
           "attribute": "owned", "scope": "pre-1920", "value_kind": "count", "unit": "",
           "value": "38", "display": "38 coins", "valid_at": "2026-07-18T00:00:00+00:00"}
    parsed = kind.parse(row)
    assert parsed["subject_uuid"] == "ent-abc" and parsed["value"] == "38"
    assert parsed["attribute"] == "owned"


# --- Piece B: subject_uuid keying (backward-compatible + fail-closed) ---------------

@pytest.mark.unit
def test_key_falls_back_to_text_subject_when_no_uuid():
    # existing kinds pass no subject_uuid -> byte-identical legacy key.
    legacy = ViewRepository._key("ns", "The User", "counter")
    assert legacy == "ns::the user::counter"


@pytest.mark.unit
def test_key_uses_uuid_segment_when_provided():
    keyed = ViewRepository._key("ns", "The User", "ss_abc123", subject_uuid="Ent-UUID-1")
    assert keyed == "ns::ent-uuid-1::ss_abc123"        # UUID drives identity, not the text
    text = ViewRepository._key("ns", "The User", "ss_abc123")
    assert keyed != text                                # entity-anchored, not text-anchored


@pytest.mark.unit
def test_key_rejects_blank_subject_uuid():
    # a present-but-blank UUID must raise, never silently fall back to text keying.
    with pytest.raises(ValueError):
        ViewRepository._key("ns", "The User", "ss_abc", subject_uuid="   ")


# --- fail-closed slot validation (over-merge guard) --------------------------------

@pytest.mark.unit
def test_slot_rejects_empty_attribute_and_unknown_value_kind():
    kind = ScalarStateKind()
    with pytest.raises(ValueError):  # empty attribute would collapse unrelated values
        kind.key_discriminator({"attribute": "", "value_kind": "count", "value": 1})
    with pytest.raises(ValueError):  # unknown value_kind is not a typed scalar family
        kind.key_discriminator({"attribute": "owned", "value_kind": "bogus", "value": 1})
    # a valid slot with blank scope + unit stays legal (unscoped/unitless attribute series).
    ok = kind.key_discriminator(
        {"attribute": "owned", "scope": "", "value_kind": "count", "unit": "", "value": 1})
    assert ok.startswith("ss_")


# --- Piece B: full record() write path (fake repo, no live Neo4j) ------------------

class _FakeNeo4j:
    """Captures execute() calls; returns no current version so a write takes the CREATE path."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query: str, params: dict | None = None):
        self.calls.append((query, params or {}))
        return []  # _current_by_key -> None -> CREATE

    def create_extra(self) -> dict:
        """The $extra dict from the CREATE statement (carries view_key + any view_subject_uuid)."""
        for q, p in self.calls:
            if "CREATE (n:" in q and "extra" in p:
                return p["extra"]
        raise AssertionError("no CREATE call was captured")


@pytest.mark.unit
def test_record_scalar_state_places_uuid_in_key_and_extra():
    repo = ViewRepository(_FakeNeo4j())
    res = repo.record(
        "scalar_state", subject="my coin collection", subject_uuid="ent-coins-1",
        namespace="ns", attribute="owned", scope="pre-1920", value_kind="count",
        unit="", value=38, display="38 coins",
    )
    extra = repo.neo4j.create_extra()
    assert extra["view_subject_uuid"] == "ent-coins-1"       # UUID stamped on the node
    assert extra["view_subject"] == "my coin collection"     # display preserved, not the UUID
    assert "ent-coins-1" in extra["view_key"]                # UUID drives the key
    assert res["view_key"].split("::")[1] == "ent-coins-1"   # identity segment IS the UUID


@pytest.mark.unit
def test_record_legacy_counter_has_no_subject_uuid_and_text_key():
    repo = ViewRepository(_FakeNeo4j())
    repo.record("counter", subject="The User", namespace="ns", counter="playlists", value=20)
    extra = repo.neo4j.create_extra()
    assert "view_subject_uuid" not in extra                  # legacy write: no UUID prop at all
    assert extra["view_key"] == "ns::the user::playlists"    # text-anchored key unchanged


@pytest.mark.unit
def test_record_scalar_state_wrapper_anchors_on_uuid_and_keeps_display():
    # The ergonomic wrapper (C.2) drives the entity-anchored write end to end.
    repo = ViewRepository(_FakeNeo4j())
    res = repo.record_scalar_state(
        subject="my coins", subject_uuid="ent-coins", attribute="owned", scope="pre-1920",
        value_kind="count", unit="", value=38, display="38 coins", namespace="ns")
    extra = repo.neo4j.create_extra()
    assert extra["view_subject_uuid"] == "ent-coins"         # identity anchored on the entity UUID
    assert extra["view_subject"] == "my coins"               # display preserved
    assert "ent-coins" in extra["view_key"]
    assert extra["ss_value"] == "38" and extra["ss_display"] == "38 coins"
    assert "ent-coins" in res["view_key"]


@pytest.mark.unit
def test_record_scalar_state_persists_canonical_duration_with_elapsed_display():
    repo = ViewRepository(_FakeNeo4j())

    repo.record_scalar_state(
        subject="user",
        subject_uuid="ent-user",
        attribute="personal_best_time",
        scope="",
        value_kind="duration",
        unit="seconds",
        value=1550,
        display=None,
        valid_at="2023-05-27T10:20:00Z",
        namespace="ns",
    )

    extra = repo.neo4j.create_extra()
    create_params = next(params for query, params in repo.neo4j.calls if "CREATE (n:" in query)
    assert extra["ss_value"] == "1550"
    assert extra["ss_unit"] == "seconds"
    assert extra["ss_display"] == "25:50"
    assert "personal best time: 25:50" in create_params["name"]


# --- C.2 same-value authority refresh + slot retirement ----------------------------

class _UnchangedSigNeo4j:
    """Simulates an existing current version with the SAME signature, so a write hits the
    unchanged-value refresh path (no new CREATE). Captures the $refresh dict."""

    def __init__(self, sig: str) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._sig = sig

    def execute(self, query: str, params: dict | None = None):
        self.calls.append((query, params or {}))
        # _current_by_key: report an existing current version with a matching signature.
        if "coalesce(n.view_current, n.qs_current, true)" in query:
            return [{"uuid": "cur-1", "sig": self._sig, "valid_at": "2026-07-01T00:00:00+00:00"}]
        return []

    def refresh_dict(self):
        for q, p in self.calls:
            if "n += $refresh" in q:
                return p.get("refresh")
        raise AssertionError("no unchanged-value refresh call captured")

    def replace_call(self):
        for q, p in self.calls:
            if "n.episode_uuids = $eps" in q:
                return q, p
        raise AssertionError("no provenance-replace call captured")


@pytest.mark.unit
def test_same_signature_fold_fully_refreshes_projection():
    # Identical value AND valid_at (same signature), but contributors/tier/display changed. The
    # authoritative refresh path must stamp the fresh audit receipt AND the full derived surface
    # (view_subject, name, summary, ss_*), so nothing on the node goes stale (review C.2).
    kind = ScalarStateKind()
    valid_at = "2026-07-01T00:00:00+00:00"
    sig = kind.signature({"value": 38, "valid_at": valid_at})
    fake = _UnchangedSigNeo4j(sig)
    repo = ViewRepository(fake)
    repo.record_scalar_state(
        subject="coin collection", subject_uuid="ent-coins", attribute="owned", scope="pre-1920",
        value_kind="count", unit="", value=38, display="38 coins", valid_at=valid_at, namespace="ns",
        audit={"scalar_effective_tier": "agent", "scalar_contributors": ["anc", "d1"]})
    refresh = fake.refresh_dict()
    assert refresh["scalar_effective_tier"] == "agent"          # authority snapshot refreshed
    assert refresh["scalar_contributors"] == ["anc", "d1"]
    assert refresh["view_subject"] == "coin collection"         # surface fields refreshed too
    assert refresh["ss_display"] == "38 coins"
    assert "coin collection" in refresh["name"]


@pytest.mark.unit
def test_same_signature_replaces_provenance_not_union():
    # Existing same-value/same-valid_at View references ep-old; the rebuild's contributors are now
    # exactly [ep-new]. The unchanged-signature path must REPLACE provenance (not union): set
    # episode_uuids=[ep-new], supporting_event_count=1, prune ep-old MENTIONS, merge ep-new.
    kind = ScalarStateKind()
    valid_at = "2026-07-01T00:00:00+00:00"
    fake = _UnchangedSigNeo4j(kind.signature({"value": 38, "valid_at": valid_at}))
    repo = ViewRepository(fake)
    repo.record_scalar_state(
        subject="my coins", subject_uuid="ent-coins", attribute="owned", scope="",
        value_kind="count", unit="", value=38, valid_at=valid_at, namespace="ns",
        episode_uuids=["ep-new"],
        audit={"scalar_effective_tier": "user", "scalar_contributors": ["a-new"]})
    q, p = fake.replace_call()
    assert p["eps"] == ["ep-new"]                                    # exact replacement set
    assert "n.supporting_event_count = size($eps)" in q             # count = exact length
    assert "WHERE NOT old.uuid IN $eps" in q and "DELETE m" in q    # prune stale MENTIONS (ep-old)
    assert "MERGE (e)-[:MENTIONS]->(n)" in q                        # link the new set
    assert p["refresh"]["scalar_contributors"] == ["a-new"]         # refreshed contributor snapshot
    # union behavior must NOT appear on this path
    assert "coalesce(n.episode_uuids, [])" not in q


class _LwwFakeNeo4j:
    """Simulates an existing current version (for the LWW/authoritative-rebuild path). Routes
    _current_by_key to the given current row; every other query returns []."""

    def __init__(self, current: dict | None) -> None:
        self.current = current
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query: str, params: dict | None = None):
        self.calls.append((query, params or {}))
        if "coalesce(n.view_current, n.qs_current, true)" in query:  # _current_by_key
            return [self.current] if self.current else []
        return []

    def created(self) -> bool:
        return any("CREATE (n:" in q for q, _ in self.calls)

    def create_extra(self) -> dict:
        for q, p in self.calls:
            if "CREATE (n:" in q and "extra" in p:
                return p["extra"]
        raise AssertionError("no CREATE captured")

    def create_param(self, key: str):
        for q, p in self.calls:
            if "CREATE (n:" in q:
                return p.get(key)
        raise AssertionError("no CREATE captured")


@pytest.mark.unit
def test_authoritative_rebuild_bypasses_lww_on_backward_correction():
    # Existing owned=40 @ August. A correction moved the August claim to `sold`, so the current log
    # folds owned back to 37 @ July. Authoritative rebuild MUST install 37 despite the earlier
    # valid_at (rebuild is a full replacement, not a late incremental event).
    current = {"uuid": "old", "sig": "40|2026-08-01T00:00:00+00:00",
               "valid_at": "2026-08-01T00:00:00+00:00"}
    repo = ViewRepository(_LwwFakeNeo4j(current))
    res = repo.record_scalar_state(
        subject="my coins", subject_uuid="ent-coins", attribute="owned", scope="",
        value_kind="count", unit="", value=37, valid_at="2026-07-01T00:00:00+00:00",
        audit={"scalar_effective_tier": "user", "scalar_contributors": ["a37"]})
    assert repo.neo4j.created() is True          # new version installed, not stale-skipped
    assert not res.get("stale_skipped")
    assert repo.neo4j.create_extra()["ss_value"] == "37"


@pytest.mark.unit
def test_incremental_write_still_honors_lww():
    # Contrast: an ORDINARY (non-authoritative) counter write with an older valid_at IS rejected —
    # LWW still protects incremental writes from late arrivals.
    current = {"uuid": "old", "sig": "40", "valid_at": "2026-08-01T00:00:00+00:00"}
    repo = ViewRepository(_LwwFakeNeo4j(current))
    res = repo.record("counter", subject="s", counter="coins", value=37,
                      valid_at="2026-07-01T00:00:00+00:00")
    assert res.get("stale_skipped") is True
    assert repo.neo4j.created() is False


@pytest.mark.unit
def test_same_value_new_anchor_reversions_with_fresh_surface():
    # Same value (38) but the anchor moved July -> August (different valid_at) -> different signature
    # -> a NEW version with the August surface, not a refresh of the stale July View.
    current = {"uuid": "old", "sig": "38|2026-07-01T00:00:00+00:00",
               "valid_at": "2026-07-01T00:00:00+00:00"}
    repo = ViewRepository(_LwwFakeNeo4j(current))
    repo.record_scalar_state(
        subject="coin collection", subject_uuid="ent-coins", attribute="owned", scope="",
        value_kind="count", unit="", value=38, display="38 coins",
        valid_at="2026-08-01T00:00:00+00:00",
        audit={"scalar_effective_tier": "user", "scalar_contributors": ["aAug"]})
    assert repo.neo4j.created() is True                       # re-versioned, not a stale refresh
    assert repo.neo4j.create_extra()["view_subject"] == "coin collection"
    assert "2026-08-01" in str(repo.neo4j.create_param("valid_at"))


@pytest.mark.unit
def test_list_and_retire_scalar_state_queries():
    class _Cap:
        def __init__(self, rows):
            self.rows = rows
            self.calls = []

        def execute(self, q, params=None):
            self.calls.append((q, params or {}))
            return self.rows

    repo = ViewRepository(_Cap([{"uuid": "n", "view_key": "vk", "attribute": "owned",
                                 "scope": "", "value_kind": "count", "unit": ""}]))
    views = repo.list_scalar_state_views(subject_uuid="ent-coins", namespace="ns")
    q = repo.neo4j.calls[-1][0]
    assert "view_kind:'scalar_state'" in q and "view_subject_uuid:$u" in q
    assert "n.group_id AS namespace" in q          # projects the filtered silo so callers can assert it
    assert views[0]["attribute"] == "owned"

    repo2 = ViewRepository(_Cap([{"retired": 1}]))
    assert repo2.retire_scalar_state(view_key="vk") is True
    rq = repo2.neo4j.calls[-1][0]
    assert "n.view_current = false" in rq and "n.retired = true" in rq
