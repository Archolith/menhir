from __future__ import annotations

import hashlib
import importlib.util
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
