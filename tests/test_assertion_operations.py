"""The operation table is complete, and no other module decides by an operation's name.

A reader that compares an operation with a literal name (``op != "occurrence"``) silently gives any
operation added later the meaning of that branch. Readers ask ``menhir.domain.assertion_operations``
instead, where a missing name means nothing (fail closed).
"""

from __future__ import annotations

import ast
import dataclasses
import logging
import pathlib
import re

import pytest

from menhir.domain import assertion_operations as ops
from menhir.domain import typed_assertion

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "menhir"
_TABLE_MODULE = "domain/assertion_operations.py"
_NAMES = frozenset(ops.OPERATION_SEMANTICS)
_CYPHER_OPERATION_RE = re.compile(
    r"\.operation\b[^\n]{0,80}?(?:'(?:" + "|".join(sorted(_NAMES)) + r")'|\"(?:"
    + "|".join(sorted(_NAMES)) + r")\")"
)

#: Sites that may still name an operation, with why. A new entry is a review trigger: route the
#: decision through the table instead.
_ALLOWED_SITES: dict[str, str] = {}


def _literal_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and node.value in _NAMES:
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in node.elts if isinstance(e, ast.Constant) and e.value in _NAMES]
    return []


def _scan(source: str) -> list[int]:
    """Line numbers where ``source`` decides by an operation name."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            if any(_literal_names(part) for part in (node.left, *node.comparators)):
                lines.append(node.lineno)
        elif isinstance(node, ast.MatchValue) and _literal_names(node.value):
            lines.append(node.lineno)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _CYPHER_OPERATION_RE.search(node.value)
        ):
            lines.append(node.lineno)
    return lines


def _operation_name_sites() -> list[str]:
    sites: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel != _TABLE_MODULE:
            sites += [f"{rel}:{line}" for line in _scan(path.read_text(encoding="utf-8"))]
    return sites


def test_every_field_is_decided_for_every_operation() -> None:
    fields = dataclasses.fields(ops.OperationSemantics)
    assert all(f.default is dataclasses.MISSING for f in fields), "fields must not default"
    assert all(f.default_factory is dataclasses.MISSING for f in fields)
    for name, entry in ops.OPERATION_SEMANTICS.items():
        assert entry.name == name
        # None: readable when stored, but no lane may write it yet (readers ship first).
        assert entry.writer_lane in (ops.SCALAR_LANE, ops.COUNTING_LANE, None)


def test_the_derived_sets_match_the_released_contract() -> None:
    assert ops.OPERATIONS == {"absolute", "delta", "expire"}
    assert ops.COUNTING_OPERATIONS == {"occurrence"}
    assert ops.ASSERTION_OPERATIONS == {"absolute", "delta", "expire", "occurrence", "approximate"}
    assert ops.ASSERTION_OPERATIONS >= ops.OPERATIONS | ops.COUNTING_OPERATIONS
    assert not ops.OPERATIONS & ops.COUNTING_OPERATIONS
    assert ops.VALUE_CLAIM_OPERATIONS == {"absolute", "delta", "expire"}
    assert ops.SETS_VALUE_OPERATIONS == {"absolute"}
    assert ops.COUNTING_OCCURRENCE_OPERATIONS == {"occurrence"}
    assert ops.HISTORY_VALUE_OPERATIONS == {"absolute", "delta", "expire", "approximate"}
    # typed_assertion re-exports the same objects, so existing importers see the table.
    assert typed_assertion.OPERATIONS is ops.OPERATIONS
    assert typed_assertion.COUNTING_OPERATIONS is ops.COUNTING_OPERATIONS
    assert typed_assertion.ASSERTION_OPERATIONS is ops.ASSERTION_OPERATIONS


def test_each_operation_has_exactly_one_role() -> None:
    for entry in ops.OPERATION_SEMANTICS.values():
        roles = [entry.sets_value, entry.adjusts_value, entry.ends_value, entry.counting_occurrence,
                 entry.approximates_value]
        assert sum(roles) == 1, entry


@pytest.mark.parametrize("operation", [None, "", "zz_future", "Absolute", " absolute", 3, b"delta"])
def test_an_unknown_operation_means_nothing(operation, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=ops.__name__)
    assert ops.semantics_of(operation) is ops.UNKNOWN
    assert not ops.is_known(operation)
    for predicate in (ops.sets_value, ops.adjusts_value, ops.ends_value,
                      ops.is_counting_occurrence, ops.is_value_claim, ops.demotes_newer_view,
                      ops.approximates_value, ops.is_listed_in_history, ops.folds_into_view):
        assert predicate(operation) is False


def test_an_unknown_operation_is_logged_once() -> None:
    ops._UNKNOWN_SEEN.discard(repr("zz_logged_once"))
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    ops.logger.addHandler(handler)
    try:
        for _ in range(3):
            ops.semantics_of("zz_logged_once")
    finally:
        ops.logger.removeHandler(handler)
    assert len(records) == 1


def test_cypher_lists_come_from_the_table() -> None:
    assert ops.VALUE_CLAIM_OPS_CYPHER == "['absolute', 'delta', 'expire']"
    assert ops.SETS_VALUE_OPS_CYPHER == "['absolute']"
    assert ops.COUNTING_OCCURRENCE_OPS_CYPHER == "['occurrence']"
    with pytest.raises(ValueError):
        ops.cypher_list(["absolute", "x' OR 1=1 //"])


def test_no_module_decides_by_an_operation_name() -> None:
    """THE RATCHET: comparisons, match cases and Cypher filters on operation names live only in
    the table module. Route a new decision through ``assertion_operations`` instead."""
    found = _operation_name_sites()
    unexplained = sorted(set(found) - set(_ALLOWED_SITES))
    assert unexplained == [], (
        "these sites compare an operation with a literal name, so a new operation would take "
        f"that branch silently; use menhir.domain.assertion_operations: {unexplained}"
    )


def test_the_scan_sees_each_form_it_bans() -> None:
    """Positive control: the ratchet's own scanner catches every banned form, and not a predicate."""
    sample = (
        'a = op != "occurrence"\n'
        'b = op in ("absolute", "delta")\n'
        'match op:\n    case "expire":\n        pass\n'
        "Q = \"MATCH (a) WHERE a.operation = 'occurrence' RETURN a\"\n"
        "c = is_counting_occurrence(op)\n"
    )
    assert sorted(_scan(sample)) == [1, 2, 4, 6]
