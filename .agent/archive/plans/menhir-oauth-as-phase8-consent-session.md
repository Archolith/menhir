# Phase 8 — consent session cookie (true one-click after first) (child plan)

**Parent:** `menhir-embedded-oauth-as-plan.md` (row 8). **Depends on:** P6 `/authorize` ✓.
**Status:** authored 2026-07-09. Extends `api/oauth_authorize.py` (no new endpoint).

After a successful admin approval, remember it in a signed, HTTP-only, short-TTL cookie so
repeat `GET /oauth/authorize` requests skip the consent page and immediately issue a code —
the standard IdP "already have a session" one-click. Flag stays OFF (`MENHIR_OAUTH_AS_ENABLED`).

## Design

- **Cookie** `menhir_as_session`: value = `b64(payload).b64(HMAC_SHA256(secret, payload))` where
  `payload = {kind:"session", sub:"menhir-admin", iat}`. Signed with the same server secret
  source as the Phase 6 integrity token (`MENHIR_OAUTH_AS_CONSENT_SECRET` / per-process
  random). The `kind:"session"` tag gives domain separation — a consent_token cannot be
  replayed as a session cookie or vice versa.
- **Attributes:** `HttpOnly` (XSS cannot read it), `Secure` when `MENHIR_PUBLIC_BASE_URL` is
  https (loopback http still works), `SameSite=Lax` (sent on top-level GET navigations so the
  connector's redirect one-clicks, but not on cross-site POST — CSRF-safe; the POST already
  requires the operator secret + integrity token), `Path=/oauth/authorize`,
  `Max-Age = MENHIR_OAUTH_AS_SESSION_TTL_S` (default 600s, short).
- **Set** on every successful POST approval (refreshes TTL).
- **One-click GET:** after the normal param validation (client exists, exact redirect_uri,
  PKCE, scope), if a valid unexpired session cookie is present **and** an operator key is still
  configured, issue the code and 302 immediately instead of rendering consent. Subject = the
  cookie's `sub`. Validation always runs first, so a stale cookie never bypasses the
  open-redirect / PKCE / scope checks.

## Security notes (Phase 10 audits)

- one-click still enforces exact redirect_uri match + PKCE presence + scope subset (validation
  precedes the cookie check); the issued code is still PKCE-bound and single-use, so it is
  useless to anyone without the client's `code_verifier`.
- session cannot be forged (HMAC) and expires quickly; `HttpOnly` blocks XSS theft;
  `SameSite=Lax` limits cross-site abuse; requiring a configured operator key means removing
  the admin secret disables one-click even with a live cookie.
- domain-separated from the consent integrity token via the `kind` tag.

## Files

- EDIT `src/menhir/api/oauth_authorize.py` — session sign/verify helpers, shared
  `_issue_code_redirect`, cookie set, one-click branch in `GET`, cookie set on `POST` approve.
- ADD `tests/test_oauth_consent_session.py`.
- EDIT `CHANGELOG.md`; master plan row 8 → DONE; handoff → point to Phase 9.

## Test matrix (tests/test_oauth_consent_session.py)

Reuse the Phase 6 fixtures (isolate client + auth-code singletons; fixed
`MENHIR_OAUTH_AS_CONSENT_SECRET`; settings with `operator_key="s3cret"`).

1. POST approve sets a `menhir_as_session` cookie that is HttpOnly.
2. GET with a valid session cookie → 302 with `code` (+ state), no consent HTML; code redeemable
   with subject `menhir-admin`.
3. GET with a garbage/tampered cookie → 200 consent page (falls through).
4. GET with an expired cookie (`MENHIR_OAUTH_AS_SESSION_TTL_S=-1`) → 200 consent page.
5. One-click still validates: valid cookie + unknown client_id → 400; valid cookie + bad
   redirect_uri → 400.
6. operator_key unconfigured + valid cookie → 200 consent page (no one-click).

## Out of scope

- Phase 9 resource self-wiring + connector E2E (flag turns ON there). Phase 10 audit.
