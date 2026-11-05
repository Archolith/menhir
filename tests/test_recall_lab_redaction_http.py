"""Recall Lab redaction through the real HTTP handlers (CF-269 second pass)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from menhir.config import MemorySettings
from menhir.domain.recall import (
    RecallResult,
    ScalarAuthorityContributor,
    ScalarAuthorityVerdict,
    ScoredMemory,
)
from menhir.explorer.recall_lab import redact_recall_lab_payload
from tests.test_recall_lab import (
    _body,
    _create_test_app,
    _result,
    _StubExplorerRepo,
    _StubJudgeBackend,
    _StubJudgeLLM,
    _StubRecallLabStore,
    _StubRecallService,
)

QUERY = "find the launch decision"
HIDE = {"menhir_reveal": "0"}


def _rich_result(query: str, *, scope: str = "PERSISTENT", uuid: str | None = None) -> RecallResult:
    base = _result(query, content_lane=False)
    memory = base.results[0]
    contributor = ScalarAuthorityContributor(
        assertion_id="assert-1", relation="bike_spend", operation="absolute", value=40,
        stated_span="I spent $40 on lights", valid_at="2026-03-15", evidence_tier="agent",
        episode_uuid="ep-1", source_sentence="Yesterday I spent $40 on lights.",
    )
    verdict = ScalarAuthorityVerdict(
        kind="current", status="leads", subject_uuid="subj-1", attribute="medication_dose",
        scope=scope, value_kind="measurement", unit="mg", value=40, valid_at="2026-03-15",
        view_uuid="view-1", has_foundation=True, contributors=(contributor,), contributors_total=1,
    )
    mem = ScoredMemory(**{**memory.__dict__, "uuid": uuid or memory.uuid})
    return RecallResult(
        query=base.query, preset=base.preset, results=[mem],
        candidates_evaluated=base.candidates_evaluated, nodes_touched=base.nodes_touched,
        trace=base.trace, authority_layer=(verdict,),
    )


@dataclass
class _RichRecall:
    scope: str = "PERSISTENT"
    uuid: str | None = None
    calls: list[Any] = field(default_factory=list)

    async def recall(self, query: str, **kwargs: Any) -> RecallResult:
        return _rich_result(query, scope=self.scope, uuid=self.uuid)


def _write_then_read(recall: Any, **app_kwargs: Any):
    store = _StubRecallLabStore()
    app = _create_test_app(
        settings=MemorySettings(privacy_redact=False),
        repo=_StubExplorerRepo(),
        recall_service=recall,
        recall_lab_store=store,
        **app_kwargs,
    )
    return app, store


@pytest.mark.unit
def test_history_detail_and_list_redacted_for_reader_without_reveal() -> None:
    app, _store = _write_then_read(_RichRecall())
    with TestClient(app) as client:
        written = client.post("/explorer/api/recall-lab/run", json=_body())
        assert "Secret baseline memory" in written.text  # writer with reveal on sees content
        assert "I spent $40 on lights" in written.text

        revealed = client.get("/explorer/api/recall-lab/history/1")
        assert "Secret baseline memory" in revealed.text and QUERY in revealed.text

        client.cookies.update(HIDE)
        detail = client.get("/explorer/api/recall-lab/history/1")
        listing = client.get("/explorer/api/recall-lab/history")
    for text in (detail.text, listing.text):
        assert "Secret baseline" not in text
        assert "I spent $40" not in text
        assert "Yesterday I spent" not in text
        assert QUERY not in text
        assert "medication_dose" not in text
    body = detail.json()
    assert body["result"]["query"] == "[hidden]"
    assert body["request"]["query"] == "[hidden]"
    assert listing.json()["runs"][0]["query"] == "[hidden]"
    memory = body["result"]["arms"][0]["results"][0]
    assert memory["uuid"] == "memory-baseline" and memory["final_score"] == 0.9


@pytest.mark.unit
def test_stored_row_written_before_fix_is_masked_at_read_time() -> None:
    app, store = _write_then_read(_RichRecall())
    store.runs.append({
        "id": 1, "recorded_at": "x",
        "request": {"query": QUERY},
        "result": {"query": QUERY, "arms": [{"id": "a", "results": [], "authority_layer": [{
            "kind": "current", "value": 185,
            "contributors": [{"value": 40, "source_sentence": "old raw sentence"}],
        }]}]},
    })
    with TestClient(app, cookies=HIDE) as client:
        text = client.get("/explorer/api/recall-lab/history/1").text
    assert "old raw sentence" not in text and "185" not in text and QUERY not in text


@pytest.mark.unit
def test_judge_error_input_value_not_leaked_when_redacted() -> None:
    backend = _StubJudgeBackend(response="""{
      "winner": "R1", "ranking": ["R1", "R2"],
      "scores": {"R1": {"relevance": 5, "completeness": 4, "ranking": 5, "noise_control": 4,
                        "omission_safety": 4},
                 "R2": {"relevance": 3, "completeness": 3, "ranking": 3, "noise_control": 2,
                        "omission_safety": 3}},
      "serious_omissions": {"R1": "the user takes 40mg of fluoxetine", "R2": []},
      "reason": "ok"}""")
    body = _body()
    body["judge"] = True
    body["judge_passes"] = 1
    app = _create_test_app(
        settings=MemorySettings(privacy_redact=True),
        repo=_StubExplorerRepo(),
        recall_service=_StubRecallService(),
        llm=_StubJudgeLLM(backend),
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=body)
    judgment = response.json()["judgment"]
    assert judgment["ok"] is False
    assert "fluoxetine" not in response.text
    assert judgment["error"] == "ValidationError"


@pytest.mark.unit
def test_judge_error_hides_input_even_when_revealed() -> None:
    from menhir.explorer.recall_lab import RecallLabJudgeResponse

    with pytest.raises(Exception) as info:
        RecallLabJudgeResponse.model_validate({
            "winner": "R1", "ranking": ["R1"], "scores": {}, "reason": "r",
            "serious_omissions": {"R1": "the user takes 40mg of fluoxetine"},
        })
    assert "fluoxetine" not in str(info.value)


@pytest.mark.unit
def test_non_vocabulary_scope_is_masked_but_node_scope_passes() -> None:
    app, _ = _write_then_read(_RichRecall(scope="fluoxetine_for_depression"))
    with TestClient(app, cookies=HIDE) as client:
        text = client.post("/explorer/api/recall-lab/run", json=_body()).text
    assert "fluoxetine_for_depression" not in text

    ok_app, _ = _write_then_read(_RichRecall(scope="PERSISTENT"))
    with TestClient(ok_app, cookies=HIDE) as client:
        arm = client.post("/explorer/api/recall-lab/run", json=_body()).json()["arms"][0]
    assert arm["authority_layer"][0]["scope"] == "PERSISTENT"
    assert arm["results"][0]["scope"] == "PERSISTENT"


@pytest.mark.unit
def test_synthetic_scalar_id_hashed_stably_and_distinctly() -> None:
    synthetic = "scalar-asof:default:2026-03-15:subj-1:medication_dose:fluoxetine:measurement:mg"
    other = synthetic.replace("mg", "kg")
    app, _ = _write_then_read(_RichRecall(uuid=synthetic))
    with TestClient(app, cookies=HIDE) as client:
        arms = client.post("/explorer/api/recall-lab/run", json=_body()).json()["arms"]
    ids = {arm["results"][0]["uuid"] for arm in arms}
    assert len(ids) == 1  # same raw id -> same hash across arms, so baseline matching works
    hashed = ids.pop()
    assert hashed.startswith("h~") and "medication_dose" not in hashed
    again = redact_recall_lab_payload({"arms": [{"results": [{"uuid": hashed}]}]}, reveal=False)
    assert again["arms"][0]["results"][0]["uuid"] == hashed  # idempotent
    other_hash = redact_recall_lab_payload(
        {"arms": [{"results": [{"uuid": other}]}]}, reveal=False
    )["arms"][0]["results"][0]["uuid"]
    assert other_hash != hashed


@pytest.mark.unit
def test_arm_error_is_class_name_only_when_redacted() -> None:
    @dataclass
    class _Boom:
        async def recall(self, query: str, **kwargs: Any) -> RecallResult:
            raise RuntimeError("row says user takes fluoxetine")

    app = _create_test_app(
        settings=MemorySettings(privacy_redact=True),
        repo=_StubExplorerRepo(),
        recall_service=_Boom(),
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=_body())
    assert "fluoxetine" not in response.text
    assert response.json()["arms"][0]["error"] == "RuntimeError"


@pytest.mark.unit
def test_prefix_row_with_event_authority_layer_and_unknown_keys_masked() -> None:
    app, store = _write_then_read(_RichRecall())
    store.runs.append({
        "id": 1, "recorded_at": "x", "request": {"query": QUERY},
        "result": {
            "query": QUERY, "surprise": "top secret",
            "arms": [{
                "id": "a", "label": "A", "ok": True, "elapsed_ms": 3.0, "surprise_arm": "arm secret",
                "event_authority_layer": [{
                    "kind": "history", "predicate": "took", "stated_span": "I took fluoxetine",
                    "object_display": "fluoxetine", "object_key": "fluoxetine_key",
                }],
            }],
            "judgment": {"ok": True, "winner_id": "a", "extra_reason": "judge secret",
                         "passes": [{"winner_id": "a", "extra": "pass secret", "reason": "r secret"}]},
        },
    })
    with TestClient(app, cookies=HIDE) as client:
        body = client.get("/explorer/api/recall-lab/history/1").json()
    text = str(body)
    for leak in ("fluoxetine", "top secret", "arm secret", "judge secret", "pass secret", "r secret", "took"):
        assert leak not in text
    arm = body["result"]["arms"][0]
    assert arm["id"] == "a" and arm["elapsed_ms"] == 3.0 and arm["ok"] is True
    assert body["result"]["judgment"]["winner_id"] == "a"


@pytest.mark.unit
def test_history_kind_passes_and_error_text_helpers() -> None:
    from menhir.explorer.recall_lab import _mask_value, _redact_allowlist, _redact_error_text

    assert _redact_allowlist([{"kind": "history"}], reveal=False) == [{"kind": "history"}]
    assert _redact_error_text("fluoxetine: 40mg daily") == "[hidden]"
    assert _redact_error_text("fluoxetine") == "[hidden]"
    assert _redact_error_text("RuntimeError: secret") == "RuntimeError"
    assert _redact_error_text("Recall Lab judge: extra secret text") == "[hidden]"
    assert _redact_error_text("Recall Lab judge requires an available runtime LLM").startswith("Recall")
    once = _mask_value({"R1": "x"})
    assert _mask_value(once) == once


@pytest.mark.unit
def test_redacted_run_stores_no_raw_query_but_reveal_run_does() -> None:
    for redact, expect_raw in ((True, False), (False, True)):
        store = _StubRecallLabStore()
        app = _create_test_app(
            settings=MemorySettings(privacy_redact=redact),
            repo=_StubExplorerRepo(),
            recall_service=_RichRecall(),
            recall_lab_store=store,
        )
        with TestClient(app) as client:
            client.post("/explorer/api/recall-lab/run", json=_body())
        stored = str(store.runs[0])
        assert (QUERY in str(store.runs[0]["request"])) is expect_raw
        assert (QUERY in stored) is expect_raw
        assert store.runs[0]["request"]["limit"] == 5  # non-content request keys survive


@pytest.mark.unit
def test_reveal_mode_payload_is_untouched() -> None:
    payload = {"query": QUERY, "arms": [{"error": "RuntimeError: detail", "results": [{"name": "n"}]}]}
    assert redact_recall_lab_payload(payload, reveal=True) is payload
