"""Unit tests for the Explorer's concurrent Recall Lab."""

from __future__ import annotations

import asyncio
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
    TemporalFact,
)
from menhir.domain.retrieval_trace_models import (
    CandidateTrace,
    RelevanceBreakdown,
    RetrievalTrace,
)
from menhir.domain.retrieval_tuning import CandidateSource, RetrievalScoreKind
from menhir.explorer import create_app
from menhir.explorer.recall_lab import DEFAULT_ARMS
from menhir.infrastructure.telemetry.store import McpTelemetryStore


@dataclass
class _StubExplorerRepo:
    def execute(
        self,
        query: str,
        params: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        return []

    def close(self) -> None:
        return None


@dataclass
class _StubRecallLabStore:
    runs: list[dict[str, Any]] = field(default_factory=list)

    def record_recall_lab_run(
        self,
        *,
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> int:
        run_id = len(self.runs) + 1
        self.runs.append({
            "id": run_id,
            "recorded_at": "2026-07-14T12:00:00+00:00",
            "request": request_payload,
            "result": result_payload,
        })
        return run_id

    def fetch_recall_lab_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = []
        for run in reversed(self.runs[-limit:]):
            result = run["result"]
            judgment = result.get("judgment") or {}
            rows.append({
                "id": run["id"],
                "recorded_at": run["recorded_at"],
                "query": result["query"],
                "preset": result["preset"],
                "namespace": result.get("namespace"),
                "judge_enabled": bool(judgment),
                "judge_ok": judgment.get("ok"),
                "judge_model": judgment.get("model"),
                "winner_id": judgment.get("winner_id"),
                "tied_ids": judgment.get("tied_ids") or [],
                "arms": [
                    {"id": arm["id"], "label": arm["label"], "hits": len(arm.get("results") or [])}
                    for arm in result["arms"]
                ],
            })
        return rows

    def fetch_recall_lab_run(self, run_id: int) -> dict[str, Any] | None:
        return next((run for run in self.runs if run["id"] == run_id), None)


def _create_test_app(**kwargs: Any):
    kwargs.setdefault("recall_lab_store", _StubRecallLabStore())
    return create_app(**kwargs)


def _result(query: str, *, content_lane: bool) -> RecallResult:
    suffix = "content" if content_lane else "baseline"
    breakdown = RelevanceBreakdown(
        semantic_similarity=0.8,
        adjacency_bonus=0.0,
        recency_bonus=0.1,
        prominence_bonus=0.0,
        conflict_bonus=0.0,
        type_boost=0.0,
        preset="knowledge",
        alpha=0.2,
        beta=0.1,
        gamma=0.1,
        delta=0.0,
    )
    memory = ScoredMemory(
        uuid=f"memory-{suffix}",
        name=f"Secret {suffix} memory",
        content=f"Secret {suffix} content",
        scope="PERSISTENT",
        memory_type="Entity",
        final_score=0.9,
        breakdown=breakdown,
        retrieval_score=0.7,
        retrieval_score_kind=RetrievalScoreKind.WEIGHTED_RRF_NORMALIZED,
    )
    source = CandidateSource.CONTENT_VECTOR if content_lane else CandidateSource.VECTOR
    candidate = CandidateTrace(
        uuid=memory.uuid,
        source=source,
        similarity=0.8,
        survived_floor=True,
        score_parts=breakdown,
        final_score=0.9,
        rank=1,
        content_rank=1 if content_lane else None,
        content_cosine=0.88 if content_lane else None,
        contributing_sources=frozenset({source}),
    )
    return RecallResult(
        query=query,
        preset="knowledge",
        results=[memory],
        candidates_evaluated=7,
        nodes_touched=1,
        trace=RetrievalTrace(
            query=query,
            preset="knowledge",
            total_ms=12,
            phases={"candidate_generation": 8},
            candidates=[candidate],
        ),
    )


@dataclass
class _StubRecallService:
    fail_content_lane: bool = False
    degrade_content_lane: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    active: int = 0
    max_active: int = 0

    async def recall(self, query: str, **kwargs: Any) -> RecallResult:
        self.calls.append({"query": query, **kwargs})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            tuning = kwargs["tuning"]
            if self.fail_content_lane and tuning.enable_content_vector:
                raise RuntimeError("content index unavailable")
            result = _result(query, content_lane=tuning.enable_content_vector)
            if self.degrade_content_lane and tuning.enable_content_vector:
                return RecallResult(
                    query=result.query,
                    preset=result.preset,
                    results=[],
                    candidates_evaluated=0,
                    nodes_touched=0,
                    note="Recall search backend failed; this is not a confirmed zero-match result.",
                    search_error="graphiti_search_unavailable:AttributeError",
                    trace=result.trace,
                )
            return result
        finally:
            self.active -= 1


@dataclass
class _StubJudgeBackend:
    response: str = """{
      "winner": "R1",
      "ranking": ["R1", "R2"],
      "scores": {
        "R1": {"relevance": 5, "completeness": 4, "ranking": 5, "noise_control": 4, "omission_safety": 4},
        "R2": {"relevance": 3, "completeness": 3, "ranking": 3, "noise_control": 2, "omission_safety": 3}
      },
      "serious_omissions": {"R1": [], "R2": ["missing deployment detail"]},
      "reason": "R1 places the more directly useful memory first."
    }"""
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create_chat_completion(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.response


@dataclass
class _StubJudgeLLM:
    backend: _StubJudgeBackend
    chat_model: str = "independent-judge-model"


def _body() -> dict[str, Any]:
    return {
        "query": "find the launch decision",
        "preset": "knowledge",
        "limit": 5,
        "candidate_k": 20,
        "arms": [
            {
                "id": "a",
                "label": "A",
                "tuning": {"enable_bm25": True},
            },
            {
                "id": "b",
                "label": "B",
                "tuning": {
                    "enable_bm25": True,
                    "enable_content_vector": True,
                    "content_vector_weight": 0.7,
                },
            },
        ],
    }


@pytest.mark.unit
def test_recall_lab_frontier_preset_uses_active_facet() -> None:
    arm = next(item for item in DEFAULT_ARMS if item["id"] == "d")
    tuning = arm["tuning"]

    assert "active facet" in arm["label"]
    assert tuning["enable_facet_candidates"] is True
    assert tuning["enable_facet_shadow"] is False
    assert tuning["enable_oracle_ranking"] is True
    assert tuning["enable_intent_lens"] is True
    assert tuning["enable_warden_gate"] is True
    assert tuning["enable_evidence_anchor"] is True


@pytest.mark.unit
def test_recall_lab_working_frontier_preserves_d_and_softens_evidence_gate() -> None:
    d_arm = next(item for item in DEFAULT_ARMS if item["id"] == "d")
    e_arm = next(item for item in DEFAULT_ARMS if item["id"] == "e")

    expected = dict(d_arm["tuning"])
    expected["enable_evidence_anchor"] = False

    assert "production base + frontier" in e_arm["label"]
    assert e_arm["tuning"] == expected
    assert d_arm["tuning"]["enable_evidence_anchor"] is True


@pytest.mark.unit
def test_recall_lab_isolates_facet_and_oracle_ranking_arms() -> None:
    f_arm = next(item for item in DEFAULT_ARMS if item["id"] == "f")
    g_arm = next(item for item in DEFAULT_ARMS if item["id"] == "g")

    assert "facet ranking only" in f_arm["label"]
    assert f_arm["tuning"]["enable_facet_candidates"] is True
    assert f_arm["tuning"]["facet_weight"] == 0.5
    assert f_arm["tuning"]["enable_oracle_ranking"] is False
    assert f_arm["tuning"]["enable_intent_lens"] is False
    assert f_arm["tuning"]["enable_warden_gate"] is False
    assert f_arm["tuning"]["enable_assertion_shadow"] is False

    assert "oracle ranking only" in g_arm["label"]
    assert g_arm["tuning"]["enable_facet_candidates"] is False
    assert g_arm["tuning"]["enable_oracle_ranking"] is True
    assert g_arm["tuning"]["oracle_rank_weight"] == 0.25
    assert g_arm["tuning"]["enable_intent_lens"] is True
    assert g_arm["tuning"]["enable_warden_gate"] is False
    assert g_arm["tuning"]["enable_assertion_shadow"] is False


@pytest.mark.unit
def test_recall_lab_isolates_warden_from_working_frontier() -> None:
    e_arm = next(item for item in DEFAULT_ARMS if item["id"] == "e")
    h_arm = next(item for item in DEFAULT_ARMS if item["id"] == "h")

    expected = dict(e_arm["tuning"])
    expected["enable_warden_gate"] = False

    assert "without warden" in h_arm["label"]
    assert h_arm["tuning"] == expected
    assert e_arm["tuning"]["enable_warden_gate"] is True


@pytest.mark.unit
def test_recall_lab_page_renders_default_arms() -> None:
    app = _create_test_app(settings=MemorySettings(), repo=_StubExplorerRepo())
    with TestClient(app) as client:
        response = client.get("/explorer/recall-lab")
    assert response.status_code == 200
    assert "Recall Lab" in response.text
    assert "Production" in response.text
    assert "add content vector" in response.text
    assert "Pure read" in response.text
    assert "Blinded LLM judge" in response.text
    assert "one per line" in response.text
    assert "active facet + warden + oracle" in response.text
    assert "production base + frontier" in response.text
    assert "production base + facet ranking only" in response.text
    assert "production base + oracle ranking only" in response.text
    assert "E frontier without warden" in response.text


@pytest.mark.unit
def test_recall_lab_accepts_all_nine_default_arms() -> None:
    recall = _StubRecallService()
    app = _create_test_app(
        settings=MemorySettings(),
        repo=_StubExplorerRepo(),
        recall_service=recall,
    )
    body = _body()
    body["arms"] = DEFAULT_ARMS

    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=body)

    assert response.status_code == 200
    assert [arm["id"] for arm in response.json()["arms"]] == [
        "production", "a", "b", "c", "d", "e", "f", "g", "h",
    ]
    assert recall.max_active == 9


@pytest.mark.unit
def test_recall_lab_judge_prompt_disambiguates_ranking_quality_score() -> None:
    from menhir.explorer.recall_lab import _JUDGE_SYSTEM_PROMPT

    assert '"ranking" is a 0-5 quality score' in _JUDGE_SYSTEM_PROMPT
    assert "put 6, 7, or 8" in _JUDGE_SYSTEM_PROMPT


@pytest.mark.unit
def test_recall_lab_runs_arms_concurrently_and_read_only() -> None:
    recall = _StubRecallService()
    app = _create_test_app(
        settings=MemorySettings(),
        repo=_StubExplorerRepo(),
        recall_service=recall,
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=_body())

    assert response.status_code == 200
    payload = response.json()
    assert [arm["id"] for arm in payload["arms"]] == ["a", "b"]
    assert recall.max_active == 2
    assert all(call["trace"] is True for call in recall.calls)
    assert all(call["update_access"] is False for call in recall.calls)
    assert recall.calls[0]["tuning"].enable_bm25 is True
    assert recall.calls[1]["tuning"].enable_content_vector is True
    assert recall.calls[1]["tuning"].content_vector_weight == 0.7
    assert payload["arms"][1]["results"][0]["source"] == "content_vector"
    assert payload["arms"][1]["results"][0]["content_rank"] == 1


@pytest.mark.unit
def test_recall_lab_isolates_arm_failure() -> None:
    recall = _StubRecallService(fail_content_lane=True)
    app = _create_test_app(
        settings=MemorySettings(),
        repo=_StubExplorerRepo(),
        recall_service=recall,
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=_body())

    assert response.status_code == 200
    arms = response.json()["arms"]
    assert arms[0]["ok"] is True
    assert arms[0]["results"][0]["uuid"] == "memory-baseline"
    assert arms[1]["ok"] is False
    assert "content index unavailable" in arms[1]["error"]


@pytest.mark.unit
def test_recall_lab_marks_degraded_search_instead_of_normal_empty_result() -> None:
    recall = _StubRecallService(degrade_content_lane=True)
    app = _create_test_app(
        settings=MemorySettings(),
        repo=_StubExplorerRepo(),
        recall_service=recall,
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=_body())

    degraded = response.json()["arms"][1]
    assert degraded["ok"] is True
    assert degraded["degraded"] is True
    assert degraded["search_error"] == "graphiti_search_unavailable:AttributeError"
    assert "not a confirmed zero-match" in degraded["note"]


@pytest.mark.unit
def test_recall_lab_does_not_judge_degraded_arms(caplog) -> None:
    recall = _StubRecallService(degrade_content_lane=True)
    backend = _StubJudgeBackend()
    body = _body()
    body["judge"] = True
    app = _create_test_app(
        settings=MemorySettings(),
        repo=_StubExplorerRepo(),
        recall_service=recall,
        llm=_StubJudgeLLM(backend),
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=body)

    judgment = response.json()["judgment"]
    assert judgment["ok"] is False
    assert judgment["skipped"] is True
    assert "fewer than two healthy" in judgment["error"]
    assert backend.calls == []
    assert "Recall Lab judge skipped" in caplog.text


@pytest.mark.unit
def test_recall_lab_blinds_and_aggregates_llm_judging_passes(monkeypatch) -> None:
    monkeypatch.setattr(
        "menhir.explorer.recall_lab.random.SystemRandom.shuffle",
        lambda _self, _items: None,
    )
    recall = _StubRecallService()
    backend = _StubJudgeBackend()
    body = _body()
    body["judge"] = True
    body["judge_passes"] = 2
    body["arms"][0]["label"] = "PRIVATE PRODUCTION LABEL"
    body["arms"][1]["label"] = "PRIVATE CONTENT LABEL"
    app = _create_test_app(
        settings=MemorySettings(),
        repo=_StubExplorerRepo(),
        recall_service=recall,
        llm=_StubJudgeLLM(backend),
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=body)

    assert response.status_code == 200
    payload = response.json()
    judgment = payload["judgment"]
    assert judgment["ok"] is True
    assert judgment["passes_completed"] == 2
    assert judgment["model"] == "independent-judge-model"
    assert judgment["winner_id"] in {"a", "b"}
    assert judgment["average_scores"][judgment["winner_id"]]["relevance"] == 5.0
    assert len(backend.calls) == 2
    assert all(call["temperature"] == 0.0 for call in backend.calls)
    assert all("PRIVATE PRODUCTION LABEL" not in call["user_prompt"] for call in backend.calls)
    assert all("PRIVATE CONTENT LABEL" not in call["user_prompt"] for call in backend.calls)
    assert all("enable_content_vector" not in call["user_prompt"] for call in backend.calls)
    assert all("R1:" in call["user_prompt"] and "R2:" in call["user_prompt"] for call in backend.calls)
    assert all("_judge_results" not in arm for arm in payload["arms"])


@pytest.mark.unit
def test_recall_lab_keeps_results_when_llm_judge_output_is_invalid() -> None:
    recall = _StubRecallService()
    backend = _StubJudgeBackend(response="not json")
    body = _body()
    body["judge"] = True
    body["judge_passes"] = 1
    app = _create_test_app(
        settings=MemorySettings(),
        repo=_StubExplorerRepo(),
        recall_service=recall,
        llm=_StubJudgeLLM(backend),
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=body)

    payload = response.json()
    assert response.status_code == 200
    assert payload["judgment"]["ok"] is False
    assert "JSON object" in payload["judgment"]["error"]
    assert payload["arms"][0]["results"]


@pytest.mark.unit
def test_recall_lab_saves_and_reads_run_history() -> None:
    recall = _StubRecallService()
    store = _StubRecallLabStore()
    app = _create_test_app(
        settings=MemorySettings(),
        repo=_StubExplorerRepo(),
        recall_service=recall,
        recall_lab_store=store,
    )
    with TestClient(app) as client:
        run = client.post("/explorer/api/recall-lab/run", json=_body())
        history = client.get("/explorer/api/recall-lab/history")
        detail = client.get("/explorer/api/recall-lab/history/1")

    assert run.status_code == 200
    assert run.json()["saved"] is True
    assert run.json()["saved_run_id"] == 1
    assert history.json()["runs"][0]["query"] == "find the launch decision"
    assert detail.json()["result"]["arms"][0]["results"]


@pytest.mark.unit
def test_recall_lab_sqlite_history_round_trip(tmp_path) -> None:
    store = McpTelemetryStore(db_path=tmp_path / "telemetry.db")
    request = {"query": "saved query", "preset": "knowledge", "judge": True}
    result = {
        "query": "saved query",
        "preset": "knowledge",
        "namespace": None,
        "arms": [{"id": "production", "label": "Production", "ok": True, "results": []}],
        "judgment": {
            "ok": True,
            "model": "judge-model",
            "winner_id": "production",
            "tied_ids": [],
        },
    }

    run_id = store.record_recall_lab_run(request_payload=request, result_payload=result)

    assert run_id == 1
    assert store.fetch_recall_lab_runs()[0]["winner_id"] == "production"
    saved = store.fetch_recall_lab_run(run_id)
    assert saved is not None
    assert saved["request"] == request
    assert saved["result"] == result


@pytest.mark.unit
def test_recall_lab_redacts_json_memory_content() -> None:
    recall = _StubRecallService()
    app = _create_test_app(
        settings=MemorySettings(privacy_redact=True),
        repo=_StubExplorerRepo(),
        recall_service=recall,
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=_body())

    memory = response.json()["arms"][0]["results"][0]
    assert memory["uuid"] == "memory-baseline"
    assert memory["name"] == "[hidden]"
    assert memory["content"] == "[hidden]"


def _result_with_authority(query: str) -> RecallResult:
    result = _result(query, content_lane=False)
    contributor = ScalarAuthorityContributor(
        assertion_id="assert-1",
        relation="bike_spend",
        operation="add",
        value=185,
        stated_span="I spent another $40 on bike lights",
        valid_at="2026-03-15",
        evidence_tier="direct",
        episode_uuid="ep-1",
    )
    verdict = ScalarAuthorityVerdict(
        kind="current",
        status="leads",
        subject_uuid="memory-baseline",
        attribute="bike_spend",
        scope="finance",
        value_kind="currency",
        unit="usd",
        value=185,
        valid_at="2026-03-15",
        view_uuid="memory-baseline",
        has_foundation=True,
        contributors=(contributor,),
        contributors_total=1,
    )
    return RecallResult(
        query=result.query,
        preset=result.preset,
        results=[
            ScoredMemory(
                **{**result.results[0].__dict__, "is_scalar_authority": True},
            )
        ],
        candidates_evaluated=result.candidates_evaluated,
        nodes_touched=result.nodes_touched,
        trace=result.trace,
        authority_layer=(verdict,),
    )


@dataclass
class _StubAuthorityRecallService:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def recall(self, query: str, **kwargs: Any) -> RecallResult:
        self.calls.append({"query": query, **kwargs})
        return _result_with_authority(query)


@pytest.mark.unit
def test_recall_lab_surfaces_authority_layer() -> None:
    """Arm results must carry authority_layer + per-memory scalar-authority flags so a caller
    (e.g. a benchmark harness) can reconstruct the same [AUTHORITATIVE CURRENT MEMORY] labeling
    the production /api/recall route provides -- Recall Lab calls recall_service.recall()
    directly and previously dropped these three fields entirely."""
    recall = _StubAuthorityRecallService()
    app = _create_test_app(
        settings=MemorySettings(privacy_redact=False),
        repo=_StubExplorerRepo(),
        recall_service=recall,
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=_body())

    arm = response.json()["arms"][0]
    memory = arm["results"][0]
    assert memory["is_scalar_authority"] is True
    assert memory["is_superseded_view"] is False

    verdicts = arm["authority_layer"]
    assert verdicts and len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict["kind"] == "current"
    assert verdict["status"] == "leads"
    assert verdict["subject_uuid"] == "memory-baseline"
    assert verdict["value"] == 185
    assert verdict["contributors"][0]["stated_span"] == "I spent another $40 on bike lights"
    assert arm["event_authority_layer"] is None


@pytest.mark.unit
def test_recall_lab_redacts_authority_layer_free_text() -> None:
    """Privacy redaction must reach the authority layer too: value and each contributor's
    stated_span are memory content, not structural metadata, and were previously exempt from
    redact_mapping's flat field-name match because that vocabulary predates this shape."""
    recall = _StubAuthorityRecallService()
    app = _create_test_app(
        settings=MemorySettings(privacy_redact=True),
        repo=_StubExplorerRepo(),
        recall_service=recall,
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=_body())

    verdict = response.json()["arms"][0]["authority_layer"][0]
    assert verdict["value"] == "[hidden]"
    assert verdict["contributors"][0]["stated_span"] == "[hidden]"
    # Structural fields survive redaction -- a caller still needs these to interpret the verdict.
    assert verdict["kind"] == "current"
    assert verdict["subject_uuid"] == "memory-baseline"
    assert verdict["has_foundation"] is True


def _result_with_temporal_facts(query: str) -> RecallResult:
    result = _result(query, content_lane=False)
    memory = result.results[0]
    facts = (
        TemporalFact(
            fact="user goes to the gym on Tuesdays, Thursdays, and Saturdays",
            valid_at="2023-06-01T00:10:00Z",
            invalid_at="2023-08-15T10:57:00Z",
            created_at="2023-06-01T00:10:00Z",
            expired_at="2023-08-15T10:57:00Z",
            is_current_belief=False,
            temporal_role="superseded_belief",
        ),
    )
    return RecallResult(
        query=result.query,
        preset=result.preset,
        results=[ScoredMemory(**{**memory.__dict__, "temporal_facts": facts})],
        candidates_evaluated=result.candidates_evaluated,
        nodes_touched=result.nodes_touched,
        trace=result.trace,
    )


@dataclass
class _StubTemporalFactsRecallService:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def recall(self, query: str, **kwargs: Any) -> RecallResult:
        self.calls.append({"query": query, **kwargs})
        return _result_with_temporal_facts(query)


@pytest.mark.unit
def test_recall_lab_surfaces_temporal_facts() -> None:
    """Each memory's temporal_facts (world-time/belief-time/supersession evidence) must reach
    the caller -- /api/recall (routes.py) includes it, but Recall Lab's _serialize_result
    previously omitted it entirely, which silently dropped the "previously vs now" /
    supersession context a benchmark's answer prompt (or any Recall Lab caller) depends on,
    for every arm regardless of tuning."""
    recall = _StubTemporalFactsRecallService()
    app = _create_test_app(
        settings=MemorySettings(privacy_redact=False),
        repo=_StubExplorerRepo(),
        recall_service=recall,
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=_body())

    memory = response.json()["arms"][0]["results"][0]
    facts = memory["temporal_facts"]
    assert len(facts) == 1
    assert facts[0]["fact"] == "user goes to the gym on Tuesdays, Thursdays, and Saturdays"
    assert facts[0]["valid_at"] == "2023-06-01T00:10:00Z"
    assert facts[0]["invalid_at"] == "2023-08-15T10:57:00Z"
    assert facts[0]["temporal_role"] == "superseded_belief"
    assert facts[0]["is_current_belief"] is False


@pytest.mark.unit
def test_recall_lab_redacts_temporal_facts_free_text() -> None:
    """The fact string is memory content and must be masked under privacy redaction; the
    dates/role are structural and must survive so a caller can still place the fact in time."""
    recall = _StubTemporalFactsRecallService()
    app = _create_test_app(
        settings=MemorySettings(privacy_redact=True),
        repo=_StubExplorerRepo(),
        recall_service=recall,
    )
    with TestClient(app) as client:
        response = client.post("/explorer/api/recall-lab/run", json=_body())

    fact = response.json()["arms"][0]["results"][0]["temporal_facts"][0]
    assert fact["fact"] == "[hidden]"
    assert fact["valid_at"] == "2023-06-01T00:10:00Z"
    assert fact["temporal_role"] == "superseded_belief"


@pytest.mark.unit
def test_recall_lab_validates_bounds_and_requires_runtime() -> None:
    app = _create_test_app(settings=MemorySettings(), repo=_StubExplorerRepo())
    with TestClient(app) as client:
        unavailable = client.post("/explorer/api/recall-lab/run", json=_body())
        invalid_body = _body()
        invalid_body["candidate_k"] = 2
        invalid = client.post("/explorer/api/recall-lab/run", json=invalid_body)

    assert unavailable.status_code == 503
    assert invalid.status_code == 422
