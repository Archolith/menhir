"""CF-93: `_utc_now_iso` was declared sixteen times.

The same one-line helper, copied into sixteen modules across `infrastructure/` and `services/`.
All sixteen were behaviourally identical -- the one structural outlier, `verifier_sync.py`, differed
only by importing `datetime` inside the function -- so nothing was broken. The finding is that
nothing kept them identical.

That is not hypothetical here: CF-77's `Embed` alias was copied the same way and one copy silently
dropped `| None`. Every stored `created_at`, lease expiry and saga receipt in this system is written
through one of these, so a single "improvement" in one copy (dropping the timezone, switching to
`time.time()`, truncating microseconds) would produce timestamps that compare wrongly against the
aware ones already stored.
"""

from __future__ import annotations

import ast
import pathlib
import re
from datetime import datetime

import pytest

from menhir.clock import utc_now_iso

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "menhir"


def test_the_timestamp_is_timezone_aware() -> None:
    """THE PROPERTY WORTH PROTECTING. A naive timestamp compares wrongly against the aware ones
    already stored, and `scalar_state_fold` raises TypeError outright when a naive `as_of` meets an
    aware `valid_at` (DOMB-COR-3)."""
    parsed = datetime.fromisoformat(utc_now_iso())

    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_it_round_trips_through_fromisoformat() -> None:
    """Every consumer stores this string and something later parses it back."""
    assert datetime.fromisoformat(utc_now_iso()) is not None


def test_microseconds_are_retained() -> None:
    """Truncating to whole seconds would collapse orderings the saga journal depends on -- receipts
    written in the same second still have to sort by write order."""
    assert re.search(r"\.\d+", utc_now_iso()), utc_now_iso()


def test_no_module_declares_its_own_copy() -> None:
    """The structural half: sixteen copies became one, and a seventeenth must not appear."""
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path.name == "clock.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("_utc_now_iso", "utc_now_iso"):
                offenders.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert offenders == [], f"re-declared instead of imported from menhir.clock: {offenders}"


def test_the_scan_can_actually_find_a_declaration() -> None:
    """POSITIVE CONTROL: without this the scan above would pass on a codebase where the helper had
    been deleted entirely."""
    tree = ast.parse((_SRC / "clock.py").read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    assert "utc_now_iso" in names


def test_the_consumers_bind_the_shared_function() -> None:
    """Identity through the alias each module imports it under, so a local re-definition that
    happened to be spelled correctly is still caught."""
    import importlib

    for mod_name in (
        "menhir.infrastructure.graph_operations",
        "menhir.infrastructure.metric_receipts",
        "menhir.services.merge_coordinator",
        "menhir.services.scheduler_lease",
        "menhir.services.verifier_sync",
    ):
        mod = importlib.import_module(mod_name)
        bound = getattr(mod, "_utc_now_iso", None)
        assert bound is utc_now_iso, f"{mod_name} does not use the shared clock"
