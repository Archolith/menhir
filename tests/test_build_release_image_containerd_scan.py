"""#615: scanner evidence on the containerd image store (Docker 29, ubuntu-26.04 runners).

There `docker image inspect` reports the stored manifest digest as the image ID, and
`docker save` writes a Docker v2 manifest into the archive's index. Syft and Grype scan the
archive and report the config digest. A report is accepted only when it names the config of
the manifest whose hash-checked digest is the candidate's image ID.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from tests.test_build_release_image import (
    GRYPE_IMAGE,
    MODULE,
    SYFT_IMAGE,
    _grype_report,
    _json_bytes,
    _syft_report,
)

CONFIG_DIGEST = "sha256:" + "c" * 64
OTHER_CONFIG = "sha256:" + "d" * 64
DOCKER_V2 = "application/vnd.docker.distribution.manifest.v2+json"


def _containerd_archive(path: Path, *, config_digest: str = CONFIG_DIGEST,
                        media_type: str = DOCKER_V2, tamper: bool = False) -> str:
    """A `docker save` archive as Docker 29's containerd store writes it; returns the
    manifest digest, which is what `docker image inspect` reports as the image ID."""
    manifest = _json_bytes({
        "schemaVersion": 2,
        "mediaType": DOCKER_V2,
        "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                   "digest": config_digest, "size": 10},
        "layers": [],
    })
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    index = _json_bytes({"schemaVersion": 2, "manifests": [
        {"mediaType": media_type, "digest": digest, "size": len(manifest),
         "annotations": {"config.digest": config_digest}},
    ]})
    with tarfile.open(path, mode="w:") as tar:
        for name, data in (("index.json", index),
                           (f"blobs/sha256/{digest[7:]}", manifest + (b" " if tamper else b""))):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return digest


def test_a_docker_v2_manifest_maps_to_its_config(tmp_path: Path) -> None:
    archive = tmp_path / "image.tar"
    manifest_digest = _containerd_archive(archive)
    assert MODULE.archive_config_for_manifest(archive, manifest_digest) == CONFIG_DIGEST
    assert MODULE.archive_manifest_digests(archive, CONFIG_DIGEST) == frozenset({manifest_digest})


def test_an_unknown_or_tampered_manifest_maps_to_nothing(tmp_path: Path) -> None:
    archive = tmp_path / "image.tar"
    _containerd_archive(archive)
    assert MODULE.archive_config_for_manifest(archive, "sha256:" + "e" * 64) is None
    tampered = tmp_path / "tampered.tar"
    digest = _containerd_archive(tampered, tamper=True)
    with pytest.raises(MODULE.BuildImageError, match="does not match its digest"):
        MODULE.archive_config_for_manifest(tampered, digest)
    not_tar = tmp_path / "not-a-tar"
    not_tar.write_bytes(b"sealed")
    assert MODULE.archive_config_for_manifest(not_tar, digest) is None


@pytest.mark.parametrize("validator, report", [
    (MODULE.validate_sbom, _syft_report),
    (MODULE.validate_vulnerability_scan, lambda image_id: _grype_report(image_id=image_id)),
])
def test_a_report_naming_the_candidate_config_is_accepted(validator, report) -> None:
    manifest_digest = "sha256:" + "f" * 64
    summary = validator(report(CONFIG_DIGEST), manifest_digest, CONFIG_DIGEST)
    assert summary["source_image_id"] == CONFIG_DIGEST


@pytest.mark.parametrize("validator, report", [
    (MODULE.validate_sbom, _syft_report),
    (MODULE.validate_vulnerability_scan, lambda image_id: _grype_report(image_id=image_id)),
])
def test_a_report_for_a_different_image_is_still_rejected(validator, report) -> None:
    """The check is not loosened: neither the manifest ID nor the candidate config is named."""
    manifest_digest = "sha256:" + "f" * 64
    with pytest.raises(MODULE.BuildImageError) as excinfo:
        validator(report(OTHER_CONFIG), manifest_digest, CONFIG_DIGEST)
    message = str(excinfo.value)
    assert "does not identify the expected candidate image" in message
    # #615 diagnostics: the failure names every ID involved.
    assert manifest_digest in message and CONFIG_DIGEST in message and OTHER_CONFIG in message


def test_without_a_candidate_config_only_the_image_id_is_accepted() -> None:
    with pytest.raises(MODULE.BuildImageError):
        MODULE.validate_sbom(_syft_report(CONFIG_DIGEST), "sha256:" + "f" * 64)


def test_evidence_on_the_containerd_store_binds_to_the_archive_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "release-image.tar"
    manifest_digest = _containerd_archive(archive)

    def scanner(*, scanner: str, image: str, archive: Path) -> bytes:
        return (_syft_report(CONFIG_DIGEST) if scanner == "syft"
                else _grype_report(image_id=CONFIG_DIGEST))

    monkeypatch.setattr(MODULE, "_run_scanner", scanner)
    evidence = MODULE.generate_evidence(
        artifact_root=tmp_path, archive=archive, archive_sha256="3" * 64,
        image_id=manifest_digest, syft_image=SYFT_IMAGE, grype_image=GRYPE_IMAGE,
    )
    for kind in ("sbom", "vulnerability_scan"):
        assert evidence[kind]["subject"]["image_id"] == manifest_digest
        assert evidence[kind]["validation"]["source_image_id"] == CONFIG_DIGEST


def test_evidence_for_a_report_about_another_archive_image_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "release-image.tar"
    manifest_digest = _containerd_archive(archive)
    monkeypatch.setattr(MODULE, "_run_scanner",
                        lambda **_: _syft_report(OTHER_CONFIG))
    with pytest.raises(MODULE.BuildImageError, match="does not identify"):
        MODULE.generate_evidence(
            artifact_root=tmp_path, archive=archive, archive_sha256="3" * 64,
            image_id=manifest_digest, syft_image=SYFT_IMAGE, grype_image=GRYPE_IMAGE,
        )
