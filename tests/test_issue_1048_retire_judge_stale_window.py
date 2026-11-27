"""#1048: retire_judge's stale_anchor signal must not depend on the namespace's top-N dirtied window."""

from __future__ import annotations

from typing import Any

import pytest

from menhir.services import retire_judge as svc


class _FakeToolEvents:
    """Honours `limit` (most recently dirtied first) and `memory_uuids` like the real read."""

    stale_rows: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    def __init__(self, _adapter: Any) -> None:
        pass

    def stale_anchored_memories(self, *, namespace=None, limit=200, memory_uuids=None, project=None):
        type(self).calls.append({"limit": limit, "memory_uuids": memory_uuids})
        rows = sorted(type(self).stale_rows, key=lambda r: r["dirty_at"], reverse=True)
        if memory_uuids is not None:
            rows = [r for r in rows if r["memory_uuid"] in set(memory_uuids)]
        return rows[:limit]


@pytest.fixture(autouse=True)
def _fake_repo(monkeypatch):
    _FakeToolEvents.stale_rows, _FakeToolEvents.calls = [], []
    monkeypatch.setattr(svc, "ToolEventRepository", _FakeToolEvents)


def _stale(uuid: str, dirty_at: str) -> dict[str, Any]:
    return {"memory_uuid": uuid, "path": f"{uuid}.py", "project": "p", "dirty_at": dirty_at,
            "anchored_at": "2020-01-01T00:00:00", "operation": "edit"}


def _nominate(candidate_uuids: list[str]) -> list[dict[str, Any]]:
    rows = [{"uuid": u, "labels": ["Episodic"], "content": "c"} for u in candidate_uuids]
    return svc.nominate(lambda _q, _p: rows, "ns", limit=50)


def test_candidate_with_oldest_anchor_beyond_the_window_still_gets_stale_anchor():
    # 1500 other stale anchors, all newer than the candidate's: it is outside any top-1000 window.
    _FakeToolEvents.stale_rows = [_stale("cand", "2021-01-01T00:00:00")] + [
        _stale(f"other-{n}", f"2022-01-01T00:{n // 60:02d}:{n % 60:02d}") for n in range(1500)
    ]
    out = _nominate(["cand"])
    assert [c["uuid"] for c in out] == ["cand"]
    assert out[0]["signals"] == ["stale_anchor"]
    assert out[0]["anchor"]["path"] == "cand.py"


def test_a_non_candidates_stale_anchor_does_not_leak_into_the_result():
    _FakeToolEvents.stale_rows = [_stale("cand", "2021-01-01T00:00:00"),
                                  _stale("outsider", "2022-01-01T00:00:00")]
    out = _nominate(["cand", "clean"])
    assert {c["uuid"] for c in out} == {"cand"}  # "clean" has no signal; "outsider" is not a candidate
    assert all(c["uuid"] != "outsider" for c in out)


def test_no_candidates_makes_no_stale_read_and_does_not_error():
    _FakeToolEvents.stale_rows = [_stale("outsider", "2022-01-01T00:00:00")]
    assert _nominate([]) == []
    assert _FakeToolEvents.calls == []
