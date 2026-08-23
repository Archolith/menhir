"""CF-195 -- the operator's lever for a weight swap the stamp cannot see.

`view_embedder_version` stamps `<base_url>|<embed_model>`. That catches a model change and an
endpoint change. It cannot catch a weight swap behind the SAME alias at the SAME URL: the GGUF
file changes, the embedding space changes with it, and nothing in the configuration moves -- so
`backfill_assertion_embeddings`, which re-embeds rows whose stamp differs, never fires and old and
new vectors coexist in one cosine surface.

The entry's preferred close is a server-side model digest. That stayed unimplemented here and the
reason is recorded rather than assumed: no llama-server was reachable to test whether one is
exposed (nothing on :8083, nothing on the ubuntu-server port either), so the claim that it does not
expose one is INHERITED, not verified. This implements the entry's own listed alternative.

**Why appended and not substituted.** A replacement stamp would let an operator set a constant and
collapse two genuinely different endpoints into one identity -- silently declaring two embedding
spaces equivalent, which is a worse failure than the one being fixed and in the same family. An
appended component can only ever SPLIT one identity into two. The direction of the failure mode is
the whole design, so it is pinned below.
"""

from __future__ import annotations

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.view_embedder import view_embedder_version


def _settings(**overrides) -> MemorySettings:
    base = dict(
        graphiti_embed_provider="openai",
        openai_embed_model="text-embedding-3-small",
        openai_api_key="sk-test",
    )
    base.update(overrides)
    return MemorySettings(**base)


@pytest.mark.unit
def test_setting_the_override_changes_the_stamp() -> None:
    """THE FINDING. Without this there is no way to declare "the weights moved"."""
    before = view_embedder_version(_settings())
    after = view_embedder_version(_settings(embed_version_override="gguf-2026-08-23"))

    assert before is not None and after is not None
    assert after != before, "the override did not reach the stamp"


@pytest.mark.unit
def test_two_different_overrides_are_two_different_identities() -> None:
    """A re-embed is triggered by DIFFERENCE, so two swaps must not collide."""
    first = view_embedder_version(_settings(embed_version_override="weights-a"))
    second = view_embedder_version(_settings(embed_version_override="weights-b"))

    assert first != second


@pytest.mark.unit
def test_an_unset_override_leaves_the_stamp_byte_identical() -> None:
    """Merely shipping this setting must not re-embed the corpus. The stamp drives a corpus-wide
    backfill, so a stamp that changed just by adding the field would be a migration nobody asked
    for -- the same one-time cost this entry already paid once."""
    assert view_embedder_version(_settings()) == "https://api.openai.com/v1|text-embedding-3-small"


@pytest.mark.unit
@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_a_blank_or_whitespace_override_counts_as_absent(blank: str) -> None:
    """`MENHIR_EMBED_VERSION=` in a .env must not silently re-embed everything."""
    assert view_embedder_version(_settings(embed_version_override=blank)) == view_embedder_version(
        _settings()
    )


@pytest.mark.unit
def test_the_override_is_appended_and_cannot_erase_endpoint_or_model() -> None:
    """THE DESIGN CONSTRAINT. If the override were substituted for the stamp, an operator setting
    a constant would merge two different endpoints into one embedding identity -- declaring two
    incompatible vector spaces equivalent. Appending makes that unrepresentable: the endpoint and
    model always remain part of the identity."""
    stamp = view_embedder_version(_settings(embed_version_override="v2"))

    assert stamp is not None
    assert stamp.startswith("https://api.openai.com/v1|text-embedding-3-small")
    assert stamp.endswith("|v2")


@pytest.mark.unit
def test_one_override_cannot_merge_two_distinct_endpoints() -> None:
    """The failure a replacement stamp would allow, asserted directly against the real function."""
    same_override = "pinned"
    a = view_embedder_version(
        _settings(
            graphiti_embed_provider="local",
            local_llm_embed_base_url="http://host-a:8083/v1",
            local_llm_embed_model="nomic-embed-text-v1.5",
            embed_version_override=same_override,
        )
    )
    b = view_embedder_version(
        _settings(
            graphiti_embed_provider="local",
            local_llm_embed_base_url="http://host-b:8083/v1",
            local_llm_embed_model="nomic-embed-text-v1.5",
            embed_version_override=same_override,
        )
    )

    assert a != b, "one override collapsed two endpoints into a single embedding identity"


@pytest.mark.unit
def test_an_unconfigured_provider_still_returns_none_with_an_override_set() -> None:
    """The override must not resurrect a stamp for a provider that cannot embed at all -- None
    means "skip write-time embedding and let the backfill fill later", and a stamp here would
    mark rows as embedded under an identity nothing produced."""
    settings = _settings(graphiti_embed_provider="openai", openai_embed_model="",
                         embed_version_override="v2")

    assert view_embedder_version(settings) is None


@pytest.mark.unit
def test_the_override_is_read_from_the_environment() -> None:
    """The setting is useless if the env var never reaches it."""
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, {"MENHIR_EMBED_VERSION": "from-env-2026"}, clear=False):
        settings = MemorySettings.from_env()

    assert settings.embed_version_override == "from-env-2026"
