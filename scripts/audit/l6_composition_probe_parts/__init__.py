"""Facade package re-exporting the decomposed L6 composition probe building blocks.

``scripts/audit/l6_composition_probe.py`` imports every name below from this package so
the original ``l6_composition_probe`` module path keeps exposing the same surface.
"""

from .corpus import (
    CATEGORIES as CATEGORIES,
    CONTROL_SIGNALS as CONTROL_SIGNALS,
    LOSSY_BOUNDARIES as LOSSY_BOUNDARIES,
    SKIP_DIRS as SKIP_DIRS,
    WEAKENING_PARAMS as WEAKENING_PARAMS,
)
from .indexing import (
    ROOT as ROOT,
    SRC as SRC,
    TESTS as TESTS,
    CallSite as CallSite,
    FuncDef as FuncDef,
    ModuleIndex as ModuleIndex,
    _annotation as _annotation,
    _callee_name as _callee_name,
    _handler_names as _handler_names,
    _rel as _rel,
    index_source as index_source,
    iter_py as iter_py,
)
from .scoring import (
    Pair as Pair,
    analyse as analyse,
    classify as classify,
    context_boundaries as context_boundaries,
    index_tests as index_tests,
    propagate_control_signals as propagate_control_signals,
)
