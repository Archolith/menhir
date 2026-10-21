"""Cloud credential outcome constants and the OpenAI provider configuration check."""

from __future__ import annotations

import logging

CREDENTIAL_VERIFIED = "verified"
CREDENTIAL_REJECTED = "rejected"
CREDENTIAL_UNVERIFIED = "unverified"

logger = logging.getLogger(__name__)


def check_openai_provider_configuration(
    *,
    api_key: str,
    chat_model: str,
    embed_model: str,
    credential_status: str | None = None,
) -> bool:
    """Validate cloud OpenAI provider configuration.

    ``credential_status`` is the shared result of :func:`probe_openai_credential`. Only a
    definite rejection fails the check: a probe that could not reach the provider (blocked
    outbound sockets, a slow network) leaves startup exactly as permissive as before, and
    real request failures are still handled at call time.
    """

    if not api_key.strip():
        logger.error("OpenAI provider is configured but OPENAI_API_KEY is missing.")
        return False
    if not chat_model.strip() and not embed_model.strip():
        logger.error("OpenAI provider is configured but no chat or embedding model is set.")
        return False
    if credential_status == CREDENTIAL_REJECTED:
        logger.error("OpenAI rejected OPENAI_API_KEY (401/403) on GET /models.")
        return False
    logger.info(
        "OpenAI provider configuration %s for startup (chat=%s, embed=%s).",
        "verified" if credential_status == CREDENTIAL_VERIFIED else "accepted (credential not verified)",
        chat_model or "(none)",
        embed_model or "(none)",
    )
    return True
