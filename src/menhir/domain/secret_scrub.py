"""Credential detection and redaction for memory text (issue #577).

This is defence in depth, not a guarantee. A detector misses things: secrets in
formats it has no plugin for, split or re-encoded secrets, and anything an LLM
restates in different words. It also uses no entropy plugins, so an unlabelled
random token is not found. Treat a clean scan as "nothing recognised", never as
"nothing sensitive".

Pure module: no network, no I/O, and secret values are never logged.
"""
from __future__ import annotations

from dataclasses import dataclass

from detect_secrets.core.scan import scan_line
from detect_secrets.settings import transient_settings

_PLUGINS: tuple[str, ...] = (
    "ArtifactoryDetector",
    "AWSKeyDetector",
    "AzureStorageKeyDetector",
    "BasicAuthDetector",
    "CloudantDetector",
    "DiscordBotTokenDetector",
    "GitHubTokenDetector",
    "GitLabTokenDetector",
    "IbmCloudIamDetector",
    "IbmCosHmacDetector",
    "JwtTokenDetector",
    "KeywordDetector",
    "MailchimpDetector",
    "NpmDetector",
    "OpenAIDetector",
    "PrivateKeyDetector",
    "PypiTokenDetector",
    "SendGridDetector",
    "SlackDetector",
    "SoftlayerDetector",
    "SquareOAuthDetector",
    "StripeDetector",
    "TelegramBotTokenDetector",
    "TwilioKeyDetector",
)

_CONFIG: dict[str, list[object]] = {
    "plugins_used": [{"name": name} for name in _PLUGINS],
    "filters_used": [],
}


@dataclass(frozen=True)
class SecretHit:
    kind: str
    value: str


def detect_secrets(text: str) -> list[SecretHit]:
    """Return the distinct credentials recognised in ``text``, in first-seen order."""
    hits: list[SecretHit] = []
    seen: set[tuple[str, str]] = set()
    with transient_settings(_CONFIG):
        for line in text.splitlines():
            for found in scan_line(line):
                value = found.secret_value
                if not value:
                    continue
                key = (found.type, value)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(SecretHit(kind=found.type, value=value))
    return hits


_POLICIES = ("redact", "refuse")
_MINT = object()


class SecretRefused(ValueError):
    """Raised under policy ``refuse``. Carries kinds only, never values."""

    def __init__(self, kinds: tuple[str, ...]) -> None:
        super().__init__(f"memory text contains credential(s): {', '.join(kinds)}")
        self.kinds = kinds


class ScrubbedText(str):
    """Text that has been through ``prepare_memory_text``.

    Only that function can make one (a token guards ``__new__``); ``kinds`` lists the
    credential kinds that were redacted, never the values.
    """

    kinds: tuple[str, ...]

    def __new__(cls, value: str, kinds: tuple[str, ...], _token: object = None) -> "ScrubbedText":
        if _token is not _MINT:
            raise TypeError("ScrubbedText is created only by prepare_memory_text")
        obj = super().__new__(cls, value)
        obj.kinds = kinds
        return obj


def resolve_policy() -> str:
    """The configured policy (``MENHIR_SECRET_POLICY``); invalid values raise."""
    import os

    value = os.environ.get("MENHIR_SECRET_POLICY", "redact").strip().lower() or "redact"
    if value not in _POLICIES:
        raise ValueError("MENHIR_SECRET_POLICY must be 'redact' or 'refuse'")
    return value


def prepare_memory_text(raw: str, *, policy: str | None = None) -> ScrubbedText:
    """The one handler every memory-text writer goes through. Idempotent.

    ``refuse`` raises ``SecretRefused`` (a ``ValueError``) when anything is recognised.
    """
    if isinstance(raw, ScrubbedText):
        return raw
    chosen = policy if policy is not None else resolve_policy()
    if chosen not in _POLICIES:
        raise ValueError("policy must be 'redact' or 'refuse'")
    text, hits = redact(raw)
    kinds = tuple(dict.fromkeys(h.kind for h in hits))
    if hits and chosen == "refuse":
        raise SecretRefused(kinds)
    return ScrubbedText(text, kinds, _MINT)


def ensure_scrubbed(raw: str | None, *, policy: str | None = None) -> ScrubbedText | None:
    """Repository-boundary guard: a plain ``str`` is scrubbed here, never stored as is."""
    if raw is None:
        return None
    return prepare_memory_text(raw, policy=policy)


def redact(text: str) -> tuple[str, list[SecretHit]]:
    """Replace every detected value with ``[REDACTED:<kind>]``; return text and hits."""
    hits = detect_secrets(text)
    if not hits:
        return text, hits
    redacted = text
    for hit in sorted(hits, key=lambda h: len(h.value), reverse=True):
        redacted = redacted.replace(hit.value, f"[REDACTED:{hit.kind}]")
    return redacted, hits
