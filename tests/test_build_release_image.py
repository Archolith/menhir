from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_release_image", ROOT / "deploy" / "build_release_image.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

COMMIT = "b" * 40
IMAGE = "ghcr.io/archolith/menhir"
VERSION = "1.2.3-4"
IMAGE_TAG = f"{IMAGE}:{VERSION}"
IMAGE_ID = "sha256:" + "1" * 64
CONFIG_SHA256 = "2" * 64
ARCHIVE_SHA256 = "3" * 64
REGISTRY_DIGEST = "sha256:" + "4" * 64
PYTHON_BASE = "python@sha256:" + "5" * 64
SYFT_IMAGE = "docker.io/anchore/syft@sha256:" + "6" * 64
GRYPE_IMAGE = "docker.io/anchore/grype@sha256:" + "7" * 64


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True).encode("ascii")


def _syft_report(image_id: str = IMAGE_ID) -> bytes:
    return _json_bytes({
        "descriptor": {"name": "syft", "version": "1.20.0"},
        "source": {"type": "image", "target": {"id": image_id}},
        "schema": {
            "version": "16.0.0",
            "url": "https://raw.githubusercontent.com/anchore/syft/main/schema/json/schema-16.0.0.json",
        },
        "artifacts": [{"id": "pkg-1", "name": "menhir"}],
        "artifactRelationships": [],
    })


def _grype_report(
    *,
    image_id: str = IMAGE_ID,
    matches: list[str] | None = None,
    ignored_matches: list[str] | None = None,
) -> bytes:
    def findings(severities: list[str]) -> list[dict[str, Any]]:
        return [
            {"vulnerability": {"id": f"CVE-{number}", "severity": severity}}
            for number, severity in enumerate(severities, 1)
        ]

    return _json_bytes({
        "descriptor": {
            "name": "grype",
            "version": "0.90.0",
            "db": {"built": "2026-09-08T00:00:00Z", "checksum": "sha256:db"},
        },
        "source": {"type": "image", "target": {"imageID": image_id}},
        "matches": findings(matches or []),
        "ignoredMatches": findings(ignored_matches or []),
    })


def _wheelhouse(root: Path) -> Path:
    root.mkdir(parents=True)
    files = {
        "archolith_oauth-1.2.3-py3-none-any.whl": b"oauth-wheel",
        "menhir-1.2.3-py3-none-any.whl": b"menhir-wheel",
    }
    lines = []
    for name, body in files.items():
        (root / name).write_bytes(body)
        lines.append(f"{hashlib.sha256(body).hexdigest()}  {name}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")
    return root


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )


def _validation_bundle(
    root: Path,
    *,
    sbom_image_id: str = IMAGE_ID,
    evidence_archive_sha256: str | None = None,
) -> tuple[SimpleNamespace, dict[str, Any], dict[str, Any]]:
    root.mkdir()
    evidence_root = root / "release-image-evidence"
    evidence_root.mkdir()
    archive = root / "release-image.tar"
    archive.write_bytes(b"sealed release image")
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    sbom_raw = _syft_report(sbom_image_id)
    scan_raw = _grype_report(matches=["High"], ignored_matches=["Low"])
    sbom_path = root / MODULE.EVIDENCE_FILES["sbom"]
    scan_path = root / MODULE.EVIDENCE_FILES["vulnerability_scan"]
    sbom_path.write_bytes(sbom_raw)
    scan_path.write_bytes(scan_raw)
    subject = {
        "image_id": IMAGE_ID,
        "image_archive_sha256": evidence_archive_sha256 or archive_sha256,
    }
    evidence = {
        "required": True,
        "sbom": {
            "artifact_path": MODULE.EVIDENCE_FILES["sbom"],
            "sha256": hashlib.sha256(sbom_raw).hexdigest(),
            "scanner_image": SYFT_IMAGE,
            "subject": subject,
            "validation": MODULE.validate_sbom(sbom_raw, sbom_image_id),
        },
        "vulnerability_scan": {
            "artifact_path": MODULE.EVIDENCE_FILES["vulnerability_scan"],
            "sha256": hashlib.sha256(scan_raw).hexdigest(),
            "scanner_image": GRYPE_IMAGE,
            "subject": subject,
            "validation": MODULE.validate_vulnerability_scan(scan_raw, IMAGE_ID),
        },
    }
    labels = {
        "commit": COMMIT,
        "version": VERSION,
        "wheel_manifest_sha256": "8" * 64,
        "oauth_wheel_sha256": "9" * 64,
    }
    metadata = {
        "schema": 3,
        "source_commit": COMMIT,
        "image_tag": IMAGE_TAG,
        "image_id": IMAGE_ID,
        "config_sha256": CONFIG_SHA256,
        "image_archive_sha256": archive_sha256,
        "python_base": PYTHON_BASE,
        "labels": labels,
        "evidence": evidence,
    }
    metadata_path = root / "release-image-metadata.json"
    _write_json(metadata_path, metadata)
    identity = {
        "schema": 2,
        "source_commit": COMMIT,
        "image_tag": IMAGE_TAG,
        "image_id": IMAGE_ID,
        "config_sha256": CONFIG_SHA256,
        "image_archive": {
            "artifact_path": "release-image.tar",
            "sha256": archive_sha256,
        },
        "metadata": {
            "artifact_path": "release-image-metadata.json",
            "sha256": MODULE.sha256_file(metadata_path),
        },
        "evidence": {
            "sbom": evidence["sbom"]["sha256"],
            "vulnerability_scan": evidence["vulnerability_scan"]["sha256"],
        },
    }
    identity_path = root / "release-image-identity.json"
    _write_json(identity_path, identity)
    args = SimpleNamespace(
        repo=root,
        version=VERSION,
        image=IMAGE,
        python_base=PYTHON_BASE,
        metadata=metadata_path,
        identity=identity_path,
        image_archive=archive,
        expected_identity_sha256=MODULE.sha256_file(identity_path),
        output=None,
    )
    return args, metadata, identity


def _publication_metadata() -> dict[str, Any]:
    return {
        "source_commit": COMMIT,
        "image_tag": IMAGE_TAG,
        "image_id": IMAGE_ID,
        "config_sha256": CONFIG_SHA256,
        "image_archive_sha256": ARCHIVE_SHA256,
        "labels": {"commit": COMMIT, "version": VERSION},
    }


def test_derives_and_verifies_oauth_wheel_digest(tmp_path: Path) -> None:
    wheelhouse = _wheelhouse(tmp_path / "wheelhouse")
    assert MODULE.oauth_wheel_sha256(wheelhouse) == hashlib.sha256(b"oauth-wheel").hexdigest()


def test_rejects_changed_wheel_bytes(tmp_path: Path) -> None:
    wheelhouse = _wheelhouse(tmp_path / "wheelhouse")
    next(wheelhouse.glob("archolith_oauth*.whl")).write_bytes(b"changed")
    with pytest.raises(MODULE.BuildImageError, match="digest mismatch"):
        MODULE.oauth_wheel_sha256(wheelhouse)


def test_build_command_contains_all_derived_labels(tmp_path: Path) -> None:
    command = MODULE.build_command(
        repo=tmp_path,
        dockerfile=tmp_path / "deploy" / "Dockerfile",
        image_tag=IMAGE_TAG,
        python_base=PYTHON_BASE,
        commit=COMMIT,
        version=VERSION,
        wheel_manifest="c" * 64,
        oauth_wheel="d" * 64,
    )
    joined = "\n".join(command)
    assert f"RELEASE_COMMIT={COMMIT}" in joined
    assert f"RELEASE_VERSION={VERSION}" in joined
    assert "WHEEL_MANIFEST_SHA256=" + "c" * 64 in joined
    assert "OAUTH_WHEEL_SHA256=" + "d" * 64 in joined
    assert command[-2:] == [IMAGE_TAG, str(tmp_path)]


@pytest.mark.parametrize(
    ("scanner", "image"),
    [
        ("syft", SYFT_IMAGE),
        ("syft", "anchore/syft@sha256:" + "a" * 64),
        ("grype", GRYPE_IMAGE),
        ("grype", "anchore/grype@sha256:" + "a" * 64),
    ],
)
def test_scanners_must_be_official_images_pinned_by_digest(scanner: str, image: str) -> None:
    assert MODULE._validate_scanner_image(image, scanner) == image


@pytest.mark.parametrize(
    ("scanner", "image"),
    [
        ("syft", "anchore/syft:latest"),
        ("syft", "ghcr.io/example/syft@sha256:" + "a" * 64),
        ("syft", "docker.io/anchore/grype@sha256:" + "a" * 64),
        ("grype", "docker.io/anchore/grype@sha256:bad"),
    ],
)
def test_rejects_tagged_unofficial_or_malformed_scanner_images(
    scanner: str, image: str,
) -> None:
    with pytest.raises(MODULE.BuildImageError, match="official image pinned"):
        MODULE._validate_scanner_image(image, scanner)


def test_syft_json_must_describe_the_exact_candidate_and_have_packages() -> None:
    summary = MODULE.validate_sbom(_syft_report(), IMAGE_ID)
    assert summary == {
        "format": "syft-json",
        "scanner_version": "1.20.0",
        "source_image_id": IMAGE_ID,
        "package_count": 1,
    }
    with pytest.raises(MODULE.BuildImageError, match="expected candidate image"):
        MODULE.validate_sbom(_syft_report("sha256:" + "a" * 64), IMAGE_ID)
    malformed = json.loads(_syft_report())
    malformed["artifacts"] = []
    with pytest.raises(MODULE.BuildImageError, match="contains no packages"):
        MODULE.validate_sbom(_json_bytes(malformed), IMAGE_ID)


def test_grype_json_is_structurally_validated_and_summarized() -> None:
    summary = MODULE.validate_vulnerability_scan(
        _grype_report(matches=["High", "Medium"], ignored_matches=["Low"]), IMAGE_ID,
    )
    assert summary["format"] == "grype-json"
    assert summary["source_image_id"] == IMAGE_ID
    assert summary["severity_counts"] == {
        "Unknown": 0,
        "Negligible": 0,
        "Low": 1,
        "Medium": 1,
        "High": 1,
        "Critical": 0,
    }
    assert summary["policy"] == {"maximum_allowed_severity": "High", "passed": True}


@pytest.mark.parametrize("field", ["matches", "ignoredMatches"])
def test_critical_findings_fail_policy_even_when_ignored(field: str) -> None:
    report = json.loads(_grype_report())
    report[field] = [{"vulnerability": {"id": "CVE-critical", "severity": "Critical"}}]
    with pytest.raises(MODULE.BuildImageError, match="rejected 1 critical"):
        MODULE.validate_vulnerability_scan(_json_bytes(report), IMAGE_ID)


def test_grype_requires_active_matches_but_allows_omitted_empty_ignored_matches() -> None:
    report = json.loads(_grype_report())
    del report["matches"]
    with pytest.raises(MODULE.BuildImageError, match="matches is malformed"):
        MODULE.validate_vulnerability_scan(_json_bytes(report), IMAGE_ID)
    report = json.loads(_grype_report())
    del report["ignoredMatches"]
    assert MODULE.validate_vulnerability_scan(_json_bytes(report), IMAGE_ID)[
        "severity_counts"
    ]["Critical"] == 0


def test_scanners_read_the_sealed_archive_without_invoking_real_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "release-image.tar"
    archive.write_bytes(b"archive")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        scanner = "syft" if SYFT_IMAGE in command else "grype"
        stdout = _syft_report() if scanner == "syft" else _grype_report()
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    assert MODULE._run_scanner(scanner="syft", image=SYFT_IMAGE, archive=archive) == _syft_report()
    assert MODULE._run_scanner(scanner="grype", image=GRYPE_IMAGE, archive=archive) == _grype_report()

    scanner_calls = [call for call in calls if call[0][1] == "run"]
    volume_calls = [call for call in calls if call[0][1] == "volume"]
    for (command, kwargs), scanner_image in zip(
        scanner_calls, (SYFT_IMAGE, GRYPE_IMAGE), strict=True,
    ):
        assert command[:4] == ["docker", "run", "--rm", "--pull"]
        assert "never" in command
        assert "--read-only" in command
        assert "no-new-privileges=true" in command
        assert scanner_image in command
        assert f"docker-archive:/candidate/{archive.name}" in command
        assert kwargs == {"check": False, "capture_output": True}
    assert "none" in scanner_calls[0][0]
    assert len(volume_calls) == 2
    assert volume_calls[0][0][2] == "create"
    assert volume_calls[1][0][2] == "rm"
    assert volume_calls[0][0][-1] == volume_calls[1][0][-1]
    assert "type=volume" in " ".join(scanner_calls[1][0])


def test_generated_evidence_is_fixed_path_hash_and_archive_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "release-image.tar"
    archive.write_bytes(b"sealed")
    scanned_archives: list[Path] = []

    def fake_scanner(*, scanner: str, image: str, archive: Path) -> bytes:
        scanned_archives.append(archive)
        assert image == {"syft": SYFT_IMAGE, "grype": GRYPE_IMAGE}[scanner]
        return _syft_report() if scanner == "syft" else _grype_report(matches=["High"])

    monkeypatch.setattr(MODULE, "_run_scanner", fake_scanner)
    evidence = MODULE.generate_evidence(
        artifact_root=tmp_path,
        archive=archive,
        archive_sha256=ARCHIVE_SHA256,
        image_id=IMAGE_ID,
        syft_image=SYFT_IMAGE,
        grype_image=GRYPE_IMAGE,
    )

    assert scanned_archives == [archive, archive]
    assert set(evidence) == {"required", "sbom", "vulnerability_scan"}
    assert evidence["required"] is True
    for kind, relative in MODULE.EVIDENCE_FILES.items():
        entry = evidence[kind]
        evidence_path = tmp_path / relative
        assert entry["artifact_path"] == relative
        assert entry["sha256"] == MODULE.sha256_file(evidence_path)
        assert entry["subject"] == {
            "image_id": IMAGE_ID,
            "image_archive_sha256": ARCHIVE_SHA256,
        }


def test_build_emits_validation_schema_3_and_identity_schema_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "Dockerfile").write_text("FROM scratch\n", encoding="ascii")
    _wheelhouse(deploy / "wheelhouse")
    output = tmp_path / "release-image-metadata.json"
    identity_path = tmp_path / "release-image-identity.json"
    archive = tmp_path / "release-image.tar"
    args = SimpleNamespace(
        repo=tmp_path,
        dockerfile=Path("deploy/Dockerfile"),
        wheelhouse=Path("deploy/wheelhouse"),
        syft_image=SYFT_IMAGE,
        grype_image=GRYPE_IMAGE,
        expected_commit=COMMIT,
        image=IMAGE,
        version=VERSION,
        python_base=PYTHON_BASE,
        output=output,
        identity=identity_path,
        image_archive=archive,
    )

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["docker", "image", "save"]:
            Path(command[command.index("--output") + 1]).write_bytes(b"sealed release image")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(MODULE, "git_commit", lambda _repo: COMMIT)
    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    monkeypatch.setattr(
        MODULE,
        "inspect_image",
        lambda _tag, expected: (IMAGE_ID, expected, CONFIG_SHA256),
    )
    monkeypatch.setattr(
        MODULE,
        "_run_scanner",
        lambda *, scanner, **_kwargs: _syft_report() if scanner == "syft" else _grype_report(),
    )

    metadata = MODULE.create_validation_bundle(args)
    identity = json.loads(identity_path.read_text(encoding="ascii"))
    assert metadata["schema"] == 3
    assert identity["schema"] == 2
    assert identity["metadata"]["sha256"] == MODULE.sha256_file(output)
    assert identity["image_archive"]["sha256"] == MODULE.sha256_file(archive)
    assert metadata["evidence"]["sbom"]["subject"]["image_archive_sha256"] == MODULE.sha256_file(archive)


def test_verification_revalidates_reports_and_reuses_the_exact_sealed_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, metadata, identity = _validation_bundle(tmp_path / "release-candidate")
    calls: list[list[str]] = []
    monkeypatch.setattr(MODULE, "git_commit", lambda _repo: COMMIT)
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(
        MODULE,
        "inspect_image",
        lambda _tag, _expected: (IMAGE_ID, metadata["labels"], CONFIG_SHA256),
    )

    actual_metadata, actual_identity = MODULE.verify_validation_bundle(args, load_image=True)
    assert actual_metadata == metadata
    assert actual_identity == identity
    assert calls == [["docker", "image", "load", "--input", str(args.image_archive.resolve())]]


def test_verification_rejects_an_sbom_for_a_different_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _metadata, _identity = _validation_bundle(
        tmp_path / "release-candidate", sbom_image_id="sha256:" + "a" * 64,
    )
    monkeypatch.setattr(MODULE, "git_commit", lambda _repo: COMMIT)
    with pytest.raises(MODULE.BuildImageError, match="expected candidate image"):
        MODULE.verify_validation_bundle(args, load_image=False)


def test_verification_rejects_evidence_bound_to_another_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _metadata, _identity = _validation_bundle(
        tmp_path / "release-candidate", evidence_archive_sha256="a" * 64,
    )
    monkeypatch.setattr(MODULE, "git_commit", lambda _repo: COMMIT)
    with pytest.raises(MODULE.BuildImageError, match="wrong candidate subject"):
        MODULE.verify_validation_bundle(args, load_image=False)


def test_remote_manifest_rejects_malformed_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.CompletedProcess([], 0, stdout="Digest: sha256:bad\n", stderr="")
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(MODULE.BuildImageError, match="malformed"):
        MODULE.remote_manifest_digest(f"{IMAGE}:candidate-{ARCHIVE_SHA256}")


def test_existing_archive_bound_candidate_is_verified_without_a_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _publication_metadata()
    candidate_tag = f"{IMAGE}:candidate-{ARCHIVE_SHA256}"
    verified: list[dict[str, Any]] = []
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(MODULE, "remote_manifest_digest", lambda tag: REGISTRY_DIGEST)
    monkeypatch.setattr(
        MODULE,
        "_verify_remote_candidate",
        lambda **kwargs: verified.append(kwargs),
    )
    monkeypatch.setattr(
        MODULE,
        "_push_digest",
        lambda _tag: pytest.fail("an existing candidate must not be pushed"),
    )

    digest, idempotent, actual_tag = MODULE.publish_candidate(metadata)
    assert (digest, idempotent, actual_tag) == (REGISTRY_DIGEST, True, candidate_tag)
    assert verified == [{"repository": IMAGE, "digest": REGISTRY_DIGEST, "metadata": metadata}]


def test_remote_candidate_verification_pulls_and_checks_the_sealed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _publication_metadata()
    digest_ref = f"{IMAGE}@{REGISTRY_DIGEST}"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(
        MODULE,
        "inspect_image",
        lambda image, expected: (IMAGE_ID, expected, CONFIG_SHA256),
    )
    monkeypatch.setattr(MODULE, "remote_manifest_digest", lambda image: REGISTRY_DIGEST)

    MODULE._verify_remote_candidate(
        repository=IMAGE, digest=REGISTRY_DIGEST, metadata=metadata,
    )
    assert calls == [["docker", "pull", digest_ref]]


def test_new_candidate_uses_full_archive_sha_and_never_moves_the_version_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _publication_metadata()
    candidate_tag = f"{IMAGE}:candidate-{ARCHIVE_SHA256}"
    remote_results = iter((None, None, REGISTRY_DIGEST))
    tagged: list[list[str]] = []
    pushed: list[str] = []
    verified: list[dict[str, Any]] = []
    monkeypatch.setattr(MODULE.subprocess, "run", lambda command, **_kwargs: tagged.append(command))
    monkeypatch.setattr(MODULE, "remote_manifest_digest", lambda _tag: next(remote_results))
    monkeypatch.setattr(MODULE, "_push_digest", lambda tag: pushed.append(tag) or REGISTRY_DIGEST)
    monkeypatch.setattr(MODULE, "_verify_remote_candidate", lambda **kwargs: verified.append(kwargs))

    digest, idempotent, actual_tag = MODULE.publish_candidate(metadata)
    assert (digest, idempotent, actual_tag) == (REGISTRY_DIGEST, False, candidate_tag)
    assert tagged == [["docker", "image", "tag", IMAGE_TAG, candidate_tag]]
    assert pushed == [candidate_tag]
    assert IMAGE_TAG not in pushed
    assert verified == [{"repository": IMAGE, "digest": REGISTRY_DIGEST, "metadata": metadata}]


def test_candidate_publication_fails_if_registry_tag_changes_during_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raced_digest = "sha256:" + "a" * 64
    results = iter((None, None, raced_digest))
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(MODULE, "remote_manifest_digest", lambda _tag: next(results))
    monkeypatch.setattr(MODULE, "_push_digest", lambda _tag: REGISTRY_DIGEST)
    monkeypatch.setattr(
        MODULE,
        "_verify_remote_candidate",
        lambda **_kwargs: pytest.fail("a raced candidate must not be trusted"),
    )
    with pytest.raises(MODULE.BuildImageError, match="changed during registry verification"):
        MODULE.publish_candidate(_publication_metadata())


def test_publication_schema_2_emits_digest_only_image_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _publication_metadata()
    identity = {"evidence": {"sbom": "8" * 64, "vulnerability_scan": "9" * 64}}
    output = tmp_path / "release-image-publication.json"
    args = SimpleNamespace(expected_identity_sha256="a" * 64, output=output)
    candidate_tag = f"{IMAGE}:candidate-{ARCHIVE_SHA256}"
    monkeypatch.setattr(
        MODULE, "verify_validation_bundle", lambda _args, load_image: (metadata, identity),
    )
    monkeypatch.setattr(
        MODULE,
        "publish_candidate",
        lambda _metadata: (REGISTRY_DIGEST, False, candidate_tag),
    )

    publication = MODULE.publish_bundle(args)
    assert publication["schema"] == 2
    assert publication["image_ref"] == f"{IMAGE}@{REGISTRY_DIGEST}"
    assert publication["image_tag"] == IMAGE_TAG
    assert publication["candidate_tag"] == candidate_tag
    assert publication["candidate_ref"] == f"{candidate_tag}@{REGISTRY_DIGEST}"
    assert json.loads(output.read_text(encoding="ascii")) == publication
