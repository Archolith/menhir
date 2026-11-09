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
#: the operation code (never the model) assigns to a hedged single number.
APPROXIMATE_OPERATION = "approximate"


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
    #: a hedged value ("close to 1300"): shown beside a View, never sets or changes its value.
    approximates_value: bool
    #: listed as an entry of the slot's scalar history.
    listed_in_history: bool
    #: a newer one in a View's own slot keeps that View advisory (the freshness gate).
    demotes_newer_view: bool
    #: the perceiving model may name it in its output. False: code assigns it after the model answers.
    model_emittable: bool


_ENTRIES: tuple[OperationSemantics, ...] = (
    OperationSemantics(
        name="absolute", writer_lane=SCALAR_LANE, sets_value=True, adjusts_value=False,
        ends_value=False, counting_occurrence=False, approximates_value=False,
        listed_in_history=True, demotes_newer_view=False,
        model_emittable=True,
    ),
    OperationSemantics(
        name="delta", writer_lane=SCALAR_LANE, sets_value=False, adjusts_value=True,
        ends_value=False, counting_occurrence=False, approximates_value=False,
        listed_in_history=True, demotes_newer_view=False,
        model_emittable=True,
    ),
    OperationSemantics(
        name="expire", writer_lane=SCALAR_LANE, sets_value=False, adjusts_value=False,
        ends_value=True, counting_occurrence=False, approximates_value=False,
        listed_in_history=True, demotes_newer_view=False,
        model_emittable=True,
    ),
    OperationSemantics(
        name="occurrence", writer_lane=COUNTING_LANE, sets_value=False, adjusts_value=False,
        ends_value=False, counting_occurrence=True, approximates_value=False,
        listed_in_history=True, demotes_newer_view=True,
        model_emittable=True,
    ),
    # #519. Written by code, never by the model: the scalar lane relabels a hedged single number as
    # approximate when personal_memory_scalar_approximate_enabled is on. writer_lane=SCALAR_LANE
    # lets a stored row and its identity validate; model_emittable=False keeps it out of
    # MODEL_OPERATIONS, so a model-named "approximate" is rejected with the flag on or off.
    OperationSemantics(
        name="approximate", writer_lane=SCALAR_LANE, sets_value=False, adjusts_value=False,
        ends_value=False, counting_occurrence=False, approximates_value=True,
        listed_in_history=True, demotes_newer_view=True,
        model_emittable=False,
    ),
)

#: every operation a stored TypedAssertion may carry, by name.
OPERATION_SEMANTICS: Mapping[str, OperationSemantics] = MappingProxyType(
    {entry.name: entry for entry in _ENTRIES}
)

#: what an unrecognised or missing operation means everywhere: nothing.
UNKNOWN = OperationSemantics(
    name="", writer_lane=None, sets_value=False, adjusts_value=False, ends_value=False,
    counting_occurrence=False, approximates_value=False, listed_in_history=False,
    demotes_newer_view=False, model_emittable=False,
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


def folds_into_view(operation: object) -> bool:
    """A fold input: a value claim, or a counting occurrence (folded only with counting on)."""
    s = semantics_of(operation)
    return s.sets_value or s.adjusts_value or s.ends_value or s.counting_occurrence


def approximates_value(operation: object) -> bool:
    return semantics_of(operation).approximates_value


def is_listed_in_history(operation: object) -> bool:
    return semantics_of(operation).listed_in_history


def demotes_newer_view(operation: object) -> bool:
    return semantics_of(operation).demotes_newer_view


def operations_where(predicate: Callable[[OperationSemantics], bool]) -> frozenset[str]:
    """Names of every table entry the predicate accepts."""
    return frozenset(entry.name for entry in _ENTRIES if predicate(entry))


#: operations the scalar lane may write (code-assigned ones included).
OPERATIONS: frozenset[str] = operations_where(lambda s: s.writer_lane == SCALAR_LANE)
#: operations the perceiving model may name in a scalar-lane row (a subset of OPERATIONS).
MODEL_OPERATIONS: frozenset[str] = operations_where(
    lambda s: s.writer_lane == SCALAR_LANE and s.model_emittable
)
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
#: history entries other than counting occurrences (those are listed only with counting on).
HISTORY_VALUE_OPERATIONS: frozenset[str] = operations_where(
    lambda s: s.listed_in_history and not s.counting_occurrence
)

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
HISTORY_VALUE_OPS_CYPHER: str = cypher_list(HISTORY_VALUE_OPERATIONS)

__all__ = [
    "APPROXIMATE_OPERATION",
    "ASSERTION_OPERATIONS",
    "COUNTING_LANE",
    "COUNTING_OCCURRENCE_OPERATIONS",
    "COUNTING_OCCURRENCE_OPS_CYPHER",
    "COUNTING_OPERATIONS",
    "HISTORY_VALUE_OPERATIONS",
    "HISTORY_VALUE_OPS_CYPHER",
    "MODEL_OPERATIONS",
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
    "approximates_value",
    "cypher_list",
    "demotes_newer_view",
    "ends_value",
    "folds_into_view",
    "is_counting_occurrence",
    "is_known",
    "is_listed_in_history",
    "is_value_claim",
    "operations_where",
    "semantics_of",
    "sets_value",
]
