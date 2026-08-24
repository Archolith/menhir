---
artifact_schema: 1
artifact_uuid: 304142d9-eacc-49ac-938e-86e4ed7b1b30
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir OAuth refresh + ChatGPT web integration

## Objective

Complete Menhir's embedded OAuth authorization server far enough for a durable ChatGPT web MCP connection. Wire the refresh-token support already implemented in `archolith_oauth` into Menhir, then use a real ChatGPT connection to discover any remaining interoperability gaps.

This is AS-005 completion, not an auth redesign.

## Current state

Already implemented:

- Streamable HTTP MCP at `/mcp-http`.
- OAuth protected-resource metadata and authorization-server discovery.
- Dynamic client registration for public clients.
- Authorization-code flow with mandatory PKCE S256.
- RFC 8707 `resource` binding through authorization and token exchange.
- JWT access tokens, JWKS, issuer/audience/scope validation.
- Single-owner consent gated by `MENHIR_OPERATOR_KEY`.
- OAuth scopes mapped to Menhir authorization tiers.

Missing:

- Menhir-owned refresh-token store configuration.
- `refresh_token` handling at `/oauth/token`.
- `offline_access` / `refresh_token` advertisement when enabled.
- A real ChatGPT end-to-end compatibility proof.

`archolith_oauth` already provides refresh issuance, hashed persistent storage, single-use rotation, replay-family revocation, and `exchange_refresh_token()`. Menhir should consume those primitives rather than reimplement them.

## Non-goals

- Multi-user account management.
- Replacing the current `MENHIR_OPERATOR_KEY` consent gate for the single-owner deployment.
- Replacing the embedded AS with an external identity provider.
- Redesigning Menhir scopes or tiers.
- Changing MCP tool behavior.
- Preemptively adding ChatGPT-specific compatibility code before a real connection demonstrates a need.

## Phase 1 — Refresh store and configuration

### Files

- Add `src/menhir/api/oauth_refresh_store.py`.
- Update `src/menhir/config/settings_model.py`.
- Update `src/menhir/api/server_support.py`.
- Update `.env.example`.

### Work

1. Add `oauth_as_refresh_tokens_enabled`, default `False`.
2. Add `oauth_as_refresh_ttl_s`, default 30 days.
3. Mirror the existing auth-code/client-store configuration pattern and construct `archolith_oauth.RefreshTokenStore` under `oauth_as_db_path(...)`.
4. Configure the store during server prerequisite construction when the embedded AS and refresh support are enabled.
5. Keep refresh disabled by default so existing deployments retain current behavior.

### Acceptance

- Refresh state survives a process restart.
- Refresh tokens are never stored in plaintext.
- Invalid/non-positive refresh TTL configuration fails at startup.
- With the feature disabled, current OAuth behavior is unchanged.

## Phase 2 — Metadata, scopes, and DCR

### Files

- `src/menhir/api/oauth_as_metadata.py`
- `src/menhir/api/oauth_as_register.py`
- Authorization scope resolution only if required by tests.

### Work

1. Pass `issue_refresh_tokens` and `refresh_token_ttl_s` into `AuthorizationServerConfig`.
2. When refresh is enabled, advertise:
   - `grant_types_supported`: `authorization_code`, `refresh_token`.
   - `scopes_supported`: existing Menhir scopes plus `offline_access`.
3. Update DCR responses to advertise `refresh_token` only when the server actually supports it.
4. Allow `offline_access` as an OAuth protocol scope without treating it as a Menhir permission tier.
5. When refresh is disabled, continue advertising authorization-code only.

### Acceptance

- Discovery and DCR truthfully reflect the feature flag.
- `offline_access` is accepted only when refresh support is enabled.
- `offline_access` never grants read/write/admin authority.

## Phase 3 — Token endpoint wiring

### File

- `src/menhir/api/oauth_token.py`

### Work

Branch on `grant_type`:

1. `authorization_code`
   - Preserve the current path.
   - Pass the configured refresh store to `exchange_authorization_code()`.
   - Return a refresh token only when refresh is enabled and the authorization carried `offline_access`.
2. `refresh_token`
   - Require `refresh_token`, `client_id`, and `resource`.
   - Call `exchange_refresh_token()` from `archolith_oauth`.
   - Return the rotated refresh token with the new access token.
3. Preserve existing OAuth error shape and `Cache-Control: no-store` / `Pragma: no-cache` behavior.

### Acceptance

- Code exchange with `offline_access` returns access + refresh tokens.
- Code exchange without `offline_access` returns no refresh token.
- A valid refresh rotates successfully.
- The consumed refresh token cannot be reused.
- Replay causes the token family to be revoked as implemented by `archolith_oauth`.
- Wrong `client_id` or `resource` is rejected.
- New access tokens retain the expected subject, scopes, audience, and client identity.

## Phase 4 — Authorization regression checks

Prove that adding the OAuth protocol scope does not change Menhir authorization semantics:

- `menhir:read`, `menhir:write`, and `menhir:admin` remain the only scopes that determine Menhir tier.
- `offline_access` changes token longevity only.
- Tier-filtered `tools/list` remains unchanged.
- Tool invocation gates remain unchanged.
- Namespace pins and per-client tool allowlists remain unchanged.

## Phase 5 — Automated protocol tests

Add focused tests for:

- Refresh-disabled metadata and DCR compatibility.
- Refresh-enabled metadata and DCR advertisement.
- Authorization-code exchange with and without `offline_access`.
- Successful refresh rotation.
- Replay/family-revocation behavior.
- Refresh persistence across store reconstruction/restart.
- Wrong client/resource rejection.
- Scope-to-tier mapping with `offline_access` present.
- Feature-disabled regression behavior.

Do not duplicate `archolith_oauth`'s internal storage tests; test Menhir's wiring and externally observable contract.

## Phase 6 — Real ChatGPT acceptance test

Deploy an HTTPS-accessible Menhir instance with:

- `MENHIR_PUBLIC_BASE_URL` set to the public origin.
- `MENHIR_OAUTH_AS_ENABLED=true`.
- refresh-token support enabled.
- `MENHIR_OPERATOR_KEY` configured.
- MCP endpoint `/mcp-http`.

Then prove this sequence with ChatGPT web:

1. ChatGPT reaches `/mcp-http` and discovers protected-resource metadata.
2. It discovers the Menhir authorization server and registers its client.
3. It starts authorization with PKCE and the correct `resource`.
4. The owner approves on Menhir's consent page.
5. ChatGPT exchanges the code and receives access + refresh tokens.
6. `tools/list` succeeds and one read/recall tool succeeds.
7. Force or shorten access-token expiry.
8. ChatGPT refreshes without requiring another consent/login.
9. A second MCP call succeeds with the refreshed access token.

Capture the actual ChatGPT DCR payload, redirect URI, requested scopes, and OAuth errors if any. Those observations, not assumptions, decide follow-up work.

## Phase 7 — Integration-driven hardening only

Do not implement these unless the real ChatGPT test demonstrates they are required:

- MCP tool-level `securitySchemes` changes.
- Additional `WWW-Authenticate` behavior beyond the existing resource-server challenge.
- CIMD support.
- ChatGPT-specific proxy/redirect accommodations.
- External IdP or multi-user login.

Any such finding becomes a separately scoped follow-up rather than expanding AS-005 during implementation.

## Rollout

1. Land refresh support default-off.
2. Enable it on the private/test deployment.
3. Run the real ChatGPT acceptance flow.
4. Fix demonstrated interoperability defects only.
5. Enable it on the intended Menhir deployment.
6. Treat any future default-on decision as a separate owner decision.

## Done criteria

This plan is complete when:

- ChatGPT connects to Menhir through OAuth rather than a static/query API key.
- One owner consent survives access-token expiry through refresh.
- Refresh rotation succeeds and replay is rejected.
- No plaintext refresh credential is persisted.
- Access tokens remain correctly bound to `/mcp-http`'s configured resource/audience.
- Existing Menhir tier, namespace, and tool authorization behavior does not regress.
- Automated Menhir wiring tests pass.
- `.env.example` documents the refresh settings.
- The real ChatGPT compatibility run and any observed deviations are recorded.

## Suggested implementation commits

1. `feat(oauth): configure persistent refresh-token storage`
2. `feat(oauth): advertise refresh grant and offline_access`
3. `feat(oauth): wire refresh-token grant into token endpoint`
4. `test(oauth): prove refresh rotation and authorization invariants`
5. `docs(oauth): record ChatGPT MCP compatibility run`
