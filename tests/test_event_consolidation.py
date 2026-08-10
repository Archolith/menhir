"""Event-consolidation RUNNER tests (offline; fakes only).

No external calls and no benchmark-derived IDs/content. These cover the Phase-3 event-history
backfill runner: target selection, the fail-closed page spine (persist -> lane rebuild -> cursor
advance), self-subject admission + canonicalization, structural fail-closed, budget gating, and
full-page pagination under a shared LLM call count.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from menhir.services.event_consolidation import (
    EventConsolidationConfig,
    run_event_consolidation,
    select_event_targets,
)


def _episode_count(log: str) -> int:
    indexes = [int(m) for m in re.findall(r"^\[(\d+)\] ", log, flags=re.M)]
    return (max(indexes) + 1) if indexes else 0


def _row(uuid: str, content: str, valid_at: str, cursor_at: str) -> dict:
    return {
        "uuid": uuid,
        "valid_at": valid_at,
        "cursor_at": cursor_at,
        "content": content,
        "source_kind": "chat",
        "session_id": "s1",
    }


class FakeLlm:
    """Counting callable LLM. When no explicit response is queued it returns a structurally valid
    empty episode-envelope array sized to the number of numbered episodes in the prompt."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls = 0
        self.logs: list[str] = []
        self.raise_on_call: Exception | None = None

    def __call__(self, system: str, user: str) -> str:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.calls += 1
        self.logs.append(user)
        if self._responses:
            return self._responses.pop(0)
        n = _episode_count(user)
        return json.dumps([{"episode": i, "events": []} for i in range(n)])


class FakeGraph:
    """Fake EventConsolidationGraph driving a queue of batch pages plus recording seams."""

    def __init__(self, pages: list[list[dict]] | None = None) -> None:
        self.pages = list(pages or [])
        self.discovered = ["discovered-a", "discovered-b"]
        self.discovery_calls: list[tuple[str, int]] = []
        self.loads = 0
        self.advances: list[tuple[str, str, str, str, str]] = []
        self.activated = 0
        self.self_uuid = "self-entity-uuid"
        self.records: list = []
        self.record_result: dict | None = None
        self.rebuild_complete = True
        self.rebuilt_lanes: list = []
        self.raise_on_load: Exception | None = None
        self.raise_on_advance: Exception | None = None
        self.raise_on_activate: Exception | None = None

    def list_event_dirty_evidence_namespaces(
        self, *, perceiver_version: str, limit: int
    ) -> list[str]:
        self.discovery_calls.append((perceiver_version, limit))
        return list(self.discovered)

    def load_next_event_evidence_batch(
        self, namespace: str, *, perceiver_version: str, limit: int
    ):
        self.loads += 1
        if self.raise_on_load is not None:
            exc, self.raise_on_load = self.raise_on_load, None
            raise exc
        return self.pages.pop(0) if self.pages else []

    def advance_event_cursor(
        self,
        namespace: str,
        *,
        cursor_at: str,
        cursor_uuid: str,
        perceiver_version: str,
        at: str,
    ) -> None:
        if self.raise_on_advance is not None:
            raise self.raise_on_advance
        self.advances.append((namespace, cursor_at, cursor_uuid, perceiver_version, at))

    def activate_event_history(self) -> dict:
        self.activated += 1
        if self.raise_on_activate is not None:
            raise self.raise_on_activate
        return {"activated": True}

    def ensure_self_entity(self, namespace: str) -> str:
        return self.self_uuid

    def record_typed_event_assertion(self, assertion):
        self.records.append(assertion)
        if self.record_result is not None:
            return self.record_result
        return {
            "assertion_id": f"aid-{len(self.records)}",
            "created": True,
            "superseded_prior": False,
            "binding_pending": False,
            "binding_mismatch": False,
        }

    def event_history_service(self):
        return self

    def rebuild_lanes(self, lanes):
        self.rebuilt_lanes.append(lanes)
        return {
            "complete": self.rebuild_complete,
            "lane_count": len(lanes),
            "results": [],
        }


def _default_config(**overrides) -> EventConsolidationConfig:
    kwargs = {"batch_size": 500}
    kwargs.update(overrides)
    return EventConsolidationConfig(**kwargs)


# --------------------------------------------------------------------------- targets


@pytest.mark.unit
def test_select_event_targets_explicit_vs_discovered():
    graph = FakeGraph()

    explicit = asyncio.run(select_event_targets(graph, ["x", "y"], "v1", 10))
    assert explicit == ["x", "y"]
    assert graph.discovery_calls == []  # explicit allowlist short-circuits discovery

    discovered = asyncio.run(select_event_targets(graph, None, "v1", 10))
    assert discovered == ["discovered-a", "discovered-b"]
    assert graph.discovery_calls == [("v1", 10)]


# --------------------------------------------------------------------------- success path


def _two_row_success_response() -> str:
    return json.dumps(
        [
            {
                "episode": 0,
                "events": [
                    {
                        "subject": "user",
                        "predicate": "bought",
                        "object": "a notebook",
                        "object_display": "",
                        "domain": "stationery",
                        "when": "2026-07-18",
                        "stated_span": "I bought a notebook on 2026-07-18.",
                    }
                ],
            },
            {
                "episode": 1,
                "events": [
                    {
                        "subject": "user",
                        "predicate": "got",
                        "object": "a new chair",
                        "object_display": "",
                        "domain": "furniture",
                        "when": "",
                        "stated_span": "I got a new chair.",
                    }
                ],
            },
        ]
    )


@pytest.mark.unit
def test_success_persists_rebuilds_advances_canonical_self_and_reference_time():
    t1 = _row(
        "t1",
        "I bought a notebook on 2026-07-18.",
        valid_at="2026-07-01T00:00:00Z",
        cursor_at="2026-07-05T00:00:00Z",
    )
    t2 = _row(
        "t2",
        "I got a new chair.",
        valid_at="2026-07-02T00:00:00Z",
        cursor_at="2026-07-06T00:00:00Z",
    )
    graph = FakeGraph(pages=[[t1, t2]])
    llm = FakeLlm(responses=[_two_row_success_response()])

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(),
    )

    assert graph.activated == 1
    # both assertions persisted and created
    assert len(graph.records) == 2
    assert metrics["event_assertions_recorded"] == 2
    assert metrics["event_assertions_created"] == 2
    # canonical self subject, domain forced to None, exact turn-evidence identity
    for assertion in graph.records:
        assert assertion.subject_display == "user"
        assert assertion.domain is None
    assert graph.records[0].turn_evidence_uuid == "t1"
    assert graph.records[1].turn_evidence_uuid == "t2"
    # explicit when vs episode-reference (row valid_at) time basis
    assert graph.records[0].time_basis == "explicit"
    assert "2026-07-18" in graph.records[0].valid_at
    assert graph.records[1].time_basis == "episode_reference"
    assert graph.records[1].valid_at == "2026-07-02T00:00:00Z"
    # lane rebuild ran complete; cursor advanced to the FINAL row of the page
    assert graph.rebuilt_lanes and graph.rebuilt_lanes[0]
    assert metrics["event_views_rebuilt"] >= 1
    assert graph.advances[-1] == (
        "ns1",
        "2026-07-06T00:00:00Z",
        "t2",
        "v1",
        graph.advances[-1][4],
    )
    assert metrics["event_namespaces_processed"] == 1
    assert metrics["event_namespaces_failed"] == 0
    assert metrics["event_llm_calls"] == llm.calls == 1
    assert metrics["event_namespaces_dirty"] == 1


@pytest.mark.unit
def test_valid_empty_envelope_advances():
    graph = FakeGraph(
        pages=[
            [
                _row(
                    "t1",
                    "just some ordinary prose",
                    valid_at="2026-07-01T00:00:00Z",
                    cursor_at="2026-07-05T00:00:00Z",
                ),
            ]
        ]
    )
    llm = FakeLlm()  # default: one empty envelope for the single episode

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(),
    )

    assert graph.records == []
    assert graph.rebuilt_lanes == []  # nothing admitted -> no rebuild needed
    assert metrics["event_assertions_recorded"] == 0
    assert metrics["event_namespaces_failed"] == 0
    assert graph.advances == [
        ("ns1", "2026-07-05T00:00:00Z", "t1", "v1", graph.advances[0][4])
    ]


# --------------------------------------------------------------------------- structural fail-closed


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [
        "this is not json at all",  # malformed_json
        json.dumps([{"no": "episode-shape"}]),  # missing envelope / bad structure
    ],
)
def test_structurally_invalid_response_does_not_advance(response):
    graph = FakeGraph(
        pages=[
            [
                _row(
                    "t1",
                    "I bought a notebook on 2026-07-18.",
                    valid_at="2026-07-01T00:00:00Z",
                    cursor_at="2026-07-05T00:00:00Z",
                ),
            ]
        ]
    )
    llm = FakeLlm(responses=[response])

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(),
    )

    assert graph.records == []
    assert graph.advances == []  # cursor stays dirty
    assert metrics["event_namespaces_failed"] == 1
    assert metrics["event_errors"].get("structural_output", 0) >= 1
    assert (
        metrics["event_namespaces_processed"] == 1
    )  # the page was attempted, not advanced


# --------------------------------------------------------------------------- self-subject admission


@pytest.mark.unit
def test_non_self_subject_rejected_but_structurally_valid_page_advances():
    graph = FakeGraph(
        pages=[
            [
                _row(
                    "t1",
                    "I bought a notebook on 2026-07-18.",
                    valid_at="2026-07-01T00:00:00Z",
                    cursor_at="2026-07-05T00:00:00Z",
                ),
            ]
        ]
    )
    llm = FakeLlm(
        responses=[
            json.dumps(
                [
                    {
                        "episode": 0,
                        "events": [
                            {
                                "subject": "the boss",
                                "predicate": "bought",
                                "object": "a notebook",
                                "object_display": "",
                                "domain": "",
                                "when": "",
                                "stated_span": "I bought a notebook on 2026-07-18.",
                            }
                        ],
                    }
                ]
            )
        ]
    )

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(),
    )

    assert graph.records == []  # non-self never persisted
    assert metrics["event_proposals"] == 1
    assert metrics["event_drop_reasons"].get("non_self_subject") == 1
    assert metrics["event_namespaces_failed"] == 0
    assert graph.advances != []  # zero admitted events still advances


@pytest.mark.unit
@pytest.mark.parametrize(
    "subject", ["user", "the user", "I", "me", "myself", "speaker"]
)
def test_all_self_tokens_are_admitted_and_canonicalized(subject):
    graph = FakeGraph(
        pages=[
            [
                _row(
                    "t1",
                    "I bought a notebook on 2026-07-18.",
                    valid_at="2026-07-01T00:00:00Z",
                    cursor_at="2026-07-05T00:00:00Z",
                ),
            ]
        ]
    )
    llm = FakeLlm(
        responses=[
            json.dumps(
                [
                    {
                        "episode": 0,
                        "events": [
                            {
                                "subject": subject,
                                "predicate": "bought",
                                "object": "a notebook",
                                "object_display": "",
                                "domain": "stationery",
                                "when": "2026-07-18",
                                "stated_span": "I bought a notebook on 2026-07-18.",
                            }
                        ],
                    }
                ]
            )
        ]
    )

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(),
    )

    assert len(graph.records) == 1
    assert graph.records[0].subject_display == "user"  # canonicalized
    assert graph.records[0].domain is None
    assert graph.records[0].namespace == "ns1"
    assert metrics["event_drop_reasons"].get("non_self_subject", 0) == 0


# --------------------------------------------------------------------------- persist / rebuild fail-closed


@pytest.mark.unit
def test_binding_pending_does_not_advance():
    graph = FakeGraph(
        pages=[
            [
                _row(
                    "t1",
                    "I bought a notebook on 2026-07-18.",
                    valid_at="2026-07-01T00:00:00Z",
                    cursor_at="2026-07-05T00:00:00Z",
                ),
            ]
        ]
    )
    graph.record_result = {
        "assertion_id": "aid-x",
        "created": True,
        "superseded_prior": False,
        "binding_pending": True,
        "binding_mismatch": False,
    }
    llm = FakeLlm(
        responses=[
            json.dumps(
                [
                    {
                        "episode": 0,
                        "events": [
                            {
                                "subject": "user",
                                "predicate": "bought",
                                "object": "a notebook",
                                "object_display": "",
                                "domain": "",
                                "when": "2026-07-18",
                                "stated_span": "I bought a notebook on 2026-07-18.",
                            }
                        ],
                    }
                ]
            )
        ]
    )

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(),
    )

    assert graph.advances == []
    assert metrics["event_namespaces_failed"] == 1
    assert metrics["event_errors"].get("persist_rejected", 0) >= 1


@pytest.mark.unit
def test_incomplete_rebuild_does_not_advance():
    graph = FakeGraph(
        pages=[
            [
                _row(
                    "t1",
                    "I bought a notebook on 2026-07-18.",
                    valid_at="2026-07-01T00:00:00Z",
                    cursor_at="2026-07-05T00:00:00Z",
                ),
            ]
        ]
    )
    graph.rebuild_complete = False
    llm = FakeLlm(
        responses=[
            json.dumps(
                [
                    {
                        "episode": 0,
                        "events": [
                            {
                                "subject": "user",
                                "predicate": "bought",
                                "object": "a notebook",
                                "object_display": "",
                                "domain": "",
                                "when": "2026-07-18",
                                "stated_span": "I bought a notebook on 2026-07-18.",
                            }
                        ],
                    }
                ]
            )
        ]
    )

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(),
    )

    # the assertion WAS persisted, but the incomplete rebuild leaves the cursor dirty
    assert len(graph.records) == 1
    assert graph.advances == []
    assert metrics["event_namespaces_failed"] == 1
    assert metrics["event_errors"].get("rebuild_incomplete", 0) >= 1


# --------------------------------------------------------------------------- budget + pagination


@pytest.mark.unit
def test_budget_prevents_untouched_call_and_advance():
    graph = FakeGraph(
        pages=[[_row("t1", "x", valid_at="2026-07-01", cursor_at="2026-07-05")]]
    )
    llm = FakeLlm()
    config = EventConsolidationConfig()

    metrics = run_event_consolidation(
        graph, event_targets=["ns1"], counting_llm=llm, call_budget=0, config=config
    )

    assert graph.loads == 0
    assert graph.advances == []
    assert metrics["event_namespaces_processed"] == 0
    assert metrics["event_llm_calls"] == 0
    assert llm.calls == 0


@pytest.mark.unit
def test_full_page_pagination_and_shared_call_count():
    page1 = [
        _row(
            "t1",
            "first note",
            valid_at="2026-07-01T00:00:00Z",
            cursor_at="2026-07-01T00:00:00Z",
        ),
        _row(
            "t2",
            "second note",
            valid_at="2026-07-02T00:00:00Z",
            cursor_at="2026-07-02T00:00:00Z",
        ),
    ]
    page2 = [
        _row(
            "t3",
            "third note",
            valid_at="2026-07-03T00:00:00Z",
            cursor_at="2026-07-03T00:00:00Z",
        ),
        _row(
            "t4",
            "fourth note",
            valid_at="2026-07-04T00:00:00Z",
            cursor_at="2026-07-04T00:00:00Z",
        ),
    ]
    graph = FakeGraph(pages=[page1, page2])
    llm = FakeLlm()  # empty envelopes sized per page

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(batch_size=2),
    )

    assert metrics["event_batches"] == 2
    assert metrics["event_namespaces_processed"] == 1
    assert len(graph.advances) == 2  # one cursor advance per page
    assert graph.advances[0][1:3] == ("2026-07-02T00:00:00Z", "t2")
    assert graph.advances[1][1:3] == ("2026-07-04T00:00:00Z", "t4")
    assert metrics["event_llm_calls"] == 2  # one extract call per page with episodes
    assert llm.calls == 2


# --------------------------------------------------------------------------- exception boundaries


@pytest.mark.unit
def test_extraction_exception_fails_page_no_persist_rebuild_cursor():
    graph = FakeGraph(
        pages=[
            [
                _row(
                    "t1",
                    "I bought a notebook on 2026-07-18.",
                    valid_at="2026-07-01T00:00:00Z",
                    cursor_at="2026-07-05T00:00:00Z",
                ),
            ]
        ]
    )
    llm = FakeLlm()
    llm.raise_on_call = RuntimeError("extraction boom")

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(),
    )

    assert graph.records == []
    assert graph.rebuilt_lanes == []
    assert graph.advances == []  # no persist/rebuild/cursor on extraction failure
    assert metrics["event_namespaces_failed"] == 1
    assert metrics["event_errors"].get("extraction_failed", 0) == 1
    assert metrics["event_namespaces_processed"] == 1  # page was touched


@pytest.mark.unit
def test_load_exception_fails_namespace_no_cursor_and_continues():
    graph = FakeGraph(pages=[[]])  # would be empty if load succeeded
    graph.raise_on_load = RuntimeError("load boom")

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1", "ns2"],
        counting_llm=FakeLlm(),
        call_budget=None,
        config=_default_config(),
    )

    assert graph.advances == []
    assert metrics["event_namespaces_failed"] == 1
    assert metrics["event_errors"].get("load_failed", 0) == 1
    # ns1 failed closed; processing continued to ns2 (its empty load succeeded)
    assert metrics["event_namespaces_processed"] == 1
    assert metrics["event_namespaces_failed"] == 1


@pytest.mark.unit
def test_cursor_advance_exception_marks_namespace_failed_no_crash():
    graph = FakeGraph(
        pages=[
            [
                _row(
                    "t1",
                    "I bought a notebook on 2026-07-18.",
                    valid_at="2026-07-01T00:00:00Z",
                    cursor_at="2026-07-05T00:00:00Z",
                ),
            ]
        ]
    )
    graph.raise_on_advance = RuntimeError("cursor boom")
    llm = FakeLlm()  # empty envelope -> zero admitted, still advances

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(),
    )

    assert graph.advances == []
    assert metrics["event_namespaces_failed"] == 1
    assert metrics["event_errors"].get("cursor_advance_failed", 0) == 1


@pytest.mark.unit
def test_activation_failure_fails_all_supplied_targets():
    graph = FakeGraph()
    graph.raise_on_activate = RuntimeError("activation boom")

    metrics = run_event_consolidation(
        graph,
        event_targets=["ns1", "ns2", "ns3"],
        counting_llm=FakeLlm(),
        call_budget=None,
        config=_default_config(),
    )

    assert graph.loads == 0
    assert graph.advances == []
    assert metrics["event_namespaces_processed"] == 0
    assert metrics["event_namespaces_failed"] == 3  # all targets, none processed
    assert metrics["event_errors"].get("activation_failed", 0) == 1


@pytest.mark.unit
def test_episode_content_capped_to_2000_chars_preserving_prefix():
    long_content = "A" * 5000
    graph = FakeGraph(
        pages=[
            [
                _row(
                    "t1",
                    long_content,
                    valid_at="2026-07-01T00:00:00Z",
                    cursor_at="2026-07-05T00:00:00Z",
                ),
            ]
        ]
    )
    llm = FakeLlm()  # empty envelope -> no proposals

    run_event_consolidation(
        graph,
        event_targets=["ns1"],
        counting_llm=llm,
        call_budget=None,
        config=_default_config(),
    )

    # the extraction prompt must carry only the first 2000 chars (exact prefix preserved)
    assert len(llm.logs) == 1
    assert llm.logs[0] == "[0] " + "A" * 2000
