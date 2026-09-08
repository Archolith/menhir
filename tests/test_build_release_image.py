from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_release_image", ROOT / "deploy" / "build_release_image.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _wheelhouse(root: Path, oauth_name: str = "archolith_oauth-1.2.3-py3-none-any.whl") -> Path:
    root.mkdir()
    files = {
        oauth_name: b"oauth-wheel",
        "menhir-1.2.3-py3-none-any.whl": b"menhir-wheel",
    }
    lines = []
    for name, body in files.items():
        (root / name).write_bytes(body)
        lines.append(f"{hashlib.sha256(body).hexdigest()}  {name}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")
    return root


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
        image_tag="ghcr.io/archolith/menhir:1.2.3-4",
        python_base="python@sha256:" + "a" * 64,
        commit="b" * 40,
        version="1.2.3-4",
        wheel_manifest="c" * 64,
        oauth_wheel="d" * 64,
    )
    joined = "\n".join(command)
    assert "RELEASE_COMMIT=" + "b" * 40 in joined
    assert "RELEASE_VERSION=1.2.3-4" in joined
    assert "WHEEL_MANIFEST_SHA256=" + "c" * 64 in joined
    assert "OAUTH_WHEEL_SHA256=" + "d" * 64 in joined
    assert command[-2:] == ["ghcr.io/archolith/menhir:1.2.3-4", str(tmp_path)]


def test_required_evidence_rejects_missing_sbom_or_scan(tmp_path: Path) -> None:
    with pytest.raises(MODULE.BuildImageError, match="required for publication"):
        MODULE.stage_evidence(
            repo=tmp_path,
            artifact_root=tmp_path / "artifact",
            sbom="sbom.json",
            scan=None,
            required=True,
        )


def test_required_evidence_is_copied_to_fixed_hash_bound_paths(tmp_path: Path) -> None:
    (tmp_path / "source-sbom.json").write_bytes(b"sbom")
    (tmp_path / "source-scan.json").write_bytes(b"scan")
    artifact = tmp_path / "artifact"
    evidence = MODULE.stage_evidence(
        repo=tmp_path,
        artifact_root=artifact,
        sbom="source-sbom.json",
        scan="source-scan.json",
        required=True,
    )
    assert evidence["required"] is True
    assert evidence["sbom"] == {
        "artifact_path": "release-image-evidence/sbom",
        "sha256": hashlib.sha256(b"sbom").hexdigest(),
    }
    assert evidence["vulnerability_scan"] == {
        "artifact_path": "release-image-evidence/scan",
        "sha256": hashlib.sha256(b"scan").hexdigest(),
    }


def test_evidence_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(MODULE.BuildImageError, match="normalized repository-relative"):
        MODULE._safe_relative_file(tmp_path, "../sbom.json", "sbom")


def test_evidence_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="ascii")
    link = tmp_path / "sbom.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(MODULE.BuildImageError, match="symlink"):
        MODULE._safe_relative_file(tmp_path, "sbom.json", "sbom")


def test_remote_manifest_rejects_malformed_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.CompletedProcess([], 0, stdout="Digest: sha256:bad\n", stderr="")
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(MODULE.BuildImageError, match="malformed"):
        MODULE.remote_manifest_digest("ghcr.io/archolith/menhir:1.2.3-4")


def test_existing_different_release_tag_is_not_pushed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "sha256:" + "a" * 64
    existing = "sha256:" + "b" * 64
    pushed: list[str] = []
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        MODULE, "_push_digest", lambda tag: pushed.append(tag) or candidate,
    )
    monkeypatch.setattr(MODULE, "remote_manifest_digest", lambda tag: existing)
    release = "ghcr.io/archolith/menhir:1.2.3-4"
    with pytest.raises(MODULE.BuildImageError, match="already exists"):
        MODULE.publish_immutable(release, "c" * 64)
    assert pushed == ["ghcr.io/archolith/menhir:candidate-" + "c" * 32]


def test_existing_same_release_tag_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "sha256:" + "a" * 64
    pushed: list[str] = []
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        MODULE, "_push_digest", lambda tag: pushed.append(tag) or candidate,
    )
    monkeypatch.setattr(MODULE, "remote_manifest_digest", lambda tag: candidate)
    release = "ghcr.io/archolith/menhir:1.2.3-4"
    digest, idempotent, _candidate_tag = MODULE.publish_immutable(release, "c" * 64)
    assert digest == candidate
    assert idempotent is True
    assert release not in pushed


def test_new_release_tag_is_pushed_only_after_two_absence_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "sha256:" + "a" * 64
    release = "ghcr.io/archolith/menhir:1.2.3-4"
    pushed: list[str] = []
    remote_results = iter((None, None, candidate))
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        MODULE, "_push_digest", lambda tag: pushed.append(tag) or candidate,
    )
    monkeypatch.setattr(
        MODULE, "remote_manifest_digest", lambda tag: next(remote_results),
    )
    digest, idempotent, candidate_tag = MODULE.publish_immutable(release, "c" * 64)
    assert digest == candidate
    assert idempotent is False
    assert pushed == [candidate_tag, release]
