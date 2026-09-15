"""Release dependencies must be immutable and verifiable.

Until 2026-09-15 that meant a reviewed Git commit, because neither first-party package was
published. Both are on PyPI now, so the guarantee is stronger and this file asserts the
stronger form: an exact version pin (never a range), resolved from the registry rather than a
repository, and carrying hashes in the lock so the release wheelhouse can be built with
``--require-hashes``.

The property being protected has not changed -- you cannot silently swap what ships -- but a
git+https requirement cannot carry a hash and cannot be installed without git, which is why it
was traded for a registry pin.
"""

from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PINNED_FIRST_PARTY = {
    "archolith-oauth": "0.3.1",
    "archolith-mcp-framework": "0.2.0",
}


def _dependencies() -> list[str]:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["dependencies"]


def _lock_package(name: str) -> dict:
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = [package for package in lock["package"] if package["name"] == name]
    assert len(packages) == 1, f"expected exactly one {name} entry in uv.lock, got {len(packages)}"
    return packages[0]


def test_first_party_dependencies_are_exact_version_pins() -> None:
    dependencies = _dependencies()
    for name, version in PINNED_FIRST_PARTY.items():
        matching = [d for d in dependencies if d.lower().replace(" ", "").startswith(name)]
        assert matching == [f"{name}=={version}"], (
            f"{name} must be pinned exactly as {name}=={version}; found {matching}"
        )


def test_first_party_dependencies_resolve_from_pypi_not_a_repository() -> None:
    for name in PINNED_FIRST_PARTY:
        source = _lock_package(name)["source"]
        assert source == {"registry": "https://pypi.org/simple"}, (
            f"{name} resolves from {source}; a repository source cannot be hash-verified "
            "and cannot be installed without git"
        )


def test_first_party_dependencies_carry_hashes_in_the_lock() -> None:
    """Without hashes the release wheelhouse cannot be built with --require-hashes, which is
    what makes the shipped closure verifiable."""

    for name in PINNED_FIRST_PARTY:
        package = _lock_package(name)
        wheels = package.get("wheels") or []
        sdist = package.get("sdist") or {}
        hashes = [w.get("hash") for w in wheels] + [sdist.get("hash")]
        assert any(h and h.startswith("sha256:") for h in hashes), (
            f"{name} has no sha256 hash in uv.lock"
        )


def test_no_dependency_is_resolved_from_a_repository() -> None:
    """A single VCS requirement anywhere re-imposes git on every install and breaks
    ``pip install archolith-menhir`` on a machine without it."""

    vcs = [d for d in _dependencies() if "@" in d and "://" in d]
    assert vcs == [], f"dependencies resolved from a repository: {vcs}"
