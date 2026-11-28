"""#483: a combined-extraction response that is valid JSON but not an object is an
output-parse failure, not a TypeError filed as a code bug."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from graphiti_core.prompts.extract_nodes_and_edges import CombinedExtraction
from graphiti_core.prompts.models import Message

import menhir.infrastructure.graphiti_extraction_policy as policy
from menhir.services.enrichment_failures import (
    classify_enrichment_failure,
    is_graphiti_output_parse_error,
)


async def _respond(payload):
    inner = SimpleNamespace(generate_response=AsyncMock(return_value=payload))
    receipt = policy.begin_extraction_receipt("ep-483", "user: Alice uses Neo4j.")
    proxy = policy._PayloadSanitizingLLMClient(SimpleNamespace(llm_client=inner), receipt)
    try:
        return await proxy.generate_response(
            [Message(role="user", content="Alice uses Neo4j.")], response_model=CombinedExtraction,
        )
    finally:
        policy.clear_extraction_receipt()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[{"name": "Alice"}], "text", 3, None])
async def test_non_object_response_is_an_output_parse_error(payload) -> None:
    with pytest.raises(policy.CombinedExtractionShapeError) as info:
        await _respond(payload)
    err = info.value
    assert is_graphiti_output_parse_error(err)
    assert classify_enrichment_failure(err, error_type=type(err).__name__) == classify_enrichment_failure(
        ValueError("Expecting value: line 1 column 1 (char 0)"), error_type="JSONDecodeError",
    )
    # The retry job classifies the persisted message alone, with no type.
    assert is_graphiti_output_parse_error(str(err))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_object_response_still_reaches_the_sanitizer() -> None:
    out = await _respond({"extracted_entities": [], "edges": []})
    assert isinstance(out, dict) and out.get("edges") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_other_response_models_pass_through_untouched() -> None:
    inner = SimpleNamespace(generate_response=AsyncMock(return_value=["x"]))
    receipt = policy.begin_extraction_receipt("ep-483b", "user: hi")
    proxy = policy._PayloadSanitizingLLMClient(SimpleNamespace(llm_client=inner), receipt)
    try:
        assert await proxy.generate_response([], response_model=None) == ["x"]
    finally:
        policy.clear_extraction_receipt()
