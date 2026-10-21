"""Model profiles own model-specific behavior; the default profile must change nothing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.infrastructure.model_profiles import (
    DeepSeekProfile,
    Gpt6Profile,
    ModelProfile,
    OpenAIReasoningProfile,
    resolve_model_profile,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("model", "endpoint", "expected"),
    [
        ("gpt-4o-mini", None, "default"),
        ("gpt-4o", "https://api.openai.com/v1", "default"),
        (None, None, "default"),
        ("gpt-6-luna", None, "gpt-6"),
        ("GPT-6-Luna", "https://api.openai.com/v1", "gpt-6"),
        ("gpt-5.6-luna", None, "openai-reasoning"),
        ("o3-mini", None, "openai-reasoning"),
        # OpenRouter slugs: OpenRouter translates params; production prompts stay unchanged.
        ("openai/gpt-5.6-luna", "https://openrouter.ai/api/v1", "default"),
        ("openai/gpt-6-luna", "https://openrouter.ai/api/v1", "default"),
        ("deepseek-v4-flash", None, "deepseek"),
        ("chat-model", "https://api.deepseek.com/v1", "deepseek"),
    ],
)
def test_profile_resolution(model, endpoint, expected) -> None:
    assert resolve_model_profile(model, endpoint=endpoint).name == expected


def test_profile_hierarchy_and_defaults() -> None:
    assert isinstance(resolve_model_profile("gpt-6-luna"), Gpt6Profile)
    assert isinstance(resolve_model_profile("gpt-6-luna"), OpenAIReasoningProfile)
    assert type(resolve_model_profile("gpt-4o-mini")) is ModelProfile
    default = resolve_model_profile("gpt-4o-mini")
    assert default.structured_output_mode == "json_schema"
    assert default.provider_extra_body() == {}
    assert default.extraction_instructions() == ""
    kwargs = {"model": "gpt-4o-mini", "max_tokens": 10, "temperature": 0}
    assert default.shape_request(kwargs, base_url=None) == kwargs


def test_deepseek_profile_behavior() -> None:
    profile = resolve_model_profile("deepseek-v4-flash")
    assert isinstance(profile, DeepSeekProfile)
    assert profile.structured_output_mode == "json_object"
    assert profile.provider_extra_body() == {"thinking": {"type": "disabled"}}
    assert profile.extraction_instructions() == ""


def test_only_gpt6_adds_extraction_instructions() -> None:
    gpt6 = resolve_model_profile("gpt-6-luna").extraction_instructions()
    assert "possessive" in gpt6 and "one edge per distinct fact" in gpt6
    for model in ("gpt-4o-mini", "gpt-5.6-luna", "openai/gpt-6-luna", "deepseek-v4-flash"):
        assert resolve_model_profile(model).extraction_instructions() == ""


# --- extraction prompt wiring -------------------------------------------------------------

import menhir.infrastructure.graphiti_extraction_policy as patches  # noqa: E402


class _FakeClients:
    def __init__(self, model: object, base_url: object = None) -> None:
        self.llm_client = SimpleNamespace(model=model, config=SimpleNamespace(base_url=base_url))

    def model_copy(self, *, update: dict) -> "_FakeClients":
        copy = _FakeClients(None)
        copy.llm_client = update["llm_client"]
        return copy


async def _instructions_for(monkeypatch, clients) -> list[str]:
    """Run a relationless first pass so the repair pass also fires; return both instructions."""
    patches.begin_extraction_receipt(
        "ep-profile", "I'm actually using a new app I recently downloaded.", source_description="user"
    )
    calls: list[str] = []

    async def fake_extract(*args, **kwargs):
        calls.append(kwargs["custom_extraction_instructions"])
        receipt = patches.get_extraction_receipt()
        if len(calls) == 1:
            receipt.raw_entity_count, receipt.raw_edge_count = 1, 0
            return [], [], {}
        receipt.raw_entity_count, receipt.raw_edge_count = 2, 1
        return [SimpleNamespace(name="user"), SimpleNamespace(name="new app")], [object()], {}

    monkeypatch.setattr(patches, "extract_nodes_and_edges", fake_extract)
    try:
        await patches._run_graphiti_combined_extraction(
            clients=clients,
            episode=SimpleNamespace(uuid="ep-profile"),
            previous_episodes=[],
            entity_types=None,
            excluded_entity_types=None,
            custom_extraction_instructions="CALLER CONTRACT",
        )
    finally:
        patches.clear_extraction_receipt()
    return calls


@pytest.mark.asyncio
async def test_gpt6_block_reaches_main_and_repair_passes(monkeypatch) -> None:
    calls = await _instructions_for(monkeypatch, _FakeClients("gpt-6-luna"))
    block = Gpt6Profile().extraction_instructions().strip()
    assert len(calls) == 2
    assert block in calls[0]
    assert block in calls[1]
    assert "CORRECTIVE RE-EXTRACTION" in calls[1]


@pytest.mark.asyncio
async def test_default_profile_instructions_are_byte_identical(monkeypatch) -> None:
    """A default-profile model gets exactly what a client with no model name gets."""
    baseline = await _instructions_for(monkeypatch, object())
    default = await _instructions_for(monkeypatch, _FakeClients("gpt-4o-mini"))
    openrouter = await _instructions_for(
        monkeypatch, _FakeClients("openai/gpt-6-luna", "https://openrouter.ai/api/v1")
    )
    assert default == baseline
    assert openrouter == baseline
    assert "MODEL PRECISION" not in "".join(baseline)
