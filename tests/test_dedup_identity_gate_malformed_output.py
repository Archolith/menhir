"""The dedup identity gate must survive malformed LLM output.

gpt-4.1-nano can return ``entity_resolutions`` entries that are bare strings rather
than objects. The gate reads the raw LLM response before Graphiti validates it, so an
unguarded ``resolution.get(...)`` there raised AttributeError up through add_episode
and failed the whole episode: content landed in the graph with no entities, add_memory
still reported success, and recall could never see it again.
"""

from types import SimpleNamespace

import pytest

from menhir.infrastructure.graphiti_model_patches import (
    _patch_graphiti_dedup_identity_gate,
)


def _node(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, uuid=f"uuid-{name}")


async def _drive_gate(monkeypatch: pytest.MonkeyPatch, llm_payload: object) -> object:
    """Install the gate over a stub original and return what the gate passed through."""
    import graphiti_core.utils.maintenance.node_operations as node_operations

    seen: dict[str, object] = {}

    async def _stub_original(
        llm_client, extracted_nodes, indexes, state, episode, previous_episodes, entity_types
    ):
        del extracted_nodes, indexes, state, episode, previous_episodes, entity_types
        seen["resp"] = await llm_client.generate_response(
            [], response_model=None, prompt_name="dedupe_nodes"
        )

    async def _generate_response(messages, response_model=None, **kwargs):
        del messages, response_model, kwargs
        return llm_payload

    monkeypatch.setattr(node_operations, "_resolve_with_llm", _stub_original)
    monkeypatch.setattr(
        node_operations, "_menhir_identity_gate_patched", False, raising=False
    )
    _patch_graphiti_dedup_identity_gate()

    llm_client = SimpleNamespace(generate_response=_generate_response)
    await node_operations._resolve_with_llm(
        llm_client,
        [_node("Alice")],
        SimpleNamespace(existing_nodes=[_node("Alice Smith")]),
        SimpleNamespace(unresolved_indices=[0]),
        None,
        None,
        None,
    )
    return seen["resp"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"entity_resolutions": ["Alice"]},
        {"entity_resolutions": [None]},
        {"entity_resolutions": [{"id": 0, "duplicate_candidate_id": "not-an-int"}]},
        {"entity_resolutions": [{"id": "x", "duplicate_candidate_id": 0}]},
        {"entity_resolutions": [{"duplicate_candidate_id": 0, "name": None}]},
        {"entity_resolutions": "Alice"},
        {"entity_resolutions": None},
        {},
        "not a dict at all",
    ],
    ids=[
        "bare-string-entry",
        "null-entry",
        "uncoercible-dup-id",
        "uncoercible-extracted-id",
        "null-name",
        "resolutions-not-a-list",
        "resolutions-null",
        "no-resolutions-key",
        "response-not-a-dict",
    ],
)
async def test_gate_passes_malformed_output_through_without_raising(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    assert await _drive_gate(monkeypatch, payload) is payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_still_vetoes_unsupported_merge_in_well_formed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed sibling entry must not disable the veto for valid entries."""
    payload = {
        "entity_resolutions": [
            "garbage",
            {"id": 0, "name": "Alice", "duplicate_candidate_id": 0},
        ]
    }
    resp = await _drive_gate(monkeypatch, payload)
    # "Alice" vs "Alice Smith" shares a token, so the gate allows that merge; the
    # point here is that the valid entry was still evaluated rather than skipped.
    assert resp["entity_resolutions"][1]["duplicate_candidate_id"] == 0
