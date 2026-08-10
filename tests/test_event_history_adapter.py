"""Pure/fake unit tests for the Event History adapter seam on MemoryGraphAdapter.

Covers: __post_init__ repository wiring, every typed-event delegate, the
event_history_service factory binding, and every event-timeline View delegate.
No Neo4j, no fixture IDs, generic synthetic data only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.typed_event_repository import TypedEventAssertionRepository
from menhir.services.event_history_service import EventHistoryService


@dataclass
class _StubNeo4jRepository:
    """Minimal Neo4j stand-in that records execute calls and returns canned rows."""

    responses: list[list[dict[str, object]]] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(
        self, query: str, params: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        self.calls.append({"query": query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


def _capture(*, returns: dict[str, Any] | None = None) -> Any:
    """Capture fake exposing the typed-event repo and View repo surface."""
    returns = returns or {}
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class _Fake:
        def _record(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            calls.append((name, args, kwargs))
            return returns.get(name, object())

        def activate(self) -> Any:
            return self._record("activate", (), {})

        def record_event_assertion(self, assertion: Any) -> Any:
            return self._record("record_event_assertion", (assertion,), {})

        def assertions_for_lane(
            self,
            lane: Any,
            *,
            include_superseded: bool = False,
            materializable_only: bool = False,
        ) -> Any:
            return self._record(
                "assertions_for_lane",
                (lane,),
                {"include_superseded": include_superseded, "materializable_only": materializable_only},
            )

        def assertions_for_subject_predicate(
            self,
            subject_uuid: str,
            predicate: str,
            *,
            namespace: str | None = None,
            include_superseded: bool = False,
            materializable_only: bool = True,
        ) -> Any:
            return self._record(
                "assertions_for_subject_predicate",
                (subject_uuid, predicate),
                {"namespace": namespace, "include_superseded": include_superseded,
                 "materializable_only": materializable_only},
            )

        def assertion_by_key(self, assertion_key: str) -> Any:
            return self._record("assertion_by_key", (assertion_key,), {})

        def current_for_source(self, source_key: str) -> Any:
            return self._record("current_for_source", (source_key,), {})

        def record_event_timeline(self, **kwargs: Any) -> Any:
            return self._record("record_event_timeline", (), kwargs)

        def fetch_event_timeline(self, **kwargs: Any) -> Any:
            return self._record("fetch_event_timeline", (), kwargs)

        def list_event_timeline_views(self, **kwargs: Any) -> Any:
            return self._record("list_event_timeline_views", (), kwargs)

        def retire_event_timeline(self, **kwargs: Any) -> Any:
            return self._record("retire_event_timeline", (), kwargs)

        def draw_event_timeline_entries(self, **kwargs: Any) -> Any:
            return self._record("draw_event_timeline_entries", (), kwargs)

        def list_event_timeline_entries(self, **kwargs: Any) -> Any:
            return self._record("list_event_timeline_entries", (), kwargs)

    fake = _Fake()
    fake.calls = calls
    return fake


def _bare_adapter(events: Any, views: Any) -> MemoryGraphAdapter:
    """Adapter without __post_init__ (via __new__) so unrelated construction cannot interfere."""
    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter._typed_event_assertions = events
    adapter._views = views
    return adapter


@pytest.mark.unit
def test_post_init_installs_typed_event_assertion_repository_bound_to_same_neo4j() -> None:
    neo4j = _StubNeo4jRepository()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    repo = adapter._typed_event_assertions
    assert isinstance(repo, TypedEventAssertionRepository)
    assert repo._neo4j is neo4j


@pytest.mark.unit
def test_activate_event_history_forwards_and_returns() -> None:
    events = _capture(returns={"activate": {"queries_executed": 8}})
    adapter = _bare_adapter(events=events, views=_capture())

    assert adapter.activate_event_history() == {"queries_executed": 8}
    assert events.calls == [("activate", (), {})]


@pytest.mark.unit
def test_record_typed_event_assertion_forwards_positional_identity_and_result() -> None:
    events = _capture(returns={"record_event_assertion": {"assertion_id": "a-1"}})
    adapter = _bare_adapter(events=events, views=_capture())
    assertion = object()

    assert adapter.record_typed_event_assertion(assertion) == {"assertion_id": "a-1"}
    name, args, kwargs = events.calls[0]
    assert name == "record_event_assertion"
    assert args == (assertion,)
    assert kwargs == {}


@pytest.mark.unit
def test_event_assertions_for_lane_forwards_exact_keywords_and_result() -> None:
    sentinel = object()
    events = _capture(returns={"assertions_for_lane": sentinel})
    adapter = _bare_adapter(events=events, views=_capture())
    lane = object()

    result = adapter.event_assertions_for_lane(
        lane, include_superseded=True, materializable_only=True
    )

    assert result is sentinel
    name, args, kwargs = events.calls[0]
    assert name == "assertions_for_lane"
    assert args == (lane,)
    assert kwargs == {"include_superseded": True, "materializable_only": True}


@pytest.mark.unit
def test_event_assertions_for_lane_forwards_defaults() -> None:
    events = _capture()
    adapter = _bare_adapter(events=events, views=_capture())
    lane = object()

    adapter.event_assertions_for_lane(lane)

    name, args, kwargs = events.calls[0]
    assert name == "assertions_for_lane"
    assert args == (lane,)
    assert kwargs == {"include_superseded": False, "materializable_only": False}


@pytest.mark.unit
def test_event_assertions_for_subject_predicate_forwards_exact_args_and_result() -> None:
    sentinel = object()
    events = _capture(returns={"assertions_for_subject_predicate": sentinel})
    adapter = _bare_adapter(events=events, views=_capture())

    result = adapter.event_assertions_for_subject_predicate(
        "subj-a", "purchased", namespace="ns",
        include_superseded=True, materializable_only=True,
    )

    assert result is sentinel
    name, args, kwargs = events.calls[0]
    assert name == "assertions_for_subject_predicate"
    assert args == ("subj-a", "purchased")
    assert kwargs == {"namespace": "ns", "include_superseded": True, "materializable_only": True}


@pytest.mark.unit
def test_event_assertions_for_subject_predicate_forwards_defaults() -> None:
    events = _capture()
    adapter = _bare_adapter(events=events, views=_capture())

    adapter.event_assertions_for_subject_predicate("subj-a", "purchased")

    name, args, kwargs = events.calls[0]
    assert name == "assertions_for_subject_predicate"
    assert args == ("subj-a", "purchased")
    assert kwargs == {"namespace": None, "include_superseded": False, "materializable_only": True}


@pytest.mark.unit
def test_typed_event_assertion_by_key_forwards_key_and_result() -> None:
    sentinel = object()
    events = _capture(returns={"assertion_by_key": sentinel})
    adapter = _bare_adapter(events=events, views=_capture())

    assert adapter.typed_event_assertion_by_key("key-1") is sentinel
    name, args, kwargs = events.calls[0]
    assert name == "assertion_by_key"
    assert args == ("key-1",)
    assert kwargs == {}


@pytest.mark.unit
def test_current_typed_event_assertion_for_source_forwards_key_and_result() -> None:
    sentinel = object()
    events = _capture(returns={"current_for_source": sentinel})
    adapter = _bare_adapter(events=events, views=_capture())

    assert adapter.current_typed_event_assertion_for_source("src-1") is sentinel
    name, args, kwargs = events.calls[0]
    assert name == "current_for_source"
    assert args == ("src-1",)
    assert kwargs == {}


@pytest.mark.unit
def test_event_history_service_uses_adapter_event_source_and_adapter_as_sink() -> None:
    events = _capture()
    adapter = _bare_adapter(events=events, views=_capture())

    service = adapter.event_history_service()

    assert isinstance(service, EventHistoryService)
    assert service.source is events
    assert service.sink is adapter


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("record_event_timeline", {"subject": "s", "subject_uuid": "u", "predicate": "p"}),
        ("fetch_event_timeline", {"subject_uuid": "u", "predicate": "p"}),
        ("list_event_timeline_views", {"subject_uuid": "u"}),
        ("retire_event_timeline", {"view_key": "vk"}),
        ("draw_event_timeline_entries", {"view_uuid": "vu", "entries": []}),
        ("list_event_timeline_entries", {"view_uuid": "vu", "offset": 2, "limit": 10}),
    ],
)
def test_event_timeline_view_delegates_forward_exact_kwargs_and_return(name: str, kwargs: Any) -> None:
    sentinel = object()
    views = _capture(returns={name: sentinel})
    adapter = _bare_adapter(events=_capture(), views=views)

    result = getattr(adapter, name)(**kwargs)

    assert result is sentinel
    assert views.calls == [(name, (), kwargs)]
