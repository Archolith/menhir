from __future__ import annotations

import pytest

from menhir.domain.secret_scrub import detect_secrets, prepare_memory_text, redact

# Built by concatenation so the literal is not a scannable token in the repository.
PAT = "github_pat_" + "11ABCDEFG0aBcDeFgHiJkL" + "_" + "m" * 34 +"N0pQrStUvWxYz1234567890AB"

CONTEXTS = [
    f"GITHUB_TOKEN={PAT}",
    f'export GH_TOKEN="{PAT}"',
    f"my pat is {PAT} for the bot",
    f"Authorization: Bearer {PAT}",
]


def test_fixture_has_the_fine_grained_shape() -> None:
    assert len(PAT) == len("github_pat_") + 22 + 1 + 59


@pytest.mark.parametrize("text", CONTEXTS)
def test_fine_grained_pat_redacted(text: str) -> None:
    hits = detect_secrets(text)
    assert [h.value for h in hits if h.kind == "GitHub Token"] == [PAT]
    out, _ = redact(text)
    assert PAT not in out
    assert "[REDACTED:GitHub Token]" in out


def test_fine_grained_pat_redacted_in_stored_memory_text() -> None:
    stored = prepare_memory_text(f"note: use {PAT} for CI", policy="redact")
    assert PAT not in stored
    assert "GitHub Token" in stored.kinds


@pytest.mark.parametrize(
    "text",
    [
        "Fine-grained tokens start with github_pat_ and are longer.",
        "github_pat_short123",
        "see docs/github_pat_rotation.md",
    ],
)
def test_prefix_mentions_not_flagged(text: str) -> None:
    assert detect_secrets(text) == []
