"""CF-195: the embed provenance stamp carries the model NAME plus the resolved endpoint.

A local llama-server serves a GGUF file under an operator-chosen alias. Swapping the weights while
keeping the alias changes the embedding space with no change to the old model-name-only stamp, so
`backfill_assertion_embeddings` never re-embeds and old and new vectors coexist in one cosine
surface. The stamp now includes the normalized base_url: `f"{base_url}|{embed_model}"`, so the same
model served from a different endpoint is a different embedding space.

This is distinct from the dimension-mismatch case (`graphiti_client.py` catches that); a
same-dimension weight swap produces no signal, so the stamp must carry provenance that does change.

Never put `api_key` in the stamp — it is persisted on every embedded row. Only base_url and model.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure import view_embedder


class _Provider:
    def __init__(
        self,
        *,
        kind: str = "local",
        base_url: str = "http://localhost:8080/v1",
        api_key: str = "",
        embed_model: str = "embed-test",
    ) -> None:
        self.kind = type("K", (), {"value": kind})()
        self.base_url = base_url
        self.api_key = api_key
        self.embed_model = embed_model

    def supports_graphiti_openai_contract(self) -> bool:
        return self.kind.value in {"local", "openai"}


def _patch(monkeypatch, provider) -> None:
    monkeypatch.setattr(
        view_embedder.ProviderConfig,
        "for_graphiti_embedder",
        classmethod(lambda cls, settings: provider),
    )


@pytest.mark.unit
def test_stamp_contains_base_url_and_model_name(monkeypatch) -> None:
    """The stamp carries BOTH the endpoint and the model, joined by a separator."""
    _patch(monkeypatch, _Provider(base_url="http://localhost:8080/v1", embed_model="nomic-embed"))
    assert view_embedder.view_embedder_version(object()) == "http://localhost:8080/v1|nomic-embed"


@pytest.mark.unit
def test_changing_model_changes_stamp(monkeypatch) -> None:
    """Same endpoint, different model -> different stamp (re-embed must fire)."""
    _patch(monkeypatch, _Provider(base_url="http://localhost:8080/v1", embed_model="m1"))
    first = view_embedder.view_embedder_version(object())
    _patch(monkeypatch, _Provider(base_url="http://localhost:8080/v1", embed_model="m2"))
    second = view_embedder.view_embedder_version(object())
    assert first == "http://localhost:8080/v1|m1"
    assert second != first


@pytest.mark.unit
def test_changing_base_url_changes_stamp(monkeypatch) -> None:
    """Same model, different endpoint -> different stamp (the CF-195 case)."""
    _patch(monkeypatch, _Provider(base_url="http://a:1/v1", embed_model="m1"))
    first = view_embedder.view_embedder_version(object())
    _patch(monkeypatch, _Provider(base_url="http://b:2/v1", embed_model="m1"))
    second = view_embedder.view_embedder_version(object())
    assert first == "http://a:1/v1|m1"
    assert second != first


@pytest.mark.unit
def test_trailing_slash_is_normalized(monkeypatch) -> None:
    """`http://x:8080/v1` and `http://x:8080/v1/` must produce the SAME stamp, else a spurious
    corpus-wide re-embed fires on an identical endpoint."""
    _patch(monkeypatch, _Provider(base_url="http://x:8080/v1/", embed_model="m1"))
    with_slash = view_embedder.view_embedder_version(object())
    _patch(monkeypatch, _Provider(base_url="http://x:8080/v1", embed_model="m1"))
    no_slash = view_embedder.view_embedder_version(object())
    assert with_slash == "http://x:8080/v1|m1"
    assert with_slash == no_slash


@pytest.mark.unit
def test_stamp_never_contains_api_key(monkeypatch) -> None:
    """The stamp is persisted on every embedded row; it must never leak the api_key."""
    _patch(
        monkeypatch,
        _Provider(base_url="http://localhost:8080/v1", api_key="CANARY-SECRET-KEY", embed_model="m1"),
    )
    stamp = view_embedder.view_embedder_version(object())
    assert "CANARY-SECRET-KEY" not in stamp


@pytest.mark.unit
def test_userinfo_is_stripped_from_base_url(monkeypatch) -> None:
    """`http://user:pass@host` base_url must not leak its credentials into the persisted stamp."""
    _patch(
        monkeypatch,
        _Provider(base_url="http://user:supersecretpw@localhost:8080/v1", embed_model="m1"),
    )
    stamp = view_embedder.view_embedder_version(object())
    assert "supersecretpw" not in stamp
    assert "user" not in stamp
    assert stamp == "http://localhost:8080/v1|m1"


@pytest.mark.unit
def test_positive_control_normal_provider_returns_stamp(monkeypatch) -> None:
    """POSITIVE CONTROL: a normally-configured provider returns a non-None stamp. Without this every
    assertion above would pass against a function that always returns None."""
    _patch(monkeypatch, _Provider(base_url="http://localhost:8080/v1", embed_model="m1"))
    assert view_embedder.view_embedder_version(object()) is not None


@pytest.mark.unit
def test_no_embed_model_returns_none(monkeypatch) -> None:
    """No embed model configured -> None (write-time embedding skipped, backfill fills later)."""
    _patch(monkeypatch, _Provider(embed_model=""))
    assert view_embedder.view_embedder_version(object()) is None


@pytest.mark.unit
def test_non_openai_provider_returns_none(monkeypatch) -> None:
    """A provider that does not support the OpenAI contract -> None, not a stamp."""
    _patch(monkeypatch, _Provider(kind="gemini", embed_model="m1"))
    assert view_embedder.view_embedder_version(object()) is None


@pytest.mark.unit
def test_raising_provider_resolution_returns_none(monkeypatch) -> None:
    """A ProviderConfig.for_graphiti_embedder that RAISES -> None, never a propagated exception."""

    def _boom(cls, settings):
        raise RuntimeError("misconfigured provider")

    monkeypatch.setattr(view_embedder.ProviderConfig, "for_graphiti_embedder", classmethod(_boom))
    assert view_embedder.view_embedder_version(object()) is None
