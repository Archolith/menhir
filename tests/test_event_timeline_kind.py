"""TimelineKind event-lane mode (Phase 2A.1): predicate/domain event lanes over the existing
`timeline` kind, with the legacy subject-only timeline contract untouched. Offline unit tests on
the kind + its dedicated event normalizer (no Neo4j), mirroring `test_scalar_state_view.py`.

Covers: legacy output-shape invariance; collision-safe `timeline:event:` lane keys; domain-without-
predicate fail-closed; the fixed event-entry schema/allowlist; invalid-time exclusion + UTC
canonicalization; out-of-order sorting; exact-replay dedup under reversed input; same-time distinct
assertions; signature sensitivity to quote/object/provenance/lane; occurrence/history surface;
event write_props + conditional parse fields; and episode UUIDs / latest valid_at."""

from __future__ import annotations

import pytest

from menhir.infrastructure.view_models import (
    TimelineKind,
    _event_lane_suffix,
    _normalize_entries,
    _normalize_event_entries,
)

_EP = "ep-1"
_SRC = "ep-1:0:5:0"


def _entry(**overrides):
    """A valid synthetic event entry; override any field for a specific scenario."""
    base = {
        "assertion_key": "ak-1",
        "source_key": _SRC,
        "when": "2026-05-10T12:00:00Z",
        "what": "acquired a tripod",
        "object_key": "obj:tripod",
        "object_uuid": "obj-uuid-1",
        "quote": "I picked up a new tripod last week",
        "episode_uuid": _EP,
        "turn_evidence_uuid": "turn-1",
        "time_basis": "explicit",
        "evidence_tier": "agent",
    }
    base.update({k: v for k, v in overrides.items() if v is not None})
    for k, v in overrides.items():
        if v is None:
            base.pop(k, None)
    return base


def _event_payload(entries, predicate="purchased", domain=None):
    p = {"entries": entries}
    if predicate is not None:
        p["predicate"] = predicate
    if domain is not None:
        p["domain"] = domain
    return p


# --- legacy subject-only output shape stays exact -----------------------------------------

@pytest.mark.unit
def test_legacy_key_discriminator_is_constant():
    kind = TimelineKind()
    assert kind.key_discriminator({"entries": []}) == "timeline"
    assert kind.key_discriminator({"entries": [{"when": "x", "what": "y"}]}) == "timeline"


@pytest.mark.unit
def test_legacy_signature_is_ordered_when_what():
    kind = TimelineKind()
    a = {"entries": [{"when": "2026-01-01", "what": "b"}, {"when": "2026-01-02", "what": "a"}]}
    rev = {"entries": [{"when": "2026-01-02", "what": "a"}, {"when": "2026-01-01", "what": "b"}]}
    # At the kind layer the legacy signature hashes the given (when, what) pairs IN ORDER, so raw
    # reversal differs here (unchanged legacy behavior — `_timeline_sig` is intentionally
    # order-sensitive).
    assert kind.signature(a) != kind.signature(rev)
    # Order-independence is restored by the legacy normalizer's canonical (when, what) sort, which
    # is what the wrapper (`record_timeline`) applies before the kind sees the entries.
    assert kind.signature({"entries": _normalize_entries(rev["entries"])}) == \
        kind.signature({"entries": _normalize_entries(a["entries"])})
    changed = {"entries": [{"when": "2026-01-01", "what": "c"}, {"when": "2026-01-02", "what": "a"}]}
    assert kind.signature(a) != kind.signature(changed)


@pytest.mark.unit
def test_legacy_surface_and_render_unchanged():
    kind = TimelineKind()
    entries = [{"when": "2026-01-01T00:00:00Z", "what": "alpha"},
               {"when": "2026-01-02T00:00:00Z", "what": "beta", "episode_uuid": "e9"}]
    name, summary = kind.surface("the user", {"entries": entries})
    assert name.startswith("timeline of the user:")
    assert "alpha" in name and "beta" in name
    assert "Timeline — the user" in summary


@pytest.mark.unit
def test_legacy_write_props_and_parse_shape():
    kind = TimelineKind()
    entries = [{"when": "2026-01-01", "what": "a", "episode_uuid": "e1"}]
    props = kind.write_props("u", "k", {"entries": entries})
    assert props == {"view_value": 1.0, "view_payload": '[{"when": "2026-01-01", "what": "a", "episode_uuid": "e1"}]'}
    assert "view_predicate" not in props and "view_domain" not in props
    row = {"uuid": "n", "subject": "u", "count": 1,
           "payload": '[{"when": "2026-01-01", "what": "a"}]', "valid_at": "x"}
    parsed = kind.parse(row)
    assert set(parsed) == {"uuid", "subject", "count", "valid_at", "entries"}
    assert "predicate" not in parsed and "domain" not in parsed


# --- event discriminator: normalization + collision safety -------------------------------

@pytest.mark.unit
def test_event_lane_suffix_is_stable_and_normalized():
    a = _event_lane_suffix("  Purchased ", "  Camera_Lens ")
    b = _event_lane_suffix("purchased", "camera_lens")
    assert a == b
    assert a.startswith("timeline:event:")


@pytest.mark.unit
def test_event_lane_suffix_collision_safe_on_colon():
    # Raw colon concat would collide; length-prefixed percent-encoding must not.
    x = _event_lane_suffix("a", "b:c")
    y = _event_lane_suffix("a:b", "c")
    assert x != y
    assert _event_lane_suffix("purchased", None) != _event_lane_suffix("purchased", "camera")
    assert _event_lane_suffix("purchased", None) != _event_lane_suffix("sold", None)


@pytest.mark.unit
def test_event_key_discriminator_distinguishes_lanes():
    kind = TimelineKind()
    purchased = kind.key_discriminator(_event_payload([], predicate="purchased"))
    sold = kind.key_discriminator(_event_payload([], predicate="sold"))
    acquired = kind.key_discriminator(
        _event_payload([], predicate="purchased", domain="camera_lens"))
    assert purchased.startswith("timeline:event:")
    assert purchased != sold
    assert purchased != acquired


# --- domain without predicate fails closed ------------------------------------------------

@pytest.mark.unit
def test_domain_without_predicate_rejected():
    kind = TimelineKind()
    payload = {"entries": [], "domain": "camera_lens"}  # predicate absent/blank
    with pytest.raises(ValueError):
        kind.key_discriminator(payload)


# --- event-entry schema: required fields + allowlist --------------------------------------

@pytest.mark.unit
def test_required_event_fields_missing_raises():
    for missing in ("assertion_key", "source_key", "when", "what", "object_key", "quote",
                    "episode_uuid"):
        with pytest.raises(ValueError, match="required"):
            _normalize_event_entries([_entry(**{missing: None})])


@pytest.mark.unit
def test_event_entries_keep_only_allowlist():
    extra = _entry()
    extra["intruder"] = "drop-me"
    extra["nested"] = {"x": 1}
    norm = _normalize_event_entries([extra])
    assert len(norm) == 1
    assert set(norm[0]) == {
        "assertion_key", "source_key", "when", "what", "object_key", "object_uuid",
        "quote", "episode_uuid", "turn_evidence_uuid", "time_basis", "evidence_tier",
    }


@pytest.mark.unit
def test_optional_uuids_may_be_none():
    e = _entry(object_uuid=None, turn_evidence_uuid=None)
    norm = _normalize_event_entries([e])[0]
    assert norm["object_uuid"] is None and norm["turn_evidence_uuid"] is None


# --- invalid time exclusion + UTC canonicalization ---------------------------------------

@pytest.mark.unit
def test_invalid_time_excluded_and_valid_canonicalized_to_utc_z():
    bad = _entry(assertion_key="bad", when="not-a-time")
    naive = _entry(assertion_key="naive", when="2026-05-10T12:00:00+00:00")
    zulu = _entry(assertion_key="zulu", when="2026-05-10T12:00:00Z")
    offset = _entry(assertion_key="offset", when="2026-05-10T14:00:00+02:00")
    norm = _normalize_event_entries([bad, naive, zulu, offset])
    assert len(norm) == 3  # invalid excluded
    assert all(e["when"].endswith("Z") for e in norm)
    assert {e["assertion_key"] for e in norm} == {"naive", "zulu", "offset"}
    # +02:00 14:00 == UTC 12:00 == Z 12:00
    assert norm[0]["when"] == norm[1]["when"] == "2026-05-10T12:00:00Z"


# --- out-of-order sorting ----------------------------------------------------------------

@pytest.mark.unit
def test_entries_sorted_ascending_by_world_time_then_assertion_key():
    late = _entry(assertion_key="ak-late", when="2026-07-01T00:00:00Z")
    early = _entry(assertion_key="ak-early", when="2026-01-01T00:00:00Z")
    mid = _entry(assertion_key="ak-mid", when="2026-04-01T00:00:00Z")
    norm = _normalize_event_entries([late, early, mid])
    assert [e["assertion_key"] for e in norm] == ["ak-early", "ak-mid", "ak-late"]


@pytest.mark.unit
def test_same_time_distinct_assertions_in_assertion_key_order():
    b = _entry(assertion_key="ak-b", when="2026-05-10T12:00:00Z")
    a = _entry(assertion_key="ak-a", when="2026-05-10T12:00:00Z")
    c = _entry(assertion_key="ak-c", when="2026-05-10T12:00:00Z")
    norm = _normalize_event_entries([c, a, b])
    assert [e["assertion_key"] for e in norm] == ["ak-a", "ak-b", "ak-c"]


# --- exact-replay dedup -------------------------------------------------------------------

@pytest.mark.unit
def test_exact_replay_dedup_independent_of_input_order():
    base = _entry()
    forwards = [base, dict(base), _entry(assertion_key="ak-2", when="2026-06-01T00:00:00Z")]
    reversed_input = list(reversed(forwards))
    norm_f = _normalize_event_entries(forwards)
    norm_r = _normalize_event_entries(reversed_input)
    assert norm_f == norm_r
    assert len(norm_f) == 2  # the duplicate replay collapsed to one canonical row


@pytest.mark.unit
def test_same_key_same_time_differing_payload_chooses_deterministic_representative():
    # Exact replays share assertion_key AND (by construction) when, but may differ in the
    # non-identity payload (quote/metadata). The representative must be TOTAL/input-order
    # independent, not resolved by the stable-sort tie (which is input order).
    rep_a = _entry(assertion_key="ak-shared", when="2026-05-10T12:00:00Z",
                   quote="quote alpha", source_key="ep:1:1:0")
    rep_b = _entry(assertion_key="ak-shared", when="2026-05-10T12:00:00Z",
                   quote="quote beta", source_key="ep:2:2:0")
    other = _entry(assertion_key="ak-other", when="2026-06-01T00:00:00Z")

    forward = [rep_b, other, rep_a]   # rep_b before rep_a in input
    reverse = [rep_a, other, rep_b]   # rep_a before rep_b in input
    norm_f = _normalize_event_entries(forward)
    norm_r = _normalize_event_entries(reverse)

    assert norm_f == norm_r == [rep_a, other]
    # The canonical representative is stable and the returned order is (when, assertion_key).
    assert [e["assertion_key"] for e in norm_f] == ["ak-shared", "ak-other"]


# --- signature sensitivity ----------------------------------------------------------------

@pytest.mark.unit
def test_event_signature_changes_on_quote_object_provenance_and_lane():
    kind = TimelineKind()
    base = _event_payload([_entry()], predicate="purchased", domain="camera_lens")
    s_base = kind.signature(base)
    assert kind.signature(_event_payload([_entry()], predicate="purchased", domain="camera_lens")) == s_base

    quote = _event_payload([_entry(quote="a totally different quote")],
                           predicate="purchased", domain="camera_lens")
    assert kind.signature(quote) != s_base

    obj = _event_payload([_entry(object_key="obj:other", object_uuid="other-uuid")],
                         predicate="purchased", domain="camera_lens")
    assert kind.signature(obj) != s_base

    prov = _event_payload([_entry(source_key="ep:9:9:9", episode_uuid="ep-other")],
                          predicate="purchased", domain="camera_lens")
    assert kind.signature(prov) != s_base

    lane_pred = _event_payload([_entry()], predicate="sold", domain="camera_lens")
    assert kind.signature(lane_pred) != s_base

    lane_dom = _event_payload([_entry()], predicate="purchased", domain="other_domain")
    assert kind.signature(lane_dom) != s_base


# --- event surface/render wording --------------------------------------------------------

@pytest.mark.unit
def test_event_surface_and_render_occurrence_language():
    kind = TimelineKind()
    entries = [_entry()]
    payload = _event_payload(entries, predicate="purchased", domain="camera_lens")
    name, summary = kind.surface("the user", payload)
    # occurrence/history wording, predicate + domain + object + date readable
    assert "history" in name
    assert "purchased" in name and "camera_lens" in name
    assert "tripod" in name
    assert "Occurrence history" in summary
    assert "purchased" in summary and "tripod" in summary
    assert '"I picked up a new tripod last week"' in summary
    # must NOT claim current ownership / supersession
    low = (name + summary).lower()
    assert "owns" not in low and "currently" not in low and "supersede" not in low


# --- event write_props + conditional parse -----------------------------------------------

@pytest.mark.unit
def test_event_write_props_stamps_predicate_and_domain():
    kind = TimelineKind()
    payload = _event_payload([_entry()], predicate="purchased", domain="camera_lens")
    props = kind.write_props("u", "k", payload)
    assert props["view_value"] == 1.0
    assert props["view_predicate"] == "purchased"
    assert props["view_domain"] == "camera_lens"
    assert "ak-1" in props["view_payload"]  # payload still carries the full entries

    nodom = _event_payload([_entry()], predicate="purchased", domain=None)
    props_nodom = kind.write_props("u", "k", nodom)
    assert props_nodom["view_predicate"] == "purchased"
    assert "view_domain" not in props_nodom  # only stamped in event mode, and only when present


@pytest.mark.unit
def test_event_parse_adds_predicate_domain_only_when_predicate_present():
    kind = TimelineKind()
    event_row = {"uuid": "n", "subject": "u", "count": 1, "payload": "[]",
                 "predicate": "purchased", "domain": "camera_lens", "valid_at": "x"}
    parsed = kind.parse(event_row)
    assert parsed["predicate"] == "purchased" and parsed["domain"] == "camera_lens"

    legacy_row = {"uuid": "n", "subject": "u", "count": 1, "payload": "[]",
                  "predicate": None, "domain": None, "valid_at": "x"}
    legacy = kind.parse(legacy_row)
    assert "predicate" not in legacy and "domain" not in legacy


# --- episode UUIDs + latest valid_at -----------------------------------------------------

@pytest.mark.unit
def test_episode_uuids_and_latest_valid_at():
    kind = TimelineKind()
    entries = [
        _entry(assertion_key="ak-old", when="2026-01-01T00:00:00Z", episode_uuid="ep-old"),
        _entry(assertion_key="ak-new", when="2026-09-01T00:00:00Z", episode_uuid="ep-new"),
    ]
    payload = _event_payload(entries, predicate="purchased")
    assert kind.episode_uuids(payload) == ["ep-old", "ep-new"]
    assert kind.valid_at(payload) == "2026-09-01T00:00:00Z"  # latest retained world time
