"""CF-76 — one canonical default-namespace constant, single-sourced.

The two domain aliases and the repository's ``DEFAULT_NAMESPACE`` must derive from
``domain.namespace.DEFAULT_NAMESPACE`` rather than re-spelling the literal.

**`is` cannot check that here, and the first version of this file assumed it could.** CPython
interns short string literals, so two independently written ``"default"`` constants ARE the same
object — `is` passes on exactly the defect it was meant to catch. Verified by mutation: re-spelling
``DEFAULT_TODO_NAMESPACE = "default"`` left all identity assertions green.

So the binding is asserted STRUCTURALLY, by parsing each module and checking the constant is
assigned from the canonical NAME, not from a string literal. The identity assertions are kept
underneath as a cheap consistency check, with their real limitation stated rather than implied.

CF-150 later folded the two private ``_safe_namespace`` helpers into
``domain.namespace.normalize_namespace``. The two aliases below are imported *through each
repository module* rather than from the domain module, so these assertions still exercise the name
each repository's own call sites resolve. That the call sites actually reach it is asserted
structurally in ``test_cf150_normalize_namespace_single_source.py``.
"""

from __future__ import annotations

import pytest

from menhir.domain.namespace import DEFAULT_NAMESPACE
from menhir.domain.todo_location import DEFAULT_TODO_NAMESPACE
from menhir.domain.work_artifact import DEFAULT_ARTIFACT_NAMESPACE
from menhir.infrastructure.todo_repository import (
    normalize_namespace as _todo_safe_namespace,
)
from menhir.infrastructure.work_artifact_repository import (
    normalize_namespace as _artifact_safe_namespace,
)


@pytest.mark.unit
def test_todo_namespace_is_the_canonical_object() -> None:
    # Consistency only -- see the module docstring: interning makes this pass on a re-spelled
    # literal too. `test_constants_are_assigned_from_the_canonical_name` is the real guard.
    assert DEFAULT_TODO_NAMESPACE is DEFAULT_NAMESPACE


@pytest.mark.unit
def test_artifact_namespace_is_the_canonical_object() -> None:
    assert DEFAULT_ARTIFACT_NAMESPACE is DEFAULT_NAMESPACE


@pytest.mark.unit
def test_repository_default_namespace_is_not_a_local_rebind() -> None:
    # The shadowing rebind is gone: inside the repository the name now IS the
    # canonical constant, so it cannot silently diverge from it.
    import menhir.infrastructure.todo_repository as todo_repo

    assert todo_repo.DEFAULT_NAMESPACE is DEFAULT_NAMESPACE


@pytest.mark.parametrize(
    "namespace",
    [None, "", "   ", "tenant-a", "  tenant-a  "],
)
def test_both_safe_namespace_helpers_agree(namespace) -> None:
    # Was: separate functions in separate modules, CF-76 merging only their constant. Since CF-150
    # they are one function, so this now holds by construction -- kept as the behavioural half,
    # with `test_call_sites_resolve_the_canonical_helper` in the CF-150 file as the structural guard.
    assert _todo_safe_namespace(namespace) == _artifact_safe_namespace(namespace)


@pytest.mark.parametrize(
    "namespace",
    [None, "", "   ", "tenant-a", "  tenant-a  "],
)
def test_safe_namespace_resolves_to_canonical_default(namespace) -> None:
    expected = DEFAULT_NAMESPACE if not (namespace or "").strip() else (namespace or "").strip()
    assert _todo_safe_namespace(namespace) == expected
    assert _artifact_safe_namespace(namespace) == expected


@pytest.mark.unit
def test_positive_control_value_is_still_default() -> None:
    # A fix that aliased everything to the wrong string would pass the identity
    # tests above; the value itself must remain "default".
    assert DEFAULT_NAMESPACE == "default"
    assert DEFAULT_TODO_NAMESPACE == "default"
    assert DEFAULT_ARTIFACT_NAMESPACE == "default"


@pytest.mark.parametrize(
    ("module_path", "constant"),
    [
        ("src/menhir/domain/todo_location.py", "DEFAULT_TODO_NAMESPACE"),
        ("src/menhir/domain/work_artifact.py", "DEFAULT_ARTIFACT_NAMESPACE"),
    ],
)
@pytest.mark.unit
def test_constants_are_assigned_from_the_canonical_name(module_path: str, constant: str) -> None:
    """The real guard. Each alias must be assigned FROM `DEFAULT_NAMESPACE`, not from a literal.

    This is what interning prevents `is` from checking, and re-spelling the literal is precisely
    the state CF-76 describes -- three constants that agree today and can silently diverge.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    tree = ast.parse((root / module_path).read_text(encoding="utf-8"))

    assigned_from = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == constant for t in node.targets
        ):
            assigned_from = node.value

    assert assigned_from is not None, f"{constant} is not assigned in {module_path}"
    assert isinstance(assigned_from, ast.Name), (
        f"{constant} is assigned from {ast.dump(assigned_from)[:60]}, not from a name -- "
        "a re-spelled literal is the CF-76 defect"
    )
    assert assigned_from.id == "DEFAULT_NAMESPACE"


@pytest.mark.unit
def test_the_repository_does_not_rebind_the_canonical_name() -> None:
    """The shadowing half of CF-76: `DEFAULT_NAMESPACE = DEFAULT_TODO_NAMESPACE` inside the
    repository made the canonical name mean something else in that module."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    tree = ast.parse(
        (root / "src/menhir/infrastructure/todo_repository.py").read_text(encoding="utf-8")
    )

    rebinds = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "DEFAULT_NAMESPACE" for t in node.targets)
    ]

    assert rebinds == [], "todo_repository rebinds DEFAULT_NAMESPACE; it must import it directly"
