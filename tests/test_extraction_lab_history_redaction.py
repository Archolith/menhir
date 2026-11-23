"""Extraction Lab history is re-redacted at read time (#82); judge-skip log line is clean (#103)."""

from __future__ import annotations

import copy
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from menhir.config import MemorySettings
from menhir.explorer import create_app
from menhir.explorer import recall_lab
from menhir.explorer.recall_lab import RecallLabRequest, run_recall_lab
from menhir.privacy import MASK
from tests.test_recall_lab import _body, _StubExplorerRepo

SECRETS = [
    "launch codename Falcon",
    "earlier turn about Dr. Okafor",
    "gold mention Okafor Clinic",
    "gold proposition takes 40mg",
    "extracted mention Okafor",
    "extracted fact Okafor prescribes aspirin",
    "known entity Falcon",
    "private note in unknown key",
    "connection to db.internal.example failed",
]
HIDE = {"menhir_reveal": "0"}


def _detail_row() -> dict[str, Any]:
    return {
        "id": 7,
        "recorded_at": "2026-07-14T12:00:00+00:00",
        "request": {
            "current_message": SECRETS[0],
            "previous_episodes": [{"text": SECRETS[1], "created_at": "2026-07-13T12:00:00+00:00"}],
            "gold": {"mentions": [SECRETS[2]], "propositions": [SECRETS[3]], "update_language": []},
            "arms": [{"id": "a", "label": "Baseline", "enabled": True, "tuning": {"prompt_variant": "baseline"}}],
            "source_namespace": "lme-abc",
        },
        "result": {
            "current_message": SECRETS[0],
            "arms": [
                {
                    "id": "a",
                    "label": "Baseline",
                    "ok": False,
                    "elapsed_ms": 12.5,
                    "tuning": {"prompt_variant": "baseline", "model": "gpt-x", "temperature": 0.0},
                    "error": f"RuntimeError: {SECRETS[8]}",
                    "extraction": {
                        "mentions": [{"text": SECRETS[4], "labels": ["Person"]}],
                        "propositions": [{"fact": SECRETS[5], "source": SECRETS[4]}],
                    },
                    "gold_scores": {"mention_recall": 0.5},
                    "known_entities_used": [SECRETS[6]],
                    "unknown_key": SECRETS[7],
                }
            ],
        },
    }


def _list_row() -> dict[str, Any]:
    return {
        "id": 7,
        "recorded_at": "2026-07-14T12:00:00+00:00",
        "current_message": SECRETS[0],
        "arms": [
            {
                "id": "a",
                "label": "Baseline",
                "ok": False,
                "tuning": {"prompt_variant": "baseline"},
                "gold_scores": {"mention_recall": 0.5},
                "error": f"RuntimeError: {SECRETS[8]}",
                "unknown_key": SECRETS[7],
            }
        ],
    }


class _Store:
    def fetch_extraction_lab_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [copy.deepcopy(_list_row())]

    def fetch_extraction_lab_run(self, run_id: int) -> dict[str, Any] | None:
        return copy.deepcopy(_detail_row()) if run_id == 7 else None


def _client(*, privacy_redact: bool) -> TestClient:
    app = create_app(
        settings=MemorySettings(privacy_redact=privacy_redact),
        repo=_StubExplorerRepo(),
        extraction_lab_store=_Store(),
    )
    return TestClient(app)


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, secret
    assert MASK in text


@pytest.mark.unit
def test_hidden_masks_list_and_detail() -> None:
    with _client(privacy_redact=True) as client:
        listing = client.get("/explorer/api/extraction-lab/history")
        detail = client.get("/explorer/api/extraction-lab/history/7")
    _assert_clean(listing.text)
    _assert_clean(detail.text)

    run = listing.json()["runs"][0]
    assert run["id"] == 7 and run["recorded_at"] == "2026-07-14T12:00:00+00:00"
    arm = run["arms"][0]
    assert arm["id"] == "a" and arm["label"] == "Baseline" and arm["ok"] is False
    assert arm["gold_scores"] == {"mention_recall": 0.5}
    assert arm["error"] == "RuntimeError"
    assert arm["unknown_key"] == MASK
    assert run["current_message"] == MASK

    body = detail.json()
    assert body["id"] == 7 and body["recorded_at"] == "2026-07-14T12:00:00+00:00"
    darm = body["result"]["arms"][0]
    assert darm["id"] == "a" and darm["label"] == "Baseline" and darm["ok"] is False
    assert darm["tuning"] == {"prompt_variant": "baseline", "model": "gpt-x", "temperature": 0.0}
    assert darm["error"] == "RuntimeError"
    assert darm["elapsed_ms"] == 12.5
    assert darm["unknown_key"] == MASK
    assert darm["known_entities_used"] == [MASK]
    assert body["request"]["previous_episodes"][0]["created_at"] == "2026-07-13T12:00:00+00:00"
    assert body["request"]["gold"]["update_language"] == []


@pytest.mark.unit
def test_reveal_cookie_on_loopback_and_privacy_off_return_unchanged() -> None:
    with _client(privacy_redact=True) as client:
        client.cookies.update({"menhir_reveal": "1"})
        assert client.get("/explorer/api/extraction-lab/history/7").json() == _detail_row()
        assert client.get("/explorer/api/extraction-lab/history").json() == {"runs": [_list_row()]}
    with _client(privacy_redact=False) as client:
        assert client.get("/explorer/api/extraction-lab/history/7").json() == _detail_row()
        client.cookies.update(HIDE)
        _assert_clean(client.get("/explorer/api/extraction-lab/history/7").text)


@pytest.mark.unit
def test_missing_run_is_404() -> None:
    with _client(privacy_redact=True) as client:
        assert client.get("/explorer/api/extraction-lab/history/99").status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_judge_skipped_log_has_no_query_or_raw_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    query = "find the launch decision"
    raw = "ConnectionError: dial secret-host.internal for launch decision"

    async def failing_arm(service: object, request: object, arm: object, *, reveal: bool) -> dict[str, Any]:
        return {"id": getattr(arm, "id", "x"), "ok": False, "error": raw}

    monkeypatch.setattr(recall_lab, "_run_arm", failing_arm)
    request = RecallLabRequest(**{**_body(), "judge": True})
    with caplog.at_level(logging.WARNING, logger=recall_lab.logger.name):
        out = await run_recall_lab(object(), request, reveal=False)
    assert out["judgment"]["skipped"] is True
    text = caplog.text
    assert "judge skipped" in text
    assert query not in text and "launch decision" not in text
    assert "secret-host" not in text
    assert "ConnectionError" in text
    assert f"query_chars={len(query)}" in text
