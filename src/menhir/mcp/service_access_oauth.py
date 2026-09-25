"""OAuth request-context binding and tool-scope denial for MCP service access."""

from __future__ import annotations

from contextvars import ContextVar, Token

from menhir.config.oauth import OAuthConfig
from menhir.core.request_context import get_request_auth_mode

_request_oauth_context: ContextVar[
    tuple[OAuthConfig, frozenset[str]] | None
] = ContextVar("menhir_request_oauth_context", default=None)


class McpOAuthInvocationDenied(PermissionError):
    """An OAuth-only tool tier denial that clients may answer with step-up auth."""

    def __init__(self, *, tool_name: str, minimum_scope: str, challenge: str) -> None:
        self.tool_name = tool_name
        self.minimum_scope = minimum_scope
        self.description = (
            f"Access token lacks the {minimum_scope} scope required for {tool_name}."
        )
        self.challenge = challenge
        super().__init__(self.description)


def bind_request_oauth_context(
    config: OAuthConfig,
    scopes: frozenset[str],
) -> Token[tuple[OAuthConfig, frozenset[str]] | None]:
    """Bind the verified request OAuth snapshot for tool-result challenges."""
    return _request_oauth_context.set((config, frozenset(scopes)))


def reset_request_oauth_context(
    token: Token[tuple[OAuthConfig, frozenset[str]] | None],
) -> None:
    _request_oauth_context.reset(token)


def oauth_tool_scope_denial(
    *,
    tool_name: str,
    minimum_scope: str,
) -> McpOAuthInvocationDenied | None:
    """Build a typed denial only for a verified OAuth request missing tool scope."""
    if get_request_auth_mode() != "oauth":
        return None
    bound = _request_oauth_context.get()
    if bound is None:
        return None
    config, verified_scopes = bound
    if minimum_scope in verified_scopes:
        return None
    description = (
        f"Access token lacks the {minimum_scope} scope required for {tool_name}."
    )
    return McpOAuthInvocationDenied(
        tool_name=tool_name,
        minimum_scope=minimum_scope,
        challenge=config.challenge(
            error="insufficient_scope",
            description=description,
            scope=minimum_scope,
        ),
    )
