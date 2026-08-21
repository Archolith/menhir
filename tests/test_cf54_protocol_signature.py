"""CF-54: ScalarConsolidationGraph must declare the kwarg its own call site uses.

The Protocol under-declared `scalar_state_service` as `(self)` while the same module calls it
with `scalar_history_enabled=...` and the real adapter accepts that kwarg. A type checker
reading the Protocol flags the call, and a second adapter written against it would raise
TypeError at that call site. Fix: the Protocol method matches the implementation's signature.
"""

from __future__ import annotations

import inspect

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.scalar_consolidation import ScalarConsolidationGraph

_FAKE_SELF = object()
_KWARG = "scalar_history_enabled"
_DEFAULT = False


def _protocol_sig() -> inspect.Signature:
    return inspect.signature(ScalarConsolidationGraph.scalar_state_service)


def _impl_sig() -> inspect.Signature:
    return inspect.signature(MemoryGraphAdapter.scalar_state_service)


@pytest.mark.unit
def test_protocol_matches_implementation_signature() -> None:
    """Signature agreement, structurally: name, keyword-only kind, and default all match."""
    proto_param = _protocol_sig().parameters[_KWARG]
    impl_param = _impl_sig().parameters[_KWARG]
    assert proto_param.name == impl_param.name == _KWARG
    assert proto_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert impl_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert proto_param.default == impl_param.default == _DEFAULT


@pytest.mark.unit
def test_call_site_is_satisfiable_by_protocol() -> None:
    """The call the module makes must bind on the Protocol signature (was a TypeError)."""
    bound = _protocol_sig().bind(_FAKE_SELF, scalar_history_enabled=True)
    assert bound.arguments[_KWARG] is True


@pytest.mark.unit
def test_no_argument_call_still_binds() -> None:
    """POSITIVE CONTROL: the kwarg has a default, so a bare call still binds."""
    bound = _protocol_sig().bind(_FAKE_SELF)
    assert _protocol_sig().parameters[_KWARG].default == _DEFAULT
    assert _KWARG not in bound.arguments  # default applies, not a required argument
