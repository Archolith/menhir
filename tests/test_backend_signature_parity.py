"""Signature-parity test across the three MemoryBackend surfaces (SSOT-01).

menhir has three implementations of the backend contract: the abstract
``MemoryBackend`` protocol, the in-process ``RuntimeProvider``, and the
HTTP-backed ``BackendClient``. A prior audit found that all three expose the
same *method names* (67 of them), but nothing enforced that their *parameter
names and defaults* also matched -- which is exactly how BackendClient.recall
silently dropped ``include_invalidated`` while the other two surfaces had it.

This test asserts full keyword-parameter parity (names + defaults) for every
method the three surfaces share, so a future parameter addition to one
surface without the others fails CI instead of shipping a broken call path.
"""

from __future__ import annotations

import inspect

import pytest

from menhir.core.backend_impl import BackendClient, RuntimeProvider
from menhir.core.backend_protocol import MemoryBackend

_SURFACES = {
    "MemoryBackend": MemoryBackend,
    "RuntimeProvider": RuntimeProvider,
    "BackendClient": BackendClient,
}

# aclose is BackendClient-only HTTP transport lifecycle plumbing (closes the
# owned httpx.AsyncClient) -- it is not part of the backend contract and has
# no RuntimeProvider/MemoryBackend counterpart by design.
_METHOD_NAME_EXCLUDE = {"aclose"}

# Known signature drift not yet remediated. Do not add entries here without a
# corresponding plan reference in .agent/plans/ssot-remediation-2026-07-11.md.
_KNOWN_SIGNATURE_DRIFT: set[str] = set()


def _public_async_methods(cls: type) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
        and name not in _METHOD_NAME_EXCLUDE
        and inspect.iscoroutinefunction(member)
    }


def _kwonly_signature(cls: type, name: str) -> dict[str, object]:
    """Name -> default (or the sentinel _EMPTY for required) for keyword-only params."""
    sig = inspect.signature(getattr(cls, name))
    return {
        p.name: p.default
        for p in sig.parameters.values()
        if p.kind == inspect.Parameter.KEYWORD_ONLY
    }


@pytest.mark.unit
def test_all_three_surfaces_expose_the_same_method_names() -> None:
    method_sets = {label: _public_async_methods(cls) for label, cls in _SURFACES.items()}
    baseline_name, baseline = next(iter(method_sets.items()))
    for label, methods in method_sets.items():
        missing = baseline - methods
        extra = methods - baseline
        assert not missing and not extra, (
            f"{label} method set differs from {baseline_name}: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )


@pytest.mark.unit
def test_shared_methods_have_identical_keyword_signatures() -> None:
    """Catches drift like SSOT-01: same method name, different kwargs/defaults."""
    shared_methods = set.intersection(
        *(_public_async_methods(cls) for cls in _SURFACES.values())
    ) - _KNOWN_SIGNATURE_DRIFT
    mismatches: list[str] = []
    for name in sorted(shared_methods):
        signatures = {
            label: _kwonly_signature(cls, name) for label, cls in _SURFACES.items()
        }
        baseline_label, baseline_sig = next(iter(signatures.items()))
        for label, sig in signatures.items():
            if sig != baseline_sig:
                mismatches.append(
                    f"{name}: {baseline_label}={baseline_sig} != {label}={sig}"
                )
    assert not mismatches, "Backend surface signature drift:\n" + "\n".join(mismatches)


@pytest.mark.unit
def test_scan_for_conflicts_default_limit_is_150_everywhere() -> None:
    """SSOT-06: 100/150/500 default fragmentation, canonicalized on 150.

    Covers all four call sites: the backend contract layer (already asserted
    generically above) plus the two MCP-tool-layer entry points that had
    their own independently hardcoded defaults -- the plain importable
    function and the registered tool endpoint.
    """
    from menhir.mcp.tools.conflict.scan_conflicts import (
        ScanConflictsTool,
        scan_for_conflicts,
    )

    assert inspect.signature(scan_for_conflicts).parameters["limit"].default == 150
    assert inspect.signature(ScanConflictsTool.endpoint).parameters["limit"].default == 150
