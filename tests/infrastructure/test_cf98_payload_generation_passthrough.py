"""#98 -- the payload path must present the CALLER's claim generation, not one it just read.

The bug was quiet and total: the handler re-read the binding, overwrote the caller's
`identity_generation` with what it had just read, and the fence then compared that field against
the same row microseconds later. The compare-and-set compared a number with itself, so the race it
exists to catch -- caller settles, time passes, the identity transfers elsewhere, the caller writes
anyway -- was wide open on the one path where the caller is furthest in time from its own scan.

These tests are about the handler's *contract*, not the fence's Cypher (which is pinned in
`test_cf257_stale_claim.py` and its online sibling): the generation reaching the fence must be the
one the caller carried, and a payload with no claim at all must be refused rather than be handed a
freshly-minted one.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _source_of_payload_path() -> str:
    from pathlib import Path

    import menhir.core.backend_runtime_data_ops as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    start = source.index("write_project_structure cannot establish an identity")
    end = source.index("if \"symbols\" not in scan", start)
    return source[start:end]


def test_the_handler_never_assigns_identity_generation() -> None:
    """The single line that made the CAS vacuous. Its absence is the fix.

    Asserted against the source because the alternative -- driving the whole payload path with a
    fake graph to observe a field -- tests the harness more than the handler, and the property is
    a one-line invariant: this function may read a binding, it may not write the caller's claim.
    """
    section = _source_of_payload_path()

    assert "scan_obj.identity_generation =" not in section, (
        "the payload handler must not assign the claim generation; taking it from the binding it "
        "is about to be checked against is #98"
    )
    assert "scan_obj.project_id = bound_id" in section, (
        "the id is still server-resolved -- only the GENERATION comes from the caller"
    )


def test_a_payload_with_no_claim_generation_is_refused() -> None:
    """Nothing to compare means nothing was checked.

    Filling it in server-side is what the bug did. Refusing is the only other honest option: the
    endpoint is deprecated and operator-only, so the cost of refusing an older client is a clear
    error on a path that already tells callers to move to `scan_and_write_project`.
    """
    section = _source_of_payload_path()

    assert "if scan_obj.identity_generation is None:" in section
    refusal = section[section.index("if scan_obj.identity_generation is None:"):]
    assert "ProjectIdentityRefused" in refusal[:400]
    assert "scan_and_write_project" in refusal[:800], (
        "a refusal has to name the path the caller should use instead"
    )


def test_the_binding_call_survives_for_its_other_effects() -> None:
    """`bind_project_identity` is still called: it stamps `root_key` on bindings written before
    that property existed, and without it the write boundary cannot match on root_key at all.
    What changed is that its return value is no longer read."""
    section = _source_of_payload_path()

    assert "bind_project_identity" in section
    assert "binding = await" not in section, "the return value must not be captured or used"


def test_the_transport_boundary_still_carries_the_caller_generation() -> None:
    """The passthrough is only meaningful if the value survives deserialization."""
    from menhir.core.backend_shared import _project_scan_from_dict

    scan = _project_scan_from_dict(
        {
            "name": "proj",
            "root_path": "/srv/proj",
            "stack": "python",
            "description": "",
            "project_id": "11111111-2222-3333-4444-555555555555",
            "identity_generation": 7,
        }
    )

    assert scan.identity_generation == 7
    assert scan.project_id == "11111111-2222-3333-4444-555555555555"


def test_an_absent_generation_deserializes_as_none_not_zero() -> None:
    """Zero is a real comparison value -- `coalesce(p.claim_generation, 0)` in the fence -- so an
    absent claim must arrive as None and be refused, never coerced into a number that could match
    an unstamped row."""
    from menhir.core.backend_shared import _project_scan_from_dict

    scan = _project_scan_from_dict(
        {"name": "proj", "root_path": "/srv/proj", "stack": "python", "description": ""}
    )

    assert scan.identity_generation is None
