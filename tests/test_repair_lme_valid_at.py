from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

import scripts.repair_lme_valid_at as repair


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def data(self) -> list[dict[str, object]]:
        return self._rows


class _Session:
    def __init__(self) -> None:
        self.write_query = ""
        self.write_params: dict[str, object] = {}

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def run(self, query: str, params: dict[str, object]) -> _Result:
        if "RETURN e.uuid AS uuid" in query:
            return _Result([{
                "uuid": "episode-1",
                "name": "episode-sid-1",
                "ns": "lme-test-1",
                "valid_at": datetime(2026, 7, 23, tzinfo=timezone.utc),
            }])
        self.write_query = query
        self.write_params = params
        return _Result([{"n": 0}])


class _Driver:
    def __init__(self) -> None:
        self.session_obj = _Session()
        self.closed = False

    def session(self) -> _Session:
        return self.session_obj

    def close(self) -> None:
        self.closed = True


def test_apply_counts_matched_writes_and_rechecks_namespace(monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    driver = _Driver()
    monkeypatch.setattr(repair.GraphDatabase, "driver", lambda *args, **kwargs: driver)
    monkeypatch.setattr(repair, "build_session_times", lambda: {
        "sid-1": datetime(2023, 5, 25, 20, 21, tzinfo=timezone.utc),
    })
    monkeypatch.setattr(repair, "session_of", lambda _name: "sid-1")
    monkeypatch.setattr(sys, "argv", ["repair_lme_valid_at.py", "--apply"])

    assert repair.main() == 0

    assert "WHERE e.group_id STARTS WITH $p" in driver.session_obj.write_query
    assert driver.session_obj.write_params["p"] == repair.NAMESPACE_PREFIX
    output = capsys.readouterr().out
    assert "1 episodes would be rewritten" in output
    assert "repaired 0 episodes" in output
    assert driver.closed is True


def test_apply_and_revert_are_mutually_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_connection(*_args, **_kwargs):
        pytest.fail("Conflicting flags reached driver creation.")

    monkeypatch.setattr(repair.GraphDatabase, "driver", unexpected_connection)
    monkeypatch.setattr(sys, "argv", ["repair_lme_valid_at.py", "--apply", "--revert"])

    with pytest.raises(SystemExit) as exc:
        repair.main()

    assert exc.value.code == 2
