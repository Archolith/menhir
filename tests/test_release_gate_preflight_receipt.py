"""#606 and #607: scripts/release.py preflights its environment, and `--tag` reuses a fresh receipt.

#606: a workstation missing what a selected lane needs is refused in seconds, before any lane,
with the command that fixes it.
#607: a passing full check-only run writes a receipt; `--tag` on the same tag, HEAD and
interpreter reuses only the offline and online lanes and reruns every other check.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("release_gate_606", ROOT / "scripts" / "release.py")
assert _SPEC is not None and _SPEC.loader is not None
release = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = release  # dataclasses resolve annotations through sys.modules
_SPEC.loader.exec_module(release)

pytestmark = pytest.mark.unit

SHA = "b" * 40


def _ctx(lanes, *, tagging=False, head=SHA):
    return release.Context(
        tag="v9.9.9", version="9.9.9", head=head, lanes=frozenset(lanes), tagging=tagging
    )


# ---------------------------------------------------------------------------- #606 preflight


def test_tests_yml_pins_exactly_one_beacon_commit() -> None:
    assert len(release.beacon_pins()) == 1


def test_missing_beacon_interpreter_is_refused_with_the_pinned_fix(monkeypatch) -> None:
    monkeypatch.delenv("MENHIR_TEST_BEACON_PYTHON", raising=False)
    with pytest.raises(release.CheckFailed) as err:
        release.check_preflight(_ctx({"offline"}))
    message = str(err.value)
    (pin,) = release.beacon_pins()
    assert "MENHIR_TEST_BEACON_PYTHON is not set" in message
    assert f"beacon.git@{pin}" in message and "MENHIR_TEST_BEACON_PYTHON=" in message


def test_a_beacon_path_that_does_not_exist_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MENHIR_TEST_BEACON_PYTHON", str(tmp_path / "missing-python"))
    with pytest.raises(release.CheckFailed, match="does not exist"):
        release.check_preflight(_ctx({"offline"}))


def test_an_unreachable_test_neo4j_is_refused_in_seconds(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:9")
    started = time.monotonic()
    with pytest.raises(release.CheckFailed, match="test Neo4j at bolt://127.0.0.1:9"):
        release.check_preflight(_ctx({"online"}))
    assert time.monotonic() - started < 5


def test_unauthenticated_gh_is_refused_when_ci_image_or_tag_needs_it(monkeypatch) -> None:
    monkeypatch.setattr(
        release, "_probe", lambda cmd, timeout: None if cmd[0] == "gh" else pytest.fail(cmd)
    )
    for ctx in (_ctx({"ci"}), _ctx({"image"}), _ctx(set(), tagging=True)):
        with pytest.raises(release.CheckFailed, match="gh auth login"):
            release.check_preflight(ctx)


def test_preflight_probes_only_what_the_selected_checks_need(monkeypatch) -> None:
    monkeypatch.setattr(release, "_probe", lambda *a, **k: pytest.fail("no probe expected"))
    monkeypatch.setattr(
        release, "_test_neo4j_problem", lambda *a, **k: pytest.fail("no neo4j probe expected")
    )
    monkeypatch.delenv("MENHIR_TEST_BEACON_PYTHON", raising=False)
    assert "nothing" in release.check_preflight(_ctx({"tree", "changelog"}))


def test_every_problem_is_reported_at_once(monkeypatch) -> None:
    monkeypatch.delenv("MENHIR_TEST_BEACON_PYTHON", raising=False)
    monkeypatch.setattr(release, "_probe", lambda cmd, timeout: None)
    monkeypatch.setattr(release, "_test_neo4j_problem", lambda env, **k: "ServiceUnavailable")
    with pytest.raises(release.CheckFailed) as err:
        release.check_preflight(_ctx({"lint", "offline", "online", "ci"}))
    message = str(err.value)
    for needle in ("ruff", "MENHIR_TEST_BEACON_PYTHON", "test Neo4j", "gh auth login"):
        assert needle in message


# ---------------------------------------------------------------------------- main() wiring


class FakeChecks:
    """Replaces every check; records which ran, and fails the ones named in `fail`."""

    def __init__(self, monkeypatch, fail=()):
        self.ran: list[str] = []
        self.fail = set(fail)
        checks = {}
        for name in release.CHECK_ORDER:
            checks[name] = self._make(name)
        monkeypatch.setattr(release, "CHECKS", checks)
        monkeypatch.setattr(release, "do_tag", lambda ctx: None)
        monkeypatch.setattr(release, "watch_publish", lambda ctx: None)

    def _make(self, name):
        def check(ctx):
            self.ran.append(name)
            if name == "branch":
                ctx.head = SHA
            if name in self.fail:
                raise release.CheckFailed(f"{name} failed")
            return f"{name} ok"

        return check


@pytest.fixture
def receipts(monkeypatch, tmp_path):
    monkeypatch.setattr(release, "RECEIPT_DIR", tmp_path / ".release-gate")
    return tmp_path / ".release-gate"


def test_a_preflight_failure_stops_before_every_lane(monkeypatch, receipts) -> None:
    checks = FakeChecks(monkeypatch, fail={"preflight"})
    assert release.main(["v9.9.9"]) == 1
    assert checks.ran == ["preflight"]


def test_preflight_runs_even_with_only(monkeypatch, receipts) -> None:
    checks = FakeChecks(monkeypatch)
    assert release.main(["v9.9.9", "--only", "lint"]) == 0
    assert checks.ran == ["preflight", "lint"]


def test_a_full_check_only_pass_writes_a_receipt_and_a_partial_one_does_not(
    monkeypatch, receipts
) -> None:
    FakeChecks(monkeypatch)
    assert release.main(["v9.9.9", "--only", "lint,offline"]) == 0
    assert not (receipts / "v9.9.9.json").exists()
    assert release.main(["v9.9.9"]) == 0
    data = json.loads((receipts / "v9.9.9.json").read_text(encoding="utf-8"))
    assert data["head"] == SHA and data["python"] == release.PY
    assert data["results"] == {"offline": "offline ok", "online": "online ok"}


def test_a_failed_check_only_run_writes_no_receipt(monkeypatch, receipts) -> None:
    FakeChecks(monkeypatch, fail={"image"})
    assert release.main(["v9.9.9"]) == 1
    assert not (receipts / "v9.9.9.json").exists()


def test_tag_reuses_only_the_slow_lanes_and_reruns_every_other_check(
    monkeypatch, receipts
) -> None:
    FakeChecks(monkeypatch)
    assert release.main(["v9.9.9"]) == 0
    checks = FakeChecks(monkeypatch)
    assert release.main(["v9.9.9", "--tag"]) == 0
    assert checks.ran == [n for n in release.CHECK_ORDER if n not in release.REUSABLE]
    assert not (receipts / "v9.9.9.json").exists()  # consumed by the release


def test_fresh_reruns_everything(monkeypatch, receipts) -> None:
    FakeChecks(monkeypatch)
    assert release.main(["v9.9.9"]) == 0
    checks = FakeChecks(monkeypatch)
    assert release.main(["v9.9.9", "--tag", "--fresh"]) == 0
    assert checks.ran == list(release.CHECK_ORDER)


def test_a_failing_rerun_check_still_refuses_the_tag(monkeypatch, receipts) -> None:
    FakeChecks(monkeypatch)
    assert release.main(["v9.9.9"]) == 0
    tagged = []
    checks = FakeChecks(monkeypatch, fail={"ci"})
    monkeypatch.setattr(release, "do_tag", lambda ctx: tagged.append(ctx.tag))
    assert release.main(["v9.9.9", "--tag"]) == 1
    assert "ci" in checks.ran and not tagged


# ---------------------------------------------------------------------------- receipt validity


def _write(receipts, **overrides):
    payload = {
        "tag": "v9.9.9",
        "head": SHA,
        "python": release.PY,
        "created": time.time(),
        "results": {"offline": "o", "online": "n"},
        **overrides,
    }
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "v9.9.9.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_matching_recent_receipt_is_reused(receipts) -> None:
    _write(receipts)
    results, _note = release.load_receipt(_ctx(set()))
    assert results == {"offline": "o", "online": "n"}


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"head": "c" * 40}, "HEAD is"),
        ({"tag": "v9.9.8"}, "another tag"),
        ({"python": "/other/python"}, "another interpreter"),
        ({"created": time.time() - 3 * 3600}, "min old"),
        ({"created": time.time() + 3600}, "min old"),
        ({"results": {"offline": "o"}}, "every slow lane"),
    ],
)
def test_a_receipt_is_not_reused_when_anything_differs(receipts, overrides, reason) -> None:
    _write(receipts, **overrides)
    results, note = release.load_receipt(_ctx(set()))
    assert results is None and reason in note


def test_no_receipt_and_a_corrupt_receipt_are_not_reused(receipts) -> None:
    assert release.load_receipt(_ctx(set()))[0] is None
    receipts.mkdir(parents=True)
    (receipts / "v9.9.9.json").write_text("{not json", encoding="utf-8")
    results, note = release.load_receipt(_ctx(set()))
    assert results is None and "unreadable" in note


def test_a_stale_receipt_makes_tag_run_the_slow_lanes(monkeypatch, receipts) -> None:
    _write(receipts, created=time.time() - 3 * 3600)
    checks = FakeChecks(monkeypatch)
    assert release.main(["v9.9.9", "--tag"]) == 0
    assert checks.ran == list(release.CHECK_ORDER)
