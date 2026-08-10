import pytest
from menhir import __version__

@pytest.mark.unit
def test_version() -> None:
    """Verify package imports correctly and version is defined.

    Deliberately not pinned to a literal version string (SSOT-13): __version__
    is now resolved once from installed package metadata (which itself derives
    from pyproject.toml), so hardcoding a duplicate literal here is exactly
    the kind of drift-prone duplication that fix removed. Assert the
    resolution mechanism worked instead of a specific value.
    """
    assert isinstance(__version__, str) and __version__ != ""

@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_fixture_mock() -> None:
    """Verify asyncio test plugin works."""
    assert True
