"""CF-303: publish-mode remote verification shares the archive-manifest acceptance of verify mode.

No network or Docker: ``subprocess.run``, ``inspect_image`` and ``remote_manifest_digest`` are
replaced, and the OCI archive is a small tar written under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_release_image_remote_store", ROOT / "deploy" / "build_release_image.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

IMAGE = "ghcr.io/archolith/menhir"
IMAGE_TAG = f"{IMAGE}:1.2.3-4"
REGISTRY_DIGEST = "sha256:" + "4" * 64
BLOB_CONFIG = {"User": "10001:10001", "Labels": {"a": "b"}}
LABELS = {"commit": "b" * 40, "version": "1.2.3-4"}
ARCHIVE_SHA256 = "3" * 64


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True).encode("ascii")


def _sealed_archive(path: Path) -> tuple[str, str]:
    """Write an OCI archive; return (config digest = classic image ID, manifest digest)."""
    config_blob = _json_bytes({"config": BLOB_CONFIG, "rootfs": {}})
    config_digest = "sha256:" + hashlib.sha256(config_blob).hexdigest()
    manifest = _json_bytes({
        "schemaVersion": 2,
        "config": {"digest": config_digest, "size": len(config_blob)},
        "layers": [],
    })
    manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    index = _json_bytes({"schemaVersion": 2, "manifests": [{
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": manifest_digest, "size": len(manifest),
    }]})
    with tarfile.open(path, mode="w:") as tar:
        for name, data in (
            ("index.json", index),
            (f"blobs/sha256/{manifest_digest[7:]}", manifest),
            (f"blobs/sha256/{config_digest[7:]}", config_blob),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return config_digest, manifest_digest


def _verify_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, remote_id: str, remote_config: str,
    sealed_id: str, sealed_config: str, archive: Path | None,
) -> None:
    metadata = {"labels": LABELS, "image_id": sealed_id, "config_sha256": sealed_config}
    monkeypatch.setattr(MODULE.subprocess, "run", lambda command, **_kw: subprocess.CompletedProcess(command, 0))
    monkeypatch.setattr(MODULE, "inspect_image", lambda _ref, expected: (remote_id, expected, remote_config))
    monkeypatch.setattr(MODULE, "remote_manifest_digest", lambda _ref: REGISTRY_DIGEST)
    kwargs = {} if archive is None else {"archive": archive}
    MODULE._verify_remote_candidate(
        repository=IMAGE, digest=REGISTRY_DIGEST, metadata=metadata, **kwargs,
    )


def test_publish_host_with_containerd_store_accepts_the_correct_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    archive = tmp_path / "release-image.tar"
    config_digest, manifest_digest = _sealed_archive(archive)
    _verify_remote(
        monkeypatch, tmp_path,
        remote_id=manifest_digest, remote_config=MODULE.canonical_sha256(BLOB_CONFIG),
        sealed_id=config_digest, sealed_config="2" * 64, archive=archive,
    )


def test_publish_host_with_containerd_store_refuses_a_manifest_the_archive_does_not_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    archive = tmp_path / "release-image.tar"
    config_digest, _manifest_digest = _sealed_archive(archive)
    with pytest.raises(MODULE.BuildImageError, match="registry candidate image ID differs"):
        _verify_remote(
            monkeypatch, tmp_path,
            remote_id="sha256:" + "d" * 64, remote_config=MODULE.canonical_sha256(BLOB_CONFIG),
            sealed_id=config_digest, sealed_config="2" * 64, archive=archive,
        )


def test_publish_host_with_containerd_store_refuses_a_config_that_is_not_the_archive_blob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    archive = tmp_path / "release-image.tar"
    config_digest, manifest_digest = _sealed_archive(archive)
    with pytest.raises(MODULE.BuildImageError, match="registry candidate config differs"):
        _verify_remote(
            monkeypatch, tmp_path,
            remote_id=manifest_digest, remote_config=MODULE.canonical_sha256({"User": "root"}),
            sealed_id=config_digest, sealed_config="2" * 64, archive=archive,
        )


def test_manifest_id_is_not_accepted_without_the_sealed_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    archive = tmp_path / "release-image.tar"
    config_digest, manifest_digest = _sealed_archive(archive)
    with pytest.raises(MODULE.BuildImageError, match="registry candidate image ID differs"):
        _verify_remote(
            monkeypatch, tmp_path,
            remote_id=manifest_digest, remote_config=MODULE.canonical_sha256(BLOB_CONFIG),
            sealed_id=config_digest, sealed_config="2" * 64, archive=None,
        )


def test_classic_store_still_accepts_and_refuses_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    sealed_id = "sha256:" + "1" * 64
    _verify_remote(
        monkeypatch, tmp_path, remote_id=sealed_id, remote_config="2" * 64,
        sealed_id=sealed_id, sealed_config="2" * 64, archive=None,
    )
    with pytest.raises(MODULE.BuildImageError, match="registry candidate config differs"):
        _verify_remote(
            monkeypatch, tmp_path, remote_id=sealed_id, remote_config="9" * 64,
            sealed_id=sealed_id, sealed_config="2" * 64, archive=None,
        )


def test_classic_store_does_not_open_the_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    sealed_id = "sha256:" + "1" * 64
    monkeypatch.setattr(
        MODULE, "archive_manifest_digests",
        lambda *_a: pytest.fail("the classic ID needs no archive"),
    )
    _verify_remote(
        monkeypatch, tmp_path, remote_id=sealed_id, remote_config="2" * 64,
        sealed_id=sealed_id, sealed_config="2" * 64, archive=tmp_path / "unused.tar",
    )


def test_publish_candidate_hands_the_archive_to_every_remote_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    archive = tmp_path / "release-image.tar"
    metadata = {"image_tag": IMAGE_TAG, "image_archive_sha256": ARCHIVE_SHA256}
    results = iter((None, None, REGISTRY_DIGEST))
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(MODULE, "remote_manifest_digest", lambda _tag: next(results))
    monkeypatch.setattr(MODULE, "_push_digest", lambda _tag: REGISTRY_DIGEST)
    monkeypatch.setattr(MODULE, "_verify_remote_candidate", lambda **kw: seen.append(kw))
    MODULE.publish_candidate(metadata, archive=archive)
    assert [kw["archive"] for kw in seen] == [archive]


def test_publish_bundle_passes_the_verified_archive_to_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    archive = tmp_path / "release-image.tar"
    metadata = {
        "image_tag": IMAGE_TAG, "source_repository": "Archolith/menhir", "source_commit": "b" * 40,
        "image_id": "sha256:" + "1" * 64, "config_sha256": "2" * 64,
        "image_archive_sha256": ARCHIVE_SHA256,
    }
    seen: list[Path | None] = []
    monkeypatch.setattr(
        MODULE, "verify_validation_bundle",
        lambda _args, load_image: (metadata, {"evidence": {}}),
    )
    monkeypatch.setattr(
        MODULE, "publish_candidate",
        lambda _metadata, archive=None: seen.append(archive) or (REGISTRY_DIGEST, False, "tag"),
    )
    args = SimpleNamespace(
        expected_identity_sha256="a" * 64, output=tmp_path / "out.json", image_archive=archive,
    )
    MODULE.publish_bundle(args)
    assert seen == [archive.resolve()]
