from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

import scripts.repair_lme_valid_at as repair
import scripts._verify_valid_at_repair as verifier


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def data(self) -> list[dict[str, object]]:
        return self._rows


class _Session:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.write_query = ""
        self.write_params: dict[str, object] = {}
        self.committed = False
        self.rolled_back = False
        self.batch_counts = iter([1])
        self.rows = rows or [{
            "uuid": "episode-1", "name": "episode-sid-1", "ns": "lme-test-1",
            "valid_at": datetime(2026, 7, 23, tzinfo=timezone.utc), "original": None,
        }]

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def run(self, query: str, params: dict[str, object]) -> _Result:
        if "RETURN e.uuid AS uuid" in query:
            return _Result(self.rows)
        self.write_query = query
        self.write_params = params
        return _Result([{"n": next(self.batch_counts)}])

    def begin_transaction(self):
        session = self

        class _Transaction:
            run = session.run

            def __enter__(self):
                return self

            def __exit__(self, *_):
                session.rolled_back = not session.committed

            def commit(self):
                session.committed = True

        return _Transaction()


class _Driver:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.session_obj = _Session(rows)
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
        ("1", "sid-1"): datetime(2023, 5, 25, 20, 21, tzinfo=timezone.utc),
    })
    monkeypatch.setattr(repair, "session_of", lambda _name: "sid-1")
    monkeypatch.setattr(sys, "argv", ["repair_lme_valid_at.py", "--apply"])

    assert repair.main() == 0

    assert "e.group_id STARTS WITH $p" in driver.session_obj.write_query
    assert "e.valid_at = row.expected_valid_at" in driver.session_obj.write_query
    assert driver.session_obj.write_params["p"] == repair.NAMESPACE_PREFIX
    [planned] = driver.session_obj.write_params["rows"]
    assert planned["expected_valid_at"] == driver.session_obj.rows[0]["valid_at"]
    assert planned["namespace"] == "lme-test-1"
    output = capsys.readouterr().out
    assert "1 episodes would be rewritten" in output
    assert "repaired 1 episodes" in output
    assert driver.session_obj.committed
    assert driver.closed is True


def test_apply_rolls_back_earlier_batches_when_a_later_batch_changes(monkeypatch, capsys):
    driver = _Driver()
    row = driver.session_obj.rows[0]
    driver.session_obj.rows = [dict(row, uuid=f"episode-{i}") for i in range(501)]
    driver.session_obj.batch_counts = iter([500, 0])
    monkeypatch.setattr(repair.GraphDatabase, "driver", lambda *args, **kwargs: driver)
    monkeypatch.setattr(repair, "build_session_times", lambda: {
        ("1", "sid-1"): datetime(2023, 5, 25, 20, 21, tzinfo=timezone.utc),
    })
    monkeypatch.setattr(repair, "session_of", lambda _name: "sid-1")
    monkeypatch.setattr(sys, "argv", ["repair_lme_valid_at.py", "--apply"])

    assert repair.main() == 2
    assert driver.session_obj.rolled_back
    assert driver.closed
    assert "matched 0 of 1 planned episodes" in capsys.readouterr().err


@pytest.mark.parametrize("backup", [None, datetime(2026, 7, 23, tzinfo=timezone.utc)])
def test_apply_refuses_control_or_repaired_mismatch_before_write_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backup: datetime | None,
) -> None:
    driver = _Driver([{
        "uuid": "episode-1", "name": "episode-sid-1", "ns": "lme-test-1",
        "valid_at": datetime(2023, 1, 1, tzinfo=timezone.utc), "original": backup,
    }])
    monkeypatch.setattr(repair.GraphDatabase, "driver", lambda *args, **kwargs: driver)
    monkeypatch.setattr(repair, "build_session_times", lambda: {
        ("1", "sid-1"): datetime(2023, 5, 25, 20, 21, tzinfo=timezone.utc),
    })
    monkeypatch.setattr(repair, "session_of", lambda _name: "sid-1")
    monkeypatch.setattr(sys, "argv", ["repair_lme_valid_at.py", "--apply"])

    assert repair.main() == 2
    assert driver.session_obj.write_query == ""
    assert "REFUSING --apply" in capsys.readouterr().err


def test_apply_and_revert_are_mutually_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_connection(*_args, **_kwargs):
        pytest.fail("Conflicting flags reached driver creation.")

    monkeypatch.setattr(repair.GraphDatabase, "driver", unexpected_connection)
    monkeypatch.setattr(sys, "argv", ["repair_lme_valid_at.py", "--apply", "--revert"])

    with pytest.raises(SystemExit) as exc:
        repair.main()

    assert exc.value.code == 2


def test_verifier_scopes_prefix_and_fails_on_controls_and_repaired_disagreements(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _VerifySession(_Session):
        def run(self, query: str, params: dict[str, object]) -> _Result:
            assert "WHERE e.group_id STARTS WITH $p" in query
            assert params == {"p": "custom-"}
            return _Result([
                {
                    "uuid": "control", "name": "episode-sid-1", "ns": "custom-1",
                    "valid_at": datetime(2023, 1, 1, tzinfo=timezone.utc),
                    "original": None,
                },
                {
                    "uuid": "repaired", "name": "episode-sid-1", "ns": "custom-1",
                    "valid_at": datetime(2023, 1, 2, tzinfo=timezone.utc),
                    "original": datetime(2026, 7, 23, tzinfo=timezone.utc),
                },
                {
                    "uuid": "repaired-missing", "name": "episode-sid-1", "ns": "custom-1",
                    "valid_at": datetime(2023, 5, 25, 20, 21, tzinfo=timezone.utc),
                    "original": None, "was_missing": True,
                },
            ])

    class _VerifyDriver:
        def __init__(self) -> None:
            self.session_obj = _VerifySession()

        def session(self) -> _VerifySession:
            return self.session_obj

        def close(self) -> None:
            return None

    monkeypatch.setattr(verifier.GraphDatabase, "driver", lambda *args, **kwargs: _VerifyDriver())
    monkeypatch.setattr(verifier, "build_session_times", lambda: {("1", "sid-1"): datetime(
        2023, 5, 25, 20, 21, tzinfo=timezone.utc)})
    monkeypatch.setattr(verifier.gt, "load", lambda: {"question": {}})
    monkeypatch.setattr(verifier, "session_of", lambda _name: "sid-1")
    monkeypatch.setattr(sys, "argv", ["_verify_valid_at_repair.py", "--prefix", "custom-"])

    assert verifier.main() == 1
    output = capsys.readouterr().out
    assert "already correct, fixture DISAGREES" in output
    assert "repaired row, fixture DISAGREES" in output
    assert "repaired row, fixture AGREES" in output


def test_fixture_index_scopes_shared_hyphenated_session_ids_by_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "answer_ab-cd_1"
    suffix = "644c0e5a-1234-4321-aaaa-123456789abc"
    monkeypatch.setattr(verifier.gt, "load", lambda: {
        "first": {"haystack_session_ids": [sid], "haystack_dates": ["2023/05/25 (Thu) 20:21"]},
        "second": {"haystack_session_ids": [sid], "haystack_dates": ["2023/05/27 (Sat) 10:20"]},
    })

    for prefix in ("episode-", "episode-lme-first-"):
        assert verifier.session_of(f"{prefix}{sid}-{suffix}") == sid

    assert verifier.build_session_times() == {
        ("first", sid): datetime(2023, 5, 25, 20, 21, tzinfo=timezone.utc),
        ("second", sid): datetime(2023, 5, 27, 10, 20, tzinfo=timezone.utc),
    }


@pytest.mark.parametrize("script", [repair, verifier])
def test_conflicting_fixture_dates_refuse_before_opening_driver(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], script,
) -> None:
    monkeypatch.setattr(script.gt, "load", lambda: {
        "first": {
            "haystack_session_ids": ["answer_shared", "answer_shared"],
            "haystack_dates": ["2023/05/25 (Thu) 20:21", "2023/05/27 (Sat) 10:20"],
        }
    })
    monkeypatch.setattr(sys, "argv", [script.__name__])

    def unexpected_connection(*_args, **_kwargs):
        pytest.fail("Conflicting fixture reached driver creation.")

    monkeypatch.setattr(script.GraphDatabase, "driver", unexpected_connection)
    assert script.main() == 2
    assert "conflicting fixture dates for first/answer_shared" in capsys.readouterr().err
