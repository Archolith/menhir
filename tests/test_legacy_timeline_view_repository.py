"""ViewRepository legacy timeline wrappers: record_timeline / fetch_timeline behavior.

No live Neo4j. A tiny `_CaptureRepo` subclass overrides only generic `record` / `_fetch_current`
so the wrappers' keying/normalization delegation can be asserted without a write core.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.view_models import _normalize_entries
from menhir.infrastructure.view_repository import ViewRepository


class FakeNeo4j:
    def __init__(self):
        self.executed: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.executed.append((query, params or {}))
        return []


class _CaptureRepo(ViewRepository):
    """Full ViewRepository, but generic `record` / `_fetch_current` are stubbed to capture their
    arguments."""

    def __init__(self, fake):
        super().__init__(fake)
        self.record_calls: list[tuple[str, dict]] = []
        self.fetch_calls: list[tuple[str, str]] = []

    def record(self, kind_name, **kwargs):
        self.record_calls.append((kind_name, kwargs))
        return {"view_key": "k", "view_value": float(len(kwargs.get("entries") or []))}

    def _fetch_current(self, kind_name, key, *, view_class=None):
        self.fetch_calls.append((kind_name, key))
        return {"uuid": "v1", "subject": "Alpha", "count": 2, "valid_at": "2026-05-10T12:00:00Z",
                "entries": [], "predicate": "purchased", "domain": None}


@pytest.mark.unit
def test_legacy_record_timeline_and_fetch_timeline_unchanged():
    repo = _CaptureRepo(FakeNeo4j())
    res = repo.record_timeline(subject="The User", namespace="ns",
                               entries=[{"when": "2026-01-01", "what": "b", "extra": "x"},
                                        {"when": "2026-01-02", "what": "a"}])
    (kind, kwargs), = repo.record_calls
    assert kind == "timeline"
    # legacy path passes NO subject_uuid and never sets authoritative / predicate / domain.
    assert "subject_uuid" not in kwargs
    assert kwargs.get("authoritative") is not True
    assert "predicate" not in kwargs and "domain" not in kwargs
    # legacy normalize keeps only when/what/episode_uuid and sorts ascending by when.
    assert kwargs["entries"] == _normalize_entries(
        [{"when": "2026-01-01", "what": "b"}, {"when": "2026-01-02", "what": "a"}])
    assert all(set(e) == {"when", "what", "episode_uuid"} for e in kwargs["entries"])
    assert res["count"] == 2
    # fetch_timeline looks up the plain text subject-only key.
    repo.fetch_timeline(subject="The User", namespace="ns")
    assert repo.fetch_calls[-1] == ("timeline", "ns::the user::timeline")
