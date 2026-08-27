"""CF-38: every secret in MemorySettings must be excluded from its repr.

Regression guard against a settings object leaking secret values when it is
repr'd, str'd, or nested inside another container that gets logged or returned.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from menhir.config import MemorySettings

#: The twelve secret-bearing fields that must never appear in a repr.
SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "neo4j_password",
        "local_llm_api_key",
        "openai_api_key",
        "gemini_api_key",
        "api_key",
        "operator_key",
        "agent_key",
        "readonly_key",
        "oauth_as_consent_secret",
        "source_fence_token",
        "langfuse_public_key",
        "langfuse_secret_key",
    }
)

#: Distinct recognizable canary per secret field, keyed by field name.
CANARIES: dict[str, str] = {
    "neo4j_password": "CANARY_NEO4J_PASSWORD",
    "local_llm_api_key": "CANARY_LOCAL_LLM_API_KEY",
    "openai_api_key": "CANARY_OPENAI_API_KEY",
    "gemini_api_key": "CANARY_GEMINI_API_KEY",
    "api_key": "CANARY_API_KEY",
    "operator_key": "CANARY_OPERATOR_KEY",
    "agent_key": "CANARY_AGENT_KEY",
    "readonly_key": "CANARY_READONLY_KEY",
    "oauth_as_consent_secret": "CANARY_OAUTH_AS_CONSENT_SECRET",
    "source_fence_token": "CANARY_SOURCE_FENCE_TOKEN",
    "langfuse_public_key": "CANARY_LANGFUSE_PUBLIC_KEY",
    "langfuse_secret_key": "CANARY_LANGFUSE_SECRET_KEY",
}

#: A non-secret field whose value must still appear in the repr (positive control).
NON_SECRET_FIELD = "neo4j_uri"


def _build_settings() -> MemorySettings:
    kwargs = {name: canary for name, canary in CANARIES.items()}
    return MemorySettings(**kwargs)


@pytest.mark.unit
def test_twelve_secret_fields_have_repr_false() -> None:
    """The repr-hidden field set is EXACTLY the twelve secrets (drift guard)."""
    hidden = {f.name for f in fields(MemorySettings) if f.repr is False}
    assert hidden == SECRET_FIELDS


@pytest.mark.unit
def test_none_of_the_canaries_appear_in_repr() -> None:
    settings = _build_settings()
    rendered = repr(settings)
    for name, canary in CANARIES.items():
        assert canary not in rendered, f"secret field {name} leaked in repr"


@pytest.mark.unit
def test_none_of_the_canaries_appear_in_str() -> None:
    settings = _build_settings()
    rendered = str(settings)
    for name, canary in CANARIES.items():
        assert canary not in rendered, f"secret field {name} leaked in str"


@pytest.mark.unit
def test_none_of_the_canaries_appear_in_fstring() -> None:
    settings = _build_settings()
    rendered = f"{settings}"
    for name, canary in CANARIES.items():
        assert canary not in rendered, f"secret field {name} leaked in f-string"


@pytest.mark.unit
def test_none_of_the_canaries_appear_in_nested_containers() -> None:
    settings = _build_settings()
    for rendered in (repr({"settings": settings}), repr([settings])):
        for name, canary in CANARIES.items():
            assert canary not in rendered, f"secret field {name} leaked in nested container"


@pytest.mark.unit
def test_non_secret_field_still_appears_in_repr() -> None:
    """Positive control: a non-secret value must still render, so an empty
    repr would fail this test rather than vacuously passing the negative ones."""
    settings = MemorySettings(**{name: canary for name, canary in CANARIES.items()})
    assert settings.neo4j_uri in repr(settings)
    assert settings.neo4j_database in repr(settings)


@pytest.mark.unit
def test_repr_starts_with_class_name() -> None:
    """Positive control: the repr is a real MemorySettings repr, not empty."""
    settings = _build_settings()
    assert repr(settings).startswith("MemorySettings(")


@pytest.mark.unit
def test_secret_values_still_readable_by_attribute() -> None:
    """repr=False must not affect normal attribute access."""
    settings = _build_settings()
    for name, canary in CANARIES.items():
        assert getattr(settings, name) == canary
