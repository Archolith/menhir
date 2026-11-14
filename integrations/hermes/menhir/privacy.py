"""Conservative local guard for obvious credentials in prompts and proposed memories.

Hermes already scrubs its provider boundary; this additionally protects direct tool
calls and deployments that omit that host-side scrub. It is not a complete DLP engine.
"""

from __future__ import annotations

import re

_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{15,}\b"),
    re.compile(r"\bAuthorization\s*:\s*Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"\b(?:password|passwd|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[^\s,;]{6,}",
               re.IGNORECASE),
)


def contains_obvious_credentials(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CREDENTIAL_PATTERNS)
