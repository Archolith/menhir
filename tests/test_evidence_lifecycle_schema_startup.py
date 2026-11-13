"""#603: startup brings the evidence-lifecycle constraints online every time, and refuses writers
when it cannot. Online behaviour is in tests/infrastructure/test_evidence_lifecycle_schema_online.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.infrastructure.evidence_lifecycle_schema import EvidenceLifecycleSchemaError

pytestmark = [pytest.mark.unit]


def _artifacts(ensure):
    calls: list[str] = []

    def _ensure():
        calls.append("evidence_schema")
        return ensure()

    adapter = SimpleNamespace(
        retire_obsolete_indexes=lambda: [],
        ensure_vector_indexes=lambda: {},
        ensure_evidence_lifecycle_schema=_ensure,
        # An existing install: phase one is online, so its bootstrap is skipped.
        phase_one_schema_ready=lambda **_: True,
        bootstrap_phase_one=lambda: pytest.fail("phase-one bootstrap must be skipped"),
        sync_edge_counts=lambda: (calls.append("edge_counts"), 0)[1],
    )
    artifacts = SimpleNamespace(
        capabilities=SimpleNamespace(graphiti_ready=False),
        graph_adapter=adapter,
        graphiti_client=None,
    )
    return artifacts, calls


@pytest.mark.asyncio
async def test_existing_install_still_gets_the_constraints() -> None:
    from menhir.core.bootstrap import prepare_memory_runtime

    artifacts, calls = _artifacts(lambda: {"status": "ok", "skipped": False})
    result = await prepare_memory_runtime(artifacts)

    assert "evidence_schema" in calls
    assert result["evidence_lifecycle_schema"] == {"status": "ok", "skipped": False}


@pytest.mark.asyncio
async def test_constraint_failure_refuses_startup() -> None:
    from menhir.core.bootstrap import prepare_memory_runtime

    def _fail():
        raise EvidenceLifecycleSchemaError("not online")

    artifacts, calls = _artifacts(_fail)
    with pytest.raises(EvidenceLifecycleSchemaError):
        await prepare_memory_runtime(artifacts)
    assert "edge_counts" not in calls, "nothing after the gate may run"


def test_constraint_set_matches_the_activation_ddl() -> None:
    """Readiness checks exactly the constraints the DDL creates."""
    from menhir.infrastructure.evidence_lifecycle_schema import (
        EVIDENCE_LIFECYCLE_REQUIRED_CONSTRAINTS,
    )
    from menhir.infrastructure.schema import get_view_evidence_lifecycle_activation_queries

    ddl = " ".join(get_view_evidence_lifecycle_activation_queries())
    for name, _type, _entity, labels, props in EVIDENCE_LIFECYCLE_REQUIRED_CONSTRAINTS:
        assert f"CREATE CONSTRAINT {name} IF NOT EXISTS" in ddl
        assert f":{labels[0]})" in ddl and f".{props[0]} IS UNIQUE" in ddl
