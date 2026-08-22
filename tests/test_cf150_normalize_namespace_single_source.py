"""CF-150 -- one `normalize_namespace`, and the call sites actually read it.

The finding was never a behavioural divergence. Three declarations agreed on all eight inputs the
register tabulated; the finding is that *nothing kept them agreeing*, on the very field that is the
tenancy boundary. `domain/merge_eligibility.py` held the canonical one with **zero callers outside
its own module** (CF-147), while each repository carried a private `_safe_namespace` doing the same
thing against its own alias of the default constant.

**Identity alone is not the guard, and this is trap T17.** Asserting
`todo_repository.normalize_namespace is domain.namespace.normalize_namespace` proves a name is
re-exported; it proves nothing about which function the repository's own code *calls*. A module can
import the survivor at the top and still call a resurrected local helper twelve hundred lines down.
So the load-bearing tests here parse each module and assert (a) no module redeclares the helper and
(b) every namespace-resolution call site names the canonical function.

The behavioural table is reproduced verbatim from the register so a future change to the survivor
has to disagree with the evidence that closed the finding.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from menhir.domain.namespace import DEFAULT_NAMESPACE, normalize_namespace

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[1]

#: Every module that used to declare its own copy, plus the one that declared the canonical one.
CONSUMERS = (
    "src/menhir/domain/merge_eligibility.py",
    "src/menhir/infrastructure/todo_repository.py",
    "src/menhir/infrastructure/work_artifact_repository.py",
)


def _tree(rel: str) -> ast.Module:
    return ast.parse((REPO / rel).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the structural guard -- what identity cannot check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", CONSUMERS)
def test_no_module_redeclares_the_helper(rel: str) -> None:
    """A re-added local `_safe_namespace` or `normalize_namespace` is the CF-150 defect returning."""
    redeclared = [
        node.name
        for node in ast.walk(_tree(rel))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in ("normalize_namespace", "_safe_namespace")
    ]
    assert redeclared == [], f"{rel} declares {redeclared}; it must import the canonical helper"


@pytest.mark.parametrize("rel", CONSUMERS)
def test_each_consumer_imports_it_from_the_domain_module(rel: str) -> None:
    imported = [
        alias.name
        for node in ast.walk(_tree(rel))
        if isinstance(node, ast.ImportFrom) and node.module == "menhir.domain.namespace"
        for alias in node.names
    ]
    assert "normalize_namespace" in imported, f"{rel} does not import it from menhir.domain.namespace"


@pytest.mark.parametrize("rel", CONSUMERS)
def test_call_sites_resolve_the_canonical_helper(rel: str) -> None:
    """THE ONE THAT MATTERS (trap T17). Import plus identity would both pass while the real call
    sites still went through a resurrected local copy. Assert on the calls themselves."""
    calls = [
        node.func.id
        for node in ast.walk(_tree(rel))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "normalize_namespace" in calls, f"{rel} imports the helper but never calls it"
    assert "_safe_namespace" not in calls, f"{rel} still calls a local _safe_namespace"


def test_the_survivor_is_one_object_across_every_consumer() -> None:
    """Cheap consistency check. Kept underneath the structural ones, not in place of them --
    interning cannot fool this the way it fools CF-76's constant identity, but a stale re-export
    still would."""
    import menhir.domain.merge_eligibility as merge
    import menhir.infrastructure.todo_repository as todo
    import menhir.infrastructure.work_artifact_repository as artifact

    assert merge.normalize_namespace is normalize_namespace
    assert todo.normalize_namespace is normalize_namespace
    assert artifact.normalize_namespace is normalize_namespace


# ---------------------------------------------------------------------------
# behaviour -- the register's own table, so the survivor cannot drift off its evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "default"),
        ("", "default"),
        ("   ", "default"),
        ("default", "default"),
        ("DEFAULT", "DEFAULT"),
        ("  MyProj  ", "MyProj"),
        ("tenant-a", "tenant-a"),
        ("  tenant-a  ", "tenant-a"),
    ],
)
def test_the_register_table_still_holds(value, expected: str) -> None:
    assert normalize_namespace(value) == expected


def test_case_is_preserved_not_folded() -> None:
    """POSITIVE CONTROL. A 'normalize' that lowercased would pass every default-collapsing test
    above and silently merge two distinct silos -- the worst available failure on a tenancy field."""
    assert normalize_namespace("TenantA") != normalize_namespace("tenanta")


def test_the_default_comes_from_the_canonical_constant() -> None:
    """Not a re-spelled literal. CF-76's lesson: interning means `==` cannot tell those apart, so
    check the source."""
    fn = next(
        node
        for node in ast.walk(_tree("src/menhir/domain/namespace.py"))
        if isinstance(node, ast.FunctionDef) and node.name == "normalize_namespace"
    )
    literals = [
        node.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == "default"
    ]
    assert literals == [], "normalize_namespace re-spells the 'default' literal instead of using the constant"
    assert normalize_namespace(None) == DEFAULT_NAMESPACE


# ---------------------------------------------------------------------------
# the boundary CF-150 must NOT cross
# ---------------------------------------------------------------------------


def test_it_is_not_merged_with_stamped_namespace() -> None:
    """`stamped_namespace('')` returns `''` today -- the pre-canonicalization spelling that stage 2
    of the default-namespace migration plan is responsible for changing. Folding the two helpers
    together would enact that stage as a side effect of a hygiene fix, with no read-side migration
    shipped ahead of it. Pinned so the next person to notice the near-duplication reads this first."""
    from menhir.domain.namespace import stamped_namespace

    assert stamped_namespace("") == ""
    assert normalize_namespace("") == DEFAULT_NAMESPACE
