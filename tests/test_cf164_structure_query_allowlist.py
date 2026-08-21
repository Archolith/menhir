"""CF-164: `query_structure` dispatched a caller-supplied string through `getattr`.

    method = getattr(self._structure, f"query_{query_type}", None)

`structure_queries` defines 14 `query_*` methods; the boundary advertises 13 and its error message
lists the same 13. The two reachable-but-undocumented types were `query_contained_repos` and
`query_linked_memories`.

WHERE THE EXPOSURE ACTUALLY IS -- and it is not where the entry implies. The MCP tool does NOT pass
the caller's string down: `mcp/tools/recall/query_structure.py` runs an explicit
`if query_type == "..."` chain and hands a LITERAL to the backend. The reachable path is REST:
`query_structure` is listed in `_BACKEND_METHODS` (`api/routes_support.py:601`), falls to the
readonly remainder in the tier map, and `backend_runtime_data_ops.query_structure` passes
`query_type` straight through to the adapter after special-casing three types.

So a readonly REST caller could name `linked_memories` -- which is CF-126's unscoped recall --
through a type nothing documents.

THE SET IS 11, NOT 13. `projects`, `orphan_structure_projects` and `documents` are answered
upstream in `backend_runtime_data_ops.query_structure` and never reach the dispatch. Deriving the
allowlist from the advertised 13 would have added three names that resolve to nothing here.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = pytest.mark.unit


class _Structure:
    """Stands in for StructureGraphWriter: records which query_* method was reached."""

    def __init__(self) -> None:
        self.called: str | None = None

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("query_"):
            raise AttributeError(name)

        def _method(project: str, **kwargs: Any) -> str:
            self.called = name
            return f"{name}:{project}"

        return _method


def _adapter() -> tuple[MemoryGraphAdapter, _Structure]:
    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    structure = _Structure()
    object.__setattr__(adapter, "_structure", structure)
    return adapter, structure


# ---------------------------------------------------------------------------
# the finding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query_type", ["contained_repos", "linked_memories"])
def test_the_undocumented_types_are_refused(query_type: str) -> None:
    """THE FINDING. Both resolve to real methods on the repository; neither is advertised."""
    adapter, structure = _adapter()

    with pytest.raises(ValueError, match="Unknown structure query type"):
        adapter.query_structure("proj", query_type)

    assert structure.called is None, "the method must not be reached at all"


def test_the_refusal_does_not_reveal_that_the_method_exists() -> None:
    """A distinct message for 'exists but is not allowed' would enumerate the private surface for
    anyone probing. A refused-but-real type must be indistinguishable from a typo."""
    adapter, _structure = _adapter()

    # The messages interpolate the requested type, so they cannot be byte-equal. The property is
    # that the TEMPLATE is identical: nothing in the text distinguishes the two cases.
    real_but_private = _err(adapter, "linked_memories").replace("linked_memories", "<T>")
    pure_typo = _err(adapter, "definitely_not_a_method").replace("definitely_not_a_method", "<T>")

    assert real_but_private == pure_typo
    assert "not allowed" not in real_but_private.lower()
    assert "permitted" not in real_but_private.lower()


def _err(adapter: MemoryGraphAdapter, query_type: str) -> str:
    with pytest.raises(ValueError) as caught:
        adapter.query_structure("proj", query_type)
    return str(caught.value)


def test_a_newly_added_repository_method_is_not_automatically_exposed() -> None:
    """The durable half. `getattr` dispatch meant every future `query_*` method became a public
    REST-reachable type the moment it was written, with nobody deciding that."""
    adapter, structure = _adapter()

    # The stub answers ANY query_* name, so this only passes because of the allowlist.
    with pytest.raises(ValueError, match="Unknown structure query type"):
        adapter.query_structure("proj", "some_future_method")

    assert structure.called is None


# ---------------------------------------------------------------------------
# positive controls -- every documented type must still work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query_type",
    sorted(MemoryGraphAdapter.STRUCTURE_QUERY_TYPES),
)
def test_every_allowed_type_still_dispatches(query_type: str) -> None:
    """POSITIVE CONTROL, the one that matters most: an allowlist that refused everything would
    satisfy every test above. Parameterized over the allowlist itself so adding a type to it
    without a real method behind it fails here."""
    adapter, structure = _adapter()

    result = adapter.query_structure("proj", query_type)

    assert structure.called == f"query_{query_type}"
    assert result == f"query_{query_type}:proj"


def test_every_allowed_type_resolves_to_a_real_repository_method() -> None:
    """The stub above answers any name, so it cannot catch an allowlist entry that has no real
    implementation. Check the allowlist against the REAL class."""
    from menhir.infrastructure.structure_queries import StructureGraphWriter

    missing = sorted(
        qt
        for qt in MemoryGraphAdapter.STRUCTURE_QUERY_TYPES
        if not callable(getattr(StructureGraphWriter, f"query_{qt}", None))
    )

    assert missing == [], f"allowlisted types with no query_* method: {missing}"


def test_the_allowlist_covers_every_type_the_mcp_tool_dispatches() -> None:
    """The two sets must not drift. The MCP tool hands literals to the backend; each one that is
    not handled upstream has to be in this allowlist or that tool breaks.

    Read from the tool's source rather than restated here, so adding a branch to the tool without
    allowlisting its type fails this test instead of failing in production."""
    import re
    from pathlib import Path

    import menhir.mcp.tools.recall.query_structure as tool_mod

    source = Path(tool_mod.__file__).read_text(encoding="utf-8")
    dispatched = set(re.findall(r'query_type == "([a-z_]+)"', source))

    # Answered in backend_runtime_data_ops.query_structure before the adapter is reached.
    handled_upstream = {"projects", "orphan_structure_projects", "documents"}

    unroutable = sorted(dispatched - handled_upstream - MemoryGraphAdapter.STRUCTURE_QUERY_TYPES)
    assert unroutable == [], f"tool dispatches types the adapter refuses: {unroutable}"
    assert dispatched, "the regex found nothing -- the tool's dispatch shape changed"
