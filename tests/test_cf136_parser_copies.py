"""CF-136: the CF-55 parser fix must not be re-inlined into three modules.

`parse_iso8601` (the Neo4j-tolerant parser in `domain.temporal`) is the single canonical
definition. It was copied byte-identically (minus the zone-id-suffix strip) into
`git_staleness` and `structure_temporal`, so each of those returned `None` on the exact
`toString(...)Z[UTC]` format production emits -- and each filter then skipped the unparseable
bound, silently failing open. This test makes N=0 copies enforceable: every module's `_parse`
must BE `parse_iso8601` (identity), not a look-alike.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from menhir.domain import git_staleness, structure_temporal, temporal

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULES = {
    "temporal": temporal,
    "git_staleness": git_staleness,
    "structure_temporal": structure_temporal,
}
_SRC_FILES = {
    "temporal": _REPO_ROOT / "src/menhir/domain/temporal.py",
    "git_staleness": _REPO_ROOT / "src/menhir/domain/git_staleness.py",
    "structure_temporal": _REPO_ROOT / "src/menhir/domain/structure_temporal.py",
}
_ZONED = "2026-08-14T10:00:00Z[UTC]"   # Neo4j toString(...) form the old copies rejected
_UNSUFFIXED = "2026-08-14T10:00:00Z"   # form the old copies already handled


@pytest.mark.unit
@pytest.mark.parametrize("name", list(_MODULES))
def test_zoned_suffix_parses_to_real_datetime(name: str) -> None:
    """The finding: `_parse` on the production format returns a datetime, not None."""
    dt = _MODULES[name]._parse(_ZONED)
    assert isinstance(dt, datetime)
    assert dt == datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)


@pytest.mark.unit
@pytest.mark.parametrize("name", list(_MODULES))
def test_parse_is_identity_with_canonical_parser(name: str) -> None:
    """One object now: each module's `_parse` IS `parse_iso8601`, so a re-inline fails here."""
    assert _MODULES[name]._parse is temporal.parse_iso8601


@pytest.mark.unit
def test_no_def_parse_definition_in_any_of_the_three_modules() -> None:
    """Drift guard for the census: N copies -> N=0 `def _parse(` in these sources."""
    for name, path in _SRC_FILES.items():
        source = path.read_text(encoding="utf-8")
        assert f"def _parse(" not in source, f"{name} re-inlined a `def _parse(`"


@pytest.mark.unit
@pytest.mark.parametrize("name", list(_MODULES))
def test_unsuffixed_form_still_parses_to_same_value(name: str) -> None:
    """POSITIVE CONTROL: inputs that already worked are unchanged (strictly additive)."""
    expected = temporal.parse_iso8601(_UNSUFFIXED)
    assert expected is not None
    assert _MODULES[name]._parse(_UNSUFFIXED) == expected


@pytest.mark.unit
@pytest.mark.parametrize("name", list(_MODULES))
def test_unparseable_still_returns_none_not_raise(name: str) -> None:
    """POSITIVE CONTROL: the 'never raises' contract holds through the alias."""
    assert _MODULES[name]._parse("not-a-timestamp") is None
    assert _MODULES[name]._parse(None) is None
