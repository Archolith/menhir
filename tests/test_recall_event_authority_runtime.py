"""Focused async tests for the RecallService event-history authority runtime seam.

Covers the runtime wiring only (never the pure classifier/selector, which is covered by
test_event_history_authority.py): flag-off identity/no call, flag-on leading verdict with exact
canonical lookup args, recognized-empty advisory, namespace-none/no call, nested/third-party/no call,
and repository-exception fail-open identity. Uses generic synthetic assertions; no benchmark ids,
answers, or fixture-specific wording.
"""

from __future__ import annotations

import uuid

import pytest

import menhir.services.recall_service as recall_service_mod
from menhir.domain.event_history import TypedEventAssertion
from menhir.domain.recall import RecallResult
from menhir.domain.namespace import stamped_namespace
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


class _StubAdapter:
    """Minimal in-memory graph-adapter stand-in recording event-assertion lookup args."""

    def __init__(self, assertions: list[TypedEventAssertion] | None = None) -> None:
        self.assertions = assertions or []
        self.calls: list[dict] = []
        self.raise_on_lookup: BaseException | None = None

    def event_assertions_for_subject_predicate(
        self,
        subject_uuid: str,
        predicate: str,
        *,
        namespace: str | None = None,
        include_superseded: bool = False,
        materializable_only: bool = True,
    ) -> list[TypedEventAssertion]:
        self.calls.append(
            {
                "subject_uuid": subject_uuid,
                "predicate": predicate,
                "namespace": namespace,
                "include_superseded": include_superseded,
                "materializable_only": materializable_only,
            }
        )
        if self.raise_on_lookup is not None:
            raise self.raise_on_lookup
        return list(self.assertions)


def _make(
    *,
    object_key: str,
    valid_at: str = "2026-01-05T00:00:00Z",
    namespace: str | None = None,
    subject_uuid: str = "subj-a",
) -> TypedEventAssertion:
    return TypedEventAssertion(
        subject_uuid=subject_uuid,
        subject_display="Alpha",
        predicate="acquired",
        object_key=object_key,
        object_display="Object X",
        domain=None,
        namespace=namespace,
        valid_at=valid_at,
        learned_at="2026-01-06T00:00:00Z",
        stated_span="an exact stated span",
        episode_uuid="ep-1",
        turn_evidence_uuid="ev-1",
        span_start=10,
        span_end=30,
        claim_ordinal=0,
    )


def _base_result() -> RecallResult:
    return RecallResult(
        query="",
        preset="KNOWLEDGE",
        results=[],
        candidates_evaluated=0,
        nodes_touched=0,
        note="synthetic",
    )


def _patch_run_recall(monkeypatch: pytest.MonkeyPatch) -> RecallResult:
    original = _base_result()

    async def _run_recall(*args, **kwargs) -> RecallResult:
        return original

    monkeypatch.setattr(recall_service_mod, "run_recall", _run_recall)
    return original


def _service(
    monkeypatch: pytest.MonkeyPatch, adapter: _StubAdapter, *, enabled: bool,
) -> tuple[RecallService, RecallResult]:
    original = _patch_run_recall(monkeypatch)
    svc = RecallService(
        graphiti_client=object(),
        graph_adapter=adapter,
        scoring_service=ScoringService(),
        event_history_authority_enabled=enabled,
    )
    return svc, original


# --------------------------------------------------------------------------- flag-off


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_off_returns_original_and_makes_no_call(monkeypatch) -> None:
    adapter = _StubAdapter()
    svc, original = _service(monkeypatch, adapter, enabled=False)

    result = await svc.recall(
        "which notebook did I buy most recently?", namespace="alice")

    assert result is original
    assert result.event_authority_layer is None
    assert adapter.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforce_mode_excludes_legacy_unconfirmed_event_authority(monkeypatch) -> None:
    adapter = _StubAdapter([_make(object_key="legacy-notebook")])
    adapter.canonical_self_binding_mode = "enforce"
    svc, original = _service(monkeypatch, adapter, enabled=True)

    result = await svc.recall(
        "which notebook did I buy most recently?", namespace="alice"
    )

    assert result is original
    assert result.event_authority_layer is None
    assert adapter.calls == []


# --------------------------------------------------------------------------- namespace-none


@pytest.mark.unit
@pytest.mark.asyncio
async def test_namespace_none_makes_no_call(monkeypatch) -> None:
    adapter = _StubAdapter()
    svc, original = _service(monkeypatch, adapter, enabled=True)

    result = await svc.recall(
        "which notebook did I buy most recently?", namespace=None)

    assert result is original
    assert result.event_authority_layer is None
    assert adapter.calls == []


# --------------------------------------------------------------------------- nested / third-party


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_third_party_makes_no_call(monkeypatch) -> None:
    adapter = _StubAdapter()
    svc, original = _service(monkeypatch, adapter, enabled=True)

    result = await svc.recall(
        "Which notebook did my friend ask 'did I buy' most recently?",
        namespace="alice")

    assert result is original
    assert result.event_authority_layer is None
    assert adapter.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_third_party_query_makes_no_call(monkeypatch) -> None:
    adapter = _StubAdapter()
    svc, original = _service(monkeypatch, adapter, enabled=True)

    result = await svc.recall(
        "which notebook did you buy most recently?", namespace="alice")

    assert result is original
    assert result.event_authority_layer is None
    assert adapter.calls == []


# --------------------------------------------------------------------------- flag-on leading


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flag_on_leading_verdict_and_exact_lookup_args(monkeypatch) -> None:
    namespace = "alice"
    stamped = stamped_namespace(namespace)
    subject_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"menhir-self:{stamped}"))
    adapter = _StubAdapter(
        [
            _make(object_key="notebook-a", valid_at="2026-01-05T00:00:00Z", namespace=stamped, subject_uuid=subject_uuid),
            _make(object_key="notebook-b", valid_at="2026-02-05T00:00:00Z", namespace=stamped, subject_uuid=subject_uuid),
        ]
    )
    svc, _ = _service(monkeypatch, adapter, enabled=True)

    result = await svc.recall(
        "which notebook did I buy most recently?", namespace=namespace)

    assert result.event_authority_layer is not None
    verdict = result.event_authority_layer[0]
    assert verdict.status == "leads"
    assert verdict.gate == "pass"
    assert verdict.object_key == "notebook-b"
    assert verdict.subject_uuid == subject_uuid

    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["subject_uuid"] == subject_uuid
    assert call["predicate"] == "acquired"
    assert call["namespace"] == stamped
    assert call["include_superseded"] is False
    assert call["materializable_only"] is True


# --------------------------------------------------------------------------- recognized-empty advisory


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recognized_empty_returns_advisory(monkeypatch) -> None:
    namespace = "alice"
    adapter = _StubAdapter([])
    svc, _ = _service(monkeypatch, adapter, enabled=True)

    result = await svc.recall(
        "which notebook did I buy most recently?", namespace=namespace)

    assert result.event_authority_layer is not None
    verdict = result.event_authority_layer[0]
    assert verdict.status == "advisory"
    assert verdict.object_key is None
    assert len(adapter.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_missing_anchor_guard_returns_advisory(monkeypatch) -> None:
    namespace = "alice"
    stamped = stamped_namespace(namespace)
    subject_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"menhir-self:{stamped}"))
    adapter = _StubAdapter([
        _make(object_key="road bike", namespace=stamped, subject_uuid=subject_uuid),
    ])
    svc, _ = _service(monkeypatch, adapter, enabled=True)

    result = await svc.recall(
        "Before I purchased the touring bike, do I own any other bikes?",
        namespace=namespace,
    )

    assert result.event_authority_layer is not None
    verdict = result.event_authority_layer[0]
    assert verdict.status == "advisory"
    assert verdict.gate == "anchor"
    assert verdict.kind == "anchor_guard"
    assert verdict.object_key is None
    assert len(adapter.calls) == 1


# --------------------------------------------------------------------------- repository exception fail-open


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repository_exception_fail_open_identity(monkeypatch) -> None:
    adapter = _StubAdapter()
    adapter.raise_on_lookup = RuntimeError("repo boom")
    svc, original = _service(monkeypatch, adapter, enabled=True)

    result = await svc.recall(
        "which notebook did I buy most recently?", namespace="alice")

    assert result is original
    assert result.event_authority_layer is None
    assert len(adapter.calls) == 1
