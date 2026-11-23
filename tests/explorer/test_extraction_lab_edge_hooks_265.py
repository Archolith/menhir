"""#265: the Extraction Lab resolves edges with the production Graphiti's hooks, so with the
anchored-time flags on its results match ingest (expiry hook, invalidation hook, preserved
attributes)."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from menhir.explorer import extraction_lab as lab

pytestmark = pytest.mark.unit

EXPIRY = object()
INVALIDATION = object()
PRESERVED = frozenset({"time_expiry", "time_world_end"})


def _graphiti_client(**graphiti_attrs) -> SimpleNamespace:
    clients = SimpleNamespace(llm_client=SimpleNamespace(temperature=0.0))
    client = SimpleNamespace(clients=clients, close=AsyncMock(), **graphiti_attrs)
    return SimpleNamespace(client=client)


async def _run(graphiti_client) -> AsyncMock:
    resolve = AsyncMock(return_value=([], [], []))
    score = lab.ExtractionGoldScore(
        mention_recall=0.0, mention_precision=0.0, proposition_recall=0.0,
        proposition_precision=0.0, update_capture_rate=0.0, unsupported_inference_rate=0.0,
    )
    targets = {
        "menhir.config.MemorySettings.from_env": dict(return_value=None),
        "menhir.infrastructure.graphiti_client.GraphitiClient.from_settings": dict(
            return_value=graphiti_client
        ),
        "menhir.explorer.extraction_lab._apply_extraction_patches": dict(
            new=lambda *a, **k: (lambda: None)
        ),
        "graphiti_core.utils.maintenance.node_operations.extract_nodes": dict(
            new=AsyncMock(return_value=([], {}))
        ),
        "graphiti_core.utils.maintenance.node_operations.resolve_extracted_nodes": dict(
            new=AsyncMock(return_value=([], {}, []))
        ),
        "graphiti_core.utils.maintenance.edge_operations.extract_edges": dict(
            new=AsyncMock(return_value=[])
        ),
        "graphiti_core.utils.maintenance.edge_operations.resolve_extracted_edges": dict(new=resolve),
        "graphiti_core.utils.bulk_utils.resolve_edge_pointers": dict(return_value=[]),
        "menhir.explorer.extraction_lab._score_extraction": dict(new=AsyncMock(return_value=score)),
    }
    arm = lab.ExtractionLabArm(id="a", label="a", tuning=lab.ExtractionLabTuning())
    request = lab.ExtractionLabRequest(current_message="Rachel moved to the suburbs.", arms=[arm])
    with ExitStack() as stack:
        for target, kwargs in targets.items():
            stack.enter_context(patch(target, **kwargs))
        result = await lab._run_extraction_arm(arm, request)
    assert not getattr(result, "error", None), result
    return resolve


async def test_lab_passes_the_production_hooks_to_edge_resolution() -> None:
    resolve = await _run(_graphiti_client(
        edge_expiry_hook=EXPIRY, edge_invalidation_hook=INVALIDATION,
        preserved_edge_attributes=PRESERVED,
    ))
    assert resolve.await_count == 1
    kwargs = resolve.await_args.kwargs
    assert kwargs["edge_expiry_hook"] is EXPIRY
    assert kwargs["edge_invalidation_hook"] is INVALIDATION
    assert kwargs["preserved_attributes"] == PRESERVED


async def test_flags_off_keeps_the_legacy_call_shape() -> None:
    resolve = await _run(_graphiti_client(
        edge_expiry_hook=None, edge_invalidation_hook=None, preserved_edge_attributes=frozenset(),
    ))
    assert resolve.await_count == 1
    kwargs = resolve.await_args.kwargs
    assert not {"edge_expiry_hook", "edge_invalidation_hook", "preserved_attributes"} & set(kwargs)
