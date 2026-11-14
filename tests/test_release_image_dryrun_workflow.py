"""#613/#615: the release image dry run builds and verifies on every release runner, never
publishing, and release-image.yml only ever runs on an allowlisted GitHub-hosted label."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DRYRUN = (WORKFLOWS / "release-image-dryrun.yml").read_text(encoding="ascii")
IMAGE = (WORKFLOWS / "release-image.yml").read_text(encoding="ascii")
RUNNERS = ("ubuntu-24.04", "ubuntu-26.04")


def _job(text: str, name: str) -> str:
    lines = text.splitlines()
    start = lines.index(f"  {name}:")
    selected = [lines[start]]
    for line in lines[start + 1:]:
        if line and not line.startswith("   "):
            break
        selected.append(line)
    return "\n".join(selected)


def test_dry_run_triggers_nightly_on_release_paths_and_by_hand() -> None:
    triggers = DRYRUN.split("\npermissions:")[0]
    assert re.search(r'cron: "[^"]+"', triggers)
    assert "workflow_dispatch:" in triggers
    for path in ("deploy/**", ".github/workflows/release-image.yml",
                 ".github/workflows/release-image-dryrun.yml", ".github/workflows/publish-pypi.yml"):
        assert f'- "{path}"' in triggers
    assert "pull_request_target" not in DRYRUN


def test_dry_run_never_pushes_logs_in_or_attests() -> None:
    image = _job(DRYRUN, "image")
    assert "uses: ./.github/workflows/release-image.yml" in image
    assert "push: false" in image
    assert "push: true" not in DRYRUN
    for forbidden in ("docker/login-action", "secrets.", "attest-build-provenance"):
        assert forbidden not in DRYRUN
    # The write grants exist only so GitHub starts the call; they reach only release-image.yml's
    # publish job, which push: false skips, and the resolve job stays read-only.
    assert DRYRUN.count("packages: write") == 1 and "packages: write" in image
    resolve = _job(DRYRUN, "resolve")
    assert ": write" not in resolve
    publish_if = re.search(r"(?m)^    if: (.+)$", _job(IMAGE, "publish")).group(1)
    assert publish_if.startswith("${{ inputs.push && ")
    for job in ("validate", "verify"):
        assert ": write" not in _job(IMAGE, job).split("    steps:")[0]


def test_dry_run_covers_both_release_runners_without_fail_fast() -> None:
    image = _job(DRYRUN, "image")
    assert "runner: [ubuntu-24.04, ubuntu-26.04]" in image
    assert "fail-fast: false" in image
    assert "runner: ${{ matrix.runner }}" in image
    # scripts/release.py looks the jobs up by this name.
    assert "name: dry run ${{ matrix.runner }}" in image


def test_dry_run_uses_the_release_python_base_and_a_valid_version() -> None:
    assert 'read_python_base(Path("deploy/release-python-base.txt"))' in DRYRUN
    assert "version: 0.0.0-${{ github.run_number }}" in DRYRUN


def test_release_image_verify_job_runs_only_without_push_and_never_logs_in() -> None:
    verify = _job(IMAGE, "verify")
    assert "if: ${{ !inputs.push }}" in verify
    assert "--mode verify" in verify
    assert '--expected-identity-sha256 "$EXPECTED_IDENTITY_SHA256"' in verify
    assert "ref: ${{ needs.validate.outputs.commit_sha }}" in verify
    for forbidden in ("--mode publish", "--mode build", "docker/login-action", "secrets.",
                      "packages: write", "id-token: write", "attest", "environment:"):
        assert forbidden not in verify
    # The stage names scripts/release.py requires.
    assert "name: Verify sealed candidate without publishing" in verify
    assert "name: Build and seal candidate without publishing" in _job(IMAGE, "validate")


def test_every_release_image_job_runs_on_an_allowlisted_label() -> None:
    for name in ("validate", "verify", "publish"):
        job = _job(IMAGE, name)
        runs_on = re.search(r"(?m)^    runs-on: (.+)$", job).group(1)
        assert runs_on == "${{ inputs.runner == 'ubuntu-26.04' && 'ubuntu-26.04' || 'ubuntu-24.04' }}"
        steps = job.split("    steps:\n", 1)[1]
        assert steps.lstrip().startswith("- name: Refuse an unlisted runner")
        assert "ubuntu-24.04|ubuntu-26.04) ;;" in steps


def test_runner_input_defaults_to_ubuntu_24_04_on_both_triggers() -> None:
    triggers = IMAGE.split("\npermissions:")[0]
    assert triggers.count("      runner:\n") == 2
    assert triggers.count("        default: ubuntu-24.04\n") == 2
    for runner in RUNNERS:
        assert f"          - {runner}\n" in triggers


def test_matrix_legs_upload_distinct_validation_artifacts() -> None:
    assert ("release-image-validation-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${RUNNER_INPUT}"
            in _job(IMAGE, "validate"))


def test_publications_of_one_version_still_serialize_across_runners() -> None:
    assert ("group: release-image-${{ inputs.image }}-${{ inputs.version }}-"
            "${{ inputs.push && 'publish' || inputs.runner }}") in IMAGE


def test_build_failure_reports_the_image_store() -> None:
    validate = _job(IMAGE, "validate")
    failure = validate[validate.index("if (( build_status != 0 )); then"):]
    assert "docker version" in failure
    assert "docker info --format '{{.Driver}} {{json .DriverStatus}}'" in failure


def test_dry_run_actions_are_pinned_to_full_commit_ids() -> None:
    uses = re.findall(r"(?m)^\s+uses: ([^\s#]+)", DRYRUN)
    assert uses
    for action in uses:
        if action.startswith("./"):
            continue
        assert re.fullmatch(r"[^@]+/[^@]+@[0-9a-f]{40}", action)
