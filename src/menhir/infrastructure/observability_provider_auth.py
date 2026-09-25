"""Process-wide record of the most recent LLM provider credential rejection.

Extracted verbatim from ``observability.py``, which re-exports these names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from menhir.infrastructure.observability_usage import LLMCallHandle


@dataclass(frozen=True)
class ProviderAuthFailure:
    """The most recent credential rejection from an LLM provider, process-wide."""

    kind: str
    endpoint: str | None
    model: str | None
    at: str  # ISO-8601 UTC
    detail: str

    def summary(self) -> str:
        return (
            f"the LLM provider rejected our credentials at {self.at} on {self.endpoint or self.kind}"
            f"{f' ({self.model})' if self.model else ''}: {self.detail}"
        )


_provider_auth_failure: ProviderAuthFailure | None = None

_AUTH_ERROR_MARKERS = (
    "invalid_api_key",
    "incorrect api key",
    "authenticationerror",
    "error code: 401",
    "error code: 403",
    "unauthorized",
)


def is_provider_auth_error(error: BaseException) -> bool:
    """True when ``error`` is the provider refusing our credentials (401/403), not a transport fault.

    Matched on the exception class name and message rather than an SDK type so the seam does
    not import a specific provider SDK's exception hierarchy.
    """

    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return True
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def record_provider_auth_failure(handle: LLMCallHandle, error: BaseException) -> None:
    global _provider_auth_failure
    from datetime import datetime, timezone

    detail = str(error).strip().splitlines()[0][:200] if str(error).strip() else type(error).__name__
    _provider_auth_failure = ProviderAuthFailure(
        kind=handle.kind,
        endpoint=handle.endpoint,
        model=handle.model,
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        detail=detail,
    )


def clear_provider_auth_failure() -> None:
    global _provider_auth_failure
    _provider_auth_failure = None


def last_provider_auth_failure() -> ProviderAuthFailure | None:
    """Return the standing credential rejection, or None once a call has succeeded since."""

    return _provider_auth_failure
