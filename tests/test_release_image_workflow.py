from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-image.yml"
TEXT = WORKFLOW.read_text(encoding="ascii")
BUILDER_TEXT = (ROOT / "deploy" / "build_release_image.py").read_text(encoding="ascii")

SYFT_DEFAULT = (
    "docker.io/anchore/syft@sha256:"
    "600896ff278677fb13b16dc35999452f4e79636fc098c9fee6ce3cffef9858e2"
)
GRYPE_DEFAULT = (
    "docker.io/anchore/grype@sha256:"
    "461c87fefcd20d133f4d99db4623608637eecb439045f9fc8d6eebbb4149e766"
)


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
        for name in (
            "version", "image", "ref", "python_base", "push", "syft_image", "grype_image",
        ):
            assert re.search(rf"(?m)^      {name}:$", inputs)
        assert "sbom_path:" not in inputs
        assert "scan_path:" not in inputs
    assert '--version "$VERSION"' in TEXT
    assert '--image "$IMAGE"' in TEXT


def test_scanner_inputs_have_reviewed_full_digest_defaults_and_allow_overrides() -> None:
    triggers = _block("on", 0)
    for trigger_name in ("workflow_call", "workflow_dispatch"):
        trigger = _nested_block(triggers, trigger_name, 2)
        inputs = _nested_block(trigger, "inputs", 4)
        for input_name, expected, version in (
            ("syft_image", SYFT_DEFAULT, "v1.51.1"),
            ("grype_image", GRYPE_DEFAULT, "v0.118.0"),
        ):
            scanner = _nested_block(inputs, input_name, 6)
            assert "required: false" in scanner
            assert "type: string" in scanner
            assert f'default: "{expected}"' in scanner
            assert version in scanner
            assert re.fullmatch(
                r"docker\.io/anchore/(?:syft|grype)@sha256:[0-9a-f]{64}", expected,
            )

    validate = _block("validate", 2)
    assert "SYFT_IMAGE: ${{ inputs.syft_image }}" in validate
    assert "GRYPE_IMAGE: ${{ inputs.grype_image }}" in validate
    assert '--syft-image "$SYFT_IMAGE"' in validate
    assert '--grype-image "$GRYPE_IMAGE"' in validate


def test_validation_builds_a_frozen_wheelhouse_from_the_clean_checkout() -> None:
    validate = _block("validate", 2)
    checkout_at = validate.index("Checkout requested source")
    source_at = validate.index("Resolve exact validation commit")
    wheelhouse_at = validate.index("Build locked offline wheelhouse")
    image_at = validate.index("Build, validate, and seal release image")
    assert checkout_at < source_at < wheelhouse_at < image_at
    assert "ref: ${{ inputs.ref || github.sha }}" in validate
    assert "persist-credentials: false" in validate
    assert "enable-cache: false" in validate
    assert "rm -rf -- deploy/wheelhouse" in validate
    assert "uv export \\" in validate
    assert "--frozen" in validate
    assert "--no-group dev" in validate
    assert "--no-emit-project" in validate
    assert "python -m pip wheel" in validate
    assert "--no-cache-dir" in validate
    assert "--no-deps" in validate
    assert "uv build" not in validate
    assert "--no-hashes" not in validate
    assert validate.count("--require-hashes") >= 3
    assert "build-backend-requirements.txt" in validate
    assert "setuptools==83.0.0 --hash=sha256:" in validate
    assert "wheel==0.46.2 --hash=sha256:" in validate
    assert "--no-build-isolation" in validate
    assert "export PIP_NO_INDEX=1" in validate
    assert "git archive --format=tar" in validate
    assert "offline wheelhouse closure is missing" in validate
    assert "export PYTHONHASHSEED=0" in validate
    assert "export TZ=UTC" in validate
    assert 'wheelhouse / "SHA256SUMS"' in validate
    assert '--expected-commit "$SOURCE_COMMIT"' in validate
    assert '["diff", "--quiet"]' in BUILDER_TEXT
    assert '["diff", "--cached", "--quiet"]' in BUILDER_TEXT


def test_scanners_are_official_digest_pins_acquired_before_the_build() -> None:
    validate = _block("validate", 2)
    scanner_at = validate.index("Acquire digest-pinned scanners")
    build_at = validate.index("Build, validate, and seal release image")
    assert scanner_at < build_at
    assert '"GRYPE_IMAGE": {"anchore/grype", "docker.io/anchore/grype"}' in validate
    assert '"SYFT_IMAGE": {"anchore/syft", "docker.io/anchore/syft"}' in validate
    assert 're.fullmatch(r"sha256:[0-9a-f]{64}", digest)' in validate
    assert "must use the official image pinned by digest" in validate
    assert 'docker pull "$SYFT_IMAGE"' in validate
    assert 'docker pull "$GRYPE_IMAGE"' in validate


def test_validation_generates_and_uploads_structurally_checked_evidence() -> None:
    validate = _block("validate", 2)
    assert "--mode build" in validate
    assert "--syft-image" in validate
    assert "--grype-image" in validate
    assert "release-image-evidence/" in validate
    assert "release-image.tar" in validate
    assert '"docker-archive:/candidate/{archive.name}"' in BUILDER_TEXT
    assert '"artifacts"' in BUILDER_TEXT
    assert '"artifactRelationships"' in BUILDER_TEXT
    assert 'for field in ("matches", "ignoredMatches")' in BUILDER_TEXT
    assert 'if counts["Critical"]:' in BUILDER_TEXT
    assert '"image_archive_sha256": archive_sha256' in BUILDER_TEXT


def test_metadata_schema_versions_are_validation_3_identity_2_publication_2() -> None:
    assert re.search(r"metadata = \{\n\s+\"schema\": 3,", BUILDER_TEXT)
    assert re.search(r"identity = \{\n\s+\"schema\": 2,", BUILDER_TEXT)
    assert re.search(r"publication = \{\n\s+\"schema\": 2,", BUILDER_TEXT)
    publish = _block("publish", 2)
    assert 'if metadata.get("schema") != 2:' in publish


def test_permissions_and_registry_credentials_are_publication_only() -> None:
    assert _block("permissions", 0).rstrip() == "permissions:\n  contents: read"
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert _permission_names(validate) == {"contents"}
    assert _permission_names(publish) == {"attestations", "contents", "id-token", "packages"}
    assert "packages: write" not in validate
    assert "id-token: write" not in validate
    assert "attestations: write" not in validate
    assert "docker/login-action@" not in validate
    assert "secrets.GITHUB_TOKEN" not in validate
    assert "docker/login-action@" in publish
    assert "secrets.GITHUB_TOKEN" in publish
    assert TEXT.count("persist-credentials: false") == 2
    assert TEXT.count("packages: write") == 1
    assert TEXT.count("id-token: write") == 1
    assert TEXT.count("attestations: write") == 1


def test_publication_checks_out_the_exact_commit_emitted_by_validation() -> None:
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert "commit_sha: ${{ steps.source.outputs.commit_sha }}" in validate
    assert '["git", "rev-parse", "HEAD"]' in validate
    assert "ref: ${{ needs.validate.outputs.commit_sha }}" in publish
    assert "ref: ${{ inputs.ref || github.sha }}" not in publish


def test_publication_reuses_the_sealed_archive_and_never_rebuilds() -> None:
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert "--image-archive release-image.tar" in validate
    assert "actions/upload-artifact@" in validate
    assert "actions/download-artifact@" in publish
    assert "--mode verify" in publish
    assert "--mode publish" in publish
    assert publish.count("--image-archive release-candidate/release-image.tar") == 2
    assert "--mode build" not in publish
    assert "pip wheel" not in publish
    assert "uv build" not in publish
    assert "docker build" not in publish


def test_identity_and_loaded_image_are_verified_before_registry_login() -> None:
    publish = _block("publish", 2)
    verify_at = publish.index("Verify identity, evidence, and loaded image before login")
    login_at = publish.index("Log in to GitHub Container Registry")
    publication_at = publish.index("Publish exact candidate digest")
    assert verify_at < login_at < publication_at
    assert "identity_sha256: ${{ steps.identity.outputs.sha256 }}" in TEXT
    assert '--expected-identity-sha256 "$EXPECTED_IDENTITY_SHA256"' in publish
    assert "release-image-identity.json" in publish
    assert "release-image-metadata.json" in publish


def test_candidate_tag_uses_full_archive_sha_and_output_ref_is_digest_only() -> None:
    publish = _block("publish", 2)
    assert 'expected_candidate_tag = f"{os.environ[\'EXPECTED_IMAGE\']}:candidate-{archive_sha256}"' in publish
    assert 'expected = f"{os.environ[\'EXPECTED_IMAGE\']}@{digest}"' in publish
    assert 'expected_image_tag = f"{os.environ[\'EXPECTED_IMAGE\']}:{os.environ[\'EXPECTED_VERSION\']}"' in publish
    assert "candidate-{metadata['image_archive_sha256']}" in BUILDER_TEXT
    assert "_push_digest(candidate_tag)" in BUILDER_TEXT
    assert "_push_digest(image_tag)" not in BUILDER_TEXT
    assert "image_archive_sha256'][:" not in BUILDER_TEXT


def test_existing_candidate_is_verified_and_registry_races_fail_closed() -> None:
    assert "if existing is not None:" in BUILDER_TEXT
    assert BUILDER_TEXT.count("_verify_remote_candidate(") >= 4
    assert 'subprocess.run(["docker", "pull", digest_ref], check=True)' in BUILDER_TEXT
    assert "registry candidate image ID differs from the sealed candidate" in BUILDER_TEXT
    assert "registry candidate config differs from the sealed candidate" in BUILDER_TEXT
    assert "digest-pinned registry candidate did not verify" in BUILDER_TEXT
    assert "candidate tag changed during registry verification" in BUILDER_TEXT


def test_release_candidate_publication_is_serialized() -> None:
    concurrency = _block("concurrency", 0)
    assert "inputs.image" in concurrency
    assert "inputs.version" in concurrency
    assert "cancel-in-progress: false" in concurrency


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
    publication_at = publish.index("Publish exact candidate digest")
    bind_at = publish.index("Bind publication outputs to verified metadata")
    image_attestation_at = publish.index("- name: Attest published image")
    metadata_attestation_at = publish.index("- name: Attest publication metadata")
    assert publication_at < bind_at < image_attestation_at < metadata_attestation_at
    assert "subject-digest: ${{ steps.release.outputs.registry_digest }}" in publish
    assert "subject-path: release-image-publication.json" in publish
    assert "attest-build-provenance" not in validate
    assert TEXT.count("actions/attest-build-provenance@") == 2


def test_attestation_is_verified_against_canonical_repo_and_source_sha() -> None:
    validate = _block("validate", 2)
    publish = _block("publish", 2)
    assert "if: $" + "{{ github.repository == 'Archolith/menhir' }}" in validate
    assert "checkout origin is not the canonical Archolith/menhir repository" in validate
    assert "Capture and verify canonical GitHub attestation trust" in publish
    assert "gh attestation trusted-root" in publish
    assert '--repo "$SOURCE_REPOSITORY"' in publish
    assert '--source-digest "$SOURCE_COMMIT"' in publish
    assert "--signer-repo" not in publish
    assert '--signer-workflow "$SOURCE_REPOSITORY/.github/workflows/release-image.yml"' in publish
    assert "--custom-trusted-root release-image-attestation-trusted-root.jsonl" in publish
    assert "--deny-self-hosted-runners" in publish
    assert publish.index("Capture and verify canonical GitHub attestation trust") < publish.index(
        "Upload publication metadata and bound evidence"
    )


def test_third_party_actions_are_pinned_to_full_commit_ids() -> None:
    uses = re.findall(r"(?m)^\s+uses: ([^\s#]+)", TEXT)
    assert uses
    for action in uses:
        owner_repo, separator, revision = action.partition("@")
        assert separator == "@"
        assert "/" in owner_repo
        assert re.fullmatch(r"[0-9a-f]{40}", revision)
