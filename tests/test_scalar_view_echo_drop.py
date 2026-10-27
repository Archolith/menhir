"""Echo drop alone: observations echoing a verdict slot leave recall, but counted View verdicts keep
their inline items (unlike collapse, which also hides them). Offline, same fakes as the collapse tests.
"""

from __future__ import annotations

import pytest

from menhir.config.feature_flags import FEATURES
from menhir.config.settings_model import MemorySettings
from tests.test_scalar_view_collapse_drilldown import (
    _ids,
    _other_slot_hit,
    _recall,
    _state_view,
    _wire,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_echo_drop_drops_echo_but_counted_verdict_keeps_items(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Fails if echo drop does not drop the echo, or if it also hides counted items like collapse.
    _wire(stub_memory_graph_adapter, state_view=_state_view("counted"), extra_hits=[_other_slot_hit()])
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        scalar_view_echo_drop_enabled=True, counting_enabled=True)
    assert "obs-postcards-1" not in _ids(res)
    assert "obs-other-1" in _ids(res)
    verdict = res.authority_layer[0]
    assert len(verdict.contributors) == 1 and verdict.contributors_total == 1
    assert verdict.contributors_truncated is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_echo_drop_keeps_observation_whose_view_was_skipped(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    # Counted View with counting off is skipped -> no verdict -> the observation stays.
    _wire(stub_memory_graph_adapter, state_view=_state_view("counted"))
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter,
                        scalar_view_echo_drop_enabled=True, counting_enabled=False)
    assert not res.authority_layer
    assert "obs-postcards-1" in _ids(res)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_both_flags_off_keeps_echo(stub_graphiti_client, stub_memory_graph_adapter):
    _wire(stub_memory_graph_adapter, state_view=_state_view())
    res = await _recall(stub_graphiti_client, stub_memory_graph_adapter)
    assert "obs-postcards-1" in _ids(res)


@pytest.mark.unit
def test_echo_drop_flag_registered_default_off_and_needs_authority(monkeypatch):
    feature = FEATURES["personal_memory_scalar_view_echo_drop_enabled"]
    assert feature.env_var == "MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_ECHO_DROP_ENABLED"
    assert feature.default is False
    assert "personal_memory_scalar_state_enabled" in feature.requires
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_ECHO_DROP_ENABLED", "1")
    assert MemorySettings.from_env().personal_memory_scalar_view_echo_drop_enabled is True
