"""Unit tests for the explorer candidates review surface."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from menhir.config import MemorySettings
from menhir.explorer import create_app


@dataclass
class _StubCandidateRepo:
    calls: list[dict[str, object]] = field(default_factory=list)

    _CANDIDATE_ROWS = {
        "candidate-1": {
            "uuid": "candidate-1",
            "name": "Candidate Entity 1",
            "content": "This is a candidate entity with some content",
            "source": "enrichment-engine",
            "cluster_id": "cluster-x",
            "kind": "person",
            "type": "tech-lead",
            "label": "potential-contact",
            "evidence_strength": 0.87,
            "distinct_sessions": 3,
            "first_seen": "2026-06-10T10:00:00Z",
            "last_seen": "2026-06-20T14:30:00Z",
            "notes": ["high confidence", "validated by session-2"],
            "created_at": "2026-06-10T10:00:00Z",
        },
        "candidate-2": {
            "uuid": "candidate-2",
            "name": "Candidate Entity 2",
            "content": "Another candidate with different characteristics",
            "source": "llm-inference",
            "cluster_id": "cluster-y",
            "kind": "organization",
            "type": "startup",
            "label": "emerging-partner",
            "evidence_strength": 0.65,
            "distinct_sessions": 1,
            "first_seen": "2026-06-19T09:00:00Z",
            "last_seen": "2026-06-20T11:15:00Z",
            "notes": None,
            "created_at": "2026-06-19T09:00:00Z",
        },
    }

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        params = params or {}
        self.calls.append({"query": query, "params": params})

        # Explorer's own read-only listing query (unaffected by SSOT-05 -- Explorer
        # still reads directly; only mutations now route through CandidateService).
        if "MATCH (n:Entity)" in query and "n.scope = 'CANDIDATE'" in query:
            return list(self._CANDIDATE_ROWS.values())

        # CandidateRepository.promote_candidate (SSOT-05: canonical approval path)
        if "SET n.scope = 'PERSISTENT'" in query:
            row = self._CANDIDATE_ROWS.get(str(params.get("uuid")))
            return [{"promoted": 1 if row is not None else 0}]

        # CandidateRepository.reject_candidate (SSOT-05: canonical rejection path)
        if "DETACH DELETE n" in query:
            row = self._CANDIDATE_ROWS.get(str(params.get("uuid")))
            return [{"rejected": 1 if row is not None else 0}]

        # CandidateRepository.fetch_candidate (SSOT-05: CandidateService looks the
        # candidate up before promoting/rejecting it)
        if "{uuid: $uuid}" in query and "n.scope = 'CANDIDATE'" in query:
            row = self._CANDIDATE_ROWS.get(str(params.get("uuid")))
            return [row] if row is not None else []

        return []

    def close(self) -> None:
        return None


@pytest.mark.unit
def test_candidates_query_filters_scope() -> None:
    repo = _StubCandidateRepo()
    app = create_app(settings=MemorySettings(), repo=repo)
    with TestClient(app) as client:
        response = client.get("/explorer")
    assert response.status_code == 200
    assert "Candidate Entity 1" in response.text
    assert "Candidate Entity 2" in response.text
    assert len([c for c in repo.calls if "n.scope = 'CANDIDATE'" in c["query"]]) > 0


@pytest.mark.unit
def test_approve_candidate_success() -> None:
    repo = _StubCandidateRepo()
    app = create_app(settings=MemorySettings(), repo=repo)
    with TestClient(app) as client:
        response = client.post("/explorer/candidates/candidate-1/approve")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "approved"
    assert payload["uuid"] == "candidate-1"


@pytest.mark.unit
def test_approve_candidate_not_found() -> None:
    repo = _StubCandidateRepo()
    app = create_app(settings=MemorySettings(), repo=repo)
    with TestClient(app) as client:
        response = client.post("/explorer/candidates/missing-uuid/approve")
    assert response.status_code == 404


@pytest.mark.unit
def test_reject_candidate_success() -> None:
    repo = _StubCandidateRepo()
    app = create_app(settings=MemorySettings(), repo=repo)
    with TestClient(app) as client:
        response = client.post("/explorer/candidates/candidate-2/reject")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "rejected"
    assert payload["uuid"] == "candidate-2"


@pytest.mark.unit
def test_reject_candidate_not_found() -> None:
    repo = _StubCandidateRepo()
    app = create_app(settings=MemorySettings(), repo=repo)
    with TestClient(app) as client:
        response = client.post("/explorer/candidates/missing-uuid/reject")
    assert response.status_code == 404


@pytest.mark.unit
def test_candidates_partial_renders() -> None:
    repo = _StubCandidateRepo()
    app = create_app(settings=MemorySettings(), repo=repo)
    with TestClient(app) as client:
        response = client.get("/explorer/partials/candidates")
    assert response.status_code == 200
    assert "Candidate Entity 1" in response.text
    assert "Candidate Entity 2" in response.text
    assert "cluster-x" in response.text
    assert "cluster-y" in response.text


@pytest.mark.unit
def test_candidates_partial_empty_state() -> None:
    class _EmptyRepo(_StubCandidateRepo):
        def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
            params = params or {}
            self.calls.append({"query": query, "params": params})
            return []

    repo = _EmptyRepo()
    app = create_app(settings=MemorySettings(), repo=repo)
    with TestClient(app) as client:
        response = client.get("/explorer/partials/candidates")
    assert response.status_code == 200
    assert "No candidate entities awaiting review" in response.text
