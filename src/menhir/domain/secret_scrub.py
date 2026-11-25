"""Credential detection and redaction for memory text (issue #577).

This is defence in depth, not a guarantee. A detector misses things: secrets in
formats it has no plugin for, split or re-encoded secrets, and anything an LLM
restates in different words. It also uses no entropy plugins, so an unlabelled
random token is not found. Treat a clean scan as "nothing recognised", never as
"nothing sensitive".

Pure module: no network, no I/O, and secret values are never logged.
"""
from __future__ import annotations

import re
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

# GitHub and GitLab tokens are matched here, not by detect-secrets: its GitHubTokenDetector and
# GitLabTokenDetector capture only the prefix (`ghp`, `glpat`), so redacting their hit left the
# token body in place (GHSA-ppqh-gffg-6j74), and it knows no fine-grained `github_pat_` tokens.
# Each pattern accepts every token those plugins accept, and longer ones; the hit is the full match.
_GITHUB_TOKEN_KIND = "GitHub Token"
_GITLAB_TOKEN_KIND = "GitLab Token"
_FULL_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (_GITHUB_TOKEN_KIND, re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}")),
    (_GITHUB_TOKEN_KIND, re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}")),
    (_GITLAB_TOKEN_KIND, re.compile(r"(?:glpat|gldt|glft|glsoat|glrt)-[A-Za-z0-9_\-]{20,}")),
    (_GITLAB_TOKEN_KIND, re.compile(r"glcbt-(?:[0-9a-fA-F]{2}_)?[A-Za-z0-9_\-]{20,}")),
)


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
            found_pairs = [(found.type, found.secret_value) for found in scan_line(line)]
            found_pairs += [
                (kind, m.group(0))
                for kind, pattern in _FULL_TOKEN_PATTERNS
                for m in pattern.finditer(line)
            ]
            for kind, value in found_pairs:
                if not value:
                    continue
                key = (kind, value)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(SecretHit(kind=kind, value=value))
    return hits


_POLICIES = ("redact", "refuse", "off")
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
        raise ValueError("MENHIR_SECRET_POLICY must be 'redact', 'refuse' or 'off'")
    return value


def non_refusing_policy() -> str:
    """For worker and failure paths: never raise on legacy rows, but honour ``off``."""
    return "off" if resolve_policy() == "off" else "redact"


def prepare_memory_text(raw: str, *, policy: str | None = None) -> ScrubbedText:
    """The one handler every memory-text writer goes through. Idempotent.

    ``refuse`` raises ``SecretRefused`` (a ``ValueError``) when anything is recognised.
    """
    if isinstance(raw, ScrubbedText):
        return raw
    chosen = policy if policy is not None else resolve_policy()
    if chosen not in _POLICIES:
        raise ValueError("policy must be 'redact', 'refuse' or 'off'")
    if chosen == "off":
        return ScrubbedText(raw, (), _MINT)  # operator opted out: store as given
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
