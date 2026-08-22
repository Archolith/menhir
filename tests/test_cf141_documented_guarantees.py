"""CF-141 -- four documented guarantees that did not hold, in files measuring 1.0000 line coverage.

The entry's own point is the reason this file exists: line coverage cannot see a comment that lies.
All five items sat in `privacy.py`, `operator_diagnostics.py` and `core/bootstrap.py`, and every one
of them was executed by the suite -- executing a line says nothing about whether the sentence above
it is true.

Only item 5 was a behaviour change; the rest were comments corrected to match code that was already
right. So most of what is asserted here is a DOCUMENTED CLAIM, checked against the code it describes.
That is unusual for a test file and is the whole point: the defect class is claim-vs-code drift, so
the assertion has to be about the claim.

Item 1 of the entry (`STRUCTURAL_FIELDS` documented but unenforced) was already fixed by CF-14 before
this was worked; `tests/test_cf14_structural_fields.py` owns it and nothing is duplicated here.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[1]
PRIVACY = REPO / "src/menhir/privacy.py"
DIAGNOSTICS = REPO / "src/menhir/operator_diagnostics.py"
BOOTSTRAP = REPO / "src/menhir/core/bootstrap.py"


# ---------------------------------------------------------------------------
# item 5 -- the only behaviour change
# ---------------------------------------------------------------------------


def test_the_graphiti_status_reports_both_conjuncts_of_its_own_gate() -> None:
    """THE ONE THAT WAS WRONG AT RUNTIME. `bootstrap` builds indices only when
    `graphiti_ready and graphiti_client_available`, but reported `"ok" if graphiti_ready`. With an
    Unavailable client the indices are not built and the status still read "ok".

    Asserted structurally because reaching the return needs a full artifacts bundle, a live graph
    adapter and two `asyncio.to_thread` hops -- a test that elaborate would be pinning the mocks.
    The claim is narrow and local: the status expression must test the same two names the gate does.
    """
    tree = ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))

    gates = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        == {"graphiti_ready", "graphiti_client_available"}
    ]
    assert len(gates) == 1, "expected exactly one two-conjunct graphiti gate"

    status_exprs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.IfExp)
        and isinstance(node.body, ast.Constant)
        and node.body.value == "ok"
        and isinstance(node.orelse, ast.Constant)
        and node.orelse.value == "skipped"
    ]
    assert len(status_exprs) == 1, "expected exactly one ok/skipped graphiti status expression"

    tested = {n.id for n in ast.walk(status_exprs[0].test) if isinstance(n, ast.Name)}
    assert tested == {"graphiti_ready", "graphiti_client_available"}, (
        f"status tests {tested}, but the gate that decides whether indices are built tests both"
    )


# ---------------------------------------------------------------------------
# item 3 -- a type comment that excluded a value the code emits
# ---------------------------------------------------------------------------


def test_every_emitted_check_status_is_a_documented_one() -> None:
    """The comment said `pass | warn | fail`; `admin_key_status` emits `"info"`. Rather than pin the
    corrected comment's wording, derive both sides: collect the status literals the module actually
    constructs and require the docs to name each one. A future fifth value fails this without anyone
    remembering to update a list."""
    src = DIAGNOSTICS.read_text(encoding="utf-8")
    tree = ast.parse(src)

    emitted = {
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "status" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
    }
    assert emitted, "found no status= literals; the extractor is broken, not the module"

    documented = src.split("status: str")[0]
    for value in sorted(emitted):
        assert f'"{value}"' in documented, f'status "{value}" is emitted but not documented'


def test_only_fail_and_warn_escalate_the_aggregate() -> None:
    """The other half of item 3's correction: the comment now asserts that "info" is
    non-escalating. Check the aggregator agrees, so the sentence is not a fresh untrue claim."""
    tree = ast.parse(DIAGNOSTICS.read_text(encoding="utf-8"))

    compared = {
        node.comparators[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Subscript)
        and isinstance(node.comparators[0], ast.Constant)
        and isinstance(node.comparators[0].value, str)
        and isinstance(node.left.slice, ast.Constant)
        and node.left.slice.value == "status"
    }
    assert compared == {"fail", "warn"}, f"aggregate escalates on {compared}"


# ---------------------------------------------------------------------------
# item 4 -- status aggregates a superset of the `checks` it returns
# ---------------------------------------------------------------------------


def test_the_superset_aggregation_is_recorded_where_it_happens() -> None:
    """NARROWER THAN FILED, and the narrowing is the finding's own correction. The entry said a
    consumer "can find no failing row in the list it was given" -- but `oauth_preflight` IS returned,
    under `oauth_resource_server`, so the row is in the response, just not under `checks`. Nothing is
    dropped; the shape is surprising. Recorded rather than restructured, because merging the lists
    would change a published response shape to fix a documentation defect."""
    src = DIAGNOSTICS.read_text(encoding="utf-8")
    assert '"oauth_resource_server": oauth_preflight' in src, (
        "the OAuth checks are no longer returned -- item 4 becomes real data loss, not a shape note"
    )
    aggregation = src.split("all_checks = list(checks)")[0]
    assert "CF-141" in aggregation.rsplit("# -- Aggregate status --", 1)[-1]


# ---------------------------------------------------------------------------
# item 2 -- a module docstring contradicting its own function docstring
# ---------------------------------------------------------------------------


def test_the_module_docstring_no_longer_claims_over_masking() -> None:
    """`redact_log_line` masks only quoted spans clearing a length-and-whitespace floor, so it
    under-masks by construction. The module docstring claimed the opposite -- and it is the sentence
    an operator reads before screen-sharing a log tail, which is why it was the one to correct
    rather than the function's own disclaiming docstring (CF-96 took the same view)."""
    from menhir import privacy

    doc = privacy.__doc__ or ""
    assert "better to over-mask than to leak" not in doc
    assert "UNDER-masks" in doc


def test_the_module_and_function_docstrings_agree_that_it_is_not_a_guarantee() -> None:
    """The contradiction, asserted from both ends. A future edit that re-strengthens either one
    without the other reintroduces exactly item 2."""
    from menhir import privacy

    module_doc = (privacy.__doc__ or "").lower()
    fn_doc = (privacy.redact_log_line.__doc__ or "").lower()

    assert "heuristic" in module_doc and "heuristic" in fn_doc
    assert "not a\n    hard guarantee" in fn_doc or "not a hard guarantee" in " ".join(fn_doc.split())
