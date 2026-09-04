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
    _patch_graphiti_dedupe_resolutions,
)


def _node(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, uuid=f"uuid-{name}")


async def _drive_gate(
    monkeypatch: pytest.MonkeyPatch,
    llm_payload: object,
    candidate_name: str = "Alice Smith",
) -> object:
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
        SimpleNamespace(existing_nodes=[_node(candidate_name)]),
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
async def test_gate_vetoes_unsupported_merge_beside_a_malformed_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed sibling entry must not disable the veto for the entry beside it."""
    payload = {
        "entity_resolutions": [
            "garbage",
            {"id": 0, "name": "Alice", "duplicate_candidate_id": 0},
        ]
    }
    # Candidate "Zebra Corp" shares no token, acronym or substring with "Alice",
    # so the gate has no positive identity evidence and must override the merge.
    resp = await _drive_gate(monkeypatch, payload, candidate_name="Zebra Corp")

    assert resp["entity_resolutions"][1]["duplicate_candidate_id"] == -1
    assert resp["entity_resolutions"][0] == "garbage"  # left exactly as the LLM sent it


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_allows_supported_merge_beside_a_malformed_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guards must not turn the gate into a blanket veto either."""
    payload = {
        "entity_resolutions": [
            "garbage",
            {"id": 0, "name": "Alice", "duplicate_candidate_id": 0},
        ]
    }
    resp = await _drive_gate(monkeypatch, payload, candidate_name="Alice Smith")

    assert resp["entity_resolutions"][1]["duplicate_candidate_id"] == 0


@pytest.mark.unit
def test_the_same_malformed_payload_survives_the_real_validation_path() -> None:
    """The gate and Graphiti's own validation must agree on what malformed means.

    The gate reads the raw response; PatchedNodeResolutions validates it afterwards.
    A shape that is fatal in either place fails the episode, so the bare-string entry
    that caused the production failure is asserted against the real model here, not
    only against the interceptor.
    """
    import graphiti_core.utils.maintenance.node_operations as node_operations

    _patch_graphiti_dedupe_resolutions()
    node_resolutions = node_operations.NodeResolutions

    parsed = node_resolutions(
        **{"entity_resolutions": ["Alice", None, {"id": 0, "duplicate_candidate_id": 0}]}
    )

    # The unreadable entries are dropped rather than raising, and the usable one survives.
    assert len(parsed.entity_resolutions) == 1
    assert parsed.entity_resolutions[0].id == 0
    assert parsed.entity_resolutions[0].duplicate_candidate_id == 0
