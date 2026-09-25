"""Scalar-state activation gate, extracted from `typed_scalar_service`.

Re-exported by `menhir.services.typed_scalar_service`, which remains the public import surface.
"""

from __future__ import annotations

from typing import Any


class ScalarStateNotActivatedError(RuntimeError):
    """Raised when scalar-state activation ran without leaving the required DDL online — the
    identity-versioned indexes are not ready, so recording an assertion is unsafe. Distinct from the
    repository's `ScalarStateActivationError` (which fires when activation is REFUSED over a
    legacy/incompatible store): this fires when activation was ATTEMPTED but the schema did not come
    up, so we still fail closed rather than record into an unindexed store."""


def ensure_scalar_state_activated(adapter: Any) -> dict[str, Any]:
    """Enforce the C.4.3 activation ordering: bring scalar-state online (and PASS) before any
    assertion is recorded. ALWAYS calls `adapter.activate_scalar_state()` — never short-circuits on a
    `scalar_state_schema_ready()` precheck — so a rolled-back or hand-altered store still hits the
    repository's exact-match identity-version gate (which raises `ScalarStateActivationError` over any
    incompatible node). Then verifies `scalar_state_schema_ready()` AFTER activation and fails closed
    (`ScalarStateNotActivatedError`) if the DDL is not online. Idempotent. Returns the activation
    result plus `schema_ready`."""
    result = dict(adapter.activate_scalar_state())
    if not adapter.scalar_state_schema_ready():
        raise ScalarStateNotActivatedError(
            "scalar-state activation ran but the required DDL is not ONLINE; refusing to record "
            "assertions into an unindexed store")
    result["schema_ready"] = True
    return result
