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


def redact(text: str) -> tuple[str, list[SecretHit]]:
    """Replace every detected value with ``[REDACTED:<kind>]``; return text and hits."""
    hits = detect_secrets(text)
    if not hits:
        return text, hits
    redacted = text
    for hit in sorted(hits, key=lambda h: len(h.value), reverse=True):
        redacted = redacted.replace(hit.value, f"[REDACTED:{hit.kind}]")
    return redacted, hits
