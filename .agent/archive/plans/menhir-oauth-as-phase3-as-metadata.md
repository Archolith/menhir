# Phase 3 — Authorization Server Metadata (RFC 8414)

Parent: `menhir-embedded-oauth-as-plan.md`. Authored after Phase 0
(`../reviews/menhir-oauth-as-interop-findings.md`): **Rung 2a, library = joserfc,
DCR + CIMD, PKCE S256, public clients only.** Depends on **Phase 1** (the JWKS endpoint
the metadata points at). Bite-sized: one endpoint + config gate + tests.

**Project:** `projects/archolith/menhir/`.

## Objective

Serve `/.well-known/oauth-authorization-server` so MCP clients (ChatGPT, claude.ai web,
Claude Code) can discover the embedded AS per RFC 8414. Both connectors resolve this from
the `authorization_servers` entry in the protected-resource metadata; both wait at most
**10 seconds** — the endpoint must be static/instant, no I/O.

## Context / anchors

- Pattern to mirror: `src/menhir/api/oauth_metadata.py` — module-level `APIRouter`,
  `include_in_schema=False`, config gate that 404s when the feature is off. Add the new
  route in a **new module** `src/menhir/api/oauth_as_metadata.py` (the AS is a separate
  concern from RS metadata; keeps Phase 10's audit surface clean).
- Enablement flag: `MENHIR_OAUTH_AS_ENABLED` (new; distinct from `MENHIR_OAUTH_ENABLED`,
  which is the *resource server* switch). Read via the existing `_get_setting` helper
  pattern (`src/menhir/api/oauth.py:48`), settings attr `oauth_as_enabled`.
- Issuer identity: `MENHIR_PUBLIC_BASE_URL` (already read by `build_oauth_config`,
  `oauth.py:151-153`). The AS issuer IS this origin — no separate issuer var.
- Auth exemption: `/.well-known/*` paths must stay outside `BearerAuthMiddleware`
  protection — confirm the existing exempt logic covers the new path the same way it
  covers `/.well-known/oauth-protected-resource` (`src/menhir/api/auth.py`), add to the
  exempt set if it is path-listed rather than prefix-matched.
- Wire-up: `app.include_router(...)` beside `oauth_metadata_router` in
  `src/menhir/api/server.py:141`.

## Metadata document (exact fields)

```json
{
  "issuer": "<MENHIR_PUBLIC_BASE_URL>",
  "authorization_endpoint": "<base>/oauth/authorize",
  "token_endpoint": "<base>/oauth/token",
  "registration_endpoint": "<base>/oauth/register",
  "jwks_uri": "<base>/.well-known/jwks.json",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none"],
  "scopes_supported": ["menhir:read", "menhir:write", "menhir:admin"]
}
```

Notes:
- `token_endpoint_auth_methods_supported: ["none"]` — public clients only (Phase 0
  profile). No client secrets.
- `jwks_uri` points at Phase 1's endpoint. Phases 4/6/7 make the three `/oauth/*`
  endpoints real; advertising them before they exist is why **this endpoint 404s unless
  `MENHIR_OAUTH_AS_ENABLED` is true**, and the flag stays off until Phase 9 wiring.
- `scopes_supported` reuses `OAuthConfig.scopes_supported` (`oauth.py:93`) — one source.

## Tasks

1. `src/menhir/api/oauth_as_metadata.py`: router + `GET
   /.well-known/oauth-authorization-server` (+ path-suffix variant, mirroring the RS
   metadata route shape) returning the document above; 404 when AS disabled; 500 with a
   clear message when `MENHIR_PUBLIC_BASE_URL` is missing while enabled.
2. Add `oauth_as_enabled` handling (env `MENHIR_OAUTH_AS_ENABLED`, default false) — either
   a `MemorySettings` field or `_get_setting`-style env read local to the module; follow
   whichever `build_oauth_config` symmetry is cleaner. It must NOT flip the RS
   `oauth_enabled` behavior.
3. Verify/extend the auth-middleware exemption for the new well-known path; add a test
   asserting it is reachable with zero credentials while `/api/*` stays 401.
4. Register the router in `server.py`.
5. Tests (`tests/test_oauth_as_metadata.py`): disabled→404; enabled→200 with exactly the
   fields above; issuer/endpoints derive from `MENHIR_PUBLIC_BASE_URL`; unauthenticated
   fetch works with OAuth RS mode simultaneously enabled.

## Acceptance criteria

- `MENHIR_OAUTH_AS_ENABLED=false` (default): route 404s; no behavior change anywhere else
  (full existing suite green).
- Enabled: RFC 8414 document served, instant, unauthenticated, fields exactly as spec'd.
- No modification to existing tests.
