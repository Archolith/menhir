"""TimelineKind legacy subject-only contract. Offline unit tests on the kind (no Neo4j).

Moved verbatim from the deleted test_event_timeline_kind.py when the event-lane mode was archived
(2026-10-28); the legacy output shape they pin is unchanged."""

from __future__ import annotations

import pytest

from menhir.infrastructure.view_models import TimelineKind, _normalize_entries


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
