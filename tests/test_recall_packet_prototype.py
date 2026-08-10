"""Tests for the inspection-only typed recall packet."""

import pytest

from menhir.explorer.recall_packet_prototype import (
    QUERY_FILTERED_PACKET_VERSION,
    build_query_filtered_recall_packet,
    build_typed_recall_packet,
)
from menhir.infrastructure.view_repository import ScalarStateKind


def _sections(packet):
    return {section["id"]: section["entries"] for section in packet["sections"]}


def test_packet_separates_authority_history_events_and_untyped_content():
    packet = build_typed_recall_packet(
        {
            "assertions": [
                {
                    "id": "as-current",
                    "evidence_id": "turn-current",
                    "operation": "delta",
                    "learned_at": "2026-01-03T00:00:00Z",
                    "stated_span": "I added two more",
                    "source_quote": "I added two more bikes",
                },
                {
                    "id": "as-old",
                    "evidence_id": "turn-old",
                    "operation": "absolute",
                    "learned_at": "2026-01-01T00:00:00Z",
                    "stated_span": "I owned two bikes",
                    "source_quote": "I owned two bikes",
                },
            ],
            "views": [
                {
                    "id": "state-current",
                    "current": True,
                    "subject": "user",
                    "attribute": "owned",
                    "scope": "bikes",
                    "display": "4",
                    "value": "4",
                    "value_kind": "count",
                    "valid_at": "2026-01-03T00:00:00Z",
                    "derivation": "mixed",
                    "contributor_ids": ["as-old", "as-current"],
                },
                {
                    "id": "state-superseded",
                    "current": False,
                    "subject": "user",
                    "attribute": "owned",
                    "scope": "bikes",
                    "display": "2",
                },
            ],
            "history_views": [
                {
                    "id": "history-current",
                    "current": True,
                    "subject": "user",
                    "attribute": "owned",
                    "scope": "bikes",
                    "derivation": "mixed",
                    "entries": [
                        {
                            "assertion_id": "as-old",
                            "operation": "absolute",
                            "value": "2",
                            "valid_at": "2026-01-01T00:00:00Z",
                            "stated_span": "I owned two bikes",
                        },
                        {
                            "assertion_id": "as-current",
                            "operation": "delta",
                            "value": "2",
                            "valid_at": "2026-01-03T00:00:00Z",
                            "stated_span": "I added two more",
                        },
                    ],
                }
            ],
            "event_assertions": [
                {
                    "id": "event-as-1",
                    "assertion_key": "event-key-1",
                    "evidence_id": "episode-1",
                    "turn_evidence_id": "turn-event-1",
                    "subject": "user",
                    "predicate": "purchased",
                    "domain": "bicycles",
                    "object_key": "road-bike",
                    "object_display": "a road bike",
                    "stated_span": "I bought a road bike",
                    "valid_at": "2026-01-02T00:00:00Z",
                    "learned_at": "2026-01-02T01:00:00Z",
                    "superseded": False,
                }
            ],
            "event_views": [
                {
                    "id": "event-view-1",
                    "current": True,
                    "subject": "user",
                    "predicate": "purchased",
                    "domain": "bicycles",
                    "contributor_ids": ["event-key-1"],
                    "entries": [
                        {
                            "when": "2026-01-02T00:00:00Z",
                            "what": "purchased a road bike",
                            "object_key": "road-bike",
                            "quote": "I bought a road bike",
                        }
                    ],
                }
            ],
            "memory_inventory": [
                {
                    "id": "content:0",
                    "memory_type": "content",
                    "subject": "user",
                    "relation": "RELATES_TO",
                    "object": "tennis tournament",
                    "content": (
                        "user has been thinking about signing up for a tennis "
                        "tournament that is happening on May 6th"
                    ),
                    "episode_ids": ["episode-plan"],
                    "valid_at": "2026-05-01T00:00:00Z",
                    "learned_at": "2026-05-02T00:00:00Z",
                },
                {
                    "id": "content:1",
                    "memory_type": "content",
                    "subject": "user",
                    "relation": "LIKES",
                    "object": "tennis",
                    "content": "user likes tennis",
                    "episode_ids": ["episode-content"],
                },
            ],
        }
    )

    sections = _sections(packet)
    assert packet["mode"] == "inspection_only"
    assert packet["production_recall_changed"] is False
    assert [entry["value"] for entry in sections["current_state"]] == ["4"]
    assert sections["current_state"][0]["authority"] == "current"
    assert sections["current_state"][0]["learned_at"] == "2026-01-03T00:00:00Z"
    assert [entry["derivation"] for entry in sections["change_history"]] == [
        "absolute",
        "delta",
    ]
    assert len(sections["completed_events"]) == 1
    assert sections["completed_events"][0]["authority"] == "completed"
    assert "turn-event-1" in sections["completed_events"][0]["source_ids"]
    assert "tentative_plans" not in sections
    assert "memory_text: user has been thinking about" in packet["text"]
    assert len(sections["general_content"]) == 2
    assert {entry["authority"] for entry in sections["general_content"]} == {"context"}
    assert "Do not infer scalar, event, or intent authority" in packet["text"]


def test_packet_uses_duration_view_display_while_retaining_canonical_unit():
    view_props = ScalarStateKind().write_props(
        "user",
        "key",
        {
            "attribute": "personal_best_time",
            "scope": "",
            "value_kind": "duration",
            "unit": "seconds",
            "value": 1550,
        },
    )
    packet = build_typed_recall_packet(
        {
            "views": [
                {
                    "id": "state-current",
                    "current": True,
                    "subject": "user",
                    "attribute": view_props["ss_attribute"],
                    "scope": view_props["ss_scope"],
                    "display": view_props["ss_display"],
                    "value": view_props["ss_value"],
                    "value_kind": view_props["ss_kind"],
                    "unit": view_props["ss_unit"],
                }
            ]
        }
    )

    entry = _sections(packet)["current_state"][0]
    assert entry["value"] == "25:50"
    assert entry["canonical_value"] == "1550"
    assert entry["unit"] == "seconds"
    assert "user.personal_best_time = 25:50 |" in packet["text"]
    assert "canonical=1550 seconds" in packet["text"]
    assert "25:50 seconds" not in packet["text"]


def test_untyped_content_is_never_assigned_intent_authority():
    packet = build_typed_recall_packet(
        {
            "memory_inventory": [
                {
                    "id": "content:0",
                    "memory_type": "content",
                    "subject": "tournament",
                    "relation": "HAPPENS_ON",
                    "object": "May 6",
                    "content": "the user is thinking about a tournament on May 6",
                }
            ]
        }
    )
    sections = _sections(packet)
    assert "tentative_plans" not in sections
    assert len(sections["general_content"]) == 1
    assert sections["general_content"][0]["authority"] == "context"


def test_empty_packet_is_explicit_and_stable():
    packet = build_typed_recall_packet({})
    assert all(not section["entries"] for section in packet["sections"])
    assert packet["text"].count("- none") == 4


def _full_packet_for_filtering():
    return {
        "version": "typed-recall-packet/prototype-v1",
        "sections": [
            {
                "id": "current_state",
                "entries": [
                    {
                        "id": "view-bikes",
                        "kind": "scalar_state",
                        "authority": "current",
                        "subject": "user",
                        "attribute": "owned",
                        "scope": "bikes",
                        "value": "4",
                        "valid_at": "2026-01-03",
                        "source_quote": "I own four bikes now",
                    },
                    {
                        "id": "view-cats",
                        "kind": "scalar_state",
                        "authority": "current",
                        "subject": "user",
                        "attribute": "owned",
                        "scope": "cats",
                        "value": "2",
                    },
                ],
            },
            {
                "id": "change_history",
                "entries": [
                    {
                        "id": "history-bikes:0",
                        "kind": "scalar_history",
                        "authority": "advisory",
                        "subject": "user",
                        "attribute": "owned",
                        "scope": "bikes",
                        "value": "2",
                        "valid_at": "2026-01-01",
                    },
                    {
                        "id": "history-bikes:1",
                        "kind": "scalar_history",
                        "authority": "advisory",
                        "subject": "user",
                        "attribute": "owned",
                        "scope": "bikes",
                        "value": "4",
                        "valid_at": "2026-01-03",
                    },
                ],
            },
            {"id": "completed_events", "entries": []},
            {
                "id": "general_content",
                "entries": [
                    {"id": "content:0", "content": "unrelated complete inventory"}
                ],
            },
        ],
    }


def _retrieval_rows():
    return [
        {
            "rank": 1,
            "uuid": "memory-bike-context",
            "name": "user bicycle ownership",
            "content": "The user talks about owning bicycles.",
            "memory_type": "content",
            "temporal_facts": [
                {
                    "fact": "The user owns bicycles",
                    "valid_at": "2026-01-03",
                    "created_at": "2026-01-04",
                    "is_current_belief": True,
                }
            ],
        },
        {
            "rank": 2,
            "uuid": "view-bikes",
            "name": "user owned bikes",
            "content": "4",
            "memory_type": "view",
            "is_scalar_authority": False,
        },
        {
            "rank": 3,
            "uuid": "memory-plan",
            "name": "possible race",
            "content": "The user is thinking about entering a bicycle race.",
            "memory_type": "content",
        },
        {
            "rank": 4,
            "uuid": "memory-noise",
            "name": "cooking",
            "content": "The user made soup. " * 600,
            "memory_type": "content",
        },
    ]


def test_query_filtered_packet_joins_retrieved_scalar_authority_and_filters_inventory():
    packet = build_query_filtered_recall_packet(
        _full_packet_for_filtering(),
        _retrieval_rows(),
        "How many bikes does the user own now?",
        max_chars=3_000,
    )
    sections = _sections(packet)
    assert packet["version"] == QUERY_FILTERED_PACKET_VERSION
    assert packet["production_recall_changed"] is False
    assert packet["budget"]["actual_chars"] <= 3_000
    assert [entry["id"] for entry in sections["current_state"]] == ["view-bikes"]
    assert sections["current_state"][0]["authority"] == "current"
    assert sections["change_history"] == []
    assert "unrelated complete inventory" not in packet["text"]
    assert "view-cats" not in packet["selection"]["included_ids"]
    assert "memory-plan" in packet["selection"]["included_ids"]
    assert "tentative_plans" not in sections
    assert next(
        entry for entry in sections["general_content"] if entry["id"] == "memory-plan"
    )["authority"] == "context"


def test_query_filtered_packet_adds_single_word_inflected_scalar_identity():
    packet = build_query_filtered_recall_packet(
        _full_packet_for_filtering(),
        [
            {
                "rank": 1,
                "uuid": "related-bike-memory",
                "name": "bikes",
                "content": "The user previously had three bikes.",
                "memory_type": "semantic",
            }
        ],
        "How many bikes do I currently own?",
    )
    assert [entry["id"] for entry in _sections(packet)["current_state"]] == [
        "view-bikes"
    ]


def test_query_filtered_packet_compacts_around_query_relevant_sentence():
    irrelevant = "The user discussed sculpture materials and techniques. " * 30
    packet = build_query_filtered_recall_packet(
        _full_packet_for_filtering(),
        [
            {
                "rank": 1,
                "uuid": "sculpture-memory",
                "name": "abstract ocean sculpture",
                "content": (
                    irrelevant
                    + "The user has spent 10-12 hours on the abstract ocean sculpture."
                ),
                "memory_type": "semantic",
                "temporal_facts": [
                    {
                        "fact": "The earlier estimate was five hours.",
                        "is_current_belief": False,
                    }
                ],
            }
        ],
        "How many hours have I spent on my abstract ocean sculpture?",
    )
    entry = _sections(packet)["general_content"][0]
    assert "10-12 hours" in entry["content"]
    assert len(entry["content"]) <= 650
    assert "temporal_fact (belief=superseded)" in packet["text"]


def test_query_filtered_packet_ignores_empty_current_temporal_metadata():
    packet = build_query_filtered_recall_packet(
        _full_packet_for_filtering(),
        [
            {
                "rank": 1,
                "uuid": "superseded-trip",
                "name": "family trip to Hawaii",
                "content": "The user remembered a family trip to Hawaii.",
                "memory_type": "semantic",
                "temporal_facts": [
                    {"fact": None, "is_current_belief": True},
                    {
                        "fact": "The Hawaii belief was superseded by a later trip.",
                        "valid_at": "2023-05-29",
                        "invalid_at": "2023-05-30",
                        "is_current_belief": False,
                    },
                ],
            }
        ],
        "Where was the most recent family trip?",
    )
    entry = _sections(packet)["general_content"][0]
    assert entry["authority"] == "superseded"
    assert [fact["fact"] for fact in entry["temporal_facts"]] == [
        "The Hawaii belief was superseded by a later trip."
    ]


def test_query_filtered_packet_adds_matching_history_only_for_history_intent():
    current = build_query_filtered_recall_packet(
        _full_packet_for_filtering(), _retrieval_rows(), "How many bikes are owned?"
    )
    previous = build_query_filtered_recall_packet(
        _full_packet_for_filtering(),
        _retrieval_rows(),
        "How many bikes were owned previously before the change?",
    )
    assert _sections(current)["change_history"] == []
    assert [entry["id"] for entry in _sections(previous)["change_history"]] == [
        "history-bikes:0",
        "history-bikes:1",
    ]
    assert previous["selection"]["history_intent"] is True


def test_query_filtered_packet_is_deterministic_and_obeys_small_budget():
    first = build_query_filtered_recall_packet(
        _full_packet_for_filtering(),
        _retrieval_rows(),
        "How many bikes does the user own?",
        max_chars=2_000,
        max_general=1,
    )
    second = build_query_filtered_recall_packet(
        _full_packet_for_filtering(),
        reversed(_retrieval_rows()),
        "How many bikes does the user own?",
        max_chars=2_000,
        max_general=1,
    )
    assert first == second
    assert len(first["text"]) <= 2_000
    assert len(_sections(first)["general_content"]) <= 1


def test_query_filtered_packet_keeps_possible_plan_as_general_content():
    packet = build_query_filtered_recall_packet(
        _full_packet_for_filtering(),
        [
            {
                "rank": 1,
                "uuid": "generic-uuid",
                "name": "future garden",
                "content": "They have been meaning to plant a garden.",
                "memory_type": "content",
            }
        ],
        "What might they do in the garden?",
    )
    assert _sections(packet)["general_content"][0]["id"] == "generic-uuid"
    assert _sections(packet)["general_content"][0]["authority"] == "context"
    assert "lme-" not in packet["text"]


def test_authority_verdict_can_select_typed_view_without_lexical_routing():
    packet = build_query_filtered_recall_packet(
        _full_packet_for_filtering(),
        [
            {
                "rank": 1,
                "uuid": "ranked-context",
                "name": "bicycle background",
                "content": "Background about cycling.",
                "memory_type": "semantic",
            }
        ],
        "Give me the authoritative total.",
        authority_layer=[{"view_uuid": "view-bikes", "status": "leads"}],
    )
    sections = _sections(packet)
    assert [entry["id"] for entry in sections["current_state"]] == ["view-bikes"]
    assert packet["selection"]["selection_mode"] == "durable_ids"


@pytest.mark.parametrize(
    ("query", "uuid", "name", "content", "memory_type"),
    [
        (
            "What approval is required before a production deployment?",
            "governance-policy-7",
            "production deployment approval policy",
            "Production deployments require approval from two reviewers.",
            "policy",
        ),
        (
            "Which parser strategy does this compiler use?",
            "architecture-decision-12",
            "parser architecture decision",
            "The compiler uses a Pratt parser for expression precedence.",
            "decision",
        ),
    ],
)
def test_ranked_governance_and_coding_memories_keep_generic_production_contract(
    query, uuid, name, content, memory_type
):
    packet = build_query_filtered_recall_packet(
        _full_packet_for_filtering(),
        [
            {
                "rank": 1,
                "uuid": uuid,
                "name": name,
                "content": content,
                "memory_type": memory_type,
                "temporal_facts": [],
            }
        ],
        query,
    )
    sections = _sections(packet)
    assert sections["current_state"] == []
    assert sections["change_history"] == []
    assert sections["completed_events"] == []
    assert sections["general_content"] == [
        {
            "id": uuid,
            "kind": memory_type,
            "authority": "context",
            "subject": name,
            "value": content,
            "content": content,
            "valid_at": None,
            "learned_at": None,
            "retrieval_rank": 1,
            "source_ids": [uuid],
            "temporal_facts": [],
        }
    ]
    assert packet["selection"]["selection_mode"] == "durable_ids"
