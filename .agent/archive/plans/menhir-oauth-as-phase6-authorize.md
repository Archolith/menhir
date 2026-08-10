# Phase 6 — `/oauth/authorize` + admin-gated consent + PKCE (child plan)

**Parent:** `menhir-embedded-oauth-as-plan.md` (row 6). **Depends on:** P2 client store ✓,
P5 auth-code store ✓. **Handoff context:** `menhir-oauth-as-build-handoff.md`.
**Status:** authored 2026-07-09; build in this session.

This is the hardest, most security-critical phase. The whole endpoint is security logic, so
it is written by hand (per the handoff working pattern); only the **test file** is delegated,
with exact anchors. Flag stays OFF (`MENHIR_OAUTH_AS_ENABLED`) — endpoints 404 when disabled.

## Deliverable

New module `src/menhir/api/oauth_authorize.py` exposing `router` with:
- `GET  /oauth/authorize` — validate params, render admin consent page (HTML).
- `POST /oauth/authorize` — verify admin secret + integrity token, re-validate, issue code,
  302 back to `redirect_uri`.

Wire `oauth_authorize_router` into `create_app` (`api/server.py`) beside the existing
`oauth_as_metadata_router` / `oauth_as_register_router` includes. New tests in
`tests/test_oauth_authorize.py`. CHANGELOG entry. Flip master-plan row 6 to DONE.

## Request contract (GET /oauth/authorize)

Query params (OAuth 2.1 authorization-code + PKCE, public client):

| param | rule |
|---|---|
| `client_id` | REQUIRED; must exist in `get_client_store()`. |
| `redirect_uri` | REQUIRED; must **exactly** match one of the client's registered `redirect_uris` (string equality — no prefix/substring/normalization). |
| `response_type` | REQUIRED; must equal `code`. |
| `code_challenge` | REQUIRED; non-empty. |
| `code_challenge_method` | REQUIRED; must equal `S256` (reject `plain`/others). |
| `scope` | OPTIONAL; if present, every requested scope must be ⊆ the client's granted `scopes`. If absent, default to the client's full granted set. |
| `state` | OPTIONAL; opaque; echoed back on every redirect. |
| `resource` | OPTIONAL (RFC 8707); carried into the code binding for Phase 7 `aud`. |

## Error handling dichotomy (OAuth 2.1 §4.1.2.1) — the load-bearing security rule

- **Untrusted target → do NOT redirect. Return a direct 400 HTML/JSON error.** This applies to:
  - unknown/absent `client_id`
  - `redirect_uri` missing or not an exact match to the registered set

  Redirecting in these cases would hand a code or error to an attacker-chosen URI (open
  redirect / code leak). These are `400` and rendered on Menhir's own page, never a 302.

- **Trusted redirect_uri established, but the request is otherwise invalid → 302 redirect** to
  `redirect_uri?error=<code>&error_description=<...>&state=<state>` with the standard error
  codes:
  - `response_type != code` → `unsupported_response_type`
  - missing `code_challenge` / wrong/absent `code_challenge_method` → `invalid_request`
  - requested scope not ⊆ granted → `invalid_scope`

  `state` is echoed on the error redirect when it was supplied.

## Consent page (GET, all params valid)

Render a minimal self-contained HTML page (no external assets, CSP-safe) showing:
- `client_name` and `client_id` (provenance),
- the requested scopes,
- the `redirect_uri` the code will be sent to.

**XSS:** every interpolated value (`client_name`, `redirect_uri`, `scope`, `state`) is
attacker-influenced (client_name via DCR; redirect_uri/state via query). HTML-escape all text
with `html.escape(..., quote=True)`; escape every hidden-field attribute value. The page is
served to the **admin's** browser and the form takes the admin secret, so an XSS here is
credential theft — escaping is mandatory, not cosmetic.

Form (`method=POST action=/oauth/authorize`):
- hidden fields: `client_id`, `redirect_uri`, `scope` (resolved granted set, space-joined),
  `state`, `code_challenge`, `code_challenge_method`, `resource`.
- hidden `consent_token` — stateless integrity/CSRF token (below).
- `admin_secret` password input.
- `decision` submit buttons: `approve` / `deny`.

## Integrity / CSRF token (stateless — no session cookie until Phase 8)

`consent_token = urlsafe_b64(payload) + "." + urlsafe_b64(HMAC_SHA256(secret, payload_bytes))`
where `payload = json({client_id, redirect_uri, scope, code_challenge, code_challenge_method,
resource, state, iat})`.

- `secret`: per-process random (`secrets.token_bytes(32)`), overridable via
  `MENHIR_OAUTH_AS_CONSENT_SECRET` for tests/multi-restart determinism. Server runs
  `workers=1`, so a per-process key is coherent; tokens are short-lived so restart
  invalidation is acceptable.
- On POST: recompute HMAC (constant-time `hmac.compare_digest`), reject on mismatch; reject if
  `now - iat > MENHIR_OAUTH_AS_CONSENT_TTL_S` (default 300s); and reject if any signed field
  disagrees with the submitted form field. This binds approval to exactly the params shown —
  closes a display-vs-submit `redirect_uri` swap even when both pass validation.
- Ambient-CSRF is already blocked because approval requires the operator secret, which is not
  in a cookie and is never auto-sent by a browser; the token is defense-in-depth + integrity.

## Admin gate + issue (POST /oauth/authorize)

1. Flag check: 404 when `_as_enabled(settings)` is false.
2. Parse form. Verify `consent_token` (sig + freshness + field agreement). On failure → 400
   (do not redirect; the request integrity is unproven).
3. Re-validate everything from scratch exactly as GET does (client exists, redirect_uri exact
   match, response params, scope subset). Untrusted-target failures → 400; trusted-redirect
   protocol failures → 302 error redirect.
4. If `decision == deny` → 302 `redirect_uri?error=access_denied&state=<state>`.
5. Admin secret: constant-time compare `admin_secret` against `settings.operator_key`
   (`MENHIR_OPERATOR_KEY`). If `operator_key` is empty (unconfigured) → **reject** (403, cannot
   approve without a configured admin secret). On mismatch → re-render consent page with an
   error, status 401, **no** code issued, **no** redirect.
6. On approve + valid secret: `subject = "menhir-admin"` (single-admin model). Call
   `get_auth_code_store().issue(client_id=..., redirect_uri=..., scope=<granted space-joined>,
   code_challenge=..., code_challenge_method="S256", resource=..., subject="menhir-admin")` →
   raw code. 302 to `redirect_uri?code=<raw>&state=<state>` (append `state` only when supplied).

## Security musts (Phase 10 will audit these)

- exact `redirect_uri` match (no prefix/substring/scheme coercion); no open redirect.
- PKCE required; only `S256`; `plain` rejected.
- error dichotomy: no redirect on untrusted client_id/redirect_uri.
- consent requires the operator secret; constant-time compare; empty operator_key cannot
  approve.
- integrity token verified (sig + TTL + field agreement).
- all HTML output escaped.
- code carries the approving admin as `subject`; scope stored is the resolved granted set.

## Files

- ADD `src/menhir/api/oauth_authorize.py` (hand-written).
- EDIT `src/menhir/api/server.py` — import + `app.include_router(oauth_authorize_router)`.
- ADD `tests/test_oauth_authorize.py` (delegated with anchors; reviewed).
- EDIT `CHANGELOG.md`.
- EDIT `.agent/plans/menhir-embedded-oauth-as-plan.md` — row 6 → DONE.

## Test matrix (tests/test_oauth_authorize.py)

Mirror `test_oauth_as_register.py` fixture (isolate `oauth_client_store` + `auth_code_store`
singletons via `MENHIR_OAUTH_AS_DIR`; set `MENHIR_OAUTH_AS_CONSENT_SECRET` fixed;
`SimpleNamespace(oauth_as_enabled=True, oauth_public_base_url=..., operator_key="s3cret")`).

1. disabled flag → GET and POST both 404.
2. GET unknown client_id → 400, no redirect.
3. GET redirect_uri not exact-match → 400, no redirect.
4. GET response_type!=code → 302 to redirect_uri with `error=unsupported_response_type` + state.
5. GET missing code_challenge → 302 `error=invalid_request`.
6. GET code_challenge_method=plain → 302 `error=invalid_request`.
7. GET scope not subset → 302 `error=invalid_scope`.
8. GET valid → 200 HTML consent page containing escaped client_name + a `consent_token`.
9. GET client_name with `<script>` → escaped in the page (no raw tag).
10. POST approve + correct secret → 302 to redirect_uri with `code` + `state`; code redeemable
    once via `get_auth_code_store().redeem(...)` and bound to client_id/redirect_uri/subject.
11. POST approve + wrong secret → 401, no code issued (store empty / redeem fails).
12. POST approve + empty operator_key config → 403.
13. POST deny → 302 `error=access_denied` + state.
14. POST tampered consent_token (bad sig) → 400.
15. POST tampered field (redirect_uri differs from signed) → 400.
16. POST expired consent_token (iat old) → 400.
17. POST tampered redirect_uri to an unregistered value → 400 (re-validation, no redirect).

## Out of scope (later phases)

- Consent session cookie / true one-click (Phase 8).
- Token minting (Phase 7 `/oauth/token`).
- Refresh tokens.
