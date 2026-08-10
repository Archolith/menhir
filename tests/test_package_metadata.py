"""Installed-distribution metadata must match the public package contract."""

from importlib.metadata import version
from pathlib import Path

import pytest

import menhir


@pytest.mark.unit
def test_runtime_version_uses_distribution_metadata() -> None:
    assert menhir.__version__ == version("archolith-menhir")


@pytest.mark.unit
def test_explorer_template_static_references_exist() -> None:
    explorer_root = Path(menhir.__file__).parent / "explorer"
    for template in (explorer_root / "templates").glob("*.html"):
        contents = template.read_text(encoding="utf-8")
        for asset in ("extraction-lab.js", "recall-lab.js", "explorer.js"):
            if f"/explorer/static/{asset}" in contents:
                assert (explorer_root / "static" / asset).is_file()
