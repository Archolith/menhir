from __future__ import annotations

import time

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.domain.secret_scrub import SecretHit, detect_secrets, redact

JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)

POSITIVES = {
    "aws": "Use AKIAIOSFODNN7EXAMPLE for the bucket.",
    "github": "token " + "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "openai": "key " + "sk-" + "abcdefghijklmnopqrstT3BlbkFJabcdefghijklmnopqrst",
    "private_key": "-----BEGIN RSA " + "PRIVATE KEY-----",
    "jwt": "bearer " + JWT,
    "keyword": 'password = "hunter2hunter2"',
    "slack": "bot " + "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx",
    "stripe": "key " + "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
}

NEGATIVES = [
    "episode 5eed948d-c960-4585-ad47-20090df008ae is queued",
    "fixed in commit 58d39ced5a1b2c3d4e5f60718293a4b5c6d7e8f9",
    "digest 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "The user prefers dark mode and lives in Paris.",
    "Remember to rotate the password every 90 days.",
    "def token_count(text): return len(text.split())",
]


@pytest.mark.parametrize("name", sorted(POSITIVES))
def test_positive_detected_and_redacted(name: str) -> None:
    text = POSITIVES[name]
    hits = detect_secrets(text)
    assert len(hits) == 1
    assert hits[0].value in text
    out, _ = redact(text)
    assert hits[0].value not in out
    assert f"[REDACTED:{hits[0].kind}]" in out


@pytest.mark.parametrize("text", NEGATIVES)
def test_negative_not_flagged(text: str) -> None:
    assert detect_secrets(text) == []


def test_redact_returns_same_object_when_clean() -> None:
    text = "The user prefers dark mode."
    out, hits = redact(text)
    assert out is text
    assert hits == []


def test_redact_two_secrets_on_separate_lines() -> None:
    a = POSITIVES["aws"]
    b = POSITIVES["github"]
    out, hits = redact(f"{a}\n{b}\n")
    assert len(hits) == 2
    for hit in hits:
        assert hit.value not in out
    assert out.count("[REDACTED:") == 2


def test_redact_repeated_secret_everywhere() -> None:
    text = POSITIVES["aws"] + " and again AKIAIOSFODNN7EXAMPLE"
    out, hits = redact(text)
    assert len(hits) == 1
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert out.count("[REDACTED:") == 2


def test_redact_is_deterministic() -> None:
    text = f"{POSITIVES['aws']}\n{POSITIVES['stripe']}"
    assert redact(text) == redact(text)


def test_secret_hit_is_frozen() -> None:
    hit = SecretHit(kind="k", value="v")
    with pytest.raises(AttributeError):
        hit.value = "x"  # type: ignore[misc]


def test_large_prose_scans_quickly() -> None:
    sentence = "The user prefers dark mode and lives in Paris, near the river. "
    text = (sentence * 800)[:48000]
    start = time.perf_counter()
    assert detect_secrets(text) == []
    assert time.perf_counter() - start < 1.0


def test_secret_policy_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENHIR_SECRET_POLICY", raising=False)
    assert MemorySettings.from_env().secret_policy == "redact"
    monkeypatch.setenv("MENHIR_SECRET_POLICY", " Refuse ")
    assert MemorySettings.from_env().secret_policy == "refuse"
    monkeypatch.setenv("MENHIR_SECRET_POLICY", "banana")
    with pytest.raises(ValueError, match="MENHIR_SECRET_POLICY"):
        MemorySettings.from_env()


def test_off_policy_is_accepted_and_leaves_text_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.domain.secret_scrub import ScrubbedText, non_refusing_policy, prepare_memory_text

    monkeypatch.setenv("MENHIR_SECRET_POLICY", "off")
    assert MemorySettings.from_env().secret_policy == "off"
    text = POSITIVES["aws"]
    out = prepare_memory_text(text)
    assert isinstance(out, ScrubbedText) and str(out) == text and out.kinds == ()
    assert non_refusing_policy() == "off"
    monkeypatch.setenv("MENHIR_SECRET_POLICY", "refuse")
    assert non_refusing_policy() == "redact"
