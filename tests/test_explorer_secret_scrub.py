"""#577: the explorer credential-scrubs stored rows before rendering."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from menhir.config import MemorySettings
from menhir.explorer import create_app
from menhir.explorer.app import TEMPLATES, JSONResponse

AWS = "AKIAIOSFODNN7EXAMPLE"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
LEAK = f"aws {AWS} and gh {GH}"


class _Repo:
    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        params = params or {}
        if "MATCH (e:Episodic)" in query and "e.processing_state IN ['PENDING', 'ENRICHING']" in query:
            return [{
                "uuid": "q1", "name": LEAK, "session_id": "s1", "source": "t",
                "processing_state": "PENDING", "preview": LEAK,
            }]
        if "RETURN {\n            uuid: n.uuid" in query:
            return [{"detail": {
                "uuid": params["uuid"], "name": "n", "labels": ["Entity"], "scope": "SESSION",
                "session_id": "s1", "source": "t", "content": LEAK, "neighbors": [], "episodes": [],
            }}]
        if "RETURN DISTINCT\n            node.uuid AS uuid" in query:
            return [{"uuid": "e1", "label": LEAK, "labels": ["Entity"], "scope": "SESSION"}]
        return []

    def close(self) -> None:
        return None


class _Store:
    def fetch_recall_lab_runs(self, limit: int = 100) -> list[dict[str, object]]:
        return [{"id": 1, "result": {"arms": [{"rows": [{"text": LEAK}]}]}}]

    def fetch_extraction_lab_runs(self, limit: int = 100) -> list[dict[str, object]]:
        return [{"id": 1, "request": {"text": LEAK}, "result": {"echo": LEAK}}]


def _client() -> TestClient:
    app = create_app(settings=MemorySettings(), repo=_Repo(), recall_lab_store=_Store(), extraction_lab_store=_Store())
    return TestClient(app)


def _clean(text: str) -> None:
    assert AWS not in text
    assert GH not in text
    assert "[REDACTED:" in text


ROUTES = [
    "/explorer/partials/queue",
    "/explorer/partials/node/n1",
    "/explorer/api/graph/n1",
    "/explorer/api/recall-lab/history",
    "/explorer/api/extraction-lab/history",
]


@pytest.mark.unit
def test_json_response_helper_scrubs_nested_content() -> None:
    body = JSONResponse({"a": [{"t": LEAK}], "n": 3}).body.decode()
    _clean(body)


@pytest.mark.unit
def test_template_helper_scrubs_context() -> None:
    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    resp = TEMPLATES.TemplateResponse(request, "_queue.html", {"queued_episodes": [{"uuid": "q", "name": LEAK, "preview": LEAK, "session_id": "s", "source": "t", "processing_state": "PENDING", "processing_error": None}]})
    _clean(resp.body.decode())


@pytest.mark.unit
@pytest.mark.parametrize("path", ROUTES)
def test_routes_scrub(path: str) -> None:
    with _client() as client:
        response = client.get(path)
    assert response.status_code == 200
    _clean(response.text)


@pytest.mark.unit
@pytest.mark.parametrize("path", ROUTES)
def test_policy_off_keeps_text(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_SECRET_POLICY", "off")
    with _client() as client:
        response = client.get(path)
    assert response.status_code == 200
    assert AWS in response.text


@pytest.mark.unit
@pytest.mark.parametrize("path", ROUTES)
def test_detector_failure_fails_closed(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_text: str):
        raise RuntimeError("detector down")

    monkeypatch.setattr("menhir.domain.payload_scrub.redact", boom)
    with _client() as client:
        response = client.get(path)
    assert response.status_code == 500
    assert AWS not in response.text
    assert GH not in response.text
    assert "aws " not in response.text
