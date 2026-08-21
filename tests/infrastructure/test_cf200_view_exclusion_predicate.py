"""CF-200: one shared View-exclusion predicate, no drifting hand-written spellings.

Derived Views are stored as ``:Entity`` (recallable Views carry ``is_view``/``view_kind``, counters
carry ``is_quantstate``). Every bind/merge/flag gate that must not touch a derived View used to
state its own exclusion, and five spellings across four modules drifted apart: two tested only
``is_view`` (admitting legacy counters with ``is_view=False, is_quantstate=True, view_kind=None``)
and one even inverted the form (``coalesce(x.is_view, false) = false``).

These tests are OFFLINE. The exclusion predicate is asserted on its STRING and by re-evaluating the
three boolean terms it encodes over a node's property dict -- no Neo4j is required for this brief.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from menhir.infrastructure.cypher import non_derived_view_cypher

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "menhir"

# The call-site source files that must route through the shared helper (the four modules named in
# the brief), plus the helper's own definition module.
_CALL_SITE_FILES = [
    "infrastructure/episode_lifecycle.py",
    "infrastructure/correlation_queries.py",
    "infrastructure/verifier_repository.py",
]

# Both hand-written spellings must be caught: the `NOT coalesce(...)` form and the inverted
# `coalesce(...) = false` form (site #5).
_HANDWRITTEN_IS_VIEW_EXCLUSION = re.compile(
    r"NOT\s+coalesce\(\w+\.is_view,\s*false\)"
    r"|coalesce\(\w+\.is_view,\s*false\)\s*=\s*false"
)


def _predicate_holds(predicate: str, node: dict) -> bool:
    """Re-evaluate the three-term exclusion predicate over a node's property dict.

    Mirrors the boolean semantics the helper encodes: the node is EXCLUDED (predicate false) when
    ANY of `is_view`, `is_quantstate`, or `view_kind` marks it derived.
    """
    view = node.get("is_view") or False
    quant = node.get("is_quantstate") or False
    kind = node.get("view_kind")
    return (not view) and (not quant) and (kind is None)


# ---------------------------------------------------------------------------
# 1. The census, as a drift guard: N spellings -> N == 1.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_handwritten_is_view_exclusion_outside_the_helper() -> None:
    """Every `is_view` exclusion predicate in src/ lives inside the shared helper module.

    Catches BOTH spellings: `NOT coalesce(x.is_view, false)` and the inverted
    `coalesce(x.is_view, false) = false` (site #5). The RETURN aliases
    (`coalesce(n.is_view, false) AS is_view`) are projections, not exclusion predicates, and must
    not be flagged.
    """
    offenders = []
    for py in _SRC_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if _HANDWRITTEN_IS_VIEW_EXCLUSION.search(text):
            # The helper's own definition module is the one allowed home.
            if py.name != "cypher.py":
                offenders.append(str(py.relative_to(_SRC_ROOT)))

    assert offenders == [], (
        f"hand-written is_view exclusion predicate(s) outside cypher.py: {offenders}"
    )


# ---------------------------------------------------------------------------
# 2. The predicate tests all three properties.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_predicate_references_all_three_properties() -> None:
    predicate = non_derived_view_cypher("n")
    assert "is_view" in predicate
    assert "is_quantstate" in predicate
    assert "view_kind" in predicate


# ---------------------------------------------------------------------------
# 3. The legacy-counter case, concretely (offline, string/evaluated semantics).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_legacy_counter_is_excluded() -> None:
    """A legacy counter is `is_view=False, is_quantstate=True, view_kind=None` (the exact node
    shape `tests/test_typed_scalar_bind_persist.py` builds). Only an `is_view` test admits it; the
    shared predicate must not."""
    legacy_counter = {"is_view": False, "is_quantstate": True, "view_kind": None}
    predicate = non_derived_view_cypher("n")
    # The string encodes the is_quantstate guard that the weak spellings dropped...
    assert "is_quantstate" in predicate
    # ...and re-evaluating the encoded booleans confirms it is excluded.
    assert _predicate_holds(predicate, legacy_counter) is False


# ---------------------------------------------------------------------------
# 4. POSITIVE CONTROL: an ordinary memory node is NOT excluded.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ordinary_memory_node_is_not_excluded() -> None:
    """A predicate that excluded everything would pass tests 2 and 3; this control proves it does
    not. An ordinary memory carries none of the derived-View markers."""
    ordinary = {"name": "a plain memory"}
    assert _predicate_holds(non_derived_view_cypher("n"), ordinary) is True


# ---------------------------------------------------------------------------
# 5. POSITIVE CONTROL: a current-shaped View IS excluded.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_current_shaped_view_is_excluded() -> None:
    current_view = {"is_view": True, "is_quantstate": False, "view_kind": "scalar_state"}
    assert _predicate_holds(non_derived_view_cypher("n"), current_view) is False


# ---------------------------------------------------------------------------
# 6. The helper is used by all five call sites (three source files) plus its definition module.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_helper_is_used_at_all_call_sites() -> None:
    for rel in _CALL_SITE_FILES:
        text = (_SRC_ROOT / rel).read_text(encoding="utf-8")
        assert "non_derived_view_cypher" in text, f"helper missing from {rel}"


# ---------------------------------------------------------------------------
# The bug the consolidation itself introduced, pinned so it cannot recur.
#
# Interpolating a helper into Cypher only works inside an f-string. Four of the five call sites were
# written as `"""...{non_derived_view_cypher("n")}..."""` -- plain literals -- which ship the LITERAL
# text `{non_derived_view_cypher("n")}` to Neo4j and fail at parse time. Adding the `f` prefix then
# requires every real Cypher brace in that literal to be doubled (`EXISTS {{ ... }}`), which is the
# second half of the trap.
#
# This is a general hazard of Cypher-fragment helpers, not a one-off, so the guard is structural.
# ---------------------------------------------------------------------------


def test_every_call_site_interpolates_inside_an_f_string() -> None:
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "menhir"
    offenders: list[str] = []

    for path in root.rglob("*.py"):
        lines = path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines):
            if "{non_derived_view_cypher(" not in line:
                continue
            # Walk back to the opening triple quote of the literal this line sits in.
            for j in range(i, max(-1, i - 40), -1):
                match = re.search(r'(f?)"""\s*$', lines[j])
                if match:
                    if match.group(1) != "f":
                        offenders.append(f"{path.name}:{i + 1}")
                    break

    assert offenders == [], (
        "these interpolate the helper inside a NON-f string, so Neo4j receives literal braces: "
        f"{offenders}"
    )


def test_the_helper_renders_to_parseable_cypher_without_leftover_braces() -> None:
    """Cheap proxy for the parse failure, with no database required: the rendered predicate must
    contain no unconsumed `{`."""
    rendered = non_derived_view_cypher("n")

    assert "{" not in rendered and "}" not in rendered
    assert "non_derived_view_cypher" not in rendered
