"""The View write refusal must name WHICH gate rejected it.

`create_and_supersede` drops its row for three independent reasons and every one of them used to
raise the same "must resolve to live evidence" string. That misreporting sent a real investigation
after a race condition for what was a deterministic cross-tenant bug, so each gate is pinned here.
"""

from typing import Any

import pytest

from menhir.infrastructure.view_write_repository import ViewWriteRepositoryMixin


class _StubNeo4j:
    """Returns one canned diagnosis row; `_diagnose_refusal` issues exactly one read."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def execute(self, query: str, params: dict[str, Any] | None = None, **_: Any) -> list:
        return [self._row] if self._row is not None else []


def _diagnose(row: dict[str, Any] | None, *, old_uuid: str | None = None,
              eps: list[str] | None = None) -> str:
    repo = ViewWriteRepositoryMixin(neo4j=_StubNeo4j(row))
    return repo._diagnose_refusal(
        label="Entity", key="k", old_uuid=old_uuid, eps=eps if eps is not None else ["e1"],
        namespace_key="agent-status", evidence_scope="true",
    )


def _probe(**overrides: Any) -> dict[str, Any]:
    found = {"in_tenant": True, "finalized": True, "quarantined": False, "generation": 0}
    found.update(overrides)
    return {"eid": "e1", "found": [found]}


@pytest.mark.unit
def test_concurrent_supersession_reports_lost_update_not_evidence() -> None:
    """Gate 1. The single most misleading case: no evidence problem exists at all."""
    msg = _diagnose(
        {"actual_uuids": ["winner"], "fence_generation": 0, "probes": [_probe()]},
        old_uuid="ours",
    )

    assert "LOST UPDATE" in msg
    assert "'winner'" in msg and "'ours'" in msg
    assert "NOT an evidence problem" in msg


@pytest.mark.unit
def test_cross_tenant_contributor_is_named_as_such() -> None:
    """Gate 2, the admission-audit bug: evidence is live and healthy, just in another silo."""
    msg = _diagnose({"actual_uuids": [], "fence_generation": 0,
                     "probes": [_probe(in_tenant=False)]})

    assert "DIFFERENT tenant" in msg
    assert "agent-status" in msg


@pytest.mark.unit
@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ({"eid": "e1", "found": []}, "no :Episodic/:TurnEvidence node"),
        (_probe(finalized=False), "not usable evidence"),
        (_probe(quarantined=True), "not usable evidence"),
    ],
)
def test_unresolvable_contributors_are_distinguished(probe: dict[str, Any], expected: str) -> None:
    msg = _diagnose({"actual_uuids": [], "fence_generation": 0, "probes": [probe]})

    assert expected in msg


@pytest.mark.unit
def test_ambiguous_contributor_is_reported() -> None:
    both = {"in_tenant": True, "finalized": True, "quarantined": False, "generation": 0}
    msg = _diagnose({"actual_uuids": [], "fence_generation": 0,
                     "probes": [{"eid": "e1", "found": [both, dict(both)]}]})

    assert "AMBIGUOUS" in msg


@pytest.mark.unit
def test_fence_generation_mismatch_is_not_reported_as_missing_evidence() -> None:
    """Gate 3. Evidence resolved live; only the stamping fence disagrees."""
    msg = _diagnose({"actual_uuids": [], "fence_generation": 0,
                     "probes": [_probe(generation=5)]})

    assert "FENCE GENERATION mismatch" in msg
    assert "[5]" in msg and "generation 0" in msg
    assert "must resolve to live" not in msg


@pytest.mark.unit
def test_diagnosis_failure_falls_back_to_the_generic_refusal() -> None:
    """Best-effort: a diagnosis that itself errors must never mask the refusal."""

    class _Exploding:
        def execute(self, *_a: Any, **_k: Any) -> list:
            raise RuntimeError("neo4j down")

    repo = ViewWriteRepositoryMixin(neo4j=_Exploding())
    msg = repo._diagnose_refusal(
        label="Entity", key="k", old_uuid=None, eps=["e1"],
        namespace_key="agent-status", evidence_scope="true",
    )

    assert "must resolve to live" in msg


@pytest.mark.unit
def test_empty_result_falls_back_to_the_generic_refusal() -> None:
    assert "must resolve to live" in _diagnose(None)
