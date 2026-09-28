"""Check that the CI E2E run proved every collected runnable lane."""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CONTAINER_LANE = "test_e2e_06_release_container"
REQUIRED_LANES = frozenset(
    {
        "test_e2e_01_cold_install_and_protocol[mvp_default]",
        "test_e2e_02_memory_lifecycle[mvp_default]",
        "test_e2e_03_coding_workflow[mvp_default]",
        "test_e2e_04_workartifacts[mvp_default]",
        "test_e2e_05_todos[mvp_default]",
        "test_e2e_06_packaged_graphiti_fork[mvp_default]",
        "test_e2e_07_restart_interruption[mvp_default]",
        "test_e2e_08_isolation[mvp_default]",
        "test_e2e_08_os_denied_directory_preserves_structure[mvp_default]",
        "test_e2e_08_provider_failure_is_not_silent[mvp_default]",
        "test_e2e_08_malformed_artifact_metadata[mvp_default]",
        CONTAINER_LANE,
        "test_e2e_08_missing_fork_refusal",
    }
)


def validate_ci_evidence(
    junit_path: Path,
    evidence_root: Path,
    *,
    allow_container_pending: bool,
    required_lanes: frozenset[str] = REQUIRED_LANES,
) -> None:
    """Compare pytest outcomes with the lane verdicts from this one campaign run."""

    cases = list(ET.parse(junit_path).iter("testcase"))
    if not cases:
        raise ValueError("pytest reported no E2E test cases")

    names = [case.get("name") for case in cases]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("pytest reported missing or duplicate E2E lane names")

    result_paths = list(evidence_root.glob("*/**/result.json"))
    run_dirs = {path.relative_to(evidence_root).parts[0] for path in result_paths}
    if len(run_dirs) != 1:
        raise ValueError(
            f"expected evidence from exactly one run; found {len(run_dirs)}"
        )

    results = {}
    errors = []
    for name in sorted(required_lanes - set(names)):
        errors.append(f"required E2E lane not collected: {name}")
    for path in result_paths:
        relative = path.relative_to(evidence_root)
        if len(relative.parts) != 3:
            errors.append(f"unexpected evidence path: {relative}")
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"cannot read {relative}: {exc}")
            continue
        lane = relative.parts[1]
        if (
            not isinstance(result, dict)
            or lane in results
            or result.get("lane") != lane
        ):
            errors.append(f"duplicate or mislabeled lane evidence: {relative}")
            continue
        results[lane] = result

    expected = set(names)
    for name in sorted(expected - results.keys()):
        errors.append(f"missing result.json for {name}")
    for name in sorted(results.keys() - expected):
        errors.append(f"evidence has no collected test case: {name}")

    pass_count = 0
    for case in cases:
        name = case.get("name")
        result = results.get(name)
        if result is None:
            continue
        skipped = case.find("skipped") is not None
        failed = case.find("failure") is not None or case.find("error") is not None
        criteria = result.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            errors.append(f"{name}: missing criterion evidence")
            continue
        if any(not isinstance(item, dict) for item in criteria.values()):
            errors.append(f"{name}: malformed criterion evidence")
            continue
        unproven = result.get("criteria_unproven")
        if not isinstance(unproven, list) or any(
            not isinstance(item, str) for item in unproven
        ):
            errors.append(f"{name}: malformed unproven criteria")
            continue

        if allow_container_pending and name == CONTAINER_LANE:
            if not skipped or failed or result.get("status") != "PENDING":
                errors.append(f"{name}: expected an explicit PENDING pytest skip")
            if (
                result.get("criteria_total") != len(criteria)
                or result.get("criteria_passed") != 0
                or set(unproven) != set(criteria)
                or any(item.get("passed") is not False for item in criteria.values())
            ):
                errors.append(f"{name}: PENDING criteria are incomplete")
            continue

        if skipped or failed:
            errors.append(f"{name}: unexpected pytest skip or failure")
        if result.get("status") != "PASS":
            errors.append(f"{name}: evidence status is not PASS")
        if (
            result.get("criteria_total") != len(criteria)
            or result.get("criteria_passed") != len(criteria)
            or unproven != []
            or any(item.get("passed") is not True for item in criteria.values())
        ):
            errors.append(f"{name}: PASS criteria are incomplete")
        if not skipped and not failed and result.get("status") == "PASS":
            pass_count += 1

    if not pass_count:
        errors.append("no runnable lane has PASS evidence")
    if errors:
        raise ValueError(
            "E2E evidence gate failed:\n" + "\n".join(f"- {error}" for error in errors)
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        validate_ci_evidence(
            args.junit,
            args.evidence,
            allow_container_pending=not os.getenv(
                "MENHIR_E2E_RELEASE_IMAGE_BUNDLE", ""
            ).strip(),
        )
    except (OSError, ValueError, ET.ParseError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print("E2E evidence gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
