from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-image.yml"
TEXT = WORKFLOW.read_text(encoding="ascii")


def _block(header: str, indent: int) -> str:
    lines = TEXT.splitlines()
    marker = " " * indent + header + ":"
    start = lines.index(marker)
    selected = [lines[start]]
    for line in lines[start + 1 :]:
        if line and len(line) - len(line.lstrip(" ")) <= indent:
            break
        selected.append(line)
    return "\n".join(selected)


def _permission_names(block: str) -> set[str]:
    permission_block = _nested_block(block, "permissions", 4)
    return set(re.findall(r"(?m)^      ([a-z-]+): (?:read|write)$", permission_block))


def _nested_block(parent: str, header: str, indent: int) -> str:
    lines = parent.splitlines()
    marker = " " * indent + header + ":"
    start = lines.index(marker)
    selected = [lines[start]]
    for line in lines[start + 1 :]:
        if line and len(line) - len(line.lstrip(" ")) <= indent:
            break
        selected.append(line)
    return "\n".join(selected)


def test_workflow_is_ascii_yaml_text_without_a_byte_order_mark() -> None:
    raw = WORKFLOW.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    raw.decode("ascii")
    assert TEXT.startswith("name:")
    assert "\non:\n" in TEXT


def test_workflow_is_reusable_and_manually_dispatchable_with_bound_inputs() -> None:
    triggers = _block("on", 0)
    called = _nested_block(triggers, "workflow_call", 2)
    dispatched = _nested_block(triggers, "workflow_dispatch", 2)
    for trigger in (called, dispatched):
        inputs = _nested_block(trigger, "inputs", 4)
        for name in ("version", "image", "ref", "python_base", "push", "sbom_path", "scan_path"):
            assert re.search(rf"(?m)^      {name}:$", inputs)
    assert "Release version and image tag" in called
    assert "ref: ${{ inputs.ref || github.sha }}" in TEXT
    assert "--version \"$VERSION\"" in TEXT
    assert "--image \"$IMAGE\"" in TEXT


def test_permissions_are_least_privilege_and_publication_scoped() -> None:
    top_level_permissions = _block("permissions", 0)
    assert top_level_permissions.rstrip() == "permissions:\n  contents: read"

    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert _permission_names(validate) == {"contents"}
    assert _permission_names(publish) == {
        "attestations",
        "contents",
        "id-token",
        "packages",
    }
    assert "packages: write" not in validate
    assert TEXT.count("packages: write") == 1
    assert TEXT.count("id-token: write") == 1
    assert TEXT.count("attestations: write") == 1
    assert "pull-requests:" not in TEXT


def test_no_push_validation_is_separate_from_approved_publication() -> None:
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert "Build and validate release image without push" in validate
    assert "--push" not in validate
    assert "needs: validate" in publish
    assert "environment: menhir-release-publication" in publish
    assert "docker/login-action@" in publish
    assert "registry: ghcr.io" in publish
    assert publish.count("--push") == 1
    assert TEXT.count("--push") == 1


def test_release_builder_is_the_only_image_build_and_no_deploy_command_exists() -> None:
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert validate.count("python deploy/build_release_image.py") == 1
    assert publish.count("python deploy/build_release_image.py") == 1
    assert TEXT.count("python deploy/build_release_image.py") == 2
    for forbidden in (
        "docker build",
        "docker push",
        "docker compose",
        "kubectl ",
        "personal_deploy.py",
        "release_flow.py",
        "scp ",
        "ssh ",
    ):
        assert forbidden not in TEXT


def test_untrusted_pull_request_events_cannot_publish() -> None:
    triggers = _block("on", 0)
    publish = _block("publish", 2)
    assert "pull_request:" not in triggers
    assert "pull_request_target:" not in triggers
    condition = next(line.strip() for line in publish.splitlines() if line.strip().startswith("if:"))
    assert "inputs.push" in condition
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.event_name == 'push'" in condition
    assert "environment: menhir-release-publication" in publish


def test_metadata_optional_evidence_and_attestations_follow_publication() -> None:
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    for job in (validate, publish):
        assert "release-image-metadata.json" in job
        assert "${{ inputs.sbom_path }}" in job
        assert "${{ inputs.scan_path }}" in job
        assert "actions/upload-artifact@" in job

    push_at = publish.index("--push")
    image_attestation_at = publish.index("- name: Attest published image")
    metadata_attestation_at = publish.index("- name: Attest publication metadata")
    assert push_at < image_attestation_at < metadata_attestation_at
    assert "subject-digest: ${{ steps.release.outputs.registry_digest }}" in publish
    assert "subject-path: release-image-metadata.json" in publish
    assert "attest-build-provenance" not in validate
    assert TEXT.count("actions/attest-build-provenance@") == 2


def test_third_party_actions_are_pinned_to_full_commit_ids() -> None:
    uses = re.findall(r"(?m)^\s+uses: ([^\s#]+)", TEXT)
    assert uses
    for action in uses:
        owner_repo, separator, revision = action.partition("@")
        assert separator == "@"
        assert "/" in owner_repo
        assert re.fullmatch(r"[0-9a-f]{40}", revision)
