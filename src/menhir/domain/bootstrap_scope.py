"""Bootstrap pin scope parsing and selection policy.

``user_flagged`` remains the lifecycle-retention override.  ``bootstrap_scope``
is a separate, nullable selector controlling whether a retained semantic memory
is injected into startup context.
"""

from __future__ import annotations

import unicodedata


GENERAL_BOOTSTRAP_SCOPE = "general"
WORKSPACE_BOOTSTRAP_PREFIX = "workspace:"
NONE_BOOTSTRAP_SCOPE = "none"


def normalize_workspace_key(value: str | None) -> str:
    """Return the canonical workspace key, or ``""`` when none was supplied."""

    key = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    if not key:
        return ""
    if any(unicodedata.category(char).startswith("C") for char in key):
        raise ValueError("workspace key must not contain control characters")
    return key


def normalize_bootstrap_scope(value: str | None) -> str | None:
    """Normalize a write-side bootstrap scope.

    Empty values and the explicit ``none`` command mean retention-only.  Valid
    pins are ``general`` and ``workspace:<canonical-key>``.
    """

    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw or raw.casefold() == NONE_BOOTSTRAP_SCOPE:
        return None
    if raw.casefold() == GENERAL_BOOTSTRAP_SCOPE:
        return GENERAL_BOOTSTRAP_SCOPE
    if raw.casefold().startswith(WORKSPACE_BOOTSTRAP_PREFIX):
        key = normalize_workspace_key(raw.split(":", 1)[1])
        if not key:
            raise ValueError("workspace bootstrap scope requires a non-empty key")
        return f"{WORKSPACE_BOOTSTRAP_PREFIX}{key}"
    raise ValueError(
        "bootstrap_scope must be empty, 'none', 'general', or 'workspace:<key>'"
    )


def normalize_bootstrap_scope_for_flag(value: str | None, flagged: bool) -> str | None:
    """Normalize a write-side bootstrap scope AND enforce the pin/flagged pair.

    A bootstrap pin is a retained memory injected into startup context; an
    unflagged pin is exactly what lifecycle decay removes, so a pin requires
    ``flagged=True``. Returns the normalized scope (``None`` when there is no
    pin), raising ``ValueError`` when a pin is present but the memory is not
    flagged. The raised message is load-bearing for callers.
    """
    normalized = normalize_bootstrap_scope(value)
    if normalized is not None and not flagged:
        raise ValueError("bootstrap_scope requires flagged=true")
    return normalized


def bootstrap_selection(workspace: str | None) -> tuple[str, list[str]]:
    """Return the receipt key and allowed pin scopes for a bootstrap read."""

    key = normalize_workspace_key(workspace)
    if not key:
        return GENERAL_BOOTSTRAP_SCOPE, [GENERAL_BOOTSTRAP_SCOPE]
    selection_key = f"{WORKSPACE_BOOTSTRAP_PREFIX}{key}"
    return selection_key, [GENERAL_BOOTSTRAP_SCOPE, selection_key]
