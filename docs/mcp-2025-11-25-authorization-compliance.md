# MCP 2025-11-25 Authorization Compliance Roadmap

> **STATUS UPDATE 2026-07-10:** the OAuth resource-server slice this roadmap called for **has since
> landed** (2026-07-09). When `MENHIR_OAUTH_ENABLED=true`, the remote HTTP surfaces validate external
> IdP access tokens (JWT/JWKS, algorithm allowlist, `iss`/`aud`/`exp`, scope→tier), expose
> `/.well-known/oauth-protected-resource`, and emit RFC 6750 `WWW-Authenticate` challenges — the
> authorization-spec direction. RS mode is verified against a live Auth0 tenant. Static bearer/API-key
> middleware remains the **default local-development** path (loopback), not the remote posture. The
> durable auth reference is now `docs/security-posture.md` (§3–§5); the "Current State" below describes
> the pre-OAuth baseline this roadmap planned from. Remaining toward full spec conformance: the
> interactive authorization-code + PKCE browser flow against a real IdP (post-MVP, decision-gated).

## Context

Menhir currently exposes a local stdio MCP surface and remote HTTP MCP surfaces. The stdio transport does not need to use the MCP OAuth authorization flow. The remote HTTP surfaces (`/mcp` for SSE and `/mcp-http` for Streamable HTTP) originally used static bearer/API-key middleware only — protected, but not compliant with the MCP 2025-11-25 Authorization specification. As of 2026-07-09 an OAuth resource-server mode exists (see the status banner above).

This document outlines the changes needed to make the remote HTTP MCP endpoints compliant while preserving the existing stdio experience.

## Current State

Relevant implementation files:

- `src/menhir/api/server.py`
  - Creates the FastAPI app.
  - Mounts `/mcp` and `/mcp-http`.
  - Wraps the app in `BearerAuthMiddleware`.
- `src/menhir/api/auth.py`
  - Accepts configured static bearer keys.
  - Supports `operator`, `agent`, and `readonly` tiers.
  - Allows `?api_key=` query authentication for MCP paths.
- `src/menhir/api/mcp_remote.py`
  - Builds SSE and Streamable HTTP MCP apps.

## Compliance Gaps

### 1. Static API-key auth instead of OAuth 2.1 access-token validation

Current behavior compares the provided bearer token directly against configured static keys. MCP remote HTTP authorization should treat Menhir as an OAuth 2.1 protected resource / resource server and validate access tokens issued by an authorization server.

Needed changes:

- Add an OAuth token validation layer.
- Support JWT validation via authorization-server JWKS where possible.
- Support opaque-token introspection only if explicitly configured.
- Validate issuer, audience/resource, expiration, not-before, scopes, and token type.

### 2. Missing Protected Resource Metadata discovery

Remote MCP endpoints need a Protected Resource Metadata endpoint so clients can discover the resource identifier and authorization servers.

Needed changes:

- Add `/.well-known/oauth-protected-resource`.
- Include metadata for the Menhir MCP resource.
- Include authorization server metadata locations.
- Ensure metadata can represent both `/mcp` and `/mcp-http` if they are treated as distinct resource URLs.

Example shape:

```json
{
  "resource": "https://memory.example.com/mcp-http",
  "authorization_servers": [
    "https://auth.example.com"
  ],
  "scopes_supported": [
    "menhir:read",
    "menhir:write",
    "menhir:admin"
  ],
  "bearer_methods_supported": ["header"]
}
```

### 3. Missing `WWW-Authenticate` challenge metadata

Unauthorized remote MCP responses should include a `WWW-Authenticate` challenge that points clients to the protected-resource metadata.

Needed changes:

- For `401 Unauthorized`, emit a header similar to:

```http
WWW-Authenticate: Bearer resource_metadata="https://memory.example.com/.well-known/oauth-protected-resource"
```

- Include `error`, `error_description`, and `scope` attributes where useful.
- Keep JSON error bodies for existing observability, but do not rely on JSON alone for discovery.

### 4. Query-string token authentication must be removed for MCP endpoints

Current middleware accepts `?api_key=` for `/mcp` and `/mcp-http`. MCP authorization requires bearer tokens in the `Authorization` header. Query-string credentials are also risky because they leak into logs, browser history, referers, and proxies.

Needed changes:

- Remove query-string auth for `/mcp` and `/mcp-http`.
- Optionally keep a short, explicitly documented deprecation window for non-MCP REST endpoints only, if needed.
- Add tests proving `/mcp-http?api_key=...` is rejected.

### 5. Missing audience/resource validation

The remote MCP server should reject tokens that were issued for another resource.

Needed changes:

- Add configured resource identifier(s):
  - `MENHIR_MCP_RESOURCE`
  - Optional separate `MENHIR_MCP_SSE_RESOURCE`
  - Optional separate `MENHIR_MCP_HTTP_RESOURCE`
- Validate token audience/resource claim against the expected MCP resource.
- Reject tokens without a matching audience/resource.

### 6. Scope-to-tier mapping needs to be explicit

The current static-key model maps tokens to `operator`, `agent`, and `readonly` tiers. OAuth should map scopes to the same internal tiers or to finer-grained permissions.

Suggested mapping:

| Scope | Tier | Purpose |
| --- | --- | --- |
| `menhir:read` | `readonly` | Recall and read-only resources |
| `menhir:write` | `agent` | Add memory, ingest, normal agent operations |
| `menhir:admin` | `operator` | Maintenance, lifecycle, privileged tools |

Needed changes:

- Centralize tool permission policy.
- Require scope checks before privileged MCP tool execution.
- Keep existing request context binding (`bind_request_tier`) after OAuth validation.

## Proposed Implementation Plan

### Phase 1: Discovery and challenges

Add:

- `src/menhir/api/oauth_metadata.py`
- Route for `/.well-known/oauth-protected-resource`
- Config fields for:
  - `MENHIR_PUBLIC_BASE_URL`
  - `MENHIR_MCP_RESOURCE`
  - `MENHIR_AUTHORIZATION_SERVERS`
  - `MENHIR_OAUTH_SCOPES_SUPPORTED`
- `WWW-Authenticate` header generation in unauthorized responses.

Tests:

- Metadata endpoint returns valid JSON.
- 401 from `/mcp-http` includes `WWW-Authenticate` with `resource_metadata`.

### Phase 2: OAuth token validation

Add:

- `src/menhir/api/oauth.py`
- JWT verifier backed by JWKS.
- Optional opaque-token introspection provider.
- Config fields for issuer, JWKS URI, introspection URI, client auth, accepted audiences/resources.

Tests:

- Valid token accepted.
- Expired token rejected.
- Wrong issuer rejected.
- Wrong audience/resource rejected.
- Missing required scope rejected.

### Phase 3: Replace remote MCP API-key auth

Change:

- Keep API-key auth for local/private REST API only if still useful.
- Use OAuth middleware for `/mcp` and `/mcp-http`.
- Remove `?api_key=` fallback for MCP endpoints.
- Keep stdio MCP unchanged.

Tests:

- `/mcp-http` rejects query token.
- `/mcp-http` accepts valid header bearer token.
- `/mcp` SSE handshake rejects missing bearer token with proper challenge.

### Phase 4: Documentation and migration

Update:

- `README.md`
- `.env.example`
- Remote MCP setup docs

Document:

- stdio: no OAuth required.
- remote HTTP: OAuth required.
- how to configure authorization server metadata.
- how scopes map to Menhir tiers.
- migration from static API keys.

## Suggested Acceptance Criteria

- [ ] `/.well-known/oauth-protected-resource` exists and advertises Menhir resource metadata.
- [ ] 401 responses for remote MCP endpoints include `WWW-Authenticate: Bearer resource_metadata=...`.
- [ ] Remote MCP endpoints require `Authorization: Bearer <access_token>`.
- [ ] Query-string token auth is rejected for `/mcp` and `/mcp-http`.
- [ ] Tokens are validated for issuer, expiration, audience/resource, and scope.
- [ ] Scope mapping preserves current internal `readonly`, `agent`, and `operator` tiers.
- [ ] Existing stdio MCP behavior is unchanged.
- [ ] Tests cover SSE and Streamable HTTP authorization behavior.

## Security Follow-up

A separate cleanup should rotate and remove any committed local MCP config secrets, bearer tokens, or API keys from repository history and local configuration examples.
