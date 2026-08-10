from __future__ import annotations

import json
from types import SimpleNamespace

from menhir.services.typed_scalar_service import TypedScalarPerceptionService


class _Adapter:
    def activate_scalar_state(self): return {"ok": True}
    def scalar_state_schema_ready(self): return True
    def fetch_linked_entities_for_episode(self, _uuid): return []
    def record_typed_assertion(self, **_kwargs): return SimpleNamespace(uuid="assertion")
    def mark_projection_complete(self, _ids): return None
    def ensure_self_entity(self, _namespace): return None


class _State:
    def rebuild_scalar_state(self, *_args, **_kwargs): return None


class _Llm:
    def __init__(self, response):
        self.calls = 0
        self.response = response

    def __call__(self, _system, _user):
        self.calls += 1
        return self.response


def _episode(uuid, content): return SimpleNamespace(uuid=uuid, content=content, reference_time=None)


def _run(monkeypatch, episodes, llm, *, k=3, router=True, shadow=False, promoted=("c_count",)):
    persisted = []
    monkeypatch.setattr(
        "menhir.services.typed_scalar_service.bind_and_persist_typed_scalars",
        lambda decisions, **_kwargs: persisted.append(decisions) or {"bound": len(decisions)},
    )
    service = TypedScalarPerceptionService(
        _Adapter(), _State(), deterministic_router_enabled=router,
        deterministic_shadow_enabled=shadow,
        deterministic_router_promoted_classes=promoted,
    )
    result = service.perceive_and_persist(
        episodes, llm, k=k, namespace="ns", episode_reference_time=lambda _uuid: None,
    )
    return result, persisted


def test_router_skips_llm_only_for_promoted_deterministic_episodes(monkeypatch):
    episodes = [_episode("det", "I have 4 coins."), _episode("content", "No scalar claim here.")]
    llm = _Llm("[]")
    result, persisted = _run(monkeypatch, episodes, llm)
    assert llm.calls == 3
    assert result["committed"] == 1
    assert len(persisted) == 1


def test_router_sends_only_ambiguous_review_subset_to_each_llm_pass(monkeypatch):
    episodes = [_episode("det", "I have 4 coins."), _episode("review", "Maybe I have 4 coins.")]
    response = json.dumps([{"episode": 0, "observations": []}])
    llm = _Llm(response)
    result, _persisted = _run(monkeypatch, episodes, llm, k=3)
    assert llm.calls == 3
    assert result["committed"] == 1


def test_default_empty_promotion_reviews_deterministic_and_content_only(monkeypatch):
    episodes = [_episode("det", "I have 4 coins."), _episode("content", "No scalar claim here.")]
    llm = _Llm("[]")
    result, _persisted = _run(monkeypatch, episodes, llm, promoted=())
    assert llm.calls == 3
    assert result["committed"] == 0


def test_promoted_deterministic_plus_zero_proposal_reviews_only_latter(monkeypatch):
    episodes = [_episode("det", "I have 4 coins."), _episode("content", "No scalar claim here.")]
    llm = _Llm("[]")
    result, persisted = _run(monkeypatch, episodes, llm, promoted=("c_count",))
    assert llm.calls == 3
    assert result["committed"] == 1
    assert [decision.proposal.episode_uuid for decision in persisted[0] if decision.committed] == ["det"]


def test_flag_off_keeps_legacy_llm_call_count(monkeypatch):
    episodes = [_episode("one", "I have 4 coins.")]
    response = json.dumps([{"episode": 0, "observations": [{
        "subject": "user", "attribute": "coins", "scope": "", "value_kind": "count",
        "unit": "", "operation": "absolute", "value": 4, "stated_span": "I have 4 coins",
    }]}])
    llm = _Llm(response)
    result, _persisted = _run(monkeypatch, episodes, llm, router=False)
    assert llm.calls == 3
    assert result["committed"] == 1


def test_router_failure_uses_legacy_all_episode_llm_path(monkeypatch):
    episodes = [_episode("one", "I have 4 coins."), _episode("two", "I have 5 coins.")]
    response = json.dumps([{"episode": 0, "observations": []}, {"episode": 1, "observations": []}])
    llm = _Llm(response)

    class _BrokenExtractor:
        def extract(self, _episodes):
            raise RuntimeError("boom")

    monkeypatch.setattr("menhir.services.deterministic_scalar_router.DeterministicScalarExtractor", _BrokenExtractor)
    result, persisted = _run(monkeypatch, episodes, llm)
    assert llm.calls == 3
    assert result["committed"] == 0
    assert all(not decision.reason.startswith("deterministic_") for batch in persisted for decision in batch)


def test_extractor_exception_audit_reports_legacy_llm_routes_without_episode_content(monkeypatch):
    events = []
    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.is_enabled", lambda: True)
    monkeypatch.setattr(
        "menhir.infrastructure.consolidation_audit.audit",
        lambda event, state, **kwargs: events.append((event, state, kwargs)),
    )

    class _BrokenExtractor:
        def extract(self, _episodes):
            raise RuntimeError("sensitive episode content must not enter audit details")

    monkeypatch.setattr("menhir.services.deterministic_scalar_router.DeterministicScalarExtractor", _BrokenExtractor)
    episodes = [_episode("one", "secret one"), _episode("two", "secret two")]
    _run(monkeypatch, episodes, _Llm("[]"))

    details = [item for item in events if item[0] == "deterministic_router"][0][2]["details"]
    assert details["route_counts"] == {"llm_review": 2}
    assert details["reviewed_episodes"] == 2
    assert details["failure"] == "RuntimeError"
    assert details["failure_details"] == {"kind": "exception", "exception_type": "RuntimeError"}
    assert "sensitive episode content" not in repr(details)


def test_invalid_promoted_class_audit_reports_bounded_router_failure(monkeypatch):
    events = []
    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.is_enabled", lambda: True)
    monkeypatch.setattr(
        "menhir.infrastructure.consolidation_audit.audit",
        lambda event, state, **kwargs: events.append((event, state, kwargs)),
    )
    episodes = [_episode("one", "I have 4 coins."), _episode("two", "No scalar claim here.")]
    _run(monkeypatch, episodes, _Llm("[]"), promoted=("not_a_real_class",))

    details = [item for item in events if item[0] == "deterministic_router"][0][2]["details"]
    assert details["route_counts"] == {"llm_review": 2}
    assert details["reviewed_episodes"] == 2
    assert details["failure"] == "ValueError"
    assert details["failure_details"] == {"kind": "exception", "exception_type": "ValueError"}
    assert details["promoted_class_ids"] == ("not_a_real_class",)


def test_decision_conversion_mismatch_fails_closed_to_all_llm_episodes(monkeypatch):
    events = []
    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.is_enabled", lambda: True)
    monkeypatch.setattr(
        "menhir.infrastructure.consolidation_audit.audit",
        lambda event, state, **kwargs: events.append((event, state, kwargs)),
    )
    monkeypatch.setattr(
        "menhir.services.deterministic_scalar_router.DeterministicScalarRouter._to_decisions",
        lambda _result: [],
    )
    episodes = [_episode("one", "I have 4 coins."), _episode("two", "No scalar claim here.")]
    llm = _Llm("[]")
    _run(monkeypatch, episodes, llm)

    assert llm.calls == 3
    details = [item for item in events if item[0] == "deterministic_router"][0][2]["details"]
    assert details["route_counts"] == {"llm_review": 2}
    assert details["reviewed_episodes"] == 2
    assert details["failure"] == "deterministic_router_contract_failure:decision_conversion_mismatch"


def test_mixed_review_deterministic_review_merges_in_original_order(monkeypatch):
    episodes = [
        _episode("review-a", "Maybe I have 4 coins."),
        _episode("det", "I have 5 coins."),
        _episode("review-b", "Maybe I have 6 coins."),
    ]
    response = json.dumps([
        {"episode": 0, "observations": [{"subject": "user", "attribute": "coins", "scope": "", "value_kind": "count", "unit": "", "operation": "absolute", "value": 4, "stated_span": "I have 4 coins"}]},
        {"episode": 1, "observations": [{"subject": "user", "attribute": "coins", "scope": "", "value_kind": "count", "unit": "", "operation": "absolute", "value": 6, "stated_span": "I have 6 coins"}]},
    ])
    llm = _Llm(response)
    _result, persisted = _run(monkeypatch, episodes, llm)
    assert llm.calls == 3
    assert [decision.proposal.episode_uuid for decision in persisted[0] if decision.committed] == ["review-a", "det", "review-b"]


def test_router_audit_is_bounded_and_quote_free(monkeypatch):
    events = []
    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.is_enabled", lambda: True)
    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.audit", lambda event, state, **kwargs: events.append((event, state, kwargs)))
    episodes = [_episode("det", "I have 4 coins."), _episode("content", "No scalar claim here.")]
    _run(monkeypatch, episodes, _Llm("[]"))
    router_events = [item for item in events if item[0] == "deterministic_router"]
    assert len(router_events) == 1
    details = router_events[0][2]["details"]
    assert details["episodes_total"] == 2
    assert details["deterministic_decisions"] == 1
    assert details["route_counts"] == {"deterministic_scalar": 1, "llm_review": 1}
    assert details["route_class_counts"] == {"deterministic_scalar": {"c_count": 1}}
    assert "I have 4 coins" not in repr(details)


def test_router_shadow_reuses_one_extraction_and_scopes_to_review_subset(monkeypatch):
    events = []
    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.is_enabled", lambda: True)
    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.audit", lambda event, state, **kwargs: events.append((event, state, kwargs)))
    from menhir.services.deterministic_scalar_router import DeterministicScalarExtractor

    calls = []
    class _SpyExtractor:
        def extract(self, episodes):
            calls.append(tuple(episode.uuid for episode in episodes))
            return DeterministicScalarExtractor().extract(episodes)

    monkeypatch.setattr("menhir.services.deterministic_scalar_router.DeterministicScalarExtractor", _SpyExtractor)
    episodes = [_episode("det", "I have 4 coins."), _episode("review", "Maybe I have 5 coins.")]
    _run(monkeypatch, episodes, _Llm("[]"), shadow=True)
    assert calls == [("det", "review")]
    shadow = [item for item in events if item[0] == "deterministic_shadow"][0]
    assert shadow[2]["details"]["comparison_scope"] == "llm_reviewed_subset"
    assert shadow[2]["details"]["compositional"]["diagnostic_vs_llm"]["diagnostic_llm_router_misses"] == 0
