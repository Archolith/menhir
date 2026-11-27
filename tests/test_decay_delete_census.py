"""Census extension: the decay delete is reached only through a gated call site.

``test_auto_mutation_census`` classifies ``bridge_and_delete`` as internal to the coordinator, so a
new caller of ``DeleteCoordinator.delete_decayed_node`` that skipped ``auto_delete_mode()`` would
pass it. This pins that entry point the same way the census pins the others.
"""

from __future__ import annotations

import ast

import pytest

from tests.test_auto_mutation_census import (
    _attr_or_name,
    _functions,
    _own_nodes,
    _sources,
    calls_gate,
)

pytestmark = pytest.mark.unit

ENTRY = "delete_decayed_node"
ALLOWED_SITES = {"services/lifecycle_decay.py::LifecycleDecayMixin._run_decay"}


def _entry_sites() -> dict[str, ast.AST]:
    sites = {}
    for rel, source in _sources():
        for qualname, func in _functions(ast.parse(source)):
            if qualname == ENTRY:
                continue
            for node in _own_nodes(func):
                if isinstance(node, ast.Call) and ENTRY in (
                    _attr_or_name(node.func), *(_attr_or_name(a) for a in node.args)
                ):
                    sites[f"{rel}::{qualname}"] = func
    return sites


def test_delete_decayed_node_is_called_only_from_the_gated_decay_sweep():
    sites = _entry_sites()

    assert set(sites) == ALLOWED_SITES
    for site, func in sites.items():
        assert calls_gate(func, "delete"), f"{site} reaches the decay delete without auto_delete_mode()"
