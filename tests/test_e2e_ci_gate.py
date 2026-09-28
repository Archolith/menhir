"""Regression checks for the installed-wheel E2E CI gate."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e import conftest as e2e_conftest
from tests.e2e._harness.ci_evidence_gate import validate_ci_evidence


def _report(path: Path, outcomes: dict[str, str]) -> None:
    suite = ET.Element("testsuite")
    for lane, outcome in outcomes.items():
        case = ET.SubElement(suite, "testcase", name=lane)
        if outcome != "passed":
            ET.SubElement(case, outcome)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def _evidence(root: Path, lane: str, status: str) -> None:
    directory = root / "run-1" / lane
    directory.mkdir(parents=True, exist_ok=True)
    passed = status == "PASS"
    (directory / "result.json").write_text(
        json.dumps(
            {
                "lane": lane,
                "status": status,
                "criteria_total": 1,
                "criteria_passed": int(passed),
                "criteria_unproven": [] if passed else ["criterion"],
                "criteria": {"criterion": {"passed": passed}},
            }
        ),
        encoding="utf-8",
    )


def test_wheel_build_failure_fails_enabled_campaign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(e2e_conftest, "tree_is_clean", lambda _: (True, []))

    def fail_build(_: Path) -> None:
        raise RuntimeError("forced wheel build failure")

    monkeypatch.setattr(e2e_conftest, "build_wheel", fail_build)
    fixture = e2e_conftest.e2e_installed.__wrapped__(
        SimpleNamespace(work_root=tmp_path)
    )
    with pytest.raises(pytest.fail.Exception, match="forced wheel build failure"):
        next(fixture)


def test_gate_accepts_passes_and_explicit_container_pending(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    evidence = tmp_path / "evidence"
    _report(
        report,
        {
            "test_new_lane[mvp_default]": "passed",
            "test_e2e_06_release_container": "skipped",
        },
    )
    _evidence(evidence, "test_new_lane[mvp_default]", "PASS")
    _evidence(evidence, "test_e2e_06_release_container", "PENDING")

    validate_ci_evidence(
        report,
        evidence,
        allow_container_pending=True,
        required_lanes=frozenset(
            {"test_new_lane[mvp_default]", "test_e2e_06_release_container"}
        ),
    )


def test_gate_rejects_missing_runnable_result(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    evidence = tmp_path / "evidence"
    _report(report, {"test_first": "passed", "test_new_lane": "passed"})
    _evidence(evidence, "test_first", "PASS")

    with pytest.raises(ValueError, match="missing result.json for test_new_lane"):
        validate_ci_evidence(
            report,
            evidence,
            allow_container_pending=True,
            required_lanes=frozenset({"test_first", "test_new_lane"}),
        )


def test_gate_rejects_unexpected_skip_even_with_pending_result(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    evidence = tmp_path / "evidence"
    _report(report, {"test_first": "passed", "test_new_lane": "skipped"})
    _evidence(evidence, "test_first", "PASS")
    _evidence(evidence, "test_new_lane", "PENDING")

    with pytest.raises(ValueError, match="test_new_lane: unexpected pytest skip"):
        validate_ci_evidence(
            report,
            evidence,
            allow_container_pending=True,
            required_lanes=frozenset({"test_first", "test_new_lane"}),
        )


def test_gate_rejects_pass_without_proven_criteria(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    evidence = tmp_path / "evidence"
    _report(report, {"test_new_lane": "passed"})
    _evidence(evidence, "test_new_lane", "PASS")
    path = evidence / "run-1" / "test_new_lane" / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    result["criteria"]["criterion"]["passed"] = False
    path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ValueError, match="test_new_lane: PASS criteria are incomplete"):
        validate_ci_evidence(
            report,
            evidence,
            allow_container_pending=True,
            required_lanes=frozenset({"test_new_lane"}),
        )


def test_gate_rejects_a_missing_required_lane(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    evidence = tmp_path / "evidence"
    _report(report, {"test_first": "passed"})
    _evidence(evidence, "test_first", "PASS")

    with pytest.raises(
        ValueError, match="required E2E lane not collected: test_second"
    ):
        validate_ci_evidence(
            report,
            evidence,
            allow_container_pending=True,
            required_lanes=frozenset({"test_first", "test_second"}),
        )
