"""4R match: the span-preserving prompt sentence, the gate-dropped tally, and its API passthrough.

Fakes are copied (not imported) from tests/test_count_match_runner.py and tests/test_api_routes.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.services.counting_consolidation import (
    COUNTING_MATCH_PROMPT_ADDENDUM,
    CountingConsolidationConfig,
    counting_system_prompt,
    run_counting_consolidation,
)

# ------------------------------------------------------------------ prompt


@pytest.mark.unit
@pytest.mark.parametrize("extract_v2", [False, True])
def test_match_prompt_tells_model_not_to_widen_stated_span(extract_v2):
    # Fails if the sentence is removed from the addendum (a list span then goes unchallenged).
    prompt = counting_system_prompt(extract_v2, match=True)
    assert "never widen" in prompt
    assert "stated_span" in prompt
    assert "one observation per thing" in prompt
    assert "quoting only that thing's own words" in prompt


@pytest.mark.unit
@pytest.mark.parametrize("extract_v2", [False, True])
def test_match_off_prompt_has_neither_addendum_nor_span_sentence(extract_v2):
    # Fails if the sentence leaks into the match-off prompt.
    prompt = counting_system_prompt(extract_v2, match=False)
    assert COUNTING_MATCH_PROMPT_ADDENDUM not in prompt
    assert "never widen" not in prompt
    assert "one observation per thing" not in prompt


# ------------------------------------------------------------------ runner


@dataclass
class _Ep:
    uuid: str
    content: str


def _build_episodes(rows):
    return [_Ep(uuid=str(r["uuid"]), content=str(r["content"])) for r in rows]


def _row(i):
    return {"uuid": f"t{i}", "valid_at": f"2026-07-0{i}T00:00:00Z",
            "cursor_at": f"2026-07-0{i}T00:00:00Z", "content": f"I finished kit {i}"}


def _occ(aid, day):
    return dict(
        assertion_id=aid, subject_uuid="s1", value_kind="count", attribute="model_kit_count",
        count_object="model kit", count_item="", valid_at=f"2026-07-{day:02d}T00:00:00+00:00",
        episode_uuid=f"ep-{aid}", count_status="",
    )


class _Svc:
    def rebuild_scalar_state(self, subject, *, namespace=None, **kw):
        pass

    def rebuild_scalar_projections(self, subject, *, namespace=None, **kw):
        pass


class _Graph:
    def __init__(self, pages):
        self.pages = list(pages)
        self.occ_by_id = {"a1": _occ("a1", 1), "a2": _occ("a2", 2)}

    def scalar_state_service(self, **kw):
        return _Svc()

    def list_counting_dirty_namespaces(self, **kw):
        return []

    def load_next_counting_batch(self, namespace, **kw):
        return self.pages.pop(0) if self.pages else []

    def advance_counting_cursor(self, namespace, **kw):
        pass

    def claim_counting_namespace(self, namespace, **kw):
        return {"incarnation": "inc-1", "epoch": 7}

    def renew_counting_claim(self, namespace, **kw):
        return True

    def release_counting_claim(self, namespace, **kw):
        pass

    def prior_version_occurrences(self, namespace, turn_ids, **kw):
        return []

    def mark_projection_complete(self, assertion_ids):
        return len(assertion_ids)

    def retire_prior_version_occurrences(self, namespace, turn_ids, **kw):
        return []

    def mark_counting_flag_state(self, namespace, **kw):
        pass

    def scalar_claims_for_turns(self, namespace, turn_ids, **kw):
        return []

    def count_occurrences_by_ids(self, namespace, ids, **kw):
        return [self.occ_by_id[i] for i in ids if i in self.occ_by_id]

    def counted_occurrences_for_subjects(self, namespace, subjects, **kw):
        return []

    def undecided_count_occurrences(self, namespace, **kw):
        return []

    def record_count_decisions(self, namespace, decisions, **kw):
        return {"fence_ok": True}


class _Perception:
    """Returns one (decisions, committed) pair per perceive call, in order."""

    pairs: list[tuple[int, int]] = []

    def __init__(self, graph, state_service, **kwargs):
        pass

    def ensure_activated(self):
        pass

    def perceive_and_persist(self, episodes, llm, **kwargs):
        decisions, committed = _Perception.pairs.pop(0)
        return {"bound": 2, "advisory": 0, "decisions": decisions, "committed": committed,
                "results": [{"bound": True, "assertion_id": "a1"},
                            {"bound": True, "assertion_id": "a2"}]}


@pytest.fixture
def perception(monkeypatch):
    import menhir.services.typed_scalar_perception as tsp

    _Perception.pairs = []
    monkeypatch.setattr(tsp, "TypedScalarPerceptionService", _Perception)
    return _Perception


class _Llm:
    """Callable (system, user) -> str: the decide step (PR 2) calls it; "[]" votes nothing."""

    def __init__(self):
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        return "[]"


def _run(graph, *, match):
    # batch_size == page length so a full page makes the runner read the next one.
    return run_counting_consolidation(
        graph, namespaces=["ns-1"], counting_llm=_Llm(), build_episodes=_build_episodes,
        k=1, threshold=1.0, call_budget=None, embed=None,
        config=CountingConsolidationConfig(
            perceiver_version="cv1", scalar_perceiver_version="sv1", batch_size=2,
            match=match, extract_v2=False))


@pytest.mark.unit
def test_match_on_reports_gate_dropped_as_decisions_minus_committed(perception):
    # Fails if the pass does not report rows the k samples dropped (decisions - committed).
    perception.pairs = [(5, 2)]
    out = _run(_Graph([[_row(1), _row(2)]]), match=True)
    assert out["counting_gate_dropped"] == 3


@pytest.mark.unit
def test_match_on_gate_dropped_sums_over_pages(perception):
    # Fails if only the last (or first) page is counted instead of the sum.
    perception.pairs = [(5, 2), (4, 3)]
    out = _run(_Graph([[_row(1), _row(2)], [_row(1), _row(2)]]), match=True)
    assert perception.pairs == []  # both pages were perceived
    assert out["counting_gate_dropped"] == 3 + 1


@pytest.mark.unit
def test_match_on_gate_dropped_floors_each_page_at_zero(perception):
    # Fails if a page with committed > decisions subtracts from the other page's drops.
    perception.pairs = [(2, 5), (5, 2)]
    out = _run(_Graph([[_row(1), _row(2)], [_row(1), _row(2)]]), match=True)
    assert out["counting_gate_dropped"] == 3  # not 0 (= -3 + 3)


@pytest.mark.unit
def test_match_off_result_has_no_gate_dropped_key(perception):
    # Fails if the key is emitted while match is off (result keys must be unchanged).
    perception.pairs = [(5, 2)]
    out = _run(_Graph([[_row(1), _row(2)]]), match=False)
    assert out["counting_batches"] == 1
    assert "counting_gate_dropped" not in out


# ------------------------------------------------------------------ API


@pytest.fixture
def client():
    graph_adapter = SimpleNamespace(
        list_scalar_dirty_namespaces=lambda *, perceiver_version, limit=500: ["ns-bench"],
        count_turn_evidence=lambda ns: 2,
    )
    built = SimpleNamespace(
        ingest_service=SimpleNamespace(enrichment_enabled=lambda: True),
        scheduler=SimpleNamespace(status_snapshot=lambda: {"running": True}),
        graph_adapter=graph_adapter,
        settings=SimpleNamespace(
            personal_memory_consolidation_chat_model="pm-model",
            personal_memory_scalar_state_enabled=True,
            personal_memory_scalar_state_perceiver_version="v2",
            personal_memory_scalar_history_enabled=True,
        ),
    )
    ctx = SimpleNamespace(
        built=built,
        session=SimpleNamespace(session_id="process-session", user_id="claude-code"),
        capabilities=SimpleNamespace(startup_mode="full", failures=[]),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = ctx
    with patch.object(api_routes, "_get_backend", return_value=SimpleNamespace()):
        yield TestClient(app)


def _post_run(client, result):
    consolidate = AsyncMock(return_value=result)
    with patch("menhir.infrastructure.sync_llm.make_sync_chat",
               return_value=lambda prompt, user: "x"), \
            patch("menhir.infrastructure.view_embedder.make_view_embedder", return_value=None), \
            patch("menhir.services.scheduler_tasks.consolidate_personal_memory", new=consolidate):
        return client.post("/api/phase3/run", json={"namespace": "ns-bench"})


@pytest.mark.unit
def test_phase3_run_passes_match_counters_through(client):
    # Fails if the handler drops or mis-maps any of the three fields.
    resp = _post_run(client, {"counting_decided_counted": 7,
                              "counting_decided_uncertain": 4,
                              "counting_gate_dropped": 9})
    assert resp.status_code == 200
    data = resp.json()
    assert data["counting_decided_counted"] == 7
    assert data["counting_decided_uncertain"] == 4
    assert data["counting_gate_dropped"] == 9


@pytest.mark.unit
def test_phase3_run_match_counters_are_null_when_absent(client):
    # Fails if absent keys are coerced to 0 (match-off must be distinguishable from zero).
    resp = _post_run(client, {})
    assert resp.status_code == 200
    data = resp.json()
    for key in ("counting_decided_counted", "counting_decided_uncertain", "counting_gate_dropped"):
        assert key in data and data[key] is None
