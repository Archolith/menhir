from __future__ import annotations

import hashlib
import importlib.util
import zipfile
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "deploy" / "lib" / "verify_wheelhouse.py"
SPEC = importlib.util.spec_from_file_location("verify_wheelhouse", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wheel(root: Path, filename: str, distribution: str) -> Path:
    path = root / filename
    dist_info = distribution.replace("-", "_") + "-1.0.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", f"Name: {distribution}\nVersion: 1.0\n")
    return path


def _manifest(root: Path, wheels: list[Path]) -> Path:
    path = root / "SHA256SUMS"
    path.write_text(
        "".join(f"{_sha256(wheel)}  {wheel.name}\n" for wheel in wheels),
        encoding="ascii",
    )
    return path


def test_verifies_exact_manifest_and_oauth_wheel(tmp_path: Path) -> None:
    oauth = _wheel(tmp_path, "archolith_oauth-1.0-py3-none-any.whl", "archolith-oauth")
    menhir = _wheel(tmp_path, "menhir-1.0-py3-none-any.whl", "menhir")
    manifest = _manifest(tmp_path, [oauth, menhir])

    MODULE.verify(tmp_path, manifest, _sha256(manifest), _sha256(oauth))


@pytest.mark.parametrize("case", ["manifest", "oauth"])
def test_rejects_digest_mismatch(tmp_path: Path, case: str) -> None:
    oauth = _wheel(tmp_path, "archolith_oauth-1.0-py3-none-any.whl", "archolith-oauth")
    manifest = _manifest(tmp_path, [oauth])
    manifest_digest = "0" * 64 if case == "manifest" else _sha256(manifest)
    oauth_digest = "0" * 64 if case == "oauth" else _sha256(oauth)

    with pytest.raises(ValueError, match="digest|authorized"):
        MODULE.verify(tmp_path, manifest, manifest_digest, oauth_digest)


def test_rejects_unmanifested_or_missing_wheel(tmp_path: Path) -> None:
    oauth = _wheel(tmp_path, "archolith_oauth-1.0-py3-none-any.whl", "archolith-oauth")
    manifest = _manifest(tmp_path, [oauth])
    extra = _wheel(tmp_path, "extra-1.0-py3-none-any.whl", "extra")

    with pytest.raises(ValueError, match="digest mismatch|closure mismatch"):
        MODULE.verify(tmp_path, manifest, _sha256(manifest), _sha256(oauth))

    extra.unlink()
    oauth.unlink()
    with pytest.raises(ValueError, match="closure mismatch"):
        MODULE.verify(tmp_path, manifest, _sha256(manifest), "0" * 64)


def test_rejects_symlink_and_non_wheel_entries(tmp_path: Path) -> None:
    oauth = _wheel(tmp_path, "archolith_oauth-1.0-py3-none-any.whl", "archolith-oauth")
    manifest = _manifest(tmp_path, [oauth])
    (tmp_path / "unexpected.txt").write_text("x", encoding="ascii")

    with pytest.raises(ValueError, match="unexpected wheelhouse entry"):
        MODULE.verify(tmp_path, manifest, _sha256(manifest), _sha256(oauth))


def test_rejects_duplicate_or_traversal_manifest_names(tmp_path: Path) -> None:
    manifest = tmp_path / "SHA256SUMS"
    manifest.write_text(f"{'0' * 64}  ../escape.whl\n", encoding="ascii")

    with pytest.raises(ValueError, match="invalid wheel manifest line"):
        MODULE.verify(tmp_path, manifest, _sha256(manifest), "0" * 64)
