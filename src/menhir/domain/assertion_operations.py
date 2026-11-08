"""What each TypedAssertion operation means, in one place.

Readers ask this table (``semantics_of`` and the predicates below) instead of comparing operation
names. An operation missing from the table resolves to ``UNKNOWN``: it sets, adjusts, ends and counts
nothing and never demotes a View. A name stored before its readers learn it therefore fails closed
instead of being taken for a value. ``tests/test_assertion_operations.py`` fails when an entry leaves
a field undecided or when another module compares an operation with a literal name.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

logger = logging.getLogger(__name__)

#: the lane allowed to write an operation. The scalar parse path may emit only scalar-lane names.
SCALAR_LANE = "scalar"
COUNTING_LANE = "counting"


@dataclass(frozen=True)
class OperationSemantics:
    """How every reader treats one operation. No field has a default: a new entry decides each."""

    name: str
    writer_lane: str | None
    #: a stated value: may anchor a slot's current value (the fold's "absolute").
    sets_value: bool
    #: a signed change to a numeric value (the fold's "delta").
    adjusts_value: bool
    #: the value ENDED with no stated replacement; carries the old value (the fold's "expire").
    ends_value: bool
    #: one happening counted by the counting lane; never part of the scalar fold.
    counting_occurrence: bool
    #: a newer one in a View's own slot keeps that View advisory (the freshness gate).
    demotes_newer_view: bool


_ENTRIES: tuple[OperationSemantics, ...] = (
    OperationSemantics(
        name="absolute", writer_lane=SCALAR_LANE, sets_value=True, adjusts_value=False,
        ends_value=False, counting_occurrence=False, demotes_newer_view=False,
    ),
    OperationSemantics(
        name="delta", writer_lane=SCALAR_LANE, sets_value=False, adjusts_value=True,
        ends_value=False, counting_occurrence=False, demotes_newer_view=False,
    ),
    OperationSemantics(
        name="expire", writer_lane=SCALAR_LANE, sets_value=False, adjusts_value=False,
        ends_value=True, counting_occurrence=False, demotes_newer_view=False,
    ),
    OperationSemantics(
        name="occurrence", writer_lane=COUNTING_LANE, sets_value=False, adjusts_value=False,
        ends_value=False, counting_occurrence=True, demotes_newer_view=True,
    ),
)

#: every operation a stored TypedAssertion may carry, by name.
OPERATION_SEMANTICS: Mapping[str, OperationSemantics] = MappingProxyType(
    {entry.name: entry for entry in _ENTRIES}
)

#: what an unrecognised or missing operation means everywhere: nothing.
UNKNOWN = OperationSemantics(
    name="", writer_lane=None, sets_value=False, adjusts_value=False, ends_value=False,
    counting_occurrence=False, demotes_newer_view=False,
)

_UNKNOWN_SEEN: set[str] = set()
_UNKNOWN_SEEN_MAX = 64
_UNKNOWN_LOCK = threading.Lock()


def _note_unknown(operation: object) -> None:
    label = repr(operation)[:80]
    with _UNKNOWN_LOCK:
        if label in _UNKNOWN_SEEN or len(_UNKNOWN_SEEN) >= _UNKNOWN_SEEN_MAX:
            return
        _UNKNOWN_SEEN.add(label)
    logger.warning("unknown typed-assertion operation %s; treated as carrying no value", label)


def semantics_of(operation: object) -> OperationSemantics:
    """The table entry for ``operation``; ``UNKNOWN`` (logged once) for anything else."""
    if isinstance(operation, str):
        entry = OPERATION_SEMANTICS.get(operation)
        if entry is not None:
            return entry
    _note_unknown(operation)
    return UNKNOWN


def is_known(operation: object) -> bool:
    return isinstance(operation, str) and operation in OPERATION_SEMANTICS


def sets_value(operation: object) -> bool:
    return semantics_of(operation).sets_value


def adjusts_value(operation: object) -> bool:
    return semantics_of(operation).adjusts_value


def ends_value(operation: object) -> bool:
    return semantics_of(operation).ends_value


def is_counting_occurrence(operation: object) -> bool:
    return semantics_of(operation).counting_occurrence


def is_value_claim(operation: object) -> bool:
    """A scalar-fold input: sets, adjusts or ends a value. Unknown names are not."""
    s = semantics_of(operation)
    return s.sets_value or s.adjusts_value or s.ends_value


def demotes_newer_view(operation: object) -> bool:
    return semantics_of(operation).demotes_newer_view


def operations_where(predicate: Callable[[OperationSemantics], bool]) -> frozenset[str]:
    """Names of every table entry the predicate accepts."""
    return frozenset(entry.name for entry in _ENTRIES if predicate(entry))


#: operations the scalar lane may emit.
OPERATIONS: frozenset[str] = operations_where(lambda s: s.writer_lane == SCALAR_LANE)
#: operations only the counting lane may write.
COUNTING_OPERATIONS: frozenset[str] = operations_where(lambda s: s.writer_lane == COUNTING_LANE)
#: every operation a stored TypedAssertion may carry.
ASSERTION_OPERATIONS: frozenset[str] = frozenset(OPERATION_SEMANTICS)
#: scalar-fold inputs (see ``is_value_claim``).
VALUE_CLAIM_OPERATIONS: frozenset[str] = operations_where(
    lambda s: s.sets_value or s.adjusts_value or s.ends_value
)
SETS_VALUE_OPERATIONS: frozenset[str] = operations_where(lambda s: s.sets_value)
COUNTING_OCCURRENCE_OPERATIONS: frozenset[str] = operations_where(lambda s: s.counting_occurrence)

_CYPHER_NAME_RE = re.compile(r"[a-z_]+")


def cypher_list(operations: Iterable[str]) -> str:
    """A Cypher list literal of table names, sorted, for an ``IN`` filter in a query string."""
    names = sorted(operations)
    for name in names:
        if name not in OPERATION_SEMANTICS or not _CYPHER_NAME_RE.fullmatch(name):
            raise ValueError(f"not a table operation: {name!r}")
    return "[" + ", ".join(f"'{name}'" for name in names) + "]"


#: Cypher list literals, built from the table, for query strings.
VALUE_CLAIM_OPS_CYPHER: str = cypher_list(VALUE_CLAIM_OPERATIONS)
SETS_VALUE_OPS_CYPHER: str = cypher_list(SETS_VALUE_OPERATIONS)
COUNTING_OCCURRENCE_OPS_CYPHER: str = cypher_list(COUNTING_OCCURRENCE_OPERATIONS)

__all__ = [
    "ASSERTION_OPERATIONS",
    "COUNTING_LANE",
    "COUNTING_OCCURRENCE_OPERATIONS",
    "COUNTING_OCCURRENCE_OPS_CYPHER",
    "COUNTING_OPERATIONS",
    "OPERATIONS",
    "OPERATION_SEMANTICS",
    "OperationSemantics",
    "SCALAR_LANE",
    "SETS_VALUE_OPERATIONS",
    "SETS_VALUE_OPS_CYPHER",
    "UNKNOWN",
    "VALUE_CLAIM_OPERATIONS",
    "VALUE_CLAIM_OPS_CYPHER",
    "adjusts_value",
    "cypher_list",
    "demotes_newer_view",
    "ends_value",
    "is_counting_occurrence",
    "is_known",
    "is_value_claim",
    "operations_where",
    "semantics_of",
    "sets_value",
]
