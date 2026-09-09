"""Evidence-anchor invariant: no View may be written with contributors the gate cannot accept.

INVARIANT
    Every View writer that declares contributor UUIDs must normalize them to anchors the shared
    FACT writer's gate accepts -- evidence with ``evidence_finalized = true`` -- before the write.

WHY IT IS A SYSTEM PROPERTY AND NOT A CHECK IN ONE FUNCTION
    ``_write_version``'s gate is all-or-nothing: ``WHERE resolved_count = size($eps)``. ONE
    unacceptable anchor does not degrade the receipt, it makes the whole write return no rows and
    raise. ``evidence_finalized`` is only ever set on :TurnEvidence (turn_evidence_repository) or on
    publication-intent artifacts, while ``TypedAssertion.episode_uuid`` is a POLYMORPHIC anchor that
    may hold an :Episodic. So any writer passing raw assertion anchors can emit a write that is
    UNSATISFIABLE BY CONSTRUCTION -- a ValueError out of the sink that takes the whole consolidation
    pass down.

    Measured 2026-09-09 on date-smoke cc5ded98 with scalar history enabled: ``record_scalar_state``
    resolved its anchors and succeeded, while ``record_counter`` and ``record_scalar_history`` passed
    the raw list. 14 refusals, every frame ``record_scalar_history``, 0 Views written against 2
    TypedAssertions.

WHAT THESE TESTS PIN
    1. Enforcement lives at the ONE point every contributor-declaring writer crosses (``record``),
       not in each ergonomic wrapper, so a NEW writer is covered without remembering to opt in.
    2. Resolution happens BEFORE the surface is rendered, because it can change the list (an
       Episodic maps to its grounding turn; two Episodic anchors can collapse onto one turn) and the
       summary quotes the contributor count.
    3. The census: no ``record_*`` wrapper may hand a raw contributor list to ``record`` again. This
       fails when a new wrapper appears without an enforcement decision.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from menhir.infrastructure.scalar_view_repository import ScalarViewRepositoryMixin
from menhir.infrastructure.view_repository import ViewRepository
from menhir.infrastructure.view_write_repository import ViewWriteRepositoryMixin


def test_resolver_lives_on_the_mixin_that_owns_record() -> None:
    """Enforcement must be reachable from `record` even when the write mixin stands alone.

    `ViewWriteRepositoryMixin` is instantiated directly by other tests, so a resolver that lived on
    a sibling mixin would raise AttributeError there rather than enforce.
    """
    assert hasattr(ViewWriteRepositoryMixin, "_resolve_evidence_anchors"), (
        "record() calls self._resolve_evidence_anchors; it must be defined on the SAME mixin that "
        "defines record(), not on a sibling that only the composed ViewRepository picks up"
    )
    assert hasattr(ViewRepository, "_resolve_evidence_anchors")
    assert not hasattr(ScalarViewRepositoryMixin, "_resolve_evidence_anchors"), (
        "two definitions would let the composed MRO silently pick the wrong one"
    )


def test_record_resolves_anchors_before_rendering_the_surface() -> None:
    """Order matters: resolution can CHANGE the contributor list, and the summary quotes its size."""
    src = inspect.getsource(ViewWriteRepositoryMixin.record)
    # dedent, not cleandoc: cleandoc reflows the BODY relative to the docstring and the
    # result no longer parses as a function.
    tree = ast.parse(textwrap.dedent(src))
    resolve_line = surface_line = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "_resolve_evidence_anchors":
            resolve_line = node.lineno if resolve_line is None else min(resolve_line, node.lineno)
        if isinstance(fn, ast.Attribute) and fn.attr == "surface":
            surface_line = node.lineno if surface_line is None else min(surface_line, node.lineno)
    assert resolve_line is not None, "record() must resolve contributor anchors"
    assert surface_line is not None, "record() must render a surface (test anchor drifted)"
    assert resolve_line < surface_line, (
        "anchors must be resolved BEFORE kind.surface(), or the stored contributor count and the "
        "count quoted in the summary disagree"
    )


def _wrapper_calls_to_record() -> list[tuple[str, ast.Call]]:
    """Every `self.record(...)` call inside a `record_*` wrapper, across the View repositories."""
    root = Path(inspect.getfile(ViewWriteRepositoryMixin)).parent
    found: list[tuple[str, ast.Call]] = []
    for path in sorted(root.glob("*view*repository*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not fn.name.startswith("record_"):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "record"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                ):
                    found.append((f"{path.name}::{fn.name}", node))
    return found


def test_census_finds_the_known_contributor_declaring_writers() -> None:
    """Guard the census itself: if this drops to zero the AST query stopped matching anything."""
    names = {name for name, _ in _wrapper_calls_to_record()}
    assert len(names) >= 3, f"census matched too few writers ({names}) -- the AST query drifted"
    for expected in (
        "scalar_view_repository.py::record_counter",
        "scalar_view_repository.py::record_scalar_state",
        "scalar_view_repository.py::record_scalar_history",
    ):
        assert expected in names, f"{expected} vanished from the census; re-derive the boundary"


@pytest.mark.parametrize("site", _wrapper_calls_to_record(), ids=lambda s: s[0])
def test_no_wrapper_re_resolves_or_bypasses_the_chokepoint(site: tuple[str, ast.Call]) -> None:
    """A wrapper must hand contributors through untouched and let `record` normalize them.

    Resolving in a wrapper is how the original defect hid: one wrapper did it, two did not, and
    nothing failed until a graph with Episodic-anchored assertions reached the history path.
    """
    name, call = site
    for kw in call.keywords:
        if kw.arg != "episode_uuids":
            continue
        resolves_here = any(
            isinstance(n, ast.Attribute) and n.attr == "_resolve_evidence_anchors"
            for n in ast.walk(kw.value)
        )
        assert not resolves_here, (
            f"{name} resolves evidence anchors itself. Enforcement belongs in record() so every "
            "writer is covered; a per-wrapper call is what left record_counter and "
            "record_scalar_history unsatisfiable."
        )
