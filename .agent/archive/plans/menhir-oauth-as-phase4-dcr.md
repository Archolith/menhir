# Phase 4 — Dynamic Client Registration (`/oauth/register`, RFC 7591)

Parent: `menhir-embedded-oauth-as-plan.md`. Authored 2026-07-09 after Phase 0
(`../reviews/menhir-oauth-as-interop-findings.md`): **Rung 2a, public clients + PKCE S256,
DCR now / CIMD alongside.** Depends on **Phase 2** (the client store this writes to).

**Project:** `projects/archolith/menhir/`.

## Objective

Serve `POST /oauth/register` so connectors (ChatGPT DCR, claude.ai web / Claude Code DCR)
can self-register and obtain a `client_id` with no operator pre-provisioning. Writes to the
Phase 2 `OAuthClientStore`; `/authorize` (Phase 6) and `/token` (Phase 7) consume the
registered identity for provenance.

## Scope decision (read first)

- **In scope:** the RFC 7591 DCR endpoint (public clients, `token_endpoint_auth_method
  = "none"`, PKCE-only). No outbound network I/O.
- **Deferred to a follow-on (Phase 4b), NOT built here:** the **CIMD accept-path**
  (client_id = HTTPS URL the AS fetches). CIMD adds an **outbound fetch = SSRF surface**
  (must enforce https-only, block loopback/private/link-local IPs, cap size + timeout,
  cache) and deserves its own review; bundling it into DCR would widen this endpoint's audit
  surface. Both connectors support DCR out of the box, so DCR alone unblocks one-click.

## Context / anchors

- Store: `src/menhir/api/oauth_client_store.py` — `OAuthClient`, `OAuthClientStore.register`,
  `new_client_id`, `get_client_store()` (singleton bound to `oauth_as_db_path()/menhir_oauth_as.db`).
- Gate + settings pattern: mirror `src/menhir/api/oauth_as_metadata.py` — `_as_enabled(settings)`
  reads `MENHIR_OAUTH_AS_ENABLED`; 404 when the AS is disabled.
- redirect_uri safety: reuse `is_loopback_host` (`src/menhir/config/settings.py`) with
  `urllib.parse.urlparse`. Accept a redirect URI only if scheme is `https`, OR scheme is
  `http` AND host is loopback (OAuth 2.1 §redirect URI rules; native/localhost dev clients).
- Supported scopes: `OAuthConfig.scopes_supported` (via `build_oauth_config(settings)`).
- Router wiring: `app.include_router(...)` beside the others in `src/menhir/api/server.py`.
- `/oauth/*` is under neither `/api/` nor `/mcp` → already outside `BearerAuthMiddleware`
  (DCR is unauthenticated by spec). Confirm against `api/auth.py` path checks.

## Endpoint contract

`POST /oauth/register`, gated by `MENHIR_OAUTH_AS_ENABLED` (404 when off). Request is
RFC 7591 client metadata JSON. Validation (reject → HTTP 400 with
`{"error": "invalid_client_metadata" | "invalid_redirect_uri", "error_description": ...}`):

1. Body must be a JSON object; else `invalid_client_metadata`.
2. `redirect_uris`: required, non-empty list of strings, **max 5**; each must pass the
   https-or-loopback-http check → else `invalid_redirect_uri`.
3. `token_endpoint_auth_method`: if present must be `"none"` (public profile); anything else
   → `invalid_client_metadata`. Default `"none"`.
4. `grant_types` (if present) ⊆ `{authorization_code, refresh_token}`; `response_types`
   (if present) ⊆ `{code}`; else `invalid_client_metadata`.
5. `scope` (space-delimited string, optional): intersect with `scopes_supported`; the granted
   scope is that intersection (default: all supported scopes if omitted). Unknown scopes are
   dropped, not an error (RFC 7591 allows the AS to narrow).
6. `client_name`: optional string, trimmed, length-capped (e.g. 255); default `""`.

On success → **HTTP 201** with the RFC 7591 response:
```json
{
  "client_id": "<16-hex>",
  "client_id_issued_at": <unix int>,
  "redirect_uris": [...],
  "token_endpoint_auth_method": "none",
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "client_name": "<name>",
  "scope": "menhir:read menhir:write menhir:admin"
}
```
No `client_secret` (public clients). Persist via `OAuthClientStore.register(OAuthClient(...))`
with `client_secret_hash=""`, `token_endpoint_auth_method="none"`.

## Tasks

1. `src/menhir/api/oauth_as_register.py`: router + `POST /oauth/register` implementing the
   contract. Meat first: the redirect_uri validator + the metadata validation, then the
   store write + response. Read the store via `get_client_store()`.
2. Register the router in `server.py`.
3. Tests (`tests/test_oauth_as_register.py`), pointing the store at a tmp dir via
   `MENHIR_OAUTH_AS_DIR` + resetting the `get_client_store` singleton:
   - disabled → 404.
   - happy path → 201, `client_id` present, echoes redirect_uris, `token_endpoint_auth_method
     == "none"`, no `client_secret`; the client is retrievable from the store.
   - missing/empty `redirect_uris` → 400 `invalid_client_metadata`.
   - non-https, non-loopback redirect (`http://evil.example.com/cb`) → 400
     `invalid_redirect_uri`; `https://...` and `http://127.0.0.1/cb` accepted.
   - `> 5` redirect_uris → 400.
   - `token_endpoint_auth_method: "client_secret_post"` → 400 `invalid_client_metadata`.
   - unsupported scope narrowed (request `menhir:read foo:bar` → granted `menhir:read`).
   - two registrations get distinct `client_id`s.

## Acceptance criteria

- `MENHIR_OAUTH_AS_ENABLED=false`: 404, no behavior change elsewhere (suite green).
- Enabled: RFC 7591 registration works; only public + PKCE + https/loopback redirects; the
  registered client is durable and usable by later phases. No existing test modified.

## Out of scope

- CIMD accept-path (Phase 4b — SSRF-guarded outbound fetch).
- Client update/delete (`PUT`/`DELETE` on the registration) and registration access tokens.
- Rate limiting the endpoint (tracked separately; DCR is enabled only on public-server tier).
