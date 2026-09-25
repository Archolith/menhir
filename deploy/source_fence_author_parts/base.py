"""Shared limits, pinning constants, and the source-fence error type."""

from __future__ import annotations

import re
from datetime import timedelta

MAX_JSON_BYTES = 1024 * 1024
MAX_KEY_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4096
NETWORK_TIMEOUT_SECONDS = 15
EVIDENCE_MAX_AGE = timedelta(minutes=5)
RECEIPT_VALIDITY = timedelta(minutes=5)

_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DNS_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\."
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)

_CHALLENGE_KEYS = frozenset({
    "challenge", "instance_id", "key_id", "mutation_fence", "release_id",
    "runtime_mode", "signature",
})
_REFUSAL = {
    "error": "temporarily_unavailable",
    "error_description": "candidate-readonly mode does not admit authority mutations",
}
_EVIDENCE_COMMON_KEYS = frozenset({
    "schema", "kind", "release_id", "release_manifest_sha256", "source_id",
    "observed_utc",
})


class SourceFenceError(ValueError):
    """A safe, operator-facing source-fence refusal."""
