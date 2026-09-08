from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-image.yml"
TEXT = WORKFLOW.read_text(encoding="ascii")
BUILDER_TEXT = (ROOT / "deploy" / "build_release_image.py").read_text(encoding="ascii")


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


def _permission_names(block: str) -> set[str]:
    permission_block = _nested_block(block, "permissions", 4)
    return set(re.findall(r"(?m)^      ([a-z-]+): (?:read|write)$", permission_block))


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
    assert "--version \"$VERSION\"" in TEXT
    assert "--image \"$IMAGE\"" in TEXT


def test_permissions_are_least_privilege_and_publication_scoped() -> None:
    assert _block("permissions", 0).rstrip() == "permissions:\n  contents: read"
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert _permission_names(validate) == {"contents"}
    assert _permission_names(publish) == {"attestations", "contents", "id-token", "packages"}
    assert "packages: write" not in validate
    assert TEXT.count("packages: write") == 1
    assert TEXT.count("id-token: write") == 1
    assert TEXT.count("attestations: write") == 1
    assert "pull-requests:" not in TEXT


def test_publication_checks_out_the_exact_commit_emitted_by_validation() -> None:
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert "commit_sha: ${{ steps.source.outputs.commit_sha }}" in validate
    assert '["git", "rev-parse", "HEAD"]' in validate
    assert "ref: ${{ needs.validate.outputs.commit_sha }}" in publish
    assert "ref: ${{ inputs.ref || github.sha }}" not in publish


def test_publication_loads_the_sealed_candidate_and_never_rebuilds() -> None:
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert "--mode build" in validate
    assert "--image-archive release-image.tar" in validate
    assert "actions/upload-artifact@" in validate
    assert "actions/download-artifact@" in publish
    assert "--mode verify" in publish
    assert "--mode publish" in publish
    assert "--image-archive release-candidate/release-image.tar" in publish
    assert "--mode build" not in publish
    assert "docker build" not in publish


def test_identity_and_loaded_image_are_verified_before_registry_login() -> None:
    publish = _block("publish", 2)
    verify_at = publish.index("Verify identity, evidence, and loaded image before login")
    login_at = publish.index("Log in to GitHub Container Registry")
    push_at = publish.index("Publish candidate without moving an existing release tag")
    assert verify_at < login_at < push_at
    assert "identity_sha256: ${{ steps.identity.outputs.sha256 }}" in TEXT
    assert "--expected-identity-sha256 \"$EXPECTED_IDENTITY_SHA256\"" in publish
    assert "release-image-identity.json" in publish
    assert "release-image-metadata.json" in publish


def test_builder_binds_and_revalidates_the_entire_downloaded_candidate() -> None:
    assert "sha256_file(identity_path) != expected_identity" in BUILDER_TEXT
    assert "downloaded validation metadata digest does not match identity" in BUILDER_TEXT
    assert "downloaded image archive digest does not match identity" in BUILDER_TEXT
    assert "loaded image ID does not match validation identity" in BUILDER_TEXT
    assert "loaded image config does not match validation identity" in BUILDER_TEXT
    assert "loaded image labels do not match validation metadata" in BUILDER_TEXT
    assert "downloaded {kind} evidence digest does not match identity" in BUILDER_TEXT


def test_push_requires_hash_bound_sbom_and_scan_evidence() -> None:
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert 'if [[ "$PUSH" == "true" ]]' in validate
    assert "evidence_args+=(--require-evidence)" in validate
    assert "evidence_args+=(--sbom \"$SBOM_PATH\")" in validate
    assert "evidence_args+=(--scan \"$SCAN_PATH\")" in validate
    assert "--require-evidence" in publish
    assert "release-image-evidence/" in TEXT


def test_release_tag_publication_is_serialized_and_immutable() -> None:
    publish = _block("publish", 2)
    concurrency = _block("concurrency", 0)
    assert "inputs.image" in concurrency
    assert "inputs.version" in concurrency
    assert "cancel-in-progress: false" in concurrency
    assert "without moving an existing release tag" in publish
    assert "release tag already exists at" in BUILDER_TEXT
    assert "return candidate_digest, True, candidate_tag" in BUILDER_TEXT


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


def test_attestations_follow_publication_and_use_verified_digest() -> None:
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    publication_at = publish.index("Publish candidate without moving an existing release tag")
    image_attestation_at = publish.index("- name: Attest published image")
    metadata_attestation_at = publish.index("- name: Attest publication metadata")
    assert publication_at < image_attestation_at < metadata_attestation_at
    assert "subject-digest: ${{ steps.release.outputs.registry_digest }}" in publish
    assert "subject-path: release-image-publication.json" in publish
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
